"""Usage history in SQLite (data/netmon.db).

Traffic is added up in memory and written once a minute as per-adapter totals
per local hour, so the disk is barely touched; a crash loses at most a minute.
All database work runs on one background thread, never on the live-data loop.

With packet capture on, each hour also records how much of an adapter's traffic
stayed in the local network (LAN) and how much went to the home country
(domestic), and how much each program used.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from bisect import bisect_right
from pathlib import Path
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from .adapters import Adapter
from .paths import DATA_DIR
from .periods import Period, hour_start
from .speed import Tick

DB_FILE = DATA_DIR / "netmon.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adapters (
    id          INTEGER PRIMARY KEY,
    guid        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    physical    INTEGER NOT NULL
);
-- Bytes per adapter per local hour (ts = start of the hour, unix seconds).
-- rx/tx: the adapter's counters (everything). From packet capture: lan_rx/lan_tx
-- the part that stayed in the local network, irx/itx the internet traffic it saw
-- (hours without the capture have none), drx/dtx the part of that to/from the home country.
CREATE TABLE IF NOT EXISTS usage_hour (
    ts      INTEGER NOT NULL,
    adapter INTEGER NOT NULL REFERENCES adapters(id),
    rx      INTEGER NOT NULL DEFAULT 0,
    tx      INTEGER NOT NULL DEFAULT 0,
    lan_rx  INTEGER NOT NULL DEFAULT 0,
    lan_tx  INTEGER NOT NULL DEFAULT 0,
    drx     INTEGER NOT NULL DEFAULT 0,
    dtx     INTEGER NOT NULL DEFAULT 0,
    irx     INTEGER NOT NULL DEFAULT 0,
    itx     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, adapter)
) WITHOUT ROWID;
-- Programs seen by the packet capture; key = the .exe's path, the services of an svchost...
CREATE TABLE IF NOT EXISTS programs (
    id   INTEGER PRIMARY KEY,
    key  TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    exe  TEXT NOT NULL,
    path TEXT NOT NULL
);
-- Bytes per program per local hour: rx/tx internet, lan_rx/lan_tx local network,
-- drx/dtx the domestic part of the internet traffic.
CREATE TABLE IF NOT EXISTS program_hour (
    ts      INTEGER NOT NULL,
    program INTEGER NOT NULL REFERENCES programs(id),
    rx      INTEGER NOT NULL DEFAULT 0,
    tx      INTEGER NOT NULL DEFAULT 0,
    lan_rx  INTEGER NOT NULL DEFAULT 0,
    lan_tx  INTEGER NOT NULL DEFAULT 0,
    drx     INTEGER NOT NULL DEFAULT 0,
    dtx     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, program)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- The whole network's internet traffic per local hour, from a router via the relay.
-- rx/tx: the interface's exact counters. drx/dtx and arx/atx: domestic and all
-- traffic as the router's counting rules see it; the domestic share is drx/arx.
CREATE TABLE IF NOT EXISTS router_hour (
    ts     INTEGER NOT NULL,
    router TEXT NOT NULL,
    iface  TEXT NOT NULL,
    rx     INTEGER NOT NULL DEFAULT 0,
    tx     INTEGER NOT NULL DEFAULT 0,
    drx    INTEGER NOT NULL DEFAULT 0,
    dtx    INTEGER NOT NULL DEFAULT 0,
    arx    INTEGER NOT NULL DEFAULT 0,
    atx    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, router, iface)
) WITHOUT ROWID;
"""
_ROUTER_VALUES = ("rx", "tx", "drx", "dtx", "arx", "atx")
_CAPTURED = ("lan_rx", "lan_tx", "drx", "dtx", "irx", "itx")      # added to usage_hour by the packet capture
USAGE_VALUES = ("rx", "tx", "drx", "dtx", "irx", "itx", "lan_rx", "lan_tx")  # what totals() and series() give
PROGRAM_VALUES = ("rx", "tx", "lan_rx", "lan_tx", "drx", "dtx")  # per program and hour

log = logging.getLogger(__name__)


def _hour_shares(start: float, end: float) -> list[tuple[int, float]]:
    """The local hours that [start, end] touches, each with its share (0..1) of the window."""
    if end - start <= 0:
        return [(int(hour_start(end)), 1.0)]
    shares = []
    hour = hour_start(start)
    while hour < end:
        following = hour_start(hour + 3600 + 60)  # the next local hour, whatever the clocks do
        overlap = min(end, following) - max(start, hour)
        if overlap > 0:
            shares.append((int(hour), overlap / (end - start)))
        hour = following
    return shares


class History:
    def __init__(self, path=DB_FILE):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")    # readers never wait for the writer
        self._db.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, far fewer disk flushes
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(usage_hour)")}
        for column in _CAPTURED:  # databases from before packet capture
            if column not in columns:
                self._db.execute(f"ALTER TABLE usage_hour ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        self._lock = threading.Lock()
        self._program_ids: dict[str, int] = {}
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history")
        self._adapter_ids: dict[str, int] = {}
        self._pending: dict[str, list[int]] = {}  # guid -> [rx, tx] in the current minute
        self._adapters: dict[str, Adapter] = {}
        self._minute: float | None = None
        self.listeners: list[Callable[[], None]] = []  # run on the history thread after each save

    # ---- recording (called by the monitor once a second, on its loop)

    def on_tick(self, tick: Tick) -> None:
        minute = tick.time // 60 * 60
        if self._minute is not None and minute != self._minute:
            self._flush()
        self._minute = minute
        for a in tick.adapters:
            if a.rx or a.tx:
                totals = self._pending.setdefault(a.adapter.guid, [0, 0])
                totals[0] += a.rx
                totals[1] += a.tx
                self._adapters[a.adapter.guid] = a.adapter

    def _flush(self) -> None:
        if self._pending:
            batch, self._pending = self._pending, {}
            self._worker.submit(self._save, self._minute, batch, dict(self._adapters))

    def _save(self, minute: float, batch: dict[str, list[int]], adapters: dict[str, Adapter]) -> None:
        hour = int(hour_start(minute))
        try:
            with self._lock:
                self._db.execute("BEGIN")
                for guid, (rx, tx) in batch.items():
                    self._db.execute(
                        "INSERT INTO usage_hour (ts, adapter, rx, tx) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (ts, adapter) DO UPDATE SET rx = rx + excluded.rx, tx = tx + excluded.tx",
                        (hour, self._adapter_id(adapters[guid]), rx, tx))
                self._db.execute("COMMIT")
        except sqlite3.Error:
            log.exception("Saving usage failed")
            with self._lock:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
            return
        for listener in self.listeners:
            try:
                listener()
            except Exception:
                log.exception("History listener failed")

    def _adapter_id(self, adapter: Adapter) -> int:
        if adapter.guid not in self._adapter_ids:
            self._db.execute(
                "INSERT INTO adapters (guid, name, description, physical) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guid) DO UPDATE SET name = excluded.name, description = excluded.description, "
                "physical = excluded.physical",
                (adapter.guid, adapter.name, adapter.description, int(adapter.physical)))
            row = self._db.execute("SELECT id FROM adapters WHERE guid = ?", (adapter.guid,)).fetchone()
            self._adapter_ids[adapter.guid] = row[0]
        return self._adapter_ids[adapter.guid]

    def add_capture(self, minute: float, adapters: dict[str, list[int]],
                    programs: dict[str, tuple[tuple[str, str, str], list[int]]]) -> None:
        """A minute of packet capture: per adapter GUID [lan_rx, lan_tx, drx, dtx, irx, itx];
        per program key ((name, exe, path), [rx, tx, lan_rx, lan_tx, drx, dtx])."""
        self._worker.submit(self._save_capture, minute, adapters, programs)

    def _save_capture(self, minute: float, adapters: dict[str, list[int]],
                      programs: dict[str, tuple[tuple[str, str, str], list[int]]]) -> None:
        hour = int(hour_start(minute))
        try:
            with self._lock:
                self._db.execute("BEGIN")
                for guid, values in adapters.items():
                    adapter = self._adapters.get(guid)  # seen by the monitor; captured adapters always are
                    if adapter:
                        self._db.execute(
                            "INSERT INTO usage_hour (ts, adapter, lan_rx, lan_tx, drx, dtx, irx, itx) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (ts, adapter) DO UPDATE SET "
                            "lan_rx = lan_rx + excluded.lan_rx, lan_tx = lan_tx + excluded.lan_tx, "
                            "drx = drx + excluded.drx, dtx = dtx + excluded.dtx, "
                            "irx = irx + excluded.irx, itx = itx + excluded.itx",
                            (hour, self._adapter_id(adapter), *values))
                for key, (details, values) in programs.items():
                    self._db.execute(
                        "INSERT INTO program_hour (ts, program, rx, tx, lan_rx, lan_tx, drx, dtx) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (ts, program) DO UPDATE SET "
                        "rx = rx + excluded.rx, tx = tx + excluded.tx, lan_rx = lan_rx + excluded.lan_rx, "
                        "lan_tx = lan_tx + excluded.lan_tx, drx = drx + excluded.drx, dtx = dtx + excluded.dtx",
                        (hour, self._program_id(key, *details), *values))
                self._db.execute("COMMIT")
        except sqlite3.Error:
            log.exception("Saving the per-program usage failed")
            with self._lock:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")

    def _program_id(self, key: str, name: str, exe: str, path: str) -> int:
        cached = self._program_ids.get(key)
        if cached is None:
            self._db.execute(
                "INSERT INTO programs (key, name, exe, path) VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                "name = excluded.name, exe = excluded.exe, "
                "path = CASE WHEN excluded.path <> '' THEN excluded.path ELSE programs.path END",
                (key, name, exe, path))
            cached = self._program_ids[key] = self._db.execute(
                "SELECT id FROM programs WHERE key = ?", (key,)).fetchone()[0]
        return cached

    def submit(self, fn: Callable, *args) -> Future:
        """Run fn on the history thread (after any pending saves)."""
        return self._worker.submit(fn, *args)

    def close(self) -> None:
        """Save the last partial minute and close the database."""
        self._flush()
        self._worker.shutdown(wait=True)
        with self._lock:
            self._db.close()

    # ---- questions (any thread)

    @staticmethod
    def _scope(adapter: str | None) -> tuple[str, tuple]:
        """Which adapters count: one by GUID, or all physical ones (like the live view)."""
        if adapter:
            return "a.guid = ?", (adapter,)
        return "a.physical = 1", ()

    @staticmethod
    def _sums(exclude_lan: bool) -> str:
        """The USAGE_VALUES summed; rx and tx without the local network's share when asked."""
        rx, tx = ("MAX(h.rx - h.lan_rx, 0)", "MAX(h.tx - h.lan_tx, 0)") if exclude_lan else ("h.rx", "h.tx")
        columns = (rx, tx) + tuple(f"h.{key}" for key in USAGE_VALUES[2:])
        return ", ".join(f"COALESCE(SUM({column}), 0)" for column in columns)

    def totals(self, start: float, end: float, adapter: str | None = None,
               exclude_lan: bool = False) -> dict[str, int]:
        """Bytes between two times: rx/tx, and from the packet capture domestic (drx/dtx),
        internet (irx/itx) and local network (lan_rx/lan_tx)."""
        where, args = self._scope(adapter)
        with self._lock:
            row = self._db.execute(
                f"SELECT {self._sums(exclude_lan)} FROM usage_hour h "
                f"JOIN adapters a ON a.id = h.adapter WHERE h.ts >= ? AND h.ts < ? AND {where}",
                (int(start), int(end), *args)).fetchone()
        return dict(zip(USAGE_VALUES, row))

    def series(self, periods: list[Period], adapter: str | None = None,
               exclude_lan: bool = False) -> list[dict[str, int]]:
        """totals() for each period (sorted, back to back)."""
        if not periods:
            return []
        where, args = self._scope(adapter)
        with self._lock:
            rows = self._db.execute(
                f"SELECT h.ts, {self._sums(exclude_lan)} FROM usage_hour h JOIN adapters a ON a.id = h.adapter "
                f"WHERE h.ts >= ? AND h.ts < ? AND {where} GROUP BY h.ts",
                (int(periods[0].start), int(periods[-1].end), *args)).fetchall()
        starts = [p.start for p in periods]
        result = [[0] * len(USAGE_VALUES) for _ in periods]
        for ts, *values in rows:
            bucket = result[bisect_right(starts, ts) - 1]
            for i, value in enumerate(values):
                bucket[i] += value
        return [dict(zip(USAGE_VALUES, v)) for v in result]

    def program_totals(self, start: float, end: float) -> list[dict]:
        """Each program's bytes between two times, most used first."""
        sums = ", ".join(f"SUM(h.{key})" for key in PROGRAM_VALUES)
        with self._lock:
            rows = self._db.execute(
                f"SELECT p.id, p.key, p.name, p.exe, p.path, {sums} FROM program_hour h "
                f"JOIN programs p ON p.id = h.program WHERE h.ts >= ? AND h.ts < ? GROUP BY p.id "
                f"ORDER BY SUM(h.rx + h.tx + h.lan_rx + h.lan_tx) DESC", (int(start), int(end))).fetchall()
        return [{"id": pid, "key": key, "name": name, "exe": exe, "path": path, **dict(zip(PROGRAM_VALUES, values))}
                for pid, key, name, exe, path, *values in rows]

    def program(self, program_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT key, name, exe, path FROM programs WHERE id = ?", (program_id,)).fetchone()
        return dict(zip(("key", "name", "exe", "path"), row)) if row else None

    def first_capture(self) -> float | None:
        """When per-program usage was first recorded."""
        with self._lock:
            row = self._db.execute("SELECT MIN(ts) FROM program_hour").fetchone()
        return row[0]

    # ---- the router's traffic (whole network)

    def add_router_rows(self, rows: list[dict], last_id: int) -> None:
        """Store relay rows (traffic between two router readings) and remember how far we got.

        A row covers [ts - span, ts]; its bytes are shared out over the local hours
        in that window, so a gap of a few hours doesn't land in a single hour.
        """
        buckets: dict[tuple[int, str, str], list[float]] = {}
        for row in rows:
            row = dict(row)
            # Rows from the first router script have no "all traffic" counters; use the
            # interface counters in their place, so they mix sensibly with newer rows.
            if not row.get("arx") and row.get("drx"):
                row["arx"] = row["rx"]
            if not row.get("atx") and row.get("dtx"):
                row["atx"] = row["tx"]
            for hour, share in _hour_shares(row["ts"] - row["span"], row["ts"]):
                totals = buckets.setdefault((hour, row["router"], row["iface"]), [0.0] * len(_ROUTER_VALUES))
                for i, key in enumerate(_ROUTER_VALUES):
                    totals[i] += row.get(key, 0) * share
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for (hour, router, iface), values in buckets.items():
                    self._db.execute(
                        "INSERT INTO router_hour (ts, router, iface, rx, tx, drx, dtx, arx, atx) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (ts, router, iface) DO UPDATE SET "
                        "rx = rx + excluded.rx, tx = tx + excluded.tx, drx = drx + excluded.drx, "
                        "dtx = dtx + excluded.dtx, arx = arx + excluded.arx, atx = atx + excluded.atx",
                        (hour, router, iface, *(round(v) for v in values)))
                self._db.execute("INSERT INTO meta (key, value) VALUES ('router_last_id', ?) "
                                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(last_id),))
                self._db.execute("COMMIT")
            except sqlite3.Error:
                self._db.execute("ROLLBACK")
                raise

    def router_totals(self, start: float, end: float) -> tuple[int, ...]:
        """(rx, tx, drx, dtx, arx, atx) of the whole network between two times."""
        sums = ", ".join(f"COALESCE(SUM({key}), 0)" for key in _ROUTER_VALUES)
        with self._lock:
            row = self._db.execute(f"SELECT {sums} FROM router_hour WHERE ts >= ? AND ts < ?",
                                   (int(start), int(end))).fetchone()
        return tuple(row)

    def router_series(self, periods: list[Period]) -> list[tuple[int, ...]]:
        if not periods:
            return []
        sums = ", ".join(f"SUM({key})" for key in _ROUTER_VALUES)
        with self._lock:
            rows = self._db.execute(f"SELECT ts, {sums} FROM router_hour WHERE ts >= ? AND ts < ? GROUP BY ts",
                                    (int(periods[0].start), int(periods[-1].end))).fetchall()
        starts = [p.start for p in periods]
        result = [[0] * len(_ROUTER_VALUES) for _ in periods]
        for ts, *values in rows:
            bucket = result[bisect_right(starts, ts) - 1]
            for i, value in enumerate(values):
                bucket[i] += value
        return [tuple(v) for v in result]

    # ---- the Settings page: export, size, delete

    def usage_rows(self) -> list[tuple]:
        """Every hour of every adapter: ts, name, physical, rx, tx, lan_rx, lan_tx, irx, itx, drx, dtx."""
        with self._lock:
            return self._db.execute(
                "SELECT h.ts, a.name, a.physical, h.rx, h.tx, h.lan_rx, h.lan_tx, h.irx, h.itx, h.drx, h.dtx "
                "FROM usage_hour h JOIN adapters a ON a.id = h.adapter ORDER BY h.ts, a.name").fetchall()

    def program_rows(self) -> list[tuple]:
        """Every hour of every program: ts, name, exe, path, rx, tx, lan_rx, lan_tx, drx, dtx."""
        with self._lock:
            return self._db.execute(
                "SELECT h.ts, p.name, p.exe, p.path, h.rx, h.tx, h.lan_rx, h.lan_tx, h.drx, h.dtx "
                "FROM program_hour h JOIN programs p ON p.id = h.program ORDER BY h.ts, p.name").fetchall()

    def router_rows(self) -> list[tuple]:
        with self._lock:
            return self._db.execute("SELECT ts, router, iface, rx, tx, drx, dtx, arx, atx FROM router_hour "
                                    "ORDER BY ts, router, iface").fetchall()

    def size(self) -> int:
        """Bytes the database takes on disk."""
        return sum(f.stat().st_size for f in (self.path, self.path.with_name(self.path.name + "-wal"))
                   if f.exists())

    def clear_usage(self) -> None:
        """Forget this PC's usage history (adapters and programs). The router's data stays."""
        def clear():
            with self._lock:
                self._db.execute("BEGIN")
                for table in ("usage_hour", "program_hour", "programs"):
                    self._db.execute(f"DELETE FROM {table}")
                self._db.execute("DELETE FROM meta WHERE key = 'quota_alerts'")
                self._db.execute("COMMIT")
                self._program_ids.clear()
                self._db.execute("VACUUM")
            log.info("Usage history deleted")
            for listener in self.listeners:
                listener()
        self._worker.submit(clear).result()

    def first_record(self) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT MIN(ts) FROM usage_hour").fetchone()
        return row[0]

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                             "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

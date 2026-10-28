"""Ping history: every ping kept a week, one-minute statistics and outages for good.

The recorder collects each ping (from ping.Pinger) into the minute it belongs to
and writes the minute to the database when the next one starts. Three or more
pings lost in a row make an outage; it lasts until the first answer comes back.
report() reads it all back for the Ping tab.
"""
from __future__ import annotations

import threading
import time
from array import array
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from . import jalali
from .periods import midnight
from .ping import KINDS, Echo

if TYPE_CHECKING:
    from .history import History

OUTAGE_AFTER = 3      # pings lost in a row that make an outage
RAW_DAYS = 7          # each single ping is kept this long; the minute statistics stay
LOST, NOT_SENT = 0xFFFF, 0xFFFE   # in a minute's raw data; anything else is a round trip in ms
_MAX_RTT = 0xFFF0
_PAUSE = 5            # seconds without pings (PC asleep, pinging turned off) end an outage
_PRUNE_EVERY = 3600
_MAX_OUTAGES = 500    # listed on the Ping tab

# The Ping tab's ranges: (seconds shown, seconds per point).
RANGES = {"1h": (3600, 1), "24h": (86400, 60), "7d": (7 * 86400, 600), "30d": (30 * 86400, 3600)}


# ---- a minute's raw data: 60 unsigned 16-bit values, second 0 first

def empty_raw() -> array:
    return array("H", [NOT_SENT] * 60)


def merge_raw(old: bytes, new: bytes) -> bytes:
    """Two recordings of the same minute (NetMon restarted, or the target changed in it)."""
    a, b = array("H", old), array("H", new)
    return array("H", (y if y != NOT_SENT else x for x, y in zip(a, b))).tobytes()


def _percentile(sorted_values: list[float], share: float) -> float:
    """Nearest-rank percentile of sorted values."""
    rank = max(1, -(-len(sorted_values) * share // 1))  # ceil
    return sorted_values[int(rank) - 1]


def _jitter(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return sum(abs(b - a) for a, b in zip(values, values[1:])) / (len(values) - 1)


def minute_stats(raw: bytes) -> dict | None:
    """sent, lost, rtt_min, rtt_avg (0.1 ms), rtt_max, rtt_p95, jitter (0.1 ms) of a minute; None if nothing was sent."""
    values = array("H", raw)
    sent = sum(1 for v in values if v != NOT_SENT)
    if not sent:
        return None
    got = [v for v in values if v < NOT_SENT]
    stats = {"sent": sent, "lost": sent - len(got), "rtt_min": None, "rtt_avg": None, "rtt_max": None,
             "rtt_p95": None, "jitter": None}
    if got:
        ordered = sorted(got)
        jitter = _jitter(got)
        stats.update(rtt_min=ordered[0], rtt_max=ordered[-1], rtt_p95=_percentile(ordered, 0.95),
                     rtt_avg=round(sum(got) / len(got) * 10),
                     jitter=round(jitter * 10) if jitter is not None else None)
    return stats


class _Run:
    """Pings lost in a row - an outage once there are OUTAGE_AFTER of them."""
    def __init__(self, host: str, start: int):
        self.host, self.start, self.last, self.lost = host, start, start, 0


class PingRecorder:
    def __init__(self, history: History):
        self._history = history
        self._lock = threading.Lock()
        self._minutes: dict[str, tuple[int, str, array]] = {}  # kind -> (minute, host, raw) being filled
        self._runs: dict[str, _Run] = {}
        self._pruned = 0.0
        self.listeners: list[Callable[[], None]] = []  # a minute was saved, an outage began or ended

    # ---- recording (on the pinger's threads)

    def on_echo(self, echo: Echo) -> None:
        with self._lock:
            changed = self._add(echo)
            changed |= self._track(echo)
        if changed:
            self._history.submit(self._notify)  # after the writes above

    def _add(self, echo: Echo) -> bool:
        minute = echo.t // 60 * 60
        saved = False
        for kind, (m, host, raw) in list(self._minutes.items()):  # a minute is over: save it
            if m < minute or (kind == echo.kind and host != echo.host):
                self._history.add_ping_minute(kind, m, host, raw.tobytes())
                del self._minutes[kind]
                saved = True
        if echo.kind not in self._minutes:
            self._minutes[echo.kind] = (minute, echo.host, empty_raw())
        raw = self._minutes[echo.kind][2]
        raw[echo.t - minute] = LOST if echo.rtt is None else min(round(echo.rtt), _MAX_RTT)
        if saved and time.time() - self._pruned > _PRUNE_EVERY:
            self._pruned = time.time()
            self._history.submit(self._history.prune_ping, time.time() - RAW_DAYS * 86400)
        return saved

    def _track(self, echo: Echo) -> bool:
        """Notice outages; True if one began or ended."""
        run = self._runs.get(echo.kind)
        changed = False
        if run and (run.host != echo.host or echo.t - run.last > _PAUSE):
            changed = self._end(echo.kind, run.last + 1)  # no pings for a while: it ends where they stopped
            run = None
        if echo.rtt is None:
            if run is None:
                run = self._runs[echo.kind] = _Run(echo.host, echo.t)
            run.lost += 1
            run.last = echo.t
            if run.lost == OUTAGE_AFTER or (run.lost > OUTAGE_AFTER and run.lost % 60 == 0):
                # saved while it lasts too, so a crash still leaves it (with its length so far)
                self._history.save_outage(echo.kind, run.start, run.host, run.lost, None)
                changed |= run.lost == OUTAGE_AFTER
        elif run:
            changed |= self._end(echo.kind, echo.t)
        return changed

    def _end(self, kind: str, ended: int) -> bool:
        run = self._runs.pop(kind)
        if run.lost < OUTAGE_AFTER:
            return False
        self._history.save_outage(kind, run.start, run.host, run.lost, ended)
        return True

    def _notify(self) -> None:
        for listener in self.listeners:
            try:
                listener()
            except Exception:
                pass

    def close(self) -> None:
        """NetMon is exiting: save the minute so far and end any outage where the pings stopped."""
        with self._lock:
            for kind, (minute, host, raw) in self._minutes.items():
                self._history.add_ping_minute(kind, minute, host, raw.tobytes())
            self._minutes.clear()
            for kind in list(self._runs):
                self._end(kind, self._runs[kind].last + 1)

    def clear(self) -> None:
        """The ping history was deleted: forget the minute so far too."""
        with self._lock:
            self._minutes.clear()
            self._runs.clear()

    # ---- for report()

    def pending(self) -> tuple[dict[str, tuple[int, str, bytes]], dict[str, tuple[int, int, int]]]:
        """The minutes being filled, and the outages not ended yet ({kind: (start, lost, last lost ping)})."""
        with self._lock:
            minutes = {kind: (m, host, raw.tobytes()) for kind, (m, host, raw) in self._minutes.items()}
            open_outages = {kind: (r.start, r.lost, r.last) for kind, r in self._runs.items()
                            if r.lost >= OUTAGE_AFTER}
        return minutes, open_outages


# ---- the Ping tab

def _when(t: float) -> str:
    """'1405/08/02 14:32:05' (Jalali date, local time)."""
    moment = datetime.fromtimestamp(t)
    return f"{jalali.format_numeric(moment.date())} {moment:%H:%M:%S}"


def _day_marks(start: float, end: float) -> list[list]:
    """The local days the range touches (the first may begin before it), for the time axis and
    tooltips: [midnight, '21 Mehr', 'Monday 21 Mehr 1405']."""
    marks = []
    day = datetime.fromtimestamp(start).date()
    while True:
        ts = midnight(day)
        if ts >= end:
            return marks
        marks.append([ts, jalali.format_long(day, year=False), jalali.format_long(day, weekday=True)])
        day = datetime.fromtimestamp(ts + 36 * 3600).date()  # the next day, whatever the clocks do


def _cause(outage: dict, gateway_outages: list[dict], gateway_seen: Callable[[int, int], bool]) -> str:
    """Was the local network down too ("local"), or only the internet ("internet")? "" = can't tell."""
    start, end = outage["start"], outage["end"]
    overlap = sum(max(0, min(end, g["end"]) - max(start, g["start"])) for g in gateway_outages)
    if overlap * 2 >= end - start:
        return "local"
    return "internet" if gateway_seen(start, end) else ""


def report(history: History, recorder: PingRecorder, range_name: str, now: float | None = None) -> dict:
    span, step = RANGES[range_name]
    now = int(time.time() if now is None else now)
    end = now // step * step + step  # the point holding "now" is the last one
    start = end - span
    with_raw = step == 1 or span <= RAW_DAYS * 86400  # each ping needed: 1-hour chart, exact p95
    rows = history.ping_minutes(start // 60 * 60, end, with_raw)
    minutes, open_outages = recorder.pending()
    by_key = {(r["kind"], r["ts"]): r for r in rows}
    for kind, (m, host, raw) in minutes.items():  # the minute being filled isn't saved yet
        old = by_key.get((kind, m))
        if old and old.get("raw"):
            raw = merge_raw(old["raw"], raw)
        stats = minute_stats(raw)
        if stats:
            by_key[(kind, m)] = {"kind": kind, "ts": m, "host": host, "raw": raw, **stats}
    rows = sorted(by_key.values(), key=lambda r: r["ts"])

    n = span // step
    series = {kind: {"avg": [None] * n, "lo": [None] * n, "hi": [None] * n, "max": [None] * n,
                     "sent": [0] * n, "lost": [0] * n} for kind in KINDS}
    hosts: dict[str, str] = {}
    totals = {kind: {"sent": 0, "lost": 0, "sum": 0.0, "got": 0, "min": None, "max": None,
                     "jitter_sum": 0.0, "jitter_n": 0, "values": [], "p95_sum": 0.0, "exact": True}
              for kind in KINDS}
    weights = {kind: [0] * n for kind in KINDS}
    for r in rows:
        kind, s = r["kind"], series[r["kind"]]
        hosts[kind] = r["host"]
        tot = totals[kind]
        raw = r.get("raw")
        if step == 1:
            if not raw:
                continue
            for i, v in enumerate(array("H", raw)):
                idx = r["ts"] + i - start
                if v == NOT_SENT or not 0 <= idx < n:
                    continue
                s["sent"][idx] = 1
                if v == LOST:
                    s["lost"][idx] = 1
                    tot["lost"] += 1
                else:
                    s["avg"][idx] = v
                    tot["values"].append(v)
                    tot["sum"] += v
                    tot["got"] += 1
                tot["sent"] += 1
            continue
        idx = (r["ts"] - start) // step
        if not 0 <= idx < n:
            continue
        got = r["sent"] - r["lost"]
        s["sent"][idx] += r["sent"]
        s["lost"][idx] += r["lost"]
        tot["sent"] += r["sent"]
        tot["lost"] += r["lost"]
        if not got:
            continue
        avg = r["rtt_avg"] / 10
        w = weights[kind][idx]
        # line: average; band: the fastest ping up to the minutes' typical p95 (their mean, weighted by replies)
        s["avg"][idx] = avg if s["avg"][idx] is None else (s["avg"][idx] * w + avg * got) / (w + got)
        s["hi"][idx] = r["rtt_p95"] if s["hi"][idx] is None else (s["hi"][idx] * w + r["rtt_p95"] * got) / (w + got)
        weights[kind][idx] = w + got
        s["lo"][idx] = r["rtt_min"] if s["lo"][idx] is None else min(s["lo"][idx], r["rtt_min"])
        s["max"][idx] = r["rtt_max"] if s["max"][idx] is None else max(s["max"][idx], r["rtt_max"])
        tot["sum"] += avg * got
        tot["got"] += got
        tot["min"] = r["rtt_min"] if tot["min"] is None else min(tot["min"], r["rtt_min"])
        tot["max"] = r["rtt_max"] if tot["max"] is None else max(tot["max"], r["rtt_max"])
        if r["jitter"] is not None:
            tot["jitter_sum"] += r["jitter"] / 10 * got
            tot["jitter_n"] += got
        tot["p95_sum"] += r["rtt_p95"] * got
        if raw:
            tot["values"].extend(v for v in array("H", raw) if v < NOT_SENT)
        else:
            tot["exact"] = False

    # Outages touching the range; the ones going on now get their length so far.
    outages = []
    for o in history.ping_outages(start, end):
        run = open_outages.get(o["kind"]) if o["end"] is None else None
        ongoing = False
        if run and run[0] == o["start"]:
            o["lost"] = run[1]
            ongoing = now - run[2] <= _PAUSE  # still losing pings; else pinging stopped during it
            o["end"] = now + 1 if ongoing else run[2] + 1
        elif o["end"] is None:
            o["end"] = o["start"] + o["lost"]
        o["ongoing"] = ongoing
        o["duration"] = o["end"] - o["start"]
        o["label"] = _when(o["start"])
        outages.append(o)
    gateway = [o for o in outages if o["kind"] == "gateway"]
    gateway_minutes = sorted(r["ts"] for r in rows if r["kind"] == "gateway" and r["sent"])

    def gateway_seen(a: int, b: int) -> bool:
        return any(a - 60 < ts < b for ts in gateway_minutes)

    for o in outages:
        o["cause"] = _cause(o, gateway, gateway_seen) if o["kind"] == "internet" else "local"

    stats = {}
    for kind in KINDS:
        tot = totals[kind]
        values = sorted(tot["values"])
        if step == 1:
            exact, p95 = True, (_percentile(values, 0.95) if values else None)
            jitter = _jitter([v for v in series[kind]["avg"] if v is not None])
            low, high = (values[0], values[-1]) if values else (None, None)
        else:
            exact = tot["exact"] and len(values) == tot["got"]
            p95 = (_percentile(values, 0.95) if exact and values
                   else (tot["p95_sum"] / tot["got"] if tot["got"] else None))
            jitter = tot["jitter_sum"] / tot["jitter_n"] if tot["jitter_n"] else None
            low, high = tot["min"], tot["max"]
        mine = [o for o in outages if o["kind"] == kind]
        stats[kind] = {
            "sent": tot["sent"], "lost": tot["lost"],
            "loss": 100 * tot["lost"] / tot["sent"] if tot["sent"] else None,
            "average": tot["sum"] / tot["got"] if tot["got"] else None,
            "best": low, "worst": high, "p95": p95, "p95_exact": exact, "jitter": jitter,
            "outages": len(mine),
            "outage_time": sum(min(o["end"], end) - max(o["start"], start) for o in mine),
            "longest": max((o["duration"] for o in mine), default=0),
        }
    outages.sort(key=lambda o: o["start"], reverse=True)
    first = history.ping_first()
    return {"range": range_name, "start": start, "end": end, "step": step, "now": now,
            "series": series, "stats": stats, "hosts": hosts, "outages": outages[:_MAX_OUTAGES],
            "more_outages": max(0, len(outages) - _MAX_OUTAGES), "days": _day_marks(start, end),
            "first": first, "first_label": _when(first) if first else ""}

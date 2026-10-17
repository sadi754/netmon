"""Which program uses how much: every packet on the physical adapters, counted per program.

Npcap captures the headers of each packet (see npcap.py). The ports in them tell
which socket - and so which program - a packet belongs to (Windows' connection
table, see connections.py). Once a second this gives live speeds per program and
per connection; once a minute the totals go to the usage history, split into
internet / local network (LAN) / domestic (the home country).

Only what NetMon needs is kept: addresses, ports and sizes. Nothing leaves the PC.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

from . import npcap, winutil
from .adapters import Adapter
from .apps import App, AppCatalog
from .connections import list_sockets
from .geoip import GeoIP, scope
from .history import History
from .names import Names
from .settings import Settings
from .speed import Tick

INTERVAL = 1.0      # seconds between updates
FLOW_IDLE = 120     # seconds before a quiet flow is forgotten
RESOLVE_FOR = 5     # seconds to keep looking for a new flow's program
LIVE_SECONDS = 3    # live speeds are averaged over this many seconds
RETRY_AFTER = 30    # seconds before trying an adapter again that failed to open
SNOOP_EVERY = 0.1   # new flows: read the connection table at most this often...
SNOOP_KEEP = 10     # ... and remember who owned which socket for this long (short connections)

UNKNOWN = App(id=-1, key="unknown", exe="", name="Unknown program",
              note="Traffic of connections that closed before NetMon could see which program had them.")
OTHER = App(id=-2, key="other", exe="", name="Other traffic",
            note="Packets without a program: ping (ICMP), ARP, router announcements and similar.")
_LAN_SCOPES = ("loopback", "lan", "multicast")
_PROTO_NAMES = {6: "TCP", 17: "UDP"}

log = logging.getLogger(__name__)


def _count(batch: bytes, mac: bytes, counts: dict) -> None:
    """Add up one batch of captured frames per flow: key -> [received, sent] bytes on the wire.
    key = (protocol, local IP, local port, remote IP, remote port); IPs as raw bytes."""
    for offset, captured, wire in npcap.iterate(batch):
        b = batch
        ethertype = b[offset + 12] << 8 | b[offset + 13]
        ip = offset + 14
        if ethertype == 0x8100:  # VLAN tag
            ethertype = b[offset + 16] << 8 | b[offset + 17]
            ip += 4
        sent = b[offset + 6:offset + 12] == mac
        if ethertype == 0x0800:
            proto = b[ip + 9]
            src, dst = b[ip + 12:ip + 16], b[ip + 16:ip + 20]
            l4 = ip + (b[ip] & 15) * 4
            fragment = (b[ip + 6] & 0x1F) | b[ip + 7]  # a later piece of a split packet has no ports
        elif ethertype == 0x86DD:
            proto = b[ip + 6]
            src, dst = b[ip + 8:ip + 24], b[ip + 24:ip + 40]
            l4 = ip + 40
            fragment = 0
        else:
            key = (-1, b"", 0, b"", 0)  # not IP: ARP, LLDP...
            counts.setdefault(key, [0, 0])[sent] += wire
            continue
        if (proto == 6 or proto == 17) and not fragment and l4 + 4 <= offset + captured:
            sport, dport = b[l4] << 8 | b[l4 + 1], b[l4 + 2] << 8 | b[l4 + 3]
        else:
            sport = dport = 0
        if sent:
            key = (proto, src, sport, dst, dport)
        else:
            key = (proto, dst, dport, src, sport)
        counts.setdefault(key, [0, 0])[sent] += wire


def _ip(raw: bytes) -> str:
    if not raw:
        return ""
    return socket.inet_ntop(socket.AF_INET if len(raw) == 4 else socket.AF_INET6, raw)


class Flow:
    """Traffic between one local and one remote address/port pair."""
    __slots__ = ("proto", "local", "lport", "remote", "rport", "first", "last", "rx", "tx",
                 "app", "pid", "lan", "cc", "adapter", "held", "recent")

    def __init__(self, key: tuple, now: float, adapter: str):
        proto, local, self.lport, remote, self.rport = key
        self.proto = proto
        self.local, self.remote = _ip(local), _ip(remote)
        self.first = self.last = now
        self.rx = self.tx = 0                # bytes so far
        self.app: App | None = None          # None while its program is still being looked for
        self.pid = -1
        kind = scope(self.remote) if self.remote else "lan"
        self.lan = kind in _LAN_SCOPES
        self.cc = ""                         # country of the remote address (internet only)
        self.adapter = adapter
        self.held = [0, 0]                   # bytes waiting until the program is known
        self.recent = [0, 0, 0, 0]           # bytes received, sent: previous second, this second

    @property
    def row_key(self) -> str:
        return f"{_PROTO_NAMES.get(self.proto, self.proto)}|{self.local}|{self.lport}|{self.remote}|{self.rport}"


class _AdapterCapture:
    """Two capture threads for one adapter: one counts every packet, one reads names (DNS, TLS)."""

    def __init__(self, monitor: TrafficMonitor, adapter: Adapter):
        self.adapter = adapter
        self.stop = threading.Event()
        self.error = ""
        self._monitor = monitor
        threading.Thread(target=self._count, name=f"capture-{adapter.name}", daemon=True).start()
        threading.Thread(target=self._names, name=f"names-{adapter.name}", daemon=True).start()

    def _count(self) -> None:
        try:
            capture = npcap.Capture(self.adapter.guid)
        except npcap.NpcapError as e:
            self.error = str(e)
            self._monitor.capture_failed(self, str(e))
            return
        mac, guid = self.adapter.mac, self.adapter.guid
        dropped_seen, checked = 0, time.monotonic()
        try:
            while not self.stop.is_set():
                batch = capture.read()
                if batch:
                    counts: dict = {}
                    _count(batch, mac, counts)
                    self._monitor.add_counts(guid, counts)
                if time.monotonic() - checked > 30:
                    checked, dropped = time.monotonic(), capture.dropped()
                    if dropped > dropped_seen:
                        log.warning("Npcap dropped %d packets on %s", dropped - dropped_seen, self.adapter.name)
                        dropped_seen = dropped
        except npcap.NpcapError as e:
            self.error = str(e)
            self._monitor.capture_failed(self, str(e))
        except Exception:
            log.exception("Packet capture on %s failed", self.adapter.name)
            self.error = "The packet capture stopped unexpectedly - see data/netmon.log."
            self._monitor.capture_failed(self, self.error)
        finally:
            capture.close()

    def _names(self) -> None:
        try:
            capture = npcap.Capture(self.adapter.guid, npcap.NAMES, npcap.NAMES_FILTER)
        except npcap.NpcapError:
            return  # the counting thread reports problems
        names = self._monitor.names
        try:
            while not self.stop.is_set():
                batch = capture.read()
                for offset, captured, _ in npcap.iterate(batch):
                    names.learn(batch[offset:offset + captured])
        except Exception:
            log.debug("Name capture on %s stopped", self.adapter.name, exc_info=True)
        finally:
            capture.close()


class TrafficMonitor:
    def __init__(self, settings: Settings, geoip: GeoIP, apps: AppCatalog, names: Names, history: History):
        self._settings = settings
        self._geoip = geoip
        self._apps = apps
        self.names = names
        self._history = history
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}            # adapter GUID -> flow key -> [rx, tx], since the last update
        self._flows: dict[tuple, Flow] = {}
        self._unresolved: list[Flow] = []
        self._captures: dict[str, _AdapterCapture] = {}
        self._wanted: dict[str, Adapter] = {}          # physical adapters that are up (from the monitor)
        self._failed: dict[str, tuple[float, str]] = {}  # GUID -> (when capturing failed, why)
        self._update_lock = threading.Lock()
        self._owners: dict[tuple, tuple[int, float]] = {}  # socket -> (pid, when seen), from recent snoops
        self._snoop = False                            # a new flow appeared: look at the table soon
        self._snooped = 0.0
        self._owners_pruned = 0.0
        self._countries: dict[str, str] = {}
        self._live: dict[int, list] = {}               # app id -> [app, [rx, tx] per second, newest last]
        self._minute: float | None = None
        self._minute_adapters: dict[str, list[int]] = {}
        self._minute_programs: dict[str, tuple[tuple[str, str, str], list[int]]] = {}
        self._wake = threading.Event()
        self.status: dict = {"state": "off"}
        self.flow_view: dict[str, dict] = {}           # for the Connections tab: row key -> speeds and totals
        self.udp_flows: list[dict] = []                # UDP traffic, which Windows' table can't show
        self.programs: list[dict] = []                 # live speeds per program, busiest first
        self.status_listeners: list[Callable[[], None]] = []
        self.live_listeners: list[Callable[[], None]] = []
        threading.Thread(target=self._run, name="traffic", daemon=True).start()

    # ---- called from other threads

    def on_tick(self, tick: Tick) -> None:
        """Once a second from the monitor: which physical adapters are up."""
        wanted = {a.adapter.guid: a.adapter for a in tick.adapters if a.adapter.physical and a.adapter.up}
        if wanted.keys() != self._wanted.keys():
            self._wanted = wanted
            self._wake.set()

    def apply_settings(self) -> None:
        self._wake.set()

    def add_counts(self, guid: str, counts: dict) -> None:
        new = False
        with self._lock:
            pending = self._pending.setdefault(guid, {})
            for key, (rx, tx) in counts.items():
                totals = pending.get(key)
                if totals:
                    totals[0] += rx
                    totals[1] += tx
                else:
                    pending[key] = [rx, tx]
                    new = new or key not in self._flows
        if new:  # catch its program while the connection is still open, even if it's a short one
            self._snoop = True
            self._wake.set()

    def capture_failed(self, capture: _AdapterCapture, message: str) -> None:
        log.warning("Capture on %s: %s", capture.adapter.name, message)
        self._failed[capture.adapter.guid] = (time.monotonic(), message)
        self._wake.set()

    # ---- the update thread

    def _run(self) -> None:
        next_update = time.monotonic()
        while True:
            self._wake.wait(max(0.0, next_update - time.monotonic()))
            self._wake.clear()
            try:
                with self._update_lock:
                    if self._snoop and time.monotonic() - self._snooped >= SNOOP_EVERY:
                        self._read_owners()
                    self._manage_captures()
                    if time.monotonic() >= next_update:
                        next_update = time.monotonic() + INTERVAL
                        self._update(time.time())
            except Exception:
                log.exception("Traffic update failed")

    def _set_status(self, status: dict) -> None:
        if status != self.status:
            self.status = status
            for listener in self.status_listeners:
                try:
                    listener()
                except Exception:
                    log.exception("Capture status listener failed")

    def _manage_captures(self) -> None:
        """Start and stop the capture threads to match the settings and the adapters that are up."""
        if not self._settings.capture_enabled:
            self._stop_all()
            self._set_status({"state": "off"})
            return
        try:
            version = npcap.version()
        except npcap.NpcapError as e:
            self._stop_all()
            self._set_status({"state": "missing", "message": str(e)})
            return
        wanted, now = dict(self._wanted), time.monotonic()
        for guid in [g for g in self._captures if g not in wanted or self._captures[g].error]:
            self._captures.pop(guid).stop.set()
        for guid, adapter in wanted.items():
            failed_at = self._failed.get(guid, (-RETRY_AFTER, ""))[0]
            if guid not in self._captures and now - failed_at >= RETRY_AFTER:
                self._failed.pop(guid, None)
                self._captures[guid] = _AdapterCapture(self, adapter)
        problems = [(wanted[g].name, message) for g, (_, message) in self._failed.items() if g in wanted]
        if problems and not self._captures:
            self._set_status({"state": "error", "version": version, "message": problems[0][1]})
        else:
            self._set_status({"state": "on" if self._captures else "idle", "version": version,
                              "adapters": sorted(c.adapter.name for c in self._captures.values()),
                              "problems": [f"{name}: {message}" for name, message in problems]})

    def _stop_all(self) -> None:
        for capture in self._captures.values():
            capture.stop.set()
        self._captures.clear()
        if self._flows or self._live:
            self._flows.clear()
            self._unresolved.clear()
            self._live.clear()
            self.flow_view, self.udp_flows, self.programs = {}, [], []
            self._publish_live()

    def _update(self, now: float) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        minute = now // 60 * 60
        if self._minute is not None and minute != self._minute:
            self._save_minute()
        self._minute = minute
        home = self._settings.home_country or winutil.region_code()

        moved: list[tuple[Flow, int, int]] = []
        for guid, counts in pending.items():
            for key, (rx, tx) in counts.items():
                flow = self._flows.get(key)
                if flow is None:
                    flow = self._flows[key] = Flow(key, now, guid)
                    if flow.proto in _PROTO_NAMES and flow.lport:
                        self._unresolved.append(flow)
                    else:
                        flow.app = OTHER
                    if not flow.lan and flow.remote:
                        flow.cc = self._country(flow.remote)
                flow.adapter, flow.last = guid, now
                flow.rx += rx
                flow.tx += tx
                flow.recent[2] += rx
                flow.recent[3] += tx
                moved.append((flow, rx, tx))
        if self._unresolved:
            self._resolve(now)

        second: dict[int, list] = {}
        for flow, rx, tx in moved:
            if flow.app is None:  # still looking for its program: keep the bytes for now
                flow.held[0] += rx
                flow.held[1] += tx
                continue
            self._account(flow, rx, tx, home, second)
        self._shift_live(second)
        self._age_flows(now)
        self._publish_live()

    def _country(self, ip: str) -> str:
        cc = self._countries.get(ip)
        if cc is None:
            if len(self._countries) > 50000:
                self._countries.clear()
            cc = self._countries[ip] = self._geoip.lookup(ip).get("cc", "")
        return cc

    def _read_owners(self) -> None:
        """Note which program owns each socket right now (pid 0 = already closed: not useful)."""
        now = time.monotonic()
        self._snoop, self._snooped = False, now
        owners = self._owners
        for s in list_sockets():
            if s.pid:
                if s.proto == "TCP":
                    owners[(6, s.lport, s.remote, s.rport) if s.remote else (6, s.lport)] = (s.pid, now)
                else:
                    owners[(17, s.lport)] = (s.pid, now)
        if len(owners) > 5000 or now - self._owners_pruned > SNOOP_KEEP:
            self._owners = {k: v for k, v in owners.items() if now - v[1] <= SNOOP_KEEP}
            self._owners_pruned = now

    def _resolve(self, now: float) -> None:
        """Find the programs of new flows in Windows' connection table (read now and in recent snoops)."""
        self._read_owners()
        owners = self._owners
        still = []
        for flow in self._unresolved:
            if flow.proto == 6:
                found = owners.get((6, flow.lport, flow.remote, flow.rport)) or owners.get((6, flow.lport))
            else:
                found = owners.get((17, flow.lport))
            pid = found[0] if found else None
            if pid is not None:
                flow.pid = pid
                app = self._apps.for_pid(pid)
                flow.app = UNKNOWN if app.key.startswith("pid:") else app
            elif now - flow.first >= RESOLVE_FOR:
                flow.app = UNKNOWN
            if flow.app is None:
                still.append(flow)
        self._unresolved = still
        home = self._settings.home_country or winutil.region_code()
        for flow in self._flows.values():  # release the bytes kept while looking
            if flow.app is not None and (flow.held[0] or flow.held[1]):
                rx, tx = flow.held
                flow.held = [0, 0]
                self._account(flow, rx, tx, home, None)

    def _account(self, flow: Flow, rx: int, tx: int, home: str, second: dict | None) -> None:
        app = flow.app
        details = (app.name, app.exe, app.path)
        values = self._minute_programs.setdefault(app.key, (details, [0] * 6))[1]
        adapter = self._minute_adapters.setdefault(flow.adapter, [0] * 6)  # lan, domestic, internet: rx/tx
        if flow.lan:
            values[2] += rx
            values[3] += tx
            adapter[0] += rx
            adapter[1] += tx
        else:
            values[0] += rx
            values[1] += tx
            adapter[4] += rx
            adapter[5] += tx
            if flow.cc and flow.cc == home:
                values[4] += rx
                values[5] += tx
                adapter[2] += rx
                adapter[3] += tx
        if second is not None:
            entry = second.setdefault(app.id, [app, 0, 0])
            entry[1] += rx
            entry[2] += tx

    def _shift_live(self, second: dict[int, list]) -> None:
        for app_id, (app, rx, tx) in second.items():
            if app_id not in self._live:
                self._live[app_id] = [app, [(0, 0)] * LIVE_SECONDS]
            self._live[app_id][0] = app
        for app_id in list(self._live):
            history = self._live[app_id][1]
            _, rx, tx = second.get(app_id, (None, 0, 0))
            history = history[1:] + [(rx, tx)]
            if not any(rx or tx for rx, tx in history):
                del self._live[app_id]
            else:
                self._live[app_id][1] = history

    def _age_flows(self, now: float) -> None:
        view, udp = {}, []
        for key in [k for k, f in self._flows.items() if now - f.last > FLOW_IDLE]:
            del self._flows[key]
        for flow in self._flows.values():
            down, up = (flow.recent[0] + flow.recent[2]) / 2, (flow.recent[1] + flow.recent[3]) / 2
            flow.recent = [flow.recent[2], flow.recent[3], 0, 0]
            if not flow.remote or flow.proto not in _PROTO_NAMES:
                continue
            stats = {"down": round(down), "up": round(up), "rx": flow.rx, "tx": flow.tx}
            view[flow.row_key] = stats
            if flow.proto == 17 and flow.app is not None:
                udp.append({**stats, "local": flow.local, "lport": flow.lport, "remote": flow.remote,
                            "rport": flow.rport, "first": flow.first, "last": flow.last, "app": flow.app,
                            "pid": flow.pid})
        self.flow_view, self.udp_flows = view, udp

    def _publish_live(self) -> None:
        programs = []
        for app, history in self._live.values():
            rx = sum(r for r, _ in history) / LIVE_SECONDS
            tx = sum(t for _, t in history) / LIVE_SECONDS
            programs.append({"id": app.id, "name": app.name, "exe": app.exe, "icon": app.icon is not None,
                             "down": round(rx), "up": round(tx)})
        programs.sort(key=lambda p: p["down"] + p["up"], reverse=True)
        self.programs = programs
        for listener in self.live_listeners:
            try:
                listener()
            except Exception:
                log.exception("Live traffic listener failed")

    def _save_minute(self) -> None:
        if self._minute_adapters or self._minute_programs:
            self._history.add_capture(self._minute, self._minute_adapters, self._minute_programs)
        self._minute_adapters, self._minute_programs = {}, {}

    def close(self) -> None:
        """Stop capturing and save the last partial minute (call before the history closes)."""
        with self._update_lock:
            for capture in self._captures.values():
                capture.stop.set()
            self._update(time.time())
            self._save_minute()

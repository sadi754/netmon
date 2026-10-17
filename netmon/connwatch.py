"""The Connections tab's data: every socket with its program, place and owner.

Built every 2 seconds, and only while a window shows the tab - otherwise this
costs nothing. Everything is read locally (Windows' tables, the GeoIP files);
only the optional host-name lookups ask the DNS server. With packet capture on,
each connection also gets its speed and data, UDP traffic gets its destinations,
and site names come from the PC's own DNS answers and TLS hellos first.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from . import vpn, winutil
from .adapters import list_adapters
from .apps import AppCatalog
from .connections import list_sockets
from .geoip import GeoIP, scope
from .names import Names
from .rdns import ReverseDNS
from .settings import Settings
from .traffic import TrafficMonitor

INTERVAL = 2.0   # seconds between updates
UDP_SHOWN = 60   # seconds a quiet UDP flow stays in the table
UDP_ACTIVE = 5   # ... and counts as "Active"

log = logging.getLogger(__name__)


class ConnectionWatcher:
    def __init__(self, settings: Settings, geoip: GeoIP, apps: AppCatalog, traffic: TrafficMonitor, names: Names):
        self._settings = settings
        self._geoip = geoip
        self._traffic = traffic
        self._names = names
        self._rdns = ReverseDNS()
        self.apps = apps               # also serves the program icons to the window
        self._scopes: dict[str, str] = {}
        self._watching = threading.Event()
        self._wake = threading.Event()
        self.listeners: list[Callable[[str], None]] = []  # get each update as JSON text
        threading.Thread(target=self._run, name="connections", daemon=True).start()

    def watch(self, on: bool) -> None:
        """Start (a window shows the tab) or stop updating."""
        if on:
            self._watching.set()
            self._wake.set()
        else:
            self._watching.clear()

    def refresh(self) -> None:
        """Update now (a setting changed, the GeoIP database arrived)."""
        self._wake.set()

    def home_country(self) -> str:
        return self._settings.home_country or winutil.region_code()

    def _run(self) -> None:
        while True:
            self._watching.wait()
            started = time.monotonic()
            try:
                text = json.dumps(self.snapshot(), separators=(",", ":"))
                for listener in self.listeners:
                    listener(text)
            except Exception:
                log.exception("Reading the connections failed")
            self._wake.wait(max(0.0, INTERVAL - (time.monotonic() - started)))
            self._wake.clear()

    def snapshot(self) -> dict:
        self.apps.refresh()
        sockets = list_sockets()
        rdns = self._settings.rdns_enabled
        capturing = self._traffic.status.get("state") == "on"
        traffic = self._traffic.flow_view if capturing else {}
        rows, places, apps, active = [], {}, {}, set()
        for s in sockets:
            app = self.apps.for_pid(s.pid)
            apps[app.id] = app
            row = {"key": s.key, "proto": s.proto, "local": s.local, "lport": s.lport,
                   "remote": s.remote, "rport": s.rport, "state": s.state, "pid": s.pid,
                   "app": app.id, "opened": round(s.opened)}
            if s.remote:
                active.add(s.pid)
                row.update(traffic.get(f"{s.proto}|{s.local}|{s.lport}|{s.remote}|{s.rport}", {}))
                if s.remote not in places:
                    places[s.remote] = self._describe(s.remote, rdns)
            rows.append(row)
        now = time.time()
        for flow in self._traffic.udp_flows if capturing else []:  # UDP: only the capture knows where it goes
            if now - flow["last"] > UDP_SHOWN:
                continue
            app = flow["app"]
            apps[app.id] = app
            rows.append({"key": f"UDP|{flow['local']}|{flow['lport']}|{flow['remote']}|{flow['rport']}|{flow['pid']}",
                         "proto": "UDP", "local": flow["local"], "lport": flow["lport"], "remote": flow["remote"],
                         "rport": flow["rport"], "state": "Active" if now - flow["last"] <= UDP_ACTIVE else "Idle",
                         "pid": flow["pid"], "app": app.id, "opened": round(flow["first"]),
                         "down": flow["down"], "up": flow["up"], "rx": flow["rx"], "tx": flow["tx"]})
            if flow["remote"] not in places:
                places[flow["remote"]] = self._describe(flow["remote"], rdns)
        return {
            "type": "connections",
            "t": time.time(),
            "rows": rows,
            "places": places,
            "apps": {app_id: app.as_dict() for app_id, app in apps.items()},
            "vpn": vpn.detect(list_adapters(), self.apps.names, active),
            "home": self.home_country(),
            "home_auto": not self._settings.home_country,
            "geoip": self._geoip.ready,
            "capture": capturing,
        }

    def _describe(self, ip: str, rdns: bool) -> dict:
        kind = self._scopes.get(ip)
        if kind is None:
            if len(self._scopes) > 50000:
                self._scopes.clear()
            kind = self._scopes[ip] = scope(ip)
        place = {"scope": kind}
        if kind == "internet":
            place.update(self._geoip.lookup(ip))
        if kind in ("internet", "lan", "special"):
            # The name the program asked for (DNS answer / TLS hello) beats reverse DNS.
            place["host"] = self._names.get(ip) or (self._rdns.name(ip) if rdns else "")
        return place

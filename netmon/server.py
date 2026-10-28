"""Local web server: serves the UI and streams live data over a WebSocket.

It listens on 127.0.0.1 only, so other computers can't reach it, and every
request must carry a random token created at startup, so web pages open in
your browser can't read your traffic either.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict

import uvicorn
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import (__version__, autostart, export, jalali, periods, pinghistory, router as router_mod, routerdirect,
               routerscript, usage, winutil)
from .connwatch import ConnectionWatcher
from .geoip import GeoIP
from .geoip import scope as geoip_scope
from .history import History
from .monitor import Monitor
from .paths import DATA_DIR, FROZEN, WEB_DIR
from .ping import Pinger, PingStats
from .pinghistory import PingRecorder
from .router import RouterLink
from .settings import (CONN_VIEWS, MAP_VIEWS, QUOTA_COUNTS, QUOTA_PERIODS, QUOTA_SOURCES, ROUTER_MODES, UNITS,
                       is_country_code)
from .speed import Tick
from .traffic import TrafficMonitor

log = logging.getLogger(__name__)

_HOST = re.compile(r"^[A-Za-z0-9.\-:]{1,253}$")
_RELAY_URL = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?(/[\w.\-/]*)?$")
_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_ROUTER_HOST = re.compile(r"^(https?://)?[A-Za-z0-9.\-]{1,253}(:\d{1,5})?$")
_ROUTEROS_NAME = re.compile(r"^[\w.\-]{1,64}$")


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_jalali_date(v) -> bool:
    try:
        jalali.parse(v)
        return True
    except (ValueError, AttributeError):
        return False


# Settings the window may change, each with a check of the value it sends.
_EDITABLE: dict[str, Callable[[object], bool]] = {
    "units": lambda v: v in UNITS,
    "mini_visible": lambda v: isinstance(v, bool),
    "ping_visible": lambda v: isinstance(v, bool),
    "hide_in_fullscreen": lambda v: isinstance(v, bool),
    "ping_target": lambda v: isinstance(v, str) and bool(_HOST.match(v.strip())),
    "ping_gateway": lambda v: isinstance(v, str) and (v.strip() == "" or bool(_HOST.match(v.strip()))),
    "ping_record": lambda v: isinstance(v, bool),
    "quota_enabled": lambda v: isinstance(v, bool),
    "quota_period": lambda v: v in QUOTA_PERIODS,
    "quota_days": lambda v: _is_number(v) and v == int(v) and 1 <= v <= 366,
    "quota_start": lambda v: v == "" or _is_jalali_date(v),
    "quota_limit_gb": lambda v: _is_number(v) and 0 < v < 1_000_000,
    "quota_count": lambda v: v in QUOTA_COUNTS,
    "quota_source": lambda v: v in QUOTA_SOURCES,
    "quota_domestic_percent": lambda v: _is_number(v) and v == int(v) and 0 <= v <= 100,
    "router_enabled": lambda v: isinstance(v, bool),
    "router_url": lambda v: isinstance(v, str) and (v == "" or bool(_RELAY_URL.match(v.strip()))),
    "router_token": lambda v: isinstance(v, str) and (v == "" or bool(_TOKEN.match(v.strip()))),
    "router_mode": lambda v: v in ROUTER_MODES,
    "router_host": lambda v: isinstance(v, str) and (v == "" or bool(_ROUTER_HOST.match(v.strip()))),
    "router_wan": lambda v: isinstance(v, str) and (v == "" or bool(_ROUTEROS_NAME.match(v.strip()))),
    "router_user": lambda v: isinstance(v, str) and bool(_ROUTEROS_NAME.match(v.strip())),
    "router_password": lambda v: isinstance(v, str) and 0 < len(v) <= 128,  # stored encrypted (app.py)
    "rdns_enabled": lambda v: isinstance(v, bool),
    "geoip_auto_update": lambda v: isinstance(v, bool),
    "home_country": is_country_code,
    "conn_hide_local": lambda v: isinstance(v, bool),
    "conn_show_listening": lambda v: isinstance(v, bool),
    "conn_view": lambda v: v in CONN_VIEWS,
    "capture_enabled": lambda v: isinstance(v, bool),
    "usage_exclude_lan": lambda v: isinstance(v, bool),
    "map_view": lambda v: v in MAP_VIEWS,
}
_PROGRAM_PERIODS = ("day", "week", "month")
_LIVE_PROGRAMS = 12  # programs listed on the Live page


class _RevalidatingStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        # Ask the window to check for a newer file each time (a cheap 304 if unchanged),
        # so an updated UI shows up without clearing caches.
        response.headers["Cache-Control"] = "no-cache"
        return response


def _ignore_dropped_connections(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    # When the window closes, Windows drops its connection abruptly and asyncio reports
    # it as an error ("WinError 10054"). That is normal here, so keep it out of the log.
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


class WebServer:
    def __init__(self, monitor: Monitor, pinger: Pinger, recorder: PingRecorder, history: History,
                 router: RouterLink, connections: ConnectionWatcher, geoip: GeoIP, traffic: TrafficMonitor,
                 on_show: Callable[[], None], on_settings: Callable[[dict], None]):
        self.monitor = monitor
        self.pinger = pinger
        self.recorder = recorder
        self.history = history
        self.router = router
        self.connections = connections
        self.geoip = geoip
        self.traffic = traffic
        self.on_show = on_show
        self.on_settings = on_settings  # applies validated changes (shows widgets, changes target...)
        self.token = secrets.token_urlsafe(24)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))  # 0 = let Windows pick a free port
        self.port = self._sock.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self._clients: dict[WebSocket, asyncio.Queue[str]] = {}  # open windows -> their outbox
        self._watching: set[asyncio.Queue[str]] = set()  # windows showing the Connections tab
        self._loop: asyncio.AbstractEventLoop | None = None
        app = Starlette(
            routes=[
                WebSocketRoute("/ws", self._websocket),
                Route("/api/show", self._show, methods=["POST"]),
                Route("/api/icon/{app_id:int}", self._icon),
                Route("/api/program-icon/{program_id:int}", self._program_icon),
                Route("/api/export/{name}", self._export),
                Mount("/", _RevalidatingStaticFiles(directory=WEB_DIR, html=True)),
            ],
            lifespan=self._lifespan,
        )
        # log_config=None: use NetMon's logging; uvicorn's own setup needs a console,
        # which the windowless app (pythonw.exe) doesn't have.
        config = uvicorn.Config(app, log_config=None, log_level="warning", access_log=False,
                                timeout_graceful_shutdown=2)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._run, name="web", daemon=True)
        monitor.subscribe(self._on_tick)
        pinger.subscribe(self._on_ping)
        recorder.listeners.append(self._on_ping_saved)
        connections.listeners.append(self._on_connections)
        geoip.listeners.append(self.publish_geoip)
        traffic.live_listeners.append(self._on_programs)
        traffic.status_listeners.append(self._on_capture_status)

    @property
    def url(self) -> str:
        return f"{self.origin}/?token={self.token}"

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("The local web server failed to start - see data/netmon.log")
            time.sleep(0.02)

    def stop(self) -> None:
        # Close the window's live connection politely first; shutting the server down
        # under a half-closed connection trips an error inside uvicorn.
        if self._loop and self._clients:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._close_clients(), self._loop).result(timeout=3)
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def _run(self) -> None:
        try:
            self._server.run(sockets=[self._sock])
        except Exception:
            if not self._server.should_exit:
                raise
            log.debug("Web server error while shutting down", exc_info=True)

    async def _close_clients(self) -> None:
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.close(code=1001)  # 1001 = "going away"
        for _ in range(40):  # up to 2 s for the windows to answer
            if not self._clients:
                return
            await asyncio.sleep(0.05)

    @contextlib.asynccontextmanager
    async def _lifespan(self, app):
        loop = self._loop = asyncio.get_running_loop()
        loop.set_exception_handler(_ignore_dropped_connections)
        # The monitor runs on the server's event loop, next to the WebSocket handlers.
        task = asyncio.create_task(self.monitor.run())
        yield
        task.cancel()

    def _authorized(self, conn: HTTPConnection) -> bool:
        return secrets.compare_digest(conn.query_params.get("token", ""), self.token)

    async def _show(self, request: Request) -> Response:
        if not self._authorized(request):
            return Response(status_code=403)
        await asyncio.to_thread(self.on_show)
        return Response(status_code=204)

    async def _icon(self, request: Request) -> Response:
        """A program's icon for the Connections tab (PNG)."""
        app = self.connections.apps.by_id.get(request.path_params["app_id"])
        if not self._authorized(request) or not app or not app.icon:
            return Response(status_code=404)
        return Response(app.icon, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    async def _program_icon(self, request: Request) -> Response:
        """The icon of a program in the usage history (it may not be running now)."""
        if not self._authorized(request):
            return Response(status_code=404)
        icon = await asyncio.to_thread(self._history_icon, request.path_params["program_id"])
        if not icon:
            return Response(status_code=404)
        return Response(icon, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    async def _export(self, request: Request) -> Response:
        """A CSV (or the zip of all data) to save; the window shows a "Save as" dialog."""
        if not self._authorized(request):
            return Response(status_code=404)
        name, query, settings = request.path_params["name"], request.query_params, self.monitor.settings
        if name == "usage.csv" and query.get("range") in periods.RANGES:
            data = await asyncio.to_thread(export.usage_csv, self.history, settings, query["range"])
            file = export.filename("usage", query["range"]) + ".csv"
        elif name == "programs.csv" and query.get("period") in _PROGRAM_PERIODS:
            data = await asyncio.to_thread(export.programs_csv, self.history, settings, query["period"])
            file = export.filename("programs", query["period"]) + ".csv"
        elif name == "router.csv" and query.get("range") in periods.RANGES:
            data = await asyncio.to_thread(export.router_csv, self.history, query["range"])
            file = export.filename("router", query["range"]) + ".csv"
        elif name == "connections.csv":
            snapshot = await asyncio.to_thread(self.connections.snapshot)
            data, file = export.connections_csv(snapshot), export.filename("connections") + ".csv"
        elif name == "netmon-data.zip":
            data = await asyncio.to_thread(export.all_data_zip, self.history)
            file = export.filename("data") + ".zip"
        else:
            return Response(status_code=404)
        media = "application/zip" if file.endswith(".zip") else "text/csv; charset=utf-8"
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{file}"'})

    def _history_icon(self, program_id: int) -> bytes | None:
        program = self.history.program(program_id)
        return self.connections.apps.icon_for(program["key"], program["path"], program["exe"]) if program else None

    async def _websocket(self, ws: WebSocket) -> None:
        # A browser always sends the page's origin; a foreign website would show its own.
        if not self._authorized(ws) or ws.headers.get("origin") != self.origin:
            await ws.close(code=1008)
            return
        await ws.accept()
        usage_now = await asyncio.to_thread(self._usage_message)  # database reads, off the loop
        router_now = await asyncio.to_thread(self._router_message)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
        self._clients[ws] = queue  # before "hello", so no tick falls in between
        pump = None
        try:
            await ws.send_text(json.dumps(self._hello(usage_now, router_now)))
            pump = asyncio.create_task(self._pump(ws, queue))
            while True:
                try:
                    message = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                await self._handle(queue, message)
        except (WebSocketDisconnect, RuntimeError):  # RuntimeError: we closed it ourselves
            pass
        finally:
            self._clients.pop(ws, None)
            self._watch(queue, False)
            if pump:
                pump.cancel()

    @staticmethod
    async def _pump(ws: WebSocket, queue: asyncio.Queue[str]) -> None:
        with contextlib.suppress(Exception):  # the receive loop notices the disconnect
            while True:
                await ws.send_text(await queue.get())

    async def _handle(self, queue: asyncio.Queue[str], message: dict) -> None:
        kind = message.get("type")
        if kind == "usage" and message.get("range") in periods.RANGES:
            settings = self.monitor.settings
            report = await asyncio.to_thread(usage.chart, self.history, message["range"],
                                             settings.adapter, settings.usage_exclude_lan)
            self._send(queue, {"type": "usage", **report})  # only to the window that asked
            return
        if kind == "usage_programs" and message.get("period") in _PROGRAM_PERIODS:
            report = await asyncio.to_thread(self._programs_report, message["period"])
            self._send(queue, {"type": "usage_programs", **report})
            return
        if kind == "router_usage" and message.get("range") in periods.RANGES:
            report = await asyncio.to_thread(usage.router_chart, self.history, message["range"])
            self._send(queue, {"type": "router_usage", **report})
            return
        if kind == "router_test" and message.get("mode") == "direct":
            s = self.monitor.settings
            host, wan = str(message.get("host", "")).strip(), str(message.get("wan", "")).strip()
            password = str(message.get("password") or "") or winutil.unprotect(s.router_password)
            if not _ROUTER_HOST.match(host) or not _ROUTEROS_NAME.match(wan) or not password:
                result = {"ok": False, "message": "Enter the router's address and internet interface, "
                                                  "and make the setup script first (it sets the password)."}
            else:
                result = await asyncio.to_thread(routerdirect.test, host, s.router_user, password, wan)
            self._send(queue, {"type": "router_test", **result})
            return
        if kind == "router_test":
            url, token = str(message.get("url", "")).strip(), str(message.get("token", "")).strip()
            if not _RELAY_URL.match(url) or not _TOKEN.match(token):
                result = {"ok": False, "message": "Enter the relay's address (https://…) and its READ token."}
            else:
                result = await asyncio.to_thread(router_mod.test, url, token)
            self._send(queue, {"type": "router_test", **result})
            return
        if kind == "ping_history" and message.get("range") in pinghistory.RANGES:
            report = await asyncio.to_thread(pinghistory.report, self.history, self.recorder, message["range"])
            self._send(queue, {"type": "ping_history", **report})
            return
        if kind == "ip_lookup":
            result = await asyncio.to_thread(self._ip_lookup, str(message.get("address", "")).strip())
            self._send(queue, {"type": "ip_lookup", **result})
            return
        if kind == "connections_watch":
            self._watch(queue, bool(message.get("on")))
            return
        if kind in ("geoip_download", "geoip_cancel", "geoip_remove"):
            action = {"geoip_download": self.geoip.download, "geoip_cancel": self.geoip.cancel,
                      "geoip_remove": self.geoip.remove}[kind]
            await asyncio.to_thread(action)
            return
        if kind == "autostart":
            try:
                await asyncio.to_thread(autostart.set_enabled, bool(message.get("on")))
            except OSError:
                log.exception("Changing the Windows start-up entry failed")
            self._send(queue, await asyncio.to_thread(self._system_message))
            return
        if kind == "open_folder" and message.get("what") in ("data", "log"):
            target = DATA_DIR if message["what"] == "data" else DATA_DIR / "netmon.log"
            with contextlib.suppress(OSError):
                os.startfile(target)  # Explorer / the default text editor
            return
        if kind == "delete_history":
            await asyncio.to_thread(self.history.clear_usage)
            self._send(queue, await asyncio.to_thread(self._system_message))
            return
        if kind == "delete_ping_history":
            self.recorder.clear()
            await asyncio.to_thread(self.history.clear_ping)
            self._send(queue, await asyncio.to_thread(self._system_message))
            self._broadcast({"type": "ping_saved"})  # an open Ping tab empties
            return
        if kind == "router_direct_script":
            host, wan = str(message.get("host", "")).strip(), str(message.get("wan", "")).strip()
            try:
                if not _ROUTER_HOST.match(host):
                    raise ValueError("Router address: its IP in your local network, e.g. 192.168.88.1")
                allow = await asyncio.to_thread(routerscript.local_network, host)
                password = secrets.token_urlsafe(18)
                script = routerscript.render_direct(wan, str(message.get("list", "")), password, allow)
            except OSError:
                self._send(queue, {"type": "router_script", "for": "direct", "ok": False,
                                   "message": "This PC has no route to that address - check the router's IP."})
                return
            except ValueError as e:
                self._send(queue, {"type": "router_script", "for": "direct", "ok": False, "message": str(e)})
                return
            # The script sets this password on the router; NetMon keeps it (encrypted) to read with.
            self.on_settings({"router_mode": "direct", "router_host": host, "router_wan": wan,
                              "router_user": "netmon", "router_password": password})
            self.monitor.settings.save()
            self._broadcast_settings()
            self._send(queue, {"type": "router_script", "for": "direct", "ok": True, "script": script})
            return
        if kind == "router_script":
            try:
                script = routerscript.render(str(message.get("url", "")), str(message.get("token", "")),
                                             str(message.get("wan", "")), str(message.get("list", "")))
                self._send(queue, {"type": "router_script", "ok": True, "script": script})
            except ValueError as e:
                self._send(queue, {"type": "router_script", "ok": False, "message": str(e)})
            return
        if kind != "settings":
            return
        changes = {key: message[key] for key, valid in _EDITABLE.items() if key in message and valid(message[key])}
        if "adapter" in message:
            guid = message["adapter"] or None
            last = self.monitor.last
            if guid is None or (last and any(a.adapter.guid == guid for a in last.adapters)):
                changes["adapter"] = guid
        if changes:
            self.on_settings(changes)
            self.monitor.settings.save()
        self._broadcast_settings()

    # ---- safe to call from other threads (tray, widgets, pinger)

    def notify_settings(self) -> None:
        """Settings changed elsewhere (tray, a widget's menu): update the window."""
        self._from_thread(self._broadcast_settings)

    def navigate(self, view: str) -> None:
        """Switch the open window to a page, e.g. "settings"."""
        self._from_thread(self._broadcast, {"type": "navigate", "view": view})

    def _on_ping(self, stats: PingStats) -> None:
        self._from_thread(self._broadcast, {"type": "ping", **stats.as_dict()})

    def _on_ping_saved(self) -> None:
        """A minute of pings was saved, or an outage began or ended: an open Ping tab refreshes."""
        self._from_thread(self._broadcast, {"type": "ping_saved"})

    def publish_usage(self) -> None:
        """New usage was saved (once a minute) or the quota changed: refresh the window."""
        if self._clients:  # window closed -> skip the database reads
            self._from_thread(self._broadcast, self._usage_message())

    def publish_router(self) -> None:
        """The router link synced (once a minute): refresh the Router tab."""
        if self._clients:
            self._from_thread(self._broadcast, self._router_message())

    def _system_message(self) -> dict:
        """For Settings -> General and About."""
        return {"type": "system", "autostart": autostart.enabled(), "data_dir": str(DATA_DIR),
                "db_size": self.history.size(), "exe": FROZEN}

    def _programs_report(self, period: str) -> dict:
        report = usage.programs(self.history, period, self.monitor.settings.usage_exclude_lan)
        for program in report["programs"]:  # an icon if its .exe can still be found
            program["icon"] = self.connections.apps.icon_for(program["key"], program["path"], program["exe"]) is not None
        return report

    def _ip_lookup(self, address: str) -> dict:
        """IP Lookup tab: what the GeoIP database knows about an address the user typed."""
        try:
            ipaddress.ip_address(address)
        except ValueError:
            return {"ok": False, "address": address, "message": "Enter a valid IPv4 or IPv6 address."}
        reason = {"loopback": "That's this PC.", "lan": "That's a local-network address.",
                  "multicast": "That's a multicast address.", "special": "That's a reserved/special-use address."}
        kind = geoip_scope(address)
        if kind != "internet":
            return {"ok": False, "address": address, "message": f"{reason[kind]} There's nothing to look up."}
        if not self.geoip.ready:
            return {"ok": False, "address": address, "message": "The GeoIP database isn't installed yet (Settings → Connections)."}
        found = self.geoip.lookup(address)
        if not found:
            return {"ok": False, "address": address, "message": "No match in the GeoIP database for this address."}
        return {"ok": True, "address": address, **found}

    def _programs_message(self) -> dict:
        programs = self.traffic.programs
        return {"type": "programs", "programs": programs[:_LIVE_PROGRAMS], "more": max(0, len(programs) - _LIVE_PROGRAMS)}

    def _on_programs(self) -> None:
        """The packet capture's live per-program speeds (once a second)."""
        if self._clients:
            self._from_thread(self._broadcast, self._programs_message())

    def _on_capture_status(self) -> None:
        self._from_thread(self._broadcast, {"type": "capture", **self.traffic.status})

    def publish_geoip(self) -> None:
        """The GeoIP database changed or is downloading: update Settings and the Connections tab."""
        status = self.geoip.status()
        self._from_thread(self._broadcast, {"type": "geoip", **status})
        if status["state"] != "downloading":
            self.connections.refresh()  # show the places right away

    def _on_connections(self, text: str) -> None:
        if self._watching:
            self._from_thread(self._send_watchers, text)

    def _router_message(self) -> dict:
        message = {"type": "router_summary", "status": self.router.status}
        if self.monitor.settings.router_enabled:
            message["summary"] = usage.router_summary(self.history)
        return message

    def _usage_message(self) -> dict:
        settings = self.monitor.settings
        return {"type": "usage_summary",
                "summary": usage.summary(self.history, settings.adapter, settings.usage_exclude_lan),
                "quota": usage.quota_report(settings, self.history)}

    def _from_thread(self, fn: Callable, *args) -> None:
        if self._loop and self._clients:
            with contextlib.suppress(RuntimeError):  # the loop is already closed when exiting
                self._loop.call_soon_threadsafe(fn, *args)

    # ---- on the server's event loop

    def _watch(self, queue: asyncio.Queue[str], on: bool) -> None:
        if on:
            self._watching.add(queue)
        else:
            self._watching.discard(queue)
        self.connections.watch(bool(self._watching))

    def _send_watchers(self, text: str) -> None:
        for queue in self._watching:
            self._send(queue, text)

    def _public_settings(self) -> dict:
        """Settings for the window - without the router password (it only learns whether one is set)."""
        settings = asdict(self.monitor.settings)
        settings["router_password_set"] = bool(settings.pop("router_password"))
        return settings

    def _broadcast_settings(self) -> None:
        self._broadcast({"type": "settings", "settings": self._public_settings()})

    def _broadcast(self, message: dict) -> None:
        text = json.dumps(message)
        for queue in self._clients.values():
            self._send(queue, text)

    @staticmethod
    def _send(queue: asyncio.Queue[str], message: dict | str) -> None:
        if queue.full():  # a slow window drops its oldest message rather than piling up
            queue.get_nowait()
        queue.put_nowait(message if isinstance(message, str) else json.dumps(message))

    def _on_tick(self, tick: Tick) -> None:
        if self._clients:  # nobody watching (window closed) -> no work
            self._broadcast(self._tick_message(tick))

    def _hello(self, usage_now: dict, router_now: dict) -> dict:
        m = self.monitor
        return {
            "type": "hello",
            "version": __version__,
            "started": m.started,
            "settings": self._public_settings(),
            "history": list(m.history),
            "tick": self._tick_message(m.last) if m.last else None,
            "ping": self.pinger.stats.as_dict(),
            "ping_gateway": self.pinger.gateway_stats.as_dict(),
            "usage": usage_now,
            "router": router_now,
            "geoip": self.geoip.status(),
            "region": winutil.region_code(),
            "capture": self.traffic.status,
            "programs": self._programs_message(),
            "system": self._system_message(),
        }

    def _tick_message(self, tick: Tick) -> dict:
        return {
            "type": "tick",
            "t": round(tick.time, 3),
            "down": round(tick.down, 1),
            "up": round(tick.up, 1),
            "rx_total": self.monitor.rx_total,
            "tx_total": self.monitor.tx_total,
            "adapters": [
                {
                    "guid": a.adapter.guid,
                    "name": a.adapter.name,
                    "description": a.adapter.description,
                    "physical": a.adapter.physical,
                    "connected": a.adapter.up,
                    "link": a.adapter.link_speed,
                    "down": round(a.rx / tick.seconds, 1) if tick.seconds else 0,
                    "up": round(a.tx / tick.seconds, 1) if tick.seconds else 0,
                    "monitored": a.monitored,
                }
                for a in tick.adapters
            ],
        }

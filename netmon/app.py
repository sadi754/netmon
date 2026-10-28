"""The desktop app: monitor + local web server + tray icon + window."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import urllib.request

from . import autostart, fmt, logs, usage, winutil
from . import __version__
from .apps import AppCatalog
from .connwatch import ConnectionWatcher
from .geoip import GeoIP
from .history import History
from .minigraph import MiniGraph
from .monitor import Monitor
from .names import Names
from .paths import DATA_DIR
from .ping import Pinger
from .pinghistory import PingRecorder
from .pingwidget import PingWidget
from .router import RouterLink
from .server import WebServer
from .settings import Settings
from .traffic import TrafficMonitor
from .tray import Tray
from .usage import QuotaAlerts

INSTANCE_FILE = DATA_DIR / "instance.json"  # how a second launch finds the running one

log = logging.getLogger(__name__)


def _self_command() -> list[str]:
    """How to start another copy of ourselves (the window runs as a separate process)."""
    if getattr(sys, "frozen", False):  # packaged .exe
        return [sys.executable]
    return [sys.executable, "-m", "netmon"]


class WindowProcess:
    def __init__(self):
        self.url = ""
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def open(self, view: str | None = None) -> None:
        """Open the window (optionally on a page, e.g. "settings"), or bring it to the front."""
        with self._lock:
            if self._process and self._process.poll() is None:
                for hwnd in winutil.windows_of(self._process.pid):
                    winutil.bring_to_front(hwnd)
                return
            url = f"{self.url}#{view}" if view else self.url
            self._process = subprocess.Popen([*_self_command(), "--window", url])

    def close(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                # Wait until it's really gone, so its connection is closed before
                # the web server shuts down (otherwise the server waits for it).
                try:
                    self._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    log.warning("The window process did not exit in time")


def _wake_running_instance() -> None:
    """NetMon is already running: ask it to show its window, then quit."""
    try:
        info = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        winutil.allow_any_foreground()
        url = f"http://127.0.0.1:{info['port']}/api/show?token={info['token']}"
        urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=5)
    except (OSError, ValueError, KeyError) as e:
        log.warning("NetMon is already running but did not answer: %s", e)


def run(minimized: bool = False, debug: bool = False) -> None:
    logs.setup("netmon.log", debug)
    winutil.enable_dpi_awareness()
    if not winutil.claim_single_instance():
        _wake_running_instance()
        return

    log.info("NetMon %s starting (data in %s)", __version__, DATA_DIR)
    winutil.set_app_id()  # notifications and windows show as "NetMon"
    try:
        autostart.refresh()  # starts with Windows from an older place? point it here
    except OSError:
        log.warning("Couldn't check the Windows start-up entry", exc_info=True)
    settings = Settings.load()
    monitor = Monitor(settings)
    history = History()
    monitor.subscribe(history.on_tick)
    pinger = Pinger(settings)
    recorder = PingRecorder(history)
    pinger.echo_listeners.append(lambda echo: settings.ping_record and recorder.on_echo(echo))
    router_link = RouterLink(settings, history)
    geoip = GeoIP(settings)
    apps, names = AppCatalog(), Names()
    traffic = TrafficMonitor(settings, geoip, apps, names, history)  # per-program traffic (Npcap)
    monitor.subscribe(traffic.on_tick)
    connections = ConnectionWatcher(settings, geoip, apps, traffic, names)
    window = WindowProcess()

    def apply_settings(changes: dict) -> None:
        """Changes made on the window's Settings page (already validated by the server)."""
        for key, value in changes.items():
            if key == "adapter":
                monitor.select_adapter(value)
            elif key == "mini_visible":
                mini.set_visible(value)
            elif key == "ping_visible":
                ping_widget.set_visible(value)
            elif key in ("ping_target", "ping_gateway"):
                setattr(settings, key, value.strip())
                pinger.wake()
            elif key == "ping_record":
                settings.ping_record = value
                pinger.wake()
            elif key == "quota_limit_gb":
                settings.quota_limit_gb = float(value)
            elif key in ("quota_days", "quota_domestic_percent"):
                setattr(settings, key, int(value))
            elif key in ("router_url", "router_token", "router_host", "router_wan", "router_user"):
                setattr(settings, key, value.strip())
            elif key == "router_password":
                settings.router_password = winutil.protect(value)  # only this Windows user can read it back
            else:
                setattr(settings, key, value)
        if any(key.startswith("router_") for key in changes):
            router_link.wake()  # connect / sync right away
        if "rdns_enabled" in changes or "home_country" in changes:
            connections.refresh()
        if "capture_enabled" in changes:
            traffic.apply_settings()
        if any(key.startswith(("quota_", "router_", "usage_")) or key == "adapter" for key in changes):
            history.submit(after_save)  # recheck the quota and refresh the Usage page now

    server = WebServer(monitor, pinger, recorder, history, router_link, connections, geoip, traffic,
                       on_show=window.open, on_settings=apply_settings)
    window.url = server.url

    def open_settings() -> None:
        server.navigate("settings")  # in case the window is open already
        window.open("settings")

    mini = MiniGraph(monitor, on_open=window.open)
    ping_widget = PingWidget(pinger, settings, on_open=window.open, on_change_target=open_settings, above=mini)
    widgets = (mini, ping_widget)
    for widget in widgets:  # shown/hidden from the tray or its own menu -> update the Settings page
        widget.listeners.append(server.notify_settings)
    tray = Tray(monitor, on_open=lambda: window.open(), widgets=widgets)

    def hide_widgets_for_fullscreen(_tick) -> None:
        busy = winutil.fullscreen_monitor() if settings.hide_in_fullscreen else None
        for widget in widgets:
            widget.hide_for_fullscreen(busy)

    monitor.subscribe(hide_widgets_for_fullscreen)  # checked once a second

    alerts = QuotaAlerts(history, notify=tray.notify)

    def after_save() -> None:
        """Runs on the history thread after each save (once a minute) and after quota changes."""
        status = usage.quota_status(settings, history)
        tray.quota_line = (f"Quota {status.percent:.0f}% · {fmt.size(status.left)} left" if status else "")
        alerts.check(status)
        server.publish_usage()

    history.listeners.append(after_save)
    history.submit(after_save)  # quota line in the tray right from the start

    def after_router_sync() -> None:
        server.publish_router()
        if settings.quota_source == "router":
            history.submit(after_save)  # the quota counts the router's numbers

    router_link.listeners.append(after_router_sync)
    server.start()
    router_link.wake()  # first sync now that everything is listening
    INSTANCE_FILE.write_text(json.dumps({"port": server.port, "token": server.token}), encoding="utf-8")
    try:
        if not minimized:
            window.open()
        tray.run()  # blocks until "Exit" in the tray menu
    finally:
        log.info("NetMon exiting")
        for widget in widgets:
            widget.close()
        server.stop()   # closes the window's connection cleanly before the window goes
        window.close()
        traffic.close()  # its last partial minute goes to the history...
        recorder.close()  # ... so do the pings of this minute ...
        history.close()  # ... which saves its own and closes
        INSTANCE_FILE.unlink(missing_ok=True)

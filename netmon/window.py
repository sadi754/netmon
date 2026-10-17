"""The NetMon window: a native desktop window (Edge WebView2) showing the web UI.

It runs as its own process. Closing the window ends that process, which frees
all of the browser engine's memory; the monitor keeps running in the tray.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import webview

from . import icons, winutil

BACKGROUND = "#0a0e14"
CAPTION_RGB = (13, 18, 27)  # matches the app's top bar (#0d121b)

LOAD_TIMEOUT = 6.0  # seconds after the window shows; no page by then = load it again
MAX_RELOADS = 3     # per window, so a broken setup can't reload forever
# WebView2 helper processes whose failure leaves an empty window that a reload fixes.
RELOAD_ON = {"RenderProcessExited", "RenderProcessUnresponsive", "FrameRenderProcessExited", "GpuProcessExited"}

log = logging.getLogger(__name__)


def run(url: str) -> None:
    winutil.set_app_id()
    webview.settings["ALLOW_DOWNLOADS"] = True  # CSV exports: Windows' "Save as" dialog
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # links to websites open in the browser
    # NETMON_WINDOW_HIDDEN=1: build check - make the window without showing it.
    window = webview.create_window("NetMon", url, width=1180, height=800, min_size=(900, 620),
                                   background_color=BACKGROUND, hidden=os.environ.get("NETMON_WINDOW_HIDDEN") == "1")
    watchdog = PageWatchdog(window, url)
    window.events.before_show += watchdog.attach
    window.events.shown += _decorate
    window.events.shown += watchdog.start
    webview.start()


class PageWatchdog:
    """Sometimes WebView2 leaves the window empty (the page never arrives, or one
    of its helper processes crashes). Notice that, write it to window.log and
    load the page again - what closing and reopening the window would do."""

    def __init__(self, window: webview.Window, url: str):
        self.window = window
        self.url = url
        self.loaded = threading.Event()
        self.reloads = 0
        self.shown_at = 0.0
        self._lock = threading.Lock()

    def attach(self) -> None:
        """Runs on the window's thread, after the WebView2 control is made but before it starts."""
        control = self.window.native.webview
        control.CoreWebView2InitializationCompleted += self._on_ready
        control.NavigationCompleted += self._on_navigated

    def start(self) -> None:
        self.shown_at = time.monotonic()
        threading.Thread(target=self._watch, name="page-watchdog", daemon=True).start()

    def _watch(self) -> None:
        while not self.loaded.wait(LOAD_TIMEOUT):
            if not self._reload(f"the page did not load within {LOAD_TIMEOUT:.0f} s"):
                return

    def _reload(self, why: str) -> bool:
        with self._lock:
            if self.reloads >= MAX_RELOADS:
                log.error("Window still empty (%s) - gave up after %d reloads", why, self.reloads)
                return False
            self.reloads += 1
            self.loaded.clear()
            log.warning("Window empty: %s - reloading (%d/%d)", why, self.reloads, MAX_RELOADS)
        try:
            self._navigate()
        except Exception:
            log.exception("Reloading the page failed")
        return True

    def _navigate(self) -> None:
        # Not window.load_url(): WebView2 ignores a new Source equal to the current one.
        from System import Func, Type  # .NET, loaded by pywebview

        form = self.window.native

        def go():
            core = form.webview.CoreWebView2
            if core is None:  # still starting; it opens the page itself once ready
                log.warning("WebView2 has not started yet")
            else:
                core.Navigate(self.url)

        form.Invoke(Func[Type](go))

    # .NET events (they run on the window's thread)

    def _on_ready(self, sender, args) -> None:
        if not args.IsSuccess:
            log.error("WebView2 did not start: %s", args.InitializationException)
            return
        sender.CoreWebView2.ProcessFailed += self._on_process_failed

    def _on_navigated(self, sender, args) -> None:
        took = time.monotonic() - self.shown_at if self.shown_at else 0.0
        if args.IsSuccess:
            log.info("Page loaded (%.1f s after the window showed)", took)
            self.loaded.set()
        else:
            # WebView2 shows its own error page; the watchdog loads ours again.
            log.warning("Page failed to load: %s (HTTP %s)", args.WebErrorStatus,
                        getattr(args, "HttpStatusCode", "?"))

    def _on_process_failed(self, sender, args) -> None:
        kind = str(args.ProcessFailedKind)
        log.warning("WebView2 process failed: %s (reason %s, exit code %s)", kind,
                    getattr(args, "Reason", "?"), getattr(args, "ExitCode", "?"))
        if kind == "BrowserProcessExited":
            # The whole engine is gone and can't come back in this window; close it
            # rather than leave an empty one - opening it again starts a fresh engine.
            log.error("Browser engine exited - closing the window")
            threading.Thread(target=self.window.destroy, daemon=True).start()
        elif kind in RELOAD_ON:
            threading.Thread(target=self._reload, args=(f"WebView2 {kind}",), daemon=True).start()


def _decorate() -> None:
    """Dark title bar and our own icon instead of Python's."""
    small, big = winutil.small_icon_size(), winutil.big_icon_size()
    small_icon = winutil.icon_from_png(icons.png(icons.logo(small)), small)
    big_icon = winutil.icon_from_png(icons.png(icons.logo(big)), big)
    for hwnd in winutil.windows_of(os.getpid()):
        winutil.style_dark_window(hwnd, CAPTION_RGB)
        winutil.set_window_icon(hwnd, small_icon, big_icon)

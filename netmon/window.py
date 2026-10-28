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
DISPLAY_CHECK_EVERY = 2.5  # seconds: is anything on screen? (a check takes ~15 ms; only while the window shows)
BLANK = 0.995              # this much plain background colour = nothing is drawn

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
    window.events.shown += watchdog.start
    webview.start()


class PageWatchdog:
    """Sometimes WebView2 leaves the window empty: the page never arrives, one of its
    helper processes crashes, or the page is loaded but never appears on screen.
    Notice that, write it to window.log and fix it - nudge the display, or load the
    page again, which is what closing and reopening the window would do."""

    def __init__(self, window: webview.Window, url: str):
        self.window = window
        self.url = url
        self.loaded = threading.Event()
        self.reloads = 0
        self.shown_at = 0.0
        self.hwnd = 0
        self.hidden = os.environ.get("NETMON_WINDOW_HIDDEN") == "1"
        self._lock = threading.Lock()

    def attach(self) -> None:
        """Runs on the window's thread, after the WebView2 control is made but before the window shows."""
        form = self.window.native
        self.hwnd = int(form.Handle.ToInt64())
        # Style the frame now, before it shows: changing it while WebView2 sets up its
        # drawing surface (right after showing) is a suspect for windows that stay empty.
        _decorate(self.hwnd)
        form.webview.CoreWebView2InitializationCompleted += self._on_ready
        form.webview.NavigationCompleted += self._on_navigated

    def start(self) -> None:
        self.shown_at = time.monotonic()
        threading.Thread(target=self._watch, name="page-watchdog", daemon=True).start()
        if not self.hidden:
            threading.Thread(target=self._watch_display, name="display-watch", daemon=True).start()

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

    def _watch_display(self) -> None:
        """While the window is open: is the page on screen, or only the window's background?
        WebView2 sometimes stops showing a loaded page - at opening, or later while in use."""
        blank_in_a_row = 0
        while True:
            time.sleep(DISPLAY_CHECK_EVERY)
            if not self.loaded.is_set() or winutil.is_minimized(self.hwnd):
                blank_in_a_row = 0
                continue
            share = _background_share(self.hwnd)
            if share is None or share < BLANK:
                blank_in_a_row = 0
                if self.reloads:  # it's fine again: allow reloads for a later problem
                    self.reloads = 0
                continue
            blank_in_a_row += 1
            if blank_in_a_row < 2:  # a page switch can be empty for a moment
                continue
            blank_in_a_row = 0
            log.warning("Window empty: the page is loaded but nothing is on screen (%.1f%% background; page: %s)"
                        " - nudging", share * 100, self._page_state())
            self._nudge()
            time.sleep(1.0)
            share = _background_share(self.hwnd)
            if share is not None and share < BLANK:
                log.info("Window fixed by the nudge")
                continue
            self._reload("still nothing on screen after the nudge")

    def _page_state(self) -> str:
        """What the page says about itself (for the log); "no answer" if it doesn't reply in 2 s."""
        result = {}

        def ask():
            try:
                result["state"] = self.window.evaluate_js(
                    "location.hash + ' ' + document.readyState + ' ' + document.visibilityState"
                    " + ' ' + innerWidth + 'x' + innerHeight")
            except Exception as e:
                result["state"] = f"error {e}"

        asker = threading.Thread(target=ask, daemon=True)
        asker.start()
        asker.join(2)
        return result.get("state", "no answer")

    def _nudge(self) -> None:
        """Make WebView2 put its picture on screen again: hide and show the browser control."""
        from System import Func, Type  # .NET, loaded by pywebview

        control = self.window.native.webview

        def toggle():
            control.Visible = False
            control.Visible = True
            control.Focus()

        try:
            self.window.native.Invoke(Func[Type](toggle))
        except Exception:
            log.exception("Nudging the window failed")

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


def _decorate(hwnd: int) -> None:
    """Dark title bar and our own icon instead of Python's."""
    small, big = winutil.small_icon_size(), winutil.big_icon_size()
    winutil.style_dark_window(hwnd, CAPTION_RGB)
    winutil.set_window_icon(hwnd, winutil.icon_from_png(icons.png(icons.logo(small)), small),
                            winutil.icon_from_png(icons.png(icons.logo(big)), big))


def _background_share(hwnd: int) -> float | None:
    """How much of the window (0..1) is plain background colour - 1.0 means nothing is drawn."""
    captured = winutil.window_pixels(hwnd)
    if not captured:
        return None
    width, height, pixels = captured
    r0, g0, b0 = (int(BACKGROUND[i:i + 2], 16) for i in (1, 3, 5))
    same = total = 0
    edge = 8  # skip the edges: a 1-pixel control border runs along them
    for y in range(edge, height - edge, 6):
        row = y * width * 4
        for x in range(edge, width - edge, 6):
            b, g, r = pixels[row + x * 4], pixels[row + x * 4 + 1], pixels[row + x * 4 + 2]
            same += abs(r - r0) <= 6 and abs(g - g0) <= 6 and abs(b - b0) <= 6
            total += 1
    return same / total if total else None

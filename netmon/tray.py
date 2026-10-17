"""System tray icon: a live mini graph, speeds on hover, and a small menu."""
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable
from functools import partial

import pystray

from . import fmt, icons, winutil
from .monitor import Monitor
from .speed import Tick
from .widget import FloatingWidget

_DEFAULT_CEILING = 125_000_000  # bytes/s (1 Gbit/s) when the link speed is unknown

log = logging.getLogger(__name__)


def _toggle(widget: FloatingWidget, *_) -> None:
    widget.set_visible(not widget.visible)


def _shown(widget: FloatingWidget, *_) -> bool:
    return widget.visible


class _Icon(pystray.Icon):
    def _assert_icon_handle(self):
        # pystray saves every new icon to a temporary .ico file and loads it back;
        # we change the icon every second, so build it in memory instead.
        if self._icon_handle:
            return
        self._icon_handle = winutil.icon_from_png(icons.png(self.icon), self.icon.width)


class Tray:
    def __init__(self, monitor: Monitor, on_open: Callable[[], None], widgets: Iterable[FloatingWidget]):
        self._monitor = monitor
        self._size = winutil.small_icon_size()
        self._samples: deque[tuple[float, float]] = deque(maxlen=self._size)
        self._last_pixels = b""
        self.quota_line = ""  # e.g. "Quota 45% · 27.5 GB left", shown under the speeds
        widget_items = [pystray.MenuItem(w.title, partial(_toggle, w), checked=partial(_shown, w))
                        for w in widgets]
        self._icon = _Icon(
            "NetMon",
            icon=icons.graph([], self._size, _DEFAULT_CEILING),
            title="NetMon",
            menu=pystray.Menu(
                pystray.MenuItem("Open NetMon", on_open, default=True),  # also a left-click
                *widget_items,
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self.stop),
            ),
        )
        for widget in widgets:  # hidden from its own menu or the settings -> update the tick mark
            widget.listeners.append(self._icon.update_menu)
        monitor.subscribe(self._on_tick)

    def run(self) -> None:
        """Show the icon; blocks until Exit is chosen."""
        self._icon.run()

    def stop(self) -> None:
        self._icon.stop()

    def notify(self, title: str, message: str) -> bool:
        """A Windows notification from the tray icon. False if it couldn't be shown (yet)."""
        if not self._icon.visible:  # still starting up
            return False
        try:
            self._icon.notify(message, f"NetMon: {title}")
            return True
        except Exception:
            log.exception("Showing a notification failed")
            return False

    def _on_tick(self, tick: Tick) -> None:
        self._samples.append((tick.down, tick.up))
        link_bits = sum(a.adapter.link_speed for a in tick.adapters if a.monitored and a.adapter.up)
        image = icons.graph(self._samples, self._size, link_bits / 8 or _DEFAULT_CEILING)
        pixels = image.tobytes()
        if pixels != self._last_pixels:  # idle -> identical image -> skip the update
            self._last_pixels = pixels
            self._icon.icon = image
        bits = self._monitor.settings.units == "bits"
        title = f"NetMon\n↓ {fmt.speed(tick.down, bits)}\n↑ {fmt.speed(tick.up, bits)}"
        self._icon.title = f"{title}\n{self.quota_line}" if self.quota_line else title

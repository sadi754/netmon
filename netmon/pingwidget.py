"""The floating ping widget: live ping to a server of your choice (handy for games)."""
from __future__ import annotations

from collections.abc import Callable

from PIL import Image, ImageDraw

from . import minigraph, winutil
from .ping import Pinger, PingStats
from .settings import Settings
from .widget import (MENU_CUSTOM, MUTED, SS, TASKBAR_TEXT, TEXT, FloatingWidget, canvas, docked_canvas, fit, font,
                     paint, round_corners, shrink)

WIDTH, HEIGHT = 156, 44
BARS = 30  # pings in the little bar chart

# Status colours: they always come with the number, never alone.
GOOD, FAIR, POOR = (63, 185, 80), (210, 153, 34), (248, 81, 73)
_BAR = (139, 155, 180)
_MENU_TARGET = MENU_CUSTOM


def quality(ms: float) -> tuple[int, int, int]:
    """Rough feel for online games: < 60 ms good, < 120 ms playable, above that laggy."""
    return GOOD if ms < 60 else FAIR if ms < 120 else POOR


def _status_colour(stats: PingStats) -> tuple[int, int, int]:
    if stats.last is not None:
        return quality(stats.last)
    if stats.results or stats.problem:
        return POOR
    return MUTED  # waiting for the first reply


def _value(stats: PingStats) -> tuple[str, str]:
    """The big number and its unit."""
    if stats.last is not None:
        return (f"{stats.last:.0f}" if stats.last >= 1 else "<1"), "ms"
    if stats.problem == "timeout":
        return "timeout", ""
    if stats.problem:
        return "—", ""
    return "…", ""


def render(stats: PingStats, scale: float) -> Image.Image:
    big, draw, k = canvas(WIDTH, HEIGHT, scale)

    # Bar chart of the last pings (right side); a lost ping is a full-height red bar.
    x0, y0, x1, y1 = 98 * k, 8 * k, (WIDTH - 9) * k, 25 * k
    results = list(stats.results[-BARS:])
    top = max([r for r in results if r is not None] + [50.0])
    slot = (x1 - x0) / BARS
    bar = max(k, slot * 0.6)
    for i, rtt in enumerate(results):
        x = x1 - (len(results) - i) * slot + (slot - bar) / 2
        if rtt is None:
            draw.rectangle((x, y0, x + bar, y1), fill=(*POOR, 210))
        else:
            draw.rectangle((x, y1 - max(k, rtt / top * (y1 - y0)), x + bar, y1), fill=(*_BAR, 170))

    # Status dot
    cx, cy, r = 13 * k, 16.5 * k, 4 * k
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*_status_colour(stats), 255))
    image = shrink(big, k)

    # Text at the final size, so it stays crisp.
    s = scale
    text = ImageDraw.Draw(image)
    value, unit = _value(stats)
    value_font = font("seguisb.ttf", round(17 * s))
    baseline = 23 * s
    text.text((23 * s, baseline), value, font=value_font, fill=TEXT, anchor="ls")
    if unit:
        text.text((23 * s + value_font.getlength(value) + 3 * s, baseline), unit,
                  font=font("segoeui.ttf", round(10 * s)), fill=MUTED, anchor="ls")

    # Most important first, so a long host name is what gets shortened.
    details = []
    if stats.problem and stats.problem != "timeout" and stats.last is None:
        details.append(stats.problem)
    else:
        if stats.loss is not None:
            details.append(f"{stats.loss:.0f}% loss")
        if stats.jitter is not None:
            details.append(f"±{stats.jitter:.0f} ms")
    details.append(stats.target or "no target")
    small = font("segoeui.ttf", round(10 * s))
    text.text((9 * s, 35.5 * s), fit(" · ".join(details), (WIDTH - 17) * s, small),
              font=small, fill=MUTED, anchor="lm")
    return round_corners(image, k)


def render_docked(stats: PingStats, scale: float, height: int, light: bool) -> Image.Image:
    """On the taskbar: status dot and ping time, the target below."""
    value, unit = _value(stats)
    colour = _status_colour(stats)
    size = max(10, min(round(15 * scale), round(height * 0.38)))
    value_font, small = font("seguisb.ttf", size), font("segoeui.ttf", max(8, round(size * 0.66)))
    pad = round(7 * scale)
    width = round(92 * scale)
    image = docked_canvas(width, height, light)
    fg = TASKBAR_TEXT[light]
    top_y, bottom_y = height * 0.36, height * 0.76

    dot = Image.new("L", (width * SS, height * SS))
    cx, cy, r = (pad + 4 * scale) * SS, top_y * SS, 4 * scale * SS
    ImageDraw.Draw(dot).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    paint(image, dot, colour)

    text = Image.new("L", image.size)
    draw = ImageDraw.Draw(text)
    x = pad + round(14 * scale)
    draw.text((x, top_y), value, font=value_font, fill=255, anchor="lm")
    paint(image, text, fg)
    if unit:
        unit_mask = Image.new("L", image.size)
        ImageDraw.Draw(unit_mask).text((x + value_font.getlength(value) + 3 * scale, top_y), unit,
                                       font=small, fill=255, anchor="lm")
        paint(image, unit_mask, fg, 170)
    target = Image.new("L", image.size)
    ImageDraw.Draw(target).text((pad, bottom_y), fit(stats.target or "no target", width - 2 * pad, small),
                                font=small, fill=255, anchor="lm")
    paint(image, target, fg, 170)
    return image


class PingWidget(FloatingWidget):
    key = "ping"
    title = "Ping widget"
    width, height = WIDTH, HEIGHT
    corner_offset = minigraph.HEIGHT + 8  # starts just above the mini graph

    def __init__(self, pinger: Pinger, settings: Settings, on_open: Callable[[], None],
                 on_change_target: Callable[[], None], above: FloatingWidget | None = None):
        self._pinger = pinger
        self._on_change_target = on_change_target
        self._above = above  # first time shown, sit just above this widget (the mini graph)
        super().__init__(settings, on_open)
        self.listeners.append(pinger.wake)  # shown/hidden -> start/stop pinging right away
        pinger.subscribe(lambda stats: self.redraw())

    def render(self, scale: float) -> Image.Image:
        stats = self._pinger.stats
        if stats.target != self._settings.ping_target.strip():  # target just changed
            stats = PingStats(self._settings.ping_target.strip())
        return render(stats, scale)

    def render_docked(self, scale: float, height: int, light: bool) -> Image.Image:
        stats = self._pinger.stats
        if stats.target != self._settings.ping_target.strip():
            stats = PingStats(self._settings.ping_target.strip())
        return render_docked(stats, scale, height, light)

    def _default_position(self, scale: float) -> tuple[int, int]:
        other = self._above.screen_rect() if self._above else None
        if other is None:
            return super()._default_position(scale)
        gap = round(8 * scale)
        x = other.right - round(self.width * scale)  # right edges line up
        y = other.top - gap - round(self.height * scale)
        if not winutil.on_screen(x, y):
            y = other.bottom + gap  # no room above: go below instead
        return x, y

    def menu_items(self) -> list[tuple[int, str]]:
        return [(_MENU_TARGET, "Change target…")]

    def on_menu(self, item: int) -> None:
        if item == _MENU_TARGET:
            self._on_change_target()

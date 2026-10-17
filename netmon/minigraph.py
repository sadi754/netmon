"""The floating mini graph: the last minute of download/upload, always on top."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable

from PIL import Image, ImageDraw

from . import fmt
from .icons import DOWN, UP
from .monitor import Monitor
from .speed import Tick
from .widget import (MUTED, PANEL, SS, TASKBAR_TEXT, TEXT, FloatingWidget, canvas, docked_canvas, font, paint,
                     round_corners, shrink)

SECONDS = 60      # how much history the graph shows
WIDTH, HEIGHT = 236, 54
_FLOOR = 1024     # bytes/s - the graph never zooms in further, so idle noise stays flat


def render(samples: Iterable[tuple[float, float]], down: float, up: float,
           bits: bool, scale: float) -> Image.Image:
    """samples are (download, upload) bytes/s, oldest first."""
    big, draw, k = canvas(WIDTH, HEIGHT, scale)

    # Graph: last minute, newest on the right, scaled to the busiest second.
    # The top strip of the box is kept free for the scale label.
    x0, y0, x1, y1 = 6 * k, 6 * k, 126 * k, (HEIGHT - 6) * k
    draw.rounded_rectangle((x0, y0, x1, y1), radius=5 * k, fill=PANEL)
    plot_top = y0 + 12 * k
    values = list(samples)[-SECONDS:]
    peak = max((max(v) for v in values), default=0.0)
    top = max(peak, _FLOOR)
    step = (x1 - x0) / (SECONDS - 1)

    def line(index: int) -> list[tuple[float, float]]:
        return [(x1 - (len(values) - 1 - i) * step, y1 - v[index] / top * (y1 - plot_top))
                for i, v in enumerate(values)]

    if len(values) > 1:
        width = max(1, round(1.5 * k))
        down_line, up_line = line(0), line(1)
        draw.polygon([(down_line[0][0], y1), *down_line, (down_line[-1][0], y1)], fill=(*DOWN, 55))
        draw.line(down_line, fill=(*DOWN, 255), width=width, joint="curve")
        # Upload is a line only, with a dark outline so it stays readable over the cyan area.
        draw.line(up_line, fill=(*PANEL, 255), width=width + round(2 * k), joint="curve")
        draw.line(up_line, fill=(*UP, 255), width=width, joint="curve")
    image = shrink(big, k)

    text = ImageDraw.Draw(image)  # text at the final size, so it stays crisp
    arrow_font = font("segoeuib.ttf", round(13 * scale))
    value_font = font("seguisb.ttf", round(13 * scale))
    for row, (arrow, colour, rate) in enumerate((("↓", DOWN, down), ("↑", UP, up))):
        y = (HEIGHT / 2 + (row - 0.5) * 19) * scale
        text.text((136 * scale, y), arrow, font=arrow_font, fill=colour, anchor="lm")
        text.text(((WIDTH - 10) * scale, y), fmt.speed(rate, bits), font=value_font, fill=TEXT, anchor="rm")
    if peak:  # the graph's scale: its busiest second in the last minute
        text.text((11 * scale, 8 * scale), fmt.speed(peak, bits),
                  font=font("segoeui.ttf", round(9 * scale)), fill=MUTED, anchor="la")
    return round_corners(image, k)


_WIDEST = ("888 kbit/s", "8.88 Mbit/s", "1000 Mbit/s", "1000 KB/s", "8.88 MB/s")  # so the width never jumps


def render_docked(samples: Iterable[tuple[float, float]], down: float, up: float,
                  bits: bool, scale: float, height: int, light: bool) -> Image.Image:
    """On the taskbar: both speeds and a small graph of the last minute."""
    size = max(9, min(round(13 * scale), round(height * 0.34)))
    arrow_font, value_font = font("segoeuib.ttf", size), font("seguisb.ttf", size)
    pad, gap = round(6 * scale), round(4 * scale)
    value_left = pad + round(arrow_font.getlength("↓")) + gap
    value_right = value_left + round(max(value_font.getlength(s) for s in _WIDEST))
    gx0 = value_right + round(8 * scale)
    gx1 = gx0 + round(46 * scale)
    image = docked_canvas(gx1 + pad, height, light)
    fg = TASKBAR_TEXT[light]

    # Text: one mask per colour, laid on with paint()
    arrows = {DOWN: Image.new("L", image.size), UP: Image.new("L", image.size)}
    values = Image.new("L", image.size)
    for row, (arrow, colour, rate) in enumerate((("↓", DOWN, down), ("↑", UP, up))):
        y = height / 2 + (row - 0.5) * height * 0.44
        ImageDraw.Draw(arrows[colour]).text((pad, y), arrow, font=arrow_font, fill=255, anchor="lm")
        ImageDraw.Draw(values).text((value_right, y), fmt.speed(rate, bits), font=value_font, fill=255, anchor="rm")
    for colour, mask in arrows.items():
        paint(image, mask, colour)
    paint(image, values, fg)

    # Graph, drawn 3x larger for smooth lines
    big = (image.width * SS, image.height * SS)
    x0, x1 = gx0 * SS, gx1 * SS
    y0, y1 = round(height * 0.14) * SS, (height - round(height * 0.14)) * SS
    panel = Image.new("L", big)
    ImageDraw.Draw(panel).rounded_rectangle((x0, y0, x1, y1), radius=4 * scale * SS, fill=255)
    paint(image, panel, fg, 30)
    points = list(samples)[-SECONDS:]
    if len(points) > 1:
        top = max(max((max(v) for v in points), default=0.0), _FLOOR)
        step = (x1 - x0) / (SECONDS - 1)
        inner = y1 - 2 * scale * SS

        def line(index: int) -> list[tuple[float, float]]:
            return [(x1 - (len(points) - 1 - i) * step, inner - v[index] / top * (inner - y0 - 2 * scale * SS))
                    for i, v in enumerate(points)]

        width = max(SS, round(1.5 * scale * SS))
        down_line, up_line = line(0), line(1)
        area, down_mask, up_mask = Image.new("L", big), Image.new("L", big), Image.new("L", big)
        ImageDraw.Draw(area).polygon([(down_line[0][0], inner), *down_line, (down_line[-1][0], inner)], fill=255)
        ImageDraw.Draw(down_mask).line(down_line, fill=255, width=width, joint="curve")
        ImageDraw.Draw(up_mask).line(up_line, fill=255, width=width, joint="curve")
        paint(image, area, DOWN, 70)
        paint(image, down_mask, DOWN)
        paint(image, up_mask, UP)
    return image


class MiniGraph(FloatingWidget):
    key = "mini"
    title = "Mini graph"
    width, height = WIDTH, HEIGHT

    def __init__(self, monitor: Monitor, on_open: Callable[[], None]):
        self._samples: deque[tuple[float, float]] = deque(maxlen=SECONDS)
        self._latest = (0.0, 0.0)
        super().__init__(monitor.settings, on_open)
        monitor.subscribe(self._on_tick)

    def _on_tick(self, tick: Tick) -> None:
        self._samples.append((tick.down, tick.up))
        self._latest = (tick.down, tick.up)
        self.redraw()

    def render(self, scale: float) -> Image.Image:
        return render(list(self._samples), *self._latest, self._settings.units == "bits", scale)

    def render_docked(self, scale: float, height: int, light: bool) -> Image.Image:
        return render_docked(list(self._samples), *self._latest, self._settings.units == "bits",
                             scale, height, light)

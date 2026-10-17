"""NetMon's icons, drawn in code: the live tray graph and the app logo."""
from __future__ import annotations

import io
import math
from collections.abc import Iterable, Sequence

from PIL import Image, ImageDraw

# The app's two data colours (checked for colour-blind safety on the dark background).
DOWN = (15, 160, 196)   # #0fa0c4 cyan   - download
UP = (227, 109, 38)     # #e36d26 orange - upload
_TILE = (13, 18, 27)    # #0d121b
_BASELINE = (44, 54, 72)

_FLOOR = 1024  # bytes/s that still count as "no traffic"


def level(rate: float, ceiling: float) -> float:
    """0..1 on a log scale from 1 KB/s up to the link speed.

    Log scale so a 50 KB/s trickle is visible, yet a full link still reaches the top.
    """
    if rate <= _FLOOR or ceiling <= _FLOOR:
        return 0.0
    return min(1.0, math.log(rate / _FLOOR) / math.log(ceiling / _FLOOR))


def _render(columns: Sequence[tuple[float, float]], size: int) -> Image.Image:
    """Dark tile with bars; each column is (height 0..1, download share 0..1)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=max(2, size // 6), fill=_TILE)
    pad = max(2, size // 8)
    bottom = size - pad          # first pixel row below the plot
    height = size - 2 * pad
    bar = max(1, size // 16)     # 1 px wide at 100% scaling, wider on high-DPI screens
    draw.line((pad, bottom - 1, size - pad - 1, bottom - 1), fill=_BASELINE)
    columns = list(columns)[-((size - 2 * pad) // bar):]
    x = size - pad - bar * len(columns)  # newest column on the right
    for fraction, down_share in columns:
        h = round(fraction * height)
        down_h = round(h * down_share)
        # Download grows from the baseline, upload is stacked on top of it.
        if down_h:
            draw.rectangle((x, bottom - down_h, x + bar - 1, bottom - 1), fill=DOWN)
        if h - down_h:
            draw.rectangle((x, bottom - h, x + bar - 1, bottom - down_h - 1), fill=UP)
        x += bar
    return img


def graph(samples: Iterable[tuple[float, float]], size: int, ceiling: float) -> Image.Image:
    """Tray icon: one column per second from (download, upload) bytes/s samples."""
    columns = []
    for down, up in samples:
        total = down + up
        columns.append((level(total, ceiling), down / total if total else 1.0))
    return _render(columns, size)


def logo(size: int) -> Image.Image:
    """The app icon: the same graph style, frozen in a rising shape."""
    n = max(4, size)
    columns = [(min(1.0, 0.2 + 0.65 * i / (n - 1) + 0.12 * math.sin(i * 1.7)), 0.75) for i in range(n)]
    return _render(columns, size)


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

"""Human-friendly number formatting."""
from __future__ import annotations

_BIT_RATE_UNITS = ("bit/s", "kbit/s", "Mbit/s", "Gbit/s")
_BYTE_RATE_UNITS = ("B/s", "KB/s", "MB/s", "GB/s")
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def _scaled(value: float, units: tuple[str, ...], step: int) -> str:
    for unit in units[:-1]:
        if value < step:
            break
        value /= step
    else:
        unit = units[-1]
    if unit == units[0]:
        return f"{value:.0f} {unit}"
    # Three significant digits: 1.23 / 12.3 / 123
    decimals = 2 if value < 10 else 1 if value < 100 else 0
    return f"{value:.{decimals}f} {unit}"


def speed(bytes_per_second: float, bits: bool = True) -> str:
    """Network speeds: bits use 1000 steps (like link speeds), bytes 1024 (like Windows)."""
    if bits:
        return _scaled(bytes_per_second * 8, _BIT_RATE_UNITS, 1000)
    return _scaled(bytes_per_second, _BYTE_RATE_UNITS, 1024)


def size(num_bytes: float) -> str:
    return _scaled(num_bytes, _SIZE_UNITS, 1024)


def link(bits_per_second: int) -> str:
    return _scaled(bits_per_second, _BIT_RATE_UNITS, 1000) if bits_per_second else "-"

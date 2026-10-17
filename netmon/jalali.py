"""The Jalali (Shamsi / Persian solar hijri) calendar.

Conversions use the Pournader/Toossi algorithm (the one behind jalaali-js),
which matches the official calendar for years 1-3177.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

MONTHS = ("Farvardin", "Ordibehesht", "Khordad", "Tir", "Mordad", "Shahrivar",
          "Mehr", "Aban", "Azar", "Dey", "Bahman", "Esfand")

_BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097,
           2192, 2262, 2324, 2394, 2456, 3178)


@lru_cache(maxsize=256)
def nowruz(jy: int) -> date:
    """The Gregorian date of 1 Farvardin (new year's day) of Jalali year jy."""
    if not _BREAKS[0] < jy < _BREAKS[-1]:
        raise ValueError(f"Jalali year out of range: {jy}")
    gy = jy + 621
    leap_j, jp, jump = -14, _BREAKS[0], 0
    for jm in _BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += jump // 33 * 8 + jump % 33 // 4
        jp = jm
    n = jy - jp
    leap_j += n // 33 * 8 + (n % 33 + 3) // 4
    if jump % 33 == 4 and jump - n == 4:
        leap_j += 1
    leap_g = gy // 4 - (gy // 100 + 1) * 3 // 4 - 150
    return date(gy, 3, 20 + leap_j - leap_g)


def is_leap(jy: int) -> bool:
    return (nowruz(jy + 1) - nowruz(jy)).days == 366


def month_length(jy: int, jm: int) -> int:
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if is_leap(jy) else 29


def from_gregorian(d: date) -> tuple[int, int, int]:
    """(year, month, day) in the Jalali calendar."""
    jy = d.year - 621
    if d < nowruz(jy):
        jy -= 1
    k = (d - nowruz(jy)).days  # days since 1 Farvardin
    if k < 186:                # first six months have 31 days
        return jy, k // 31 + 1, k % 31 + 1
    k -= 186                   # the rest have 30 (Esfand 29 or 30)
    return jy, k // 30 + 7, k % 30 + 1


def to_gregorian(jy: int, jm: int, jd: int) -> date:
    if not 1 <= jm <= 12 or not 1 <= jd <= month_length(jy, jm):
        raise ValueError(f"No such Jalali date: {jy}/{jm}/{jd}")
    offset = (jm - 1) * 31 if jm <= 7 else 186 + (jm - 7) * 30
    return nowruz(jy) + timedelta(days=offset + jd - 1)


def parse(text: str) -> date:
    """'1405/07/21' (also with - or .) -> Gregorian date. Raises ValueError."""
    parts = text.strip().replace("-", "/").replace(".", "/").split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Not a Jalali date (year/month/day): {text!r}")
    return to_gregorian(*(int(p) for p in parts))


def format_numeric(d: date) -> str:
    jy, jm, jd = from_gregorian(d)
    return f"{jy}/{jm:02d}/{jd:02d}"


def format_range(first: date, last: date) -> str:
    """Compact date range: '18–24 Mehr 1405', '11 Mehr – 10 Aban 1405', or both dates in full."""
    y1, m1, d1 = from_gregorian(first)
    y2, m2, d2 = from_gregorian(last)
    if (y1, m1) == (y2, m2):
        return f"{d1}–{d2} {MONTHS[m2 - 1]} {y2}"
    if y1 == y2:
        return f"{d1} {MONTHS[m1 - 1]} – {d2} {MONTHS[m2 - 1]} {y2}"
    return f"{format_long(first)} – {format_long(last)}"


def format_long(d: date, weekday: bool = False, year: bool = True) -> str:
    """'21 Mehr 1405', optionally with the weekday: 'Tuesday 21 Mehr 1405'."""
    jy, jm, jd = from_gregorian(d)
    text = f"{jd} {MONTHS[jm - 1]}" + (f" {jy}" if year else "")
    return f"{d:%A} {text}" if weekday else text

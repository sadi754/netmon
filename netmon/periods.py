"""Time periods in local time: chart buckets (hours, days, weeks, Jalali months) and quota periods."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as clock, timedelta

from . import jalali

RANGES = {"hours": 24, "days": 30, "weeks": 12, "months": 12}  # buckets shown per chart


@dataclass(frozen=True)
class Period:
    start: float  # unix seconds, inclusive
    end: float    # unix seconds, exclusive
    label: str    # long, for tooltips and tables: "Tuesday 21 Mehr 1405"
    short: str    # for the chart axis: "21"


def midnight(d: date) -> float:
    """Local midnight at the start of a day, as unix seconds."""
    return datetime.combine(d, clock()).timestamp()


def hour_start(t: float) -> float:
    """Start of the local hour containing t (Iran is UTC+3:30, so not a whole UTC hour)."""
    local = datetime.fromtimestamp(t)
    return local.replace(minute=0, second=0, microsecond=0).timestamp()


def week_start(d: date) -> date:
    """The Saturday on or before d (the Iranian week starts on Saturday)."""
    return d - timedelta(days=(d.weekday() - 5) % 7)


def month_start(d: date) -> date:
    jy, jm, _ = jalali.from_gregorian(d)
    return jalali.to_gregorian(jy, jm, 1)


def next_month_start(d: date) -> date:
    jy, jm, _ = jalali.from_gregorian(d)
    return jalali.to_gregorian(jy + jm // 12, jm % 12 + 1, 1)


def buckets(kind: str, now: float) -> list[Period]:
    """The last RANGES[kind] periods of a kind, oldest first; the last one contains now."""
    count = RANGES[kind]
    today = datetime.fromtimestamp(now).date()
    out: list[Period] = []
    if kind == "hours":
        start = hour_start(now)
        for i in range(count - 1, -1, -1):
            s = datetime.fromtimestamp(start) - timedelta(hours=i)
            day = jalali.format_long(s.date(), year=False)
            out.append(Period(s.timestamp(), (s + timedelta(hours=1)).timestamp(),
                              f"{s:%a} {day}, {s:%H}:00–{(s + timedelta(hours=1)):%H}:00", f"{s:%H}"))
    elif kind == "days":
        for i in range(count - 1, -1, -1):
            d = today - timedelta(days=i)
            jd = jalali.from_gregorian(d)[2]
            short = jalali.format_long(d, year=False) if jd == 1 or i == count - 1 else str(jd)
            out.append(Period(midnight(d), midnight(d + timedelta(days=1)),
                              jalali.format_long(d, weekday=True), short))
    elif kind == "weeks":
        first = week_start(today)
        for i in range(count - 1, -1, -1):
            d = first - timedelta(weeks=i)
            last = d + timedelta(days=6)
            out.append(Period(midnight(d), midnight(d + timedelta(days=7)),
                              f"Week {jalali.format_range(d, last)}", jalali.format_long(d, year=False)))
    elif kind == "months":
        d = month_start(today)
        months = []
        for _ in range(count):
            months.append(d)
            d = month_start(d - timedelta(days=1))
        for d in reversed(months):
            jy, jm, _ = jalali.from_gregorian(d)
            name = jalali.MONTHS[jm - 1]
            out.append(Period(midnight(d), midnight(next_month_start(d)), f"{name} {jy}",
                              name if jm != 1 else f"{name} {jy}"))
    else:
        raise ValueError(f"Unknown range: {kind}")
    return out


def today(now: float) -> Period:
    d = datetime.fromtimestamp(now).date()
    return Period(midnight(d), midnight(d + timedelta(days=1)), jalali.format_long(d, weekday=True), "Today")


def this_week(now: float) -> Period:
    d = week_start(datetime.fromtimestamp(now).date())
    return Period(midnight(d), midnight(d + timedelta(days=7)),
                  jalali.format_range(d, d + timedelta(days=6)), "This week")


def this_month(now: float) -> Period:
    d = month_start(datetime.fromtimestamp(now).date())
    jy, jm, _ = jalali.from_gregorian(d)
    return Period(midnight(d), midnight(next_month_start(d)), f"{jalali.MONTHS[jm - 1]} {jy}", "This month")


def quota_period(kind: str, now: float, days: int = 30, start: str = "") -> Period | None:
    """The quota period containing now.

    kind "month": the Jalali calendar month; "day": today;
    "days": blocks of `days` days counted from the Jalali date `start` (prepaid packages).
    Returns None when a "days" quota starts in the future or its start date is invalid.
    """
    if kind == "day":
        return today(now)
    if kind == "month":
        return this_month(now)
    try:
        first = jalali.parse(start)
    except ValueError:
        return None
    current = datetime.fromtimestamp(now).date()
    if current < first or days < 1:
        return None
    begin = first + timedelta(days=(current - first).days // days * days)
    end = begin + timedelta(days=days)
    return Period(midnight(begin), midnight(end), jalali.format_range(begin, end - timedelta(days=1)), "")

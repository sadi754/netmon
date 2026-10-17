"""Usage summaries, chart data and the data quota (with its 80% / 100% warnings)."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from . import fmt, jalali, periods
from .history import History
from .periods import Period
from .settings import Settings

GB = 1024 ** 3
ALERT_LEVELS = (80, 100)


def _usage(t: dict[str, int]) -> dict[str, int]:
    """What Usage shows of history totals: rx/tx, and from the packet capture the domestic and
    international parts (measured; hours without the capture count as "not measured")."""
    irx, itx = min(t["irx"], t["rx"]), min(t["itx"], t["tx"])
    drx, dtx = min(t["drx"], irx), min(t["dtx"], itx)
    return {"rx": t["rx"], "tx": t["tx"], "drx": drx, "dtx": dtx, "irx": irx, "itx": itx}


def _named_periods(now: float) -> tuple[tuple[str, Period], ...]:
    return (("day", periods.today(now)), ("week", periods.this_week(now)), ("month", periods.this_month(now)))


def summary(history: History, adapter: str | None, exclude_lan: bool = False, now: float | None = None) -> dict:
    """Today, this week and this Jalali month: bytes down and up (and their domestic part)."""
    now = now or time.time()
    out = {"today": jalali.format_numeric(datetime.fromtimestamp(now).date()),
           "captured_since": history.first_capture()}  # domestic / per-program numbers exist from then on
    for key, period in _named_periods(now):
        totals = history.totals(period.start, period.end, adapter, exclude_lan)
        out[key] = {"label": period.label, **_usage(totals)}
    return out


def chart(history: History, kind: str, adapter: str | None, exclude_lan: bool = False,
          now: float | None = None) -> dict:
    """Bars for the Usage page: the last 24 hours / 30 days / 12 weeks / 12 months."""
    now = now or time.time()
    spans = periods.buckets(kind, now)
    values = history.series(spans, adapter, exclude_lan)
    first = history.first_record()
    return {
        "range": kind,
        "buckets": [{"start": p.start, "label": p.label, "short": p.short, **_usage(totals),
                     "current": p.start <= now < p.end, "before_records": first is None or p.end <= first}
                    for p, totals in zip(spans, values)],
    }


def programs(history: History, period_key: str, exclude_lan: bool = False, now: float | None = None) -> dict:
    """Each program's usage today / this week / this month, most used first (needs packet capture)."""
    now = now or time.time()
    period = dict(_named_periods(now))[period_key]
    merged: dict[tuple[str, str], dict] = {}
    for p in history.program_totals(period.start, period.end):
        rx, tx = (p["rx"], p["tx"]) if exclude_lan else (p["rx"] + p["lan_rx"], p["tx"] + p["lan_tx"])
        if not (rx or tx):
            continue
        row = {"id": p["id"], "key": p["key"], "name": p["name"], "exe": p["exe"], "path": p["path"],
               "rx": rx, "tx": tx, "drx": p["drx"], "dtx": p["dtx"], "lan": p["lan_rx"] + p["lan_tx"]}
        # A program that exited before Windows told its path was saved under its .exe name only: one row.
        same = merged.get((p["name"], p["exe"].lower()))
        if same is None:
            merged[(p["name"], p["exe"].lower())] = row
            continue
        for key in ("rx", "tx", "drx", "dtx", "lan"):
            same[key] += row[key]
        if row["path"] and not same["path"]:
            same.update(id=row["id"], key=row["key"], path=row["path"])
    rows = sorted(merged.values(), key=lambda r: r["rx"] + r["tx"], reverse=True)
    return {"period": period_key, "label": period.label, "programs": rows, "exclude_lan": exclude_lan}


def _domestic(rx: int, tx: int, drx: int, dtx: int, arx: int = 0, atx: int = 0) -> tuple[int, int]:
    """Domestic bytes down and up, out of the interface's exact rx and tx.

    The router's counting rules give domestic (drx) and all traffic (arx) as mangle
    sees it; their ratio is the domestic share, applied to the exact totals. (Mangle
    can see more bytes than the wire - a queue dropping packets, for one - but that
    cancels out in the ratio.) Without "all" counters, the raw domestic count is capped.
    """
    down = round(rx * min(1.0, drx / arx)) if arx else min(drx, rx)
    up = round(tx * min(1.0, dtx / atx)) if atx else min(dtx, tx)
    return down, up


def router_summary(history: History, now: float | None = None) -> dict:
    """The whole network (router) today, this week and this Jalali month."""
    now = now or time.time()
    out = {}
    for key, period in (("day", periods.today(now)), ("week", periods.this_week(now)),
                        ("month", periods.this_month(now))):
        rx, tx, *counted = history.router_totals(period.start, period.end)
        drx, dtx = _domestic(rx, tx, *counted)
        out[key] = {"label": period.label, "rx": rx, "tx": tx, "drx": drx, "dtx": dtx}
    return out


def router_chart(history: History, kind: str, now: float | None = None) -> dict:
    now = now or time.time()
    spans = periods.buckets(kind, now)
    buckets = []
    for p, (rx, tx, *counted) in zip(spans, history.router_series(spans)):
        drx, dtx = _domestic(rx, tx, *counted)
        buckets.append({"start": p.start, "label": p.label, "short": p.short, "rx": rx, "tx": tx,
                        "drx": drx, "dtx": dtx, "current": p.start <= now < p.end})
    return {"range": kind, "buckets": buckets}


@dataclass(frozen=True)
class QuotaStatus:
    period: Period
    limit: int        # bytes allowed in the period
    rx: int
    tx: int
    download_only: bool
    now: float
    source: str = "pc"          # "pc" or "router"
    drx: int = 0                # domestic part of rx / tx (router, or the PC's packet capture)
    dtx: int = 0
    domestic_factor: float = 1.0  # domestic traffic counts this much: 0.5 = half price

    @property
    def domestic(self) -> int:
        return self.drx if self.download_only else self.drx + self.dtx

    @property
    def used(self) -> int:
        total = self.rx if self.download_only else self.rx + self.tx
        return round(total - self.domestic * (1 - self.domestic_factor))

    @property
    def percent(self) -> float:
        return 100 * self.used / self.limit if self.limit else 0.0

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def days_left(self) -> float:
        return max(0.0, (self.period.end - self.now) / 86400)

    @property
    def projected(self) -> int:
        """Usage at the end of the period if the pace so far continues."""
        elapsed = max(self.now - self.period.start, 3600)  # don't extrapolate from minutes
        return round(self.used * (self.period.end - self.period.start) / elapsed)

    def as_dict(self) -> dict:
        return {
            "enabled": True, "label": self.period.label, "start": self.period.start, "end": self.period.end,
            "limit": self.limit, "rx": self.rx, "tx": self.tx, "used": self.used, "left": self.left,
            "percent": self.percent, "days_left": self.days_left, "projected": self.projected,
            "per_day": self.left / self.days_left if self.days_left >= 1 else self.left,
            "download_only": self.download_only, "source": self.source,
            "domestic": self.domestic, "domestic_factor": self.domestic_factor,
        }


def quota_status(settings: Settings, history: History, now: float | None = None) -> QuotaStatus | None:
    """Usage in the current quota period, or None if no quota is running right now."""
    now = now or time.time()
    if not settings.quota_enabled or settings.quota_limit_gb <= 0:
        return None
    period = periods.quota_period(settings.quota_period, now, settings.quota_days, settings.quota_start)
    if period is None:
        return None
    limit, download_only = round(settings.quota_limit_gb * GB), settings.quota_count == "download"
    if settings.quota_source == "router" and settings.router_enabled:
        rx, tx, *counted = history.router_totals(period.start, period.end)
        drx, dtx = _domestic(rx, tx, *counted)
        return QuotaStatus(period, limit, rx, tx, download_only, now, "router", drx, dtx,
                           settings.quota_domestic_percent / 100)
    # This PC: all physical adapters; domestic traffic as measured by the packet capture.
    t = _usage(history.totals(period.start, period.end, None, settings.usage_exclude_lan))
    return QuotaStatus(period, limit, t["rx"], t["tx"], download_only, now, "pc", t["drx"], t["dtx"],
                       settings.quota_domestic_percent / 100)


def quota_report(settings: Settings, history: History, now: float | None = None) -> dict:
    """What the window shows about the quota (also explains why there is none)."""
    if not settings.quota_enabled:
        return {"enabled": False}
    status = quota_status(settings, history, now)
    if status:
        return status.as_dict()
    if settings.quota_period == "days":
        try:
            first = jalali.parse(settings.quota_start)
            return {"enabled": True, "waiting": f"Your quota period starts on {jalali.format_long(first)}."}
        except ValueError:
            return {"enabled": True, "waiting": "Set the quota's start date in Settings."}
    return {"enabled": True, "waiting": "Set a quota limit in Settings."}


def _duration(days: float) -> str:
    """'12 days' or, under a day, '5 hours'."""
    if days >= 1:
        return f"{int(days)} day{'s' if int(days) != 1 else ''}"
    hours = max(1, round(days * 24))
    return f"{hours} hour{'s' if hours != 1 else ''}"


class QuotaAlerts:
    """Shows a Windows notification at 80% and 100% - once per quota period, even across restarts."""

    def __init__(self, history: History, notify: Callable[[str, str], bool]):
        self._history = history
        self._notify = notify  # returns False if it couldn't be shown; we then retry next minute

    def check(self, status: QuotaStatus | None) -> None:
        if status is None:
            return
        key = [int(status.period.start), status.limit, status.download_only, status.source, status.domestic_factor]
        state = self._history.get_meta("quota_alerts", {})
        if state.get("key") != key:  # new period or a changed quota: start over
            state = {"key": key, "sent": []}
        crossed = [level for level in ALERT_LEVELS if status.percent >= level and level not in state["sent"]]
        if not crossed:
            return
        used = f"{fmt.size(status.used)} of {fmt.size(status.limit)} used"
        if max(crossed) >= 100:
            shown = self._notify("Data quota used up",
                                 f"{used} ({status.period.label}). It resets in {_duration(status.days_left)}.")
        else:
            shown = self._notify(f"{round(status.percent)}% of your data quota used",
                                 f"{used} · {fmt.size(status.left)} left for the next "
                                 f"{_duration(status.days_left)} ({status.period.label}).")
        if shown:  # remember it only once it really appeared
            state["sent"] += crossed
            self._history.set_meta("quota_alerts", state)

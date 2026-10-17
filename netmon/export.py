"""CSV files for Excel and co.: usage, programs, the router, the connections - and all the data.

Tables as the window shows them use MB (1 MB = 1024 x 1024 bytes); the full-data
export uses exact bytes. Files start with a UTF-8 mark so Excel reads them right.
"""
from __future__ import annotations

import csv
import io
import time
import zipfile
from datetime import datetime

from . import jalali, usage
from .history import History
from .settings import Settings

MB = 1024 * 1024


def _csv(header: list[str], rows: list[list]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return ("﻿" + out.getvalue()).encode("utf-8")


def _mb(value: float) -> float:
    return round(value / MB, 3)


def _when(ts: float) -> tuple[str, str]:
    """(Gregorian "2026-10-14 13:00", Jalali "1405/07/22") for a unix time."""
    moment = datetime.fromtimestamp(ts)
    return moment.strftime("%Y-%m-%d %H:%M"), jalali.format_numeric(moment.date())


def filename(kind: str, detail: str = "") -> str:
    stamp = datetime.now().strftime("%Y-%m-%d")
    return f"netmon-{kind}{'-' + detail if detail else ''}-{stamp}"


def usage_csv(history: History, settings: Settings, kind: str) -> bytes:
    """The Usage page's bars (hours / days / weeks / months) as a table."""
    report = usage.chart(history, kind, settings.adapter, settings.usage_exclude_lan)
    rows = []
    for b in report["buckets"]:
        start, _ = _when(b["start"])
        measured = b["irx"] + b["itx"]
        rows.append([b["label"], start, _mb(b["rx"]), _mb(b["tx"]), _mb(b["rx"] + b["tx"]),
                     _mb(b["drx"] + b["dtx"]), _mb(measured - b["drx"] - b["dtx"]),
                     _mb(max(0, b["rx"] + b["tx"] - measured))])
    return _csv(["Period", "Start", "Download (MB)", "Upload (MB)", "Total (MB)", "Domestic (MB)",
                 "International (MB)", "Not measured (MB)"], rows)


def programs_csv(history: History, settings: Settings, period: str) -> bytes:
    report = usage.programs(history, period, settings.usage_exclude_lan)
    rows = [[p["name"], p["exe"], p["path"], _mb(p["rx"]), _mb(p["tx"]), _mb(p["rx"] + p["tx"]),
             _mb(p["drx"] + p["dtx"]), _mb(p["rx"] + p["tx"] - p["drx"] - p["dtx"]), _mb(p["lan"])]
            for p in report["programs"]]
    return _csv(["Program", "File", "Path", "Download (MB)", "Upload (MB)", "Total (MB)", "Domestic (MB)",
                 "International (MB)", "Local network (MB)"], rows)


def router_csv(history: History, kind: str) -> bytes:
    report = usage.router_chart(history, kind)
    rows = []
    for b in report["buckets"]:
        start, _ = _when(b["start"])
        domestic = b["drx"] + b["dtx"]
        rows.append([b["label"], start, _mb(b["rx"]), _mb(b["tx"]), _mb(b["rx"] + b["tx"]),
                     _mb(domestic), _mb(b["rx"] + b["tx"] - domestic)])
    return _csv(["Period", "Start", "Download (MB)", "Upload (MB)", "Total (MB)", "Domestic (MB)",
                 "International (MB)"], rows)


def connections_csv(snapshot: dict) -> bytes:
    """The Connections tab right now."""
    rows = []
    for r in snapshot["rows"]:
        app = snapshot["apps"].get(r["app"], {})
        place = snapshot["places"].get(r["remote"], {}) if r["remote"] else {}
        opened = _when(r["opened"])[0] if r["opened"] else ""
        rows.append([app.get("name", ""), app.get("exe", ""), r["pid"], r["proto"], r["local"], r["lport"],
                     r["remote"], r["rport"] or "", place.get("host", ""), place.get("country", ""),
                     place.get("city", ""), place.get("org", ""), place.get("asn", ""),
                     r["state"] or ("Open" if not r["remote"] else ""), opened,
                     r.get("down", ""), r.get("up", ""), r.get("rx", ""), r.get("tx", "")])
    return _csv(["Program", "File", "PID", "Protocol", "Local address", "Local port", "Remote address",
                 "Remote port", "Host", "Country", "City", "Network", "ASN", "State", "Opened",
                 "Download (B/s)", "Upload (B/s)", "Received (bytes)", "Sent (bytes)"], rows)


def all_data_zip(history: History) -> bytes:
    """Every hour NetMon has recorded, in exact bytes: this PC's adapters, its programs, the router."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        rows = [[*_when(ts), name, "yes" if physical else "no", *values]
                for ts, name, physical, *values in history.usage_rows()]
        archive.writestr("usage-hourly.csv", _csv(
            ["Hour", "Jalali date", "Adapter", "Physical", "Received", "Sent", "Local network received",
             "Local network sent", "Internet received (capture)", "Internet sent (capture)",
             "Domestic received", "Domestic sent"], rows))
        rows = [[*_when(ts), *values] for ts, *values in history.program_rows()]
        archive.writestr("programs-hourly.csv", _csv(
            ["Hour", "Jalali date", "Program", "File", "Path", "Internet received", "Internet sent",
             "Local network received", "Local network sent", "Domestic received", "Domestic sent"], rows))
        rows = [[*_when(ts), *values] for ts, *values in history.router_rows()]
        archive.writestr("router-hourly.csv", _csv(
            ["Hour", "Jalali date", "Router", "Interface", "Received", "Sent", "Domestic received (rules)",
             "Domestic sent (rules)", "All received (rules)", "All sent (rules)"], rows))
        archive.writestr("README.txt", (
            "NetMon data export, " + time.strftime("%Y-%m-%d %H:%M") + "\r\n\r\n"
            "All numbers are bytes per local hour.\r\n"
            "usage-hourly.csv: this PC's adapters. Received/Sent are the adapter's counters; the other columns\r\n"
            "  come from the packet capture (0 for hours without it).\r\n"
            "programs-hourly.csv: each program's traffic (packet capture).\r\n"
            "router-hourly.csv: the whole network from a MikroTik router. The domestic share is\r\n"
            "  'Domestic (rules)' / 'All (rules)', applied to Received / Sent.\r\n").encode("utf-8"))
    return out.getvalue()

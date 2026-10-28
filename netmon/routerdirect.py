"""Whole-network usage read straight from a MikroTik router, no relay needed.

NetMon reads the router's counters over its REST API (RouterOS 7.1+, in the local
network) once a minute while it runs. The router keeps counting while the PC is
off, so the next reading still gets that traffic - shared out evenly over the
hours it covers and marked as an estimate. Only a restart of the router in that
time loses something: its counters start again from zero, so the traffic between
the last reading and the restart is gone. NetMon notes when that happened.
"""
from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .history import History

_UPTIME = re.compile(r"^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_UPTIME_CLOCK = re.compile(r"^(?:(\d+)w)?(?:(\d+)d)?(\d+):(\d\d):(\d\d)")
VALUES = ("rx", "tx", "drx", "dtx", "arx", "atx")
_RULES = {"netmon:domestic-down": "drx", "netmon:domestic-up": "dtx", "netmon:all-down": "arx", "netmon:all-up": "atx"}
LAST_KEY, GAPS_KEY = "router_direct_last", "router_direct_gaps"
KEEP_GAPS = 20


class RouterError(Exception):
    """A problem talking to the router, worded for the person reading the Router tab."""


def parse_uptime(value: str) -> int:
    """RouterOS uptime: "1w2d3h4m5s" (REST) or "1w2d03:04:05" (terminal) -> seconds."""
    value = str(value).strip()
    m = _UPTIME_CLOCK.match(value)
    if m:
        w, d, h, mi, s = (int(x or 0) for x in m.groups())
    else:
        m = _UPTIME.match(value)
        if not m or not value:
            raise RouterError(f"Unexpected router uptime “{value}”.")
        w, d, h, mi, s = (int(x or 0) for x in m.groups())
    return w * 604800 + d * 86400 + h * 3600 + mi * 60 + s


def _base(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if host.startswith(("http://", "https://")) else "http://" + host


def _get(host: str, user: str, password: str, path: str):
    url = _base(host) + "/rest" + path
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": "Basic " + auth,
                                                   "User-Agent": f"NetMon/{__version__}"})
    # A router in the local network usually has a self-signed certificate: HTTPS here
    # protects the password on the wire; the address itself is what you typed.
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RouterError("The router refused the user name or password.") from None
        if e.code == 403:
            raise RouterError("The router's user may not use the REST API - run the setup script again.") from None
        if e.code == 404:
            raise RouterError("No REST API at this address - is it a MikroTik with RouterOS 7.1 or newer, "
                              "and is its “www” service on?") from None
        raise RouterError(f"The router answered with an error (HTTP {e.code}).") from None
    except urllib.error.URLError as e:
        raise RouterError(f"Can't reach the router at {host} ({e.reason}). Is its “www” service on and "
                          "allowed from this PC?") from None
    except (TimeoutError, OSError) as e:
        raise RouterError(f"Can't reach the router at {host} ({e}).") from None
    except ValueError:
        raise RouterError("That address answered, but not like a MikroTik router.") from None


def _number(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def read(host: str, user: str, password: str, wan: str) -> dict:
    """The router's counters right now."""
    identity = _get(host, user, password, "/system/identity").get("name", "router")
    uptime = parse_uptime(_get(host, user, password, "/system/resource").get("uptime", ""))
    found = _get(host, user, password, "/interface?name=" + urllib.parse.quote(wan))
    if not found:
        raise RouterError(f"The router has no interface named “{wan}”.")
    reading = {"router": identity, "iface": wan, "uptime": uptime,
               "rx": _number(found[0].get("rx-byte")), "tx": _number(found[0].get("tx-byte")),
               "drx": 0, "dtx": 0, "arx": 0, "atx": 0, "rules": 0}
    for rule in _get(host, user, password, "/ip/firewall/mangle?.proplist=comment,bytes"):
        key = _RULES.get(rule.get("comment", ""))
        if key:
            reading[key] = _number(rule.get("bytes"))
            reading["rules"] += 1
    return reading


def internet_interface(host: str, user: str, password: str) -> str:
    """The interface of the router's active default route, e.g. "pppoe-out1" ("" if unknown)."""
    try:
        routes = _get(host, user, password, "/ip/route?dst-address=0.0.0.0/0")
    except RouterError:
        return ""
    for route in routes:
        if route.get("active") in (True, "true"):
            gateway = route.get("immediate-gw") or route.get("gateway") or ""
            return gateway.split("%")[-1] if "%" in gateway else ("" if re.match(r"^[\d.]+$", gateway) else gateway)
    return ""


def test(host: str, user: str, password: str, wan: str) -> dict:
    """For the Test button."""
    try:
        r = read(host, user, password, wan)
    except RouterError as e:
        message = str(e)
        if "no interface named" in message:
            guess = internet_interface(host, user, password)
            if guess:
                message += f" Its internet seems to go through “{guess}”."
        return {"ok": False, "message": message}
    rules = ("domestic share measured" if r["rules"] == 4 else
             "no domestic counting rules (add a domestic list to the setup script)" if not r["rules"] else
             f"only {r['rules']} of the 4 counting rules found - run the setup script again")
    return {"ok": True, "message": f"Connected to “{r['router']}”: {wan} has moved {_size(r['rx'] + r['tx'])} "
                                   f"since the router started ({_duration(r['uptime'])} ago); {rules}."}


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _duration(seconds: int) -> str:
    d, rest = divmod(seconds, 86400)
    return f"{d} d {rest // 3600} h" if d else f"{rest // 3600} h {rest % 3600 // 60} min"


def collect(history: History, host: str, user: str, password: str, wan: str, now: float | None = None) -> dict:
    """Read the router once and store the traffic since the last reading. Returns what the Router tab shows."""
    now = now or time.time()
    reading = read(host, user, password, wan)
    previous = history.get_meta(LAST_KEY)
    stored = None
    if previous and previous.get("host") == host and previous.get("iface") == wan:
        expected = previous["uptime"] + (now - previous["ts"])  # its uptime now, had it kept running
        if reading["uptime"] < expected - 120:
            # Started again since the last reading: what it counted since then is all we know.
            boot = now - reading["uptime"]
            delta = {key: reading[key] for key in VALUES}
            span = max(1.0, reading["uptime"])
            if boot > previous["ts"]:
                gaps = history.get_meta(GAPS_KEY, [])
                gaps.append({"from": previous["ts"], "to": boot})
                history.set_meta(GAPS_KEY, gaps[-KEEP_GAPS:])
        else:
            # A counter that went down started again (e.g. counting rules made anew): take it from zero.
            delta = {key: reading[key] - previous[key] if reading[key] >= previous[key] else reading[key]
                     for key in VALUES}
            span = max(1.0, now - previous["ts"])
        if sum(delta.values()) > 0:
            stored = {"ts": now, "span": span, "router": reading["router"], "iface": wan, **delta}
            history.add_router_rows([stored])
    history.set_meta(LAST_KEY, {"host": host, "iface": wan, "ts": now, "uptime": reading["uptime"],
                                **{key: reading[key] for key in VALUES}})
    return {"reading": reading, "stored": stored, "gaps": history.get_meta(GAPS_KEY, [])}

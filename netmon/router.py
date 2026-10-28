"""Whole-network usage from a MikroTik router.

Two ways, chosen in the Router tab:
- relay: the router reports to a NetMon relay every minute; once a minute NetMon asks
  the relay for the rows it hasn't seen yet. The relay keeps recording while the PC is off.
- direct: NetMon reads the router itself (routerdirect.py), no server needed.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from . import __version__, routerdirect, winutil
from .history import History
from .settings import Settings

INTERVAL = 60      # seconds between syncs
STALE_AFTER = 300  # the router is "late" when the relay hasn't heard from it for this long
SHOW_GAPS = 7 * 86400  # direct: mention router restarts that lost traffic for this long

log = logging.getLogger(__name__)


class RelayError(Exception):
    """A problem talking to the relay, worded for the person reading the Router tab."""


def _get(url: str, token: str, path: str) -> dict:
    request = urllib.request.Request(url.rstrip("/") + path, headers={
        "Authorization": "Bearer " + token, "User-Agent": f"NetMon/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=20, context=ssl.create_default_context()) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RelayError("The relay refused the token - use its READ token.") from None
        if e.code == 404:
            raise RelayError("No NetMon relay at this address.") from None
        raise RelayError(f"The relay answered with an error (HTTP {e.code}).") from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise RelayError(f"The relay's HTTPS certificate isn't valid: {reason.verify_message}.") from None
        raise RelayError(f"Can't reach the relay ({reason}).") from None
    except (TimeoutError, OSError) as e:
        raise RelayError(f"Can't reach the relay ({e}).") from None
    except ValueError:
        raise RelayError("That address answered, but not like a NetMon relay.") from None


def test(url: str, token: str) -> dict:
    """For the Test button: does this relay answer with this token?"""
    try:
        status = _get(url, token, "/api/status")
    except RelayError as e:
        return {"ok": False, "message": str(e)}
    routers = status.get("routers", [])
    if not routers:
        return {"ok": True, "message": "Connected. The relay hasn't heard from a router yet - install the router script."}
    r = routers[0]
    return {"ok": True, "message": f"Connected. Router “{r['router']}” ({r['iface']}) last reported "
                                   f"{max(0, status['server_time'] - r['last_seen'])} seconds ago."}


class RouterLink:
    def __init__(self, settings: Settings, history: History):
        self._settings = settings
        self._history = history
        self._wake = threading.Event()
        self.status: dict = {"state": "off"}  # what the Router tab shows about the connection
        self.listeners: list[Callable[[], None]] = []  # run after each sync, on this link's thread
        threading.Thread(target=self._run, name="router", daemon=True).start()

    def wake(self) -> None:
        """Sync now (settings changed)."""
        self._wake.set()

    def _run(self) -> None:
        while True:
            s = self._settings
            if not s.router_enabled:
                self.status = {"state": "off"}
            elif s.router_mode == "direct":
                password = winutil.unprotect(s.router_password)
                if not (s.router_host and s.router_user and password and s.router_wan):
                    self.status = {"state": "setup", "mode": "direct"}
                else:
                    self._read_router(s.router_host, s.router_user, password, s.router_wan)
            elif not (s.router_url and s.router_token):
                self.status = {"state": "setup", "mode": "relay"}
            else:
                self._sync(s.router_url, s.router_token)
            for listener in self.listeners:
                try:
                    listener()
                except Exception:
                    log.exception("Router listener failed")
            self._wake.wait(INTERVAL)
            self._wake.clear()

    def _read_router(self, host: str, user: str, password: str, wan: str) -> None:
        now = time.time()
        try:
            result = routerdirect.collect(self._history, host, user, password, wan, now)
        except routerdirect.RouterError as e:
            self.status = {"state": "error", "mode": "direct", "message": str(e), "checked": now}
            return
        except (KeyError, TypeError, ValueError):
            log.exception("Reading the router failed")
            self.status = {"state": "error", "mode": "direct", "message": "The router's answer wasn't understood.",
                           "checked": now}
            return
        r = result["reading"]
        gaps = [g for g in result["gaps"] if now - g["to"] < SHOW_GAPS]
        self.status = {"state": "ok", "mode": "direct", "lag": 0, "checked": now, "gaps": gaps,
                       "routers": [{"router": r["router"], "iface": r["iface"], "uptime": r["uptime"],
                                    "last_seen": now, "rules": r["rules"]}]}
        if result["stored"] and result["stored"]["span"] > 300:
            log.info("Router: read %s after %.0f min", r["router"], result["stored"]["span"] / 60)

    def _sync(self, url: str, token: str) -> None:
        try:
            status = _get(url, token, "/api/status")
            last_id = self._history.get_meta("router_last_id", 0)
            if self._history.get_meta("router_relay") != url or status.get("last_id", 0) < last_id:
                # Another relay, or this one started over: read it from the beginning.
                last_id = 0
                self._history.set_meta("router_relay", url)
            new_rows = 0
            while True:
                page = _get(url, token, f"/api/traffic?after={last_id}&limit=5000")
                rows = [dict(zip(page["fields"], row)) for row in page["rows"]]
                if rows:
                    last_id = rows[-1]["id"]
                    self._history.add_router_rows(rows, last_id)
                    new_rows += len(rows)
                if not page.get("more"):
                    break
            routers = status.get("routers", [])
            lag = (status["server_time"] - routers[0]["last_seen"]) if routers else None
            self.status = {"state": "ok" if routers and lag < STALE_AFTER else ("late" if routers else "waiting"),
                           "routers": routers, "lag": lag, "checked": time.time(), "new_rows": new_rows}
            if new_rows:
                log.info("Router: %d new rows from the relay", new_rows)
        except RelayError as e:
            self.status = {"state": "error", "message": str(e), "checked": time.time()}
        except (KeyError, TypeError, IndexError):
            self.status = {"state": "error", "message": "The relay's answer wasn't understood.", "checked": time.time()}

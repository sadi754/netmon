"""Ping a host once per second for the ping widget.

Uses Windows' own ICMP API (IcmpSendEcho, the same thing ping.exe uses), so
no admin rights and no extra process. It only pings while the widget is shown.
"""
from __future__ import annotations

import ctypes
import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from .settings import Settings

HISTORY = 60          # pings kept for the statistics
TIMEOUT_MS = 1000
_RESOLVE_EVERY = 600  # seconds: look a host name up again now and then (servers move)

log = logging.getLogger(__name__)


class _IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class _ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [("Address", wintypes.ULONG), ("Status", wintypes.ULONG), ("RoundTripTime", wintypes.ULONG),
                ("DataSize", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("Data", ctypes.c_void_p),
                ("Options", _IP_OPTION_INFORMATION)]


_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
_IcmpCreateFile = _iphlpapi.IcmpCreateFile
_IcmpCreateFile.restype = wintypes.HANDLE
_IcmpSendEcho = _iphlpapi.IcmpSendEcho
_IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
_IcmpSendEcho.restype = wintypes.DWORD

_PAYLOAD = b"NetMon ping " + bytes(20)  # 32 bytes, like Windows' ping.exe
_REPLY_SIZE = ctypes.sizeof(_ICMP_ECHO_REPLY) + len(_PAYLOAD) + 64
_STATUS = {  # IP_STATUS codes worth a word
    11002: "network unreachable", 11003: "host unreachable", 11004: "protocol unreachable",
    11005: "port unreachable", 11010: "timeout", 11013: "TTL expired", 11050: "general failure",
}


@dataclass(frozen=True)
class PingStats:
    target: str                          # what the user typed (IP or host name)
    address: str | None = None           # the IPv4 address it resolved to
    results: tuple[float | None, ...] = ()  # last round-trip times in ms, oldest first; None = lost
    problem: str | None = None           # why the last ping failed ("timeout", "name not found"...)

    @property
    def last(self) -> float | None:
        return self.results[-1] if self.results else None

    @property
    def received(self) -> list[float]:
        return [r for r in self.results if r is not None]

    @property
    def loss(self) -> float | None:
        """Lost pings in percent."""
        if not self.results:
            return None
        return 100 * (len(self.results) - len(self.received)) / len(self.results)

    @property
    def average(self) -> float | None:
        got = self.received
        return sum(got) / len(got) if got else None

    @property
    def jitter(self) -> float | None:
        """How much the ping jumps between consecutive replies (average change, ms)."""
        got = self.received
        if len(got) < 2:
            return None
        return sum(abs(b - a) for a, b in zip(got, got[1:])) / (len(got) - 1)

    def as_dict(self) -> dict:
        got = self.received
        return {"target": self.target, "address": self.address, "results": list(self.results[-30:]),
                "problem": self.problem, "last": self.last, "loss": self.loss, "average": self.average,
                "jitter": self.jitter, "best": min(got, default=None), "worst": max(got, default=None),
                "count": len(self.results)}


def _resolve(target: str) -> str:
    if ":" in target:
        raise OSError("IPv6 isn't supported yet")
    try:
        return socket.getaddrinfo(target, None, socket.AF_INET)[0][4][0]
    except socket.gaierror:
        raise OSError("name not found") from None


def _echo(handle: int, address: str) -> tuple[float | None, str | None]:
    """One ping. Returns (round-trip ms, None) or (None, reason)."""
    reply = ctypes.create_string_buffer(_REPLY_SIZE)
    destination = int.from_bytes(socket.inet_aton(address), "little")  # IPAddr: network byte order in memory
    if _IcmpSendEcho(handle, destination, _PAYLOAD, len(_PAYLOAD), None, reply, _REPLY_SIZE, TIMEOUT_MS):
        echo = _ICMP_ECHO_REPLY.from_buffer(reply)
        if echo.Status == 0:
            return float(echo.RoundTripTime), None
        return None, _STATUS.get(echo.Status, f"error {echo.Status}")
    error = ctypes.get_last_error()
    return None, _STATUS.get(error, f"error {error}")


class Pinger:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._listeners: list[Callable[[PingStats], None]] = []
        self._wake = threading.Event()
        self.stats = PingStats(settings.ping_target)
        threading.Thread(target=self._run, name="ping", daemon=True).start()

    def subscribe(self, listener: Callable[[PingStats], None]) -> None:
        self._listeners.append(listener)

    def wake(self) -> None:
        """Re-check the settings now (target changed, widget shown or hidden)."""
        self._wake.set()

    def _publish(self, stats: PingStats) -> None:
        self.stats = stats
        for listener in self._listeners:
            try:
                listener(stats)
            except Exception:
                log.exception("Ping listener failed")

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(max(0.0, seconds))
        self._wake.clear()

    def _run(self) -> None:
        handle = _IcmpCreateFile()
        target, address, resolved_at = None, None, 0.0
        results: deque[float | None] = deque(maxlen=HISTORY)
        while True:
            started = time.monotonic()
            wanted = self._settings.ping_target.strip()
            if not (self._settings.ping_visible and wanted):
                self._sleep(1.0)
                continue
            if wanted != target:  # new target: start the statistics over
                target, address = wanted, None
                results.clear()
            if address is None or started - resolved_at > _RESOLVE_EVERY:
                try:
                    address, resolved_at = _resolve(target), started
                except OSError as e:
                    address = None
                    self._publish(PingStats(target, None, tuple(results), str(e)))
                    self._sleep(5.0)
                    continue
            rtt, problem = _echo(handle, address)
            results.append(rtt)
            self._publish(PingStats(target, address, tuple(results), problem))
            self._sleep(1.0 - (time.monotonic() - started))

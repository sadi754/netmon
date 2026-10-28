"""Ping the internet target and the local gateway once a second.

Uses Windows' own ICMP API (IcmpSendEcho, the same thing ping.exe uses), so
no admin rights and no extra process. Pings go out on a fixed one-second beat,
whether or not the previous one was answered yet: a dropped connection then
shows as lost pings, not as fewer pings. NetMon pings while the ping widget is
shown or while the ping history is recorded (Settings -> Ping).
"""
from __future__ import annotations

import ctypes
import logging
import math
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass, field

from . import winutil
from .settings import Settings

KINDS = ("internet", "gateway")  # what NetMon pings: the target in Settings, and the local router
HISTORY = 60          # pings kept for the live statistics (widget, Settings)
TIMEOUT_MS = 2000     # a busy VPN can take over a second to answer; later than this = lost
_RESOLVE_EVERY = 600  # seconds: look a host name up again now and then (servers move)
_GATEWAY_EVERY = 60   # seconds: look for the default gateway again (another network, Wi-Fi...)
_RESTART_AFTER = 5    # seconds behind the beat (the PC slept): start a new beat, don't send the missed pings

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
class Echo:
    """One ping and its answer, for the ping history."""
    kind: str               # "internet" or "gateway"
    host: str               # what was pinged, as set (or the gateway found)
    t: int                  # unix second it was sent
    rtt: float | None       # round trip in ms; None = lost


@dataclass(frozen=True)
class PingStats:
    target: str                          # what the user typed (IP or host name); for the gateway the address
    address: str | None = None           # the IPv4 address it resolved to
    results: tuple[float | None, ...] = ()  # last round-trip times in ms, oldest first; None = lost
    problem: str | None = None           # why the last ping failed ("timeout", "name not found"...)
    kind: str = "internet"
    t: int | None = None                 # when the last result's ping was sent (unix second)
    auto: bool = False                   # gateway found automatically (not set by hand)

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
        return {"kind": self.kind, "target": self.target, "address": self.address, "auto": self.auto,
                "results": list(self.results[-30:]), "t": self.t,
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


_handles = threading.local()  # one ICMP handle per worker thread


def _echo(address: str) -> tuple[float | None, str | None]:
    """One ping. Returns (round-trip ms, None) or (None, reason)."""
    handle = getattr(_handles, "handle", None)
    if handle is None:
        handle = _handles.handle = _IcmpCreateFile()
    reply = ctypes.create_string_buffer(_REPLY_SIZE)
    destination = int.from_bytes(socket.inet_aton(address), "little")  # IPAddr: network byte order in memory
    if _IcmpSendEcho(handle, destination, _PAYLOAD, len(_PAYLOAD), None, reply, _REPLY_SIZE, TIMEOUT_MS):
        echo = _ICMP_ECHO_REPLY.from_buffer(reply)
        if echo.Status == 0:
            return float(echo.RoundTripTime), None
        return None, _STATUS.get(echo.Status, f"error {echo.Status}")
    error = ctypes.get_last_error()
    return None, _STATUS.get(error, f"error {error}")


@dataclass
class _Probe:
    """One thing being pinged, and its pings on their way."""
    kind: str
    target: str = ""
    auto: bool = False
    address: str | None = None
    resolved_at: float = -math.inf  # monotonic time of the last lookup (-inf: never; Windows' clock starts at boot)
    resolving: bool = False
    generation: int = 0          # bumped on a new target: answers for the old one are dropped
    problem: str | None = None
    results: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    in_flight: deque = field(default_factory=deque)   # send times, oldest first
    answers: dict = field(default_factory=dict)       # send time -> (rtt, problem), until delivered in order

    def reset(self, target: str, auto: bool) -> None:
        self.target, self.auto, self.address, self.problem = target, auto, None, None
        self.resolved_at, self.resolving = -math.inf, False
        self.generation += 1
        self.results.clear()
        self.in_flight.clear()
        self.answers.clear()

    def stats(self, t: int | None = None) -> PingStats:
        return PingStats(self.target, self.address, tuple(self.results), self.problem, self.kind, t, self.auto)


class Pinger:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._listeners: list[Callable[[PingStats], None]] = []
        self.echo_listeners: list[Callable[[Echo], None]] = []  # every answered or lost ping (ping history)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ping")  # a few pings in flight per target
        self._probes = {kind: _Probe(kind) for kind in KINDS}
        self._gateway: str | None = None
        self._gateway_at = -math.inf
        self.stats = PingStats(settings.ping_target)             # the internet target (widget, Settings)
        self.gateway_stats = PingStats("", kind="gateway", auto=True)
        threading.Thread(target=self._run, name="ping", daemon=True).start()

    def subscribe(self, listener: Callable[[PingStats], None]) -> None:
        """Called with new statistics for either kind (see PingStats.kind)."""
        self._listeners.append(listener)

    def wake(self) -> None:
        """Re-check the settings now (target changed, widget shown or hidden)."""
        self._wake.set()

    @property
    def active(self) -> bool:
        return self._settings.ping_visible or self._settings.ping_record

    def detected_gateway(self) -> str | None:
        return self._gateway

    def _publish(self, stats: PingStats) -> None:
        if stats.kind == "internet":
            self.stats = stats
        else:
            self.gateway_stats = stats
        for listener in self._listeners:
            try:
                listener(stats)
            except Exception:
                log.exception("Ping listener failed")

    def _run(self) -> None:
        beat = math.floor(time.time()) + 1
        while True:
            self._wake.wait(max(0.0, beat - time.time()))
            if self._wake.is_set():  # settings changed: apply them, keep the beat
                self._wake.clear()
                self._apply_targets()
                continue
            now = time.time()
            if now - beat > _RESTART_AFTER:  # the PC slept: no pings for the time it was away
                beat = math.floor(now)
            sent_at, beat = int(beat), beat + 1
            if not self.active:
                continue
            try:
                self._apply_targets()
                for probe in self._probes.values():
                    if probe.kind == "internet" or self._settings.ping_record:  # the gateway is only for the history
                        self._send(probe, sent_at)
            except RuntimeError:  # "cannot schedule new futures after shutdown": NetMon is exiting
                return
            except Exception:
                log.exception("Pinging failed")

    def _wanted(self, kind: str) -> tuple[str, bool]:
        """(what to ping, found automatically?) for a kind; "" = nothing."""
        if kind == "internet":
            return self._settings.ping_target.strip(), False
        manual = self._settings.ping_gateway.strip()
        if manual:
            return manual, False
        if time.monotonic() - self._gateway_at > _GATEWAY_EVERY:
            self._gateway_at = time.monotonic()
            try:
                self._gateway = winutil.default_gateway()
            except OSError:
                log.warning("Couldn't read the routing table", exc_info=True)
                self._gateway = None
        return self._gateway or "", True

    def _apply_targets(self) -> None:
        for probe in self._probes.values():
            target, auto = self._wanted(probe.kind)
            if (target, auto) != (probe.target, probe.auto):
                with self._lock:
                    probe.reset(target, auto)
                if not target and probe.kind == "gateway":
                    probe.problem = "no gateway found"
                self._publish(probe.stats())

    def _send(self, probe: _Probe, sent_at: int) -> None:
        if not probe.target:
            return
        stale = time.monotonic() - probe.resolved_at > _RESOLVE_EVERY  # also true before the first lookup
        if stale and not probe.resolving:
            probe.resolving = True
            self._pool.submit(self._resolve_job, probe, probe.generation)
        if probe.address is None:  # still looking it up (or it failed): nothing to ping yet
            return
        with self._lock:
            probe.in_flight.append(sent_at)
        self._pool.submit(self._echo_job, probe, probe.generation, sent_at, probe.address)

    def _resolve_job(self, probe: _Probe, generation: int) -> None:
        try:
            address, problem = _resolve(probe.target), None
        except OSError as e:
            address, problem = None, str(e)
        with self._lock:
            if generation != probe.generation:
                return
            probe.resolving = False
            if address:
                probe.address, probe.resolved_at = address, time.monotonic()
                return
            # Not found: try again soon. A known address is kept meanwhile (the network may just be down).
            probe.resolved_at = time.monotonic() - _RESOLVE_EVERY + (60 if probe.address else 5)
            if probe.address:
                return
            probe.problem = problem
            stats = probe.stats()
        self._publish(stats)

    def _echo_job(self, probe: _Probe, generation: int, sent_at: int, address: str) -> None:
        try:
            rtt, problem = _echo(address)
        except Exception as e:  # never leave a ping in flight forever
            log.exception("Ping failed")
            rtt, problem = None, str(e)
        with self._lock:
            if generation != probe.generation:
                return
            probe.answers[sent_at] = (rtt, problem)
            # Answers can arrive out of order (a timeout takes 2 s, the next reply may come first):
            # hand them on in the order the pings were sent.
            while probe.in_flight and probe.in_flight[0] in probe.answers:
                t = probe.in_flight.popleft()
                rtt, problem = probe.answers.pop(t)
                probe.results.append(rtt)
                probe.problem = problem
                echo = Echo(probe.kind, probe.target, t, rtt)
                for listener in self.echo_listeners:
                    try:
                        listener(echo)
                    except Exception:
                        log.exception("Ping history listener failed")
                self._publish(probe.stats(t))

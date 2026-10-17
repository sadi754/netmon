"""Host names for IP addresses (reverse DNS / PTR records), looked up in the background.

The lookups go to the PC's normal DNS server, a few at a time, and are
remembered for an hour, so the Connections tab never waits for them.
Many addresses have no name at all; that is remembered too.
"""
from __future__ import annotations

import queue
import socket
import threading
import time

WORKERS = 4
FOUND_TTL = 3600     # seconds a name is remembered
MISSING_TTL = 900    # ... and that an address has none
MAX_WAITING = 300    # lookups queued at most; more addresses wait for the next update
MAX_REMEMBERED = 20000


class ReverseDNS:
    def __init__(self):
        self._names: dict[str, tuple[str, float]] = {}  # ip -> (name or "", expires)
        self._waiting: set[str] = set()
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        for i in range(WORKERS):  # daemon threads: a slow lookup never holds up exiting
            threading.Thread(target=self._work, name=f"rdns-{i}", daemon=True).start()

    def name(self, ip: str) -> str:
        """The address's host name if already known ("" if not, or none); looks it up if needed."""
        now = time.monotonic()
        with self._lock:
            known = self._names.get(ip)
            if known and known[1] > now:
                return known[0]
            if ip not in self._waiting and len(self._waiting) < MAX_WAITING:
                self._waiting.add(ip)
                self._queue.put(ip)
        return known[0] if known else ""  # an expired name is still better than none meanwhile

    def _work(self) -> None:
        while True:
            ip = self._queue.get()
            try:
                name = socket.gethostbyaddr(ip)[0]
            except (OSError, UnicodeError):  # no PTR record, timeout, unreachable DNS...
                name = ""
            if name == ip:
                name = ""
            with self._lock:
                self._waiting.discard(ip)
                self._names[ip] = (name, time.monotonic() + (FOUND_TTL if name else MISSING_TTL))
                if len(self._names) > MAX_REMEMBERED:
                    now = time.monotonic()
                    self._names = {k: v for k, v in self._names.items() if v[1] > now}

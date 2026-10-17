"""Turn Windows' ever-growing byte counters into per-second traffic."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .adapters import Adapter, list_adapters


def selects(selection: str | None, adapter: Adapter) -> bool:
    """Does the user's choice include this adapter?

    selection None means "all physical adapters" (VPN/virtual ones are left
    out so traffic isn't counted twice); otherwise an adapter name or GUID.
    """
    if selection is None:
        return adapter.physical
    return selection.lower() in (adapter.name.lower(), adapter.guid.lower())


@dataclass(frozen=True)
class AdapterTraffic:
    adapter: Adapter
    rx: int          # bytes received during the interval
    tx: int          # bytes sent during the interval
    monitored: bool  # counted in the total?


@dataclass(frozen=True)
class Tick:
    """One measurement interval (normally one second)."""
    time: float      # wall-clock time at the end of the interval (unix seconds)
    seconds: float   # exact length of the interval
    adapters: list[AdapterTraffic]

    @property
    def rx(self) -> int:
        return sum(a.rx for a in self.adapters if a.monitored)

    @property
    def tx(self) -> int:
        return sum(a.tx for a in self.adapters if a.monitored)

    @property
    def down(self) -> float:
        """Download speed of the monitored adapters, bytes per second."""
        return self.rx / self.seconds if self.seconds else 0.0

    @property
    def up(self) -> float:
        """Upload speed of the monitored adapters, bytes per second."""
        return self.tx / self.seconds if self.seconds else 0.0


class SpeedMeter:
    """Call sample() once per interval; it returns what moved since the last call."""

    def __init__(self, selection: str | None = None):
        self.selection = selection
        self._counters: dict[str, tuple[int, int]] = {}
        self._last = time.perf_counter()
        self._read_counters()

    def _read_counters(self) -> list[Adapter]:
        adapters = list_adapters()
        self._previous, self._counters = self._counters, {
            a.guid: (a.rx_bytes, a.tx_bytes) for a in adapters
        }
        return adapters

    def sample(self) -> Tick:
        now = time.perf_counter()
        adapters = self._read_counters()
        seconds, self._last = now - self._last, now
        traffic = []
        for adapter in adapters:
            rx = tx = 0
            previous = self._previous.get(adapter.guid)
            if previous:
                # A negative difference means the adapter was reset and its
                # counters restarted from zero; count nothing for that second.
                rx = max(0, adapter.rx_bytes - previous[0])
                tx = max(0, adapter.tx_bytes - previous[1])
            traffic.append(AdapterTraffic(adapter, rx, tx, selects(self.selection, adapter)))
        return Tick(time.time(), seconds, traffic)

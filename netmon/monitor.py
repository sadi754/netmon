"""The heart of the app: measures traffic every second and tells everyone who listens."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

from .settings import Settings
from .speed import SpeedMeter, Tick

HISTORY_SECONDS = 3600  # the live graph can show up to the last hour

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.meter = SpeedMeter(settings.adapter)
        self.started = time.time()
        self.history: deque[tuple[float, float, float]] = deque(maxlen=HISTORY_SECONDS)  # (time, down, up)
        self.rx_total = 0  # bytes since NetMon started
        self.tx_total = 0
        self.last: Tick | None = None
        self._listeners: list[Callable[[Tick], None]] = []

    def subscribe(self, listener: Callable[[Tick], None]) -> None:
        self._listeners.append(listener)

    def select_adapter(self, guid: str | None) -> None:
        self.meter.selection = guid
        self.settings.adapter = guid

    async def run(self, interval: float = 1.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            deadline += interval
            await asyncio.sleep(deadline - loop.time())
            if loop.time() - deadline > interval:  # fell behind, e.g. the PC was asleep
                deadline = loop.time()
            try:
                tick = self.meter.sample()
            except OSError:
                log.exception("Reading adapter counters failed")
                continue
            self.last = tick
            self.rx_total += tick.rx
            self.tx_total += tick.tx
            self.history.append((round(tick.time, 3), round(tick.down, 1), round(tick.up, 1)))
            for listener in self._listeners:
                try:
                    listener(tick)
                except Exception:
                    log.exception("Tick listener failed")

"""Live download/upload speeds in the terminal (milestone 1)."""
from __future__ import annotations

import ctypes
import msvcrt
import sys
import time
from collections import deque
from datetime import datetime

from . import fmt
from .speed import SpeedMeter, Tick

HISTORY_SECONDS = 60
_BARS = "▁▂▃▄▅▆▇█"

# ANSI colours - the same cyan/orange as the window and tray icon
_DOWN = "\x1b[38;2;15;160;196m"
_UP = "\x1b[38;2;227;109;38m"
_DIM = "\x1b[90m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"


def _enable_ansi() -> None:
    """Let the Windows console understand colour and cursor escape codes."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


def _wait(deadline: float) -> bool:
    """Sleep until the deadline; return False if the user pressed Q or Esc."""
    while (left := deadline - time.monotonic()) > 0:
        while msvcrt.kbhit():
            if msvcrt.getwch().lower() in ("q", "\x1b"):
                return False
        time.sleep(min(left, 0.05))
    return True


def _sparkline(values: deque[float]) -> str:
    top = max(values)
    if top <= 0:
        return _BARS[0] * len(values)
    last = len(_BARS) - 1
    return "".join(_BARS[min(last, int(v / top * len(_BARS)))] for v in values)


class _View:
    def __init__(self, selection: str | None, bits: bool):
        self.selection = selection
        self.bits = bits
        self.started = time.time()
        self.down_history = deque([0.0] * HISTORY_SECONDS, maxlen=HISTORY_SECONDS)
        self.up_history = deque([0.0] * HISTORY_SECONDS, maxlen=HISTORY_SECONDS)
        self.adapter_totals: dict[str, list[int]] = {}  # guid -> [rx, tx] since start
        self.rx_total = 0
        self.tx_total = 0

    def update(self, tick: Tick) -> None:
        for a in tick.adapters:
            totals = self.adapter_totals.setdefault(a.adapter.guid, [0, 0])
            totals[0] += a.rx
            totals[1] += a.tx
        self.rx_total += tick.rx
        self.tx_total += tick.tx
        self.down_history.append(tick.down)
        self.up_history.append(tick.up)

    def render(self, tick: Tick) -> str:
        speed = lambda v: fmt.speed(v, self.bits)  # noqa: E731
        monitored = [a.adapter.name for a in tick.adapters if a.monitored]
        if self.selection is None:
            what = f"all physical adapters {_DIM}({', '.join(monitored) or 'none found'}){_RESET}"
        else:
            what = ", ".join(monitored) or f"{self.selection} {_DIM}(not found){_RESET}"
        uptime = int(time.time() - self.started)

        lines = [
            f" {_BOLD}NetMon{_RESET} {_DIM}- live network speed{_RESET}"
            f"{'':>34}{datetime.now():%H:%M:%S}  {_DIM}up {uptime // 60}m {uptime % 60:02d}s{_RESET}",
            f" {_DIM}Monitoring:{_RESET} {what}",
            "",
            f"{_DIM}    {'ADAPTER':<22} {'TYPE':<8} {'LINK':>10}  {'↓ DOWNLOAD':>12}  {'↑ UPLOAD':>12}"
            f"  {'↓ SINCE START':>13}  {'↑ SINCE START':>13}{_RESET}",
        ]
        for a in tick.adapters:
            ad = a.adapter
            marker = "●" if a.monitored else "○"
            kind = "physical" if ad.physical else "virtual"
            head = f"  {marker} {ad.name[:22]:<22} {kind:<8} {fmt.link(ad.link_speed):>10}"
            if not ad.up:
                lines.append(f"{_DIM}{head}  {'disconnected':>12}{_RESET}")
                continue
            rx_total, tx_total = self.adapter_totals.get(ad.guid, (0, 0))
            row = (f"{head}  {_DOWN}{speed(a.rx / tick.seconds):>12}{_RESET}"
                   f"  {_UP}{speed(a.tx / tick.seconds):>12}{_RESET}"
                   f"  {fmt.size(rx_total):>13}  {fmt.size(tx_total):>13}")
            lines.append(row if a.monitored else f"{_DIM}{row}{_RESET}")

        lines += [
            f"  {_DIM}{'─' * 102}{_RESET}",
            f"    {_BOLD}{'TOTAL':<22} {'':<8} {'':>10}  {_DOWN}{speed(tick.down):>12}{_RESET}"
            f"  {_BOLD}{_UP}{speed(tick.up):>12}{_RESET}"
            f"  {_BOLD}{fmt.size(self.rx_total):>13}  {fmt.size(self.tx_total):>13}{_RESET}",
            "",
            f"  {_DOWN}↓ {_sparkline(self.down_history)}{_RESET}  "
            f"{_DIM}peak{_RESET} {speed(max(self.down_history))}",
            f"  {_UP}↑ {_sparkline(self.up_history)}{_RESET}  "
            f"{_DIM}peak{_RESET} {speed(max(self.up_history))}",
            f"  {_DIM}{'last 60 seconds':^62}{_RESET}",
            "",
            f"  {_DIM}● counted in TOTAL   ○ not counted   ·   press Q or Esc to quit{_RESET}",
        ]
        # Home the cursor and overwrite in place (no flicker), clearing leftovers.
        return "\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J"

    def summary(self) -> str:
        minutes = (time.time() - self.started) / 60
        return (f"NetMon ran {minutes:.1f} min - downloaded {fmt.size(self.rx_total)}, "
                f"uploaded {fmt.size(self.tx_total)}.")


def run(selection: str | None = None, bits: bool = True, interval: float = 1.0) -> None:
    _enable_ansi()
    meter = SpeedMeter(selection)
    view = _View(selection, bits)
    out = sys.stdout
    out.write("\x1b[?25l\x1b[2J")  # hide cursor, clear screen
    try:
        deadline = time.monotonic()
        while True:
            deadline += interval
            if not _wait(deadline):
                break
            if time.monotonic() - deadline > interval:  # fell behind, e.g. PC was asleep
                deadline = time.monotonic()
            tick = meter.sample()
            view.update(tick)
            out.write(view.render(tick))
            out.flush()
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?25h\n")  # show cursor again
        print(view.summary())

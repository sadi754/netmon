"""Start NetMon:  python -m netmon [options]   (run.bat does this for you)"""
from __future__ import annotations

import argparse
import sys

from . import __version__, fmt
from .adapters import list_adapters
from .speed import selects


def _print_adapters() -> None:
    print(f"  {'NAME':<24} {'TYPE':<9} {'STATUS':<13} {'LINK':>11}  DESCRIPTION")
    for a in list_adapters():
        kind = "physical" if a.physical else "virtual"
        status = "up" if a.up else "disconnected"
        print(f"  {a.name:<24} {kind:<9} {status:<13} {fmt.link(a.link_speed):>11}  {a.description}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="netmon", description=f"NetMon {__version__} - network bandwidth monitor")
    parser.add_argument("--minimized", action="store_true", help="start in the tray without opening the window")
    parser.add_argument("--console", action="store_true", help="show live speeds in this terminal instead")
    parser.add_argument("--list", action="store_true", help="list network adapters and exit")
    parser.add_argument("--adapter", metavar="NAME",
                        help='terminal mode: adapter to monitor, e.g. "Ethernet" (default: all physical)')
    parser.add_argument("--bytes", action="store_true", help="terminal mode: show KB/s, MB/s instead of bit/s")
    parser.add_argument("--debug", action="store_true", help="print detailed log messages to this terminal")
    parser.add_argument("--window", metavar="URL", help=argparse.SUPPRESS)  # internal: the window process
    args = parser.parse_args()
    if sys.stdout:  # None under pythonw.exe
        # Arrows and graph bars must never crash output that isn't a real console
        # (e.g. redirected to a file); unprintable characters become "?".
        sys.stdout.reconfigure(errors="replace")

    if args.window:
        from . import logs, window
        logs.setup("window.log", args.debug)
        window.run(args.window)
    elif args.list:
        _print_adapters()
    elif args.console:
        from . import console
        if args.adapter and not any(selects(args.adapter, a) for a in list_adapters()):
            parser.error(f'no adapter named "{args.adapter}" - run with --list to see the names')
        console.run(args.adapter, bits=not args.bytes)
    else:
        from . import app
        app.run(minimized=args.minimized, debug=args.debug)


if __name__ == "__main__":
    main()

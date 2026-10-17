"""Log to data/<file> (and the terminal, when there is one)."""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from .paths import DATA_DIR

log = logging.getLogger("netmon")


def setup(filename: str, debug: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(DATA_DIR / filename, maxBytes=1_000_000, backupCount=1, encoding="utf-8")]
    if sys.stderr:  # pythonw.exe has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Without a console, crashes would vanish silently - write them to the log.
    sys.excepthook = lambda *exc: log.critical("Unhandled error", exc_info=exc)
    threading.excepthook = lambda a: log.critical("Unhandled error in thread %s", a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

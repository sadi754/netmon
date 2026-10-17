"""Where NetMon keeps its files.

From the source (run.bat): everything in the project folder, data in <project>/data.
As NetMon.exe (PyInstaller): the program's own files come from the bundle; the data
goes to a "data" folder next to NetMon.exe if there is one (portable use, e.g. on
a USB stick), otherwise to %LOCALAPPDATA%\\NetMon.
"""
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))  # running as NetMon.exe
if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent         # the folder with NetMon.exe
    BUNDLE = Path(getattr(sys, "_MEIPASS", APP_DIR))         # the program's files (its "_internal" folder)
else:
    APP_DIR = BUNDLE = Path(__file__).resolve().parent.parent  # the project folder

ROOT = BUNDLE
WEB_DIR = BUNDLE / "web"  # the user interface (HTML/CSS/JS + bundled libraries)
RELAY_TEMPLATE = BUNDLE / "relay" / "router" / "netmon-feed.rsc.template"  # for the Router tab's script maker


def _data_dir() -> Path:
    if not FROZEN or (APP_DIR / "data").is_dir():
        return APP_DIR / "data"
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "NetMon"


DATA_DIR = _data_dir()  # settings, logs, the usage database and the GeoIP files

"""User settings, kept in data/settings.json."""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields

from .paths import DATA_DIR

SETTINGS_FILE = DATA_DIR / "settings.json"
UNITS = ("bits", "bytes")
QUOTA_PERIODS = ("month", "days", "day")
QUOTA_COUNTS = ("both", "download")
QUOTA_SOURCES = ("pc", "router")
CONN_VIEWS = ("list", "apps")
MAP_VIEWS = ("2d", "3d")

log = logging.getLogger(__name__)
_save_lock = threading.Lock()  # the window, tray and mini graph may all save at once


def is_country_code(value) -> bool:
    """Empty (automatic) or two capital letters like "IR"."""
    return isinstance(value, str) and (value == "" or (len(value) == 2 and value.isascii()
                                                       and value.isalpha() and value.isupper()))


@dataclass
class Settings:
    adapter: str | None = None   # adapter GUID to monitor; None = all physical adapters
    units: str = "bits"          # "bits" shows Mbit/s, "bytes" shows MB/s
    mini_visible: bool = True    # floating mini graph shown?
    mini_x: int | None = None    # its screen position; None = bottom-right corner
    mini_y: int | None = None
    mini_opacity: float = 0.9
    mini_docked: bool = False    # sitting on the taskbar?
    mini_dock_x: int | None = None  # ... this many pixels from the taskbar's left end
    ping_visible: bool = True    # floating ping widget shown? (it only pings while shown)
    ping_x: int | None = None
    ping_y: int | None = None
    ping_opacity: float = 0.9
    ping_docked: bool = False
    ping_dock_x: int | None = None
    ping_target: str = "8.8.8.8"
    hide_in_fullscreen: bool = True  # hide the widgets while a game/video is full-screen
    quota_enabled: bool = False
    quota_period: str = "month"      # "month" (Jalali), "days" (N days from quota_start) or "day"
    quota_days: int = 30
    quota_start: str = ""            # Jalali date "1405/07/01", for "days"
    quota_limit_gb: float = 50.0     # 1 GB = 1024 MB
    quota_count: str = "both"        # "both" (download + upload) or "download"
    quota_source: str = "pc"         # "pc" (this computer) or "router" (whole network, via the relay)
    quota_domestic_percent: int = 100  # how much domestic traffic counts (router source): 50 = half price
    router_enabled: bool = False     # Settings -> Integrations: MikroTik router (shows the Router tab)
    router_url: str = ""             # the NetMon relay, e.g. https://netmon.example.com
    router_token: str = ""           # the relay's READ token
    rdns_enabled: bool = True        # look up host names of connections (reverse DNS)
    home_country: str = ""           # ISO code ("IR") for "domestic"; "" = the country set in Windows
    conn_hide_local: bool = True     # Connections tab: hide this PC's own and LAN connections
    conn_show_listening: bool = False  # ... show listening ports / UDP sockets
    conn_view: str = "list"          # ... "list" or "apps" (grouped by program)
    capture_enabled: bool = True     # per-program traffic with Npcap packet capture
    usage_exclude_lan: bool = True   # usage and quota leave out local-network traffic (needs the capture)
    map_view: str = "2d"             # Map tab: "2d" (flat map) or "3d" (globe)

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable %s: %s", SETTINGS_FILE, e)
            return cls()
        settings = cls()
        # Take each known value only if it has the expected type (a hand-edited
        # file with a typo shouldn't stop NetMon from starting).
        for f in fields(cls):
            value, default = data.get(f.name), getattr(settings, f.name)
            if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)  # JSON writes 50.0 as 50
            if f.name in data and (value is None or default is None or isinstance(value, type(default))):
                setattr(settings, f.name, value)
        if settings.units not in UNITS:
            settings.units = "bits"
        if settings.quota_period not in QUOTA_PERIODS:
            settings.quota_period = "month"
        if settings.quota_count not in QUOTA_COUNTS:
            settings.quota_count = "both"
        if not 1 <= settings.quota_days <= 366:
            settings.quota_days = 30
        if settings.quota_source not in QUOTA_SOURCES:
            settings.quota_source = "pc"
        if not 0 <= settings.quota_domestic_percent <= 100:
            settings.quota_domestic_percent = 100
        if not is_country_code(settings.home_country):
            settings.home_country = ""
        if settings.conn_view not in CONN_VIEWS:
            settings.conn_view = "list"
        if settings.map_view not in MAP_VIEWS:
            settings.map_view = "2d"
        for widget in ("mini", "ping"):
            opacity = getattr(settings, f"{widget}_opacity")
            if not isinstance(opacity, (int, float)) or not 0.2 <= opacity <= 1:
                setattr(settings, f"{widget}_opacity", 0.9)
            if not (isinstance(getattr(settings, f"{widget}_x"), int)
                    and isinstance(getattr(settings, f"{widget}_y"), int)):
                setattr(settings, f"{widget}_x", None)
                setattr(settings, f"{widget}_y", None)
        return settings

    def save(self) -> None:
        with _save_lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file first so a crash can't leave a half-written file.
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, SETTINGS_FILE)

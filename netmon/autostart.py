"""Start NetMon when you sign in to Windows: the per-user "Run" registry key (no admin rights).

NetMon starts in the tray only (--minimized). If the NetMon folder moves, the
entry is pointed at the new place on the next start.
"""
from __future__ import annotations

import sys
import sysconfig
import winreg
from pathlib import Path

from .paths import APP_DIR, FROZEN

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_NAME = "NetMon"


def command() -> str:
    if FROZEN:
        return f'"{sys.executable}" --minimized'
    pythonw = Path(sys.executable).with_name("pythonw.exe")  # no console window
    return f'"{pythonw}" -m netmon --minimized'


def _registered() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            return winreg.QueryValueEx(key, _NAME)[0]
    except OSError:
        return None


def enabled() -> bool:
    return _registered() is not None


def set_enabled(on: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            _point_python_at_project()
            winreg.SetValueEx(key, _NAME, 0, winreg.REG_SZ, command())
        elif _registered() is not None:
            winreg.DeleteValue(key, _NAME)


def refresh() -> None:
    """At start: if NetMon starts with Windows from another place (the folder moved), fix the entry."""
    registered = _registered()
    if registered is not None and registered != command():
        set_enabled(True)


def _point_python_at_project() -> None:
    """From the source, "python -m netmon" only works inside the project folder, and Windows starts
    it elsewhere - a .pth file in the project's Python environment tells it where NetMon is."""
    if FROZEN:
        return
    pth = Path(sysconfig.get_paths()["purelib"]) / "netmon-dev.pth"
    if not pth.exists() or pth.read_text(encoding="utf-8").strip() != str(APP_DIR):
        pth.write_text(f"{APP_DIR}\n", encoding="utf-8")

"""Small Windows helpers (Win32 API via ctypes)."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.restype = wintypes.LPARAM
_user32.CreateIconFromResourceEx.argtypes = [
    ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, wintypes.UINT]
_user32.CreateIconFromResourceEx.restype = wintypes.HICON
_user32.DestroyIcon.argtypes = [wintypes.HICON]
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE

_ERROR_ALREADY_EXISTS = 183
_GW_OWNER = 4
_SW_RESTORE = 9
_WM_SETICON = 0x0080
_SM_CXICON = 11
_SM_CXSMICON = 49

_instance_mutex = None


def claim_single_instance(name: str = "Local\\NetMon") -> bool:
    """True if we are the only NetMon running; the claim lasts until we exit.
    (NETMON_INSTANCE=<suffix> runs a separate copy, e.g. to check a build next to the one in use.)"""
    global _instance_mutex
    _instance_mutex = _kernel32.CreateMutexW(None, False, name + os.environ.get("NETMON_INSTANCE", ""))
    return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def enable_dpi_awareness() -> None:
    """Draw sharp icons and text on scaled (125%, 150%...) displays."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except OSError:
        pass


def set_app_id(app_id: str = "NetMon") -> None:
    """Give our windows their own taskbar button instead of grouping them under Python."""
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


def region_code() -> str:
    """The country set in Windows (Settings -> Time & language -> Region), e.g. "DE"; "" if none."""
    buffer = ctypes.create_unicode_buffer(10)
    if not _kernel32.GetUserDefaultGeoName(buffer, 10):
        return ""
    return buffer.value.upper() if len(buffer.value) == 2 and buffer.value.isalpha() else ""


def allow_any_foreground() -> None:
    """Let another process bring its window to the front (we were just launched by the user)."""
    _user32.AllowSetForegroundWindow(-1)  # ASFW_ANY


def small_icon_size() -> int:
    """Tray and title-bar icon size in pixels (16 at 100% scaling, 24 at 150%...)."""
    return _user32.GetSystemMetrics(_SM_CXSMICON)


def big_icon_size() -> int:
    """Taskbar and Alt+Tab icon size in pixels."""
    return _user32.GetSystemMetrics(_SM_CXICON)


def windows_of(pid: int) -> list[int]:
    """Visible top-level windows that belong to a process."""
    found = []

    @_WNDENUMPROC
    def check(hwnd, _):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.IsWindowVisible(hwnd) and not _user32.GetWindow(hwnd, _GW_OWNER):
            found.append(hwnd)
        return True

    _user32.EnumWindows(check, 0)
    return found


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
_DESKTOP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_WS_CAPTION = 0x00C00000


def fullscreen_monitor() -> int | None:
    """The screen (monitor handle) on which a full-screen app - a game, a full-screen
    video - is in front right now, or None."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    name = ctypes.create_unicode_buffer(64)
    _user32.GetClassNameW(hwnd, name, 64)
    if name.value in _DESKTOP_CLASSES:  # the desktop itself covers the screen too
        return None
    # A maximized normal window has a title bar; full-screen games and videos don't.
    if _user32.GetWindowLongW(hwnd, -16) & _WS_CAPTION == _WS_CAPTION:  # GWL_STYLE
        return None
    window, info = wintypes.RECT(), _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    monitor = _user32.MonitorFromWindow(hwnd, 0)  # MONITOR_DEFAULTTONULL
    if not monitor or not _user32.GetWindowRect(hwnd, ctypes.byref(window)) \
            or not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    screen = info.rcMonitor
    covers = (window.left <= screen.left and window.top <= screen.top
              and window.right >= screen.right and window.bottom >= screen.bottom)
    return monitor if covers else None


def monitor_of(hwnd: int) -> int | None:
    return _user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST


_user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
_user32.MonitorFromRect.restype = wintypes.HANDLE


def on_screen(x: int, y: int) -> bool:
    """Is the point (top-left of a widget) on any screen? A second monitor may be gone."""
    return bool(_user32.MonitorFromRect(ctypes.byref(wintypes.RECT(x, y, x + 40, y + 20)), 0))


def bring_to_front(hwnd: int) -> None:
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
    _user32.SetForegroundWindow(hwnd)


def style_dark_window(hwnd: int, caption_rgb: tuple[int, int, int]) -> None:
    """Dark title bar (Windows 10/11) and a caption colour matching the app (Windows 11)."""
    dwmapi = ctypes.windll.dwmapi
    dark = ctypes.c_int(1)
    dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 20, ctypes.byref(dark), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
    r, g, b = caption_rgb
    colour = wintypes.DWORD(r | g << 8 | b << 16)  # COLORREF is 0x00BBGGRR
    dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 35, ctypes.byref(colour), 4)  # DWMWA_CAPTION_COLOR


def icon_from_png(png: bytes, size: int) -> int:
    """Turn PNG bytes into a Windows icon handle (HICON) without touching the disk."""
    hicon = _user32.CreateIconFromResourceEx(png, len(png), True, 0x00030000, size, size, 0)
    if not hicon:
        raise ctypes.WinError(ctypes.get_last_error())
    return hicon


def destroy_icon(hicon: int) -> None:
    _user32.DestroyIcon(hicon)


def set_window_icon(hwnd: int, small: int, big: int) -> None:
    _user32.SendMessageW(hwnd, _WM_SETICON, 0, small)  # ICON_SMALL: title bar
    _user32.SendMessageW(hwnd, _WM_SETICON, 1, big)    # ICON_BIG: taskbar, Alt+Tab

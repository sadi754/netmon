"""Small Windows helpers (Win32 API via ctypes)."""
from __future__ import annotations

import base64
import ctypes
import os
import socket
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
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
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
_WM_SYSCOMMAND = 0x0112
_SC_RESTORE = 0xF120
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


class _MIB_IPFORWARDROW(ctypes.Structure):
    _fields_ = [("dest", wintypes.DWORD), ("mask", wintypes.DWORD), ("policy", wintypes.DWORD),
                ("next_hop", wintypes.DWORD), ("if_index", wintypes.DWORD), ("type", wintypes.DWORD),
                ("proto", wintypes.DWORD), ("age", wintypes.DWORD), ("next_hop_as", wintypes.DWORD),
                ("metric1", wintypes.DWORD), ("metric2", wintypes.DWORD), ("metric3", wintypes.DWORD),
                ("metric4", wintypes.DWORD), ("metric5", wintypes.DWORD)]


_GetIpForwardTable = ctypes.WinDLL("iphlpapi").GetIpForwardTable
_GetIpForwardTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.BOOL]
_GetIpForwardTable.restype = wintypes.DWORD


def default_gateway() -> str | None:
    """The IPv4 router this PC's internet traffic goes to (its default route with the
    lowest metric), e.g. "192.168.88.1"; None if there is none. VPN routes without a
    next hop (on-link tunnels) are skipped, so this stays the local network's router."""
    size = wintypes.ULONG(0)
    for _ in range(3):  # the table can grow between the two calls
        buffer = ctypes.create_string_buffer(size.value or 4)
        result = _GetIpForwardTable(buffer, ctypes.byref(size), False)
        if result == 0:
            break
        if result != 122:  # ERROR_INSUFFICIENT_BUFFER
            return None
    else:
        return None
    count = wintypes.DWORD.from_buffer(buffer).value
    rows = (_MIB_IPFORWARDROW * count).from_buffer(buffer, ctypes.sizeof(wintypes.DWORD))
    defaults = [r for r in rows if r.dest == 0 and r.mask == 0 and r.next_hop != 0]
    if not defaults:
        return None
    best = min(defaults, key=lambda r: r.metric1)
    return socket.inet_ntoa(best.next_hop.to_bytes(4, "little"))  # stored in network byte order


def is_minimized(hwnd: int) -> bool:
    return bool(_user32.IsIconic(hwnd))


def bring_to_front(hwnd: int) -> None:
    if _user32.IsIconic(hwnd):
        # what a taskbar click does; ShowWindow(SW_RESTORE) from another process often leaves WebView2 unpainted
        _user32.PostMessageW(hwnd, _WM_SYSCOMMAND, _SC_RESTORE, 0)
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


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
_crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _dpapi(data: bytes, protect: bool) -> bytes:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if protect:
        ok = _crypt32.CryptProtectData(ctypes.byref(blob_in), "NetMon", None, None, None,
                                       _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    else:
        ok = _crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                         _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def protect(text: str) -> str:
    """Encrypt a secret so only this Windows user on this PC can read it back (DPAPI)."""
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii") if text else ""


def unprotect(blob: str) -> str:
    """protect() undone; "" if it can't be read (another user or PC)."""
    if not blob:
        return ""
    try:
        return _dpapi(base64.b64decode(blob), False).decode("utf-8")
    except (OSError, ValueError):
        return ""


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.UINT]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_PW_CLIENTONLY, _PW_RENDERFULLCONTENT = 0x1, 0x2


def window_pixels(hwnd: int) -> tuple[int, int, bytes] | None:
    """What a window of THIS process shows in its client area, as (width, height, BGRA bytes),
    including content drawn by a browser engine (PrintWindow with PW_RENDERFULLCONTENT)."""
    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)) or rect.right <= 0 or rect.bottom <= 0:
        return None
    width, height = rect.right, rect.bottom
    screen = _user32.GetDC(None)
    memory = _gdi32.CreateCompatibleDC(screen)
    bitmap = _gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = _gdi32.SelectObject(memory, bitmap)
    try:
        if not _user32.PrintWindow(hwnd, memory, _PW_CLIENTONLY | _PW_RENDERFULLCONTENT):
            return None
        header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32)  # top-down BGRA
        pixels = ctypes.create_string_buffer(width * height * 4)
        _gdi32.SelectObject(memory, previous)
        if not _gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header), 0):
            return None
        return width, height, pixels.raw
    finally:
        _gdi32.SelectObject(memory, previous)  # a bitmap still selected into a DC can't be deleted
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(memory)
        _user32.ReleaseDC(None, screen)

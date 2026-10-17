"""Floating desktop widgets: small always-on-top windows drawn with Pillow.

Each widget is a native Windows "layered" window (real per-pixel transparency),
not a browser, so keeping one on screen costs about a megabyte. Drag it anywhere;
double-click opens NetMon; right-click for opacity, hiding and widget-specific items.
Drop it on the taskbar and it sits there in a compact form; drag it up to take it off.
A widget class only draws itself (render, render_docked) and may add menu items.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import winreg
from collections.abc import Callable
from ctypes import wintypes
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import winutil
from .settings import Settings

OPACITIES = (1.0, 0.9, 0.75, 0.6, 0.45)

# Shared look
BG = (13, 18, 27)
PANEL = (20, 27, 39)
BORDER = (40, 50, 68)
TEXT = (230, 237, 246)
MUTED = (120, 132, 150)
SS = 3  # draw 3x larger, then shrink: smooth lines and corners
_RADIUS = 10

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- drawing helpers

@lru_cache(maxsize=32)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(name, size)  # Windows fonts, e.g. C:\Windows\Fonts\seguisb.ttf
    except OSError:
        return ImageFont.load_default(size)


def canvas(width: int, height: int, scale: float) -> tuple[Image.Image, ImageDraw.ImageDraw, float]:
    """A 3x-size canvas with the widget background. Returns (image, draw, k): k converts
    layout pixels (at 100% scaling) to canvas pixels."""
    k = scale * SS
    image = Image.new("RGB", (round(width * scale) * SS, round(height * scale) * SS), BG)
    return image, ImageDraw.Draw(image, "RGBA"), k  # "RGBA" mode blends translucent fills


def shrink(big: Image.Image, k: float) -> Image.Image:
    """Add the border and shrink to the final size. Draw text after this, so it stays crisp."""
    ImageDraw.Draw(big).rounded_rectangle((0, 0, big.width - 1, big.height - 1), radius=_RADIUS * k,
                                          outline=BORDER, width=max(1, round(k)))
    return big.resize((big.width // SS, big.height // SS), Image.LANCZOS)


def round_corners(image: Image.Image, k: float) -> Image.Image:
    """Make everything outside the rounded rectangle fully transparent."""
    mask = Image.new("L", (image.width * SS, image.height * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1), radius=_RADIUS * k, fill=255)
    image.putalpha(mask.resize(image.size, Image.LANCZOS))
    return image


def fit(text: str, max_width: float, typeface: ImageFont.FreeTypeFont) -> str:
    """Shorten text with an ellipsis so it fits."""
    if typeface.getlength(text) <= max_width:
        return text
    while text and typeface.getlength(text + "…") > max_width:
        text = text[:-1]
    return text + "…"


# On the taskbar: no background of our own, text in the taskbar's light or dark style.
TASKBAR_TEXT = {False: TEXT, True: (28, 32, 38)}  # key: light taskbar?
_HIT_ALPHA = 4  # fully transparent pixels let clicks through; this is invisible but still "there"


def docked_canvas(width: int, height: int, light: bool) -> Image.Image:
    return Image.new("RGBA", (width, height), (255, 255, 255, _HIT_ALPHA) if light else (0, 0, 0, _HIT_ALPHA))


def paint(image: Image.Image, mask: Image.Image, colour: tuple[int, int, int], alpha: int = 255) -> None:
    """Lay one colour over the image wherever the mask (an "L" image, any size) is set."""
    if mask.size != image.size:
        mask = mask.resize(image.size, Image.LANCZOS)
    if alpha != 255:
        mask = mask.point(lambda v: v * alpha // 255)
    layer = Image.new("RGBA", image.size, (*colour, 0))
    layer.putalpha(mask)
    image.alpha_composite(layer)


def _premultiplied_bgra(image: Image.Image) -> bytes:
    """Windows wants layered-window pixels as BGRA with colours pre-multiplied by alpha."""
    r, g, b, a = image.split()
    r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


# ---------------------------------------------------------------- Win32 plumbing

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def _proto(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


_H, _P = wintypes.HANDLE, ctypes.c_void_p
_GetModuleHandleW = _proto(_kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
_RegisterClassW = _proto(_user32, "RegisterClassW", wintypes.ATOM, ctypes.POINTER(_WNDCLASSW))
_CreateWindowExW = _proto(_user32, "CreateWindowExW", wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR,
                          wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, _P)
_DefWindowProcW = _proto(_user32, "DefWindowProcW", _LRESULT, wintypes.HWND, wintypes.UINT,
                         wintypes.WPARAM, wintypes.LPARAM)
_GetMessageW = _proto(_user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG),
                      wintypes.HWND, wintypes.UINT, wintypes.UINT)
_TranslateMessage = _proto(_user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_DispatchMessageW = _proto(_user32, "DispatchMessageW", _LRESULT, ctypes.POINTER(wintypes.MSG))
_PostMessageW = _proto(_user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT,
                       wintypes.WPARAM, wintypes.LPARAM)
_PostQuitMessage = _proto(_user32, "PostQuitMessage", None, ctypes.c_int)
_DestroyWindow = _proto(_user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
_ShowWindow = _proto(_user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_LoadCursorW = _proto(_user32, "LoadCursorW", _H, wintypes.HINSTANCE, _P)
_UpdateLayeredWindow = _proto(_user32, "UpdateLayeredWindow", wintypes.BOOL, wintypes.HWND, wintypes.HDC,
                              ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                              ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
                              ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD)
_GetDC = _proto(_user32, "GetDC", wintypes.HDC, wintypes.HWND)
_ReleaseDC = _proto(_user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, wintypes.HDC)
_CreateCompatibleDC = _proto(_gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
_DeleteDC = _proto(_gdi32, "DeleteDC", wintypes.BOOL, wintypes.HDC)
_CreateDIBSection = _proto(_gdi32, "CreateDIBSection", wintypes.HBITMAP, wintypes.HDC, _P,
                           wintypes.UINT, ctypes.POINTER(_P), _H, wintypes.DWORD)
_SelectObject = _proto(_gdi32, "SelectObject", _H, wintypes.HDC, _H)
_DeleteObject = _proto(_gdi32, "DeleteObject", wintypes.BOOL, _H)
_GetWindowRect = _proto(_user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_GetDpiForWindow = _proto(_user32, "GetDpiForWindow", wintypes.UINT, wintypes.HWND)
_GetDpiForSystem = _proto(_user32, "GetDpiForSystem", wintypes.UINT)
_MonitorFromRect = _proto(_user32, "MonitorFromRect", _H, ctypes.POINTER(wintypes.RECT), wintypes.DWORD)
_SystemParametersInfoW = _proto(_user32, "SystemParametersInfoW", wintypes.BOOL, wintypes.UINT,
                                wintypes.UINT, _P, wintypes.UINT)
_GetCursorPos = _proto(_user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_SetForegroundWindow = _proto(_user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
_CreatePopupMenu = _proto(_user32, "CreatePopupMenu", wintypes.HMENU)
_AppendMenuW = _proto(_user32, "AppendMenuW", wintypes.BOOL, wintypes.HMENU, wintypes.UINT,
                      ctypes.c_size_t, wintypes.LPCWSTR)
_SetMenuDefaultItem = _proto(_user32, "SetMenuDefaultItem", wintypes.BOOL, wintypes.HMENU,
                             wintypes.UINT, wintypes.UINT)
_TrackPopupMenu = _proto(_user32, "TrackPopupMenu", wintypes.BOOL, wintypes.HMENU, wintypes.UINT,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, _P)
_DestroyMenu = _proto(_user32, "DestroyMenu", wintypes.BOOL, wintypes.HMENU)
_FindWindowW = _proto(_user32, "FindWindowW", wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_FindWindowExW = _proto(_user32, "FindWindowExW", wintypes.HWND, wintypes.HWND, wintypes.HWND,
                        wintypes.LPCWSTR, wintypes.LPCWSTR)
_IsWindowVisible = _proto(_user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
_SetWindowLongPtrW = _proto(_user32, "SetWindowLongPtrW", ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int,
                            ctypes.c_ssize_t)
_SetWindowPos = _proto(_user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_RegisterWindowMessageW = _proto(_user32, "RegisterWindowMessageW", wintypes.UINT, wintypes.LPCWSTR)
_GetWindow = _proto(_user32, "GetWindow", wintypes.HWND, wintypes.HWND, wintypes.UINT)
_SetTimer = _proto(_user32, "SetTimer", ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, _P)
_KillTimer = _proto(_user32, "KillTimer", wintypes.BOOL, wintypes.HWND, ctypes.c_size_t)
_WINEVENTPROC = ctypes.WINFUNCTYPE(None, _H, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG,
                                   wintypes.DWORD, wintypes.DWORD)
_SetWinEventHook = _proto(_user32, "SetWinEventHook", _H, wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                          _WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
_UnhookWinEvent = _proto(_user32, "UnhookWinEvent", wintypes.BOOL, _H)


class _APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


_SHAppBarMessage = _proto(ctypes.WinDLL("shell32"), "SHAppBarMessage", ctypes.c_size_t, wintypes.DWORD,
                          ctypes.POINTER(_APPBARDATA))

_WS_POPUP = 0x80000000
_WS_EX_TOPMOST, _WS_EX_TOOLWINDOW, _WS_EX_LAYERED, _WS_EX_NOACTIVATE = 0x8, 0x80, 0x80000, 0x08000000
_WM_NULL, _WM_DESTROY, _WM_CLOSE, _WM_MOUSEACTIVATE = 0x0000, 0x0002, 0x0010, 0x0021
_WM_SETTINGCHANGE, _WM_CONTEXTMENU, _WM_DISPLAYCHANGE, _WM_NCHITTEST = 0x001A, 0x007B, 0x007E, 0x0084
_WM_NCLBUTTONDBLCLK, _WM_NCRBUTTONDOWN, _WM_NCRBUTTONUP = 0x00A3, 0x00A4, 0x00A5
_WM_MOVING, _WM_EXITSIZEMOVE, _WM_DPICHANGED = 0x0216, 0x0232, 0x02E0
_GWLP_HWNDPARENT = -8  # a top-level window's owner: owned windows always stay above their owner
_HWND_TOPMOST = -1
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE = 0x1, 0x2, 0x10
_SPI_SETWORKAREA = 0x002F
_ABM_GETSTATE, _ABS_AUTOHIDE = 0x4, 0x1
_MF_GRAYED = 0x1
# Explorer broadcasts this when it (re)creates the taskbar, e.g. after Explorer restarts.
_WM_TASKBARCREATED = _RegisterWindowMessageW("TaskbarCreated")
_WM_REDRAW, _WM_UPDATE_SHOWN = 0x8001, 0x8002  # our own messages (WM_APP + n)
_WM_TIMER, _RAISE_TIMER = 0x0113, 1
_GW_HWNDPREV = 3
_EVENT_SYSTEM_FOREGROUND, _WINEVENT_OUTOFCONTEXT, _WINEVENT_SKIPOWNPROCESS = 0x3, 0x0, 0x2
_HTCAPTION, _MA_NOACTIVATE = 2, 3
_SW_HIDE, _SW_SHOWNOACTIVATE = 0, 4
_IDC_ARROW = 32512
_ULW_ALPHA, _AC_SRC_ALPHA = 2, 1
_MF_STRING, _MF_CHECKED, _MF_POPUP, _MF_SEPARATOR = 0x0, 0x8, 0x10, 0x800
_TPM_RIGHTBUTTON, _TPM_NONOTIFY, _TPM_RETURNCMD = 0x2, 0x80, 0x100
_SPI_GETWORKAREA = 0x0030
_MENU_OPEN, _MENU_HIDE, _MENU_DOCK, _MENU_OPACITY = 1, 2, 3, 100
MENU_CUSTOM = 1000  # widget-specific menu items use ids from here


# ---------------------------------------------------------------- the taskbar

def taskbar() -> tuple[int, wintypes.RECT] | None:
    """The main screen's taskbar (window, rectangle) if a widget can sit on it:
    visible, horizontal and not set to auto-hide."""
    hwnd = _FindWindowW("Shell_TrayWnd", None)
    if not hwnd or not _IsWindowVisible(hwnd):
        return None
    data = _APPBARDATA(cbSize=ctypes.sizeof(_APPBARDATA))
    if _SHAppBarMessage(_ABM_GETSTATE, ctypes.byref(data)) & _ABS_AUTOHIDE:
        return None
    rect = wintypes.RECT()
    _GetWindowRect(hwnd, ctypes.byref(rect))
    if rect.right - rect.left <= rect.bottom - rect.top:  # on the left/right edge (Windows 10)
        return None
    return hwnd, rect


def tray_left(bar: int, rect: wintypes.RECT) -> int:
    """Where the notification area (clock, tray icons) starts."""
    tray = _FindWindowExW(bar, None, "TrayNotifyWnd", None)
    if tray:
        r = wintypes.RECT()
        _GetWindowRect(tray, ctypes.byref(r))
        if rect.left < r.left < rect.right:
            return r.left
    return rect.right - (rect.bottom - rect.top) * 5


def taskbar_is_light() -> bool:
    """Windows' "Choose your default Windows mode": light taskbar or dark (the default)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except OSError:
        return False


class FloatingWidget:
    """Base class. The window lives on its own thread with its own message loop;
    other threads only post messages to it."""

    key = ""            # settings prefix: "mini" -> mini_visible, mini_x, mini_y, mini_opacity, mini_docked...
    title = ""          # name in menus, e.g. "Mini graph"
    width = height = 0  # size at 100% display scaling
    corner_offset = 0   # default spot: this many pixels above the bottom-right corner

    instances: list[FloatingWidget] = []  # all widgets: docked ones make room for each other

    def __init__(self, settings: Settings, on_open: Callable[[], None]):
        self._settings = settings
        self._on_open = on_open
        self.listeners: list[Callable[[], None]] = []  # told when the widget is shown or hidden
        self._hwnd = None
        self._scale = 1.0
        self._size = (0, 0)       # of the last drawing, in pixels
        self._suppressed = False  # temporarily hidden for a full-screen app
        self._dock_height = 0     # on the taskbar: the widget's height in pixels; 0 = floating
        self._light = False       # light taskbar (dark text)?
        self._ready = threading.Event()
        FloatingWidget.instances.append(self)
        threading.Thread(target=self._thread_main, name=self.key, daemon=True).start()
        self._ready.wait(5)

    # ---- for subclasses

    def render(self, scale: float) -> Image.Image:
        raise NotImplementedError

    def render_docked(self, scale: float, height: int, light: bool) -> Image.Image:
        """The compact form on the taskbar: exactly `height` pixels tall, see docked_canvas()."""
        raise NotImplementedError

    def menu_items(self) -> list[tuple[int, str]]:
        """Extra right-click menu entries: (id >= MENU_CUSTOM, label)."""
        return []

    def on_menu(self, item: int) -> None:
        pass

    # ---- safe to call from any thread

    @property
    def visible(self) -> bool:
        return getattr(self._settings, f"{self.key}_visible")

    @property
    def docked(self) -> bool:
        return bool(self._dock_height)

    def set_visible(self, visible: bool) -> None:
        setattr(self._settings, f"{self.key}_visible", visible)
        self._settings.save()
        self._post(_WM_UPDATE_SHOWN)
        for listener in self.listeners:
            listener()

    def hide_for_fullscreen(self, monitor: int | None) -> None:
        """Called every second with the screen a full-screen app is using (or None)."""
        suppressed = bool(monitor) and bool(self._hwnd) and winutil.monitor_of(self._hwnd) == monitor
        if suppressed != self._suppressed:
            self._suppressed = suppressed
            self._post(_WM_UPDATE_SHOWN)

    def redraw(self) -> None:
        if self.visible and not self._suppressed:  # hidden -> no drawing at all
            self._post(_WM_REDRAW)

    def close(self) -> None:
        self._post(_WM_CLOSE)

    def screen_rect(self) -> wintypes.RECT | None:
        """Where the widget is on screen (also while hidden)."""
        if not self._hwnd:
            return None
        rect = wintypes.RECT()
        _GetWindowRect(self._hwnd, ctypes.byref(rect))
        # Before its first drawing the window is still 1x1 pixels; use its real size.
        width, height = self._size if self._size[0] else (round(self.width * self._scale),
                                                          round(self.height * self._scale))
        rect.right, rect.bottom = rect.left + width, rect.top + height
        return rect

    def _post(self, message: int, wparam: int = 0) -> None:
        if self._hwnd:
            _PostMessageW(self._hwnd, message, wparam, 0)

    def _setting(self, name: str):
        return getattr(self._settings, f"{self.key}_{name}")

    def _save(self, **values) -> None:
        for name, value in values.items():
            setattr(self._settings, f"{self.key}_{name}", value)
        self._settings.save()

    # ---- the window's own thread

    def _thread_main(self) -> None:
        class_name = f"NetMon.{self.key}"
        try:
            instance = _GetModuleHandleW(None)
            self._wndproc = _WNDPROC(self._handle)  # keep a reference: Windows calls it later
            cls = _WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=instance, lpszClassName=class_name,
                             hCursor=_LoadCursorW(None, ctypes.c_void_p(_IDC_ARROW)))
            _RegisterClassW(ctypes.byref(cls))
            x, y = self._saved_position() or self._default_position(_GetDpiForSystem() / 96)
            self._hwnd = _CreateWindowExW(
                _WS_EX_LAYERED | _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
                class_name, f"NetMon {self.title.lower()}", _WS_POPUP, x, y, 1, 1, None, None, instance, None)
            if not self._hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._scale = _GetDpiForWindow(self._hwnd) / 96
        except Exception:
            log.exception("Could not create the %s window", self.title)
            return
        finally:
            self._ready.set()
        if self._setting("docked"):
            self._dock(self._setting("dock_x"))  # no taskbar yet (just signed in)? TaskbarCreated docks it
        self._update_shown()
        # Another window comes to the front (also Win+D): the taskbar may have been raised without us.
        self._foreground_proc = _WINEVENTPROC(self._on_foreground)  # keep a reference
        self._hook = _SetWinEventHook(_EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, None,
                                      self._foreground_proc, 0, 0,
                                      _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
        msg = wintypes.MSG()
        while _GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _TranslateMessage(ctypes.byref(msg))
            _DispatchMessageW(ctypes.byref(msg))

    def _update_shown(self) -> None:
        if self.visible and not self._suppressed:
            self._present()
            _ShowWindow(self._hwnd, _SW_SHOWNOACTIVATE)
            self._keep_above_taskbar()
        else:
            _ShowWindow(self._hwnd, _SW_HIDE)

    def _handle(self, hwnd, msg, wparam, lparam):
        try:
            if msg == _WM_NCHITTEST:
                return _HTCAPTION  # the whole widget acts as a title bar: Windows does the dragging
            if msg == _WM_MOUSEACTIVATE:
                return _MA_NOACTIVATE  # clicking it doesn't steal focus from your work or game
            if msg == _WM_REDRAW:
                self._present()
                self._keep_above_taskbar()  # also a once-a-second safety net
                return 0
            if msg == _WM_TIMER and wparam == _RAISE_TIMER:
                _KillTimer(hwnd, _RAISE_TIMER)
                self._keep_above_taskbar()
                return 0
            if msg == _WM_UPDATE_SHOWN:
                self._update_shown()
                return 0
            if msg == _WM_NCLBUTTONDBLCLK:
                self._on_open()
                return 0
            if msg in (_WM_NCRBUTTONDOWN, _WM_CONTEXTMENU):
                return 0  # no system menu
            if msg == _WM_NCRBUTTONUP:
                self._context_menu()
                return 0
            if msg == _WM_MOVING:
                self._while_dragging(wintypes.RECT.from_address(lparam))
                return 1
            if msg == _WM_EXITSIZEMOVE:  # finished dragging
                self._dropped()
                return 0
            if msg == _WM_DPICHANGED:  # moved to a screen with different scaling
                self._scale = (wparam & 0xFFFF) / 96
                suggested = wintypes.RECT.from_address(lparam)
                if self.docked:
                    self._dock(self._setting("dock_x"))
                else:
                    self._present(position=(suggested.left, suggested.top))
                return 0
            if msg == _WM_DISPLAYCHANGE:  # screens changed: don't get lost off-screen
                if self.docked:
                    self._dock(self._setting("dock_x"))
                    return 0
                rect = wintypes.RECT()
                _GetWindowRect(hwnd, ctypes.byref(rect))
                if not _MonitorFromRect(ctypes.byref(rect), 0):
                    self._present(position=self._default_position(self._scale))
                return 0
            if msg == _WM_SETTINGCHANGE and self.docked:
                if wparam == _SPI_SETWORKAREA:  # the taskbar changed size or place
                    self._dock(self._setting("dock_x"))
                elif lparam and ctypes.wstring_at(lparam) == "ImmersiveColorSet":  # light/dark mode
                    self._light = taskbar_is_light()
                    self._present()
                return 0
            if msg == _WM_TASKBARCREATED:  # Explorer restarted: sit on the new taskbar
                if self._setting("docked"):
                    self._dock(self._setting("dock_x"))
                    self._update_shown()
                return 0
            if msg == _WM_CLOSE:
                _DestroyWindow(hwnd)
                return 0
            if msg == _WM_DESTROY:
                if getattr(self, "_hook", None):
                    _UnhookWinEvent(self._hook)
                _PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("%s: message %#x failed", self.title, msg)
        return _DefWindowProcW(hwnd, msg, wparam, lparam)

    def _draw(self) -> Image.Image:
        if self.docked:
            return self.render_docked(self._scale, self._dock_height, self._light)
        return self.render(self._scale)

    def _present(self, position: tuple[int, int] | None = None, image: Image.Image | None = None) -> None:
        """Draw the widget and hand the pixels to Windows."""
        image = image or self._draw()
        pixels = _premultiplied_bgra(image)
        width, height = self._size = image.size
        screen = _GetDC(None)
        memory_dc = _CreateCompatibleDC(screen)
        header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32)  # top-down
        bits = ctypes.c_void_p()
        bitmap = _CreateDIBSection(memory_dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, pixels, len(pixels))
            previous = _SelectObject(memory_dc, bitmap)
            blend = _BLENDFUNCTION(0, 0, round(self._setting("opacity") * 255), _AC_SRC_ALPHA)
            # Position stays as is unless asked; moving it here mid-drag would make it jump.
            point = ctypes.byref(wintypes.POINT(*position)) if position else None
            _UpdateLayeredWindow(self._hwnd, screen, point, ctypes.byref(wintypes.SIZE(width, height)),
                                 memory_dc, ctypes.byref(wintypes.POINT(0, 0)), 0,
                                 ctypes.byref(blend), _ULW_ALPHA)
            _SelectObject(memory_dc, previous)
        finally:
            _DeleteObject(bitmap)
            _DeleteDC(memory_dc)
            _ReleaseDC(None, screen)

    # ---- on the taskbar and off it again

    def _on_foreground(self, hook, event, hwnd, id_object, id_child, thread, time) -> None:
        if self.docked:  # look again once the shell has finished rearranging
            _SetTimer(self._hwnd, _RAISE_TIMER, 150, None)

    def _keep_above_taskbar(self) -> None:
        """Windows sometimes raises the taskbar without the windows it owns (leaving full
        screen, Win+D); a docked widget would stay hidden behind it until you click the
        taskbar. Put it back directly above the taskbar."""
        if not self.docked or not self.visible or self._suppressed:
            return
        found = taskbar()
        if not found:
            return
        bar = found[0]
        window = self._hwnd
        for _ in range(10_000):  # walk up the z-order: is the taskbar above us?
            window = _GetWindow(window, _GW_HWNDPREV)
            if not window:
                return
            if window == bar:
                break
        above = _GetWindow(bar, _GW_HWNDPREV)  # we go between this one and the taskbar
        _SetWindowPos(self._hwnd, above or _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)

    def _while_dragging(self, rect: wintypes.RECT) -> None:
        """Docked and still over the taskbar: slide along it, don't wander up and down."""
        bar = taskbar()
        if not self.docked or not bar or not _contains(bar[1], _cursor()):
            return
        top = bar[1].top + (bar[1].bottom - bar[1].top - (rect.bottom - rect.top)) // 2
        rect.top, rect.bottom = top, top + (rect.bottom - rect.top)

    def _dropped(self) -> None:
        """Let go after dragging: onto the taskbar, along it, or off it."""
        rect = wintypes.RECT()
        _GetWindowRect(self._hwnd, ctypes.byref(rect))
        bar = taskbar()
        cursor = _cursor()
        if bar and _contains(bar[1], cursor):
            if self.docked:
                self._dock(rect.left - bar[1].left)
            else:  # dropped onto the taskbar: centre the compact form under the mouse
                self._dock(cursor.x - bar[1].left, centre=True)
        elif self.docked:
            self._undock(cursor)
        else:
            self._save(x=rect.left, y=rect.top)

    def _dock(self, x: int | None, centre: bool = False) -> bool:
        """Sit on the taskbar, `x` pixels from its left end (None = a free spot near the clock)."""
        found = taskbar()
        if not found:
            return False
        bar, rect = found
        margin = round(4 * self._scale)
        self._dock_height = rect.bottom - rect.top - 2 * margin
        self._light = taskbar_is_light()
        image = self._draw()
        if x is None:
            x = self._free_spot(bar, rect, image.width) - rect.left
        elif centre:
            x -= image.width // 2
        x = max(0, min(x, rect.right - rect.left - image.width))
        # Owned by the taskbar: Windows keeps us above it, also after you click it.
        _SetWindowLongPtrW(self._hwnd, _GWLP_HWNDPARENT, bar)
        self._present(position=(rect.left + x, rect.top + margin), image=image)
        self._save(docked=True, dock_x=x)
        return True

    def _undock(self, near: wintypes.POINT | None = None) -> None:
        """Back to a floating widget: centred on `near` (the mouse), else where it was before."""
        self._dock_height = 0
        _SetWindowLongPtrW(self._hwnd, _GWLP_HWNDPARENT, 0)
        image = self._draw()
        position = None
        if near is not None:
            position = (near.x - image.width // 2, near.y - image.height // 2)
            if not winutil.on_screen(*position):
                position = None
        position = position or self._saved_position() or self._default_position(self._scale)
        if _on_taskbar(taskbar(), position, image.size):  # don't land back on the taskbar
            position = self._default_position(self._scale)
        self._present(position=position, image=image)
        _SetWindowPos(self._hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        self._save(docked=False, x=position[0], y=position[1])

    def _free_spot(self, bar: int, rect: wintypes.RECT, width: int) -> int:
        """Left of the clock, and left of any widget already sitting there."""
        gap = round(6 * self._scale)
        right = tray_left(bar, rect) - gap
        taken = sorted((r.left, r.right) for w in FloatingWidget.instances
                       if w is not self and w.docked and w.visible and (r := w.screen_rect()))
        for left, end in reversed(taken):
            if left < right and end > right - width:  # in the way: go left of it
                right = left - gap
        return max(rect.left, right - width)

    def _context_menu(self) -> None:
        menu = _CreatePopupMenu()
        opacity_menu = _CreatePopupMenu()
        for i, value in enumerate(OPACITIES):
            checked = _MF_CHECKED if abs(value - self._setting("opacity")) < 0.01 else 0
            _AppendMenuW(opacity_menu, _MF_STRING | checked, _MENU_OPACITY + i, f"{round(value * 100)}%")
        _AppendMenuW(menu, _MF_STRING, _MENU_OPEN, "Open NetMon")
        _SetMenuDefaultItem(menu, _MENU_OPEN, False)
        for item, label in self.menu_items():
            _AppendMenuW(menu, _MF_STRING, item, label)
        if self.docked:
            _AppendMenuW(menu, _MF_STRING, _MENU_DOCK, "Move out of taskbar")
        elif taskbar():
            _AppendMenuW(menu, _MF_STRING, _MENU_DOCK, "Move to taskbar")
        else:
            _AppendMenuW(menu, _MF_STRING | _MF_GRAYED, _MENU_DOCK, "Move to taskbar (not with auto-hide)")
        _AppendMenuW(menu, _MF_POPUP, opacity_menu, "Opacity")
        _AppendMenuW(menu, _MF_SEPARATOR, 0, None)
        _AppendMenuW(menu, _MF_STRING, _MENU_HIDE, f"Hide {self.title.lower()}")
        cursor = _cursor()
        _SetForegroundWindow(self._hwnd)  # otherwise the menu won't close when you click elsewhere
        choice = _TrackPopupMenu(menu, _TPM_RETURNCMD | _TPM_RIGHTBUTTON | _TPM_NONOTIFY,
                                 cursor.x, cursor.y, 0, self._hwnd, None)
        _PostMessageW(self._hwnd, _WM_NULL, 0, 0)
        _DestroyMenu(menu)  # also destroys the submenu
        if choice == _MENU_OPEN:
            self._on_open()
        elif choice == _MENU_HIDE:
            self.set_visible(False)
        elif choice == _MENU_DOCK:
            if self.docked:
                self._undock()
            else:
                self._dock(None)
        elif choice >= MENU_CUSTOM:
            self.on_menu(choice)
        elif choice >= _MENU_OPACITY:
            self._save(opacity=OPACITIES[choice - _MENU_OPACITY])
            self._present()

    def _saved_position(self) -> tuple[int, int] | None:
        x, y = self._setting("x"), self._setting("y")
        if x is None or y is None:
            return None
        return (x, y) if winutil.on_screen(x, y) else None

    def _default_position(self, scale: float) -> tuple[int, int]:
        """Bottom-right corner of the main screen, just above the taskbar."""
        area = wintypes.RECT()
        _SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(area), 0)
        margin = round(16 * scale)
        return (area.right - round(self.width * scale) - margin,
                area.bottom - round((self.height + self.corner_offset) * scale) - margin)


def _cursor() -> wintypes.POINT:
    point = wintypes.POINT()
    _GetCursorPos(ctypes.byref(point))
    return point


def _contains(rect: wintypes.RECT, point: wintypes.POINT) -> bool:
    return rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom


def _on_taskbar(bar: tuple[int, wintypes.RECT] | None, position: tuple[int, int],
                    size: tuple[int, int]) -> bool:
    """Would a widget at `position` have its middle on the taskbar?"""
    if not bar:
        return False
    return _contains(bar[1], wintypes.POINT(position[0] + size[0] // 2, position[1] + size[1] // 2))

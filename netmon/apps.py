"""Which program a process is: its name, description ("Google Chrome") and icon.

Windows lists every process with its .exe name (Toolhelp snapshot, no rights
needed); the full path, description and icon come from the .exe file itself.
svchost.exe hosts Windows services, so for it the services' names are shown
("Delivery Optimization", "DNS Client"...) - that's what actually uses the network.
"""
from __future__ import annotations

import ctypes
import io
import logging
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

from PIL import Image

from . import icons

ICON_SIZE = 32          # pixels; shown at about 18 px, so it stays sharp on 150% displays
_SERVICES_EVERY = 30.0  # seconds between re-reading which services run in which svchost

log = logging.getLogger(__name__)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_version = ctypes.WinDLL("version", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32")
_user32 = ctypes.WinDLL("user32")
_gdi32 = ctypes.WinDLL("gdi32")


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


class _SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD), ("dwProcessId", wintypes.DWORD), ("dwServiceFlags", wintypes.DWORD)]


class _ENUM_SERVICE_STATUS_PROCESSW(ctypes.Structure):
    _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpDisplayName", wintypes.LPWSTR),
                ("ServiceStatusProcess", _SERVICE_STATUS_PROCESS)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_advapi32.OpenSCManagerW.restype = wintypes.HANDLE
_advapi32.EnumServicesStatusExW.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR]
_advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
_version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(wintypes.UINT)]
_shell32.SHDefExtractIconW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.UINT,
                                       ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT]
_shell32.SHDefExtractIconW.restype = ctypes.c_long
_user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON, ctypes.c_int,
                               ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
_user32.DestroyIcon.argtypes = [wintypes.HICON]
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                    wintypes.HANDLE, wintypes.DWORD]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]

_TH32CS_SNAPPROCESS = 0x2
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SC_MANAGER_ENUMERATE_SERVICE = 0x4
_SERVICE_WIN32 = 0x30
_SERVICE_ACTIVE = 0x1
_ERROR_MORE_DATA = 234

# Processes without a file of their own.
_SPECIAL = {
    0: ("", "Closed connections", "Connections a program has already closed; Windows keeps them a little while."),
    4: ("System", "Windows (System)", "The Windows kernel: file sharing (SMB), NetBIOS, HTTP.sys..."),
}


def _system32_file(exe: str) -> str:
    candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", exe)
    return candidate if exe and os.path.isfile(candidate) else ""


def process_table() -> dict[int, tuple[str, int]]:
    """PID -> (.exe name, parent PID) of every running process."""
    table = {}
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
        more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[entry.th32ProcessID] = (entry.szExeFile, entry.th32ParentProcessID)
            more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return table


def _descendants(pid: int, table: dict[int, tuple[str, int]]) -> set[int]:
    """Processes started by pid, and the ones they started, and so on."""
    children: dict[int, list[int]] = {}
    for child, (_, parent) in table.items():
        if child != parent:
            children.setdefault(parent, []).append(child)
    found, todo = set(), [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


def process_path(pid: int) -> str:
    """Full path of a process's .exe, or "" if Windows won't tell (protected processes)."""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ""
    finally:
        _kernel32.CloseHandle(handle)


def services_by_pid() -> dict[int, list[str]]:
    """PID -> display names of the Windows services running in it."""
    manager = _advapi32.OpenSCManagerW(None, None, _SC_MANAGER_ENUMERATE_SERVICE)
    if not manager:
        return {}
    try:
        needed, count, resume = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD(0)
        size = 64 * 1024
        for _ in range(4):
            buffer = ctypes.create_string_buffer(size)
            ok = _advapi32.EnumServicesStatusExW(manager, 0, _SERVICE_WIN32, _SERVICE_ACTIVE, buffer, size,
                                                 ctypes.byref(needed), ctypes.byref(count), ctypes.byref(resume), None)
            if ok:
                break
            if ctypes.get_last_error() != _ERROR_MORE_DATA:
                return {}
            size = needed.value + 4096
            resume.value = 0
        else:
            return {}
        services: dict[int, list[str]] = {}
        for s in (_ENUM_SERVICE_STATUS_PROCESSW * count.value).from_buffer(buffer):
            pid = s.ServiceStatusProcess.dwProcessId
            if pid:
                services.setdefault(pid, []).append(s.lpDisplayName or s.lpServiceName)
        return services
    finally:
        _advapi32.CloseServiceHandle(manager)


def file_description(path: str) -> str:
    """The "File description" from an .exe's version information, e.g. "Google Chrome"."""
    size = _version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not _version.GetFileVersionInfoW(path, 0, size, data):
        return ""
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    languages = []
    if _version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        pairs = (wintypes.WORD * (length.value // 2)).from_address(pointer.value)
        languages = [(pairs[i], pairs[i + 1]) for i in range(0, len(pairs) - 1, 2)]
    for language, codepage in [*languages, (0x0409, 0x04B0), (0x0409, 0x04E4), (0x0000, 0x04B0)]:
        key = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\FileDescription"
        if _version.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)) and length.value > 1:
            text = ctypes.wstring_at(pointer.value, length.value).split("\0")[0].strip()
            if text:
                return text
    return ""


def _draw_icon(icon: int, background: int) -> bytes:
    """The icon drawn on a solid background, as 32-bit BGRA pixels."""
    dc = _gdi32.CreateCompatibleDC(None)
    header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=ICON_SIZE,
                               biHeight=-ICON_SIZE, biPlanes=1, biBitCount=32)  # negative height: top-down rows
    bits = ctypes.c_void_p()
    bitmap = _gdi32.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    if not bitmap:
        _gdi32.DeleteDC(dc)
        raise OSError("CreateDIBSection failed")
    old = _gdi32.SelectObject(dc, bitmap)
    try:
        ctypes.memset(bits, background, ICON_SIZE * ICON_SIZE * 4)
        _user32.DrawIconEx(dc, 0, 0, icon, ICON_SIZE, ICON_SIZE, 0, None, 0x3)  # DI_NORMAL
        _gdi32.GdiFlush()
        return ctypes.string_at(bits, ICON_SIZE * ICON_SIZE * 4)
    finally:
        _gdi32.SelectObject(dc, old)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(dc)


def file_icon_png(path: str) -> bytes | None:
    """An .exe's icon as a PNG with transparency, or None if it has none."""
    icon = wintypes.HICON()
    if _shell32.SHDefExtractIconW(path, 0, 0, ctypes.byref(icon), None, ICON_SIZE) != 0 or not icon:
        return None
    try:
        # Drawing on black and on white tells how transparent each pixel is; this works
        # for modern icons with an alpha channel and for old ones with a mask alike.
        black, white = _draw_icon(icon, 0x00), _draw_icon(icon, 0xFF)
    finally:
        _user32.DestroyIcon(icon)
    rgba = bytearray(len(black))
    for i in range(0, len(black), 4):
        alpha = 255 - (white[i + 1] - black[i + 1])
        if alpha <= 0:
            continue
        rgba[i] = min(255, black[i + 2] * 255 // alpha)      # BGRA -> RGBA, undoing the blend
        rgba[i + 1] = min(255, black[i + 1] * 255 // alpha)
        rgba[i + 2] = min(255, black[i] * 255 // alpha)
        rgba[i + 3] = alpha
    if not any(rgba[3::4]):
        return None
    out = io.BytesIO()
    Image.frombytes("RGBA", (ICON_SIZE, ICON_SIZE), bytes(rgba)).save(out, "PNG", optimize=True)
    return out.getvalue()


@dataclass
class App:
    """A program as the Connections tab shows it."""
    id: int                # small number the window uses to refer to it
    key: str               # what stays the same across restarts: the .exe's path, the services...
    exe: str               # "chrome.exe" ("" for the special entries)
    name: str              # "Google Chrome", or the services hosted by svchost.exe
    path: str = ""         # full path of the .exe, "" if unknown
    note: str = ""         # extra explanation (special entries, all services of an svchost)
    icon: bytes | None = field(default=None, repr=False)  # PNG

    def as_dict(self) -> dict:
        return {"exe": self.exe, "name": self.name, "path": self.path, "note": self.note, "icon": self.icon is not None}


class AppCatalog:
    """Remembers which program each process is. Shared by the connections watcher and the packet capture."""

    def __init__(self):
        self._lock = threading.RLock()
        self._refreshed = 0.0
        self.names: dict[int, str] = {}                       # pid -> exe name, from the last refresh
        self._window: set[int] = set()                        # NetMon's window processes (incl. WebView2)
        self._logo: bytes | None = None
        self._by_pid: dict[tuple[int, str], App] = {}          # (pid, exe) -> its app
        self._by_key: dict[str, App] = {}                      # path (+ services) -> app, so ids stay stable
        self._files: dict[str, tuple[str, bytes | None]] = {}  # path -> (description, icon)
        self._services: dict[int, list[str]] = {}
        self._services_read = 0.0
        self.by_id: dict[int, App] = {}                        # read by the web server for icons

    def refresh(self) -> None:
        """Take a fresh list of processes."""
        table = process_table()
        with self._lock:
            self._refreshed = time.monotonic()
            self.names = {pid: exe for pid, (exe, _) in table.items()}
            self._window = _descendants(os.getpid(), table)
            alive = set(self.names.items())
            for key in [k for k in self._by_pid if k not in alive]:
                del self._by_pid[key]  # the process has exited (its PID may be reused)

    def for_pid(self, pid: int) -> App:
        with self._lock:
            if pid not in self.names and time.monotonic() - self._refreshed > 0.5:
                self.refresh()  # a process started since the last look
            exe = self.names.get(pid, "")
            app = self._by_pid.get((pid, exe))
            if app is None:
                app = self._by_pid[(pid, exe)] = self._identify(pid, exe)
            return app

    def icon_for(self, key: str, path: str, exe: str) -> bytes | None:
        """The icon of a program from the usage history (it may not be running now)."""
        with self._lock:
            if key.startswith("netmon"):
                return self._netmon_logo()
            source = path or _system32_file(exe)
            return self._file_info(source)[1] if source and os.path.isfile(source) else None

    def _netmon_logo(self) -> bytes:
        if self._logo is None:
            self._logo = icons.png(icons.logo(ICON_SIZE))
        return self._logo

    def _identify(self, pid: int, exe: str) -> App:
        if pid in _SPECIAL:
            key, name, note = _SPECIAL[pid]
            return self._app(f"special:{pid}", exe=key, name=name, note=note)
        if pid == os.getpid() or pid in self._window:  # ourselves: "NetMon", not "Python"
            if pid == os.getpid():
                return self._app("netmon", exe=exe, name="NetMon", path=process_path(pid), icon=self._netmon_logo(),
                                 note="NetMon itself: its local web server, router sync, GeoIP download, host-name lookups.")
            return self._app("netmon-window", exe=exe, name="NetMon window", icon=self._netmon_logo(),  # several processes
                             note="NetMon's window (Edge WebView2), talking to NetMon on this PC.")
        path = process_path(pid)
        services = self._services_of(pid) if exe.lower() == "svchost.exe" else []
        # Windows hides the path of processes running as SYSTEM; Windows' own programs
        # live in System32, so their description and icon can still be read from there.
        details_from = path or _system32_file(exe)
        description, icon = self._file_info(details_from) if details_from else ("", None)
        if services:
            name = services[0] if len(services) == 1 else f"{services[0]} +{len(services) - 1}"
            return self._app(f"{path or exe}|{'|'.join(services)}", exe=exe, name=name, path=path,
                             note="Windows services: " + ", ".join(services), icon=icon)
        if not exe:
            return self._app(f"pid:{pid}", exe="", name=f"Process {pid}", note="This process has exited.")
        name = description or PureWindowsPath(exe).stem
        return self._app(path.lower() or exe.lower(), exe=exe, name=name, path=path, icon=icon)

    def _app(self, key: str, **details) -> App:
        app = self._by_key.get(key)
        if app is None:
            app = self._by_key[key] = App(id=len(self._by_key) + 1, key=key, **details)
            self.by_id[app.id] = app
        return app

    def _file_info(self, path: str) -> tuple[str, bytes | None]:
        if path not in self._files:
            try:
                self._files[path] = (file_description(path), file_icon_png(path))
            except OSError:
                log.debug("No details for %s", path, exc_info=True)
                self._files[path] = ("", None)
        return self._files[path]

    def _services_of(self, pid: int) -> list[str]:
        now = time.monotonic()
        if pid not in self._services and now - self._services_read > 2:  # a newly started service
            self._services_read = 0.0
        if now - self._services_read > _SERVICES_EVERY:
            self._services = services_by_pid()
            self._services_read = now
        return sorted(self._services.get(pid, []))

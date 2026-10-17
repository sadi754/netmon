"""The PC's TCP and UDP sockets, read straight from Windows (like `netstat -ano`).

GetExtendedTcpTable / GetExtendedUdpTable (iphlpapi.dll) list every socket with
the process that owns it and when it was opened - no admin rights needed.
UDP has no "connection", so Windows only knows the local side of a UDP socket;
where UDP packets go (games, QUIC/HTTP3, DNS) needs packet capture (milestone 6).
"""
from __future__ import annotations

import ctypes
import socket
import time
from ctypes import wintypes
from dataclasses import dataclass

_TCP_TABLE_OWNER_MODULE_ALL = 8  # every TCP socket, with owner PID and creation time
_UDP_TABLE_OWNER_MODULE = 2
_ERROR_INSUFFICIENT_BUFFER = 122
_MODULE_INFO = ctypes.c_uint64 * 16  # TCPIP_OWNING_MODULE_SIZE

TCP_STATES = {
    1: "Closed", 2: "Listening", 3: "SYN sent", 4: "SYN received", 5: "Established",
    6: "FIN wait 1", 7: "FIN wait 2", 8: "Close wait", 9: "Closing", 10: "Last ACK",
    11: "Time wait", 12: "Delete TCB",
}
_LISTEN = 2


class _TCPROW(ctypes.Structure):
    _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD),
                ("liCreateTimestamp", ctypes.c_int64), ("OwningModuleInfo", _MODULE_INFO)]


class _TCP6ROW(ctypes.Structure):
    _fields_ = [("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("ucRemoteAddr", ctypes.c_ubyte * 16),
                ("dwRemoteScopeId", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD),
                ("dwState", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD),
                ("liCreateTimestamp", ctypes.c_int64), ("OwningModuleInfo", _MODULE_INFO)]


class _UDPROW(ctypes.Structure):
    _fields_ = [("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD),
                ("liCreateTimestamp", ctypes.c_int64), ("dwFlags", ctypes.c_int), ("OwningModuleInfo", _MODULE_INFO)]


class _UDP6ROW(ctypes.Structure):
    _fields_ = [("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD),
                ("liCreateTimestamp", ctypes.c_int64), ("dwFlags", ctypes.c_int), ("OwningModuleInfo", _MODULE_INFO)]


assert [ctypes.sizeof(r) for r in (_TCPROW, _TCP6ROW, _UDPROW, _UDP6ROW)] == [160, 192, 160, 176], \
    "connection table row layout mismatch"

_iphlpapi = ctypes.WinDLL("iphlpapi")
_GetExtendedTcpTable = _iphlpapi.GetExtendedTcpTable
_GetExtendedUdpTable = _iphlpapi.GetExtendedUdpTable
for _fn in (_GetExtendedTcpTable, _GetExtendedUdpTable):
    _fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    _fn.restype = wintypes.DWORD


@dataclass(frozen=True)
class Socket:
    proto: str          # "TCP" or "UDP"
    local: str          # local IP address
    lport: int
    remote: str         # remote IP address, "" when there is none (listening, UDP)
    rport: int
    state: str          # "Established", "Listening", ... ("" for UDP)
    pid: int            # owning process (0 = the system, for closed connections)
    opened: float       # unix time the socket was created, 0 if unknown

    @property
    def listening(self) -> bool:
        """Waiting for others to connect (a server port), or an unconnected UDP socket."""
        return not self.remote

    @property
    def key(self) -> str:
        return f"{self.proto}|{self.local}|{self.lport}|{self.remote}|{self.rport}|{self.pid}"


def _port(value: int) -> int:
    return socket.ntohs(value & 0xFFFF)


def _ipv4(value: int) -> str:
    return socket.inet_ntop(socket.AF_INET, value.to_bytes(4, "little"))  # stored in network byte order


def _ipv6(value) -> str:
    raw = bytes(value)
    if raw[:12] == bytes(10) + b"\xff\xff":  # IPv4-mapped (dual-stack sockets): show it as IPv4
        return socket.inet_ntop(socket.AF_INET, raw[12:])
    return socket.inet_ntop(socket.AF_INET6, raw)


def _unix_time(filetime: int) -> float:
    when = filetime / 1e7 - 11644473600 if filetime > 0 else 0.0  # FILETIME counts 100 ns since 1601
    return when if when < time.time() + 60 else 0.0  # some kernel sockets report nonsense: unknown


def _table(fn, family: int, kind: int, row_type) -> ctypes.Array:
    size = wintypes.DWORD(0)
    for _ in range(5):  # the table can grow between asking its size and reading it
        buffer = ctypes.create_string_buffer(size.value or 1)
        err = fn(buffer, ctypes.byref(size), False, family, kind, 0)
        if err == 0:
            break
        if err != _ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(err)
    else:
        raise ctypes.WinError(_ERROR_INSUFFICIENT_BUFFER)
    count = wintypes.DWORD.from_buffer(buffer).value
    if not count:
        return (row_type * 0)()
    offset = ctypes.alignment(row_type)  # { DWORD dwNumEntries; ROW table[]; }, rows aligned
    return (row_type * count).from_buffer(buffer, offset)


def list_sockets() -> list[Socket]:
    """Every TCP and UDP socket on this PC right now (IPv4 and IPv6)."""
    found = []
    for r in _table(_GetExtendedTcpTable, socket.AF_INET, _TCP_TABLE_OWNER_MODULE_ALL, _TCPROW):
        listening = r.dwState == _LISTEN
        found.append(Socket("TCP", _ipv4(r.dwLocalAddr), _port(r.dwLocalPort),
                            "" if listening else _ipv4(r.dwRemoteAddr), 0 if listening else _port(r.dwRemotePort),
                            TCP_STATES.get(r.dwState, str(r.dwState)), r.dwOwningPid, _unix_time(r.liCreateTimestamp)))
    for r in _table(_GetExtendedTcpTable, socket.AF_INET6, _TCP_TABLE_OWNER_MODULE_ALL, _TCP6ROW):
        listening = r.dwState == _LISTEN
        found.append(Socket("TCP", _ipv6(r.ucLocalAddr), _port(r.dwLocalPort),
                            "" if listening else _ipv6(r.ucRemoteAddr), 0 if listening else _port(r.dwRemotePort),
                            TCP_STATES.get(r.dwState, str(r.dwState)), r.dwOwningPid, _unix_time(r.liCreateTimestamp)))
    for r in _table(_GetExtendedUdpTable, socket.AF_INET, _UDP_TABLE_OWNER_MODULE, _UDPROW):
        found.append(Socket("UDP", _ipv4(r.dwLocalAddr), _port(r.dwLocalPort), "", 0, "",
                            r.dwOwningPid, _unix_time(r.liCreateTimestamp)))
    for r in _table(_GetExtendedUdpTable, socket.AF_INET6, _UDP_TABLE_OWNER_MODULE, _UDP6ROW):
        found.append(Socket("UDP", _ipv6(r.ucLocalAddr), _port(r.dwLocalPort), "", 0, "",
                            r.dwOwningPid, _unix_time(r.liCreateTimestamp)))
    return found

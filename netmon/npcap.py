"""Packet capture with Npcap (https://npcap.com), read in batches.

Npcap is a separate, free install (its licence doesn't let programs bundle it).
Its Packet.dll hands over everything captured since the last read in one go -
often a hundred packets or more - so Python makes one call per batch instead of
one per packet; that keeps the CPU cost to a few percent even on a busy link.

NetMon only keeps the first bytes of each packet (the headers: addresses, ports,
size), except for DNS answers and TLS "hello" messages, which name the sites.
"""
from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes

NPCAP_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Npcap")
HEADERS = 96      # bytes kept of each packet: Ethernet + IPv6 + TCP headers fit
NAMES = 2048      # ... of DNS answers and TLS client hellos
# DNS answers, and TLS ClientHello messages (handshake 0x16, type 1) over IPv4 or IPv6.
NAMES_FILTER = (
    "udp src port 53"
    " or (ip and tcp and tcp[((tcp[12] & 0xf0) >> 2)] = 0x16 and tcp[((tcp[12] & 0xf0) >> 2) + 5] = 1)"
    " or (ip6 and ip6[6] = 6 and ip6[40 + ((ip6[52] & 0xf0) >> 2)] = 0x16"
    " and ip6[45 + ((ip6[52] & 0xf0) >> 2)] = 1)"
)
_NDIS_PACKET_TYPE = 0x1 | 0x2 | 0x8  # directed | multicast | broadcast: this PC's traffic, not promiscuous
_BUFFER = 8 * 1024 * 1024            # driver-side buffer
_MIN_TO_COPY = 64 * 1024             # hand data over once this much is waiting...
_READ_TIMEOUT = 100                  # ... or after this many milliseconds


class NpcapError(Exception):
    """Npcap is missing or can't be used; the message is for the Settings page."""


class _BPF_INSN(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class _BPF_PROGRAM(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.POINTER(_BPF_INSN))]


class _PACKET(ctypes.Structure):
    _fields_ = [("hEvent", wintypes.HANDLE), ("OverLapped", ctypes.c_byte * 32), ("Buffer", ctypes.c_void_p),
                ("Length", ctypes.c_uint), ("ulBytesReceived", wintypes.DWORD), ("bIoComplete", ctypes.c_ubyte)]


class _BPF_STAT(ctypes.Structure):
    _fields_ = [("bs_recv", ctypes.c_uint), ("bs_drop", ctypes.c_uint), ("ps_ifdrop", ctypes.c_uint),
                ("bs_capt", ctypes.c_uint)]


_packet = _wpcap = None


def _load():
    """Load Npcap's DLLs once; raises NpcapError if it isn't installed."""
    global _packet, _wpcap
    if _packet:
        return
    if not os.path.isfile(os.path.join(NPCAP_DIR, "Packet.dll")):
        raise NpcapError("Npcap isn't installed.")
    os.add_dll_directory(NPCAP_DIR)
    try:
        packet = ctypes.WinDLL(os.path.join(NPCAP_DIR, "Packet.dll"), use_last_error=True)
        wpcap = ctypes.CDLL(os.path.join(NPCAP_DIR, "wpcap.dll"))
    except OSError as e:
        raise NpcapError(f"Npcap is installed but couldn't be loaded ({e}).") from None
    packet.PacketGetVersion.restype = ctypes.c_char_p
    packet.PacketOpenAdapter.restype = ctypes.c_void_p
    packet.PacketOpenAdapter.argtypes = [ctypes.c_char_p]
    packet.PacketCloseAdapter.argtypes = [ctypes.c_void_p]
    for name in ("PacketSetHwFilter", "PacketSetBuff", "PacketSetMinToCopy", "PacketSetReadTimeout"):
        getattr(packet, name).argtypes = [ctypes.c_void_p, ctypes.c_int]
        getattr(packet, name).restype = ctypes.c_ubyte
    packet.PacketSetBpf.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BPF_PROGRAM)]
    packet.PacketSetBpf.restype = ctypes.c_ubyte
    packet.PacketInitPacket.argtypes = [ctypes.POINTER(_PACKET), ctypes.c_void_p, ctypes.c_uint]
    packet.PacketReceivePacket.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PACKET), ctypes.c_ubyte]
    packet.PacketReceivePacket.restype = ctypes.c_ubyte
    packet.PacketGetStats.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BPF_STAT)]
    wpcap.pcap_lib_version.restype = ctypes.c_char_p
    wpcap.pcap_open_dead.restype = ctypes.c_void_p
    wpcap.pcap_compile.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BPF_PROGRAM), ctypes.c_char_p,
                                   ctypes.c_int, ctypes.c_uint]
    wpcap.pcap_freecode.argtypes = [ctypes.POINTER(_BPF_PROGRAM)]
    wpcap.pcap_close.argtypes = [ctypes.c_void_p]
    _packet, _wpcap = packet, wpcap


def version() -> str:
    """Npcap's version, e.g. "1.89"; raises NpcapError if it can't be used."""
    _load()
    return _packet.PacketGetVersion().decode(errors="replace")


class Capture:
    """Reads one adapter's packets in batches. Use from one thread."""

    def __init__(self, adapter_guid: str, keep: int = HEADERS, packet_filter: str = ""):
        _load()
        self._handle = _packet.PacketOpenAdapter(f"\\Device\\NPF_{adapter_guid}".encode())
        if not self._handle:
            error = ctypes.get_last_error()
            if error == 5:  # access denied
                raise NpcapError("Npcap only lets administrators capture. Reinstall Npcap without "
                                 "\u201cRestrict Npcap driver's access to Administrators only\u201d.")
            raise NpcapError(f"Npcap couldn't open the adapter (error {error}).")
        try:
            _packet.PacketSetHwFilter(self._handle, _NDIS_PACKET_TYPE)
            _packet.PacketSetBuff(self._handle, _BUFFER)
            _packet.PacketSetMinToCopy(self._handle, _MIN_TO_COPY)
            _packet.PacketSetReadTimeout(self._handle, _READ_TIMEOUT)
            self._set_filter(keep, packet_filter)
        except Exception:
            self.close()
            raise
        self._buffer = ctypes.create_string_buffer(1024 * 1024)
        self._packet = _PACKET()
        _packet.PacketInitPacket(ctypes.byref(self._packet), self._buffer, len(self._buffer))

    def _set_filter(self, keep: int, packet_filter: str) -> None:
        if not packet_filter:  # everything, cut to `keep` bytes: the single instruction "ret #keep"
            insns = (_BPF_INSN * 1)(_BPF_INSN(0x06, 0, 0, keep))
            if not _packet.PacketSetBpf(self._handle, ctypes.byref(_BPF_PROGRAM(1, insns))):
                raise NpcapError("Npcap refused the capture filter.")
            return
        dead = _wpcap.pcap_open_dead(1, keep)  # 1 = Ethernet; compiles the filter for that link type
        program = _BPF_PROGRAM()
        try:
            if _wpcap.pcap_compile(dead, ctypes.byref(program), packet_filter.encode(), 1, 0xFFFFFFFF) != 0:
                raise NpcapError("The capture filter didn't compile.")
            if not _packet.PacketSetBpf(self._handle, ctypes.byref(program)):
                raise NpcapError("Npcap refused the capture filter.")
        finally:
            _wpcap.pcap_freecode(ctypes.byref(program))
            _wpcap.pcap_close(dead)

    def read(self) -> bytes:
        """The next batch (waits up to a tenth of a second; b"" if nothing came).

        Each packet: a header with its lengths, then its first bytes. iterate() walks them.
        """
        if not _packet.PacketReceivePacket(self._handle, ctypes.byref(self._packet), 1):
            raise NpcapError(f"Reading packets failed (error {ctypes.get_last_error()}).")
        size = self._packet.ulBytesReceived
        return ctypes.string_at(self._buffer, size) if size else b""

    def dropped(self) -> int:
        """Packets the driver had to drop because NetMon didn't read fast enough."""
        stats = _BPF_STAT()
        _packet.PacketGetStats(self._handle, ctypes.byref(stats))
        return stats.bs_drop

    def close(self) -> None:
        if self._handle:
            _packet.PacketCloseAdapter(self._handle)
            self._handle = None


_HEADER = struct.Struct("<IIH")  # after the 8-byte timestamp: caplen, datalen (on the wire), hdrlen


def iterate(batch: bytes):
    """(offset of the frame in batch, bytes captured, length on the wire) for each packet."""
    offset, end = 0, len(batch)
    unpack = _HEADER.unpack_from
    while offset + 18 <= end:
        caplen, wire, header = unpack(batch, offset + 8)
        yield offset + header, caplen, wire
        offset += (header + caplen + 3) & ~3  # records are aligned to 4 bytes

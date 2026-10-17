"""Site names for IP addresses, learned from the PC's own traffic (packet capture).

Two sources, both read from packets this PC sends or receives anyway:
- DNS answers: a program asks "www.youtube.com?" and the answer lists its IPs.
- TLS client hellos: the start of an HTTPS connection names the site (SNI).
They give the name the program actually asked for ("www.youtube.com"), which
reverse DNS often can't ("...1e100.net"). Nothing is looked up or sent anywhere.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

KEEP = 3600            # seconds a name is remembered after it was last seen
MAX_NAMES = 50000
# Encrypted Client Hello hides the real name behind a shared public one; that one says nothing.
_ECH_COVERS = {"cloudflare-ech.com"}
_U16 = struct.Struct("!H")


class Names:
    """IP address -> site name. Written by the capture thread, read by anyone."""

    def __init__(self):
        self._names: dict[str, tuple[str, float]] = {}  # ip -> (name, last seen)
        self._lock = threading.Lock()

    def get(self, ip: str) -> str:
        found = self._names.get(ip)
        return found[0] if found else ""

    def add(self, ip: str, name: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._names[ip] = (name, now)
            if len(self._names) > MAX_NAMES:
                cutoff = now - KEEP
                self._names = {k: v for k, v in self._names.items() if v[1] > cutoff}
                if len(self._names) > MAX_NAMES:  # still too many: keep the newest
                    newest = sorted(self._names.items(), key=lambda item: item[1][1])[-MAX_NAMES // 2:]
                    self._names = dict(newest)

    def learn(self, frame: bytes) -> None:
        """Look at one captured Ethernet frame (DNS answer or TLS client hello)."""
        try:
            for ip, name in parse_frame(frame):
                self.add(ip, name)
        except (IndexError, struct.error, ValueError, UnicodeError):
            pass  # truncated or not what it seemed


def parse_frame(frame: bytes) -> list[tuple[str, str]]:
    """(ip, name) pairs from a DNS answer or a TLS ClientHello inside an Ethernet frame."""
    ethertype, offset = _U16.unpack_from(frame, 12)[0], 14
    if ethertype == 0x8100:  # VLAN tag
        ethertype, offset = _U16.unpack_from(frame, 16)[0], 18
    if ethertype == 0x0800:
        proto, l4, dst = frame[offset + 9], offset + (frame[offset] & 15) * 4, frame[offset + 16:offset + 20]
        family = socket.AF_INET
    elif ethertype == 0x86DD:
        proto, l4, dst = frame[offset + 6], offset + 40, frame[offset + 24:offset + 40]
        family = socket.AF_INET6
    else:
        return []
    if proto == 17 and _U16.unpack_from(frame, l4)[0] == 53:  # UDP from port 53: a DNS answer
        return parse_dns(frame[l4 + 8:])
    if proto == 6:
        payload = frame[l4 + (frame[l4 + 12] >> 4) * 4:]
        name = parse_client_hello(payload)
        if name:
            return [(socket.inet_ntop(family, dst), name)]
    return []


def _read_name(message: bytes, offset: int) -> tuple[str, int]:
    """A (possibly compressed) DNS name at offset -> (name, offset after it)."""
    labels, end, jumps = [], None, 0
    while True:
        length = message[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:  # pointer to a name earlier in the message
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | message[offset + 1]
            jumps += 1
            if jumps > 20:
                raise ValueError("DNS name loop")
            continue
        labels.append(message[offset + 1:offset + 1 + length].decode("ascii"))
        offset += 1 + length
    return ".".join(labels).lower(), end if end is not None else offset


def parse_dns(message: bytes) -> list[tuple[str, str]]:
    """(ip, name asked for) for each A / AAAA record in a DNS answer."""
    _, flags, questions, answers = struct.unpack_from("!HHHH", message, 0)
    if not flags & 0x8000 or flags & 0x000F or not questions or not answers:  # not a successful answer
        return []
    offset = 12
    asked, offset = _read_name(message, offset)
    offset += 4  # type, class
    for _ in range(questions - 1):
        offset = _read_name(message, offset)[1] + 4
    found = []
    for _ in range(answers):
        offset = _read_name(message, offset)[1]
        kind, _, _, size = struct.unpack_from("!HHIH", message, offset)
        offset += 10
        if kind == 1 and size == 4:
            found.append((socket.inet_ntop(socket.AF_INET, message[offset:offset + 4]), asked))
        elif kind == 28 and size == 16:
            found.append((socket.inet_ntop(socket.AF_INET6, message[offset:offset + 16]), asked))
        offset += size
    return found


def parse_client_hello(data: bytes) -> str:
    """The server name (SNI) in a TLS ClientHello, or "" (also when it's cut off)."""
    if len(data) < 9 or data[0] != 0x16 or data[5] != 0x01:
        return ""
    offset = 9 + 2 + 32                              # record + handshake headers, version, random
    offset += 1 + data[offset]                       # session id
    offset += 2 + _U16.unpack_from(data, offset)[0]  # cipher suites
    offset += 1 + data[offset]                       # compression methods
    end = min(len(data), offset + 2 + _U16.unpack_from(data, offset)[0])
    offset += 2
    while offset + 4 <= end:
        kind, size = struct.unpack_from("!HH", data, offset)
        offset += 4
        if kind == 0 and offset + size <= len(data):  # server_name: list length, type 0, name length, name
            name = data[offset + 5:offset + 5 + _U16.unpack_from(data, offset + 3)[0]].decode("ascii").lower()
            return "" if name in _ECH_COVERS else name
        offset += size
    return ""

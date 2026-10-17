"""HTTPS downloads that cope with filtered networks.

Big CDNs give a host name several addresses, and some networks silently drop a
few of them. Python tries them one after another and waits about 20 seconds
for each dead one; browsers instead try them side by side and keep the first
that answers. urlopen() here does the same.
"""
from __future__ import annotations

import http.client
import selectors
import socket
import ssl
import time
import urllib.request

STAGGER = 0.3  # seconds before trying the next address alongside the ones still connecting


def connect_fast(address: tuple[str, int], timeout: float | None = None, source_address=None,
                 *args, **kwargs) -> socket.socket:
    """Like socket.create_connection(), but with all of the host's addresses racing."""
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda info: info[0] != socket.AF_INET)  # IPv4 first: IPv6 often doesn't reach the internet
    deadline = time.monotonic() + (timeout or 30)
    selector = selectors.DefaultSelector()  # on Windows a failed connect also shows up as "writable"
    pending: list[socket.socket] = []
    error: OSError | None = None
    next_try = 0.0
    try:
        while infos or pending:
            now = time.monotonic()
            if now >= deadline:
                break
            if infos and (now >= next_try or not pending):
                family, kind, proto, _, sockaddr = infos.pop(0)
                sock = socket.socket(family, kind, proto)
                sock.setblocking(False)
                if source_address:
                    sock.bind(source_address)
                code = sock.connect_ex(sockaddr)
                if code in (0, 10035, 115, 36):  # done / WSAEWOULDBLOCK / EINPROGRESS (Linux, macOS)
                    selector.register(sock, selectors.EVENT_WRITE)
                    pending.append(sock)
                else:
                    sock.close()
                    error = OSError(code, f"can't connect to {sockaddr[0]}")
                next_try = now + STAGGER
                continue
            wait = deadline - now if not infos else min(deadline, next_try) - now
            for key, _ in selector.select(max(0.0, wait)):
                sock = key.fileobj
                selector.unregister(sock)
                pending.remove(sock)
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if code == 0:
                    sock.setblocking(True)
                    sock.settimeout(timeout)
                    return sock
                sock.close()
                error = OSError(code, "connection refused or unreachable")
        raise error or TimeoutError(f"can't connect to {host}")
    finally:
        for sock in pending:
            sock.close()
        selector.close()


class _Connection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_fast


class _Handler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_Connection, request, context=self._context)


_opener = urllib.request.build_opener(_Handler(context=ssl.create_default_context()))


def urlopen(request: urllib.request.Request, timeout: float = 30):
    return _opener.open(request, timeout=timeout)

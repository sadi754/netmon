"""Where an IP address is: country, city and the network that owns it.

From DB-IP's free "IP to City Lite" and "IP to ASN Lite" databases (licence
CC BY 4.0: "IP geolocation by DB-IP"). They are not shipped with NetMon:
Settings -> Download fetches them once (about 65 MB) into data/geoip, and DB-IP
publishes a new edition every month. Lookups happen on this PC and take
microseconds - no address is ever sent anywhere to be located.
"""
from __future__ import annotations

import gzip
import ipaddress
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path

import maxminddb

from . import __version__, net
from .paths import DATA_DIR

GEOIP_DIR = DATA_DIR / "geoip"
_KINDS = {"city": ("dbip-city-lite", "City"), "asn": ("dbip-asn-lite", "ASN")}  # file name, database type
_URL = "https://download.db-ip.com/free/{name}-{edition}.mmdb.gz"
_CHUNK = 256 * 1024
_DISTRICT = re.compile(r"\s*\(.*\)$")

log = logging.getLogger(__name__)

_LAN = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10")]
_SPECIAL = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "100.64.0.0/10",  # "this network"; carrier-grade NAT (inside the ISP)
    "192.0.0.0/24", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",  # reserved, examples
    "198.18.0.0/15",  # benchmarking - also the "fake IPs" of TUN-mode proxies (Clash, sing-box)
    "240.0.0.0/4", "::/128")]


def scope(ip: str) -> str:
    """What kind of address: "loopback" (this PC), "lan", "multicast", "special" or "internet"."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "special"
    if address.is_loopback:
        return "loopback"
    if address.is_multicast or ip == "255.255.255.255":
        return "multicast"
    if any(address in net for net in _LAN):
        return "lan"
    if any(address in net for net in _SPECIAL):
        return "special"
    return "internet"


def _edition(path: Path) -> str:
    return path.stem.rsplit("-lite-", 1)[-1]  # "dbip-city-lite-2026-10" -> "2026-10"


def _month(d: date, back: int = 0) -> str:
    m = d.year * 12 + d.month - 1 - back
    return f"{m // 12}-{m % 12 + 1:02d}"


class DownloadCancelled(Exception):
    pass


class GeoIP:
    def __init__(self, folder: Path = GEOIP_DIR):
        self._dir = folder
        self._lock = threading.Lock()  # guards the open databases
        self._readers: dict[str, maxminddb.Reader] = {}
        self._files: dict[str, Path] = {}
        self._cancel = threading.Event()
        self._busy = False
        self._progress: tuple[int, int] = (0, 0)  # bytes downloaded, bytes in total
        self._message = ""                         # result of the last download, or why it failed
        self._message_ok = True
        self.listeners: list[Callable[[], None]] = []  # status changed (called on the download thread)
        for part in folder.glob("*.part"):  # left over from a download interrupted by exiting
            part.unlink(missing_ok=True)
        for kind, (name, _) in _KINDS.items():
            files = sorted(folder.glob(f"{name}-????-??.mmdb"))
            if files:
                try:
                    self._open(kind, files[-1])
                except (OSError, maxminddb.InvalidDatabaseError):
                    log.warning("Ignoring broken GeoIP file %s", files[-1], exc_info=True)

    @property
    def ready(self) -> bool:
        return "city" in self._readers

    def _open(self, kind: str, path: Path) -> None:
        reader = maxminddb.open_database(str(path))  # memory-mapped: the file isn't loaded into RAM
        with self._lock:
            old = self._readers.get(kind)
            self._readers[kind], self._files[kind] = reader, path
        if old:
            old.close()

    # ---- lookups (any thread)

    def lookup(self, ip: str) -> dict:
        """Country, city, network... of an internet address ({} if unknown or no database)."""
        with self._lock:
            try:
                city = self._readers["city"].get(ip) if "city" in self._readers else None
                owner = self._readers["asn"].get(ip) if "asn" in self._readers else None
            except ValueError:  # not an IP address
                return {}
        found = {}
        if city:
            country = city.get("country") or city.get("continent") or {}
            found["cc"] = country.get("iso_code") or country.get("code") or ""
            found["country"] = (country.get("names") or {}).get("en", "")
            name = ((city.get("city") or {}).get("names") or {}).get("en", "")
            found["city"] = _DISTRICT.sub("", name)  # "Tehran (District 6)" -> "Tehran"
            regions = city.get("subdivisions") or []
            found["region"] = (regions[0].get("names") or {}).get("en", "") if regions else ""
            location = city.get("location") or {}
            if "latitude" in location:
                found["lat"], found["lon"] = location["latitude"], location["longitude"]
        if owner:
            found["asn"] = owner.get("autonomous_system_number")
            found["org"] = owner.get("autonomous_system_organization", "")
        return found

    # ---- status for the window

    def status(self) -> dict:
        edition = _edition(self._files["city"]) if "city" in self._files else ""
        newest = _month(date.today())
        size = sum(p.stat().st_size for p in self._files.values() if p.exists())
        done, total = self._progress
        return {
            "state": "downloading" if self._busy else ("ready" if self.ready else "missing"),
            "edition": edition,
            "newer": newest if edition and edition < newest else "",
            "size": size,
            "progress": done / total if total else 0,
            "downloaded": done,
            "total": total,
            "message": self._message,
            "ok": self._message_ok,
        }

    def _changed(self) -> None:
        for listener in self.listeners:
            try:
                listener()
            except Exception:
                log.exception("GeoIP listener failed")

    # ---- download, update, remove

    def download(self) -> None:
        """Fetch the newest edition in the background (status() shows the progress)."""
        with self._lock:
            if self._busy:
                return
            self._busy = True
        self._cancel.clear()
        self._progress = (0, 0)
        self._message = ""
        threading.Thread(target=self._download, name="geoip-download", daemon=True).start()
        self._changed()

    def cancel(self) -> None:
        self._cancel.set()

    def remove(self) -> None:
        """Close and delete the databases."""
        if self._busy:
            return
        with self._lock:
            readers, self._readers = self._readers, {}
            files, self._files = self._files, {}
        for reader in readers.values():
            reader.close()
        for path in files.values():
            path.unlink(missing_ok=True)
        self._message, self._message_ok = "Database removed.", True
        self._changed()

    def _download(self) -> None:
        try:
            edition, sizes = self._find_edition()
            needed = [kind for kind in _KINDS if not (kind in self._files and _edition(self._files[kind]) == edition)]
            if not needed:
                self._message, self._message_ok = f"Already up to date - the {edition} edition is the newest.", True
                return
            self._progress = (0, sum(sizes[kind] for kind in needed))
            done = 0
            fresh = {}
            for kind in needed:
                name, db_type = _KINDS[kind]
                fresh[kind] = self._fetch(_URL.format(name=name, edition=edition),
                                          self._dir / f"{name}-{edition}.mmdb", db_type, done)
                done += sizes[kind]
            for kind, path in fresh.items():
                old = self._files.get(kind)
                self._open(kind, path)
                if old and old != path:
                    old.unlink(missing_ok=True)
            self._message, self._message_ok = f"Downloaded the {edition} edition.", True
            log.info("GeoIP: downloaded the %s edition", edition)
        except DownloadCancelled:
            self._message, self._message_ok = "Download cancelled.", True
        except Exception as e:
            self._message, self._message_ok = self._explain(e), False
            log.warning("GeoIP download failed: %r", e)
        finally:
            for part in self._dir.glob("*.part"):
                part.unlink(missing_ok=True)
            self._busy = False
            self._changed()

    @staticmethod
    def _request(url: str, method: str = "GET"):
        return net.urlopen(urllib.request.Request(url, method=method, headers={"User-Agent": f"NetMon/{__version__}"}))

    def _find_edition(self) -> tuple[str, dict[str, int]]:
        """The newest published edition ("2026-10") and the download size of each file.
        A new month's edition appears during its first days; until then, last month's is the newest."""
        today = date.today()
        for back in (0, 1):
            edition = _month(today, back)
            try:
                sizes = {}
                for kind, (name, _) in _KINDS.items():
                    with self._request(_URL.format(name=name, edition=edition), "HEAD") as response:
                        sizes[kind] = int(response.headers.get("Content-Length") or 0)
                return edition, sizes
            except urllib.error.HTTPError as e:
                if e.code != 404 or back:
                    raise
        raise OSError("no edition found")

    def _fetch(self, url: str, target: Path, db_type: str, done_before: int) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        last_report = 0.0
        with self._request(url) as response, open(part, "wb") as out:
            counter = _CountingReader(response)
            with gzip.GzipFile(fileobj=counter) as unpacked:
                while chunk := unpacked.read(_CHUNK):
                    if self._cancel.is_set():
                        raise DownloadCancelled
                    out.write(chunk)
                    now = time.monotonic()
                    if now - last_report > 0.3:
                        self._progress = (done_before + counter.count, self._progress[1])
                        self._changed()
                        last_report = now
        # gzip has already checked the file is complete (length and checksum); is it the right kind?
        with maxminddb.open_database(str(part), maxminddb.MODE_FILE) as check:
            if db_type not in check.metadata().database_type:
                raise ValueError(f"unexpected database type {check.metadata().database_type!r}")
        os.replace(part, target)
        return target

    @staticmethod
    def _explain(e: Exception) -> str:
        if isinstance(e, urllib.error.HTTPError):
            return f"DB-IP's server answered with an error (HTTP {e.code}). Try again later."
        if isinstance(e, urllib.error.URLError):
            return f"Can't reach download.db-ip.com ({e.reason})."
        if isinstance(e, (TimeoutError, ConnectionError)):
            return "The connection to download.db-ip.com broke off. Try again."
        if isinstance(e, (EOFError, gzip.BadGzipFile, maxminddb.InvalidDatabaseError, ValueError)):
            return "The downloaded file was damaged. Try again."
        return f"Download failed: {e}"


class _CountingReader:
    """Passes a download through and counts its (compressed) bytes for the progress bar."""

    def __init__(self, raw):
        self._raw = raw
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.count += len(data)
        return data

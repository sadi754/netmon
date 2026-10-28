"""RouterOS 7 scripts: the feed that sends a MikroTik's internet byte counters to the NetMon relay,
and the setup that lets NetMon read them directly."""
from __future__ import annotations

import ipaddress
import re
import socket

from .paths import DIRECT_TEMPLATE, RELAY_TEMPLATE

TEMPLATE = RELAY_TEMPLATE
_NAME = re.compile(r"^[\w.\-]{1,64}$")         # interface and address-list names
_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_URL = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?(/[\w.\-/]*)?$")
_ALLOW = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")
_PASSWORD = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")


def render(url: str, token: str, wan: str, address_list: str = "") -> str:
    """The filled-in script. Raises ValueError for values that would break it."""
    url = url.strip().rstrip("/")
    if not _URL.match(url):
        raise ValueError("The relay address should look like https://netmon.example.com")
    if not _TOKEN.match(token.strip()):
        raise ValueError("The router token is the one the relay's setup page (setup.php) showed for the router")
    if not _NAME.match(wan.strip()):
        raise ValueError("Internet interface: a RouterOS interface name, e.g. pppoe-out1")
    if address_list and not _NAME.match(address_list.strip()):
        raise ValueError("Address list: a RouterOS address-list name, e.g. IP-IR")
    wan, address_list = wan.strip(), address_list.strip()
    mangle, list_comment = _mangle(wan, address_list)
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in {"{{WAN}}": wan, "{{URL}}": url, "{{TOKEN}}": token.strip(),
                       "{{MANGLE}}": mangle, "{{LIST_COMMENT}}": list_comment}.items():
        text = text.replace(key, value)
    return text


def _mangle(wan: str, address_list: str) -> tuple[str, str]:
    """The four counting rules (if there's a domestic list) and a comment line describing them."""
    if address_list:
        # Counting rules only count (passthrough). The domestic share is domestic / all
        # from these rules, applied to the interface's exact byte counters - so anything
        # that makes mangle see a packet differently from the wire (a queue dropping it
        # after postrouting...) cancels out. ipsec-policy=...,none counts an IPsec
        # packet once, as it is on the wire, instead of before and after encryption.
        rules = (
            ("domestic-down", f'chain=prerouting in-interface="{wan}" src-address-list="{address_list}" ipsec-policy=in,none'),
            ("domestic-up", f'chain=postrouting out-interface="{wan}" dst-address-list="{address_list}" ipsec-policy=out,none'),
            ("all-down", f'chain=prerouting in-interface="{wan}" ipsec-policy=in,none'),
            ("all-up", f'chain=postrouting out-interface="{wan}" ipsec-policy=out,none'),
        )
        mangle = (
            "# Four counting rules at the top of mangle (passthrough: they only count, nothing is\n"
            f"# changed). Domestic = traffic to/from \"{address_list}\", measured as a share of all traffic.\n"
            "/ip firewall mangle remove [find comment~\"^netmon:\"]\n"
            + "".join(
                f':do {{\n'
                f'  :local rules [/ip firewall mangle find dynamic=no]\n'
                f'  :if ([:len $rules] > 0) do={{\n'
                f'    /ip firewall mangle add place-before=[:pick $rules 0] {rule} action=passthrough comment="netmon:{name}"\n'
                f'  }} else={{\n'
                f'    /ip firewall mangle add {rule} action=passthrough comment="netmon:{name}"\n'
                f'  }}\n'
                f'}}\n'
                for name, rule in reversed(rules)  # each goes to the top, so add them last-to-first
            )
        )
        list_comment = f"#   domestic list:      {address_list} (measured with four mangle counting rules)"
    else:
        mangle, list_comment = "", "#   domestic share:     not measured"
    return mangle, list_comment


def render_direct(wan: str, address_list: str, password: str, allow: str) -> str:
    """The setup script for direct reading: a read-only REST user (+ the counting rules).
    allow: the addresses that may use it, e.g. "192.168.88.0/24". Raises ValueError for bad values."""
    if not _NAME.match(wan.strip()):
        raise ValueError("Internet interface: a RouterOS interface name, e.g. pppoe-out1")
    if address_list and not _NAME.match(address_list.strip()):
        raise ValueError("Address list: a RouterOS address-list name, e.g. IP-IR")
    if not _ALLOW.match(allow) or not _PASSWORD.match(password):
        raise ValueError("Couldn't make the script (address or password).")
    wan, address_list = wan.strip(), address_list.strip()
    mangle, list_comment = _mangle(wan, address_list)
    if not address_list:  # rules from an earlier NetMon script still count
        list_comment = "#   domestic share:     from the netmon: counting rules, if the router has them"
    text = DIRECT_TEMPLATE.read_text(encoding="utf-8")
    for key, value in {"{{WAN}}": wan, "{{ALLOW}}": allow, "{{PASSWORD}}": password,
                       "{{MANGLE}}": mangle, "{{LIST_COMMENT}}": list_comment}.items():
        text = text.replace(key, value)
    return text


def local_network(router_host: str) -> str:
    """The local network this PC reaches the router from, as RouterOS writes it: "192.168.88.0/24"."""
    host = re.sub(r"^https?://", "", router_host.strip()).split("/")[0].split(":")[0]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 80))  # picks the route; nothing is sent
        ip = ipaddress.ip_address(s.getsockname()[0])
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))

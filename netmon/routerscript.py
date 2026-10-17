"""The RouterOS 7 script that feeds a MikroTik's internet byte counters to the NetMon relay."""
from __future__ import annotations

import re

from .paths import RELAY_TEMPLATE

TEMPLATE = RELAY_TEMPLATE
_NAME = re.compile(r"^[\w.\-]{1,64}$")         # interface and address-list names
_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_URL = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?(/[\w.\-/]*)?$")


def render(url: str, token: str, wan: str, address_list: str = "") -> str:
    """The filled-in script. Raises ValueError for values that would break it."""
    url = url.strip().rstrip("/")
    if not _URL.match(url):
        raise ValueError("The relay address should look like https://netmon.example.com")
    if not _TOKEN.match(token.strip()):
        raise ValueError("The router token is the INGEST_TOKEN made by relay/deploy.py --init")
    if not _NAME.match(wan.strip()):
        raise ValueError("Internet interface: a RouterOS interface name, e.g. pppoe-out1")
    if address_list and not _NAME.match(address_list.strip()):
        raise ValueError("Address list: a RouterOS address-list name, e.g. IP-IR")
    wan, address_list = wan.strip(), address_list.strip()
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
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in {"{{WAN}}": wan, "{{URL}}": url, "{{TOKEN}}": token.strip(),
                       "{{MANGLE}}": mangle, "{{LIST_COMMENT}}": list_comment}.items():
        text = text.replace(key, value)
    return text

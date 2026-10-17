"""Is a VPN or proxy in use on this PC? A generic check of three signs:

- a VPN network adapter that is connected (WireGuard, OpenVPN, Windows' own VPN...)
- a known VPN / proxy program that is running (v2rayN, Xray, sing-box, Clash...)
- the Windows system proxy (what v2rayN-style apps switch on)

A VPN that runs on the router instead can't be seen from the PC - and doesn't
need to be: the PC then sees the real addresses it talks to.
"""
from __future__ import annotations

import winreg

from .adapters import IF_TYPE_PPP, Adapter

# Words in a connected virtual adapter's name or driver that mean "VPN tunnel".
_VPN_ADAPTER_WORDS = (
    "vpn", "wireguard", "wiresock", "wintun", "tunnel", "tun", "tap-windows", "tap-", "openvpn",
    "anyconnect", "fortinet", "forti", "pangp", "juniper", "pulse secure", "softether", "zerotier",
    "tailscale", "hamachi", "nordlynx", "warp", "outline", "psiphon", "sstp", "l2tp", "ikev2", "pptp",
)
# Virtual adapters that are not VPNs (virtual machines, containers...).
_NOT_VPN = ("hyper-v", "virtualbox", "vmware", "docker", "wsl", "bluetooth", "loopback", "wi-fi direct", "npcap")

# exe name (lower case) -> what it is
VPN_PROGRAMS = {
    "v2rayn.exe": "v2rayN", "xray.exe": "Xray", "v2ray.exe": "V2Ray", "sing-box.exe": "sing-box",
    "clash.exe": "Clash", "clash-verge.exe": "Clash Verge", "verge-mihomo.exe": "Clash Verge", "mihomo.exe": "mihomo",
    "clash-meta.exe": "Clash Meta", "nekoray.exe": "NekoRay", "nekobox.exe": "NekoBox", "hiddify.exe": "Hiddify",
    "hiddifycli.exe": "Hiddify", "openvpn.exe": "OpenVPN", "wireguard.exe": "WireGuard",
    "wiresock-client.exe": "WireSock", "wiresock-vpn-client.exe": "WireSock", "outline.exe": "Outline",
    "psiphon3.exe": "Psiphon", "psiphon-tunnel-core.exe": "Psiphon", "lantern.exe": "Lantern", "tor.exe": "Tor",
    "shadowsocks.exe": "Shadowsocks", "sslocal.exe": "Shadowsocks", "trojan.exe": "Trojan",
    "hysteria.exe": "Hysteria", "tuic-client.exe": "TUIC", "warp-svc.exe": "Cloudflare WARP",
    "nordvpn.exe": "NordVPN", "expressvpn.exe": "ExpressVPN", "protonvpn.exe": "Proton VPN",
    "windscribe.exe": "Windscribe", "mullvad-daemon.exe": "Mullvad VPN", "forticlient.exe": "FortiClient",
    "vpnui.exe": "Cisco AnyConnect", "tailscaled.exe": "Tailscale", "zerotier-one_x64.exe": "ZeroTier",
    "vpnclient_x64.exe": "SoftEther VPN", "netch.exe": "Netch",
}

_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def is_vpn_adapter(adapter: Adapter) -> bool:
    if adapter.physical or not adapter.up:
        return False
    if adapter.if_type == IF_TYPE_PPP:  # Windows' built-in VPN connections (IKEv2, SSTP, L2TP, PPTP)
        return True
    text = f"{adapter.name} {adapter.description}".lower()
    return not any(w in text for w in _NOT_VPN) and any(w in text for w in _VPN_ADAPTER_WORDS)


def system_proxy() -> dict:
    """Windows' proxy setting (Settings -> Network -> Proxy) for this user."""
    found = {"enabled": False, "server": "", "script": ""}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
            for name, field in (("ProxyEnable", "enabled"), ("ProxyServer", "server"), ("AutoConfigURL", "script")):
                try:
                    found[field] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    pass
    except OSError:
        pass
    found["enabled"] = bool(found["enabled"]) and bool(found["server"])
    return found


def detect(adapters: list[Adapter], processes: dict[int, str], active_pids: set[int]) -> dict:
    """What this PC shows of VPNs and proxies right now.
    processes: pid -> exe name; active_pids: processes with an open connection."""
    programs: dict[str, bool] = {}  # name -> has connections
    for pid, exe in processes.items():
        name = VPN_PROGRAMS.get(exe.lower())
        if name:
            programs[name] = programs.get(name, False) or pid in active_pids
    return {
        "adapters": [{"name": a.name, "description": a.description} for a in adapters if is_vpn_adapter(a)],
        "programs": [{"name": n, "active": active} for n, active in sorted(programs.items())],
        "proxy": system_proxy(),
    }

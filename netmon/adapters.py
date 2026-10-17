"""Network adapters and their byte counters, read straight from Windows.

We call GetIfTable2 (iphlpapi.dll) - the same 64-bit counters that psutil and
Task Manager use - because it also tells us whether an adapter is a real
network card or a virtual one (VPN tunnel, Hyper-V switch, ...). That matters
for the "All adapters" total: with a VPN on the PC, every byte crosses both the
virtual adapter and the real card, so summing everything would count it twice.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

IF_MAX_STRING_SIZE = 256
IF_MAX_PHYS_ADDRESS_LENGTH = 32

IF_TYPE_PPP = 23  # also Windows' built-in VPN connections
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131  # Teredo, 6to4, IP-HTTPS: Windows' own IPv6 helpers
IF_OPER_STATUS_UP = 1

# Bits of MIB_IF_ROW2.InterfaceAndOperStatusFlags
_FLAG_HARDWARE = 0x01
_FLAG_FILTER = 0x02
_FLAG_CONNECTOR_PRESENT = 0x04

# Descriptions of adapters Windows creates for itself; never useful to show.
_HIDDEN_DESCRIPTIONS = (
    "npcap loopback",
    "wan miniport",
    "microsoft kernel debug",
    "teredo",
    "6to4",
    "ip-https",
)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self) -> str:
        d4 = bytes(self.Data4)
        return (f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-"
                f"{d4[:2].hex().upper()}-{d4[2:].hex().upper()}}}")


class MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", wintypes.ULONG),
        ("InterfaceGuid", GUID),
        ("Alias", wintypes.WCHAR * (IF_MAX_STRING_SIZE + 1)),
        ("Description", wintypes.WCHAR * (IF_MAX_STRING_SIZE + 1)),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("Mtu", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_int),
        ("MediaType", ctypes.c_int),
        ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int),
        ("DirectionType", ctypes.c_int),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_int),
        ("AdminStatus", ctypes.c_int),
        ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", GUID),
        ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64),
        ("InUcastPkts", ctypes.c_uint64),
        ("InNUcastPkts", ctypes.c_uint64),
        ("InDiscards", ctypes.c_uint64),
        ("InErrors", ctypes.c_uint64),
        ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64),
        ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64),
        ("OutOctets", ctypes.c_uint64),
        ("OutUcastPkts", ctypes.c_uint64),
        ("OutNUcastPkts", ctypes.c_uint64),
        ("OutDiscards", ctypes.c_uint64),
        ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64),
        ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64),
        ("OutQLen", ctypes.c_uint64),
    ]


assert ctypes.sizeof(MIB_IF_ROW2) == 1352, "MIB_IF_ROW2 layout mismatch"

# MIB_IF_TABLE2 is { ULONG NumEntries; MIB_IF_ROW2 Table[]; } and the rows
# start at the next 8-byte boundary.
_TABLE_ROWS_OFFSET = ctypes.alignment(MIB_IF_ROW2)

_iphlpapi = ctypes.WinDLL("iphlpapi")
_GetIfTable2 = _iphlpapi.GetIfTable2
_GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_GetIfTable2.restype = wintypes.DWORD
_FreeMibTable = _iphlpapi.FreeMibTable
_FreeMibTable.argtypes = [ctypes.c_void_p]
_FreeMibTable.restype = None


@dataclass(frozen=True)
class Adapter:
    """One adapter at one moment in time."""
    guid: str          # stable ID, survives reboots and renames
    name: str          # name shown in Windows, e.g. "Ethernet", "Wi-Fi"
    description: str   # driver name, e.g. "Intel(R) Ethernet Controller I226-V"
    physical: bool     # real network card (False = VPN / virtual)
    up: bool
    link_speed: int    # bits per second, 0 if unknown
    rx_bytes: int      # total received since the adapter came up
    tx_bytes: int      # total sent since the adapter came up
    if_type: int = 0   # IANA interface type (6 = Ethernet, 71 = Wi-Fi, 23 = PPP...)
    mac: bytes = b""   # hardware address; tells sent from received frames in a packet capture


def _is_user_facing(row: MIB_IF_ROW2) -> bool:
    """Keep the adapters you'd see in Control Panel; drop Windows internals."""
    flags = row.InterfaceAndOperStatusFlags
    if flags & _FLAG_FILTER:  # driver layers such as "...-WFP Native MAC Layer"
        return False
    if row.Type in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
        return False
    if row.Description.lower().startswith(_HIDDEN_DESCRIPTIONS):
        return False
    # Real cards are always listed (even unplugged); virtual ones only while up.
    return bool(flags & _FLAG_HARDWARE) or row.OperStatus == IF_OPER_STATUS_UP


def list_adapters() -> list[Adapter]:
    """Return a fresh snapshot of every user-facing adapter."""
    table = ctypes.c_void_p()
    err = _GetIfTable2(ctypes.byref(table))
    if err:
        raise ctypes.WinError(err)
    try:
        count = wintypes.ULONG.from_address(table.value).value
        rows = (MIB_IF_ROW2 * count).from_address(table.value + _TABLE_ROWS_OFFSET)
        adapters = []
        seen = set()
        for row in rows:
            guid = str(row.InterfaceGuid)
            if guid in seen or not _is_user_facing(row):
                continue
            seen.add(guid)
            adapters.append(Adapter(
                guid=guid,
                name=row.Alias,
                description=row.Description,
                physical=bool(row.InterfaceAndOperStatusFlags & _FLAG_HARDWARE),
                up=row.OperStatus == IF_OPER_STATUS_UP,
                link_speed=row.ReceiveLinkSpeed if row.ReceiveLinkSpeed < 2**63 else 0,
                rx_bytes=row.InOctets,
                tx_bytes=row.OutOctets,
                if_type=row.Type,
                mac=bytes(row.PhysicalAddress[:row.PhysicalAddressLength]),
            ))
        return adapters
    finally:
        _FreeMibTable(table)

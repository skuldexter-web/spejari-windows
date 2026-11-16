"""Windows Native Wifi (wlanapi.dll) access through ctypes.

Everything that touches the DLL lives in :class:`WlanClient`. The structure parsing
helpers (:func:`parse_interface_list`, :func:`parse_bss_list`) work on plain memory
addresses and use only fixed-size ctypes types, so they behave identically on any
platform and are unit-tested without Windows.

Windows does not let applications choose which channels an adapter scans: a call to
``WlanScan`` makes the driver sweep every supported channel. Spejari therefore
distributes work over several adapters in *time* (staggered scans) instead of
splitting channels between them.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass
from typing import List, Optional

from .channels import freq_to_band, freq_to_channel, in_scope
from .models import AccessPoint
from .security import derive_security, parse_ies

# --- Win32 error codes we care about -------------------------------------------------
ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_NOT_SUPPORTED = 50
ERROR_BUSY = 170
ERROR_NOT_FOUND = 1168
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_NO_MORE_ITEMS = 259

DOT11_BSS_TYPE_ANY = 3
WLAN_CLIENT_VERSION = 2

INTERFACE_STATES = {
    0: "not ready",
    1: "connected",
    2: "ad-hoc",
    3: "disconnecting",
    4: "disconnected",
    5: "associating",
    6: "discovering",
    7: "authenticating",
}


class WlanError(Exception):
    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code  # Win32 error code (0 = unknown)


def explain_error(code: int) -> str:
    return {
        ERROR_ACCESS_DENIED: "access denied - Windows blocks Wi-Fi scans without location permission "
        "(Settings > Privacy & security > Location: turn on Location services and 'Let desktop apps "
        "access your location')",
        ERROR_SERVICE_NOT_ACTIVE: "the 'WLAN AutoConfig' service is not running",
        ERROR_NOT_SUPPORTED: "the adapter or its driver does not support this operation",
        ERROR_NOT_FOUND: "adapter not found (unplugged?)",
        ERROR_INVALID_HANDLE: "WLAN handle became invalid",
        ERROR_BUSY: "adapter is busy with another scan",
    }.get(code, f"Windows error {code}")


# --- fixed-size ctypes structures (match the Windows SDK layout) --------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", ctypes.c_uint16 * 256),  # WCHAR[256]
        ("isState", ctypes.c_uint32),
    ]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", ctypes.c_uint32), ("ucSSID", ctypes.c_ubyte * 32)]


class WLAN_RATE_SET(ctypes.Structure):
    _fields_ = [("uRateSetLength", ctypes.c_uint32), ("usRateSet", ctypes.c_uint16 * 126)]


class WLAN_BSS_ENTRY(ctypes.Structure):
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("uPhyId", ctypes.c_uint32),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11BssType", ctypes.c_uint32),
        ("dot11BssPhyType", ctypes.c_uint32),
        ("lRssi", ctypes.c_int32),
        ("uLinkQuality", ctypes.c_uint32),
        ("bInRegDomain", ctypes.c_ubyte),
        ("usBeaconPeriod", ctypes.c_uint16),
        ("ullTimestamp", ctypes.c_uint64),
        ("ullHostTimestamp", ctypes.c_uint64),
        ("usCapabilityInformation", ctypes.c_uint16),
        ("ulChCenterFrequency", ctypes.c_uint32),  # kHz
        ("wlanRateSet", WLAN_RATE_SET),
        ("ulIeOffset", ctypes.c_uint32),
        ("ulIeSize", ctypes.c_uint32),
    ]


LIST_HEADER_SIZE = 8  # DWORD dwTotalSize/dwNumberOfItems (BSS list) or dwNumberOfItems/dwIndex (iface list)


# --- plain data returned to the rest of Spejari --------------------------------------
@dataclass(frozen=True)
class WlanIface:
    guid: str  # "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
    guid_bytes: bytes  # 16 bytes, as laid out in a GUID structure
    description: str
    state: int
    tag: str = ""  # short unique id, assigned by the adapter manager

    @property
    def state_text(self) -> str:
        return INTERFACE_STATES.get(self.state, f"state {self.state}")


@dataclass(frozen=True)
class BssEntry:
    bssid: str
    ssid: str
    rssi: int
    freq_mhz: int
    tsf: int  # AP timer value from the last beacon / probe response
    capability: int
    ies: bytes


def guid_to_str(raw: bytes) -> str:
    g = GUID.from_buffer_copy(raw)
    tail = bytes(g.Data4)
    return "{%08X-%04X-%04X-%s-%s}" % (g.Data1, g.Data2, g.Data3, tail[:2].hex().upper(), tail[2:].hex().upper())


def parse_interface_list(address: int) -> List[WlanIface]:
    """Parse a WLAN_INTERFACE_INFO_LIST located at *address*."""
    count = ctypes.c_uint32.from_address(address).value
    stride = ctypes.sizeof(WLAN_INTERFACE_INFO)
    out: List[WlanIface] = []
    for i in range(count):
        info = WLAN_INTERFACE_INFO.from_address(address + LIST_HEADER_SIZE + i * stride)
        raw_guid = bytes(ctypes.string_at(ctypes.addressof(info.InterfaceGuid), 16))
        chars = bytes(ctypes.string_at(ctypes.addressof(info.strInterfaceDescription), 512))
        desc = chars.decode("utf-16-le", errors="replace").split("\x00", 1)[0].strip()
        out.append(WlanIface(guid_to_str(raw_guid), raw_guid, desc or "Wi-Fi adapter", int(info.isState)))
    return out


def parse_bss_list(address: int) -> List[BssEntry]:
    """Parse a WLAN_BSS_LIST located at *address*."""
    count = ctypes.c_uint32.from_address(address + 4).value
    stride = ctypes.sizeof(WLAN_BSS_ENTRY)
    entries: List[BssEntry] = []
    for i in range(count):
        base = address + LIST_HEADER_SIZE + i * stride
        e = WLAN_BSS_ENTRY.from_address(base)
        length = min(int(e.dot11Ssid.uSSIDLength), 32)
        ssid = bytes(e.dot11Ssid.ucSSID[:length]).decode("utf-8", errors="replace").replace("\x00", "")
        if ssid.strip() == "":
            ssid = ""  # hidden network
        ies = ctypes.string_at(base + int(e.ulIeOffset), int(e.ulIeSize)) if e.ulIeSize else b""
        bssid = ":".join(f"{b:02X}" for b in bytes(e.dot11Bssid))
        entries.append(
            BssEntry(
                bssid=bssid,
                ssid=ssid,
                rssi=int(e.lRssi),
                freq_mhz=int(e.ulChCenterFrequency) // 1000,
                tsf=int(e.ullTimestamp),
                capability=int(e.usCapabilityInformation),
                ies=bytes(ies),
            )
        )
    return entries


def bss_to_access_point(e: BssEntry) -> Optional[AccessPoint]:
    """Convert a BSS entry to an AccessPoint (None for 6 GHz / unknown bands)."""
    if not in_scope(e.freq_mhz):
        return None
    auth, label = derive_security(parse_ies(e.ies), e.capability)
    return AccessPoint(
        bssid=e.bssid,
        ssid=e.ssid,
        freq=e.freq_mhz,
        channel=freq_to_channel(e.freq_mhz),
        band=freq_to_band(e.freq_mhz) or "",
        rssi=e.rssi,
        auth=auth,
        security=label,
    )


# --- the real backend ---------------------------------------------------------------
class WlanClient:
    """Thin, thread-safe wrapper around wlanapi.dll. Windows only."""

    name = "wlanapi"

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WlanError("The Windows WLAN API is only available on Windows (use --simulate to try the app).")
        from ctypes import wintypes

        self._lock = threading.RLock()
        self._dll = ctypes.WinDLL("wlanapi.dll")
        d = self._dll
        d.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                     ctypes.POINTER(ctypes.c_void_p)]
        d.WlanOpenHandle.restype = wintypes.DWORD
        d.WlanCloseHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        d.WlanCloseHandle.restype = wintypes.DWORD
        d.WlanEnumInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        d.WlanEnumInterfaces.restype = wintypes.DWORD
        d.WlanFreeMemory.argtypes = [ctypes.c_void_p]
        d.WlanFreeMemory.restype = None
        d.WlanScan.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p]
        d.WlanScan.restype = wintypes.DWORD
        d.WlanGetNetworkBssList.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.c_void_p,
                                            wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
        d.WlanGetNetworkBssList.restype = wintypes.DWORD
        self._handle = ctypes.c_void_p()
        self._open()

    # -- handle management ------------------------------------------------------------
    def _open(self) -> None:
        from ctypes import wintypes

        handle = ctypes.c_void_p()
        negotiated = wintypes.DWORD()
        rc = self._dll.WlanOpenHandle(WLAN_CLIENT_VERSION, None, ctypes.byref(negotiated), ctypes.byref(handle))
        if rc != ERROR_SUCCESS:
            raise WlanError(f"WlanOpenHandle failed: {explain_error(rc)}", int(rc))
        self._handle = handle

    def _reopen(self) -> None:
        try:
            self._dll.WlanCloseHandle(self._handle, None)
        except OSError:
            pass
        self._open()

    def close(self) -> None:
        with self._lock:
            if self._handle:
                self._dll.WlanCloseHandle(self._handle, None)
                self._handle = ctypes.c_void_p()

    # -- backend API ------------------------------------------------------------------
    def interfaces(self) -> List[WlanIface]:
        with self._lock:
            for attempt in (0, 1):
                out = ctypes.c_void_p()
                rc = self._dll.WlanEnumInterfaces(self._handle, None, ctypes.byref(out))
                if rc == ERROR_INVALID_HANDLE and attempt == 0:
                    self._reopen()
                    continue
                if rc != ERROR_SUCCESS:
                    raise WlanError(f"WlanEnumInterfaces failed: {explain_error(rc)}", int(rc))
                try:
                    return parse_interface_list(out.value)
                finally:
                    self._dll.WlanFreeMemory(out)
        return []

    def scan(self, iface: WlanIface) -> int:
        """Ask the driver for a fresh scan. Returns a Win32 error code (0 = started)."""
        guid = GUID.from_buffer_copy(iface.guid_bytes)
        with self._lock:
            rc = self._dll.WlanScan(self._handle, ctypes.byref(guid), None, None, None)
            if rc == ERROR_INVALID_HANDLE:
                self._reopen()
                rc = self._dll.WlanScan(self._handle, ctypes.byref(guid), None, None, None)
            return int(rc)

    def bss_list(self, iface: WlanIface) -> List[BssEntry]:
        guid = GUID.from_buffer_copy(iface.guid_bytes)
        with self._lock:
            out = ctypes.c_void_p()
            rc = self._dll.WlanGetNetworkBssList(self._handle, ctypes.byref(guid), None, DOT11_BSS_TYPE_ANY,
                                                 False, None, ctypes.byref(out))
            if rc == ERROR_INVALID_HANDLE:
                self._reopen()
                rc = self._dll.WlanGetNetworkBssList(self._handle, ctypes.byref(guid), None, DOT11_BSS_TYPE_ANY,
                                                     False, None, ctypes.byref(out))
            if rc != ERROR_SUCCESS:
                raise WlanError(explain_error(rc), int(rc))
            try:
                return parse_bss_list(out.value)
            finally:
                self._dll.WlanFreeMemory(out)

"""The ctypes layouts must match the Windows SDK; the parsers are tested on synthetic memory."""

import ctypes

from spejari import wlan
from spejari.wlan import (
    DOT11_SSID,
    GUID,
    WLAN_BSS_ENTRY,
    WLAN_INTERFACE_INFO,
    BssEntry,
    bss_to_access_point,
    guid_to_str,
    parse_bss_list,
    parse_interface_list,
)
from spejari.sim import ie_rsn


def test_struct_sizes_match_windows_sdk():
    assert ctypes.sizeof(GUID) == 16
    assert ctypes.sizeof(DOT11_SSID) == 36
    assert ctypes.sizeof(WLAN_INTERFACE_INFO) == 532
    assert ctypes.sizeof(WLAN_BSS_ENTRY) == 360
    assert WLAN_BSS_ENTRY.ulChCenterFrequency.offset == 92
    assert WLAN_BSS_ENTRY.ullTimestamp.offset == 72
    assert WLAN_BSS_ENTRY.usCapabilityInformation.offset == 88
    assert WLAN_BSS_ENTRY.ulIeOffset.offset == 352


def test_guid_string():
    raw = bytes.fromhex("78563412" "3412" "7856" "9a bc de f0 12 34 56 78".replace(" ", ""))
    assert guid_to_str(raw) == "{12345678-1234-5678-9ABC-DEF012345678}"


def make_bss_buffer(items):
    stride = ctypes.sizeof(WLAN_BSS_ENTRY)
    total = 8 + stride * len(items) + sum(len(i["ies"]) for i in items)
    buf = ctypes.create_string_buffer(total)
    base = ctypes.addressof(buf)
    ctypes.c_uint32.from_address(base).value = total
    ctypes.c_uint32.from_address(base + 4).value = len(items)
    ie_pos = 8 + stride * len(items)
    for n, it in enumerate(items):
        off = 8 + n * stride
        e = WLAN_BSS_ENTRY.from_address(base + off)
        ssid = it["ssid"].encode()
        e.dot11Ssid.uSSIDLength = len(ssid)
        for i, b in enumerate(ssid):
            e.dot11Ssid.ucSSID[i] = b
        for i, b in enumerate(bytes.fromhex(it["bssid"].replace(":", ""))):
            e.dot11Bssid[i] = b
        e.lRssi = it["rssi"]
        e.ulChCenterFrequency = it["freq"] * 1000
        e.ullTimestamp = it["tsf"]
        e.usCapabilityInformation = it["cap"]
        e.ulIeOffset = ie_pos - off
        e.ulIeSize = len(it["ies"])
        ctypes.memmove(base + ie_pos, it["ies"], len(it["ies"]))
        ie_pos += len(it["ies"])
    return buf


def test_parse_bss_list():
    buf = make_bss_buffer(
        [
            {"ssid": "HomeNet", "bssid": "aa:bb:cc:00:11:22", "rssi": -52, "freq": 2437, "tsf": 2**40 + 7,
             "cap": 0x0411, "ies": ie_rsn([2])},
            {"ssid": "", "bssid": "01:02:03:04:05:06", "rssi": -80, "freq": 5180, "tsf": 5, "cap": 0x0401,
             "ies": b""},
            {"ssid": "SixGig", "bssid": "11:22:33:44:55:66", "rssi": -60, "freq": 5955, "tsf": 1, "cap": 0x0411,
             "ies": b""},
        ]
    )
    entries = parse_bss_list(ctypes.addressof(buf))
    assert [e.bssid for e in entries] == ["AA:BB:CC:00:11:22", "01:02:03:04:05:06", "11:22:33:44:55:66"]
    assert entries[0].ssid == "HomeNet" and entries[0].rssi == -52 and entries[0].freq_mhz == 2437
    assert entries[0].tsf == 2**40 + 7
    assert entries[1].ssid == ""
    ap = bss_to_access_point(entries[0])
    assert (ap.channel, ap.band, ap.auth, ap.security) == (6, "2.4", "[WPA2-PSK-CCMP][ESS]", "WPA2")
    ap5 = bss_to_access_point(entries[1])
    assert (ap5.channel, ap5.band, ap5.security, ap5.hidden) == (36, "5", "Open", True)
    assert bss_to_access_point(entries[2]) is None  # 6 GHz is out of scope


def test_parse_interface_list():
    stride = ctypes.sizeof(WLAN_INTERFACE_INFO)
    buf = ctypes.create_string_buffer(8 + 2 * stride)
    base = ctypes.addressof(buf)
    ctypes.c_uint32.from_address(base).value = 2
    for n, (desc, state) in enumerate([("Intel(R) Wi-Fi 6 AX201 160MHz", 1), ("ALFA AWUS036ACM \u00e9", 4)]):
        info = WLAN_INTERFACE_INFO.from_address(base + 8 + n * stride)
        for i, b in enumerate(bytes([n + 1] * 16)):
            info.InterfaceGuid.__class__  # touch
            ctypes.memset(ctypes.addressof(info) + i, b, 1)
        data = desc.encode("utf-16-le")
        ctypes.memmove(ctypes.addressof(info.strInterfaceDescription), data, len(data))
        info.isState = state
    ifaces = parse_interface_list(base)
    assert [i.description for i in ifaces] == ["Intel(R) Wi-Fi 6 AX201 160MHz", "ALFA AWUS036ACM \u00e9"]
    assert [i.state_text for i in ifaces] == ["connected", "disconnected"]
    assert ifaces[0].guid == "{01010101-0101-0101-0101-010101010101}"


def test_wlan_client_refuses_to_run_off_windows():
    import sys
    import pytest

    if sys.platform == "win32":
        pytest.skip("real Windows")
    with pytest.raises(wlan.WlanError):
        wlan.WlanClient()

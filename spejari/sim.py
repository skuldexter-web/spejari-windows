"""Simulated Wi-Fi backend and GPS track (``--simulate``): try Spejari without hardware.

The simulator produces real 802.11 information elements, so the genuine security
parser and the whole scan pipeline are exercised.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from typing import Dict, List, Sequence

from .wlan import BssEntry, WlanIface, guid_to_str

_CHANNELS_2G = [1, 6, 11, 1, 6, 11, 3, 8, 13]
_CHANNELS_5G = [36, 40, 44, 48, 52, 100, 149, 153, 157, 161, 165]


def ie_rsn(akm: Sequence[int], pairwise: Sequence[int] = (4,), group: int = 4) -> bytes:
    body = b"\x01\x00" + b"\x00\x0f\xac" + bytes([group])
    body += len(pairwise).to_bytes(2, "little") + b"".join(b"\x00\x0f\xac" + bytes([c]) for c in pairwise)
    body += len(akm).to_bytes(2, "little") + b"".join(b"\x00\x0f\xac" + bytes([a]) for a in akm)
    body += b"\x0c\x00"  # RSN capabilities
    return bytes([48, len(body)]) + body


def ie_wpa(akm: Sequence[int] = (2,), pairwise: Sequence[int] = (2, 4), group: int = 2) -> bytes:
    body = b"\x00\x50\xf2\x01" + b"\x01\x00" + b"\x00\x50\xf2" + bytes([group])
    body += len(pairwise).to_bytes(2, "little") + b"".join(b"\x00\x50\xf2" + bytes([c]) for c in pairwise)
    body += len(akm).to_bytes(2, "little") + b"".join(b"\x00\x50\xf2" + bytes([a]) for a in akm)
    return bytes([221, len(body)]) + body


def ie_ssid(ssid: str) -> bytes:
    raw = ssid.encode("utf-8")[:32]
    return bytes([0, len(raw)]) + raw


_SECURITY_MIX = [
    (0x0411, ie_rsn([2])),            # WPA2-PSK
    (0x0411, ie_rsn([2])),
    (0x0411, ie_rsn([2, 8])),         # WPA2/3 transition
    (0x0411, ie_rsn([8])),            # WPA3-SAE
    (0x0411, ie_rsn([1])),            # WPA2-Enterprise
    (0x0411, ie_wpa() + ie_rsn([2], (4,))),  # WPA + WPA2
    (0x0401, b""),                    # open
    (0x0411, b""),                    # WEP
    (0x0401, ie_rsn([18])),           # OWE
]
_NAMES = ["HomeNet", "CoffeeShop", "FreeWifi", "Hotel_Guest", "Office", "Linksys", "TP-Link", "Skald", "Odin",
          "Valhalla", "Bifrost", "Mjolnir", "Yggdrasil", "Fenrir", "NETGEAR", "FRITZ!Box", "iPhone", "AndroidAP"]


class SimBackend:
    """Same interface as WlanClient: interfaces(), scan(iface), bss_list(iface)."""

    name = "simulate"

    def __init__(self, adapters: int = 3, networks: int = 160, seed: int = 7, stale_fraction: float = 0.25) -> None:
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._stale_fraction = stale_fraction
        self._ifaces: List[WlanIface] = []
        self._aps: List[Dict] = []
        self._tsf: Dict[tuple, int] = {}
        self._t0 = time.monotonic()
        for i in range(max(0, adapters)):
            self._ifaces.append(self._make_iface(i))
        for n in range(networks):
            band_5 = self._rng.random() < 0.45
            chan = self._rng.choice(_CHANNELS_5G if band_5 else _CHANNELS_2G)
            freq = (5000 + 5 * chan) if band_5 else (2407 + 5 * chan)
            cap, ies = _SECURITY_MIX[n % len(_SECURITY_MIX)]
            hidden = n % 23 == 0
            ssid = "" if hidden else f"{self._rng.choice(_NAMES)}-{n:03d}"
            mac = ":".join(f"{self._rng.randrange(256):02X}" for _ in range(6))
            self._aps.append({"bssid": mac, "ssid": ssid, "freq": freq, "cap": cap,
                              "ies": ie_ssid(ssid) + ies, "base": self._rng.randint(-85, -38)})

    @staticmethod
    def _make_iface(i: int) -> WlanIface:
        raw = uuid.UUID(int=(0x5EA00000 + i) << 64 | 0xC0FFEE00 + i).bytes_le
        return WlanIface(guid_to_str(raw), raw, f"Simulated Wi-Fi Adapter {i + 1}", 4)

    # -- hot-plug helpers (tests / demos) -------------------------------------------
    def set_adapter_count(self, n: int) -> None:
        with self._lock:
            self._ifaces = [self._make_iface(i) for i in range(max(0, n))]

    # -- backend API ----------------------------------------------------------------
    def interfaces(self) -> List[WlanIface]:
        with self._lock:
            return list(self._ifaces)

    def scan(self, iface: WlanIface) -> int:
        return 0

    def bss_list(self, iface: WlanIface) -> List[BssEntry]:
        now_us = int((time.monotonic() - self._t0) * 1e6)
        out: List[BssEntry] = []
        for n, ap in enumerate(self._aps):
            # each adapter hears a different, slowly changing subset (antennas, position)
            window = int(time.monotonic() / 7)
            seen = random.Random(hash((iface.guid, n, window))).random()
            if seen > 0.42:
                continue
            key = (iface.guid, ap["bssid"])
            if key in self._tsf and self._rng.random() < self._stale_fraction:
                tsf = self._tsf[key]  # cached entry, no new beacon since the last read
            else:
                tsf = now_us + n
                self._tsf[key] = tsf
            rssi = ap["base"] + self._rng.randint(-6, 6)
            out.append(BssEntry(ap["bssid"], ap["ssid"], max(-95, min(-30, rssi)), ap["freq"], tsf,
                                ap["cap"], ap["ies"]))
        return out

    def close(self) -> None:
        pass

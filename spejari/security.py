"""Security detection from 802.11 information elements (RSN / WPA) and capability bits.

The strings produced match the Android / WiGLE capability notation, e.g.
``[WPA2-PSK-CCMP][ESS]``.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

CAP_ESS = 0x0001
CAP_IBSS = 0x0002
CAP_PRIVACY = 0x0010

_OUI_RSN = b"\x00\x0f\xac"
_OUI_WPA = b"\x00\x50\xf2"

_CIPHERS_RSN = {1: "WEP40", 2: "TKIP", 4: "CCMP", 5: "WEP104", 8: "GCMP", 9: "GCMP-256", 10: "CCMP-256"}
_CIPHERS_WPA = {1: "WEP40", 2: "TKIP", 4: "CCMP", 5: "WEP104"}
_AKM_RSN = {
    1: "EAP",
    2: "PSK",
    3: "FT/EAP",
    4: "FT/PSK",
    5: "EAP/SHA-256",
    6: "PSK/SHA-256",
    8: "SAE",
    9: "FT/SAE",
    11: "EAP/SUITE-B",
    12: "EAP/SUITE-B-192",
    18: "OWE",
}
_AKM_WPA = {1: "EAP", 2: "PSK"}


def _u16(buf: bytes, pos: int) -> Optional[int]:
    if pos + 2 > len(buf):
        return None
    return buf[pos] | (buf[pos + 1] << 8)


def _suite(raw: bytes, oui: bytes, table: Dict[int, str]) -> str:
    if raw[:3] != oui:
        return "UNK"
    return table.get(raw[3], "UNK")


def _parse_cipher_akm(body: bytes, pos: int, oui: bytes, ciphers: Dict[int, str], akms: Dict[int, str]) -> Dict:
    """Parse [group cipher][pairwise list][AKM list] starting at *pos* (shared RSN/WPA layout)."""
    out: Dict = {"group": "", "pairwise": ["CCMP"], "akm": "EAP"}  # spec defaults when omitted
    if pos + 4 > len(body):
        return out
    out["group"] = _suite(body[pos : pos + 4], oui, ciphers)
    pos += 4
    count = _u16(body, pos)
    if count is None:
        return out
    pos += 2
    pairwise: List[str] = []
    for _ in range(count):
        if pos + 4 > len(body):
            break
        pairwise.append(_suite(body[pos : pos + 4], oui, ciphers))
        pos += 4
    if pairwise:
        out["pairwise"] = pairwise
    count = _u16(body, pos)
    if count is None:
        return out
    pos += 2
    names: List[str] = []
    for _ in range(count):
        if pos + 4 > len(body):
            break
        names.append(_suite(body[pos : pos + 4], oui, akms))
        pos += 4
    if names:
        out["akm"] = " ".join(names)
    return out


def parse_ies(blob: bytes) -> Dict[str, Optional[Dict]]:
    """Walk the information elements and return {"RSN": {...}|None, "WPA": {...}|None}."""
    found: Dict[str, Optional[Dict]] = {"RSN": None, "WPA": None}
    pos = 0
    while pos + 2 <= len(blob):
        eid, length = blob[pos], blob[pos + 1]
        body = blob[pos + 2 : pos + 2 + length]
        pos += 2 + length
        if eid == 48 and found["RSN"] is None:  # RSN (WPA2 / WPA3 / OWE)
            found["RSN"] = _parse_cipher_akm(body, 2, _OUI_RSN, _CIPHERS_RSN, _AKM_RSN)
        elif eid == 221 and body[:4] == _OUI_WPA + b"\x01" and found["WPA"] is None:  # WPA v1
            found["WPA"] = _parse_cipher_akm(body, 6, _OUI_WPA, _CIPHERS_WPA, _AKM_WPA)
    return found


def _akm_flags(akm: str) -> Tuple[bool, bool, bool, bool]:
    tokens = akm.split()
    eap = any("EAP" in t for t in tokens)
    psk = any("PSK" in t for t in tokens)
    sae = any("SAE" in t for t in tokens)
    owe = any("OWE" in t for t in tokens)
    return eap, psk, sae, owe


def _akm_name(eap: bool, psk: bool, sae: bool, owe: bool) -> str:
    names = [n for n, on in (("EAP", eap), ("PSK", psk), ("SAE", sae), ("OWE", owe)) if on]
    return "+".join(names) if names else "UNK"


def _cipher(block: Dict) -> str:
    pairwise = block.get("pairwise") or []
    if pairwise:
        return "+".join(pairwise)
    return block.get("group") or "UNK"


def derive_security(ies: Dict[str, Optional[Dict]], capability: int) -> Tuple[str, str]:
    """Return (WiGLE-style auth string, short label such as 'WPA2' or 'Open')."""
    tags: List[str] = []
    label = ""
    rsn, wpa = ies.get("RSN"), ies.get("WPA")
    if wpa is not None:
        eap, psk, sae, owe = _akm_flags(wpa.get("akm", ""))
        tags.append(f"[WPA-{_akm_name(eap, psk, sae, owe)}-{_cipher(wpa)}]")
        label = "WPA"
    if rsn is not None:
        eap, psk, sae, owe = _akm_flags(rsn.get("akm", ""))
        cipher = _cipher(rsn)
        if owe and not (psk or sae or eap):
            tags.append(f"[OWE-{cipher}]")
            rsn_label = "OWE"
        elif sae and not psk and not eap:
            tags.append(f"[WPA3-SAE-{cipher}]")
            rsn_label = "WPA3"
        else:
            tags.append(f"[WPA2-{_akm_name(eap, psk, sae, owe)}-{cipher}]")
            if sae and psk:
                rsn_label = "WPA2/3"
            elif eap:
                rsn_label = "WPA2-EAP"
            else:
                rsn_label = "WPA2"
        label = f"WPA/{rsn_label}" if label else rsn_label
    if not tags:
        if capability & CAP_PRIVACY:
            tags.append("[WEP]")
            label = "WEP"
        else:
            label = "Open"
    tags.append("[IBSS]" if capability & CAP_IBSS else "[ESS]")
    return "".join(tags), label

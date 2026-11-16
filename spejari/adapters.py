"""Wi-Fi adapter discovery on top of a scan backend (Windows WLAN API or the simulator)."""

from __future__ import annotations

import dataclasses
import re
from typing import Dict, List

from .wlan import WlanError, WlanIface


class AdapterError(Exception):
    pass


def natural_key(text: str):
    """Sort 'Adapter 2' before 'Adapter 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def assign_tags(ifaces: List[WlanIface]) -> List[WlanIface]:
    """Give every adapter a short unique tag (last hex digits of its GUID)."""
    hex_ids = [re.sub(r"[^0-9A-Fa-f]", "", i.guid).upper() for i in ifaces]
    for length in (4, 6, 8, 12, 32):
        tags = [h[-length:] for h in hex_ids]
        if len(set(tags)) == len(tags):
            break
    return [dataclasses.replace(i, tag=t) for i, t in zip(ifaces, tags)]


def matches(iface: WlanIface, token: str) -> bool:
    """Does an include/exclude token (tag, GUID or part of the name) select this adapter?"""
    t = token.strip().lower()
    if not t:
        return False
    return t == iface.tag.lower() or t == iface.guid.lower() or t in iface.description.lower()


class AdapterDetector:
    """Lists scan-capable adapters, applies include/exclude rules and the adapter limit."""

    def __init__(self, scan_cfg, backend) -> None:
        self.cfg = scan_cfg
        self.backend = backend
        self.truncated = 0
        self.skipped: List[Dict[str, str]] = []  # detected but unused: {tag, name, reason}

    def detect(self) -> List[WlanIface]:
        try:
            raw = self.backend.interfaces()
        except WlanError as exc:
            raise AdapterError(str(exc)) from exc
        ifaces = assign_tags(sorted(raw, key=lambda i: (natural_key(i.description), i.guid)))
        active: List[WlanIface] = []
        skipped: List[Dict[str, str]] = []
        self.truncated = 0
        for iface in ifaces:
            reason = ""
            if self.cfg.include_interfaces and not any(matches(iface, t) for t in self.cfg.include_interfaces):
                reason = "not selected"
            elif any(matches(iface, t) for t in self.cfg.exclude_interfaces):
                reason = "excluded"
            elif len(active) >= self.cfg.max_adapters:
                reason = "over limit"
                self.truncated += 1
            if reason:
                skipped.append({"tag": iface.tag, "name": iface.description, "reason": reason})
            else:
                active.append(iface)
        self.skipped = skipped
        return active

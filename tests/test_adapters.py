from spejari.adapters import AdapterDetector, assign_tags, matches, natural_key
from spejari.config import ScanConfig
from spejari.sim import SimBackend
from spejari.wlan import WlanIface


def make(i, desc="Wi-Fi"):
    raw = bytes([i]) * 16
    return WlanIface("{%02X%02X%02X%02X-0000-0000-0000-000000000000}" % (i, i, i, i), raw, desc, 4)


def test_natural_sort():
    names = ["Adapter 10", "Adapter 2", "Adapter 1"]
    assert sorted(names, key=natural_key) == ["Adapter 1", "Adapter 2", "Adapter 10"]


def test_tags_are_unique_even_for_identical_adapters():
    ifaces = assign_tags([make(i, "Same Dongle") for i in range(1, 11)])
    tags = [i.tag for i in ifaces]
    assert len(set(tags)) == 10 and all(tags)


def test_matches_by_tag_guid_or_name():
    iface = assign_tags([make(7, "ALFA AWUS036ACM")])[0]
    assert matches(iface, iface.tag)
    assert matches(iface, iface.tag.lower())
    assert matches(iface, "awus036")
    assert matches(iface, iface.guid)
    assert not matches(iface, "intel")
    assert not matches(iface, "")


def test_detector_limit_exclude_include():
    backend = SimBackend(12)
    cfg = ScanConfig(max_adapters=10)
    det = AdapterDetector(cfg, backend)
    active = det.detect()
    assert len(active) == 10 and det.truncated == 2
    assert {s["reason"] for s in det.skipped} == {"over limit"}

    cfg.exclude_interfaces = ["Adapter 1"]  # substring: matches Adapter 1, 10, 11 and 12
    active = det.detect()
    assert len(active) == 8
    assert sum(1 for s in det.skipped if s["reason"] == "excluded") == 4

    cfg.exclude_interfaces = []
    cfg.include_interfaces = [active[0].tag]
    only = det.detect()
    assert len(only) == 1 and only[0].tag == active[0].tag
    assert any(s["reason"] == "not selected" for s in det.skipped)

"""End-to-end: simulated adapters + simulated GPS through the real engine, session and exporter."""

import time
from datetime import datetime

from spejari.config import Config
from spejari.exporter import export_filename, read_journal, write_wigle_csv
from spejari.gps import GpsMonitor
from spejari.models import FeedLog
from spejari.scanner import ScanEngine
from spejari.session import Session
from spejari.sim import SimBackend


def wait_for(predicate, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def build(adapters=4, gps_source="sim", **scan):
    cfg = Config()
    cfg.scan.adapter_poll_interval = 0.5
    cfg.scan.scan_cycle_s = 2.0
    for k, v in scan.items():
        setattr(cfg.scan, k, v)
    cfg.gps.source = gps_source
    cfg.normalise()
    cfg.scan.adapter_poll_interval = 0.5
    cfg.scan.scan_cycle_s = 2.0
    backend = SimBackend(adapters, stale_fraction=0.3)
    feed = FeedLog()
    gps = GpsMonitor(cfg.gps)
    engine = ScanEngine(cfg, backend, gps, feed)
    gps.start()
    engine.start()
    return cfg, backend, feed, gps, engine


def test_full_session_with_hotplug(tmp_path):
    cfg, backend, feed, gps, engine = build(4)
    try:
        assert wait_for(lambda: engine.adapter_count() == 4)
        assert wait_for(lambda: gps.snapshot().valid)
        session = Session(cfg, feed)
        engine.begin_session(session)
        assert wait_for(lambda: session.stats().unique_bssids > 20, 12)

        backend.set_adapter_count(6)  # hot-plug two more
        assert wait_for(lambda: engine.adapter_count() == 6)
        backend.set_adapter_count(2)  # pull four out
        assert wait_for(lambda: engine.adapter_count() == 2)

        done = engine.end_session()
        assert done is session
        stats = session.stats()
        assert stats.unique_ssids > 20 and stats.logged >= stats.unique_bssids - stats.hidden - 5
        messages = [e.message for e in feed.tail(500) if e.kind in ("SYS", "WARN")]
        assert any("added" in m for m in messages) and any("removed" in m for m in messages)

        path = tmp_path / export_filename(datetime(2026, 11, 9, 12, 0, 0))
        rows = write_wigle_csv(session.observations, path)
        assert rows == len(session.observations) > 0
        assert path.suffix == ".csv"
    finally:
        engine.shutdown()
        gps.stop()


def test_nothing_is_logged_without_a_gps_fix():
    cfg, backend, feed, gps, engine = build(2, gps_source="none")
    try:
        session = Session(cfg, feed)
        engine.begin_session(session)
        assert wait_for(lambda: session.stats().unique_bssids > 5, 12)
        engine.end_session()
        stats = session.stats()
        assert stats.logged == 0 and stats.seen_no_fix > 0
    finally:
        engine.shutdown()


def test_stale_cache_entries_are_ignored():
    """A bss_list that never changes (cached beacons) yields nothing after the priming read."""
    cfg, backend, feed, gps, engine = build(1)
    try:
        frozen = backend.bss_list(backend.interfaces()[0])
        backend.bss_list = lambda iface: frozen  # same TSF values forever
        assert wait_for(lambda: engine.adapter_count() == 1)
        session = Session(cfg, feed)
        engine.begin_session(session)
        time.sleep(7)
        engine.end_session()
        assert session.stats().hits == 0
    finally:
        engine.shutdown()
        gps.stop()

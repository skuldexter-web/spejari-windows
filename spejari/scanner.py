"""Scanning engine: one worker per adapter, staggered scans, hot-plug aware manager."""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from .adapters import AdapterDetector, AdapterError, natural_key
from .config import Config
from .gps import GpsMonitor
from .models import AdapterView, FeedLog
from .session import Session
from .wlan import (
    ERROR_ACCESS_DENIED,
    ERROR_BUSY,
    BssEntry,
    WlanError,
    WlanIface,
    bss_to_access_point,
    explain_error,
)

GONE_CODES = {2, 1167, 1168}  # file/device not found, device not connected, element not found


class AdapterWorker(threading.Thread):
    """Runs scan cycles on one adapter: WlanScan, wait for the sweep, read the BSS list."""

    MAX_ERRORS = 5

    def __init__(self, engine: "ScanEngine", iface: WlanIface) -> None:
        super().__init__(name=f"spejari-scan-{iface.tag}", daemon=True)
        self.engine = engine
        self.iface = iface
        self._stop_evt = threading.Event()
        self._slot, self._slots = 0, 1
        self._tsf: Dict[str, int] = {}
        self._prime = True
        self._errors = 0
        self._last_warn = 0.0
        self.unhealthy_until = 0.0
        self.busy = False
        self.state = "starting"
        self.hits = 0
        self.scans = 0
        self.last_scan: Optional[float] = None

    # -- control ---------------------------------------------------------------
    def stop(self) -> None:
        self._stop_evt.set()

    def set_slot(self, slot: int, slots: int) -> None:
        self._slot, self._slots = slot, max(1, slots)

    @property
    def healthy(self) -> bool:
        return time.monotonic() >= self.unhealthy_until

    def _warn(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_warn > 30:
            self._last_warn = now
            self.engine.feed.system(f"{self.iface.tag}: {text}", warn=True)

    def _sleep(self, seconds: float) -> None:
        """Interruptible wait: ends early when the session stops or the worker is stopped."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop_evt.is_set() or not self.engine.scanning.is_set():
                return
            self._stop_evt.wait(min(0.1, max(0.0, end - time.monotonic())))

    # -- main loop -------------------------------------------------------------
    def run(self) -> None:
        was_scanning = False
        while not self._stop_evt.is_set():
            if not self.engine.scanning.is_set():
                self.state, was_scanning = "idle", False
                self._stop_evt.wait(0.3)
                continue
            if not was_scanning:  # a new session just started on this adapter
                was_scanning, self._prime = True, True
                self._tsf.clear()
                cfg = self.engine.cfg.scan
                if cfg.stagger and self._slots > 1:
                    self.state = "waiting"
                    self._sleep(cfg.scan_cycle_s * self._slot / self._slots)
                    continue
            if not self.healthy:
                self.state = "paused"
                self._stop_evt.wait(0.5)
                continue
            self._cycle()

    def _cycle(self) -> None:
        backend = self.engine.backend
        self.busy, self.state = True, "scanning"
        try:
            rc = backend.scan(self.iface)
            if rc not in (0, ERROR_BUSY):
                raise WlanError(explain_error(rc), rc)
            self._sleep(self.engine.cfg.scan.scan_cycle_s)
            entries = backend.bss_list(self.iface)
            self._ingest(entries)
            self.scans += 1
            self.last_scan = time.monotonic()
            self._errors = 0
        except WlanError as exc:
            self._on_error(exc)
        finally:
            self.busy = False

    def _ingest(self, entries: List[BssEntry]) -> None:
        cfg = self.engine.cfg.scan
        aps = []
        for e in entries:
            if cfg.drop_stale:
                last = self._tsf.get(e.bssid)
                self._tsf[e.bssid] = e.tsf
                if self._prime or last == e.tsf:  # first read after start, or no new beacon
                    continue
            ap = bss_to_access_point(e)
            if ap is None:
                continue
            if (ap.band == "2.4" and not cfg.band_2g) or (ap.band == "5" and not cfg.band_5g):
                continue
            aps.append(ap)
        self._prime = False
        self.hits += len(aps)
        if aps:
            self.engine.ingest_batch(aps, self.iface.tag)

    def _on_error(self, exc: WlanError) -> None:
        code = exc.code
        if code in GONE_CODES:
            self.state = "removed"
            self.unhealthy_until = time.monotonic() + 5  # the manager drops it on the next poll
            return
        if code == ERROR_ACCESS_DENIED:
            self.state = "no permission"
            self._warn(str(exc))
            self.unhealthy_until = time.monotonic() + 15
            return
        self._errors += 1
        self.state = "error"
        self._warn(f"scan failed: {str(exc)[:90]}")
        if self._errors >= self.MAX_ERRORS:
            self._errors = 0
            self.unhealthy_until = time.monotonic() + 15
        else:
            self._stop_evt.wait(min(0.5 * self._errors, 3.0))


class ScanEngine:
    """Detects adapters, schedules their scans and feeds results into the session."""

    def __init__(self, cfg: Config, backend, gps: GpsMonitor, feed: FeedLog) -> None:
        self.cfg = cfg
        self.backend = backend
        self.gps = gps
        self.feed = feed
        self.detector = AdapterDetector(cfg.scan, backend)
        self.scanning = threading.Event()
        self.session: Optional[Session] = None
        self.detect_error: Optional[str] = None
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._workers: Dict[str, AdapterWorker] = {}
        self._adapters: Dict[str, WlanIface] = {}
        self._skipped: List[Dict[str, str]] = []
        self._last_error_msg = ""
        self._warned_truncated = False
        self._warned_empty = False
        self._scan_started = 0.0
        self._slot_sig: tuple = ()
        self._manager = threading.Thread(target=self._manage, name="spejari-cluster", daemon=True)

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._manager.start()

    def shutdown(self) -> None:
        self._stop_evt.set()
        self.scanning.clear()
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=3.0)
        try:
            self.backend.close()
        except Exception:
            pass

    def begin_session(self, session: Session) -> None:
        with self._lock:
            self.session = session
            self._warned_empty = False
            self._scan_started = time.monotonic()
        self.scanning.set()

    def end_session(self, timeout: float = 20.0) -> Optional[Session]:
        """Stop scanning, let in-flight cycles deliver their results, detach the session."""
        self.scanning.clear()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not any(w.busy for w in self._workers.values()):
                    break
            time.sleep(0.05)
        with self._lock:
            session, self.session = self.session, None
        return session

    # -- data flow -------------------------------------------------------------
    def ingest_batch(self, aps, tag: str) -> None:
        session = self.session
        if session is None:
            return
        session.ingest_batch(aps, tag, self.gps.snapshot(), self.gps.utc_now())

    def adapter_count(self) -> int:
        with self._lock:
            return len(self._adapters)

    def skipped_adapters(self) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._skipped)

    def adapter_views(self) -> List[AdapterView]:
        now = time.monotonic()
        with self._lock:
            views = []
            for guid in sorted(self._adapters, key=lambda g: (natural_key(self._adapters[g].description), g)):
                iface, w = self._adapters[guid], self._workers.get(guid)
                views.append(
                    AdapterView(
                        tag=iface.tag,
                        name=iface.description,
                        state=w.state if w else "starting",
                        hits=w.hits if w else 0,
                        scans=w.scans if w else 0,
                        last_scan_age=(now - w.last_scan) if (w and w.last_scan) else None,
                        interface_state=iface.state_text,
                    )
                )
            return views

    # -- adapter manager (hot-plug aware) --------------------------------------
    def _manage(self) -> None:
        while not self._stop_evt.is_set():
            try:
                ifaces = self.detector.detect()
                self.detect_error = None
            except AdapterError as exc:
                ifaces, self.detect_error = [], str(exc)
                if str(exc) != self._last_error_msg:
                    self._last_error_msg = str(exc)
                    self.feed.system(str(exc), warn=True)
            if self.detector.truncated and not self._warned_truncated:
                self._warned_truncated = True
                self.feed.system(
                    f"{self.detector.truncated} extra adapter(s) ignored (limit {self.cfg.scan.max_adapters})",
                    warn=True,
                )
            self._apply(ifaces)
            self._check_for_silence()
            self._stop_evt.wait(self.cfg.scan.adapter_poll_interval)

    def _apply(self, ifaces: List[WlanIface]) -> None:
        new = {i.guid: i for i in ifaces}
        with self._lock:
            old = set(self._adapters)
            for guid in old - set(new):
                worker = self._workers.pop(guid, None)
                if worker:
                    worker.stop()
                self.feed.system(f"Adapter {self._adapters[guid].tag} removed")
            for guid, iface in new.items():
                worker = self._workers.get(guid)
                if worker is None or not worker.is_alive():
                    if worker is not None and guid in old:
                        self.feed.system(f"Restarting scanner on {iface.tag}", warn=True)
                    worker = AdapterWorker(self, iface)
                    self._workers[guid] = worker
                    worker.start()
                    if guid not in old:
                        self.feed.system(f"Adapter {iface.tag} added: {iface.description}")
            self._adapters = new
            self._skipped = list(self.detector.skipped)
            order = sorted(new, key=lambda g: (natural_key(new[g].description), g))
            sig = tuple(order)
            for slot, guid in enumerate(order):
                self._workers[guid].set_slot(slot, len(order))
            if sig != self._slot_sig:
                self._slot_sig = sig
                if order:
                    gap = self.cfg.scan.scan_cycle_s / len(order)
                    self.feed.system(
                        f"{len(order)} adapter(s) active - staggered: a fresh scan result about every {gap:.1f} s"
                    )
                elif old:
                    self.feed.system("No usable adapters - waiting for hot-plug", warn=True)

    def _check_for_silence(self) -> None:
        """Warn when scanning yields nothing at all (typically Windows location privacy)."""
        with self._lock:
            session = self.session
            if (
                session is None
                or self._warned_empty
                or not self.scanning.is_set()
                or not self._workers
            ):
                return
            waited = time.monotonic() - self._scan_started
            if waited < max(25.0, self.cfg.scan.scan_cycle_s * 5) or session.stats().hits > 0:
                return
            if not any(w.scans > 0 for w in self._workers.values()):
                return
            self._warned_empty = True
        self.feed.system(
            "No networks seen yet. If Wi-Fi is on, Windows may be blocking scans: Settings > Privacy & security > "
            "Location > turn on 'Location services' and 'Let desktop apps access your location'.",
            warn=True,
        )

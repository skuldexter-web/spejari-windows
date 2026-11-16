"""GPS integration: COM-port NMEA reader (u-blox friendly), gpsd client, simulator, tracker."""

from __future__ import annotations

import dataclasses
import json
import math
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .config import GpsConfig
from .geo import haversine_m
from .models import GpsFix

try:  # pyserial is only needed for COM-port GPS receivers
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None

KNOTS_TO_MPS = 0.514444
UBLOX_VENDOR_ID = 0x1546


# --------------------------------------------------------------------------- NMEA
def nmea_checksum_ok(sentence: str) -> bool:
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, chk = sentence[1:].partition("*")
    chk = chk.strip()[:2]
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(chk, 16)
    except ValueError:
        return False


def _dm_to_deg(value: str, hemi: str) -> Optional[float]:
    """Convert NMEA (d)ddmm.mmmm + hemisphere to signed decimal degrees."""
    if not value or not hemi:
        return None
    dot = value.find(".")
    if dot < 0:
        dot = len(value)
    deg_len = dot - 2
    if deg_len <= 0:
        return None
    deg = float(value[:deg_len]) + float(value[deg_len:]) / 60.0
    return -deg if hemi in ("S", "W") else deg


class NmeaState:
    """Accumulates NMEA sentences into a coherent position/fix state."""

    def __init__(self) -> None:
        self.has_fix = False
        self.mode = 0
        self.lat: Optional[float] = None
        self.lon: Optional[float] = None
        self.alt: Optional[float] = None
        self.speed_mps: Optional[float] = None
        self.track: Optional[float] = None
        self.hdop: Optional[float] = None
        self.sats_used = 0
        self.sats_seen = 0
        self.utc: Optional[datetime] = None  # set by RMC, consumed by the monitor
        self._gsa_mode = 0
        self._gsv: Dict[str, int] = {}

    def feed(self, sentence: str) -> bool:
        """Consume a sentence; True when it carried a position update (GGA/RMC)."""
        sentence = sentence.strip()
        if not nmea_checksum_ok(sentence):
            return False
        head, _, rest = sentence[1:].split("*", 1)[0].partition(",")
        fields = rest.split(",")
        kind, talker = head[-3:], head[:-3]
        try:
            if kind == "GGA":
                return self._gga(fields)
            if kind == "RMC":
                return self._rmc(fields)
            if kind == "GSA":
                self._gsa(fields)
            elif kind == "GSV":
                self._gsv[talker] = int(fields[2])
                self.sats_seen = sum(self._gsv.values())
        except (ValueError, IndexError):
            pass
        return False

    def _lose_fix(self) -> bool:
        self.has_fix = False
        self.mode = 1
        self._gsa_mode = 0
        return True

    def _gga(self, f: List[str]) -> bool:
        if int(f[5] or 0) == 0:
            return self._lose_fix()
        lat, lon = _dm_to_deg(f[1], f[2]), _dm_to_deg(f[3], f[4])
        if lat is None or lon is None:
            return self._lose_fix()
        self.lat, self.lon = lat, lon
        self.alt = float(f[8]) if f[8] else None
        self.sats_used = int(f[6] or 0)
        if f[7]:
            self.hdop = float(f[7])
        self.has_fix = True
        if self._gsa_mode in (2, 3):
            self.mode = self._gsa_mode
        else:
            self.mode = 3 if self.alt is not None else 2
        return True

    def _rmc(self, f: List[str]) -> bool:
        if f[1] != "A":
            return self._lose_fix()
        lat, lon = _dm_to_deg(f[2], f[3]), _dm_to_deg(f[4], f[5])
        if lat is None or lon is None:
            return self._lose_fix()
        self.lat, self.lon = lat, lon
        self.speed_mps = float(f[6]) * KNOTS_TO_MPS if f[6] else None
        self.track = float(f[7]) if f[7] else None
        if f[0] and len(f) > 8 and f[8]:
            stamp = datetime.strptime(f[8] + f[0][:6], "%d%m%y%H%M%S").replace(tzinfo=timezone.utc)
            if len(f[0]) > 7:
                stamp += timedelta(seconds=float("0" + f[0][6:]))
            self.utc = stamp
        self.has_fix = True
        if self.mode < 2:
            self.mode = 2
        return True

    def _gsa(self, f: List[str]) -> None:
        fix_type = int(f[1] or 0)
        self._gsa_mode = fix_type if fix_type in (1, 2, 3) else 0
        if len(f) > 15 and f[15]:  # f[14]=PDOP f[15]=HDOP f[16]=VDOP
            self.hdop = float(f[15])


# ---------------------------------------------------------------------- serial helpers
def list_serial_ports() -> List[Dict[str, object]]:
    """All COM ports: [{port, vid, pid, description, ublox}] sorted u-blox first, then by name."""
    if list_ports is None:
        return []
    items = []
    for p in list_ports.comports():
        items.append(
            {
                "port": p.device,
                "vid": p.vid or 0,
                "pid": p.pid or 0,
                "description": p.description or "",
                "ublox": (p.vid or 0) == UBLOX_VENDOR_ID,
            }
        )
    items.sort(key=lambda i: (not i["ublox"], _port_key(str(i["port"]))))
    return items


def _port_key(name: str):
    digits = "".join(ch for ch in name if ch.isdigit())
    return (name.rstrip("0123456789"), int(digits) if digits else 0)


def candidate_serial_ports(probe_other: bool = False) -> List[str]:
    """u-blox devices first; other COM ports only when explicitly allowed."""
    ports = list_serial_ports()
    chosen = [str(p["port"]) for p in ports if p["ublox"]]
    if probe_other:
        chosen += [str(p["port"]) for p in ports if not p["ublox"]]
    return chosen


# ---------------------------------------------------------------------------- monitor
class GpsMonitor(threading.Thread):
    """Background thread keeping a live GPS snapshot and cumulative distance."""

    def __init__(self, cfg: GpsConfig) -> None:
        super().__init__(name="spejari-gps", daemon=True)
        self.cfg = cfg
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._fix = GpsFix()
        self._clock_offset: Optional[float] = None
        self._last_pos: Optional[Tuple[float, float]] = None
        self._last_t = 0.0
        self._rejected = 0
        self._nmea = NmeaState()
        self._idle_status = "No GPS found (COM port / gpsd) - retrying"
        if cfg.source == "none":
            self._fix.status = "GPS disabled"

    # -- public API ------------------------------------------------------------
    def stop(self) -> None:
        self._stop_evt.set()

    def snapshot(self) -> GpsFix:
        with self._lock:
            fix = dataclasses.replace(self._fix)
        fresh = fix.last_update > 0 and (time.monotonic() - fix.last_update) <= self.cfg.stale_after_s
        fix.valid = bool(
            fix.connected and fresh and fix.mode >= self.cfg.min_fix_mode and fix.lat is not None
        )
        if fix.connected:
            if fix.valid:
                fix.status = "3D FIX" if fix.mode >= 3 else "2D FIX"
            elif fix.last_update > 0 and fix.mode >= self.cfg.min_fix_mode:
                fix.status = "FIX LOST (stale data)"
            else:
                fix.status = f"NO FIX - satellites {fix.sats_used}/{fix.sats_seen}"
        return fix

    def utc_now(self) -> datetime:
        """UTC time, corrected with GPS time when available."""
        offset = self._clock_offset
        ts = time.time() + (offset if offset is not None else 0.0)
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    # -- thread body -----------------------------------------------------------
    def run(self) -> None:
        if self.cfg.source == "none":
            return
        while not self._stop_evt.is_set():
            connected = False
            try:
                if self.cfg.source == "sim":
                    connected = self._run_sim()
                else:
                    if self.cfg.source in ("auto", "gpsd"):
                        connected = self._run_gpsd()
                    if not connected and not self._stop_evt.is_set() and self.cfg.source in ("auto", "serial"):
                        connected = self._run_serial()
            except Exception as exc:  # the GPS thread must never die
                self._idle_status = f"GPS error: {exc}"
            self._mark_disconnected()
            self._stop_evt.wait(2.0)

    # -- state helpers ---------------------------------------------------------
    def _mark_disconnected(self) -> None:
        with self._lock:
            f = self._fix
            f.connected = False
            f.valid = False
            f.mode = 0
            f.sats_used = f.sats_seen = 0
            f.status = self._idle_status

    def _begin(self, source: str, device: str) -> None:
        with self._lock:
            self._fix.connected = True
            self._fix.source = source
            self._fix.device = device

    def _set_sats(self, used: int, seen: int, hdop: Optional[float]) -> None:
        with self._lock:
            self._fix.sats_used, self._fix.sats_seen = used, seen
            if hdop is not None:
                self._fix.hdop = hdop

    def _ingest(
        self,
        source: str,
        device: str,
        mode: int,
        lat: Optional[float],
        lon: Optional[float],
        alt: Optional[float] = None,
        speed: Optional[float] = None,
        track: Optional[float] = None,
        accuracy: Optional[float] = None,
        utc: Optional[datetime] = None,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            f = self._fix
            f.connected, f.source = True, source
            if device:
                f.device = device
            f.mode = mode
            if mode >= self.cfg.min_fix_mode and lat is not None and lon is not None:
                f.lat, f.lon, f.alt, f.speed_mps, f.track = lat, lon, alt, speed, track
                f.last_update = now
                if accuracy is not None:
                    f.accuracy_m = accuracy
                self._track_distance(lat, lon, now)
            if utc is not None:
                self._clock_offset = utc.timestamp() - time.time()

    def _track_distance(self, lat: float, lon: float, now: float) -> None:
        """Cumulative distance with jitter and glitch filtering (lock held)."""
        if self._last_pos is None:
            self._last_pos, self._last_t = (lat, lon), now
            return
        dt = max(now - self._last_t, 0.001)
        self._last_t = now
        d = haversine_m(self._last_pos[0], self._last_pos[1], lat, lon)
        if d < self.cfg.min_step_m:
            return
        if d / dt > self.cfg.max_speed_mps:
            self._rejected += 1
            if self._rejected >= 3:  # persistent jump: accept as new baseline
                self._last_pos, self._rejected = (lat, lon), 0
            return
        self._rejected = 0
        self._fix.distance_m += d
        self._last_pos = (lat, lon)

    # -- simulator ---------------------------------------------------------------
    def _run_sim(self) -> bool:
        """A car driving laps around a ~1.5 km loop at about 14 m/s (50 km/h)."""
        self._begin("sim", "simulated GPS")
        centre_lat, centre_lon = 52.2297, 21.0122
        radius_m = 250.0
        speed = 14.0
        omega = speed / radius_m
        t0 = time.monotonic()
        while not self._stop_evt.is_set():
            ang = (time.monotonic() - t0) * omega
            lat = centre_lat + math.degrees(radius_m * math.sin(ang) / 6_371_000.0)
            lon = centre_lon + math.degrees(radius_m * math.cos(ang) / (6_371_000.0 * math.cos(math.radians(centre_lat))))
            self._ingest("sim", "simulated GPS", 3, lat, lon, 110.0, speed, math.degrees(ang) % 360, 4.0,
                         datetime.now(timezone.utc))
            self._set_sats(10, 14, 0.9)
            self._stop_evt.wait(0.5)
        return True

    # -- gpsd (TCP, also usable over the network) --------------------------------
    def _run_gpsd(self) -> bool:
        try:
            sock = socket.create_connection((self.cfg.gpsd_host, self.cfg.gpsd_port), timeout=2.0)
        except OSError:
            return False
        got_data = False
        started = time.monotonic()
        buf = b""
        try:
            sock.settimeout(1.0)
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            self._begin("gpsd", "")
            self._idle_status = "gpsd connection lost"
            while not self._stop_evt.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    chunk = None
                if chunk == b"":
                    break
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if self._handle_gpsd(line):
                            got_data = True
                if self.cfg.source == "auto" and not got_data and time.monotonic() - started > 8:
                    break  # gpsd has no device: fall back to a COM port
        except OSError:
            pass
        finally:
            sock.close()
        return got_data

    def _handle_gpsd(self, line: bytes) -> bool:
        try:
            msg = json.loads(line)
        except ValueError:
            return False
        cls = msg.get("class")
        if cls == "TPV":
            acc = msg.get("eph")
            if acc is None and msg.get("epx") is not None and msg.get("epy") is not None:
                acc = max(float(msg["epx"]), float(msg["epy"]))
            utc = None
            stamp = msg.get("time")
            if stamp:
                try:
                    utc = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    utc = None
            self._ingest(
                "gpsd",
                msg.get("device", ""),
                int(msg.get("mode", 0)),
                msg.get("lat"),
                msg.get("lon"),
                alt=msg.get("altHAE", msg.get("alt")),
                speed=msg.get("speed"),
                track=msg.get("track"),
                accuracy=acc,
                utc=utc,
            )
            return True
        if cls == "SKY":
            sats = msg.get("satellites") or []
            used = msg.get("uSat")
            if used is None:
                used = sum(1 for s in sats if s.get("used"))
            self._set_sats(int(used), int(msg.get("nSat", len(sats))), msg.get("hdop"))
        elif cls in ("DEVICE", "DEVICES"):
            path = msg.get("path") or ((msg.get("devices") or [{}])[0].get("path", ""))
            if path:
                with self._lock:
                    self._fix.device = path
        return False

    # -- direct COM-port NMEA ----------------------------------------------------
    def _run_serial(self) -> bool:
        if serial is None:
            self._idle_status = "pyserial is not installed (pip install pyserial)"
            return False
        ports = [self.cfg.serial_port] if self.cfg.serial_port else candidate_serial_ports(self.cfg.probe_other_ports)
        bauds = [self.cfg.baud] if self.cfg.baud else [9600, 38400, 115200, 57600, 4800]
        for port in ports:
            for baud in bauds:
                if self._stop_evt.is_set():
                    return False
                try:
                    ser = serial.Serial(port, baud, timeout=0.5)
                except (serial.SerialException, OSError, ValueError) as exc:
                    self._idle_status = f"Cannot open {port}: {str(exc)[:60]}"
                    break  # this port is unusable, try the next one
                try:
                    if not self._probe(ser):
                        continue
                    self._stream(ser, port)
                    return True
                finally:
                    try:
                        ser.close()
                    except Exception:
                        pass
        if not ports:
            self._idle_status = "No GPS COM port found - see README (u-blox driver / --gps-device COMx)"
        return False

    @staticmethod
    def _read_lines(ser, buf: bytearray) -> List[str]:
        data = ser.read(max(1, ser.in_waiting))  # blocks up to the 0.5 s port timeout
        if data:
            buf.extend(data)
        lines: List[str] = []
        while b"\n" in buf:
            raw, _, rest = bytes(buf).partition(b"\n")
            buf[:] = rest
            lines.append(raw.decode("ascii", errors="ignore").strip())
        if len(buf) > 8192:
            buf.clear()
        return lines

    def _probe(self, ser, seconds: float = 3.0) -> bool:
        deadline = time.monotonic() + seconds
        buf = bytearray()
        valid = 0
        try:
            while time.monotonic() < deadline and not self._stop_evt.is_set():
                for line in self._read_lines(ser, buf):
                    if nmea_checksum_ok(line):
                        valid += 1
        except (serial.SerialException, OSError):
            return False
        return valid >= 2

    def _stream(self, ser, port: str) -> None:
        desc = ""
        for p in list_serial_ports():
            if p["port"] == port:
                desc = str(p["description"])
        label = port + (f" ({desc})" if desc else "")
        self._nmea = NmeaState()
        self._begin("serial", label)
        self._idle_status = f"GPS on {port} disconnected"
        buf = bytearray()
        last_data = time.monotonic()
        while not self._stop_evt.is_set():
            try:
                lines = self._read_lines(ser, buf)
            except (serial.SerialException, OSError):
                return
            if lines:
                last_data = time.monotonic()
            elif time.monotonic() - last_data > 6.0:
                return
            for line in lines:
                if self._nmea.feed(line):
                    self._ingest_nmea("serial", label)
            self._set_sats(self._nmea.sats_used, self._nmea.sats_seen, self._nmea.hdop)

    def _ingest_nmea(self, source: str, device: str) -> None:
        n = self._nmea
        utc, n.utc = n.utc, None  # consume: only valid right after an RMC
        if n.has_fix:
            acc = n.hdop * 5.0 if n.hdop else None  # rough UERE-based estimate
            self._ingest(source, device, n.mode, n.lat, n.lon, n.alt, n.speed_mps, n.track, acc, utc)
        else:
            self._ingest(source, device, 1, None, None)

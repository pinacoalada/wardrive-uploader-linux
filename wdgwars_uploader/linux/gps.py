"""Location sources Linux has in place of Windows location: gpsd and GeoClue.

* **gpsd** — when gpsd runs it owns the GPS (USB, Bluetooth, or a phone feeding
  it), so its JSON stream on localhost:2947 is read like any other source.
* **GeoClue** — the desktop location service (GNOME's "Location Services"):
  a GPS or modem it knows about, or Wi-Fi positioning. As on Windows, an
  estimate coarser than 1 km (an IP-address guess) is not used.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import time
from datetime import datetime
from typing import Any

from ..wardrive.gps import Fix, _Source

log = logging.getLogger("wdgwars.gps.linux")

GPSD_ADDR = ("127.0.0.1", 2947)
GEOCLUE_MAX_ACC = 1000.0


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class GpsdGps(_Source):
    name = "gpsd"
    id = "gps:gpsd"

    @staticmethod
    def installed() -> bool:
        import shutil
        return bool(shutil.which("gpsd") or shutil.which("gpsd", path="/usr/sbin:/sbin"))

    def _run(self) -> None:
        self.state = "not running"
        while not self._stop.is_set():
            try:
                with socket.create_connection(GPSD_ADDR, timeout=3) as s:
                    s.sendall(b'?WATCH={"enable":true,"json":true};\n')
                    s.settimeout(2)
                    self.present = True
                    self.state, self.detail = "no fix yet", ""
                    buf = b""
                    while not self._stop.is_set():
                        try:
                            chunk = s.recv(8192)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._message(line)
            except OSError:
                self.state, self.detail = "not running", "install and start gpsd to use it"
            self.present = False
            self._stop.wait(10)

    def _message(self, line: bytes) -> None:
        try:
            m = json.loads(line)
        except ValueError:
            return
        cls = m.get("class")
        if cls == "SKY":
            sats = m.get("uSat")
            if sats is None and isinstance(m.get("satellites"), list):
                sats = sum(1 for x in m["satellites"] if x.get("used"))
            self._sats = int(sats) if isinstance(sats, (int, float)) else None
            return
        if cls == "DEVICES":
            devs = [d.get("path", "") for d in m.get("devices") or []]
            self.detail = ", ".join(d for d in devs if d) or "no GPS attached to gpsd"
            return
        if cls != "TPV":
            return
        mode = int(m.get("mode") or 0)
        lat, lon = _num(m.get("lat")), _num(m.get("lon"))
        if mode < 2 or lat is None or lon is None:
            self.state = "no fix yet"
            return
        eph = _num(m.get("eph"))
        if eph is None:
            ex, ey = _num(m.get("epx")), _num(m.get("epy"))
            eph = math.hypot(ex, ey) if ex is not None and ey is not None else (10.0 if mode == 3 else 25.0)
        utc = _iso(m.get("time") or "") or time.time()
        age = max(0.0, min(time.time() - utc, 5.0))
        alt = _num(m.get("altHAE")) if m.get("altHAE") is not None else _num(m.get("alt"))
        self.fix = Fix(lat, lon, alt if mode == 3 else None, eph, _num(m.get("speed")), _num(m.get("track")),
                       getattr(self, "_sats", None), time.time() - age, time.monotonic() - age, "gpsd")
        self.state = "fix"
        self.detail = f"{mode}D fix"


class GeoClueGps(_Source):
    """GeoClue2 over the system bus (dbus-python, from python3-dbus)."""

    name = "Location services"
    id = "gps:geoclue"
    DESKTOP_ID = "wardrive-uploader"

    def _run(self) -> None:
        try:
            import dbus
        except ImportError:
            self.state, self.detail = "unavailable", "python3-dbus is not installed"
            return
        while not self._stop.is_set():
            try:
                self._watch(dbus)
            except Exception as e:  # noqa: BLE001 - service missing, access denied, bus gone
                msg = str(e)
                if "AccessDenied" in msg or "not allowed" in msg.lower():
                    self.state, self.detail = "not allowed", "turn on Location Services in Settings › Privacy"
                elif "ServiceUnknown" in msg or "not provided" in msg:
                    self.state, self.detail = "unavailable", "GeoClue is not installed"
                else:
                    self.state, self.detail = "unavailable", msg[:120]
            self._stop.wait(30)

    def _watch(self, dbus) -> None:
        bus = dbus.SystemBus()
        mgr = dbus.Interface(bus.get_object("org.freedesktop.GeoClue2", "/org/freedesktop/GeoClue2/Manager"),
                             "org.freedesktop.GeoClue2.Manager")
        path = mgr.GetClient()
        obj = bus.get_object("org.freedesktop.GeoClue2", path)
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        props.Set("org.freedesktop.GeoClue2.Client", "DesktopId", self.DESKTOP_ID)
        props.Set("org.freedesktop.GeoClue2.Client", "RequestedAccuracyLevel", dbus.UInt32(8))   # EXACT
        props.Set("org.freedesktop.GeoClue2.Client", "DistanceThreshold", dbus.UInt32(0))
        client = dbus.Interface(obj, "org.freedesktop.GeoClue2.Client")
        client.Start()
        self.present = True
        self.state, self.detail = "listening", ""
        try:
            last = None
            while not self._stop.is_set():
                loc_path = props.Get("org.freedesktop.GeoClue2.Client", "Location")
                if loc_path and loc_path != "/" and loc_path != last:
                    last = loc_path
                    self._read(bus, loc_path)
                self._stop.wait(1.0)
        finally:
            self.present = False
            try:
                client.Stop()
            except Exception:  # noqa: BLE001
                pass

    def _read(self, bus, loc_path) -> None:
        p = bus.get_object("org.freedesktop.GeoClue2", loc_path)
        get = lambda k: p.Get("org.freedesktop.GeoClue2.Location", k,  # noqa: E731
                              dbus_interface="org.freedesktop.DBus.Properties")
        acc = _num(get("Accuracy")) or 99999.0
        if acc > GEOCLUE_MAX_ACC:
            self.state, self.detail = "no position", "only a coarse (IP-address) estimate"
            return
        try:
            sec, usec = get("Timestamp")
            utc = float(sec) + float(usec) / 1e6
        except Exception:  # noqa: BLE001
            utc = time.time()
        age = max(0.0, time.time() - utc)
        alt = _num(get("Altitude"))
        spd, hdg = _num(get("Speed")), _num(get("Heading"))
        self.fix = Fix(float(get("Latitude")), float(get("Longitude")),
                       alt if alt is not None and alt > -1e6 else None, acc,
                       spd if spd is not None and spd >= 0 else None, hdg if hdg is not None and hdg >= 0 else None,
                       None, time.time() - age, time.monotonic() - age, "Location services")
        self.state, self.detail = "fix", f"±{acc:.0f} m"

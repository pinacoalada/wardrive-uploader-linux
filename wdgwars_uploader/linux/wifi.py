"""Wi-Fi scanning on Linux through nl80211, on every managed-mode Wi-Fi interface.

The drop-in for the Windows Native Wifi scanner (same ``trigger``/``read``/
``adapters``/``off`` surface). Results come straight from the kernel's BSS cache,
with the raw information elements, so security is decoded exactly as on Windows
and every network carries the moment it was really last heard.

Scans are requested directly over nl80211 when the app holds CAP_NET_ADMIN, and
through NetworkManager (``nmcli device wifi rescan``) when it does not; NM only
allows a rescan every ~10 s, so the freshness window is wider on that path.
Interfaces in monitor mode are skipped here; :mod:`.monitor` owns them.
"""

from __future__ import annotations

import errno
import logging
import shutil
import subprocess
import time
from typing import Any

from ..wardrive.wifi import auth_mode, channel
from . import nl80211
from .netlink import NetlinkError

log = logging.getLogger("wdgwars.wifi")

FRESH_DIRECT = 6.0       # scans triggered every pass
FRESH_NM = 14.0          # NetworkManager rate-limits rescans
NM_RESCAN_EVERY = 10.0


def _ssid(ies: bytes) -> str:
    i = 0
    while i + 2 <= len(ies):
        tag, ln = ies[i], ies[i + 1]
        if tag == 0:
            raw = ies[i + 2:i + 2 + ln]
            return "" if not any(raw) else raw.decode("utf-8", "replace")
        i += 2 + ln
    return ""


class WifiScanner:
    def __init__(self) -> None:
        self.nl = nl80211.Nl80211()        # raises when the kernel has no nl80211
        self.adapters: list[dict[str, Any]] = []
        self.off: set[str] = set()         # adapter names switched off
        self.direct = nl80211.has_net_admin()
        self._nmcli = shutil.which("nmcli")
        self._nm_at: dict[str, float] = {}
        self._procs: list[subprocess.Popen] = []

    def close(self) -> None:
        self.nl.close()

    def _managed(self) -> list[nl80211.Iface]:
        try:
            return [i for i in self.nl.interfaces() if i.iftype == nl80211.IFTYPE_STATION]
        except (NetlinkError, OSError) as e:
            log.debug("nl80211 interface list failed: %s", e)
            return []

    def trigger(self) -> list[nl80211.Iface]:
        """Ask every adapter to scan. Returns the adapters."""
        ifs = self._managed()
        self._procs = [p for p in self._procs if p.poll() is None]
        for it in ifs:
            if it.label in self.off:
                continue
            if self.direct:
                try:
                    self.nl.trigger_scan(it.index)
                    continue
                except NetlinkError as e:
                    if e.errno in (errno.EPERM, errno.EACCES):
                        log.info("no CAP_NET_ADMIN: requesting scans through NetworkManager")
                        self.direct = False
                    else:
                        continue          # EBUSY (scan running), ENETDOWN (interface down)
            self._nm_rescan(it.name)
        return ifs

    def _nm_rescan(self, name: str) -> None:
        if not self._nmcli:
            return
        now = time.monotonic()
        if now - self._nm_at.get(name, 0.0) < NM_RESCAN_EVERY:
            return
        self._nm_at[name] = now
        try:
            self._procs.append(subprocess.Popen([self._nmcli, "device", "wifi", "rescan", "ifname", name],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                stdin=subprocess.DEVNULL))
        except OSError:
            pass

    def read(self, ifs: list[nl80211.Iface]) -> list[dict[str, Any]]:
        """Fresh networks across all adapters; the strongest reading wins when
        two adapters hear the same network."""
        fresh_s = FRESH_DIRECT if self.direct else FRESH_NM
        best: dict[str, dict[str, Any]] = {}
        now_boot, now_mono, now_utc = nl80211.boottime(), time.monotonic(), time.time()
        adapters = []
        for it in ifs:
            if it.label in self.off:
                adapters.append({"name": it.label, "state": "off", "networks": 0})
                continue
            state = "connected" if it.operstate == "up" else ("disconnected" if it.operstate else "?")
            try:
                bsss = self.nl.scan_results(it.index)
            except (NetlinkError, OSError) as e:
                adapters.append({"name": it.label, "state": f"scan failed: {e.strerror or e}", "networks": 0})
                continue
            seen = 0
            for b in bsss:
                if b["boottime_ns"]:
                    age = max(0.0, now_boot - b["boottime_ns"] / 1e9)
                elif b["age_ms"] is not None:
                    age = b["age_ms"] / 1000.0
                else:
                    continue
                if age > fresh_s:
                    continue
                mac = b["bssid"]
                if mac == "00:00:00:00:00:00":
                    continue
                seen += 1
                if mac in best and best[mac]["rssi"] >= b["rssi"]:
                    continue
                best[mac] = {
                    "mac": mac, "ssid": _ssid(b["ies"]), "rssi": b["rssi"], "freq": b["freq"],
                    "channel": channel(b["freq"]), "auth": auth_mode(b["ies"], b["cap"]),
                    "adapter": it.label, "at": now_mono - age, "utc": now_utc - age,
                }
            adapters.append({"name": it.label, "state": state, "networks": seen})
        self.adapters = adapters
        return list(best.values())

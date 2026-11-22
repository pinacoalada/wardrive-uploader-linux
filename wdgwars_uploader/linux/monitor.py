"""Monitor mode through the Linux kernel's own Wi-Fi drivers.

On Windows the app has to carry user-space USB drivers for two chipsets. Linux
already drives almost every USB Wi-Fi adapter, and mac80211 does monitor mode
for all of them, so here a USB adapter that is not connected to a network is
switched to monitor mode over nl80211, its frames are read from an AF_PACKET
socket (radiotap + 802.11) and the channel hops across 2.4 and 5 GHz. Frames go
to the same handler the user-space drivers feed, so access points, handshakes
and PMKIDs are handled exactly as on Windows.

Needs CAP_NET_ADMIN and CAP_NET_RAW (``sudo ./install.sh --monitor`` grants them
to the app's own Python). The adapter is handed back to NetworkManager in
managed mode when it is switched off, unplugged or the app quits. Built-in
(PCIe/SDIO) cards and any adapter carrying a connection are never touched.
"""

from __future__ import annotations

import errno
import json
import logging
import shutil
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Callable

from ..config import config_dir
from . import nl80211
from .netlink import NetlinkError, Rtnl

log = logging.getLogger("wdgwars.monitor.linux")

ETH_P_ALL = 0x0003
HOP = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
       36, 40, 44, 48, 52, 56, 60, 64,
       100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
       149, 153, 157, 161, 165)
DWELL_S = 0.125

# radiotap field (bit) -> (alignment, size), for the fields before the signal
_RT_FIELDS = {0: (8, 8), 1: (1, 1), 2: (1, 1), 3: (2, 4), 4: (1, 2), 5: (1, 1)}
_RT_FLAG_FCS, _RT_FLAG_BADFCS = 0x10, 0x40


def parse_radiotap(pkt: bytes) -> tuple[bytes, int | None, int | None] | None:
    """``(802.11 frame, signal dBm, frequency MHz)``, or None for a bad frame."""
    if len(pkt) < 8 or pkt[0] != 0:
        return None
    rt_len = struct.unpack_from("<H", pkt, 2)[0]
    if rt_len < 8 or rt_len > len(pkt):
        return None
    present = struct.unpack_from("<I", pkt, 4)[0]
    off = 8
    word = present
    while word & 0x80000000:               # more presence words follow
        if off + 4 > rt_len:
            return None
        word = struct.unpack_from("<I", pkt, off)[0]
        off += 4
    flags, signal, freq = 0, None, None
    for bit in range(6):
        if not present & (1 << bit):
            continue
        align, size = _RT_FIELDS[bit]
        off = (off + align - 1) & ~(align - 1)
        if off + size > rt_len:
            return None
        if bit == 1:
            flags = pkt[off]
        elif bit == 3:
            freq = struct.unpack_from("<H", pkt, off)[0] or None
        elif bit == 5:
            signal = struct.unpack_from("<b", pkt, off)[0]
        off += size
    if flags & _RT_FLAG_BADFCS:
        return None
    frame = pkt[rt_len:]
    if flags & _RT_FLAG_FCS:
        frame = frame[:-4]
    if len(frame) < 10:
        return None
    return frame, signal, freq


def _nmcli_managed(name: str, managed: bool) -> None:
    exe = shutil.which("nmcli")
    if not exe:
        return
    try:
        subprocess.run([exe, "device", "set", name, "managed", "yes" if managed else "no"],
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


class _Owned:
    """Interfaces this app put in monitor mode, so a crash never strands one."""

    @staticmethod
    def _path():
        return config_dir() / "monitor_ifaces.json"

    @classmethod
    def load(cls) -> set[str]:
        try:
            return set(json.loads(cls._path().read_text()))
        except (OSError, ValueError, TypeError):
            return set()

    @classmethod
    def save(cls, names: set[str]) -> None:
        try:
            cls._path().write_text(json.dumps(sorted(names)))
        except OSError:
            pass


class _Mon:
    def __init__(self, key: str, iface: nl80211.Iface) -> None:
        self.key = key
        self.ifname = iface.name
        self.ifindex = iface.index
        self.name = iface.product or f"{iface.name} ({iface.driver or 'USB'})"
        self.driver = iface.driver
        self.state, self.detail = "starting", ""
        self.channel = 0
        self.frames = 0
        self.stop_evt = threading.Event()
        self.thread: threading.Thread | None = None
        self.tried = time.monotonic()

    def status(self) -> dict[str, Any]:
        return {"id": f"mon:{self.key}", "name": f"{self.name} · {self.ifname}", "state": self.state,
                "detail": self.detail, "channel": self.channel, "frames": self.frames, "monitor": True}


class NativeMonitor:
    def __init__(self, on_frame: Callable[[bytes, int, int, str], None],
                 off: Callable[[], set[str]] | None = None) -> None:
        self.on_frame = on_frame
        self._off = off or (lambda: set())
        self._lock = threading.Lock()
        self.adapters: dict[str, _Mon] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._owned = _Owned.load()
        self.error = ""

    @property
    def off(self) -> set[str]:
        """USB port keys switched off on the dashboard (read live from the owner)."""
        return self._off()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="monitor-native", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 4.0) -> None:
        self._stop.set()
        with self._lock:
            mons = list(self.adapters.values())
        for m in mons:
            m.stop_evt.set()
        end = time.monotonic() + wait
        for m in mons:
            if m.thread and m.thread.is_alive():
                m.thread.join(max(0.0, end - time.monotonic()))

    def usb_keys(self) -> set[str]:
        """USB ports whose adapter a kernel driver owns (the libusb path leaves them alone)."""
        with self._lock:
            return set(self.adapters)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [m.status() for m in self.adapters.values()]

    # -- discovery -----------------------------------------------------------
    def _watch(self) -> None:
        try:
            nl = nl80211.Nl80211()
        except Exception as e:  # noqa: BLE001
            self.error = f"nl80211 unavailable: {e}"
            return
        try:
            while not self._stop.is_set():
                try:
                    self._scan(nl)
                except Exception as e:  # noqa: BLE001
                    log.debug("monitor scan failed", exc_info=True)
                    self.error = f"interface scan failed: {e}"
                self._stop.wait(4)
        finally:
            nl.close()

    def _scan(self, nl: nl80211.Nl80211) -> None:
        present = set()
        for it in nl.interfaces():
            key = it.usb_key
            if not key:
                continue                      # built-in cards stay with NetworkManager
            present.add(key)
            with self._lock:
                m = self.adapters.get(key)
                if m and m.thread and m.thread.is_alive():
                    if key in self.off:
                        m.stop_evt.set()
                    continue
                if key in self.off:
                    m = m or _Mon(key, it)
                    m.state, m.detail = "off", ""
                    self.adapters[key] = m
                    continue
                if it.iftype == nl80211.IFTYPE_MONITOR and it.name not in self._owned:
                    m = _Mon(key, it)
                    m.state, m.detail = "in use", "another program has it in monitor mode"
                    self.adapters[key] = m
                    continue
                if it.iftype == nl80211.IFTYPE_STATION and it.operstate == "up":
                    # Carrying a connection: left alone, it shows up as a scanning Wi-Fi adapter.
                    self.adapters.pop(key, None)
                    continue
                if it.iftype not in (nl80211.IFTYPE_STATION, nl80211.IFTYPE_MONITOR):
                    continue
                if m and m.state in ("needs permission", "no monitor mode") and m.ifname == it.name:
                    continue                  # retried after a replug
                if m and m.state == "stopped" and time.monotonic() - m.tried < 30:
                    continue
                if not nl80211.has_net_admin():
                    m = _Mon(key, it)
                    m.state = "needs permission"
                    m.detail = "run sudo ./install.sh --monitor to allow monitor mode"
                    self.adapters[key] = m
                    continue
                m = _Mon(key, it)
                self.adapters[key] = m
                m.thread = threading.Thread(target=self._run, args=(m,), name=f"monitor-{it.name}", daemon=True)
                m.thread.start()
        with self._lock:
            for key, m in self.adapters.items():
                if key not in present and not (m.thread and m.thread.is_alive()):
                    m.state, m.detail = "unplugged", ""

    # -- one adapter ---------------------------------------------------------
    def _run(self, m: _Mon) -> None:
        nl = rt = sock = None
        switched = False
        try:
            nl, rt = nl80211.Nl80211(), Rtnl()
            _nmcli_managed(m.ifname, False)
            m.state = "starting radio"
            try:
                rt.set_up(m.ifindex, False)
                nl.set_type(m.ifindex, nl80211.IFTYPE_MONITOR)
                switched = True
                self._owned.add(m.ifname)
                _Owned.save(self._owned)
                rt.set_up(m.ifindex, True)
            except NetlinkError as e:
                if e.errno in (errno.EOPNOTSUPP, errno.EINVAL):
                    m.state, m.detail = "no monitor mode", f"the {m.driver or 'kernel'} driver cannot do monitor mode"
                elif e.errno in (errno.EPERM, errno.EACCES):
                    m.state, m.detail = "needs permission", "run sudo ./install.sh --monitor to allow monitor mode"
                else:
                    m.state, m.detail = "stopped", f"could not switch to monitor mode: {e.strerror}"
                return
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
            sock.bind((m.ifname, ETH_P_ALL))
            sock.settimeout(0.05)
            hop = self._usable_channels(nl, m)
            if not hop:
                m.state, m.detail = "stopped", "the driver accepted no channel"
                return
            m.state, m.detail = "live", f"{len(hop)} channels"
            log.info("monitor mode on %s (%s), hopping %d channels", m.ifname, m.name, len(hop))
            self._capture(nl, sock, m, hop)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.ENXIO, errno.ENETDOWN):
                m.state, m.detail = "unplugged", ""
            else:
                m.state, m.detail = "stopped", str(e)[:140]
            log.info("monitor adapter %s stopped: %s", m.ifname, e)
        except Exception as e:  # noqa: BLE001
            m.state, m.detail = "stopped", str(e)[:140]
            log.exception("monitor adapter %s failed", m.ifname)
        finally:
            if sock is not None:
                sock.close()
            if switched:
                self._restore(nl, rt, m)
            for h in (nl, rt):
                if h is not None:
                    h.close()
            if m.state == "live":
                m.state, m.detail = ("off", "") if m.key in self.off else ("stopped", "")

    def _usable_channels(self, nl: nl80211.Nl80211, m: _Mon) -> list[int]:
        ok = []
        for ch in HOP:
            if m.stop_evt.is_set():
                break
            try:
                nl.set_freq(m.ifindex, nl80211.channel_to_freq(ch))
                ok.append(ch)
            except NetlinkError:
                pass                          # outside the regulatory domain or the radio's bands
        return ok

    def _capture(self, nl: nl80211.Nl80211, sock: socket.socket, m: _Mon, hop: list[int]) -> None:
        idx = 0
        nl.set_freq(m.ifindex, nl80211.channel_to_freq(hop[0]))
        m.channel = hop[0]
        hop_at = time.monotonic()
        while not m.stop_evt.is_set() and not self._stop.is_set():
            now = time.monotonic()
            if len(hop) > 1 and now - hop_at >= DWELL_S:
                idx = (idx + 1) % len(hop)
                try:
                    nl.set_freq(m.ifindex, nl80211.channel_to_freq(hop[idx]))
                    m.channel = hop[idx]
                except NetlinkError as e:
                    if e.errno in (errno.ENODEV, errno.ENETDOWN):
                        raise
                hop_at = now
            try:
                pkt = sock.recv(65535)
            except socket.timeout:
                continue
            p = parse_radiotap(pkt)
            if p is None:
                continue
            frame, sig, freq = p
            ch = nl80211.freq_to_channel(freq) if freq else m.channel
            m.frames += 1
            try:
                self.on_frame(frame, sig if sig is not None else -100, ch or m.channel, m.name)
            except Exception:  # noqa: BLE001
                log.debug("frame handler failed", exc_info=True)

    def _restore(self, nl, rt, m: _Mon) -> None:
        """Back to managed mode and to NetworkManager, whatever stopped the capture."""
        try:
            if rt is not None:
                rt.set_up(m.ifindex, False)
            if nl is not None:
                nl.set_type(m.ifindex, nl80211.IFTYPE_STATION)
            if rt is not None:
                rt.set_up(m.ifindex, True)
        except (NetlinkError, OSError) as e:
            log.debug("restore %s: %s", m.ifname, e)
        _nmcli_managed(m.ifname, True)
        self._owned.discard(m.ifname)
        _Owned.save(self._owned)

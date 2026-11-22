"""nl80211 over :mod:`.netlink`: Wi-Fi interfaces, scan results, monitor mode.

Reading interfaces and scan results needs no privileges. Triggering a scan,
changing an interface's type and tuning a channel need CAP_NET_ADMIN, which the
install script can grant to the app's own Python (``install.sh --monitor``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import netlink as nl

# commands
CMD_GET_INTERFACE = 5
CMD_SET_INTERFACE = 6
CMD_GET_SCAN = 32
CMD_TRIGGER_SCAN = 33
CMD_SET_CHANNEL = 65

# attributes
ATTR_WIPHY = 1
ATTR_IFINDEX = 3
ATTR_IFNAME = 4
ATTR_IFTYPE = 5
ATTR_MAC = 6
ATTR_WIPHY_FREQ = 38
ATTR_WIPHY_CHANNEL_TYPE = 39
ATTR_BSS = 47

# BSS attributes
BSS_BSSID = 1
BSS_FREQUENCY = 2
BSS_CAPABILITY = 5
BSS_INFORMATION_ELEMENTS = 6
BSS_SIGNAL_MBM = 7
BSS_SIGNAL_UNSPEC = 8
BSS_STATUS = 9
BSS_SEEN_MS_AGO = 10
BSS_BEACON_IES = 11
BSS_LAST_SEEN_BOOTTIME = 15

IFTYPE_STATION = 2
IFTYPE_MONITOR = 6
IFTYPES = {1: "ad hoc", 2: "managed", 3: "AP", 4: "AP VLAN", 5: "WDS", 6: "monitor",
           7: "mesh point", 8: "P2P client", 9: "P2P GO", 10: "P2P device", 11: "OCB", 12: "NAN"}
CHAN_NO_HT = 0


@dataclass
class Iface:
    index: int
    name: str
    iftype: int
    wiphy: int
    mac: str

    @property
    def sys(self) -> Path:
        return Path("/sys/class/net") / self.name

    @property
    def usb_key(self) -> str:
        """USB port path (``1-2.3``, the same key libusb enumeration builds), or ""."""
        try:
            real = os.path.realpath(self.sys / "device")
        except OSError:
            return ""
        if "/usb" not in real:
            return ""
        intf = os.path.basename(real)              # "1-2.3:1.0"
        return intf.split(":", 1)[0]

    @property
    def driver(self) -> str:
        try:
            return os.path.basename(os.path.realpath(self.sys / "device" / "driver"))
        except OSError:
            return ""

    @property
    def product(self) -> str:
        """The USB adapter's own product string, or a generic name for the bus."""
        dev = self.sys / "device"
        for p in (dev / ".." / "product", dev / "product"):
            try:
                s = p.read_text().strip()
                if s:
                    return s
            except OSError:
                pass
        return ""

    @property
    def operstate(self) -> str:
        try:
            return (self.sys / "operstate").read_text().strip()
        except OSError:
            return ""

    @property
    def label(self) -> str:
        bits = [b for b in (self.product, self.driver) if b]
        return f"{self.name} ({', '.join(bits)})" if bits else self.name


class Nl80211:
    def __init__(self) -> None:
        self.g = nl.Genl("nl80211")

    def close(self) -> None:
        self.g.close()

    def interfaces(self) -> list[Iface]:
        out = []
        for a in self.g.call(CMD_GET_INTERFACE, dump=True):
            if ATTR_IFINDEX not in a:
                continue
            mac = a.get(ATTR_MAC) or b""
            out.append(Iface(nl.u32(a[ATTR_IFINDEX]), nl.cstr(a.get(ATTR_IFNAME)),
                             nl.u32(a.get(ATTR_IFTYPE)), nl.u32(a.get(ATTR_WIPHY)),
                             mac.hex(":") if len(mac) == 6 else ""))
        return out

    def trigger_scan(self, ifindex: int) -> None:
        """Raises NetlinkError: EPERM without CAP_NET_ADMIN, EBUSY while a scan runs."""
        self.g.call(CMD_TRIGGER_SCAN, nl.attr_u32(ATTR_IFINDEX, ifindex))

    def scan_results(self, ifindex: int) -> list[dict]:
        """The kernel's BSS cache for one interface."""
        out = []
        for a in self.g.call(CMD_GET_SCAN, nl.attr_u32(ATTR_IFINDEX, ifindex), dump=True):
            raw = a.get(ATTR_BSS)
            if not raw:
                continue
            b = nl.parse_attrs(raw)
            bssid = b.get(BSS_BSSID) or b""
            if len(bssid) != 6:
                continue
            if BSS_SIGNAL_MBM in b:
                rssi = int(round(nl.s32(b[BSS_SIGNAL_MBM]) / 100))
            elif BSS_SIGNAL_UNSPEC in b:          # 0..100 quality: map onto dBm roughly
                rssi = int(nl.u8(b[BSS_SIGNAL_UNSPEC]) / 2 - 100)
            else:
                rssi = -100
            ies = b.get(BSS_INFORMATION_ELEMENTS) or b.get(BSS_BEACON_IES) or b""
            out.append({
                "bssid": bssid.hex(":"),
                "freq": nl.u32(b.get(BSS_FREQUENCY)),
                "cap": nl.u16(b.get(BSS_CAPABILITY)),
                "rssi": rssi,
                "ies": ies,
                "age_ms": nl.u32(b.get(BSS_SEEN_MS_AGO)) if BSS_SEEN_MS_AGO in b else None,
                "boottime_ns": nl.u64(b.get(BSS_LAST_SEEN_BOOTTIME)) if BSS_LAST_SEEN_BOOTTIME in b else None,
            })
        return out

    def set_type(self, ifindex: int, iftype: int) -> None:
        self.g.call(CMD_SET_INTERFACE, nl.attr_u32(ATTR_IFINDEX, ifindex) + nl.attr_u32(ATTR_IFTYPE, iftype))

    def set_freq(self, ifindex: int, mhz: int) -> None:
        self.g.call(CMD_SET_CHANNEL, nl.attr_u32(ATTR_IFINDEX, ifindex) + nl.attr_u32(ATTR_WIPHY_FREQ, mhz)
                    + nl.attr_u32(ATTR_WIPHY_CHANNEL_TYPE, CHAN_NO_HT))


def boottime() -> float:
    """CLOCK_BOOTTIME in seconds (what LAST_SEEN_BOOTTIME is measured against)."""
    import time
    return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", 7))


def has_net_admin() -> bool:
    """True if this process holds CAP_NET_ADMIN (12) and CAP_NET_RAW (13)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                eff = int(line.split()[1], 16)
                return bool(eff & (1 << 12)) and bool(eff & (1 << 13))
    except (OSError, ValueError):
        pass
    return os.geteuid() == 0


def freq_to_channel(mhz: int) -> int | None:
    if mhz == 2484:
        return 14
    if 2412 <= mhz <= 2472:
        return (mhz - 2407) // 5
    if 5000 < mhz < 5900:
        return (mhz - 5000) // 5
    if 5950 < mhz <= 7125:
        return (mhz - 5950) // 5
    return None


def channel_to_freq(ch: int) -> int:
    if ch == 14:
        return 2484
    return 2407 + ch * 5 if ch < 14 else 5000 + ch * 5


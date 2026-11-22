"""Process, desktop and device helpers that replace tasklist/taskkill, os.startfile,
GetUserDefaultGeoName and Windows' exclusive COM-port opens."""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

log = logging.getLogger("wdgwars.linux")


# -- processes ---------------------------------------------------------------------
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is gone for our purposes.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def kill(pid: int, wait: float = 3.0) -> None:
    """SIGTERM, then SIGKILL if it is still there after ``wait`` seconds."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as e:
        log.warning("could not stop pid %s: %s", pid, e)
        return
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def app_instances() -> list[tuple[int, list[str]]]:
    """Other running copies of this app (not watchdogs, not this process)."""
    me = os.getpid()
    uid = os.getuid()
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        pid = int(d.name)
        if pid == me:
            continue
        # The real uid from status: /proc/<pid> itself is root-owned for a process
        # running with file capabilities (install.sh --monitor), which is not dumpable.
        try:
            line = next(x for x in (d / "status").read_text().splitlines() if x.startswith("Uid:"))
            if int(line.split()[1]) != uid:
                continue
        except (OSError, StopIteration, ValueError, IndexError):
            continue
        args = cmdline(pid)
        joined = " ".join(args)
        if "--watchdog" in args or not args:
            continue
        if "wdgwars_uploader" in joined or "run_app.py" in joined or "wardrive-uploader" in os.path.basename(args[0]):
            if any(a in args for a in ("--once", "--detect", "--check", "--window")):
                continue
            out.append((pid, args))
    return out


def kill_stale_instances() -> None:
    for pid, _ in app_instances():
        log.warning("clearing stale app instance pid %s", pid)
        kill(pid)


# -- desktop -----------------------------------------------------------------------
def open_path(path: str) -> None:
    subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def country() -> str | None:
    """Two-letter country from the locale (LC_ALL / LC_ADDRESS / LANG), e.g. en_GB -> GB."""
    for var in ("LC_ALL", "LC_ADDRESS", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var) or ""
        loc = v.split(".", 1)[0].split("@", 1)[0]
        if "_" in loc:
            cc = loc.split("_", 1)[1]
            if len(cc) == 2 and cc.isalpha():
                return cc.upper()
    try:
        for line in Path("/etc/default/locale").read_text().splitlines():
            if line.startswith("LANG="):
                loc = line.split("=", 1)[1].strip().strip('"').split(".", 1)[0]
                if "_" in loc:
                    return loc.split("_", 1)[1][:2].upper()
    except OSError:
        pass
    return None


def lan_addresses() -> list[str]:
    """This machine's LAN IPv4 addresses. On Ubuntu the hostname resolves to
    127.0.1.1, so the interface list is read instead."""
    out: list[str] = []
    try:
        import json
        res = subprocess.run(["ip", "-j", "-4", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5)
        for link in json.loads(res.stdout or "[]"):
            if link.get("ifname", "").startswith(("docker", "br-", "veth", "virbr", "tailscale", "zt")):
                continue
            for a in link.get("addr_info") or []:
                ip = a.get("local")
                if ip and not ip.startswith(("127.", "169.254.")) and ip not in out:
                    out.append(ip)
    except Exception:  # noqa: BLE001
        pass
    if not out:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))       # no packet is sent
                ip = s.getsockname()[0]
                if not ip.startswith("127."):
                    out.append(ip)
        except OSError:
            pass
    return out


# -- USB / serial ------------------------------------------------------------------
def usb_kernel_driver(key: str) -> str:
    """The kernel driver bound to interface 0 of the USB device at port ``key`` (``1-2.3``)."""
    try:
        return os.path.basename(os.path.realpath(f"/sys/bus/usb/devices/{key}:1.0/driver"))
    except OSError:
        return ""


def usb_devices() -> list[dict]:
    """Every USB device from sysfs: vid, pid, key, product, devnode."""
    out = []
    for d in Path("/sys/bus/usb/devices").glob("*"):
        if ":" in d.name:
            continue
        try:
            vid = int((d / "idVendor").read_text().strip(), 16)
            pid = int((d / "idProduct").read_text().strip(), 16)
        except (OSError, ValueError):
            continue
        def rd(n: str) -> str:
            try:
                return (d / n).read_text().strip()
            except OSError:
                return ""
        bus, dev = rd("busnum"), rd("devnum")
        node = f"/dev/bus/usb/{int(bus):03d}/{int(dev):03d}" if bus.isdigit() and dev.isdigit() else ""
        out.append({"vid": vid, "pid": pid, "key": d.name, "product": rd("product"),
                    "manufacturer": rd("manufacturer"), "node": node})
    return out


def usb_accessible(node: str) -> bool:
    return bool(node) and os.access(node, os.R_OK | os.W_OK)


_patched = False


def exclusive_serial() -> None:
    """Open serial ports exclusively by default, as Windows does.

    On Windows a COM port has one owner, and the app relies on that: a port that
    is busy fails to open instead of two readers silently splitting its bytes.
    Linux allows shared opens, so pyserial's flock-based exclusive mode is made
    the default (an explicit ``exclusive=False`` still wins).
    """
    global _patched
    if _patched:
        return
    try:
        import serial.serialposix as sp
    except Exception:  # noqa: BLE001 - pyserial missing; nothing to patch
        return
    orig = sp.Serial.__init__

    def __init__(self, *args, **kwargs):  # noqa: N807
        if kwargs.get("exclusive") is None:
            kwargs["exclusive"] = True
        orig(self, *args, **kwargs)

    sp.Serial.__init__ = __init__
    _patched = True

"""Linux prerequisite checks for the dashboard (the Zadig/WinUSB checks' counterparts):
a packaged ADS-B decoder, USB and serial permissions, and the two Ubuntu services
that are known to grab hobbyist USB-serial boards (brltty and ModemManager)."""

from __future__ import annotations

import grp
import os
import shutil
from pathlib import Path

INSTALL_HINT = "Run ./install.sh from the app folder (it uses sudo for the system parts)."


def _check(name: str, ok: bool, status: str, detail: str = "", fix: str = "", url: str = "",
           action: str = "") -> dict:
    return {"name": name, "ok": ok, "status": status, "detail": detail, "fix": fix, "url": url,
            "action": action}


def decoder(cfg: dict) -> dict:
    from . import adsb as lx
    name = "ADS-B decoder"
    sysj = lx.system_json()
    if sysj:
        return _check(name, True, f"{Path(sysj).parent.name} service running", detail=sysj)
    exe = lx.find_decoder(cfg.get("dump1090_exe", ""))
    if exe:
        err = lx.decoder_error()
        if err:
            return _check(name, False, f"{os.path.basename(exe)} stopped", detail=err,
                          fix="Check the SDR is plugged in and that no other program (SDR++, GQRX, "
                              "rtl_tcp, a decoder service) has it open. It is started again on its own.")
        st = lx.status
        if st.get("mode") == "app":
            return _check(name, True, f"{os.path.basename(exe)} running", detail=exe)
        return _check(name, True, "installed", detail=exe)
    return _check(name, False, "not installed",
                  fix="sudo apt install dump1090-fa (or readsb). " + INSTALL_HINT)


def rtlsdr(present: bool) -> dict:
    name = "RTL-SDR access"
    if not present:
        return _check(name, True, "n/a (no RTL-SDR)")
    from . import adsb as lx
    from .system import usb_devices, usb_accessible, usb_kernel_driver
    from ..detect import RTLSDR_IDS
    dev = next((d for d in usb_devices() if (d["vid"], d["pid"]) in RTLSDR_IDS), None)
    if dev is None:
        return _check(name, True, "n/a (no RTL-SDR)")
    if lx.system_json():
        return _check(name, True, "in use by the decoder service")
    if not usb_accessible(dev["node"]):
        return _check(name, False, "no permission to open it",
                      fix="Install the udev rules, then replug the dongle. " + INSTALL_HINT)
    drv = usb_kernel_driver(dev["key"])
    if drv.startswith("dvb_usb"):
        return _check(name, True, f"ready (TV driver {drv} loaded; the decoder detaches it)",
                      fix="If no aircraft appear, blacklist dvb_usb_rtl28xxu. " + INSTALL_HINT)
    return _check(name, True, "ready")


def usb_radio(name: str, vid: int, pids: tuple[int, ...]) -> dict | None:
    from .system import usb_devices, usb_accessible
    dev = next((d for d in usb_devices() if d["vid"] == vid and d["pid"] in pids), None)
    if dev is None or usb_accessible(dev["node"]):
        return None
    return _check(name, False, "no permission to open it",
                  fix="Install the udev rules, then replug it. " + INSTALL_HINT)


def serial_access(ports: list) -> dict | None:
    """A USB-serial board this user cannot open (not in the dialout group yet)."""
    blocked = [p.port for p in ports if p.port.startswith("/dev/") and os.path.exists(p.port)
               and not os.access(p.port, os.R_OK | os.W_OK)]
    if not blocked:
        return None
    try:
        member = grp.getgrnam("dialout").gr_gid in os.getgroups()
    except KeyError:
        member = False
    fix = ("You were added to the dialout group but this session predates it: log out and back in."
           if member else "sudo usermod -aG dialout $USER, then log out and back in. " + INSTALL_HINT)
    return _check("Serial ports", False, "no permission: " + ", ".join(blocked), fix=fix)


def brltty() -> dict | None:
    """Ubuntu's braille daemon claims CH340/CP210x serial chips (common on ESP32 and
    mesh boards) and the port vanishes a second after it appears."""
    rule = Path("/usr/lib/udev/rules.d/85-brltty.rules")
    if not rule.exists() or Path("/etc/udev/rules.d/85-brltty.rules").exists() or not shutil.which("brltty"):
        return None
    return _check("brltty", False, "installed: it can take over USB-serial boards",
                  detail="Boards on CH340/CH341 and some CP210x chips disconnect right after plugging in.",
                  fix="If you don't use a braille display: " + INSTALL_HINT + " (it masks brltty's USB claim)")

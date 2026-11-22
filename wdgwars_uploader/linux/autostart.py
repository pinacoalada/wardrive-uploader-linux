"""Start at login on Linux (the Windows Run key + Startup shortcut).

* In a desktop session: an XDG autostart entry
  (``~/.config/autostart/wardrive-uploader.desktop``), which every desktop
  (GNOME, KDE, Xfce, ...) runs at login with the session's display, so the tray
  icon appears. Crash restarts come from the app's own watchdog, as on Windows.
* Without a desktop (a server, a Pi in a car): a systemd user service with
  ``Restart=on-failure``, started at boot when lingering is enabled
  (``loginctl enable-linger``; install.sh offers it). systemd restarts the app
  itself there, so the watchdog stands down (``WARDRIVE_SUPERVISED``).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("wdgwars.autostart")

UNIT = "wardrive-uploader.service"
OLD_UNIT = "wdgwars-uploader.service"      # the earlier Debian port's unit
DESKTOP_ID = "wardrive-uploader.desktop"


def _xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))


def _desktop_file() -> Path:
    return _xdg_config() / "autostart" / DESKTOP_ID


def _unit_file(name: str = UNIT) -> Path:
    return _xdg_config() / "systemd" / "user" / name


def _script() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "run_app.py"


def _icon() -> str:
    return str(Path(__file__).resolve().parent.parent / "webui" / "static" / "tpp.png")


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--boot"]
    return [sys.executable, str(_script()), "--boot"]


def has_desktop() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
    except Exception as e:  # noqa: BLE001 - no systemd user manager
        log.debug("systemctl --user %s: %s", " ".join(args), e)
        return None


def _desktop_text() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Wardrive Uploader\n"
        "Comment=Wardriving collector and uploader (tray)\n"
        f"Exec={shlex.join(_command())}\n"
        f"Icon={_icon()}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=5\n"
    )


def _unit_text() -> str:
    cmd = _command()
    return (
        "[Unit]\n"
        "Description=Wardrive Uploader\n"
        "Wants=network-online.target\n"
        "After=network-online.target bluetooth.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_script().parent}\n"
        f"ExecStart={shlex.join(cmd)}\n"
        "Environment=WARDRIVE_SUPERVISED=1\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def is_enabled() -> bool:
    if _desktop_file().exists():
        return True
    r = _systemctl("is-enabled", "--quiet", UNIT)
    return bool(r and r.returncode == 0)


def enable() -> bool:
    try:
        # Keep whichever mechanism started this copy; a new choice follows the session.
        use_unit = supervised() or (not has_desktop() and not _desktop_file().exists())
        if not use_unit:
            f = _desktop_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(_desktop_text(), encoding="utf-8")
            _disable_unit(UNIT)
            log.info("registered auto-start (%s)", f)
            return True
        try:
            _desktop_file().unlink()          # one login trigger, never two
        except OSError:
            pass
        u = _unit_file()
        u.parent.mkdir(parents=True, exist_ok=True)
        u.write_text(_unit_text(), encoding="utf-8")
        _systemctl("daemon-reload")
        r = _systemctl("enable", UNIT)
        if r and r.returncode == 0:
            log.info("registered auto-start (systemd user unit %s)", UNIT)
            return True
        log.warning("systemd enable failed: %s", (r.stderr if r else "no systemd user manager").strip())
    except OSError as e:
        log.warning("auto-start enable failed: %s", e)
    return False


def _disable_unit(name: str) -> None:
    f = _unit_file(name)
    if not f.exists():
        return
    _systemctl("disable", name)
    try:
        f.unlink()
    except OSError:
        pass
    _systemctl("daemon-reload")


def disable() -> None:
    try:
        _desktop_file().unlink()
    except OSError:
        pass
    _disable_unit(UNIT)
    log.info("removed auto-start")


def remove_old_name() -> None:
    """The earlier Debian port registered its own user unit; it must not start too."""
    _disable_unit(OLD_UNIT)


def supervised() -> bool:
    """True when systemd restarts the app itself (the watchdog then stays out)."""
    return os.environ.get("WARDRIVE_SUPERVISED") == "1"

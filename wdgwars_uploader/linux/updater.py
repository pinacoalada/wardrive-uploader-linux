"""Updates on Linux: the app runs from a git checkout, so an update is a fast-forward
``git pull`` of the branch it tracks, a dependency refresh and a restart.

Same surface as the Windows MSI updater (``status``, ``check()``, ``install()``),
so the dashboard and tray work unchanged. A checkout with local commits or
edits is never touched: the check reports it and the update is left to you.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import __version__

log = logging.getLogger("wdgwars.updater")

ROOT = Path(__file__).resolve().parent.parent.parent


def _git(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)


def _web_page(remote: str) -> str:
    m = re.match(r"(?:git@github\.com:|https://github\.com/)(.+?)(?:\.git)?/?$", remote.strip())
    return f"https://github.com/{m.group(1)}/commits" if m else ""


class Updater:
    def __init__(self, on_quit: Callable[[], None], on_event: Callable[[str], None]) -> None:
        self.on_quit, self.on_event = on_quit, on_event
        self.is_git = (ROOT / ".git").exists() and shutil.which("git") is not None
        self.status: dict[str, Any] = {
            "current": __version__, "latest": "", "available": False, "checked": 0.0,
            "phase": "idle", "progress": 0, "error": "", "notes": "", "page": "",
            "can_install": self.is_git, "method": "git",
        }
        if not self.is_git:
            self.status["error"] = "not a git checkout: update by reinstalling"

    def check(self) -> dict[str, Any]:
        if not self.is_git or self.status["phase"] == "installing":
            return self.status
        self.status.update(phase="checking", error="")
        try:
            f = _git("fetch", "--quiet")
            if f.returncode:
                raise RuntimeError((f.stderr or "git fetch failed").strip().splitlines()[-1])
            behind = int((_git("rev-list", "--count", "HEAD..@{u}").stdout or "0").strip() or 0)
            ahead = int((_git("rev-list", "--count", "@{u}..HEAD").stdout or "0").strip() or 0)
            dirty = bool((_git("status", "--porcelain", "--untracked-files=no").stdout or "").strip())
            remote_init = _git("show", "@{u}:wdgwars_uploader/__init__.py").stdout or ""
            m = re.search(r'__version__\s*=\s*"([^"]+)"', remote_init)
            short = (_git("rev-parse", "--short", "@{u}").stdout or "").strip()
            latest = (m.group(1) if m else __version__) + (f" ({short})" if short else "")
            notes = (_git("log", "--format=• %s", "-n", "12", "HEAD..@{u}").stdout or "").strip()
            remote = (_git("remote", "get-url", "origin").stdout or "").strip()
            can = behind > 0 and ahead == 0 and not dirty
            err = ""
            if behind and (ahead or dirty):
                err = "local changes in the app folder: update with git yourself"
            self.status.update(latest=latest, available=behind > 0, can_install=can, notes=notes[:600],
                               page=_web_page(remote), checked=time.time(), phase="idle", error=err)
        except Exception as e:  # noqa: BLE001 - offline, no upstream branch, git missing
            self.status.update(phase="idle", error=f"update check failed: {e}", checked=time.time())
        return self.status

    def install(self) -> dict[str, Any]:
        if not self.status.get("available") or not self.status.get("can_install") \
                or self.status["phase"] == "installing":
            return self.status
        self.status.update(phase="installing", progress=0, error="")
        threading.Thread(target=self._install, name="updater", daemon=True).start()
        return self.status

    def _install(self) -> None:
        try:
            old_reqs = _git("rev-parse", "HEAD:requirements.txt").stdout.strip()
            r = _git("pull", "--ff-only", "--quiet", timeout=180)
            if r.returncode:
                raise RuntimeError((r.stderr or "git pull failed").strip().splitlines()[-1])
            self.status["progress"] = 60
            if _git("rev-parse", "HEAD:requirements.txt").stdout.strip() != old_reqs:
                p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r",
                                    str(ROOT / "requirements.txt")], capture_output=True, text=True, timeout=600)
                if p.returncode:
                    log.warning("dependency refresh failed: %s", (p.stderr or "").strip()[-300:])
            self.status["progress"] = 100
            self.on_event("Updated; restarting")
            log.info("updated to %s; restarting", self.status.get("latest"))
            self._restart()
        except Exception as e:  # noqa: BLE001
            self.status.update(phase="idle", error=f"update failed: {e}")
            self.on_event(f"Update failed: {e}")
            log.warning("update failed: %s", e)

    def _restart(self) -> None:
        from .autostart import supervised
        if supervised():
            # systemd restarts a unit that exits with a failure code.
            self.on_quit()
            time.sleep(1)
            import os
            os._exit(75)
        cmd = [sys.executable, str(ROOT / "run_app.py"), "--boot"]
        subprocess.Popen(["sh", "-c", 'sleep 4; exec "$@"', "sh", *cmd], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.on_quit()

"""ADS-B decoding on Linux with the distro's own decoder.

The Windows build downloads gvanem's dump1090.exe. On Ubuntu the decoder comes
from the package manager (``dump1090-fa``, ``readsb`` or ``dump1090-mutability``)
and is used one of two ways, chosen on every poll:

1. **A decoder service is already running** (the package's systemd service owns
   the RTL-SDR and writes ``aircraft.json`` under ``/run``): its JSON is read and
   nothing is launched, so the app never fights the service for the dongle.
2. **Otherwise** the app runs the decoder itself, writing JSON into its own data
   folder. An RTL-SDR is opened by the decoder directly. A HackRF (or a PortaPack
   switched into HackRF mode) runs through the same user-space HackRF driver as
   on Windows; its samples are relayed from that driver's rtl_tcp port into the
   decoder's stdin as raw 8-bit IQ, which every Linux decoder can read
   (``--ifile - --iformat UC8``) — so the HackRF Pro fixes carry over unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..config import config_dir

log = logging.getLogger("wdgwars.adsb.linux")

# aircraft.json written by packaged decoder services
SYSTEM_JSON = (
    "/run/dump1090-fa/aircraft.json",
    "/run/readsb/aircraft.json",
    "/run/dump1090-mutability/aircraft.json",
    "/run/dump1090/aircraft.json",
    "/run/adsb-feeder-ultrafeeder/readsb/aircraft.json",
)
DECODERS = ("dump1090-fa", "readsb", "dump1090-mutability", "dump1090")
FRESH_S = 30.0
RELAUNCH_BACKOFF = 60.0
HACKRF_RATE = 2_400_000          # the rate every Linux decoder's demodulator expects from a file
ADSB_FREQ = 1_090_000_000

_proc: subprocess.Popen | None = None
_launched: tuple = ()
_last_attempt = 0.0
_lock = threading.Lock()
_relay: threading.Thread | None = None
_relay_stop = threading.Event()
status: dict[str, Any] = {"mode": "", "decoder": "", "json": "", "error": ""}


def json_dir() -> Path:
    d = config_dir() / "adsb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def own_json() -> Path:
    return json_dir() / "aircraft.json"


def _fresh(p: str | Path, max_age: float = FRESH_S) -> bool:
    try:
        return time.time() - os.stat(p).st_mtime < max_age
    except OSError:
        return False


def system_json() -> str:
    """aircraft.json of a decoder service that is running right now, or ""."""
    for p in SYSTEM_JSON:
        if _fresh(p):
            return p
    return ""


def find_decoder(configured: str = "") -> str | None:
    if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
        return configured
    path = os.environ.get("PATH", "") + ":/usr/bin:/usr/local/bin:/usr/sbin:/opt/readsb/bin"
    for name in DECODERS:
        exe = shutil.which(name, path=path)
        if exe:
            return exe
    return None


def flavor(exe: str) -> str:
    """'fa' (dump1090-fa), 'readsb' or 'mut' (dump1090-mutability / older dump1090)."""
    base = os.path.basename(exe).lower()
    if "readsb" in base:
        return "readsb"
    if "dump1090-fa" in base:
        return "fa"
    if "mutability" in base:
        return "mut"
    try:
        out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=5)
        text = (out.stdout or "") + (out.stderr or "")
    except Exception:  # noqa: BLE001
        text = ""
    if "readsb" in text.lower():
        return "readsb"
    return "fa" if "--device-type" in text else "mut"


def build_args(exe: str, source: str, home: tuple[float, float] | None, gain: Any) -> list[str]:
    fl = flavor(exe)
    args = [exe, "--quiet", "--write-json", str(json_dir())]
    if fl in ("fa", "readsb"):
        args += ["--write-json-every", "1"]
    if source == "hackrf":
        if fl in ("fa", "readsb"):
            args += ["--device-type", "ifile"]
        args += ["--ifile", "-", "--iformat", "UC8"]
    elif fl in ("fa", "readsb"):
        args += ["--device-type", "rtlsdr"]
    if home is not None:
        args += ["--lat", f"{home[0]:.6f}", "--lon", f"{home[1]:.6f}"]
    try:
        g = float(gain)
        if 0.0 <= g <= 49.6 and source != "hackrf":
            args += ["--gain", f"{g:g}"]
    except (TypeError, ValueError):
        pass
    return args


def running() -> bool:
    return _proc is not None and _proc.poll() is None


def stop() -> None:
    global _proc, _launched
    _relay_stop.set()
    p, _proc = _proc, None
    _launched = ()
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(5)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    try:
        own_json().unlink()        # never read a dead decoder's last aircraft as live
    except OSError:
        pass


def ensure(cfg: dict[str, Any], source: str, sdr_present: bool | None,
           home: tuple[float, float] | None) -> bool:
    """Make sure aircraft.json is being produced for ``source``. Non-blocking."""
    global _proc, _launched, _last_attempt
    from .. import hackrf
    sysj = system_json()
    if sysj:
        if running():
            stop()
        hackrf.stop_bridge()
        status.update(mode="service", json=sysj, error="", decoder=_service_name(sysj))
        return True
    if not source or (source == "rtlsdr" and sdr_present is False):
        # No radio to decode from (it is launched the moment one is plugged in).
        if running():
            stop()
        hackrf.stop_bridge()
        status.update(mode="", json="", error="")
        return False
    if source == "hackrf":
        hackrf.ensure_bridge()
    else:
        hackrf.stop_bridge()
    exe = find_decoder(cfg.get("dump1090_exe", ""))
    if not exe:
        status.update(mode="", decoder="", json="", error="no ADS-B decoder installed")
        return False
    key = (exe, source, home, cfg.get("dump1090_gain"))
    with _lock:
        if running():
            if key == _launched:
                return True
            stop()
        now = time.time()
        if now - _last_attempt < RELAUNCH_BACKOFF:
            return False
        _last_attempt = now
        args = build_args(exe, source, home, cfg.get("dump1090_gain"))
        try:
            errlog = open(json_dir() / "decoder.log", "wb")
        except OSError:
            errlog = subprocess.DEVNULL
        try:
            log.info("launching ADS-B decoder: %s", " ".join(args))
            _proc = subprocess.Popen(args, stdin=subprocess.PIPE if source == "hackrf" else subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=errlog, start_new_session=True)
        except OSError as e:
            status.update(mode="", error=f"could not start {os.path.basename(exe)}: {e}")
            log.warning("decoder launch failed: %s", e)
            return False
        finally:
            if errlog is not subprocess.DEVNULL:
                errlog.close()
        _launched = key
        status.update(mode="app", decoder=os.path.basename(exe), json=str(own_json()), error="")
        if source == "hackrf":
            _start_relay(_proc)
        return True


def _service_name(path: str) -> str:
    return Path(path).parent.name


def decoder_error() -> str:
    """Last lines of the decoder's stderr, when it exited."""
    if _proc is None or _proc.poll() is None:
        return ""
    try:
        lines = (json_dir() / "decoder.log").read_text(errors="replace").strip().splitlines()
        return lines[-1][:200] if lines else f"exited with code {_proc.returncode}"
    except OSError:
        return f"exited with code {_proc.returncode}"


def collect_path() -> str:
    """The aircraft.json to read: a running service's, else the app's own decoder's."""
    sysj = system_json()
    if sysj:
        return sysj
    own = own_json()
    return str(own) if running() and _fresh(own, 10) else ""


# -- HackRF -> decoder stdin -------------------------------------------------------
def _start_relay(proc: subprocess.Popen) -> None:
    global _relay
    _relay_stop.clear()
    _relay = threading.Thread(target=_relay_loop, args=(proc,), name="hackrf-relay", daemon=True)
    _relay.start()


def _relay_loop(proc: subprocess.Popen) -> None:
    """Connect to the HackRF bridge as its rtl_tcp client and pipe the IQ into the decoder."""
    from .. import hackrf
    while not _relay_stop.is_set() and proc.poll() is None:
        try:
            with socket.create_connection(("127.0.0.1", hackrf.TCP_PORT), timeout=3) as s:
                hdr = b""
                while len(hdr) < 12:
                    chunk = s.recv(12 - len(hdr))
                    if not chunk:
                        raise ConnectionError("bridge closed")
                    hdr += chunk
                s.sendall(struct.pack(">BI", 0x02, HACKRF_RATE) + struct.pack(">BI", 0x01, ADSB_FREQ))
                s.settimeout(1.0)
                log.info("HackRF relay: streaming to the decoder")
                while not _relay_stop.is_set() and proc.poll() is None:
                    try:
                        data = s.recv(262144)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    proc.stdin.write(data)
        except (BrokenPipeError, ValueError):
            break                              # decoder exited
        except OSError as e:
            log.debug("HackRF relay: %s", e)
        _relay_stop.wait(1.0)
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass

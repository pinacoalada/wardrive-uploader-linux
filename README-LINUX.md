# Wardrive Uploader on Linux (Ubuntu)

A native Linux port of [RocketGod's Wardrive Uploader](https://github.com/RocketGod-git/wardrive-uploader).
Same app, same dashboard, same uploads to WiGLE, WDGWars, Wardrift and wpa-sec. Only the
platform layer is different: everything Windows does through Win32, WinRT, PowerShell and the
registry is done here with what Linux already has. All credit for the app goes to RocketGod.

Built and tested on Ubuntu 24.04 (Python 3.12). Any recent Debian-based distro should work.

## Install

```sh
git clone https://github.com/pinacoalada/wardrive-uploader-linux wardrive-uploader
cd wardrive-uploader
./install.sh                # add --monitor for Wi-Fi monitor mode, --headless for a Pi/server
```

Then start **Wardrive Uploader** from the app menu (or `.venv/bin/python-wardrive run_app.py`) and
open <http://127.0.0.1:8787/>. Log out and back in once after the first install so the serial-port
group applies.

What `install.sh` does (re-run it any time to repair or update an install):

| Step | Why |
|---|---|
| apt: Python, PyGObject + AppIndicator, BlueZ, libusb, GeoClue, rtl-sdr, hackrf, a packaged ADS-B decoder (`dump1090-fa`, else `readsb`, else `dump1090-mutability`) | tray icon, Bluetooth, USB radios, location, aircraft |
| `.venv` with `requirements.txt` | the app's Python packages (system site-packages for the tray and D-Bus) |
| `/etc/udev/rules.d/70-wardrive-uploader.rules` | lets you open the RTL-SDR, HackRF and monitor-mode adapters, and keeps ModemManager from probing mesh/ESP32 boards. Generated from the app's own device tables. |
| groups `dialout`, `plugdev` | serial ports and USB radios |
| DVB-T blacklist for the RTL-SDR | the TV driver otherwise claims the dongle |
| brltty (asks first) | Ubuntu's braille daemon steals CH340/CH341 boards; the port vanishes a second after it appears |
| ufw (asks first, only if active) | phone GPS ports 10110/10111 from your LAN |
| `--monitor`: `setcap cap_net_admin,cap_net_raw` on `.venv/bin/python-wardrive` | monitor mode without running as root |
| `--headless`: systemd user service + `loginctl enable-linger` | starts at boot with no desktop |

`./uninstall.sh` removes all of it (`--purge` also deletes your data).

## What maps to what

| Windows | Linux |
|---|---|
| Native Wifi API scan | **nl80211** scan results (raw IEs, real "last seen" time per network) on every managed-mode adapter; scans triggered directly with `--monitor`, otherwise through NetworkManager |
| Monitor mode via user-space MT7612U / AR9271 drivers on WinUSB | **Kernel monitor mode** on *any* USB Wi-Fi adapter whose driver supports it (mt76, ath9k_htc, rtl88xxau, ...): nl80211 + AF_PACKET + radiotap, hopping 2.4/5 GHz, channels outside your regulatory domain dropped automatically. The bundled user-space drivers still run for an MT7612U/AR9271 that has no kernel driver bound. |
| Bluetooth via WinRT, one adapter | **BlueZ** through bleak, every adapter scanning at once |
| Windows location | **gpsd** (if installed) and **GeoClue** (Settings › Privacy › Location Services); coarse IP-only estimates ignored, as on Windows |
| Serial GPS on COM ports | `/dev/ttyUSB*`, `/dev/ttyACM*`, and Bluetooth GPS bound with `rfcomm bind` (`/dev/rfcomm*`) |
| gvanem dump1090.exe, auto-downloaded | the distro's decoder. A running decoder **service** (e.g. `dump1090-fa`) is read from `/run/.../aircraft.json` and never fought over the dongle; otherwise the app runs the decoder itself |
| HackRF → dump1090 over rtl_tcp | the same user-space HackRF driver (HackRF Pro fixes included); its IQ is piped into the decoder's stdin (`--ifile - --iformat UC8`) |
| Zadig / WinUSB checks | permission, decoder and brltty checks in the dashboard's setup list |
| Run key + Startup shortcut | XDG autostart entry in a desktop session; systemd user service (`Restart=on-failure`) headless |
| Watchdog sidecar (tasklist/taskkill) | the same watchdog on `/proc` + signals; stands down when systemd supervises |
| Exclusive COM ports | serial ports opened exclusively (flock) by default, so two readers never split a board's bytes |
| MSI updater | `git pull --ff-only`, dependency refresh, restart (one click on the dashboard) |
| `%APPDATA%\WardriveUploader` | `~/.config/wardrive-uploader` (mode 700). An earlier Debian-port folder `~/.config/wdgwars-uploader` is moved over once. |

## Notes

- **Monitor mode** takes USB Wi-Fi adapters that are *not connected to a network* and hands them back to
  NetworkManager when you switch them off on the dashboard, unplug them or quit. Built-in cards and an
  adapter carrying your connection are never touched.
- `--monitor` gives the app's private interpreter copy network-admin capabilities. After Ubuntu updates
  Python, re-run `./install.sh --monitor` so that copy is refreshed.
- **Tray**: Ubuntu's GNOME shows it through the AppIndicator extension (on by default). Without a desktop,
  or with `--headless`, the app runs from the dashboard alone (Quit included). SIGTERM, logout and Ctrl-C
  save the upload queue exactly like Quit.
- **ADS-B with the dump1090-fa service**: set the receiver location with `sudo dpkg-reconfigure dump1090-fa`
  (or `/etc/default/dump1090-fa`). When the app runs the decoder itself it passes the location from Settings.
- **gpsd** opens GPS serial ports itself; with `USBAUTO="true"` in `/etc/default/gpsd` it also grabs other
  USB-serial devices. Set it to `false` and list your GPS in `DEVICES=` so it leaves mesh radios alone.
- Logs: `~/.config/wardrive-uploader/uploader.log`, or `journalctl --user -u wardrive-uploader -f` for the
  headless service. `run_app.py --detect` lists what is plugged in.
- Running the tests: `.venv/bin/python -m pytest` (Linux-specific tests: `tests/test_linux_platform.py`).

## Keeping up with upstream

All Linux code lives in `wdgwars_uploader/linux/`; the shared modules only gained small
`sys.platform` hooks into it, so upstream releases merge with few conflicts:

```sh
git remote add upstream https://github.com/RocketGod-git/wardrive-uploader
git fetch upstream && git merge upstream/main
```

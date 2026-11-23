#!/usr/bin/env bash
# Wardrive Uploader - installer for Ubuntu (24.04+) and Debian-based distros.
#
#   ./install.sh              system packages, Python environment, udev rules,
#                             serial-port access, app menu entry
#   ./install.sh --monitor    also allow Wi-Fi monitor mode (gives the app's own
#                             Python CAP_NET_ADMIN + CAP_NET_RAW)
#   ./install.sh --headless   also start at boot with no desktop (systemd user
#                             service + lingering), for a Pi or a server
#   ./install.sh --yes        answer yes to every question
#   ./install.sh --no-apt     skip apt (you installed the packages yourself)
#
# Run it as your normal user; it asks for sudo for the system parts. Running it
# again is safe and is how you repair or update an install.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/.venv"
PY_APP="$VENV/bin/python-wardrive"      # the interpreter the app runs on (gets the capabilities)
RULES=/etc/udev/rules.d/70-wardrive-uploader.rules
MONITOR=0 HEADLESS=0 YES=0 APT=1

for a in "$@"; do
  case "$a" in
    --monitor) MONITOR=1 ;;
    --headless) HEADLESS=1 ;;
    --yes|-y) YES=1 ;;
    --no-apt) APT=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

c_ok=$'\e[32m' c_warn=$'\e[33m' c_bad=$'\e[31m' c_dim=$'\e[2m' c_off=$'\e[0m'
step() { echo; echo "${c_ok}==>${c_off} $*"; }
note() { echo "    ${c_dim}$*${c_off}"; }
warn() { echo "    ${c_warn}!${c_off} $*"; }
ask() {  # ask "question" -> 0 for yes
  [[ $YES == 1 ]] && return 0
  local r; read -r -p "    $1 [y/N] " r || true; [[ $r =~ ^[Yy] ]]
}

if [[ $EUID -eq 0 ]]; then
  echo "${c_bad}Run this as your normal user, not root:${c_off} it installs for you and uses sudo where needed." >&2
  exit 1
fi
command -v sudo >/dev/null || { echo "sudo is required" >&2; exit 1; }
USER_NAME="$(id -un)"

# ---------------------------------------------------------------- packages ---
if [[ $APT == 1 ]]; then
  command -v apt-get >/dev/null || { echo "apt-get not found: install the packages listed in README-LINUX.md yourself and use --no-apt" >&2; exit 1; }
  step "Installing system packages (sudo apt)"
  if ! sudo apt-get update -qq; then
    warn "apt-get update reported errors (often a third-party repo with no release for your Ubuntu version,"
    warn "e.g. FlightAware's piaware repo on 24.04). Continuing with the sources that did update."
  fi
  have() { [[ -n "$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2}' | grep -v '(none)')" ]]; }
  PKGS=(python3 python3-venv python3-pip python3-gi python3-dbus libusb-1.0-0 bluez
        libcap2-bin iw rfkill git xdg-utils)
  for p in gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7 geoclue-2.0 rtl-sdr hackrf; do
    have "$p" && PKGS+=("$p")
  done
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PKGS[@]}"

  # ADS-B decoder: the first one this distro packages.
  DECODER=""
  for p in dump1090-fa readsb dump1090-mutability; do
    if dpkg -s "$p" >/dev/null 2>&1; then DECODER="$p (already installed)"; break; fi
    if have "$p"; then
      if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$p"; then DECODER="$p"; fi
      break
    fi
  done
  if [[ -n $DECODER ]]; then
    note "ADS-B decoder: $DECODER"
  else
    warn "No ADS-B decoder in your apt sources. Aircraft need one of:"
    note "  readsb (works on Ubuntu 24.04):"
    note "    sudo bash -c \"\$(wget -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)\""
    note "  dump1090-fa: FlightAware's apt repo has no Ubuntu 24.04 release; build it from source instead"
    note "    (https://github.com/flightaware/dump1090), or use readsb."
    note "Everything else works without it. Re-run ./install.sh afterwards."
  fi
fi

PY="$(command -v python3)"
"$PY" - <<'EOF' || { echo "Python 3.11 or newer is required" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF

# ------------------------------------------------------------- python venv ---
step "Python environment ($VENV)"
if [[ ! -x "$VENV/bin/python" ]]; then
  # System site-packages: the tray (PyGObject/AppIndicator) and GeoClue (dbus) come from apt.
  "$PY" -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"
# A real copy of the interpreter (not a symlink to the system one) so file
# capabilities given to it affect this app only.
REAL_PY="$(readlink -f "$VENV/bin/python")"
if [[ ! -f "$PY_APP" ]] || ! cmp -s "$REAL_PY" "$PY_APP"; then
  cp "$REAL_PY" "$PY_APP"
fi
"$PY_APP" -c "import wdgwars_uploader, serial, usb1, bleak, meshcore" \
  || { echo "${c_bad}Python packages did not install correctly${c_off}" >&2; exit 1; }
note "ok"

# ------------------------------------------------------------- hardware access -
step "USB and serial access (udev rules, groups)"
TMP_RULES="$(mktemp)"
(cd "$APP_DIR" && "$PY_APP" -m wdgwars_uploader.linux.udev) > "$TMP_RULES"
sudo install -m 0644 "$TMP_RULES" "$RULES"
rm -f "$TMP_RULES"
if command -v udevadm >/dev/null; then
  udevadm verify --help >/dev/null 2>&1 && { sudo udevadm verify "$RULES" >/dev/null || warn "udevadm verify reported a problem in $RULES"; }
  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=usb --subsystem-match=tty || true
fi
note "rules: $RULES"
NEED_RELOGIN=0
for g in dialout plugdev; do
  getent group "$g" >/dev/null || sudo groupadd -r "$g"
  if ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$g"; then
    sudo usermod -aG "$g" "$USER_NAME"
    NEED_RELOGIN=1
    note "added $USER_NAME to $g"
  fi
done

# RTL-SDR: keep the DVB-T TV driver off the dongle.
if [[ ! -e /etc/modprobe.d/rtl-sdr-blacklist.conf && ! -e /etc/modprobe.d/blacklist-rtl-sdr.conf ]]; then
  printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\nblacklist rtl2832_sdr\n' \
    | sudo tee /etc/modprobe.d/wardrive-uploader-rtlsdr.conf >/dev/null
  note "RTL-SDR: DVB-T driver blacklisted"
fi

# brltty grabs CH340/CH341 USB-serial chips on Ubuntu (the board's port vanishes).
if [[ -e /usr/lib/udev/rules.d/85-brltty.rules && ! -e /etc/udev/rules.d/85-brltty.rules ]]; then
  warn "brltty (braille display support) takes over CH340/CH341 boards on Ubuntu."
  if ask "Stop brltty from claiming USB-serial devices? (only say no if you use a braille display)"; then
    echo "# Wardrive Uploader: overrides /usr/lib/udev/rules.d/85-brltty.rules (delete to undo)" \
      | sudo tee /etc/udev/rules.d/85-brltty.rules >/dev/null
    sudo systemctl mask --now brltty-udev.service >/dev/null 2>&1 || true
    sudo udevadm control --reload-rules
    note "brltty no longer claims USB-serial devices"
  fi
fi

# GeoClue (Location Services): authorize this app by its desktop id, so it gets
# a position without a desktop prompt (and headless, where there is no agent).
GEOCONF=/etc/geoclue/geoclue.conf
if [[ -f $GEOCONF ]] && ! grep -q '^\[wardrive-uploader\]' "$GEOCONF"; then
  printf '\n# Wardrive Uploader (added by install.sh)\n[wardrive-uploader]\nallowed=true\nsystem=true\nusers=\n' \
    | sudo tee -a "$GEOCONF" >/dev/null
  sudo systemctl try-restart geoclue.service 2>/dev/null || true
  note "GeoClue: Wardrive Uploader allowed to read location"
fi
command -v gsettings >/dev/null && [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]] \
  && [[ "$(gsettings get org.gnome.system.location enabled 2>/dev/null)" == "false" ]] \
  && warn "Location Services is off: Settings > Privacy & Security > Location (only needed if you use it as a GPS source)"

# ------------------------------------------------------------- monitor mode ---
if [[ $MONITOR == 1 ]]; then
  step "Wi-Fi monitor mode (capabilities on $PY_APP)"
  sudo setcap cap_net_admin,cap_net_raw+eip "$PY_APP"
  note "$(getcap "$PY_APP")"
  note "USB Wi-Fi adapters that are not connected to a network go into monitor mode;"
  note "switch one off on the dashboard to hand it back to NetworkManager."
elif getcap "$PY_APP" 2>/dev/null | grep -q cap_net; then
  note "monitor mode stays enabled (capabilities already set)"
fi

# ------------------------------------------------------------- firewall ------
if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  step "Firewall (Phone GPS ports 10110/10111, local network only)"
  if ask "Allow phones on your local network to reach the Phone GPS ports?"; then
    for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
      sudo ufw allow proto tcp from "$net" to any port 10110,10111 comment "Wardrive Uploader phone GPS" >/dev/null
      sudo ufw allow proto udp from "$net" to any port 10110 comment "Wardrive Uploader phone GPS" >/dev/null
    done
    note "opened"
  fi
fi

# ------------------------------------------------------------- launchers -----
step "App menu entry"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/wardrive-uploader.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Wardrive Uploader
GenericName=Wardriving collector
Comment=Collect Wi-Fi, Bluetooth, ADS-B and mesh nodes and upload them to WiGLE, WDGWars and Wardrift
Exec="$PY_APP" "$APP_DIR/run_app.py"
Icon=$APP_DIR/wdgwars_uploader/webui/static/tpp.png
Terminal=false
Categories=Network;HamRadio;
Keywords=wardriving;wigle;wifi;bluetooth;adsb;meshtastic;meshcore;
StartupNotify=false
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" >/dev/null 2>&1 || true
note "$APPS/wardrive-uploader.desktop"

if [[ $HEADLESS == 1 ]]; then
  step "Start at boot without a desktop (systemd user service)"
  (cd "$APP_DIR" && env -u DISPLAY -u WAYLAND_DISPLAY "$PY_APP" - <<'EOF'
from wdgwars_uploader.config import Config
from wdgwars_uploader.linux import autostart
cfg = Config(); cfg.set("start_on_boot", True); cfg.save()
raise SystemExit(0 if autostart.enable() else 1)
EOF
  ) || warn "could not register the systemd user service"
  sudo loginctl enable-linger "$USER_NAME"
  systemctl --user restart wardrive-uploader.service 2>/dev/null || true
  note "status: systemctl --user status wardrive-uploader   log: journalctl --user -u wardrive-uploader -f"
fi

# ------------------------------------------------------------- done ----------
step "Done"
echo "    Start it from the app menu (Wardrive Uploader), or:  $PY_APP $APP_DIR/run_app.py"
echo "    Dashboard: http://127.0.0.1:8787/    Devices found:  $PY_APP $APP_DIR/run_app.py --detect"
[[ $MONITOR == 0 ]] && echo "    Monitor mode on USB Wi-Fi adapters: ./install.sh --monitor"
if [[ $NEED_RELOGIN == 1 ]]; then
  echo "    ${c_warn}Log out and back in once${c_off} so the new group membership (serial ports) applies."
fi

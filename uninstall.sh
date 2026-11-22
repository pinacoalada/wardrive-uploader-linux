#!/usr/bin/env bash
# Wardrive Uploader - remove what install.sh set up.
#   ./uninstall.sh          launchers, autostart, udev rules, capabilities, venv
#   ./uninstall.sh --purge  also the data folder (~/.config/wardrive-uploader:
#                           keys, queued uploads, captures)
# Apt packages and group memberships are left alone.
set -uo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PURGE=0; [[ "${1:-}" == "--purge" ]] && PURGE=1
CFG="${XDG_CONFIG_HOME:-$HOME/.config}"

systemctl --user disable --now wardrive-uploader.service 2>/dev/null
rm -f "$CFG/systemd/user/wardrive-uploader.service" "$CFG/autostart/wardrive-uploader.desktop"
systemctl --user daemon-reload 2>/dev/null
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/wardrive-uploader.desktop"
pkill -f "$APP_DIR/run_app.py" 2>/dev/null
sudo rm -f /etc/udev/rules.d/70-wardrive-uploader.rules /etc/modprobe.d/wardrive-uploader-rtlsdr.conf
if [[ -e /etc/udev/rules.d/85-brltty.rules ]] && grep -q "Wardrive Uploader" /etc/udev/rules.d/85-brltty.rules; then
  sudo rm -f /etc/udev/rules.d/85-brltty.rules
  sudo systemctl unmask brltty-udev.service 2>/dev/null
fi
sudo udevadm control --reload-rules 2>/dev/null
rm -rf "$APP_DIR/.venv"
if [[ $PURGE == 1 ]]; then
  rm -rf "$CFG/wardrive-uploader"
  echo "Removed, including the data folder."
else
  echo "Removed. Your data is still in $CFG/wardrive-uploader (./uninstall.sh --purge deletes it)."
fi

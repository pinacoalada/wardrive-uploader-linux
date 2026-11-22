"""Linux platform layer.

Everything the Windows build gets from Win32, WinRT, PowerShell and the registry
lives here, implemented on what Ubuntu provides natively: nl80211 for Wi-Fi
scanning and monitor mode, BlueZ (through bleak) for Bluetooth, gpsd and GeoClue
for location, a packaged dump1090/readsb for ADS-B, systemd for start-at-login
and sysfs/procfs for device and process checks.

The upstream modules call in here behind ``sys.platform`` checks, so the shared
code stays the same as the Windows app and new upstream releases merge cleanly.
"""

from __future__ import annotations

import sys

IS_LINUX = sys.platform.startswith("linux")

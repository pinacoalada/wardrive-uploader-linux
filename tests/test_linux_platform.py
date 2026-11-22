"""Linux platform layer: nl80211/netlink, radiotap, Wi-Fi scan results, ADS-B decoder
handling (service JSON, launched decoder, HackRF relay), gpsd, autostart, udev."""
import json
import os
import socket
import stat
import struct
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

if not sys.platform.startswith("linux"):
    raise unittest.SkipTest("Linux only")

from wdgwars_uploader.linux import adsb as lx, autostart, gps, monitor, netlink as nl, nl80211, system, udev
from wdgwars_uploader.linux import wifi as lwifi


# -- netlink / nl80211 --------------------------------------------------------------
class Netlink(unittest.TestCase):
    def test_attr_roundtrip_with_padding_and_nesting(self):
        inner = nl.attr(1, b"\xaa\xbb\xcc\xdd\xee\xff") + nl.attr_u32(2, 2437)
        raw = nl.attr_u32(3, 7) + nl.attr(47, inner, nested=True) + nl.attr_str(4, "wlan1")
        a = nl.parse_attrs(raw)
        self.assertEqual(nl.u32(a[3]), 7)
        self.assertEqual(nl.cstr(a[4]), "wlan1")
        b = nl.parse_attrs(a[47])
        self.assertEqual(b[1], b"\xaa\xbb\xcc\xdd\xee\xff")
        self.assertEqual(nl.u32(b[2]), 2437)

    def test_generic_netlink_resolves_a_family(self):
        g = nl.Genl("nlctrl")           # always present: the generic netlink controller
        self.assertEqual(g.family, nl.GENL_ID_CTRL)
        g.close()

    def test_rtnetlink_lists_loopback(self):
        with nl.Rtnl() as r:
            self.assertIn("lo", [name for _i, _f, name in r.links()])

    def test_scan_results_decode(self):
        ies = bytes([0, 4]) + b"Home" + bytes([48, 20, 1, 0, 0, 0x0F, 0xAC, 4, 1, 0, 0, 0x0F, 0xAC, 4,
                                               1, 0, 0, 0x0F, 0xAC, 2, 0, 0])
        bss = (nl.attr(nl80211.BSS_BSSID, bytes.fromhex("0011223344ff")) + nl.attr_u32(nl80211.BSS_FREQUENCY, 5180)
               + nl.attr(nl80211.BSS_CAPABILITY, struct.pack("=H", 0x0011)) + nl.attr(nl80211.BSS_INFORMATION_ELEMENTS, ies)
               + nl.attr(nl80211.BSS_SIGNAL_MBM, struct.pack("=i", -6700)) + nl.attr_u32(nl80211.BSS_SEEN_MS_AGO, 1500))
        n = nl80211.Nl80211.__new__(nl80211.Nl80211)
        n.g = mock.Mock()
        n.g.call.return_value = [{nl80211.ATTR_BSS: bss}, {}]
        r = n.scan_results(4)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["bssid"], "00:11:22:33:44:ff")
        self.assertEqual((r[0]["freq"], r[0]["rssi"], r[0]["age_ms"], r[0]["cap"]), (5180, -67, 1500, 0x11))
        self.assertEqual(r[0]["ies"], ies)

    def test_channels(self):
        for ch, f in ((1, 2412), (6, 2437), (13, 2472), (14, 2484), (36, 5180), (165, 5825)):
            self.assertEqual(nl80211.channel_to_freq(ch), f)
            self.assertEqual(nl80211.freq_to_channel(f), ch)


class WifiScanner(unittest.TestCase):
    def _scanner(self, results, iface_state="up"):
        s = lwifi.WifiScanner.__new__(lwifi.WifiScanner)
        s.adapters, s.off, s.direct, s._nmcli, s._nm_at, s._procs = [], set(), True, None, {}, []
        s.nl = mock.Mock()
        it = nl80211.Iface(4, "wlp3s0", nl80211.IFTYPE_STATION, 0, "aa:bb:cc:dd:ee:ff")
        s.nl.interfaces.return_value = [it]
        s.nl.scan_results.return_value = results
        return s, it

    def test_fresh_networks_with_security_and_time_heard(self):
        rsn = bytes([48, 20, 1, 0, 0, 0x0F, 0xAC, 4, 1, 0, 0, 0x0F, 0xAC, 4, 1, 0, 0, 0x0F, 0xAC, 2, 0, 0])
        res = [
            {"bssid": "00:11:22:33:44:55", "freq": 2437, "cap": 0x11, "rssi": -50, "ies": b"\x00\x04Cafe" + rsn,
             "age_ms": 1000, "boottime_ns": None},
            {"bssid": "00:11:22:33:44:66", "freq": 2412, "cap": 0x01, "rssi": -70, "ies": b"\x00\x00",
             "age_ms": 60000, "boottime_ns": None},     # heard a minute ago: stale
        ]
        s, it = self._scanner(res)
        with mock.patch.object(nl80211.Iface, "operstate", "up"), \
             mock.patch.object(nl80211.Iface, "label", "wlp3s0 (Intel, iwlwifi)"):
            ifs = s.trigger()
            nets = s.read(ifs)
        s.nl.trigger_scan.assert_called_once_with(4)
        self.assertEqual(len(nets), 1)
        n = nets[0]
        self.assertEqual((n["ssid"], n["channel"], n["rssi"]), ("Cafe", 6, -50))
        self.assertIn("WPA2", n["auth"])
        self.assertAlmostEqual(time.time() - n["utc"], 1.0, delta=0.5)
        self.assertEqual(s.adapters, [{"name": "wlp3s0 (Intel, iwlwifi)", "state": "connected", "networks": 1}])

    def test_without_net_admin_falls_back_to_networkmanager(self):
        s, it = self._scanner([])
        s._nmcli = "/usr/bin/nmcli"
        import errno
        s.nl.trigger_scan.side_effect = nl.NetlinkError(errno.EPERM, "Operation not permitted")
        with mock.patch("subprocess.Popen") as popen:
            s.trigger()
            s.trigger()                  # rate-limited: one rescan per 10 s
        self.assertFalse(s.direct)
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0][:4], ["/usr/bin/nmcli", "device", "wifi", "rescan"])


# -- radiotap -------------------------------------------------------------------------
def _radiotap(frame, signal=-42, freq=2437, fcs=False, bad=False, ext=False):
    present = (1 << 1) | (1 << 3) | (1 << 5) | ((1 << 31) if ext else 0)
    body = struct.pack("<I", present) + (struct.pack("<I", 0) if ext else b"")
    off = 4 + len(body)
    flags = (0x10 if fcs else 0) | (0x40 if bad else 0)
    fields = bytes([flags])
    off += 1
    pad = (-off) % 2
    fields += b"\0" * pad + struct.pack("<HH", freq, 0x00A0) + struct.pack("<b", signal)
    hdr_len = 4 + len(body) + len(fields)
    return bytes([0, 0]) + struct.pack("<H", hdr_len) + body + fields + frame + (b"\xde\xad\xbe\xef" if fcs else b"")


class Radiotap(unittest.TestCase):
    BEACON = bytes([0x80, 0]) + b"\0" * 34 + b"\x00\x04Test"

    def test_parse_plain_fcs_and_extended(self):
        for kw in ({}, {"fcs": True}, {"ext": True}, {"fcs": True, "ext": True}):
            f, sig, freq = monitor.parse_radiotap(_radiotap(self.BEACON, **kw))
            self.assertEqual((f, sig, freq), (self.BEACON, -42, 2437), kw)

    def test_bad_fcs_dropped(self):
        self.assertIsNone(monitor.parse_radiotap(_radiotap(self.BEACON, fcs=True, bad=True)))
        self.assertIsNone(monitor.parse_radiotap(b"\x00\x00\x40\x00" + b"\0" * 8))

    def test_frames_reach_the_shared_beacon_handler(self):
        from wdgwars_uploader.wardrive.monitor import MonitorManager
        m = MonitorManager(on_hit=lambda h: None)
        bssid = bytes.fromhex("020000aabbcc")
        beacon = (bytes([0x80, 0, 0, 0]) + b"\xff" * 6 + bssid + bssid + b"\0\0" + b"\0" * 8 + b"\x64\x00"
                  + struct.pack("<H", 0x0011) + b"\x00\x06Garage")
        f, sig, freq = monitor.parse_radiotap(_radiotap(beacon, signal=-61, freq=5180))
        m._frame(f, sig, nl80211.freq_to_channel(freq), "Alfa AWUS036ACM")
        aps = m.fresh(5)
        self.assertEqual([(a["mac"], a["ssid"], a["rssi"], a["channel"]) for a in aps],
                         [("02:00:00:aa:bb:cc", "Garage", -61, 36)])


# -- ADS-B ----------------------------------------------------------------------------
FAKE_DECODER = textwrap.dedent("""\
    #!{py}
    import json, os, sys, time
    a = sys.argv[1:]
    out = a[a.index("--write-json") + 1]
    open(os.path.join(out, "args.json"), "w").write(json.dumps(a))
    hackrf = "--ifile" in a
    got = 0
    while True:
        if hackrf:
            chunk = sys.stdin.buffer.read(65536)
            if not chunk:
                break
            got += len(chunk)
        else:
            time.sleep(0.2)
        ac = [{{"hex": "a1b2c3", "lat": 37.6, "lon": -122.3, "flight": "TEST1"}}] if (got or not hackrf) else []
        tmp = os.path.join(out, "aircraft.json.tmp")
        open(tmp, "w").write(json.dumps({{"now": time.time(), "aircraft": ac, "iq_bytes": got}}))
        os.replace(tmp, os.path.join(out, "aircraft.json"))
""")


class Adsb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        lx.stop()
        lx._last_attempt = 0.0
        p = [mock.patch.object(lx, "json_dir", lambda: self.dir),
             mock.patch.object(lx, "SYSTEM_JSON", (str(self.dir / "svc" / "aircraft.json"),))]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.addCleanup(lx.stop)
        self.addCleanup(self.tmp.cleanup)

    def _decoder(self, name="dump1090-fa"):
        exe = self.dir / name
        exe.write_text(FAKE_DECODER.format(py=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return str(exe)

    def test_args_per_decoder(self):
        a = lx.build_args("/usr/bin/dump1090-fa", "rtlsdr", (37.5, -122.25), 40)
        self.assertIn("--device-type", a)
        self.assertEqual(a[a.index("--device-type") + 1], "rtlsdr")
        self.assertEqual(a[a.index("--lat") + 1], "37.500000")
        self.assertEqual(a[a.index("--gain") + 1], "40")
        h = lx.build_args("/usr/bin/readsb", "hackrf", None, 40)
        self.assertEqual(h[h.index("--device-type") + 1], "ifile")
        self.assertEqual(h[h.index("--ifile") + 1:h.index("--ifile") + 4], ["-", "--iformat", "UC8"])
        self.assertNotIn("--gain", h)
        m = lx.build_args("/usr/bin/dump1090-mutability", "hackrf", None, None)
        self.assertNotIn("--device-type", m)
        self.assertIn("--ifile", m)

    def test_running_service_is_read_and_nothing_launched(self):
        svc = self.dir / "svc"
        svc.mkdir()
        (svc / "aircraft.json").write_text(json.dumps({"now": 1, "aircraft": [{"hex": "abc123"}]}))
        with mock.patch("subprocess.Popen") as popen, mock.patch("wdgwars_uploader.hackrf.stop_bridge"):
            self.assertTrue(lx.ensure({}, "rtlsdr", True, None))
        popen.assert_not_called()
        self.assertEqual(lx.status["mode"], "service")
        from wdgwars_uploader import adsb
        with mock.patch.object(adsb, "_active_source", "rtlsdr"):
            self.assertEqual(adsb.collect({"adsb_json_url": "", "adsb_json_file": ""})["aircraft"][0]["hex"], "abc123")

    def test_rtlsdr_launches_the_packaged_decoder(self):
        exe = self._decoder()
        with mock.patch("wdgwars_uploader.hackrf.stop_bridge"):
            self.assertTrue(lx.ensure({"dump1090_exe": exe, "dump1090_gain": 42}, "rtlsdr", True, (51.5, -0.1)))
            self.assertTrue(lx.ensure({"dump1090_exe": exe, "dump1090_gain": 42}, "rtlsdr", True, (51.5, -0.1)))
        self._wait(lambda: lx.collect_path())
        from wdgwars_uploader import adsb
        with mock.patch.object(adsb, "_active_source", "rtlsdr"):
            obj = adsb.collect({"adsb_json_url": "", "adsb_json_file": ""})
        self.assertEqual(obj["aircraft"][0]["flight"], "TEST1")
        args = json.loads((self.dir / "args.json").read_text())
        self.assertEqual(args[args.index("--lat") + 1], "51.500000")
        lx.stop()
        self.assertFalse(lx.running())
        self.assertEqual(lx.collect_path(), "")

    def test_hackrf_samples_are_relayed_into_the_decoder(self):
        """A stand-in rtl_tcp bridge: the relay must send rate + frequency, then pipe IQ to stdin."""
        exe = self._decoder()
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        cmds = []

        def bridge():
            c, _ = srv.accept()
            c.sendall(struct.pack(">4sII", b"RTL0", 5, 29))
            c.settimeout(2)
            buf = b""
            while len(buf) < 10:
                buf += c.recv(10 - len(buf))
            cmds.extend(struct.unpack(">BIBI", buf))
            for _ in range(20):
                c.sendall(bytes(range(256)) * 64)
            time.sleep(1.5)
            c.close()

        threading.Thread(target=bridge, daemon=True).start()
        with mock.patch("wdgwars_uploader.hackrf.TCP_PORT", port), \
             mock.patch("wdgwars_uploader.hackrf.ensure_bridge"):
            self.assertTrue(lx.ensure({"dump1090_exe": exe}, "hackrf", None, None))
            self._wait(lambda: (self.dir / "aircraft.json").exists()
                       and json.loads((self.dir / "aircraft.json").read_text()).get("iq_bytes", 0) >= 20 * 16384)
        self.assertEqual(cmds, [0x02, lx.HACKRF_RATE, 0x01, lx.ADSB_FREQ])
        srv.close()

    def test_no_decoder_reports_it(self):
        with mock.patch.object(lx, "find_decoder", return_value=None), \
             mock.patch("wdgwars_uploader.hackrf.stop_bridge"):
            self.assertFalse(lx.ensure({}, "rtlsdr", True, None))
        self.assertIn("no ADS-B decoder", lx.status["error"])
        from wdgwars_uploader.linux import prereq
        with mock.patch.object(lx, "find_decoder", return_value=None):
            c = prereq.decoder({})
        self.assertFalse(c["ok"])
        self.assertIn("apt install", c["fix"])

    @staticmethod
    def _wait(cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        raise AssertionError("timed out")


# -- GPS ------------------------------------------------------------------------------
class Gpsd(unittest.TestCase):
    def test_tpv_and_sky(self):
        g = gps.GpsdGps()
        g._message(b'{"class":"SKY","uSat":9}')
        g._message(json.dumps({"class": "TPV", "mode": 3, "lat": 40.1, "lon": -105.2, "altHAE": 1600.5,
                               "eph": 4.2, "speed": 12.5, "track": 90.0,
                               "time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}).encode())
        f = g.fix
        self.assertEqual((f.lat, f.lon, f.acc, f.sats, f.alt), (40.1, -105.2, 4.2, 9, 1600.5))
        self.assertTrue(f.usable())
        g2 = gps.GpsdGps()
        g2._message(b'{"class":"TPV","mode":1}')
        self.assertIsNone(g2.fix)


# -- autostart --------------------------------------------------------------------------
class Autostart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WARDRIVE_SUPERVISED", None)

    def test_desktop_session_uses_xdg_autostart(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), mock.patch.object(autostart, "_systemctl") as sc:
            sc.return_value = mock.Mock(returncode=1)
            self.assertTrue(autostart.enable())
            f = Path(self.tmp.name) / "autostart" / "wardrive-uploader.desktop"
            self.assertIn("--boot", f.read_text())
            self.assertTrue(autostart.is_enabled())
            autostart.disable()
            self.assertFalse(f.exists())

    def test_headless_uses_a_systemd_user_unit(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(autostart, "_systemctl") as sc:
            sc.return_value = mock.Mock(returncode=0, stderr="")
            self.assertTrue(autostart.enable())
            unit = Path(self.tmp.name) / "systemd" / "user" / "wardrive-uploader.service"
            text = unit.read_text()
            self.assertIn("Restart=on-failure", text)
            self.assertIn("WARDRIVE_SUPERVISED=1", text)
            self.assertIn(mock.call("enable", "wardrive-uploader.service"), sc.call_args_list)


# -- system / udev ----------------------------------------------------------------------
class System(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(system.pid_alive(os.getpid()))
        self.assertFalse(system.pid_alive(2 ** 22 + 12345))

    def test_country_from_locale(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "", "LC_ADDRESS": "", "LC_MESSAGES": "", "LANG": "en_GB.UTF-8"}):
            self.assertEqual(system.country(), "GB")

    def test_udev_rules_are_one_rule_per_line(self):
        text = udev.rules()
        for line in text.splitlines():
            if line and not line.startswith("#"):
                self.assertNotIn("#", line)
                self.assertTrue(line.startswith(("SUBSYSTEM==", "SUBSYSTEMS==")), line)
        self.assertIn('ATTR{idVendor}=="1d50", ATTR{idProduct}=="6089"', text)    # HackRF
        self.assertIn('ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", ENV{ID_MM_DEVICE_IGNORE}="1"', text)


if __name__ == "__main__":
    unittest.main()


# -- native monitor mode ------------------------------------------------------------------
class NativeMonitor(unittest.TestCase):
    """Adapter lifecycle with nl80211, rtnetlink and the packet socket faked out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(monitor._Owned, "_path", staticmethod(lambda: Path(self.tmp.name) / "own.json"))
        p.start()
        self.addCleanup(p.stop)

    def _iface(self, name="wlx00c0ca", iftype=nl80211.IFTYPE_STATION, oper="down", key="1-2"):
        it = nl80211.Iface(9, name, iftype, 1, "00:c0:ca:00:00:01")
        props = {"usb_key": key, "operstate": oper, "product": "AWUS036ACM", "driver": "mt76x2u"}
        return it, [mock.patch.object(nl80211.Iface, k, new_callable=mock.PropertyMock, return_value=v)
                    for k, v in props.items()]

    def test_scan_states(self):
        frames = []
        nm = monitor.NativeMonitor(lambda *a: frames.append(a), off=lambda: {"9-9"})
        it, props = self._iface()
        for p in props:
            p.start()
            self.addCleanup(p.stop)
        nlm = mock.Mock()
        nlm.interfaces.return_value = [it]
        with mock.patch.object(nl80211, "has_net_admin", return_value=False):
            nm._scan(nlm)
        self.assertEqual(nm.status()[0]["state"], "needs permission")
        self.assertEqual(nm.status()[0]["id"], "mon:1-2")
        nm2 = monitor.NativeMonitor(lambda *a: None, off=lambda: {"1-2"})
        nm2._scan(nlm)
        self.assertEqual(nm2.status()[0]["state"], "off")

    def test_connected_adapter_is_left_alone(self):
        nm = monitor.NativeMonitor(lambda *a: None)
        it, props = self._iface(oper="up")
        for p in props:
            p.start()
            self.addCleanup(p.stop)
        nlm = mock.Mock()
        nlm.interfaces.return_value = [it]
        with mock.patch.object(nl80211, "has_net_admin", return_value=True), \
             mock.patch.object(threading.Thread, "start") as start:
            nm._scan(nlm)
        start.assert_not_called()
        self.assertEqual(nm.status(), [])

    def test_run_switches_hops_captures_and_restores(self):
        import errno
        got = []
        nm = monitor.NativeMonitor(lambda f, r, c, n: got.append((r, c, n)))
        it, props = self._iface()
        for p in props:
            p.start()
            self.addCleanup(p.stop)
        m = monitor._Mon("1-2", it)
        nlm, rtm = mock.Mock(), mock.Mock()

        def set_freq(idx, mhz):           # 12/13 and DFS 52-144 are refused by "regdomain"
            ch = nl80211.freq_to_channel(mhz)
            if ch in (12, 13) or 52 <= ch <= 144:
                raise nl.NetlinkError(errno.EINVAL, "Invalid argument")
        nlm.set_freq.side_effect = set_freq
        beacon = (bytes([0x80, 0, 0, 0]) + b"\xff" * 6 + bytes.fromhex("020000aabbcc") * 2 + b"\0\0"
                  + b"\0" * 8 + b"\x64\x00\x11\x00\x00\x02Hi")
        pkts = [_radiotap(beacon, signal=-55, freq=2412), _radiotap(beacon, signal=-56, freq=5745)]

        class FakeSock:
            def __init__(self, *a):
                pass
            def bind(self, addr):
                assert addr == ("wlx00c0ca", monitor.ETH_P_ALL)
            def settimeout(self, t):
                pass
            def recv(self, n):
                if pkts:
                    return pkts.pop(0)
                m.stop_evt.set()
                raise socket.timeout
            def close(self):
                pass

        with mock.patch.object(nl80211, "Nl80211", return_value=nlm), \
             mock.patch.object(monitor, "Rtnl", return_value=rtm), \
             mock.patch.object(monitor.socket, "socket", FakeSock), \
             mock.patch.object(monitor, "_nmcli_managed") as nmc:
            nm._run(m)
        self.assertEqual(got, [(-55, 1, "AWUS036ACM"), (-56, 149, "AWUS036ACM")])
        self.assertEqual(nlm.set_type.call_args_list,
                         [mock.call(9, nl80211.IFTYPE_MONITOR), mock.call(9, nl80211.IFTYPE_STATION)])
        self.assertEqual(nmc.call_args_list, [mock.call("wlx00c0ca", False), mock.call("wlx00c0ca", True)])
        self.assertEqual(m.state, "stopped")
        self.assertEqual(monitor._Owned.load(), set())     # handed back: nothing left stranded

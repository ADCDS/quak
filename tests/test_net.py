import asyncio
import contextlib
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quak import flows
from quak.connect import Target, open_camera
from quak.core.errors import AdapterInUse, NetworkError, Rejected
from quak.core.registry import Status
from quak.net import nmcli
from quak.net.http import CameraHttp, MediaFile, MediaLedger, camera_epoch
from quak.net.wifi import camera_wifi
from quak.service import CameraService
from quak.transport.sim import SimCamera, SimNetwork
from quak.transport.sim_http import SimHttpServer

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


class FakeNmcli:
    """Records nmcli/ip invocations and answers like NetworkManager 1.52 would."""

    def __init__(self, saved=None, connection="HERO11 Black"):
        self.calls: list[list[str]] = []
        self.saved = dict(saved or {"HomeNetwork": "HomeNetwork"})  # profile name -> SSID
        self.connection = connection

    def __call__(self, args):
        self.calls.append(args)
        if args[:5] == ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            return f"enp4s0:ethernet:connected:Wired connection 1\nwlx0:wifi:connected:{self.connection}\n"
        if args[:4] == ["nmcli", "-t", "-f", "WIFI-PROPERTIES"]:
            return "WIFI-PROPERTIES.AP:yes\nWIFI-PROPERTIES.2GHZ:yes\nWIFI-PROPERTIES.5GHZ:no\n"
        if args[:5] == ["nmcli", "-t", "-f", "NAME,TYPE", "connection"]:
            return "".join(f"{n}:802-11-wireless\n" for n in self.saved) + "Wired connection 1:802-3-ethernet\n"
        if args[:3] == ["nmcli", "-g", "GENERAL.AUTOCONNECT"]:
            return getattr(self, "autoconnect", "yes") + "\n"
        if args[:3] == ["nmcli", "-g", "802-11-wireless.ssid"]:
            return self.saved.get(args[-1], "") + "\n"
        if args[:4] == ["nmcli", "-t", "-f", "IP4.ADDRESS"]:
            return "IP4.ADDRESS[1]:10.42.0.1/24\n"
        if args[:3] == ["ip", "route", "get"]:
            return "1.1.1.1 via 192.168.1.1 dev enp4s0 src 192.168.1.2 uid 1000\n"
        return ""


class NmcliTests(unittest.TestCase):
    def test_terse_split_handles_escaped_colons(self):
        self.assertEqual(nmcli.split_terse(r"a\:b:c:d\\e"), ["a:b", "c", "d\\e"])

    def test_join_profile_never_takes_default_route_or_dns(self):
        fake = FakeNmcli()
        nmcli.NetworkManager(fake).join("wlx0", "GP-SIM", "pw")
        add = next(c for c in fake.calls if c[:3] == ["nmcli", "connection", "add"])
        joined = " ".join(add)
        for must in (
            "ifname wlx0",
            "con-name quak-camap",
            "autoconnect no",
            "ipv4.never-default yes",
            "ipv4.ignore-auto-dns yes",
            "ipv6.method disabled",
        ):
            self.assertIn(must, joined)

    def test_hotspot_profile_is_2g_shared_ap(self):
        fake = FakeNmcli()
        nmcli.NetworkManager(fake).hotspot_up("wlx0", "quak-lab-1", "pw")
        joined = " ".join(next(c for c in fake.calls if c[:3] == ["nmcli", "connection", "add"]))
        for must in (
            "802-11-wireless.mode ap",
            "802-11-wireless.band bg",
            "ipv4.method shared",
            "autoconnect no",
            "con-name quak-hotspot",
        ):
            self.assertIn(must, joined)

    def test_saved_ssids_exclude_quak_profiles_and_secrets_never_read(self):
        fake = FakeNmcli(saved={"HomeNetwork": "HomeNetwork", "quak-camap": "GP-SIM"})
        self.assertEqual(nmcli.NetworkManager(fake).saved_wifi_ssids(), {"HomeNetwork"})
        self.assertFalse(any("--show-secrets" in c or "-s" in c for c in fake.calls))

    def test_errors_redact_passwords(self):
        self.assertEqual(nmcli._redact(["nmcli", "wifi-sec.psk", "hunter2"]), ["nmcli", "wifi-sec.psk", "***"])

    def test_device_capabilities(self):
        dev = nmcli.NetworkManager(FakeNmcli()).pick_device(need_ap=True)
        self.assertEqual((dev.name, dev.ap_mode, dev.band_5ghz), ("wlx0", True, False))


class HotspotTests(unittest.IsolatedAsyncioTestCase):
    async def test_credentials_are_private_and_stable_and_guarded(self):
        from quak.net import hotspot

        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": d}):
            ssid, pw = hotspot.lab_credentials()
            self.assertEqual(hotspot.lab_credentials(), (ssid, pw))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(Path(d) / "hotspot.json").st_mode), 0o600)
            fake = FakeNmcli(saved={"Neighbour": ssid})  # someone else's profile using that SSID
            with self.assertRaises(NetworkError):
                async with hotspot.lab_hotspot(nmcli.NetworkManager(fake)):
                    pass
            fake = FakeNmcli()
            async with hotspot.lab_hotspot(nmcli.NetworkManager(fake)) as hs:
                self.assertEqual(hs.gateway, "10.42.0.1")
            self.assertIn(["nmcli", "connection", "delete", "quak-hotspot"], fake.calls)
            self.assertTrue(any(c[-3:] == ["HERO11 Black", "ifname", "wlx0"] for c in fake.calls))  # restored


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CameraWifiTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_path_restores_everything(self):
        cam = SimCamera(latency=0.0005)
        port = free_port()
        servers = []

        class Joiner(nmcli.NetworkManager):
            def join(self, device, ssid, password, name=nmcli.CAMERA_PROFILE, timeout=40):
                super().join(device, ssid, password, name, timeout)
                servers.append(SimHttpServer(cam, loop, port=port))
                servers[-1].start()

            def ipv4(self, device):
                return None  # the fake address isn't on this machine; don't bind to it

        loop = asyncio.get_running_loop()
        fake = FakeNmcli()
        async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
            async with camera_wifi(c, base_url=f"http://127.0.0.1:{port}", joiner=Joiner(fake)) as api:
                self.assertEqual(cam.status[Status.AP_MODE], 1)
                self.assertEqual(await asyncio.to_thread(api.version), "2.0")
            await c.wait_status(Status.AP_MODE, lambda v: v == 0, 3, poll=0.1)
        for s in servers:
            s.stop()
        self.assertIn(["nmcli", "connection", "delete", "quak-camap"], fake.calls)
        self.assertTrue(any(c[-3:] == ["HERO11 Black", "ifname", "wlx0"] for c in fake.calls))
        # auto-connect is paused on the adapter before the join and restored last; no profile is edited
        off, on = (
            ["nmcli", "device", "set", "wlx0", "autoconnect", "no"],
            ["nmcli", "device", "set", "wlx0", "autoconnect", "yes"],
        )
        join = next(i for i, c in enumerate(fake.calls) if c[:4] == ["nmcli", "connection", "add", "type"])
        self.assertLess(fake.calls.index(off), join)
        self.assertGreater(fake.calls.index(on), fake.calls.index(["nmcli", "connection", "delete", "quak-camap"]))
        self.assertFalse([c for c in fake.calls if c[:3] == ["nmcli", "connection", "modify"]])

    def test_a_killed_quak_is_cleaned_up_after(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d}):
            fake = FakeNmcli()
            nm = nmcli.NetworkManager(fake)
            self.assertTrue(nm.pause_autoconnect("wlx0", "Home"))
            # killed here. The device now reads "no", but the next pause still knows the real value...
            fake.autoconnect = "no"
            self.assertTrue(nm.pause_autoconnect("wlx0", "Home"))
            # ...and a later run puts everything back, once
            self.assertEqual(nm.recover(), ["wlx0"])
            self.assertIn(["nmcli", "connection", "delete", "quak-camap"], fake.calls)
            self.assertIn(["nmcli", "device", "set", "wlx0", "autoconnect", "yes"], fake.calls)
            self.assertEqual(fake.calls[-1][-3:], ["Home", "ifname", "wlx0"])
            fake.calls.clear()
            self.assertEqual(nm.recover(), [])
            self.assertEqual(fake.calls, [])

    def test_recovery_leaves_a_running_quak_alone(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d}):
            fake = FakeNmcli()
            nm = nmcli.NetworkManager(fake)
            nm.pause_autoconnect("wlx0", "Home")
            fake.calls.clear()
            with mock.patch("quak.net.nmcli._process_alive", return_value=True):
                self.assertEqual(nm.recover(), [])
            self.assertEqual(fake.calls, [])

    def test_recovery_without_a_marker_never_calls_nmcli(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d}):
            fake = FakeNmcli()
            self.assertEqual(nmcli.NetworkManager(fake).recover(), [])
            self.assertEqual(fake.calls, [])

    async def test_it_asks_before_taking_this_computers_network(self):
        cam = SimCamera(latency=0.0005)

        class OnlyAdapter(FakeNmcli):
            def __call__(self, args):
                if args[:3] == ["ip", "route", "get"]:
                    self.calls.append(args)
                    return "1.1.1.1 via 192.168.1.1 dev wlx0 src 192.168.1.2 uid 1000\n"
                return super().__call__(args)

        fake = OnlyAdapter()
        async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
            with mock.patch.dict(os.environ, {"QUAK_WIFI_ADAPTER": ""}), self.assertRaises(AdapterInUse) as caught:
                async with camera_wifi(c, base_url="http://127.0.0.1:9", joiner=nmcli.NetworkManager(fake)):
                    self.fail("it took the adapter anyway")
        self.assertEqual(caught.exception.device, "wlx0")
        self.assertEqual(cam.status[Status.AP_MODE], 0)
        self.assertFalse([c for c in fake.calls if c[:3] == ["nmcli", "connection", "add"]])
        self.assertFalse([c for c in fake.calls if c[:2] == ["nmcli", "device"] and "set" in c])

    def test_adapter_choice(self):
        class TwoAdapters(FakeNmcli):
            def __call__(self, args):
                if args[:5] == ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
                    self.calls.append(args)
                    return "wlan0:wifi:connected:Home\nwlan1:wifi:disconnected:\n"
                if args[:3] == ["ip", "route", "get"]:
                    return "1.1.1.1 via 192.168.1.1 dev wlan0 src 192.168.1.2 uid 1000\n"
                return super().__call__(args)

        nm = nmcli.NetworkManager(TwoAdapters())
        with mock.patch.dict(os.environ, {"QUAK_WIFI_ADAPTER": ""}):
            self.assertEqual(nm.pick_device().name, "wlan1")  # not the one this computer is online through
        with mock.patch.dict(os.environ, {"QUAK_WIFI_ADAPTER": "wlan0"}):
            self.assertEqual(nm.pick_device().name, "wlan0")  # asked for by name
        with (
            mock.patch.dict(os.environ, {"QUAK_WIFI_ADAPTER": "wlan9"}),
            self.assertRaisesRegex(NetworkError, "wlan9 not found"),
        ):
            nm.pick_device()

    def test_no_route_is_normal(self):
        """A laptop in the field has none, and that's when people use a camera."""

        def offline(args):
            raise NetworkError("RTNETLINK answers: Network is unreachable")

        self.assertIsNone(nmcli.NetworkManager(offline).default_route_device())

    def test_passwords_stay_off_command_lines(self):
        seen = {}

        class Watch(FakeNmcli):
            def __call__(self, args):
                if "passwd-file" in args:
                    path = Path(args[args.index("passwd-file") + 1])
                    seen.update(text=path.read_text(), mode=stat.S_IMODE(path.stat().st_mode), path=path)
                return super().__call__(args)

        for bring_up in ("join", "hotspot_up"):
            fake = Watch()
            nm = nmcli.NetworkManager(fake)
            getattr(nm, bring_up)("wlx0", "name", "sEcret\\pw")
            self.assertFalse([c for c in fake.calls if any("sEcret" in a for a in c)])
            self.assertEqual(seen["text"], "802-11-wireless-security.psk:sEcret\\\\pw\n")
            if os.name == "posix":
                self.assertEqual(seen["mode"], 0o600)
            self.assertFalse(seen["path"].exists())

    async def test_phone_joiner_path(self):
        from quak.net.android_wifi import AndroidJoiner

        cam = SimCamera(latency=0.0005)
        port = free_port()
        loop = asyncio.get_running_loop()
        servers, calls = [], []

        class FakeWifi:  # the QuakWifi bridge's contract
            async def join(self, ssid, password, timeout):
                calls.append(("join", ssid, password))
                servers.append(SimHttpServer(cam, loop, port=port))
                servers[-1].start()
                return {"joined": True, "bound": True}

            async def release(self):
                calls.append(("release",))
                return True

        async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
            async with camera_wifi(c, base_url=f"http://127.0.0.1:{port}", joiner=AndroidJoiner(FakeWifi())) as api:
                self.assertIsNone(api.source_ip)
                self.assertEqual(await asyncio.to_thread(api.version), "2.0")
                ssid, password = await c.wifi_credentials()
            await c.wait_status(Status.AP_MODE, lambda v: v == 0, 3, poll=0.1)
        for s in servers:
            s.stop()
        self.assertEqual(calls, [("join", ssid, password), ("release",)])

    async def test_phone_joiner_declined_prompt_is_not_retried(self):
        from quak.net.android_wifi import AndroidJoiner

        cam = SimCamera(latency=0.0005)
        calls = []

        class Declined:
            async def join(self, ssid, password, timeout):
                calls.append("join")
                return {"joined": False, "bound": False}

            async def release(self):
                calls.append("release")
                return True

        async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
            with self.assertRaisesRegex(NetworkError, "Connect prompt"):
                async with camera_wifi(c, base_url=f"http://127.0.0.1:{free_port()}", joiner=AndroidJoiner(Declined())):
                    pass
            await c.wait_status(Status.AP_MODE, lambda v: v == 0, 3, poll=0.1)
        self.assertEqual(calls, ["join", "release"])

    async def test_no_joiner_gives_clear_error(self):
        cam = SimCamera(latency=0.0005)
        async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
            with mock.patch("quak.net.wifi.default_joiner", return_value=None):
                with self.assertRaises(NetworkError):
                    async with camera_wifi(c, base_url=f"http://127.0.0.1:{free_port()}"):
                        pass


class MediaLedgerTests(unittest.TestCase):
    def test_only_files_from_this_session_can_be_deleted(self):
        theirs = MediaFile("100GOPRO", "GX010063.MP4", 1000, 10)
        ours = MediaFile("100GOPRO", "GX010070.MP4", 5000, 10)
        stranger = MediaFile("100GOPRO", "GX010071.MP4", 9000, 10)
        ledger = MediaLedger([theirs])
        ledger.add_window(4990, 5010)
        api = mock.Mock()
        self.assertEqual(ledger.ours([theirs, ours, stranger]), [ours])
        with self.assertRaises(PermissionError):
            ledger.delete_own(api, theirs)
        with self.assertRaises(PermissionError):
            ledger.delete_own(api, stranger)
        ledger.delete_own(api, ours)
        api.delete.assert_called_once_with("100GOPRO/GX010070.MP4")


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class HttpAgainstSim(unittest.IsolatedAsyncioTestCase):
    async def test_media_lifecycle_and_preview(self):
        from quak.net import stream

        cam = SimCamera(latency=0.0005)
        server = SimHttpServer(cam, asyncio.get_running_loop())
        api = CameraHttp(server.start())
        try:
            async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
                baseline = await asyncio.to_thread(api.media_list)
                ledger = MediaLedger(baseline)
                start = camera_epoch((await c.clock())[0])
                await flows.record_clip(c, 1.2, hilight_at=0.5)
                ledger.add_window(start, camera_epoch((await c.clock())[0]))
                files = await asyncio.to_thread(api.media_list)
                mine = ledger.ours(files)
                self.assertEqual(len(mine), 1)
                info = await asyncio.to_thread(api.media_info, mine[0].path)
                self.assertEqual(info["hc"], "1")
                with tempfile.TemporaryDirectory() as d:
                    dest = Path(d) / mine[0].name
                    size, digest, _ = await asyncio.to_thread(api.download, mine[0], dest)
                    self.assertEqual(size, mine[0].size)
                    self.assertEqual(len(digest), 64)
                    probe = await asyncio.to_thread(stream.probe_file, dest)
                    self.assertAlmostEqual(probe["duration"], 1.2, delta=0.6)
                self.assertTrue((await asyncio.to_thread(api.thumbnail, mine[0].path)).startswith(b"\xff\xd8"))
                await asyncio.to_thread(api.turbo, True)
                self.assertEqual(cam.status[Status.TURBO], 1)
                port = free_port()
                await asyncio.to_thread(api.preview_start, port)
                preview = await asyncio.to_thread(stream.probe_udp, port, 10)
                await asyncio.to_thread(api.preview_stop)
                self.assertEqual(preview["video"]["codec"], "h264")
                await asyncio.to_thread(ledger.delete_own, api, mine[0])
                self.assertEqual(await asyncio.to_thread(api.media_list), baseline)
        finally:
            server.stop()


class StreamLeftRunningTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_second_start_is_refused_until_a_stop(self):
        cam = SimCamera(latency=0.0005)
        server = SimHttpServer(cam, asyncio.get_running_loop())
        api = CameraHttp(server.start())
        try:
            await asyncio.to_thread(api.preview_start)
            with self.assertRaises(Rejected):
                await asyncio.to_thread(api.preview_start)
            await asyncio.to_thread(api.preview_stop)
            await asyncio.to_thread(api.preview_start)
            await asyncio.to_thread(api.preview_stop)
        finally:
            server.stop()


class LivestreamFlow(unittest.IsolatedAsyncioTestCase):
    async def test_states_and_cleanup(self):
        cam = SimCamera(latency=0.0005)
        cam.networks.append(SimNetwork("quak-lab-test", "pw123456"))
        events = []
        cam.on_stream = lambda url, on: events.append((url, on))
        async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
            r = await flows.livestream(c, "rtmp://10.42.0.1:1935/live/quak", 0.5, network=("quak-lab-test", "pw123456"))
        self.assertEqual(r.states[-1], "STREAMING")
        self.assertIn("READY", r.states)
        self.assertEqual(r.network_state, "SUCCESS_NEW_AP")
        self.assertEqual(
            events, [("rtmp://10.42.0.1:1935/live/quak", True), ("rtmp://10.42.0.1:1935/live/quak", False)]
        )
        self.assertEqual(cam.status[Status.CONNECTED_SSID], "")  # released
        self.assertEqual(cam.live["state"], 0)

    async def test_wrong_password_and_missing_network(self):
        cam = SimCamera(latency=0.0005)
        async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
            with self.assertRaises(Rejected) as cm:
                await flows.join_camera_network(c, "Home Wi-Fi", "nope")
            self.assertIn("PASSWORD_AUTH", str(cm.exception))
            with self.assertRaises(Rejected):
                await flows.join_camera_network(c, "Nowhere", "x")

    async def test_no_network_fails_cleanly(self):
        cam = SimCamera(latency=0.0005)
        async with open_camera(Target(sim=True, sim_camera=cam, keepalive=None)) as c:
            with self.assertRaises(Rejected) as cm:
                await flows.livestream(c, "rtmp://example/live", 0.5)
            self.assertIn("NETWORK", str(cm.exception))


class NetshTests(unittest.TestCase):
    SHOW = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : connected
    SSID                   : HomeNet
    Profile                : HomeNet
"""

    def test_a_translated_windows_is_understood(self):
        from quak.net import netsh

        show = """
Existe 1 interface no sistema:

    Nome                   : Wi-Fi
    Descrição              : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 12345678-1234-1234-1234-123456789abc
    Endereço físico        : 00:00:00:00:00:00
    Estado                 : conectado
    SSID                   : HomeNet
    AP BSSID               : 00:00:00:00:00:00

    Nome                   : Wi-Fi 2
    Descrição              : Outro adaptador
    GUID                   : abcdefab-1234-1234-1234-123456789abc
    Estado                 : desconectado
"""
        first, second = netsh.parse_interfaces(show)
        self.assertEqual((first.name, first.state, first.connection), ("Wi-Fi", "connected", "HomeNet"))
        self.assertEqual((second.name, second.state, second.connection), ("Wi-Fi 2", "", ""))

    def test_joining_waits_for_an_address(self):
        from quak.net import netsh

        answers = iter(["", "", "Endereço IP:   10.5.5.100\nPrefixo: 10.5.5.0/24"])

        def runner(args):
            return next(answers) if args[:3] == ["netsh", "interface", "ipv4"] else ""

        with mock.patch("quak.net.netsh.time.sleep") as sleep:
            netsh.Netsh(runner).join("Wi-Fi", "GP-SIM", "pw")
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(netsh.Netsh(lambda a: "IP: 10.5.5.100\nPrefixo: 10.5.5.0/24").ipv4("Wi-Fi"), "10.5.5.100")

    def test_parse_and_join_and_restore(self):
        from quak.net import netsh

        calls = []
        seen_xml = []

        def runner(args):
            calls.append(args)
            if args[:3] == ["netsh", "wlan", "show"]:
                return self.SHOW
            if args[:4] == ["netsh", "wlan", "add", "profile"]:
                path = args[4].split("=", 1)[1]
                seen_xml.append(Path(path).read_text())
            if args[:3] == ["netsh", "interface", "ipv4"]:
                return "    IP Address:                           10.5.5.100\n"
            return ""

        n = netsh.Netsh(runner)
        dev = n.pick_device()
        self.assertEqual((dev.name, dev.state, dev.connection), ("Wi-Fi", "connected", "HomeNet"))
        n.join("Wi-Fi", "GP-SIM", "p&ss<word>")
        self.assertIn("<connectionMode>manual</connectionMode>", seen_xml[0])
        self.assertIn("p&amp;ss&lt;word&gt;", seen_xml[0])  # escaped
        added = next(c for c in calls if c[:4] == ["netsh", "wlan", "add", "profile"])
        self.assertFalse(Path(added[4].split("=", 1)[1]).exists())  # the password file is gone
        self.assertIn(["netsh", "wlan", "connect", "name=quak-camap", "ssid=GP-SIM", "interface=Wi-Fi"], calls)
        self.assertEqual(n.ipv4("Wi-Fi"), "10.5.5.100")
        n.down("quak-camap")
        n.activate("HomeNet", "Wi-Fi")
        self.assertIn(["netsh", "wlan", "delete", "profile", "name=quak-camap"], calls)
        with self.assertRaises(NetworkError):
            n.pick_device(need_ap=True)


class RelayTests(unittest.TestCase):
    def test_udp_preview_is_served_over_http(self):
        """The phone's player gets the camera's UDP MPEG-TS through a local HTTP stream."""
        import shutil
        import urllib.request

        from quak.net.stream import UdpHttpRelay

        if not shutil.which("ffmpeg"):
            self.skipTest("needs ffmpeg")
        port = free_port()
        relay = UdpHttpRelay(port)
        url = relay.start()
        push = subprocess.Popen(
            [
                "ffmpeg",
                "-v",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=15",
                "-t",
                "6",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-f",
                "mpegts",
                f"udp://127.0.0.1:{port}?pkt_size=1316",
            ],
            stdin=subprocess.DEVNULL,
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                self.assertEqual(r.headers["Content-Type"], "video/mp2t")
                data = b""
                while len(data) < 100_000:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    data += chunk
            self.assertGreater(relay.packets, 10)
            self.assertEqual(data[0], 0x47, "MPEG-TS sync byte")
        finally:
            push.kill()
            relay.stop()


class LivestreamRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_join_that_stalls_gets_the_cleanup_and_the_retry(self):
        """A camera that never finishes joining: flows cleans up and reports it stuck; the service resets and retries."""
        from quak import flows
        from quak.core import errors
        from quak.service import CameraService

        cam = SimCamera(latency=0.0005)
        svc = CameraService(Target(sim=True, sim_camera=cam, keepalive=None), auto_reconnect=False)
        await svc.connect()
        calls, resets = [], []
        real_release = svc.cam.release_network

        async def stalled_join(c, ssid, password, log=lambda s: None):
            raise errors.Timeout(f"the camera didn't finish joining {ssid!r} within 60 s")

        async def release():
            calls.append("release")
            return await real_release()

        with (
            mock.patch.object(svc.cam, "release_network", release),
            mock.patch.object(flows, "join_camera_network", stalled_join),
        ):
            with self.assertRaises(flows.LivestreamStuck):
                await flows.livestream(svc.cam, "rtmp://10.42.0.1/live/x", 1, network=("lab", "pw"))
        self.assertEqual(calls, ["release"], "a failed join must still run the cleanup")

        results = [flows.LivestreamStuck("stalled"), "streamed"]

        async def fake_flow(*a, **k):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        async def fake_reset():
            resets.append(1)

        with mock.patch.object(flows, "livestream", fake_flow), mock.patch.object(svc, "reset_camera", fake_reset):
            self.assertEqual(await svc.livestream("rtmp://x", 1), "streamed")
        self.assertEqual(resets, [1])
        await svc.disconnect()
        await cam.close()


class SharedCameraWifiTests(unittest.IsolatedAsyncioTestCase):
    """Two joins of one adapter interrupt each other, so CameraService must never run them together."""

    async def asyncSetUp(self):
        self.svc = CameraService(Target(address="AA", sim=False))
        self.svc.cam = object()
        self.active = self.peak = self.joins = self.leaves = 0

        @contextlib.asynccontextmanager
        async def fake_camera_wifi(cam, **options):
            self.joins += 1
            await self._enter()
            await asyncio.sleep(0.01)
            await self._exit()
            try:
                yield object()
            finally:
                self.leaves += 1
                await self._enter()
                await asyncio.sleep(0.01)
                await self._exit()

        patcher = mock.patch("quak.net.wifi.camera_wifi", fake_camera_wifi)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _enter(self):
        self.active += 1
        self.peak = max(self.peak, self.active)

    async def _exit(self):
        self.active -= 1

    async def test_concurrent_callers_share_one_join(self):
        first, second, third = await asyncio.gather(self.svc.http(), self.svc.http(), self.svc.http())
        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertEqual(self.joins, 1)

    async def test_a_join_waits_for_a_leave_in_progress(self):
        await self.svc.http()
        await asyncio.gather(self.svc.release_http(), self.svc.http())
        self.assertEqual((self.joins, self.leaves), (2, 1))
        self.assertEqual(self.peak, 1)


class PreviewAroundRecordingTests(unittest.IsolatedAsyncioTestCase):
    """A recording ends the camera's preview stream, and only starting it again brings it back."""

    async def asyncSetUp(self):
        self.svc = CameraService(Target(address="AA", sim=False))
        self.api = mock.Mock()
        self.svc._http = self.api
        self.real_sleep = asyncio.sleep  # patching asyncio.sleep changes it everywhere, including here
        patcher = mock.patch("quak.service.asyncio.sleep", lambda s: self.real_sleep(0))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def settle(self):
        """Wait out the restart task, which does its HTTP calls in threads."""
        await self.real_sleep(0.3)

    async def test_stream_restarts_when_recording_stops(self):
        self.svc._preview_port = 8554
        self.svc._apply_statuses({Status.ENCODING: 1})
        await self.settle()
        self.api.preview_start.assert_not_called()  # a HERO11 can't stream during a recording
        self.svc._apply_statuses({Status.ENCODING: 0})
        await self.settle()
        self.assertEqual(self.api.preview_start.call_count, 1)

    async def test_other_status_changes_leave_the_stream_alone(self):
        self.svc._preview_port = 8554
        self.svc._apply_statuses({Status.ENCODING: 0})
        self.svc._apply_statuses({Status.ENCODING: 0, Status.BATTERY_PERCENT: 50})
        await self.settle()
        self.api.preview_start.assert_not_called()

    async def test_no_preview_nothing_to_restart(self):
        self.svc._apply_statuses({Status.ENCODING: 1})
        self.svc._apply_statuses({Status.ENCODING: 0})
        await self.settle()
        self.api.preview_start.assert_not_called()

    async def test_retries_while_the_camera_is_busy(self):
        self.svc._preview_port = 8554
        self.svc._apply_statuses({Status.ENCODING: 1})
        self.api.preview_start.side_effect = [Rejected("stream", "409"), Rejected("stream", "409"), None]
        self.svc._apply_statuses({Status.ENCODING: 0})
        await self.settle()
        self.assertEqual(self.api.preview_start.call_count, 3)

    async def test_starting_preview_clears_a_stream_left_running(self):
        self.api.preview_start.side_effect = [Rejected("stream", "409"), None]
        self.assertEqual(await self.svc.preview_start(), "udp://@:8554")
        self.assertEqual(self.api.preview_start.call_count, 2)
        self.api.preview_stop.assert_called_once()  # a stream left running by a killed app only clears this way

    async def test_a_camera_that_stays_busy_is_reported(self):
        self.api.preview_start.side_effect = Rejected("stream", "409")
        with self.assertRaises(Rejected):
            await self.svc.preview_start()
        self.assertIsNone(self.svc._preview_port)

    async def test_stopping_preview_cancels_a_pending_restart(self):
        self.svc._preview_port = 8554
        self.svc._apply_statuses({Status.ENCODING: 1})
        self.svc._apply_statuses({Status.ENCODING: 0})
        await self.svc.preview_stop()
        await self.settle()
        self.api.preview_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

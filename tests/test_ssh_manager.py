"""``SshManager``: routing, the Wi-Fi hold, the host-key policy, key selection and the OpenSSH hand-off.

Nothing real runs behind it: ``system_ssh``, the scan, the probes and the network context are fakes,
and ``subprocess.run`` and ``socket.create_connection`` fail the test if anything reaches them.
Everything on disk runs against an isolated ``QUAK_CONFIG_DIR``.
"""

import base64
import contextlib
import inspect
import json
import os
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from quak import config
from quak.core.errors import AdapterInUse, HostKeyError, NetworkError, SshError, SshKeyError
from quak.lab.remote import SshRemote
from quak.ssh import arm as sc_arm
from quak.ssh import hostkeys, keys, manager, profiles, store, system_ssh, transport
from quak.ssh.manager import SshManager, camera_network
from quak.ssh.models import ArmResult, Endpoint, ExecResult, HostKey, SshKey, SshProfile, TestResult

ADDRESS = "AA:BB:CC:DD:EE:FF"
HOST = "192.168.1.50"
AP_HOST = transport.AP_HOST
BANNER = "SSH-2.0-dropbear_2022.83"
BLOB = b"camera-host-key"
OTHER_BLOB = b"another-host-key"
PEM_BODY = "c2VjcmV0LWtleS1ib2R5"
PEM = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{PEM_BODY}\n-----END OPENSSH PRIVATE KEY-----\n"
PASSWORD = "correct horse battery"


def scanned(host: str = HOST, port: int = 22, blob: bytes = BLOB) -> HostKey:
    """What ``hostkeys.scan`` returns for this host and key."""
    encoded = base64.b64encode(blob).decode("ascii")
    return HostKey(host, port, "ssh-ed25519", hostkeys.fingerprint(blob), line=f"{host} ssh-ed25519 {encoded}")


def offered(host: str = HOST, port: int = 22, blob: bytes = BLOB) -> HostKey:
    """What the app's Dart scan returns for this host and key: no known_hosts line."""
    return HostKey(host, port, "ssh-ed25519", hostkeys.fingerprint(blob))


def pin(host: str = HOST, port: int = 22, blob: bytes = BLOB) -> HostKey:
    key = scanned(host, port, blob)
    hostkeys.add_host_key(key)
    hostkeys.remember_fingerprint(host, port, key.key_type, key.fingerprint)
    return key


def save_profile(**fields) -> SshProfile:
    profile = SshProfile(address=ADDRESS, **fields)
    profiles.save(profile)
    return profile


def store_key(key_id: str = "k_main", created: str = "2026-01-01T00:00:00") -> SshKey:
    blob = key_id.encode()
    return keys._put_key(
        SshKey(
            id=key_id,
            label=key_id,
            type="ssh-ed25519",
            private_file=f"keys/{key_id}.pem",
            public_key=keys.public_line("ssh-ed25519", blob, "me@laptop"),
            fingerprint=hostkeys.fingerprint(blob),
            created=created,
        ),
        PEM,
    )


class FakeNetwork:
    """The injected network context: who asked for it, and whether it is held right now."""

    def __init__(self):
        self.entered = []
        self.held = False

    @contextlib.asynccontextmanager
    async def __call__(self, profile):
        self.entered.append(profile.address)
        self.held = True
        try:
            yield
        finally:
            self.held = False


@dataclass
class SshCall:
    name: str
    profile: SshProfile
    endpoint: Endpoint
    key_path: Path | None
    extra: tuple
    network_held: bool
    thread: threading.Thread
    options: dict = field(default_factory=dict)


class FakeSsh:
    """Stands in for the ``system_ssh`` module and holds calls to its real signatures."""

    def __init__(self, network: FakeNetwork):
        self.network = network
        self.calls: list[SshCall] = []
        self.test_result = TestResult(ok=True, uid="uid=0(root)", uname="Linux 4.9 armv7l")
        self.exec_result = ExecResult(exit_code=3, stdout=b"out", stderr=b"err")
        self.terminal_exit = 7
        self.error: Exception | None = None
        self.on_call = lambda: None

    def _record(self, name, profile, endpoint, key_path, *extra, **options):
        inspect.signature(getattr(system_ssh, name)).bind(profile, endpoint, key_path, *extra, **options)
        self.calls.append(
            SshCall(name, profile, endpoint, key_path, extra, self.network.held, threading.current_thread(), options)
        )
        self.on_call()
        if self.error:
            raise self.error

    def test(self, profile, endpoint, key_path, **options):
        self._record("test", profile, endpoint, key_path, **options)
        return self.test_result

    def exec_command(self, profile, endpoint, key_path, command, **options):
        self._record("exec_command", profile, endpoint, key_path, command, **options)
        return self.exec_result

    def read_file(self, profile, endpoint, key_path, remote, **options):
        self._record("read_file", profile, endpoint, key_path, remote, **options)
        return b"contents"

    def write_file(self, profile, endpoint, key_path, remote, data, **options):
        self._record("write_file", profile, endpoint, key_path, remote, data, **options)

    def open_terminal(self, profile, endpoint, key_path):
        self._record("open_terminal", profile, endpoint, key_path)
        return self.terminal_exit


class ManagerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_dir = Path(tmp.name) / "config"
        for patch in (
            mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": str(self.config_dir)}),
            mock.patch("subprocess.run", side_effect=AssertionError("a test reached subprocess.run")),
            mock.patch("socket.create_connection", side_effect=AssertionError("a test reached the network")),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.network = FakeNetwork()
        self.ssh = FakeSsh(self.network)
        self.scan = mock.create_autospec(hostkeys.scan, side_effect=lambda host, port: scanned(host, port))
        self.banner = mock.create_autospec(sc_arm.ssh_banner, return_value=(True, BANNER))
        self.reachable = mock.create_autospec(transport.is_reachable, return_value=False)
        self.arm_request = mock.create_autospec(
            sc_arm.arm, return_value=ArmResult(http_status=200, accepted=True, ssh_up=True, banner=BANNER, message="up")
        )
        self.usb_nets = []
        self.manager = self.make_manager()

    def make_manager(self, **overrides) -> SshManager:
        deps = {
            "network_context": self.network,
            "ssh": self.ssh,
            "scan": self.scan,
            "banner": self.banner,
            "reachable": self.reachable,
            "arm_request": self.arm_request,
            "find_nets": lambda: self.usb_nets,
        }
        return SshManager(**{**deps, **overrides})

    def ready(self, transport: str = "host", **fields) -> SshProfile:
        """A saved profile with a stored key and, on the route it will take, a pinned host key."""
        host = fields.setdefault("host", HOST) if transport == "host" else fields.get("host")
        profile = save_profile(transport=transport, **fields)
        store_key()
        pin(host or AP_HOST, profile.ssh_port)
        return profile

    def operations(self) -> dict:
        """Each operation that logs in, by the name of the ``system_ssh`` function it ends up in."""
        return {
            "test": lambda: self.manager.test(ADDRESS),
            "exec_command": lambda: self.manager.exec(ADDRESS, "uptime"),
            "open_terminal": lambda: self.manager.open_terminal(ADDRESS),
        }


class ProfileTests(ManagerCase):
    def test_a_new_profile_takes_its_label_from_the_saved_camera(self):
        config.remember_camera(ADDRESS, model="HERO11 Black", firmware="H22.01")
        profile = self.manager.profile(ADDRESS.lower())
        self.assertEqual(profile.address, ADDRESS)
        self.assertEqual(profile.label, "HERO11 Black")

    def test_a_saved_profile_is_returned(self):
        saved = save_profile(transport="usb", user="admin")
        self.assertEqual(self.manager.profile(ADDRESS), saved)

    def test_an_invalid_saved_profile_is_an_ssh_error(self):
        store.write({"profiles": {ADDRESS: {"transport": "carrier-pigeon"}}})
        with self.assertRaisesRegex(SshError, "not valid.*profile forget"):
            self.manager.profile(ADDRESS)


class StatusTests(ManagerCase):
    async def test_a_wifi_route_never_enters_the_network_context(self):
        save_profile(transport="wifi")
        status = await self.manager.status(ADDRESS)
        self.assertEqual(self.network.entered, [])
        self.assertEqual(status.endpoint.host, AP_HOST)
        self.assertEqual(status.endpoint.transport, "wifi")
        self.assertTrue(status.ssh_up)
        self.assertEqual(status.banner, BANNER)

    async def test_auto_falling_back_to_wifi_never_enters_the_network_context(self):
        save_profile(transport="auto")
        status = await self.manager.status(ADDRESS)
        self.assertEqual(self.network.entered, [])
        self.assertEqual(status.endpoint.transport, "wifi")

    async def test_the_real_default_context_is_not_used_either(self):
        save_profile(transport="wifi")
        with mock.patch.object(manager, "open_camera", side_effect=AssertionError("opened BLE")):
            status = await self.make_manager(network_context=None).status(ADDRESS)
        self.assertEqual(status.endpoint.transport, "wifi")

    async def test_ssh_up_reports_the_scanned_key_and_its_state(self):
        for state, blob_pinned in (("unknown", None), ("trusted", BLOB), ("changed", OTHER_BLOB)):
            with self.subTest(state=state):
                hostkeys.forget_fingerprint(HOST, 22)
                save_profile(transport="host", host=HOST)
                if blob_pinned:
                    pin(blob=blob_pinned)
                status = await self.manager.status(ADDRESS)
                self.assertEqual(status.host_key_state, state)
                self.assertEqual(status.host_key.fingerprint, hostkeys.fingerprint(BLOB))
                self.assertTrue(status.reachable)
                self.assertEqual(status.last_error, "")

    async def test_ssh_down_is_not_scanned_and_reachability_comes_from_http(self):
        save_profile(transport="host", host=HOST)
        self.banner.return_value = (False, "")
        self.reachable.side_effect = lambda host, port: (host, port) == (HOST, 80)
        status = await self.manager.status(ADDRESS)
        self.assertFalse(status.ssh_up)
        self.assertTrue(status.reachable)
        self.assertEqual(status.banner, "")
        self.scan.assert_not_called()
        self.assertEqual(status.host_key_state, "unknown")
        self.assertIsNone(status.host_key)

    async def test_a_camera_answering_only_on_8080_is_reachable(self):
        save_profile(transport="host", host=HOST)
        self.banner.return_value = (False, "")
        self.reachable.side_effect = lambda host, port: port == 8080
        self.assertTrue((await self.manager.status(ADDRESS)).reachable)

    async def test_nothing_answering_is_unreachable(self):
        save_profile(transport="wifi")
        self.banner.return_value = (False, "")
        status = await self.manager.status(ADDRESS)
        self.assertFalse(status.reachable)
        self.assertFalse(status.ssh_up)

    async def test_ssh_down_with_a_pin_says_so_without_comparing(self):
        save_profile(transport="host", host=HOST)
        pinned = pin()
        self.banner.return_value = (False, "")
        status = await self.manager.status(ADDRESS)
        self.assertEqual(status.host_key_state, "trusted")
        self.assertEqual(status.host_key.fingerprint, pinned.fingerprint)
        self.assertFalse(status.ssh_up)

    async def test_something_other_than_ssh_on_the_port_is_reachable_but_not_a_banner(self):
        save_profile(transport="host", host=HOST)
        self.banner.return_value = (False, "HTTP/1.0 400 Bad Request")
        status = await self.manager.status(ADDRESS)
        self.assertTrue(status.reachable)
        self.assertFalse(status.ssh_up)
        self.assertEqual(status.banner, "")

    async def test_a_failed_scan_lands_in_last_error(self):
        save_profile(transport="host", host=HOST)
        self.scan.side_effect = TimeoutError("ssh-keyscan timed out")
        status = await self.manager.status(ADDRESS)
        self.assertIn("ssh-keyscan timed out", status.last_error)
        self.assertEqual(status.host_key_state, "unknown")

    async def test_a_route_that_cannot_be_resolved_lands_in_last_error(self):
        save_profile(transport="usb")
        status = await self.manager.status(ADDRESS)
        self.assertIsNone(status.endpoint)
        self.assertFalse(status.reachable)
        self.assertIn("no camera USB network", status.last_error)


class RoutingTests(ManagerCase):
    async def test_a_host_route_uses_no_network_context(self):
        self.ready(ssh_port=2222)
        for name, run in self.operations().items():
            with self.subTest(name):
                await run()
                call = self.ssh.calls[-1]
                self.assertEqual(call.name, name)
                self.assertEqual((call.endpoint.host, call.endpoint.ssh_port), (HOST, 2222))
                self.assertFalse(call.network_held)
        self.assertEqual(self.network.entered, [])

    async def test_a_usb_route_uses_no_network_context(self):
        self.usb_nets = [transport.UsbNet("usb0", "172.21.145.50", transport.CAMERA_USB_VID, "0049")]
        self.reachable.side_effect = lambda host, port: (host, port) == ("172.21.145.51", 80)
        save_profile(transport="usb")
        store_key()
        pin("172.21.145.51")
        await self.manager.test(ADDRESS)
        self.assertEqual(self.ssh.calls[-1].endpoint.host, "172.21.145.51")
        self.assertEqual(self.ssh.calls[-1].endpoint.interface, "usb0")
        self.assertEqual(self.network.entered, [])

    async def test_a_wifi_route_holds_the_network_for_the_whole_operation(self):
        self.ready(transport="wifi")
        for name, run in self.operations().items():
            with self.subTest(name):
                self.network.entered.clear()
                await run()
                self.assertEqual(self.ssh.calls[-1].name, name)
                self.assertTrue(self.ssh.calls[-1].network_held)
                self.assertEqual(self.ssh.calls[-1].endpoint.host, AP_HOST)
                self.assertEqual(self.network.entered, [ADDRESS])
                self.assertFalse(self.network.held)

    async def test_auto_resolving_to_wifi_holds_the_network(self):
        self.ready(transport="auto")
        await self.manager.test(ADDRESS)
        self.assertTrue(self.ssh.calls[-1].network_held)
        self.assertEqual(self.ssh.calls[-1].endpoint.transport, "wifi")

    async def test_auto_with_an_explicit_host_that_answers_stays_off_wifi(self):
        self.ready(transport="auto", host=HOST)
        self.reachable.side_effect = lambda host, port: (host, port) == (HOST, 22)
        await self.manager.test(ADDRESS)
        self.assertEqual(self.ssh.calls[-1].endpoint.host, HOST)
        self.assertEqual(self.network.entered, [])

    async def test_the_network_is_released_when_ssh_fails(self):
        self.ready(transport="wifi")
        self.ssh.error = NetworkError("ssh: timed out")
        with self.assertRaisesRegex(NetworkError, "timed out"):
            await self.manager.exec(ADDRESS, "uptime")
        self.assertEqual(self.network.entered, [ADDRESS])
        self.assertFalse(self.network.held)

    async def test_a_network_failure_reaches_the_caller_and_stops_the_operation(self):
        self.ready(transport="wifi")

        @contextlib.asynccontextmanager
        async def busy(profile):
            raise AdapterInUse("wlan0")
            yield

        with self.assertRaises(AdapterInUse):
            await self.make_manager(network_context=busy).test(ADDRESS)
        self.scan.assert_not_called()
        self.assertEqual(self.ssh.calls, [])

    async def test_blocking_work_stays_off_the_event_loop(self):
        """A BLE session held for Wi-Fi dies if its keep-alive stops, so ssh and scan must not block the loop."""
        self.ready(transport="wifi")
        loop_thread = threading.current_thread()
        scan_threads = []

        def scan(host, port):
            scan_threads.append(threading.current_thread())
            return scanned(host, port)

        run = self.make_manager(scan=scan)
        await run.test(ADDRESS)
        await run.open_terminal(ADDRESS)
        self.assertNotIn(loop_thread, scan_threads)
        self.assertNotIn(loop_thread, [call.thread for call in self.ssh.calls])


class DefaultNetworkContextTests(ManagerCase):
    def fake_camera_stack(self):
        """Stand-ins for ``open_camera`` and ``camera_wifi`` that log the order things happen in."""
        events = []
        cam = object()

        @contextlib.asynccontextmanager
        async def open_camera(target):
            events.append(f"ble up {target.address}")
            try:
                yield cam
            finally:
                events.append("ble down")

        @contextlib.asynccontextmanager
        async def camera_wifi(held_cam):
            self.assertIs(held_cam, cam)
            events.append("wifi up")
            try:
                yield
            finally:
                events.append("wifi down")

        for patch in (
            mock.patch.object(manager, "open_camera", open_camera),
            mock.patch.object(manager, "camera_wifi", camera_wifi),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return events

    async def test_it_opens_ble_and_holds_camera_wifi_around_the_operation(self):
        events = self.fake_camera_stack()
        self.ready(transport="wifi")
        self.ssh.on_call = lambda: events.append("ssh")
        await self.make_manager(network_context=None).test(ADDRESS)
        self.assertEqual(events, [f"ble up {ADDRESS}", "wifi up", "ssh", "wifi down", "ble down"])

    async def test_it_comes_down_in_order_when_the_block_raises(self):
        events = self.fake_camera_stack()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with camera_network(SshProfile(address=ADDRESS)):
                raise RuntimeError("boom")
        self.assertEqual(events, [f"ble up {ADDRESS}", "wifi up", "wifi down", "ble down"])

    async def test_a_host_route_touches_neither(self):
        events = self.fake_camera_stack()
        self.ready()
        await self.make_manager(network_context=None).test(ADDRESS)
        self.assertEqual(events, [])


class ArmTests(ManagerCase):
    async def test_a_host_route_sends_sc_to_the_endpoint_without_the_network(self):
        save_profile(transport="host", host=HOST, http_port=8081, ssh_port=2222)
        result = await self.manager.arm(ADDRESS)
        self.arm_request.assert_awaited_once_with(HOST, 8081, 2222)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(self.network.entered, [])

    async def test_a_wifi_route_holds_the_network_during_the_request(self):
        save_profile(transport="wifi")
        held = []

        async def request(host, http_port, ssh_port):
            held.append((host, self.network.held))
            return ArmResult(http_status=200, accepted=True)

        await self.make_manager(arm_request=request).arm(ADDRESS)
        self.assertEqual(held, [(AP_HOST, True)])
        self.assertFalse(self.network.held)

    async def test_arming_needs_no_key_and_no_host_key(self):
        save_profile(transport="host", host=HOST)
        await self.manager.arm(ADDRESS)
        self.scan.assert_not_called()
        self.assertEqual(self.ssh.calls, [])

    async def test_an_accepted_request_stamps_the_saved_profile(self):
        save_profile(transport="host", host=HOST)
        await self.manager.arm(ADDRESS)
        self.assertTrue(profiles.load(ADDRESS).last_arm)

    async def test_a_refused_request_comes_back_in_the_result_and_stamps_nothing(self):
        save_profile(transport="host", host=HOST)
        self.arm_request.return_value = ArmResult(http_status=500, accepted=False, message="SC was not accepted")
        result = await self.manager.arm(ADDRESS)
        self.assertFalse(result.accepted)
        self.assertEqual(result.message, "SC was not accepted")
        self.assertIsNone(profiles.load(ADDRESS).last_arm)

    async def test_an_unsaved_profile_stays_unsaved(self):
        await self.manager.arm(ADDRESS)
        self.assertIsNone(profiles.load(ADDRESS))

    async def test_auto_keeps_an_explicit_host_whose_ssh_is_still_down(self):
        """Port 22 only opens once armed, so judging the host by it would wrongly fall through to Wi-Fi."""
        save_profile(transport="auto", host=HOST)
        self.reachable.side_effect = lambda host, port: (host, port) == (HOST, 80)
        await self.manager.arm(ADDRESS)
        self.arm_request.assert_awaited_once_with(HOST, 80, 22)
        self.assertEqual(self.network.entered, [])

    async def test_auto_leaves_an_explicit_host_that_does_not_answer_at_all(self):
        save_profile(transport="auto", host=HOST)
        await self.manager.arm(ADDRESS)
        self.arm_request.assert_awaited_once_with(AP_HOST, 80, 22)
        self.assertEqual(self.network.entered, [ADDRESS])


class HostKeyPolicyTests(ManagerCase):
    def setUp(self):
        super().setUp()
        save_profile(transport="host", host=HOST)
        store_key()

    async def test_an_unknown_key_stops_every_login_and_names_the_fingerprint(self):
        for name, run in self.operations().items():
            with self.subTest(name):
                with self.assertRaises(HostKeyError) as caught:
                    await run()
                self.assertIn(hostkeys.fingerprint(BLOB), str(caught.exception))
                self.assertIn("quak ssh host trust", str(caught.exception))
        self.assertEqual(self.ssh.calls, [])
        self.assertEqual(hostkeys.state(HOST, 22), "unknown")

    async def test_a_changed_key_stops_every_login_and_keeps_the_pin(self):
        pinned = pin(blob=OTHER_BLOB)
        for name, run in self.operations().items():
            with self.subTest(name):
                with self.assertRaises(HostKeyError) as caught:
                    await run()
                self.assertIn(pinned.fingerprint, str(caught.exception))
                self.assertIn(hostkeys.fingerprint(BLOB), str(caught.exception))
                self.assertIn("quak ssh host forget", str(caught.exception))
        self.assertEqual(self.ssh.calls, [])
        self.assertEqual(hostkeys.stored_fingerprint(HOST, 22).fingerprint, pinned.fingerprint)
        self.assertEqual(hostkeys.lookup_known_hosts(HOST, 22).fingerprint, pinned.fingerprint)

    async def test_a_failed_scan_fails_closed(self):
        pin()
        for failure in (FileNotFoundError("no ssh-keyscan"), TimeoutError("timed out"), ConnectionError("no key")):
            with self.subTest(type(failure).__name__):
                self.scan.side_effect = failure
                with self.assertRaisesRegex(SshError, "quak ssh arm") as caught:
                    await self.manager.test(ADDRESS)
                self.assertNotIsInstance(caught.exception, HostKeyError)
        self.assertEqual(self.ssh.calls, [])

    async def test_a_matching_pin_lets_the_login_through(self):
        pin()
        result = await self.manager.test(ADDRESS)
        self.assertTrue(result.ok)
        self.assertEqual(self.scan.call_args.args, (HOST, 22))

    async def test_a_fingerprint_only_pin_reaches_known_hosts_before_ssh_runs(self):
        """The app pins only a fingerprint, but the system ssh enforces known_hosts."""
        key = scanned()
        hostkeys.remember_fingerprint(HOST, 22, key.key_type, key.fingerprint)
        listed = []
        self.ssh.on_call = lambda: listed.append(hostkeys.lookup_known_hosts(HOST, 22))
        await self.manager.test(ADDRESS)
        self.assertEqual([entry.fingerprint for entry in listed], [key.fingerprint])

    async def test_a_stale_known_hosts_line_behind_a_matching_pin_is_replaced(self):
        hostkeys.add_host_key(scanned(blob=OTHER_BLOB))
        key = scanned()
        hostkeys.remember_fingerprint(HOST, 22, key.key_type, key.fingerprint)
        await self.manager.test(ADDRESS)
        self.assertEqual(hostkeys.lookup_known_hosts(HOST, 22).fingerprint, key.fingerprint)

    async def test_a_known_hosts_only_pin_is_honoured(self):
        hostkeys.add_host_key(scanned())
        self.assertTrue((await self.manager.test(ADDRESS)).ok)
        hostkeys.add_host_key(scanned(blob=OTHER_BLOB))
        with self.assertRaisesRegex(HostKeyError, "has changed"):
            await self.manager.test(ADDRESS)

    async def test_scan_host_key_returns_the_key_and_pins_nothing(self):
        key = await self.manager.scan_host_key(ADDRESS)
        self.assertEqual(key.fingerprint, hostkeys.fingerprint(BLOB))
        self.assertEqual(hostkeys.state(HOST, 22), "unknown")

    async def test_scan_host_key_failure_is_an_ssh_error(self):
        self.scan.side_effect = ConnectionError("no host key")
        with self.assertRaisesRegex(SshError, "no host key"):
            await self.manager.scan_host_key(ADDRESS)

    async def test_scanning_and_trusting_hold_a_wifi_route(self):
        save_profile(transport="wifi")
        held = []

        def scan(host, port):
            held.append((host, self.network.held))
            return scanned(host, port)

        run = self.make_manager(scan=scan)
        await run.scan_host_key(ADDRESS)
        await run.trust_host(ADDRESS)
        self.assertEqual(held, [(AP_HOST, True), (AP_HOST, True)])
        self.assertFalse(self.network.held)

    async def test_trust_pins_the_fingerprint_and_writes_known_hosts(self):
        pinned = await self.manager.trust_host(ADDRESS)
        fingerprint = hostkeys.fingerprint(BLOB)
        self.assertEqual(pinned.fingerprint, fingerprint)
        self.assertEqual(hostkeys.stored_fingerprint(HOST, 22).fingerprint, fingerprint)
        self.assertEqual(hostkeys.lookup_known_hosts(HOST, 22).fingerprint, fingerprint)
        self.assertTrue((await self.manager.test(ADDRESS)).ok)

    async def test_trust_formats_a_non_default_port_for_known_hosts(self):
        save_profile(transport="host", host=HOST, ssh_port=2222)
        await self.manager.trust_host(ADDRESS)
        self.assertTrue(hostkeys.known_hosts_path().read_text().startswith(f"[{HOST}]:2222 ssh-ed25519 "))
        self.assertEqual(hostkeys.state(HOST, 2222, scanned(HOST, 2222)), "trusted")

    async def test_trust_is_repeatable(self):
        await self.manager.trust_host(ADDRESS)
        await self.manager.trust_host(ADDRESS)
        self.assertEqual(len(hostkeys.known_hosts_path().read_text().splitlines()), 1)

    async def test_trust_accepts_the_fingerprint_that_was_reviewed(self):
        fingerprint = hostkeys.fingerprint(BLOB)
        pinned = await self.manager.trust_host(ADDRESS, fingerprint=fingerprint)
        self.assertEqual(pinned.fingerprint, fingerprint)

    async def test_trust_refuses_a_key_other_than_the_one_reviewed(self):
        reviewed = hostkeys.fingerprint(OTHER_BLOB)
        with self.assertRaisesRegex(HostKeyError, "Nothing was pinned"):
            await self.manager.trust_host(ADDRESS, fingerprint=reviewed)
        self.assertEqual(hostkeys.state(HOST, 22), "unknown")
        self.assertFalse(hostkeys.known_hosts_path().exists())

    async def test_trust_will_not_replace_a_pin_that_no_longer_matches(self):
        pinned = pin(blob=OTHER_BLOB)
        with self.assertRaisesRegex(HostKeyError, "quak ssh host forget"):
            await self.manager.trust_host(ADDRESS)
        self.assertEqual(hostkeys.stored_fingerprint(HOST, 22).fingerprint, pinned.fingerprint)
        self.assertEqual(hostkeys.lookup_known_hosts(HOST, 22).fingerprint, pinned.fingerprint)

    async def test_forget_then_trust_accepts_a_regenerated_key(self):
        pin(blob=OTHER_BLOB)
        await self.manager.forget_host(ADDRESS)
        pinned = await self.manager.trust_host(ADDRESS)
        self.assertEqual(pinned.fingerprint, hostkeys.fingerprint(BLOB))

    async def test_forget_removes_both_and_says_whether_there_was_anything(self):
        pin()
        self.assertTrue(await self.manager.forget_host(ADDRESS))
        self.assertIsNone(hostkeys.stored_fingerprint(HOST, 22))
        self.assertIsNone(hostkeys.lookup_known_hosts(HOST, 22))
        self.assertFalse(await self.manager.forget_host(ADDRESS))
        with self.assertRaisesRegex(HostKeyError, "not trusted yet"):
            await self.manager.test(ADDRESS)

    async def test_forget_counts_a_known_hosts_only_entry(self):
        hostkeys.add_host_key(scanned())
        self.assertTrue(await self.manager.forget_host(ADDRESS))
        self.assertIsNone(hostkeys.lookup_known_hosts(HOST, 22))

    async def test_forget_on_a_wifi_route_joins_nothing_and_scans_nothing(self):
        save_profile(transport="wifi")
        pin(AP_HOST)
        self.assertTrue(await self.manager.forget_host(ADDRESS))
        self.assertEqual(self.network.entered, [])
        self.scan.assert_not_called()

    async def test_forget_on_auto_finds_the_explicit_host_without_needing_it_to_answer(self):
        save_profile(transport="auto", host=HOST)
        pin()
        self.assertTrue(await self.manager.forget_host(ADDRESS))
        self.assertIsNone(hostkeys.known(HOST, 22))


class KeySelectionTests(ManagerCase):
    def assert_nothing_started(self):
        self.assertEqual(self.network.entered, [])
        self.scan.assert_not_called()
        self.assertEqual(self.ssh.calls, [])

    async def test_the_profile_key_wins(self):
        store_key("k_first", created="2026-01-01T00:00:00")
        chosen = store_key("k_second", created="2026-01-02T00:00:00")
        save_profile(transport="host", host=HOST, key_id=chosen.id)
        pin()
        await self.manager.test(ADDRESS)
        self.assertEqual(self.ssh.calls[-1].key_path, keys.key_path(chosen))

    async def test_without_a_profile_key_the_oldest_stored_key_is_used(self):
        store_key("k_second", created="2026-01-02T00:00:00")
        first = store_key("k_first", created="2026-01-01T00:00:00")
        save_profile(transport="host", host=HOST)
        pin()
        await self.manager.exec(ADDRESS, "uptime")
        self.assertEqual(self.ssh.calls[-1].key_path, keys.key_path(first))

    async def test_a_profile_key_that_is_gone_does_not_fall_back_to_another(self):
        store_key("k_other")
        save_profile(transport="wifi", key_id="k_gone")
        for name, run in self.operations().items():
            with self.subTest(name):
                with self.assertRaisesRegex(SshKeyError, "k_gone"):
                    await run()
        self.assert_nothing_started()

    async def test_no_stored_key_says_how_to_get_one(self):
        save_profile(transport="wifi")
        with self.assertRaisesRegex(SshKeyError, "quak ssh key generate"):
            await self.manager.test(ADDRESS)
        self.assert_nothing_started()

    async def test_a_missing_private_file_is_reported_before_any_login(self):
        key = store_key()
        keys.key_path(key).unlink()
        save_profile(transport="wifi")
        with self.assertRaisesRegex(SshKeyError, "k_main.*missing"):
            await self.manager.test(ADDRESS)
        self.assert_nothing_started()

    async def test_auth_none_logs_in_without_a_key(self):
        save_profile(transport="host", host=HOST, auth="none", key_id="k_ignored")
        pin()
        for name, run in self.operations().items():
            with self.subTest(name):
                await run()
                self.assertIsNone(self.ssh.calls[-1].key_path)

    async def test_auth_password_refuses_batch_commands_before_touching_the_camera(self):
        save_profile(transport="wifi", auth="password")
        for run in (self.manager.test(ADDRESS), self.manager.exec(ADDRESS, "uptime")):
            with self.assertRaisesRegex(SshError, "interactive terminal") as caught:
                await run
            self.assertNotIsInstance(caught.exception, (HostKeyError, SshKeyError))
        self.assert_nothing_started()

    async def test_auth_password_opens_the_terminal_without_a_key(self):
        store_key()
        save_profile(transport="host", host=HOST, auth="password")
        pin()
        self.assertEqual(await self.manager.open_terminal(ADDRESS), self.ssh.terminal_exit)
        self.assertIsNone(self.ssh.calls[-1].key_path)

    async def test_auth_password_still_enforces_the_host_key(self):
        save_profile(transport="host", host=HOST, auth="password")
        with self.assertRaises(HostKeyError):
            await self.manager.open_terminal(ADDRESS)
        self.assertEqual(self.ssh.calls, [])

    async def test_an_embedded_terminal_is_told_which_logins_need_a_password(self):
        store_key()
        pin()
        for auth, pem, needed in (("key", PEM, False), ("password", None, True), ("none", None, False)):
            with self.subTest(auth):
                save_profile(transport="host", host=HOST, auth=auth)
                got = await self.manager.terminal_credentials(ADDRESS)
                self.assertEqual(got.private_key_pem, pem)
                self.assertFalse(got.passphrase_required)
                self.assertEqual(got.password_required, needed)

    async def test_auth_password_credentials_still_enforce_the_host_key(self):
        save_profile(transport="host", host=HOST, auth="password")
        with self.assertRaisesRegex(HostKeyError, "not trusted yet"):
            await self.manager.terminal_credentials(ADDRESS)

    async def test_private_key_material_stays_out_of_errors_and_results(self):
        key = store_key()
        save_profile(transport="host", host=HOST, key_id=key.id)
        seen = [repr(await self.manager.status(ADDRESS))]

        async def failure(expected, run=None) -> None:
            with self.assertRaises(expected) as caught:
                await (run or self.manager.test)(ADDRESS)
            seen.append(str(caught.exception))

        await failure(HostKeyError)  # unknown
        pin(blob=OTHER_BLOB)
        await failure(HostKeyError)  # changed
        await failure(HostKeyError, self.manager.trust_host)
        hostkeys.forget_fingerprint(HOST, 22)
        pin()
        self.ssh.test_result = TestResult(ok=False, error="Permission denied (publickey)")
        seen.append(repr(await self.manager.test(ADDRESS)))
        seen.append(repr(self.ssh.calls[-1]))
        self.ssh.error = NetworkError("ssh: timed out")
        await failure(NetworkError)
        self.ssh.error = None
        keys.key_path(key).unlink()
        await failure(SshKeyError)  # file missing
        keys.delete_key(key.id)
        await failure(SshKeyError)  # no longer stored
        for text in seen:
            self.assertNotIn("PRIVATE KEY", text)
            self.assertNotIn(PEM_BODY, text)


class PasswordLoginTests(ManagerCase):
    def setUp(self):
        super().setUp()
        self.key = store_key()  # there to be left alone: a password login must not pick it up
        save_profile(transport="host", host=HOST, auth="password")
        pin()

    def assert_password_login(self, call):
        self.assertIsNone(call.key_path)
        self.assertEqual(call.options["password"], PASSWORD)

    async def run_every_login(self, **kwargs):
        """test, exec and each call on a route, with the same arguments, so they all land in ``self.ssh.calls``."""
        await self.manager.test(ADDRESS, **kwargs)
        await self.manager.exec(ADDRESS, "uptime", **kwargs)
        async with self.manager.route(ADDRESS, **kwargs) as route:
            remote = SshRemote(route)
            remote.exec("uptime")
            remote.read_file("/tmp/a")
            remote.write_file("/tmp/a", b"data")

    async def test_every_login_gets_the_password_and_no_key(self):
        await self.run_every_login(password=PASSWORD)
        self.assertEqual(
            [call.name for call in self.ssh.calls],
            ["test", "exec_command", "exec_command", "read_file", "write_file"],
        )
        for call in self.ssh.calls:
            with self.subTest(call.name):
                self.assert_password_login(call)

    async def test_it_works_with_a_profile_given_for_this_run(self):
        given = SshProfile(address=ADDRESS, transport="host", host=HOST, auth="password", ssh_port=22)
        profiles.delete(ADDRESS)
        await self.manager.test(ADDRESS, profile=given, password=PASSWORD)
        self.assert_password_login(self.ssh.calls[-1])

    async def test_a_route_carries_it_without_showing_it(self):
        async with self.manager.route(ADDRESS, password=PASSWORD) as route:
            self.assertEqual(route.password, PASSWORD)
            self.assertIsNone(route.key_path)
            self.assertNotIn(PASSWORD, repr(route))

    async def test_a_wifi_route_is_held_for_a_password_login_too(self):
        save_profile(transport="wifi", auth="password")
        pin(AP_HOST)
        await self.manager.test(ADDRESS, password=PASSWORD)
        self.assert_password_login(self.ssh.calls[-1])
        self.assertTrue(self.ssh.calls[-1].network_held)
        self.assertFalse(self.network.held)

    async def test_without_a_password_it_still_refuses_before_touching_the_camera(self):
        async def open_route():
            async with self.manager.route(ADDRESS):
                self.fail("the route opened")

        for run in (self.manager.test(ADDRESS), self.manager.exec(ADDRESS, "uptime"), open_route()):
            with self.assertRaisesRegex(SshError, "--password-stdin.*interactive terminal"):
                await run
        self.scan.assert_not_called()
        self.assertEqual(self.ssh.calls, [])

    async def test_the_host_key_is_checked_before_a_password_goes_anywhere(self):
        hostkeys.forget_fingerprint(HOST, 22)
        hostkeys.remove_known_host(HOST, 22)
        pin(blob=OTHER_BLOB)
        with self.assertRaisesRegex(HostKeyError, "has changed"):
            await self.manager.test(ADDRESS, password=PASSWORD)
        with self.assertRaisesRegex(HostKeyError, "has changed"):
            await self.manager.exec(ADDRESS, "uptime", password=PASSWORD)
        self.assertEqual(self.ssh.calls, [])

    async def test_nothing_on_disk_holds_the_password(self):
        await self.run_every_login(password=PASSWORD)
        written = [path for path in self.config_dir.rglob("*") if path.is_file()]
        self.assertTrue(written)
        for path in written:
            self.assertNotIn(PASSWORD.encode(), path.read_bytes(), path)

    async def test_a_key_profile_keeps_its_key_whatever_password_comes_along(self):
        save_profile(transport="host", host=HOST, auth="key")
        await self.run_every_login(password=PASSWORD)
        self.assertEqual(len(self.ssh.calls), 5)
        for call in self.ssh.calls:
            with self.subTest(call.name):
                self.assertEqual(call.key_path, keys.key_path(self.key))
                self.assertNotIn("password", call.options)

    async def test_a_profile_with_no_auth_takes_no_password(self):
        save_profile(transport="host", host=HOST, auth="none")
        await self.run_every_login(password=PASSWORD)
        for call in self.ssh.calls:
            with self.subTest(call.name):
                self.assertIsNone(call.key_path)
                self.assertNotIn("password", call.options)

    async def test_a_key_route_carries_none(self):
        save_profile(transport="host", host=HOST, auth="key")
        async with self.manager.route(ADDRESS, password=PASSWORD) as route:
            self.assertIsNone(route.password)
            self.assertEqual(route.login_options, {})

    async def test_a_login_with_no_password_does_not_mention_one_to_ssh(self):
        """Anything shaped like ``system_ssh`` keeps working if it has never heard of passwords."""
        save_profile(transport="host", host=HOST, auth="key")
        await self.run_every_login()
        for call in self.ssh.calls:
            with self.subTest(call.name):
                self.assertNotIn("password", call.options)


class HandOffTests(ManagerCase):
    def setUp(self):
        super().setUp()
        self.profile = self.ready()

    async def test_test_adds_the_banner_and_host_key_to_what_ssh_found(self):
        result = await self.manager.test(ADDRESS)
        self.assertTrue(result.ok)
        self.assertEqual(result.uid, "uid=0(root)")
        self.assertEqual(result.uname, "Linux 4.9 armv7l")
        self.assertEqual(result.banner, BANNER)
        self.assertEqual(result.host_key.fingerprint, hostkeys.fingerprint(BLOB))
        self.assertEqual(self.ssh.calls[-1].profile, self.profile)

    async def test_a_failed_login_comes_back_in_the_result(self):
        self.ssh.test_result = TestResult(ok=False, error="Permission denied (publickey)")
        result = await self.manager.test(ADDRESS)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "Permission denied (publickey)")
        self.assertEqual(result.banner, BANNER)

    async def test_exec_passes_the_command_through_and_returns_its_exit_code(self):
        command = "cat '/tmp/a b' | wc -l; exit 3"
        result = await self.manager.exec(ADDRESS, command)
        self.assertEqual(self.ssh.calls[-1].extra, (command,))
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.stdout, b"out")
        self.assertEqual(result.stderr, b"err")

    async def test_exec_refuses_a_blank_command(self):
        for command in ("", "   "):
            with self.assertRaisesRegex(SshError, "no remote command"):
                await self.manager.exec(ADDRESS, command)
        self.scan.assert_not_called()
        self.assertEqual(self.ssh.calls, [])

    async def test_open_terminal_returns_the_exit_code(self):
        self.assertEqual(await self.manager.open_terminal(ADDRESS), 7)
        self.assertEqual(self.ssh.calls[-1].name, "open_terminal")

    async def test_the_store_holds_no_private_key_material(self):
        await self.manager.test(ADDRESS)
        text = json.dumps(store.read())
        self.assertNotIn(PEM_BODY, text)
        self.assertNotIn("PRIVATE KEY", text)

    async def test_the_system_ssh_module_is_the_default(self):
        with mock.patch.object(system_ssh, "test", return_value=TestResult(ok=True, uid="uid=0")) as run:
            result = await self.make_manager(ssh=None).test(ADDRESS)
        self.assertEqual(result.uid, "uid=0")
        run.assert_called_once()


class AsyncScanCase(ManagerCase):
    """A manager scanning the way the app does, with a fingerprint-only key and a host route."""

    def setUp(self):
        super().setUp()
        save_profile(transport="host", host=HOST)
        store_key()
        self.scan_async = mock.AsyncMock(side_effect=offered)
        self.manager = self.make_manager(scan_async=self.scan_async)


class AsyncScanTests(AsyncScanCase):
    async def test_it_is_awaited_and_the_thread_scan_stays_out_of_it(self):
        key = await self.manager.scan_host_key(ADDRESS)
        self.assertEqual(key, offered())
        self.scan_async.assert_awaited_once_with(HOST, 22)
        self.scan.assert_not_called()

    async def test_without_it_the_thread_scan_runs(self):
        key = await self.make_manager(scan_async=None).scan_host_key(ADDRESS)
        self.assertEqual(key, scanned())
        self.scan.assert_called_once_with(HOST, 22)
        self.scan_async.assert_not_awaited()

    async def test_it_runs_on_the_event_loop_with_a_wifi_route_held(self):
        save_profile(transport="wifi")
        seen = []

        async def scan(host, port):
            seen.append((host, self.network.held, threading.current_thread()))
            return offered(host, port)

        await self.make_manager(scan_async=scan).scan_host_key(ADDRESS)
        self.assertEqual(seen, [(AP_HOST, True, threading.current_thread())])
        self.assertFalse(self.network.held)

    async def test_a_failed_scan_fails_closed(self):
        hostkeys.remember_fingerprint(HOST, 22, "ssh-ed25519", offered().fingerprint)
        for failure in (TimeoutError("timed out"), ConnectionError("refused")):
            with self.subTest(type(failure).__name__):
                self.scan_async.side_effect = failure
                with self.assertRaisesRegex(SshError, str(failure)) as caught:
                    await self.manager.test(ADDRESS)
                self.assertNotIsInstance(caught.exception, HostKeyError)
        self.assertEqual(self.ssh.calls, [])

    async def test_status_reports_the_key_it_found(self):
        status = await self.manager.status(ADDRESS)
        self.assertEqual(status.host_key, offered())
        self.assertEqual(status.host_key_state, "unknown")
        self.assertEqual(status.last_error, "")

    async def test_status_puts_a_failed_scan_in_last_error(self):
        self.scan_async.side_effect = ConnectionError("refused")
        status = await self.manager.status(ADDRESS)
        self.assertIn("refused", status.last_error)
        self.assertIsNone(status.host_key)


class FingerprintOnlyTrustTests(AsyncScanCase):
    async def test_trust_remembers_the_fingerprint_and_writes_no_known_hosts(self):
        pinned = await self.manager.trust_host(ADDRESS, fingerprint=offered().fingerprint)
        self.assertEqual(pinned, offered())
        self.assertEqual(hostkeys.stored_fingerprint(HOST, 22), offered())
        self.assertEqual(hostkeys.state(HOST, 22, offered()), "trusted")
        self.assertFalse(hostkeys.known_hosts_path().exists())

    async def test_trust_is_repeatable(self):
        await self.manager.trust_host(ADDRESS)
        await self.manager.trust_host(ADDRESS)
        self.assertEqual(hostkeys.stored_fingerprint(HOST, 22), offered())

    async def test_trust_refuses_a_key_other_than_the_one_reviewed(self):
        with self.assertRaisesRegex(HostKeyError, "Nothing was pinned"):
            await self.manager.trust_host(ADDRESS, fingerprint=hostkeys.fingerprint(OTHER_BLOB))
        self.assertEqual(hostkeys.state(HOST, 22), "unknown")

    async def test_trust_will_not_replace_a_pin_that_no_longer_matches(self):
        pinned = hostkeys.remember_fingerprint(HOST, 22, "ssh-ed25519", hostkeys.fingerprint(OTHER_BLOB))
        with self.assertRaisesRegex(HostKeyError, "quak ssh host forget"):
            await self.manager.trust_host(ADDRESS)
        self.assertEqual(hostkeys.stored_fingerprint(HOST, 22), pinned)

    async def test_trust_will_not_pass_over_a_different_known_hosts_entry(self):
        hostkeys.add_host_key(scanned(blob=OTHER_BLOB))
        with self.assertRaisesRegex(HostKeyError, "has changed"):
            await self.manager.trust_host(ADDRESS)
        self.assertIsNone(hostkeys.stored_fingerprint(HOST, 22))

    async def test_trust_leaves_a_matching_known_hosts_entry_as_it_was(self):
        pin()
        before = hostkeys.known_hosts_path().read_text()
        await self.manager.trust_host(ADDRESS)
        self.assertEqual(hostkeys.known_hosts_path().read_text(), before)

    async def test_forget_then_trust_accepts_a_regenerated_key(self):
        hostkeys.remember_fingerprint(HOST, 22, "ssh-ed25519", hostkeys.fingerprint(OTHER_BLOB))
        self.assertTrue(await self.manager.forget_host(ADDRESS))
        self.assertEqual((await self.manager.trust_host(ADDRESS)).fingerprint, offered().fingerprint)


class FingerprintOnlyVerifyTests(AsyncScanCase):
    async def test_a_matching_pin_passes_and_nothing_is_written(self):
        hostkeys.remember_fingerprint(HOST, 22, "ssh-ed25519", offered().fingerprint)
        result = await self.manager.test(ADDRESS)
        self.assertEqual(result.host_key, offered())
        self.assertFalse(hostkeys.known_hosts_path().exists())

    async def test_an_unknown_key_is_refused(self):
        with self.assertRaisesRegex(HostKeyError, "not trusted yet"):
            await self.manager.test(ADDRESS)
        self.assertEqual(self.ssh.calls, [])

    async def test_a_changed_key_is_refused(self):
        hostkeys.remember_fingerprint(HOST, 22, "ssh-ed25519", hostkeys.fingerprint(OTHER_BLOB))
        with self.assertRaisesRegex(HostKeyError, "has changed"):
            await self.manager.test(ADDRESS)
        self.assertEqual(self.ssh.calls, [])

    async def test_a_known_hosts_only_pin_is_compared_too(self):
        hostkeys.add_host_key(scanned())
        self.assertEqual((await self.manager.test(ADDRESS)).host_key, offered())
        hostkeys.add_host_key(scanned(blob=OTHER_BLOB))
        with self.assertRaisesRegex(HostKeyError, "has changed"):
            await self.manager.test(ADDRESS)

    async def test_a_stale_known_hosts_line_behind_a_matching_pin_stays_for_ssh_to_refuse(self):
        """There's no line to replace it with. Left as it is, the system ssh fails closed."""
        hostkeys.add_host_key(scanned(blob=OTHER_BLOB))
        hostkeys.remember_fingerprint(HOST, 22, "ssh-ed25519", offered().fingerprint)
        await self.manager.test(ADDRESS)
        self.assertEqual(hostkeys.lookup_known_hosts(HOST, 22).fingerprint, hostkeys.fingerprint(OTHER_BLOB))

    async def test_a_later_scan_with_a_line_fills_in_known_hosts(self):
        await self.manager.trust_host(ADDRESS)
        await self.make_manager().test(ADDRESS)
        self.assertEqual(hostkeys.lookup_known_hosts(HOST, 22).fingerprint, offered().fingerprint)


class ResolveTests(ManagerCase):
    def assert_touched_nothing(self):
        self.assertEqual(self.network.entered, [])
        self.scan.assert_not_called()
        self.banner.assert_not_called()

    async def test_a_host_route(self):
        save_profile(transport="host", host=HOST, ssh_port=2222, http_port=8081)
        self.assertEqual(await self.manager.resolve(ADDRESS), Endpoint(HOST, 2222, 8081, "host"))
        self.assert_touched_nothing()

    async def test_a_usb_route(self):
        self.usb_nets = [transport.UsbNet("usb0", "172.21.145.50", transport.CAMERA_USB_VID, "0049")]
        self.reachable.side_effect = lambda host, port: (host, port) == ("172.21.145.51", 80)
        save_profile(transport="usb")
        self.assertEqual(await self.manager.resolve(ADDRESS), Endpoint("172.21.145.51", 22, 80, "usb", "usb0"))
        self.assert_touched_nothing()

    async def test_a_wifi_route_is_named_and_not_joined(self):
        save_profile(transport="wifi")
        self.assertEqual(await self.manager.resolve(ADDRESS), Endpoint(AP_HOST, 22, 80, "wifi"))
        self.assert_touched_nothing()

    async def test_a_new_profile_on_auto_falls_back_to_wifi(self):
        self.assertEqual((await self.manager.resolve(ADDRESS)).transport, "wifi")
        self.assert_touched_nothing()

    async def test_auto_takes_an_explicit_host_that_answers_as_operations_do(self):
        self.ready(transport="auto", host=HOST)
        self.reachable.side_effect = lambda host, port: (host, port) == (HOST, 22)
        endpoint = await self.manager.resolve(ADDRESS)
        self.assertEqual(endpoint.transport, "host")
        await self.manager.test(ADDRESS)
        self.assertEqual(self.ssh.calls[-1].endpoint, endpoint)

    async def test_auto_passes_over_an_explicit_host_that_does_not_answer(self):
        save_profile(transport="auto", host=HOST)
        self.assertEqual((await self.manager.resolve(ADDRESS)).host, AP_HOST)

    async def test_a_given_profile_wins_and_is_not_saved(self):
        given = SshProfile(address=ADDRESS, transport="host", host=HOST)
        self.assertEqual((await self.manager.resolve(ADDRESS, given)).host, HOST)
        self.assertEqual((await self.manager.resolve(ADDRESS, profile=given)).host, HOST)
        self.assertIsNone(profiles.load(ADDRESS))

    async def test_a_route_that_cannot_be_found_raises(self):
        save_profile(transport="usb")
        with self.assertRaisesRegex(NetworkError, "no camera USB network"):
            await self.manager.resolve(ADDRESS)

    async def test_an_invalid_saved_profile_is_an_ssh_error(self):
        store.write({"profiles": {ADDRESS: {"transport": "carrier-pigeon"}}})
        with self.assertRaisesRegex(SshError, "not valid"):
            await self.manager.resolve(ADDRESS)

    async def test_its_probes_stay_off_the_event_loop(self):
        save_profile(transport="auto", host=HOST)
        threads = []

        def reachable(host, port):
            threads.append(threading.current_thread())
            return False

        await self.make_manager(reachable=reachable).resolve(ADDRESS)
        self.assertTrue(threads)
        self.assertNotIn(threading.current_thread(), threads)


if __name__ == "__main__":
    unittest.main()

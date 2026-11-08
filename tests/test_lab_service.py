"""The Lab service's wiring: no hardware, no ssh.

The Lab's functions have tests of their own. These check that each action reaches the right one, on a worker thread
with a route held open, and that host mode is refused over the camera's own USB link.
"""

import contextlib
import threading
import unittest
from unittest import mock
from unittest.mock import call, sentinel

from quak.core.errors import HostModeError, LabError, NetworkError
from quak.lab import devices, hostmode, toolkit
from quak.lab.capabilities import audio, display, network, serial, storage, system
from quak.lab.models import NetworkInterface, PackManifest, PackModule, PackState, UsbDevice
from quak.lab.service import LabService
from quak.ssh import transport
from quak.ssh.manager import SshRoute
from quak.ssh.models import Endpoint, ExecResult, SshProfile
from quak.ssh.transport import UsbNet

ADDRESS = "AA:BB:CC:DD:EE:FF"
PROFILE = SshProfile(address=ADDRESS)
PASSWORD_PROFILE = SshProfile(address=ADDRESS, auth="password")
SECRET = "correct horse battery"
WIFI = Endpoint(host="10.5.5.9", transport="wifi")
USB = Endpoint(host="172.25.118.51", transport="usb")
DEVICE_MODE = "hostmode=2\nglue=1\nudc=19880000.dwc3\n"
HOST_MODE = "hostmode=1\nglue=1\nudc=\nhub=usb1\nhub=usb2\n"
CARD_FILE = "/tmp/fuse_d/a.mp4"
VOLUME = "/tmp/quak/mnt/usb"
CAPTURE_SIZE = 5_760_044

# What the service is asked, the function that does it, and what that function gets after the remote.
DELEGATION = [
    (call.devices(), devices, "list_devices", call()),
    (call.toolkit_status(), toolkit, "toolkit_state", call()),
    (call.toolkit_install(), toolkit, "install_core", call()),
    (call.toolkit_load(), toolkit, "load_core", call()),
    (call.toolkit_unload(), toolkit, "unload_core", call()),
    (call.toolkit_remove(), toolkit, "remove_core", call()),
    (call.storage_list(), storage, "list_volumes", call()),
    (call.storage_mount("/dev/sda1"), storage, "mount", call("/dev/sda1", None, True)),
    (call.storage_mount("/dev/sda1", "usb", read_only=False), storage, "mount", call("/dev/sda1", "usb", False)),
    (call.storage_unmount("usb"), storage, "unmount", call("usb")),
    (call.storage_list_dir(VOLUME), storage, "list_dir", call(VOLUME)),
    (call.storage_copy(CARD_FILE, VOLUME), storage, "copy", call(CARD_FILE, VOLUME, timeout=storage.COPY_TIMEOUT)),
    (call.storage_copy(CARD_FILE, VOLUME, timeout=5.0), storage, "copy", call(CARD_FILE, VOLUME, timeout=5.0)),
    (call.storage_eject("usb"), storage, "eject", call("usb")),
    (call.audio_save(1, 30), audio, "save", call(1, 30, "/tmp/fuse_d/quak/recordings")),
    (call.audio_save(1, 30, "/tmp/fuse_d/mics"), audio, "save", call(1, 30, "/tmp/fuse_d/mics")),
    (call.network_list(), network, "list_interfaces", call()),
    (call.network_configure("eth0"), network, "configure", call("eth0", True, None, 24, None, None)),
    (
        call.network_configure("eth0", dns=["1.1.1.1"]),
        network,
        "configure",
        call("eth0", True, None, 24, None, ["1.1.1.1"]),
    ),
    (
        call.network_configure("eth0", dhcp=False, address="10.0.0.2", prefix=16, gateway="10.0.0.1", dns="1.1.1.1"),
        network,
        "configure",
        call("eth0", False, "10.0.0.2", 16, "10.0.0.1", "1.1.1.1"),
    ),
    (call.display_list(), display, "list_framebuffers", call()),
    (call.serial_list(), serial, "list_ports", call()),
    (call.system_status(), system, "status", call()),
    (call.system_logs(), system, "logs", call(200)),
    (call.system_logs(tail=50), system, "logs", call(50)),
    (call.system_processes(), system, "processes", call()),
]


def answer(text: str = "", exit_code: int = 0, stderr: bytes = b"") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=text.encode(), stderr=stderr)


class FakeRemote:
    """A ``Remote`` that knows where it goes and answers by command. A list answers in turn; an exception is raised.

    A command it has no answer for succeeds, printing one finished step, as a switch script does.
    """

    def __init__(self, endpoint: Endpoint = WIFI, replies: dict | None = None) -> None:
        self.endpoint = endpoint
        self.replies = replies or {}
        self.commands: list[str] = []
        self.threads: set[int] = set()

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        self.threads.add(threading.get_ident())
        reply = self.replies.get(command, answer("done=bind\n"))
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"the service read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"the service wrote {path}")


class FakeRouteFactory:
    """Stands in for ``ssh_remote``, recording each route it opens and closes."""

    def __init__(self, remote: FakeRemote) -> None:
        self.remote = remote
        self.opened: list[tuple] = []
        self.logins: list[dict] = []  # what each route was given beyond these, kept apart from what gets compared
        self.events: list[str] = []

    @contextlib.asynccontextmanager
    async def __call__(self, manager, address, *, profile=None, **login):
        self.opened.append((manager, address, profile))
        self.logins.append(login)
        self.events.append("open")
        try:
            yield self.remote
        finally:
            self.events.append("close")


class FakeSsh:
    """The part of ``system_ssh`` that ``SshRemote`` uses to run a command."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.passwords: list[str | None] = []

    def exec_command(self, profile, endpoint, key_path, command, input=None, timeout=None, password=None) -> ExecResult:
        self.commands.append(command)
        self.passwords.append(password)
        return answer("entry=usb1\n")


class FakeManager:
    """The part of ``SshManager`` that the real ``ssh_remote`` uses."""

    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint = endpoint
        self.ssh = FakeSsh()
        self.routes: list[tuple] = []
        self.passwords: list[str | None] = []

    @contextlib.asynccontextmanager
    async def route(self, address, *, profile=None, password=None):
        self.routes.append((address, profile))
        self.passwords.append(password)
        yield SshRoute(self.ssh, profile, self.endpoint, None, password)


def lab(remote: FakeRemote | None = None, **kwargs) -> tuple[LabService, FakeRouteFactory]:
    factory = FakeRouteFactory(remote or FakeRemote())
    return LabService(sentinel.manager, ADDRESS, remote_factory=factory, **kwargs), factory


class DelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_action_runs_its_function_on_a_worker_thread_with_the_route_open(self):
        for action, module, function, expected in DELEGATION:
            name, args, kwargs = action
            with self.subTest(action=action):
                service, factory = lab()
                seen: list[tuple[int, list[str]]] = []

                def work(*_args, **_kwargs):
                    seen.append((threading.get_ident(), list(factory.events)))
                    return sentinel.result

                with mock.patch.object(module, function, autospec=True, side_effect=work) as patched:
                    result = await getattr(service, name)(*args, **kwargs)

                patched.assert_called_once_with(factory.remote, *expected.args, **expected.kwargs)
                self.assertEqual(len(seen), 1)
                thread, events_during = seen[0]
                self.assertNotEqual(thread, threading.get_ident())
                self.assertEqual(events_during, ["open"])
                self.assertEqual(factory.events, ["open", "close"])
                if name not in ("storage_unmount", "storage_eject"):
                    self.assertIs(result, sentinel.result)

    async def test_unmount_and_eject_return_nothing(self):
        for name, function in (("storage_unmount", "unmount"), ("storage_eject", "eject")):
            with self.subTest(action=name):
                service, _ = lab()
                with mock.patch.object(storage, function, autospec=True, return_value=None):
                    self.assertIsNone(await getattr(service, name)("usb"))

    async def test_the_route_is_for_this_manager_address_and_profile(self):
        service, factory = lab(profile=PROFILE)
        with mock.patch.object(system, "processes", autospec=True):
            await service.system_processes()
        self.assertEqual(factory.opened, [(sentinel.manager, ADDRESS, PROFILE)])

    async def test_each_action_holds_its_own_route(self):
        service, factory = lab()
        with mock.patch.object(system, "processes", autospec=True):
            await service.system_processes()
            await service.system_processes()
        self.assertEqual(factory.events, ["open", "close", "open", "close"])

    async def test_a_failure_comes_through_and_the_route_still_closes(self):
        remote = FakeRemote(replies={devices.LIST_SCRIPT: NetworkError("gone")})
        service, factory = lab(remote)
        with self.assertRaisesRegex(NetworkError, "gone"):
            await service.devices()
        self.assertEqual(factory.events, ["open", "close"])


class DefaultFactoryTests(unittest.IsolatedAsyncioTestCase):
    """Nothing injected but the manager, so this goes through the real ``ssh_remote`` and ``SshRemote``."""

    async def test_commands_go_through_the_managers_route(self):
        manager = FakeManager(WIFI)
        found = await LabService(manager, ADDRESS, profile=PROFILE).devices()
        self.assertEqual(found, [UsbDevice(id="usb1")])
        self.assertEqual(manager.routes, [(ADDRESS, PROFILE)])
        self.assertEqual(manager.ssh.commands, [devices.LIST_SCRIPT])

    async def test_host_on_sees_the_routes_own_endpoint(self):
        manager = FakeManager(USB)
        with self.assertRaises(HostModeError):
            await LabService(manager, ADDRESS).host_on()
        self.assertEqual(manager.ssh.commands, [])

    async def test_the_password_reaches_the_route_and_each_command_on_it(self):
        manager = FakeManager(WIFI)
        await LabService(manager, ADDRESS, profile=PASSWORD_PROFILE, password=SECRET).devices()
        self.assertEqual(manager.routes, [(ADDRESS, PASSWORD_PROFILE)])
        self.assertEqual(manager.passwords, [SECRET])
        self.assertEqual(manager.ssh.passwords, [SECRET])

    async def test_without_one_the_route_gets_none(self):
        manager = FakeManager(WIFI)
        await LabService(manager, ADDRESS, profile=PROFILE).devices()
        self.assertEqual(manager.passwords, [None])
        self.assertEqual(manager.ssh.passwords, [None])


class PasswordTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_route_of_the_service_gets_it(self):
        service, factory = lab(password=SECRET)
        with mock.patch.object(system, "processes", autospec=True):
            await service.system_processes()
            await service.system_processes()
        self.assertEqual(factory.logins, [{"password": SECRET}] * 2)

    async def test_an_empty_one_is_still_one(self):
        # It is what the app's question gives for an empty field, and only the login can say it's wrong.
        service, factory = lab(password="")
        with mock.patch.object(system, "processes", autospec=True):
            await service.system_processes()
        self.assertEqual(factory.logins, [{"password": ""}])

    async def test_a_factory_that_knows_nothing_of_passwords_still_works_for_a_key_login(self):
        service, factory = lab()
        with mock.patch.object(system, "processes", autospec=True):
            await service.system_processes()
        self.assertEqual(factory.logins, [{}])


class HostOnTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_over_the_cameras_usb_before_anything_is_sent(self):
        remote = FakeRemote(USB)
        service, factory = lab(remote)
        with self.assertRaisesRegex(HostModeError, "Connect over Wi-Fi or Ethernet first"):
            await service.host_on()
        self.assertEqual(remote.commands, [])
        self.assertEqual(factory.events, ["open", "close"])

    async def test_refused_on_a_typed_address_that_is_really_the_usb_link(self):
        remote = FakeRemote(Endpoint(host="172.25.118.51", transport="host"))
        net = UsbNet(interface="usb0", ipv4="172.25.118.50", vid="2672", pid="0059")
        service, _ = lab(remote)
        with mock.patch.object(transport, "find_usb_nets", return_value=[net]):
            with self.assertRaises(HostModeError):
                await service.host_on()
        self.assertEqual(remote.commands, [])

    async def test_a_refusal_never_reaches_the_switch(self):
        service, factory = lab()
        with (
            mock.patch.object(hostmode, "requires_network_switch", autospec=True, return_value=True) as requires,
            mock.patch.object(hostmode, "enable", autospec=True) as enable,
        ):
            with self.assertRaises(HostModeError):
                await service.host_on()
        requires.assert_called_once_with(factory.remote.endpoint)
        enable.assert_not_called()

    async def test_the_check_runs_off_the_event_loop(self):
        # It can read sysfs and run `ip` on this computer.
        service, factory = lab()
        threads: list[int] = []

        def link_survives(endpoint):
            threads.append(threading.get_ident())
            return False

        with (
            mock.patch.object(hostmode, "requires_network_switch", autospec=True, side_effect=link_survives),
            mock.patch.object(hostmode, "enable", autospec=True, return_value=sentinel.result) as enable,
        ):
            self.assertIs(await service.host_on(), sentinel.result)
        enable.assert_called_once_with(factory.remote)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    async def test_switches_when_the_link_survives_it(self):
        remote = FakeRemote(WIFI, {hostmode.STATE_SCRIPT: [answer(DEVICE_MODE), answer(HOST_MODE)]})
        service, _ = lab(remote)
        with mock.patch.object(transport, "find_usb_nets", return_value=[]):
            result = await service.host_on()
        self.assertTrue(result.state.enabled)
        self.assertEqual(remote.commands, [hostmode.STATE_SCRIPT, hostmode.ENABLE_SCRIPT, hostmode.STATE_SCRIPT])


class HostOffTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_and_recover_name_the_packs_modules_in_load_order(self):
        manifest = PackManifest(id="core", modules=[PackModule("snd-pcm", deps=["snd"]), PackModule("snd")])
        for name, function in (("host_off", "disable"), ("host_recover", "recover")):
            with self.subTest(action=name):
                service, factory = lab()
                with (
                    mock.patch.object(toolkit, "core_manifest", autospec=True, return_value=manifest),
                    mock.patch.object(hostmode, function, autospec=True, return_value=sentinel.result) as switch,
                ):
                    self.assertIs(await getattr(service, name)(), sentinel.result)
                switch.assert_called_once_with(factory.remote, modules=["snd", "snd-pcm"])

    async def test_off_unloads_the_bundled_pack_last_loaded_first(self):
        remote = FakeRemote(replies={hostmode.STATE_SCRIPT: [answer(HOST_MODE), answer(DEVICE_MODE)]})
        service, _ = lab(remote)
        result = await service.host_off()
        self.assertEqual(result.state.hostmode, hostmode.DEVICE)
        self.assertEqual(len(remote.commands), 3)
        script = remote.commands[1]
        self.assertLess(script.index("rmmod:snd_usb_audio "), script.index("rmmod:snd_pcm "))
        self.assertLess(script.index("rmmod:snd_pcm "), script.index("rmmod:snd "))


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_the_port_the_toolkit_and_the_bus_over_one_route(self):
        remote = FakeRemote(
            replies={
                hostmode.STATE_SCRIPT: answer(HOST_MODE),
                toolkit.READ_STATE_SCRIPT: answer(exit_code=3),  # no state file: not installed
                devices.LIST_SCRIPT: answer("entry=usb1\nidVendor=1d6b\n"),
            }
        )
        service, factory = lab(remote)
        status = await service.status()
        self.assertTrue(status.host_mode.enabled)
        self.assertEqual(status.toolkit, PackState())
        self.assertEqual(status.devices, [UsbDevice(id="usb1", vid="1d6b")])
        self.assertEqual(status.error, "")
        self.assertEqual(len(factory.opened), 1)
        self.assertNotIn(threading.get_ident(), remote.threads)

    async def test_a_failed_device_list_is_reported_beside_the_rest(self):
        remote = FakeRemote(
            replies={
                hostmode.STATE_SCRIPT: answer(HOST_MODE),
                toolkit.READ_STATE_SCRIPT: answer(exit_code=3),
                devices.LIST_SCRIPT: answer(exit_code=1, stderr=b"no sysfs"),
            }
        )
        service, _ = lab(remote)
        status = await service.status()
        self.assertIn("no sysfs", status.error)
        self.assertEqual(status.devices, [])
        self.assertTrue(status.host_mode.enabled)
        self.assertEqual(status.toolkit, PackState())

    async def test_a_lost_link_raises(self):
        service, factory = lab(FakeRemote(replies={hostmode.STATE_SCRIPT: NetworkError("gone")}))
        with self.assertRaisesRegex(NetworkError, "gone"):
            await service.status()
        self.assertEqual(factory.events, ["open", "close"])


class CapturingCamera(FakeRemote):
    """Answers what a save sends: a capture of ``CAPTURE_SIZE`` bytes and a copy that matches. ``failing`` fails."""

    def __init__(self, failing: str = "") -> None:
        super().__init__()
        self.failing = failing

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        if self.failing and self.failing in command:
            return answer(exit_code=1, stderr=b"it failed")
        if "ls -ldn" in command:
            return answer(f"{CAPTURE_SIZE}\n")
        if "sha256sum" in command:
            return answer(f"source={'a' * 64}\ncopy={'a' * 64}\n")
        return answer()


class AudioSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_makes_the_folder_captures_and_copies_all_over_one_route(self):
        remote = CapturingCamera()
        service, factory = lab(remote)
        saved = await service.audio_save(1, 30)
        self.assertRegex(saved["path"], r"^/tmp/fuse_d/quak/recordings/quak-\d{8}-\d{6}\.wav$")
        self.assertEqual(saved["size"], CAPTURE_SIZE)
        self.assertEqual(factory.events, ["open", "close"])
        folder, tool_check, capture, copy = remote.commands
        self.assertIn("dir=/tmp/fuse_d/quak/recordings\n", folder)
        self.assertIn(audio.TINYCAP, tool_check)
        self.assertIn(f"{audio.TINYCAP} {audio.DEFAULT_PATH} -D 1 ", capture)
        self.assertIn(" -t 30 ", capture)
        self.assertIn(f"src={audio.DEFAULT_PATH}\n", copy)
        self.assertIn(f"dst={saved['path']}\n", copy)

    async def test_it_goes_in_the_folder_it_is_told(self):
        saved = await lab(CapturingCamera())[0].audio_save(1, 5, "/tmp/fuse_d/mics")
        self.assertRegex(saved["path"], r"^/tmp/fuse_d/mics/quak-[^/]+\.wav$")

    async def test_the_folder_is_only_made_on_a_mounted_card(self):
        # Without the card, mkdir -p would succeed in the camera's RAM.
        remote = CapturingCamera()
        await lab(remote)[0].audio_save(1, 30)
        script = remote.commands[0]
        self.assertIn("vol=/tmp/fuse_d\n", script)
        self.assertLess(script.index("/proc/mounts"), script.index("mkdir -p"))

    async def test_no_folder_means_no_capture(self):
        remote = CapturingCamera(failing="mkdir -p")
        service, factory = lab(remote)
        with self.assertRaisesRegex(LabError, "could not create /tmp/fuse_d/quak/recordings: it failed"):
            await service.audio_save(1, 30)
        self.assertEqual(len(remote.commands), 1)
        self.assertEqual(factory.events, ["open", "close"])

    async def test_a_folder_off_the_sd_card_is_refused_before_anything_is_sent(self):
        for folder in ("/tmp/elsewhere", "/tmp/fuse_d/../elsewhere", "relative"):
            with self.subTest(folder):
                remote = CapturingCamera()
                with self.assertRaises(LabError):
                    await lab(remote)[0].audio_save(1, 30, folder)
                self.assertEqual(remote.commands, [])

    async def test_a_capture_that_fails_isnt_copied(self):
        remote = CapturingCamera(failing="ls -ldn")
        with self.assertRaisesRegex(LabError, "could not record from sound card 1"):
            await lab(remote)[0].audio_save(1, 30)
        self.assertFalse([command for command in remote.commands if "sha256sum" in command])

    async def test_a_copy_that_fails_is_a_failure(self):
        with self.assertRaisesRegex(LabError, "could not copy /tmp/quak-audio.wav"):
            await lab(CapturingCamera(failing="sha256sum"))[0].audio_save(1, 30)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_reads_the_interfaces_from_the_camera(self):
        listing = "2: eth0: <BROADCAST,UP> mtu 1500\\    link/ether 11:22:33:44:55:66 brd AA:BB:CC:DD:EE:FF\n"
        remote = FakeRemote(replies={network.LIST_SCRIPT: answer(listing)})
        service, _ = lab(remote)
        found = await service.network_list()
        self.assertEqual(found, [NetworkInterface(name="eth0", mac="11:22:33:44:55:66", up=True, kind="ethernet")])
        self.assertEqual(remote.commands, [network.LIST_SCRIPT])

    async def test_a_refused_setup_sends_the_camera_nothing(self):
        remote = FakeRemote()
        service, factory = lab(remote)
        with self.assertRaisesRegex(LabError, "refusing gateway"):
            await service.network_configure("eth0", dhcp=False, address="10.0.0.2", gateway="192.168.1.1")
        self.assertEqual(remote.commands, [])
        self.assertEqual(factory.events, ["open", "close"])

    async def test_a_link_lost_while_configuring_raises_and_the_route_still_closes(self):
        class LosesTheLink(FakeRemote):
            def exec(self, command, input=None, timeout=120.0):
                if command == network.LIST_SCRIPT:
                    return answer("2: eth0: <UP> mtu 1500\\    link/ether 11:22:33:44:55:66\n")
                raise NetworkError("gone")

        service, factory = lab(LosesTheLink())
        with self.assertRaisesRegex(NetworkError, "gone"):
            await service.network_configure("eth0")
        self.assertEqual(factory.events, ["open", "close"])


if __name__ == "__main__":
    unittest.main()

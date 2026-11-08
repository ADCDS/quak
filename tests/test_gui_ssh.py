"""The SSH tab, against a simulator-backed service with every SshManager method mocked.

No real ssh, no subprocess and no network: ``asyncSetUp`` makes all three fail the test if anything reaches for them.
The service is simulated but the App is told it isn't in demo mode, which is what makes the actions available.
"""

import asyncio
import importlib.util
import inspect
import os
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from quak.connect import Target
from quak.core.errors import AdapterInUse, HostKeyError, SshError, SshKeyError
from quak.service import CameraService
from quak.ssh import profiles, store
from quak.ssh.manager import SshManager, TerminalCredentials
from quak.ssh.models import (
    ArmResult,
    Endpoint,
    HostKey,
    KeyInstallResult,
    SshKey,
    SshProfile,
    SshStatus,
    TestResult,
)
from quak.transport.sim import SimCamera

try:
    import flet as ft

    from quak.gui import app as gui_app
    from quak.gui import screens
except ImportError:  # GUI extra not installed
    ft = None

ADDRESS = "SIM"
PROFILE = SshProfile(address=ADDRESS)
USB = Endpoint("172.20.145.51", transport="usb", interface="usb0")
WIFI = Endpoint("10.5.5.9", transport="wifi")
SEEN = HostKey("172.20.145.51", 22, "ssh-ed25519", "SHA256:seen")
KEY = SshKey(
    id="k_1",
    label="Laptop",
    type="ssh-ed25519",
    private_file="k_1.pem",
    public_key="ssh-ed25519 AAAA",
    fingerprint="SHA256:mine",
)
ACTIONS = ("ssh-arm", "ssh-test", "ssh-terminal")
SECRET = "correct horse battery staple"
ROOT = Path(__file__).resolve().parent.parent
NATIVE_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot a real key\n-----END OPENSSH PRIVATE KEY-----\n"
CREDENTIALS = TerminalCredentials(
    host="10.5.5.9",
    ssh_port=2222,
    username="root",
    private_key_pem=NATIVE_PEM,
    passphrase_required=False,
    expected_fingerprint="SHA256:seen",
    profile=PROFILE,
)
LOGGED_IN = {"ok": True, "uid": "uid=0(root) gid=0(root)", "uname": "Linux 4.9.0 aarch64", "error": ""}


def status(**changes) -> SshStatus:
    """A camera that's up over USB, with its key trusted."""
    ready = SshStatus(
        profile=PROFILE, endpoint=USB, reachable=True, ssh_up=True, host_key=SEEN, host_key_state="trusted"
    )
    return replace(ready, **changes)


class FakePage:
    def __init__(self, width=412):
        self.width, self.height = width, 915
        self.platform = ft.PagePlatform.LINUX
        self.controls, self.dialogs, self.navigation_bar, self.on_resize = [], [], None, None
        self.tasks = []

    def run_task(self, fn, *args):
        task = asyncio.ensure_future(fn(*args))
        self.tasks.append(task)
        return task

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass

    def show_dialog(self, d):
        self.dialogs.append(d)

    def pop_dialog(self):
        return self.dialogs.pop() if self.dialogs else None


def walk(control):
    stack, seen = [control], set()
    while stack:
        c = stack.pop()
        if c is None or isinstance(c, (str, int, float, bool, bytes)) or id(c) in seen:
            continue
        seen.add(id(c))
        if isinstance(c, (list, tuple)):
            stack.extend(c)
            continue
        yield c
        for attr in ("content", "controls", "actions", "title"):
            stack.append(getattr(c, attr, None))


def words(control) -> str:
    found = []
    for c in walk(control):
        for attr in ("value", "content", "tooltip"):
            if isinstance(getattr(c, attr, None), str):
                found.append(getattr(c, attr))
    return " | ".join(found)


def find(control, key):
    return next(c for c in walk(control) if getattr(c, "key", None) == key)


@unittest.skipIf(ft is None, "GUI extra not installed")
class Base(unittest.IsolatedAsyncioTestCase):
    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.patch(mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": tmp.name, "QUAK_LANG": ""}))
        os.environ.pop("QUAK_CAMERA_ADDRESS", None)  # patch.dict puts it back
        for target in ("subprocess.run", "subprocess.Popen", "socket.create_connection"):
            self.patch(mock.patch(target, side_effect=AssertionError(f"{target} must not run in these tests")))
        # A system ssh, as on a desktop, whatever this computer has: without one, Test goes through the extension.
        self.which = self.patch(mock.patch("shutil.which", return_value="/usr/bin/ssh"))
        self.manager = mock.create_autospec(SshManager, instance=True)
        self.manager.status.return_value = status()
        self.make_manager = self.patch(mock.patch("quak.ssh.manager.SshManager", return_value=self.manager))
        self.list_keys = self.patch(mock.patch("quak.ssh.keys.list_keys", return_value=[KEY]))
        self.sim = SimCamera(latency=0.0005)
        self.svc = CameraService(Target(sim=True, sim_camera=self.sim, keepalive=None), auto_reconnect=False)
        self.page = FakePage()
        self.app = gui_app.App(self.page, self.svc, sim=False)
        self.app.screen, self.app.more_tab = "more", "ssh"
        await self.svc.connect()

    async def asyncTearDown(self):
        await self.svc.disconnect()
        await self.sim.close()

    @property
    def view(self):
        return screens.ssh(self.app)

    async def settle(self):
        while pending := [task for task in self.page.tasks if not task.done()]:
            await asyncio.gather(*pending)

    async def press(self, key):
        find(self.view, key).on_click(None)
        await self.settle()

    async def answer(self, caption):
        button = next(b for b in self.page.dialogs[-1].actions if b.content == caption)
        button.on_click(None)
        await self.settle()

    def assert_enabled(self, *keys):
        for key in keys:
            self.assertFalse(find(self.view, key).disabled, key)

    def assert_disabled(self, *keys):
        for key in keys:
            self.assertTrue(find(self.view, key).disabled, key)


class EmptyState(Base):
    async def test_before_anything_is_checked(self):
        self.assert_enabled(*ACTIONS, "ssh-refresh")
        self.assert_disabled("ssh-trust", "ssh-forget")  # nothing is known about the key yet
        text = words(self.view)
        for expected in ("SSH", "Connection", "Not checked", "Host key", "No host key seen yet.", "KEYS", "ACTIVITY"):
            self.assertIn(expected, text)

    async def test_building_the_screen_does_nothing_on_its_own(self):
        self.assertTrue(words(self.view))
        await self.settle()
        self.make_manager.assert_not_called()
        self.list_keys.assert_not_called()

    async def test_with_no_camera_only_the_key_list_can_be_refreshed(self):
        await self.svc.disconnect()
        self.assertIn("Not connected.", words(self.view))
        self.assert_disabled(*ACTIONS, "ssh-trust", "ssh-forget")
        await self.press("ssh-refresh")
        self.manager.status.assert_not_awaited()
        self.assertIn("Laptop", words(self.view))

    async def test_with_no_camera_connected_the_last_one_can_still_use_usb(self):
        await self.svc.disconnect()
        with mock.patch.dict(os.environ, {"QUAK_CAMERA_ADDRESS": "AA:BB:CC:DD:EE:FF"}):
            self.assertNotIn("Not connected.", words(self.view))
            await self.press("ssh-refresh")
            self.manager.status.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")

    async def test_demo_mode_offers_no_ssh_and_probes_nothing(self):
        self.app.sim = True
        self.assertIn("demo mode", words(self.view))
        self.assert_disabled(*ACTIONS, "ssh-trust", "ssh-forget")
        await self.press("ssh-refresh")
        self.manager.status.assert_not_awaited()

    async def test_no_stored_keys_says_how_to_add_one(self):
        self.list_keys.return_value = []
        await self.press("ssh-refresh")
        self.assertIn("Generate key", words(self.view))
        self.assertIn("Import key", words(self.view))

    async def test_what_was_found_about_another_camera_is_not_shown(self):
        await self.press("ssh-refresh")
        self.assertIn(SEEN.fingerprint, words(self.view))
        self.app.ssh["address"] = "OTHER"
        self.assertNotIn(SEEN.fingerprint, words(self.view))
        self.assert_disabled("ssh-trust", "ssh-forget")


class ReadyState(Base):
    async def test_a_refresh_fills_in_the_screen(self):
        await self.press("ssh-refresh")
        self.manager.status.assert_awaited_once_with(ADDRESS)
        self.list_keys.assert_called_once_with()
        text = words(self.view)
        for expected in (
            "SSH is up",
            "USB",
            "172.20.145.51:22",
            "Trusted",
            "ssh-ed25519 SHA256:seen",
            "Laptop",
            "ssh-ed25519 SHA256:mine",
            "Checked: SSH is up",
        ):
            self.assertIn(expected, text)
        self.assert_enabled(*ACTIONS, "ssh-refresh", "ssh-forget")
        self.assert_disabled("ssh-trust")  # already trusted

    async def test_nothing_private_is_shown(self):
        await self.press("ssh-refresh")
        self.assertNotIn(KEY.private_file, words(self.view))

    async def test_an_unjoined_wifi_route_isnt_called_unreachable(self):
        """Checking never joins Wi-Fi, so seeing nothing there doesn't mean the camera is gone."""
        self.manager.status.return_value = status(
            endpoint=WIFI, reachable=False, ssh_up=False, host_key=None, host_key_state="unknown"
        )
        await self.press("ssh-refresh")
        text = words(self.view)
        self.assertIn("Wi-Fi not joined", text)
        self.assertNotIn("Not reachable", text)

    async def test_arm_reports_and_checks_again(self):
        self.manager.arm.return_value = ArmResult(accepted=True, ssh_up=True, message="SSH came up after 3 s")
        await self.press("ssh-arm")
        self.manager.arm.assert_awaited_once_with(ADDRESS)
        self.manager.status.assert_awaited_once_with(ADDRESS)
        self.assertIn("SSH came up after 3 s", words(self.view))

    async def test_a_login_test_reports_who_it_logged_in_as(self):
        self.manager.test.return_value = TestResult(ok=True, uid="uid=0(root)", uname="Linux 4.9 armv8")
        await self.press("ssh-test")
        self.manager.test.assert_awaited_once_with(ADDRESS)
        self.assertIn("Login works: uid=0(root) · Linux 4.9 armv8", words(self.view))

    async def test_open_terminal_says_where_it_went_and_how_it_ended(self):
        # The extension may be importable because another test put its source directory on sys.path.
        self.manager.open_terminal.return_value = 130
        with mock.patch("quak.gui.screens.ssh._terminal_control", return_value=None):
            await self.press("ssh-terminal")
        self.manager.open_terminal.assert_awaited_once_with(ADDRESS)
        text = words(self.view)
        self.assertIn("The SSH session runs in the terminal Quak was started from.", text)
        self.assertIn("SSH session ended with exit code 130.", text)

    async def test_only_one_action_runs_at_a_time(self):
        gate = asyncio.Event()

        async def slow_arm(address):
            await gate.wait()
            return ArmResult(accepted=True, ssh_up=True)

        self.manager.arm.side_effect = slow_arm
        self.manager.test.return_value = TestResult(ok=True)
        waiting = self.view
        find(waiting, "ssh-arm").on_click(None)
        await asyncio.sleep(0.01)
        self.assertIn("Running", words(self.view))
        self.assert_disabled(*ACTIONS, "ssh-refresh", "ssh-trust", "ssh-forget")
        find(waiting, "ssh-test").on_click(None)  # a tap that landed on the screen from before
        await asyncio.sleep(0.01)
        gate.set()
        await self.settle()
        self.manager.test.assert_not_awaited()
        self.assert_enabled(*ACTIONS, "ssh-refresh")


class HostKeyCard(Base):
    async def test_trust_pins_exactly_the_key_that_was_shown(self):
        self.manager.status.return_value = status(host_key_state="unknown")
        await self.press("ssh-refresh")
        self.assert_enabled("ssh-trust")
        self.assert_disabled("ssh-forget")
        self.assertIn("Not trusted", words(self.view))
        await self.press("ssh-trust")
        self.assertIn(SEEN.fingerprint, words(self.page.dialogs[-1]))
        self.manager.trust_host.assert_not_awaited()  # not before it has been looked at
        self.manager.trust_host.return_value = SEEN
        self.manager.status.return_value = status()
        await self.answer("Trust")
        self.manager.trust_host.assert_awaited_once_with(ADDRESS, fingerprint=SEEN.fingerprint)
        self.manager.scan_host_key.assert_not_awaited()
        text = words(self.view)
        self.assertIn("Trusted SHA256:seen.", text)
        self.assertIn("Trusted", text)
        self.assert_disabled("ssh-trust")

    async def test_cancelling_trust_pins_nothing(self):
        self.manager.status.return_value = status(host_key_state="unknown")
        await self.press("ssh-refresh")
        await self.press("ssh-trust")
        await self.answer("Cancel")
        self.manager.trust_host.assert_not_awaited()
        self.assertEqual(self.page.dialogs, [])

    async def test_trust_fetches_the_key_when_the_route_couldnt_show_it(self):
        """Without this, the camera's Wi-Fi would be a dead end: never joined by a check, so never a key to trust."""
        self.manager.status.return_value = status(
            endpoint=WIFI, reachable=False, ssh_up=False, host_key=None, host_key_state="unknown"
        )
        offered = HostKey("10.5.5.9", 22, "ssh-ed25519", "SHA256:offered")
        self.manager.scan_host_key.return_value = offered
        self.manager.trust_host.return_value = offered
        await self.press("ssh-refresh")
        await self.press("ssh-trust")
        self.manager.scan_host_key.assert_awaited_once_with(ADDRESS)
        self.manager.trust_host.assert_not_awaited()
        self.assertIn(offered.fingerprint, words(self.page.dialogs[-1]))
        await self.answer("Trust")
        self.manager.trust_host.assert_awaited_once_with(ADDRESS, fingerprint=offered.fingerprint)

    async def test_a_changed_key_warns_and_has_to_be_forgotten_first(self):
        self.manager.status.return_value = status(host_key_state="changed")
        await self.press("ssh-refresh")
        self.assertIn("host key has changed", words(self.view))
        self.assert_disabled("ssh-trust")
        self.assert_enabled("ssh-forget")

    async def test_forget_asks_first_then_checks_again(self):
        self.manager.forget_host.return_value = True
        await self.press("ssh-refresh")
        await self.press("ssh-forget")
        self.manager.forget_host.assert_not_awaited()
        self.manager.status.return_value = status(host_key=None, host_key_state="unknown")
        await self.answer("Forget")
        self.manager.forget_host.assert_awaited_once_with(ADDRESS)
        self.assertEqual(self.manager.status.await_count, 2)
        text = words(self.view)
        self.assertIn("Host key forgotten.", text)
        self.assertIn("No host key seen yet.", text)
        self.assert_disabled("ssh-forget")

    async def test_forget_says_so_when_nothing_was_pinned(self):
        self.manager.forget_host.return_value = False
        await self.press("ssh-refresh")
        await self.press("ssh-forget")
        await self.answer("Forget")
        self.assertIn("No host key was pinned.", words(self.view))


class KeyActions(Base):
    """Install on camera and Forget, on each stored key. Profiles are the real saved ones, in the temporary config."""

    INSTALL, FORGET = f"ssh-key-install-{KEY.id}", f"ssh-key-forget-{KEY.id}"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.delete_key = self.patch(mock.patch("quak.ssh.keys.delete_key", return_value=True))
        self.manager.install_key.return_value = KeyInstallResult(
            installed=[KEY.fingerprint], verified=True, message="SHA256:mine is now on the camera and logs in."
        )

    async def test_each_key_has_both_and_they_wait_for_a_running_action(self):
        await self.press("ssh-refresh")
        self.assertIn("Install on camera", words(self.view))
        self.assert_enabled(self.INSTALL, self.FORGET)
        self.app.ssh["busy"] = True
        self.assert_disabled(self.INSTALL, self.FORGET)

    async def test_install_asks_first_then_installs_and_checks_again(self):
        await self.press("ssh-refresh")
        await self.press(self.INSTALL)
        self.assertIn(KEY.fingerprint, words(self.page.dialogs[-1]))
        self.manager.install_key.assert_not_awaited()  # not before it has been confirmed
        await self.answer("Install")
        self.manager.install_key.assert_awaited_once_with(ADDRESS, KEY.id)
        self.assertEqual(self.manager.status.await_count, 2)
        self.assertEqual(self.list_keys.call_count, 2)
        self.assertEqual(self.app.ssh["log"][-2][1:], ("SHA256:mine is now on the camera and logs in.", False))
        self.assertEqual(self.app.ssh["error"], "")
        self.assertEqual(self.page.dialogs, [])
        self.assert_enabled(self.INSTALL, self.FORGET, *ACTIONS)

    async def test_cancelling_install_installs_nothing(self):
        await self.press("ssh-refresh")
        await self.press(self.INSTALL)
        await self.answer("Cancel")
        self.manager.install_key.assert_not_awaited()
        self.assertEqual(self.page.dialogs, [])
        self.assertEqual(self.manager.status.await_count, 1)

    async def test_with_no_camera_install_is_refused(self):
        await self.svc.disconnect()
        await self.press("ssh-refresh")
        self.assert_disabled(self.INSTALL)
        self.assert_enabled(self.FORGET)  # the store is on this device
        await self.press(self.INSTALL)  # a tap that gets through anyway
        self.assertEqual(self.page.dialogs, [])
        self.manager.install_key.assert_not_awaited()
        self.assertEqual(find(self.view, "ssh-error").content.controls[0].value, self.app.ssh["error"])
        self.assertIn("no camera to install the key on", self.app.ssh["error"])

    async def test_a_failed_install_is_shown(self):
        self.manager.install_key.side_effect = SshKeyError("the camera refused the login")
        await self.press("ssh-refresh")
        await self.press(self.INSTALL)
        await self.answer("Install")
        self.assertEqual(find(self.view, "ssh-error").content.controls[0].value, "the camera refused the login")
        self.assertEqual(self.app.ssh["log"][-1][1:], ("the camera refused the login", True))
        self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)  # App.run's toast
        self.assert_enabled(self.INSTALL, self.FORGET)

    async def test_an_install_that_wasnt_verified_is_an_error(self):
        self.manager.install_key.return_value = KeyInstallResult()
        await self.press("ssh-refresh")
        await self.press(self.INSTALL)
        await self.answer("Install")
        self.assertEqual(self.app.ssh["error"], "The camera didn't confirm the key.")

    async def test_forget_asks_first_then_deletes_and_lists_again(self):
        await self.press("ssh-refresh")
        await self.press(self.FORGET)
        self.assertIn(KEY.fingerprint, words(self.page.dialogs[-1]))
        self.delete_key.assert_not_called()
        self.list_keys.return_value = []
        await self.answer("Forget")
        self.delete_key.assert_called_once_with(KEY.id)
        self.manager.install_key.assert_not_awaited()  # nothing on the camera changes
        self.assertEqual(self.manager.status.await_count, 2)
        self.assertIn("Forgot SHA256:mine.", words(self.view))
        self.assertIn("Quak has no SSH keys yet.", words(self.view))
        self.assertEqual(self.app.ssh["error"], "")

    async def test_forget_clears_the_profile_that_names_the_key_before_deleting_it(self):
        saved = replace(PROFILE, label="Mine", auth="key", key_id=KEY.id)
        other = SshProfile(address="AA:BB:CC:DD:EE:FF", key_id="k_2")
        profiles.save(saved)
        profiles.save(other)

        named_while_deleting = []
        self.delete_key.side_effect = lambda key_id: named_while_deleting.append(profiles.load(ADDRESS).key_id) or True
        await self.press("ssh-refresh")
        await self.press(self.FORGET)
        await self.answer("Forget")
        self.delete_key.assert_called_once_with(KEY.id)
        self.assertEqual(named_while_deleting, [None])  # never a moment where it names a missing key
        self.assertEqual(profiles.load(ADDRESS), replace(saved, key_id=None))
        self.assertEqual(profiles.load(other.address), other)
        self.assertIn("The saved SSH profile no longer names that key.", words(self.view))

    async def test_forget_leaves_a_profile_naming_another_key_alone(self):
        saved = replace(PROFILE, key_id="k_2")
        profiles.save(saved)
        await self.press("ssh-refresh")
        await self.press(self.FORGET)
        await self.answer("Forget")
        self.assertEqual(profiles.load(ADDRESS), saved)
        self.assertNotIn("no longer names that key", words(self.view))

    async def test_cancelling_forget_deletes_nothing(self):
        saved = replace(PROFILE, key_id=KEY.id)
        profiles.save(saved)
        await self.press("ssh-refresh")
        await self.press(self.FORGET)
        await self.answer("Cancel")
        self.delete_key.assert_not_called()
        self.assertEqual(profiles.load(ADDRESS), saved)
        self.assertEqual(self.page.dialogs, [])

    async def test_forget_works_with_no_camera(self):
        await self.svc.disconnect()
        await self.press("ssh-refresh")
        await self.press(self.FORGET)
        await self.answer("Forget")
        self.delete_key.assert_called_once_with(KEY.id)
        self.manager.status.assert_not_awaited()

    async def test_forgetting_a_key_thats_already_gone_says_so(self):
        self.delete_key.return_value = False
        await self.press("ssh-refresh")
        await self.press(self.FORGET)
        await self.answer("Forget")
        self.assertIn("That key was already gone.", words(self.view))

    async def test_a_failed_forget_is_shown(self):
        failure = "could not delete the private key file of k_1: Permission denied"
        self.delete_key.side_effect = SshKeyError(failure)
        await self.press("ssh-refresh")
        await self.press(self.FORGET)
        await self.answer("Forget")
        self.assertEqual(find(self.view, "ssh-error").content.controls[0].value, failure)
        self.assertEqual(self.app.ssh["log"][-1][1:], (failure, True))
        self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)
        self.assert_enabled(self.INSTALL, self.FORGET)


class ErrorState(Base):
    async def test_a_raised_error_is_shown_logged_and_cleared_by_the_next_action(self):
        self.manager.test.side_effect = HostKeyError("the SSH host key is not trusted yet")
        await self.press("ssh-test")
        self.assertEqual(find(self.view, "ssh-error").content.controls[0].value, "the SSH host key is not trusted yet")
        self.assertEqual(self.app.ssh["log"][-1][1:], ("the SSH host key is not trusted yet", True))
        self.assertIsInstance(self.page.dialogs[-1], ft.SnackBar)  # App.run's toast
        self.assert_enabled(*ACTIONS, "ssh-refresh")
        await self.press("ssh-refresh")
        self.assertEqual(self.app.ssh["error"], "")
        self.assertNotIn("ssh-error", [getattr(c, "key", None) for c in walk(self.view)])
        self.assertIn("the SSH host key is not trusted yet", words(self.view))  # the activity log keeps it

    async def test_a_status_that_failed_to_find_a_route_shows_why(self):
        self.manager.status.return_value = SshStatus(
            profile=replace(PROFILE, transport="usb"), last_error="no camera USB network found"
        )
        await self.press("ssh-refresh")
        text = words(self.view)
        self.assertIn("no camera USB network found", text)
        self.assertIn("Not reachable", text)
        self.assertIn("USB", text)
        self.assertEqual(self.app.ssh["log"][-1][1:], ("no camera USB network found", True))

    async def test_a_refused_arm_is_an_error(self):
        self.manager.arm.return_value = ArmResult(accepted=False, message="SC was not accepted")
        self.manager.status.return_value = status(ssh_up=False)
        await self.press("ssh-arm")
        self.assertIn("SC was not accepted", words(self.view))
        self.assertEqual(self.app.ssh["error"], "SC was not accepted")
        self.assertIn("SSH is off", words(self.view))

    async def test_a_login_the_camera_turned_down_is_an_error(self):
        self.manager.test.return_value = TestResult(ok=False, error="Permission denied (publickey).")
        await self.press("ssh-test")
        self.assertEqual(self.app.ssh["error"], "Permission denied (publickey).")

    async def test_a_key_that_changed_between_looking_and_trusting_is_reported(self):
        self.manager.status.return_value = status(host_key_state="unknown")
        self.manager.trust_host.side_effect = HostKeyError("Nothing was pinned")
        await self.press("ssh-refresh")
        await self.press("ssh-trust")
        await self.answer("Trust")
        self.assertEqual(self.app.ssh["error"], "Nothing was pinned")

    async def test_taking_the_adapter_is_a_question_and_then_a_retry(self):
        self.manager.arm.side_effect = [AdapterInUse("wlan0"), ArmResult(accepted=True, ssh_up=True)]
        await self.press("ssh-arm")
        self.assertEqual(self.app.ssh["error"], "")
        self.assertFalse(self.app.ssh["busy"])
        self.assertIn("wlan0", self.page.dialogs[-1].title.value)
        await self.answer("Use it")
        self.assertTrue(self.svc.allow_busy_adapter)
        self.assertEqual(self.manager.arm.await_count, 2)


class PasswordLogin(Base):
    """The profile is the real saved one, in the temporary config directory. Only the manager's actions are mocked."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager.profile.side_effect = profiles.for_camera
        self.manager.status.side_effect = lambda address: status(profile=profiles.for_camera(address))
        self.manager.test.return_value = TestResult(ok=True, uid="uid=0(root)")

    async def ask(self):
        """Press Test and stop at the question. ``press`` would wait for an answer that isn't coming."""
        find(self.view, "ssh-test").on_click(None)
        for _ in range(500):
            await asyncio.sleep(0.001)
            if self.page.dialogs:
                return self.page.dialogs[-1]
        self.fail("it never asked")

    def assert_chosen(self, chosen, other):
        self.assertIsNotNone(find(self.view, f"ssh-auth-{chosen}").bgcolor)
        self.assertIsNone(find(self.view, f"ssh-auth-{other}").bgcolor)

    async def test_the_choice_appears_once_a_profile_is_known(self):
        self.assertNotIn("ssh-auth", [getattr(c, "key", None) for c in walk(self.view)])
        await self.press("ssh-refresh")
        self.assert_chosen("key", "password")

    async def test_choosing_password_saves_only_that_and_checks_again(self):
        saved = replace(PROFILE, label="Mine", key_id="k_1")
        profiles.save(saved)
        await self.press("ssh-refresh")
        await self.press("ssh-auth-password")
        self.assertEqual(profiles.load(ADDRESS), replace(saved, auth="password"))
        self.assertEqual(self.manager.status.await_count, 2)
        self.assert_chosen("password", "key")
        self.assertEqual(self.page.dialogs, [])  # nothing to store, because nothing is asked for yet

    async def test_choosing_key_goes_back_to_a_test_with_no_question(self):
        profiles.save(replace(PROFILE, auth="password"))
        await self.press("ssh-refresh")
        self.assert_chosen("password", "key")
        await self.press("ssh-auth-key")
        self.assertEqual(profiles.load(ADDRESS).auth, "key")
        self.assert_chosen("key", "password")
        await asyncio.wait_for(self.press("ssh-test"), 5)  # a question here would never be answered
        self.manager.test.assert_awaited_once_with(ADDRESS)

    async def test_a_password_profile_asks_and_the_test_gets_what_was_typed(self):
        profiles.save(replace(PROFILE, auth="password"))
        dialog = await self.ask()
        self.assertTrue(dialog.content.password)
        self.assertEqual(dialog.title.value, "SSH password")
        self.manager.test.assert_not_awaited()  # not before it has been typed
        dialog.content.value = SECRET
        await self.answer("Connect")
        self.manager.test.assert_awaited_once_with(ADDRESS, password=SECRET)
        self.assertEqual(self.page.dialogs, [])
        self.assertEqual(dialog.content.value, "")
        self.assertIn("Login works: uid=0(root)", words(self.view))

    async def test_what_was_typed_is_not_kept_anywhere(self):
        profiles.save(replace(PROFILE, auth="password"))
        dialog = await self.ask()
        dialog.content.value = SECRET
        await self.answer("Connect")
        for kept in (repr(self.app.ssh), words(self.view), repr(store.read())):
            self.assertNotIn(SECRET, kept)

    async def test_cancelling_the_question_sends_nothing(self):
        profiles.save(replace(PROFILE, auth="password"))
        await self.ask()
        await self.answer("Cancel")
        self.manager.test.assert_not_awaited()
        self.assertEqual(self.page.dialogs, [])
        self.assertEqual(self.app.ssh["error"], "")
        self.assert_enabled(*ACTIONS, "ssh-refresh")

    async def test_a_key_profile_is_tested_with_no_question_and_no_password(self):
        profiles.save(replace(PROFILE, auth="key"))
        await asyncio.wait_for(self.press("ssh-test"), 5)  # a question here would never be answered
        self.manager.test.assert_awaited_once_with(ADDRESS)
        self.assertEqual(self.page.dialogs, [])


class Manager(Base):
    def wifi(self):
        """The service's own hold on the camera's Wi-Fi, which on this simulated one would start an HTTP server."""
        return (
            mock.patch.object(self.svc, "http", mock.AsyncMock()),
            mock.patch.object(self.svc, "release_http", mock.AsyncMock()),
        )

    async def test_it_is_made_once_on_first_use(self):
        self.make_manager.assert_not_called()
        self.assertIs(self.app.ssh_manager, self.app.ssh_manager)
        self.make_manager.assert_called_once()

    async def test_its_network_context_holds_wifi_through_the_service(self):
        self.app.ssh_manager  # noqa: B018 - make it
        context = self.make_manager.call_args.kwargs["network_context"]
        patch_http, patch_release = self.wifi()
        with patch_http as http, patch_release as release:
            async with context(PROFILE):
                http.assert_awaited_once_with()
                release.assert_not_awaited()
            release.assert_awaited_once_with()

    async def test_it_lets_go_of_the_wifi_when_the_action_fails(self):
        patch_http, patch_release = self.wifi()
        with patch_http, patch_release as release, self.assertRaisesRegex(SshError, "lost"):
            async with self.app._ssh_network(PROFILE):
                raise SshError("lost")
        release.assert_awaited_once_with()

    async def test_wifi_it_never_got_isnt_released_and_the_question_about_the_adapter_gets_through(self):
        patch_http, patch_release = self.wifi()
        with patch_http as http, patch_release as release, self.assertRaises(AdapterInUse):
            http.side_effect = AdapterInUse("wlan0")
            async with self.app._ssh_network(PROFILE):
                self.fail("entered without Wi-Fi")
        release.assert_not_awaited()

    async def test_it_follows_the_service_when_demo_mode_swaps_it(self):
        self.app.ssh_manager  # noqa: B018 - make it
        context = self.make_manager.call_args.kwargs["network_context"]
        other = mock.Mock(cam=object(), http=mock.AsyncMock(), release_http=mock.AsyncMock())
        with mock.patch.object(self.app, "svc", other):
            async with context(PROFILE):
                other.http.assert_awaited_once_with()
        other.release_http.assert_awaited_once_with()

    async def test_the_service_it_started_with_is_the_one_that_lets_go(self):
        patch_http, patch_release = self.wifi()
        replacement = mock.Mock(release_http=mock.AsyncMock())
        with patch_http, patch_release as release:
            async with self.app._ssh_network(PROFILE):
                self.app.svc = replacement
            release.assert_awaited_once_with()
        replacement.release_http.assert_not_awaited()
        self.app.svc = self.svc

    async def test_with_no_camera_connected_it_raises(self):
        await self.svc.disconnect()
        patch_http, patch_release = self.wifi()
        with patch_http as http, patch_release as release, self.assertRaisesRegex(SshError, "No camera is connected"):
            async with self.app._ssh_network(PROFILE):
                self.fail("entered without a camera")
        http.assert_not_awaited()
        release.assert_not_awaited()

    async def test_a_simulated_camera_never_reaches_the_real_wifi_adapter(self):
        self.app.sim = True
        patch_http, patch_release = self.wifi()
        with patch_http as http, patch_release as release, self.assertRaisesRegex(SshError, "demo mode"):
            async with self.app._ssh_network(PROFILE):
                self.fail("entered with a simulated camera")
        http.assert_not_awaited()
        release.assert_not_awaited()

    async def test_idle_wifi_is_kept_while_an_ssh_action_runs(self):
        """A held Wi-Fi may be the route the action is on: letting it go would end the session."""
        self.svc.state = replace(self.svc.state, http=True)
        with (
            mock.patch.object(gui_app, "WIFI_IDLE_SECONDS", 0),
            mock.patch.object(self.svc, "release_http", mock.AsyncMock()) as release,
        ):
            self.app.ssh["busy"] = True
            self.app.release_wifi_soon()
            await self.settle()
            release.assert_not_awaited()
            self.app.ssh["busy"] = False
            self.app.release_wifi_soon()
            await self.settle()
            release.assert_awaited_once_with()
        self.svc.state = replace(self.svc.state, http=False)

    async def test_finishing_an_action_starts_the_wait_to_release_wifi(self):
        self.svc.state = replace(self.svc.state, http=True)
        self.manager.test.return_value = TestResult(ok=True)
        with (
            mock.patch.object(gui_app, "WIFI_IDLE_SECONDS", 0),
            mock.patch.object(self.svc, "release_http", mock.AsyncMock()) as release,
        ):
            await self.press("ssh-test")
            release.assert_awaited_once_with()
        self.svc.state = replace(self.svc.state, http=False)


def real_quak_ssh():
    """The extension's QuakSsh, loaded from its file: the extension isn't installed for these tests."""
    path = ROOT / "extensions/quak_native/src/quak_native/quak_ssh.py"
    spec = importlib.util.spec_from_file_location("quak_ssh_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {spec.name: module}):
        spec.loader.exec_module(module)
    return module.QuakSsh


class NativeLoginTest(Base):
    """Test where there's no system ssh, as on a phone: the Dart client logs in, given what the terminal would be."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.which.return_value = None
        self.native = mock.Mock(test=mock.AsyncMock(return_value=LOGGED_IN))
        module = types.ModuleType("quak_native")
        module.QuakSsh = lambda: self.native
        self.patch(mock.patch.dict(sys.modules, {"quak_native": module}))
        self.http = self.patch(mock.patch.object(self.svc, "http", mock.AsyncMock()))
        self.release = self.patch(mock.patch.object(self.svc, "release_http", mock.AsyncMock()))
        self.manager.resolve.return_value = WIFI  # the one route with a Wi-Fi to let go of
        self.manager.terminal_credentials.return_value = CREDENTIALS

    async def ask(self, **credentials):
        """Press Test and stop at the question. ``press`` would wait for an answer that isn't coming."""
        self.manager.terminal_credentials.return_value = replace(CREDENTIALS, **credentials)
        find(self.view, "ssh-test").on_click(None)
        for _ in range(500):
            await asyncio.sleep(0.001)
            if self.page.dialogs:
                return self.page.dialogs[-1]
        self.fail("it never asked")

    def assert_tested_with(self, *, passphrase=None, password=None, private_key_pem=NATIVE_PEM):
        self.native.test.assert_awaited_once_with(
            "10.5.5.9",
            2222,
            "root",
            password=password,
            private_key_pem=private_key_pem,
            passphrase=passphrase,
            expected_fingerprint="SHA256:seen",
        )
        call = self.native.test.await_args
        inspect.signature(real_quak_ssh().test).bind(None, *call.args, **call.kwargs)  # the fake takes anything
        self.manager.test.assert_not_awaited()

    async def test_it_logs_in_through_the_dart_client_with_the_terminals_credentials(self):
        await self.press("ssh-test")
        self.manager.resolve.assert_awaited_once_with(ADDRESS)
        self.manager.terminal_credentials.assert_awaited_once_with(ADDRESS)
        self.assert_tested_with()
        self.assertEqual(self.page.dialogs, [])  # a key without a passphrase asks nothing
        self.assertIn("Login works: uid=0(root) gid=0(root) · Linux 4.9.0 aarch64", words(self.view))
        self.assertEqual(self.app.ssh["error"], "")
        self.assert_enabled(*ACTIONS, "ssh-refresh")

    async def test_the_real_service_takes_these_fields(self):
        service = real_quak_ssh()
        login = {"password": None, "private_key_pem": NATIVE_PEM, "passphrase": None, "expected_fingerprint": "SHA256:x"}
        inspect.signature(service.test).bind(None, "10.5.5.9", 22, "root", **login)
        inspect.signature(service.exec).bind(None, "10.5.5.9", 22, "root", command="id", **login)
        with self.assertRaises(TypeError):  # a login that can't check the server has to be asked for by name
            inspect.signature(service.test).bind(None, "10.5.5.9", 22, "root")

    async def test_on_a_wifi_route_it_holds_the_wifi_through_the_login_then_lets_go(self):
        async def credentials(address):
            self.http.assert_awaited_once_with()
            return CREDENTIALS

        async def test(*args, **kwargs):
            self.release.assert_not_awaited()
            return LOGGED_IN

        self.manager.terminal_credentials.side_effect = credentials
        self.native.test.side_effect = test
        await self.press("ssh-test")
        self.native.test.assert_awaited_once()
        self.http.assert_awaited_once_with()
        self.release.assert_awaited_once_with()

    async def test_usb_and_host_routes_stay_off_the_wifi(self):
        for endpoint in (USB, Endpoint("192.168.1.50", transport="host")):
            with self.subTest(endpoint.transport):
                self.manager.resolve.return_value = endpoint
                await self.press("ssh-test")
        self.assertEqual(self.native.test.await_count, 2)
        self.http.assert_not_awaited()
        self.release.assert_not_awaited()

    async def test_a_locked_key_asks_for_its_passphrase_as_the_terminal_does(self):
        dialog = await self.ask(passphrase_required=True)
        self.assertEqual(dialog.title.value, "Passphrase for this SSH key")
        self.assertTrue(dialog.content.password)
        self.native.test.assert_not_awaited()  # not before it has been typed
        self.release.assert_not_awaited()  # the Wi-Fi is kept while the question is up
        dialog.content.value = SECRET
        await self.answer("Connect")
        self.assert_tested_with(passphrase=SECRET)
        self.assertEqual(dialog.content.value, "")
        self.release.assert_awaited_once_with()
        for kept in (repr(self.app.ssh), words(self.view)):
            self.assertNotIn(SECRET, kept)

    async def test_a_password_profile_asks_for_the_password_as_the_terminal_does(self):
        dialog = await self.ask(private_key_pem=None, password_required=True)
        self.assertEqual(dialog.title.value, "Password for root@10.5.5.9")
        self.assertTrue(dialog.content.password)
        dialog.content.value = SECRET
        await self.answer("Connect")
        self.assert_tested_with(password=SECRET, private_key_pem=None)
        self.assertEqual(self.page.dialogs, [])
        self.assertNotIn(SECRET, repr(self.app.ssh))

    async def test_cancelling_a_question_tests_nothing_and_lets_go_of_the_wifi(self):
        for credentials in ({"passphrase_required": True}, {"private_key_pem": None, "password_required": True}):
            with self.subTest(**credentials):
                self.release.reset_mock()
                await self.ask(**credentials)
                await self.answer("Cancel")
                self.release.assert_awaited_once_with()
                self.assertEqual(self.page.dialogs, [])
        self.native.test.assert_not_awaited()
        self.assertEqual(self.app.ssh["error"], "")
        self.assert_enabled(*ACTIONS, "ssh-refresh")

    async def test_a_login_the_dart_client_turned_down_is_shown(self):
        mismatch = "Host key mismatch: expected SHA256:seen, the server offered SHA256:other"
        self.native.test.return_value = {"ok": False, "uid": "", "uname": "", "error": mismatch}
        await self.press("ssh-test")
        self.assertEqual(find(self.view, "ssh-error").content.controls[0].value, mismatch)
        self.assertEqual(self.app.ssh["log"][-1][1:], (mismatch, True))
        self.assertNotIn("Login works", words(self.view))
        self.release.assert_awaited_once_with()
        self.assert_enabled(*ACTIONS, "ssh-refresh")

    async def test_a_failure_with_no_reason_still_says_the_login_failed(self):
        self.native.test.return_value = {"ok": False, "uid": "", "uname": "", "error": ""}
        await self.press("ssh-test")
        self.assertEqual(self.app.ssh["error"], "The camera didn't accept the login.")

    async def test_the_wifi_is_let_go_of_when_something_raises(self):
        for failure in ("credentials", "dart"):
            with self.subTest(failure):
                self.release.reset_mock()
                if failure == "credentials":
                    self.manager.terminal_credentials.side_effect = HostKeyError("the SSH host key has changed")
                    expected = "the SSH host key has changed"
                else:
                    self.manager.terminal_credentials.side_effect = None
                    self.native.test.side_effect = RuntimeError("Unknown QuakSsh method: test")  # an older extension
                    expected = "Unknown QuakSsh method: test"
                await self.press("ssh-test")
                self.assertEqual(self.app.ssh["error"], expected)
                self.release.assert_awaited_once_with()
                self.assert_enabled(*ACTIONS, "ssh-refresh")

    async def test_with_a_system_ssh_the_manager_tests_as_before(self):
        self.which.return_value = "/usr/bin/ssh"
        self.manager.test.return_value = TestResult(ok=True, uid="uid=0(root)")
        await self.press("ssh-test")
        self.which.assert_any_call("ssh")
        self.manager.test.assert_awaited_once_with(ADDRESS)
        self.native.test.assert_not_awaited()
        self.manager.resolve.assert_not_awaited()
        self.manager.terminal_credentials.assert_not_awaited()
        self.http.assert_not_awaited()  # the manager holds the Wi-Fi itself on this path
        self.release.assert_not_awaited()
        self.assertIn("Login works: uid=0(root)", words(self.view))

    async def test_without_the_extension_the_manager_tests_too(self):
        self.manager.test.return_value = TestResult(ok=True)
        for module in (None, types.ModuleType("quak_native")):  # None makes the import raise ImportError
            with self.subTest(module=module), mock.patch.dict(sys.modules, {"quak_native": module}):
                self.manager.test.reset_mock()
                await self.press("ssh-test")
                self.manager.test.assert_awaited_once_with(ADDRESS)
        self.native.test.assert_not_awaited()
        self.manager.terminal_credentials.assert_not_awaited()


class Wiring(Base):
    async def test_more_has_four_tabs_and_the_ssh_one_opens_the_screen(self):
        self.app.more_tab = "camera"
        more = screens.more(self.app)
        for tab in ("camera", "lab", "ssh", "diagnostics"):
            find(more, f"more-tab-{tab}")
        self.assertNotIn("ssh-arm", [getattr(c, "key", None) for c in walk(more)])
        find(more, "more-tab-ssh").on_click(None)
        self.assertEqual(self.app.more_tab, "ssh")
        find(self.page.controls, "ssh-arm")

    async def test_the_desktop_rail_has_an_item_for_it(self):
        self.page.width = 1400
        self.app.more_tab = "camera"
        self.app.render()
        item = next(c for c in walk(self.page.controls) if getattr(c, "tooltip", None) == "SSH")
        self.assertIsNone(item.bgcolor)
        item.on_click(None)
        self.assertEqual(self.app.more_tab, "ssh")
        find(self.page.controls, "ssh-arm")
        item = next(c for c in walk(self.page.controls) if getattr(c, "tooltip", None) == "SSH")
        self.assertEqual(item.bgcolor, self.app.t.accent_tint)

    async def test_the_tab_builds_through_more_in_a_wide_window_too(self):
        self.page.width = 1400
        self.app.render()
        find(self.page.controls, "ssh-refresh")


if __name__ == "__main__":
    unittest.main()

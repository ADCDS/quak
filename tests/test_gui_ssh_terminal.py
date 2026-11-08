"""The SSH tab's embedded terminal, against a fake QuakTerminal and a mocked SshManager, and the credentials it's given.

No real ssh, no subprocess and no network: both ``asyncSetUp`` methods make all three fail the test if anything reaches
for them. A session that doesn't end keeps the camera's Wi-Fi forever, so most of this is about who lets go of it.
"""

import asyncio
import dataclasses
import importlib.util
import inspect
import os
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quak.core.errors import AdapterInUse, HostKeyError, NetworkError, SshKeyError
from quak.ssh import hostkeys, keys, profiles, transport
from quak.ssh.manager import SshManager, TerminalCredentials
from quak.ssh.models import Endpoint, HostKey, SshProfile
from tests.test_gui_ssh import ACTIONS, ADDRESS, PROFILE, USB, WIFI, Base, find, ft, walk, words
from tests.test_ssh_manager_keys import ADDRESS as CAMERA
from tests.test_ssh_manager_keys import PEM, PEM_BODY, pin, scanned, store_key

try:
    from quak.gui import app as gui_app
except ImportError:  # GUI extra not installed
    gui_app = None

ROOT = Path(__file__).resolve().parent.parent
SECRET = "correct horse battery staple"
LAN = Endpoint("192.168.1.50", transport="host")
DART_FINGERPRINT = "SHA256:from-dart"
CREDENTIALS = TerminalCredentials(
    host="172.20.145.51",
    ssh_port=2222,
    username="root",
    private_key_pem=PEM,
    passphrase_required=False,
    expected_fingerprint="SHA256:seen",
    profile=PROFILE,
)
FIELDS = {
    "host",
    "port",
    "username",
    "private_key_pem",
    "passphrase",
    "password",
    "expected_fingerprint",
    "font_size",
    "on_connected",
    "on_disconnected",
    "on_error",
}


class FakeTerminal:
    """Shaped like quak_native.QuakTerminal (RealControl checks the fields). ``created_with`` keeps what it was given."""

    def __init__(self, **fields):
        self.created_with = dict(fields)
        vars(self).update(fields)
        self.connect = mock.AsyncMock()
        self.disconnect = mock.AsyncMock()
        self.update = mock.Mock()


class TerminalCase(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.terminals = []
        self.connect_effect = None

        def make(**fields):
            terminal = FakeTerminal(**fields)
            terminal.connect.side_effect = self.connect_effect
            self.terminals.append(terminal)
            return terminal

        module = types.ModuleType("quak_native")
        module.QuakTerminal = make
        self.patch(mock.patch.dict(sys.modules, {"quak_native": module}))
        self.http = self.patch(mock.patch.object(self.svc, "http", mock.AsyncMock()))
        self.release = self.patch(mock.patch.object(self.svc, "release_http", mock.AsyncMock()))
        self.manager.terminal_credentials.return_value = CREDENTIALS
        self.manager.resolve.return_value = WIFI  # most of this is about the one route with a Wi-Fi to let go of

    async def open(self) -> FakeTerminal:
        await self.press("ssh-terminal")
        self.assertEqual(len(self.terminals), 1)
        return self.terminals[0]

    async def until(self, check):
        for _ in range(500):
            if check():
                return
            await asyncio.sleep(0.001)
        self.fail("it never happened")

    async def ask(self, **credentials):
        """Press Open terminal and stop at the question. ``press`` would wait for an answer that isn't coming."""
        self.manager.terminal_credentials.return_value = replace(CREDENTIALS, **credentials)
        find(self.view, "ssh-terminal").on_click(None)
        await self.until(lambda: self.page.dialogs)
        return self.page.dialogs[-1]

    async def ask_for_passphrase(self):
        return await self.ask(passphrase_required=True)

    async def ask_for_password(self):
        return await self.ask(private_key_pem=None, password_required=True)

    def on_page(self, terminal) -> bool:
        return any(c is terminal for c in walk(self.page.controls))

    def assert_closed(self):
        self.assertIsNone(self.app.ssh["terminal"])
        self.assertFalse(self.app.ssh["busy"])
        self.assert_enabled(*ACTIONS, "ssh-refresh")
        self.assertNotIn("ssh-terminal-disconnect", [getattr(c, "key", None) for c in walk(self.view)])


class Opening(TerminalCase):
    async def test_the_control_is_made_from_the_credentials_and_connects(self):
        terminal = await self.open()
        self.assertEqual(set(terminal.created_with), FIELDS)
        made = terminal.created_with
        self.assertEqual(made["host"], "172.20.145.51")
        self.assertEqual(made["port"], 2222)
        self.assertEqual(made["username"], "root")
        self.assertEqual(made["private_key_pem"], PEM)
        self.assertIsNone(made["passphrase"])
        self.assertIsNone(made["password"])
        self.assertEqual(made["expected_fingerprint"], "SHA256:seen")
        self.assertEqual(made["font_size"], 14)
        for handler in ("on_connected", "on_disconnected", "on_error"):
            self.assertTrue(callable(made[handler]), handler)
        terminal.connect.assert_awaited_once_with()
        self.manager.terminal_credentials.assert_awaited_once_with(ADDRESS)
        self.manager.open_terminal.assert_not_awaited()

    async def test_the_wifi_is_held_first_and_kept(self):
        async def credentials(address):
            self.http.assert_awaited_once_with()
            return CREDENTIALS

        self.manager.terminal_credentials.side_effect = credentials
        await self.open()
        self.http.assert_awaited_once_with()
        self.release.assert_not_awaited()

    async def test_it_shows_on_the_screen_with_a_disconnect_button(self):
        terminal = await self.open()
        self.assertTrue(self.on_page(terminal))
        self.assertEqual(find(self.page.controls, "ssh-terminal-disconnect").content, "Disconnect")
        self.assertIn("Connecting…", words(self.page.controls))

    async def test_it_is_on_the_page_before_it_connects(self):
        placed = []
        self.connect_effect = lambda: placed.append(self.on_page(self.terminals[0]))
        await self.open()
        self.assertEqual(placed, [True])

    async def test_a_redraw_leaves_it_alone(self):
        """A rebuild would take the control off the page, and that hangs up the session."""
        await self.open()
        before = self.page.controls[-1]
        self.app.render()
        self.assertIs(self.page.controls[-1], before)

    async def test_connected_is_shown_without_a_rebuild(self):
        terminal = await self.open()
        before = self.page.controls[-1]
        terminal.created_with["on_connected"](None)
        await self.settle()
        self.assertIs(self.page.controls[-1], before)
        self.assertEqual(self.app.ssh["terminal"].state.value, "Connected")
        self.assertEqual(self.app.ssh["log"][-1][1:], ("Connected", False))

    async def test_everything_else_waits_while_it_is_open(self):
        """Any of these could end by letting go of the Wi-Fi the session is on."""
        await self.open()
        self.assertTrue(self.app.ssh["busy"])
        self.assert_disabled(*ACTIONS, "ssh-refresh", "ssh-trust", "ssh-forget")
        await self.press("ssh-test")
        self.manager.test.assert_not_awaited()

    async def test_idle_wifi_is_kept_while_it_is_open(self):
        self.svc.state = replace(self.svc.state, http=True)
        with mock.patch.object(gui_app, "WIFI_IDLE_SECONDS", 0):
            await self.open()
            self.app.release_wifi_soon()
            await self.settle()
            self.release.assert_not_awaited()
        self.svc.state = replace(self.svc.state, http=False)

    async def test_the_control_stops_holding_the_secrets_once_connected(self):
        given = []
        self.connect_effect = lambda: given.append(self.terminals[0].private_key_pem)
        terminal = await self.open()
        self.assertEqual(given, [PEM])
        self.assertIsNone(terminal.private_key_pem)
        self.assertIsNone(terminal.passphrase)
        self.assertIsNone(terminal.password)
        terminal.update.assert_called_once_with()


class Passphrase(TerminalCase):
    async def test_a_key_without_one_asks_nothing(self):
        await self.open()
        self.assertEqual(self.page.dialogs, [])

    async def test_a_locked_key_asks_with_a_password_field_before_anything_is_made(self):
        dialog = await self.ask_for_passphrase()
        self.assertTrue(dialog.content.password)
        self.assertEqual(self.terminals, [])
        self.http.assert_awaited_once_with()
        self.release.assert_not_awaited()  # the Wi-Fi is kept while the question is up
        self.assertTrue(self.app.ssh["busy"])
        await self.answer("Cancel")

    async def test_what_is_typed_reaches_the_control_and_goes_no_further(self):
        dialog = await self.ask_for_passphrase()
        field = dialog.content
        field.value = SECRET
        await self.answer("Connect")
        terminal = self.terminals[0]
        self.assertEqual(terminal.created_with["passphrase"], SECRET)
        terminal.connect.assert_awaited_once_with()
        self.assertEqual(self.page.dialogs, [])
        self.assertEqual(field.value, "")
        self.assertIsNone(terminal.passphrase)
        self.assertNotIn(SECRET, repr(self.app.ssh))
        self.assertNotIn(SECRET, words(self.view))

    async def test_enter_in_the_field_connects(self):
        dialog = await self.ask_for_passphrase()
        dialog.content.value = SECRET
        dialog.content.on_submit(None)
        await self.settle()
        self.assertEqual(self.terminals[0].created_with["passphrase"], SECRET)

    async def test_cancelling_makes_nothing_and_lets_go_of_the_wifi(self):
        await self.ask_for_passphrase()
        await self.answer("Cancel")
        self.assertEqual(self.terminals, [])
        self.release.assert_awaited_once_with()
        self.assertEqual(self.app.ssh["error"], "")
        self.assert_closed()

    async def test_tapping_outside_the_dialog_counts_as_cancelling(self):
        dialog = await self.ask_for_passphrase()
        self.page.pop_dialog()
        dialog.on_dismiss(None)
        await self.settle()
        self.assertEqual(self.terminals, [])
        self.release.assert_awaited_once_with()
        self.assert_closed()

    async def test_the_dismissal_after_connect_changes_nothing(self):
        dialog = await self.ask_for_passphrase()
        dialog.content.value = SECRET
        await self.answer("Connect")
        dialog.on_dismiss(None)
        await self.settle()
        self.assertEqual(self.terminals[0].created_with["passphrase"], SECRET)
        self.release.assert_not_awaited()


class Password(TerminalCase):
    async def test_a_profile_with_no_login_asks_nothing(self):
        self.manager.terminal_credentials.return_value = replace(CREDENTIALS, private_key_pem=None)
        terminal = await self.open()
        self.assertEqual(self.page.dialogs, [])
        self.assertIsNone(terminal.created_with["private_key_pem"])
        self.assertIsNone(terminal.created_with["password"])

    async def test_a_password_profile_asks_with_a_password_field_before_anything_is_made(self):
        dialog = await self.ask_for_password()
        self.assertTrue(dialog.content.password)
        self.assertIn("root@172.20.145.51", dialog.title.value)
        self.assertEqual(self.terminals, [])
        self.http.assert_awaited_once_with()
        self.release.assert_not_awaited()  # the Wi-Fi is kept while the question is up
        self.assertTrue(self.app.ssh["busy"])
        await self.answer("Cancel")

    async def test_what_is_typed_reaches_the_control_and_goes_no_further(self):
        given = []
        self.connect_effect = lambda: given.append(self.terminals[0].password)
        dialog = await self.ask_for_password()
        field = dialog.content
        field.value = SECRET
        await self.answer("Connect")
        terminal = self.terminals[0]
        self.assertEqual(given, [SECRET])
        self.assertEqual(terminal.created_with["password"], SECRET)
        self.assertIsNone(terminal.created_with["private_key_pem"])
        self.assertIsNone(terminal.created_with["passphrase"])
        terminal.connect.assert_awaited_once_with()
        self.assertEqual(self.page.dialogs, [])
        self.assertEqual(field.value, "")
        self.assertIsNone(terminal.password)
        terminal.update.assert_called_once_with()  # what takes the wipe to the Dart side
        self.assertNotIn(SECRET, repr(self.app.ssh))
        self.assertNotIn(SECRET, words(self.view))

    async def test_enter_in_the_field_connects(self):
        dialog = await self.ask_for_password()
        dialog.content.value = SECRET
        dialog.content.on_submit(None)
        await self.settle()
        self.assertEqual(self.terminals[0].created_with["password"], SECRET)

    async def test_cancelling_makes_nothing_and_lets_go_of_the_wifi(self):
        await self.ask_for_password()
        await self.answer("Cancel")
        self.assertEqual(self.terminals, [])
        self.release.assert_awaited_once_with()
        self.assertEqual(self.app.ssh["error"], "")
        self.assert_closed()

    async def test_tapping_outside_the_dialog_counts_as_cancelling(self):
        dialog = await self.ask_for_password()
        self.page.pop_dialog()
        dialog.on_dismiss(None)
        await self.settle()
        self.assertEqual(self.terminals, [])
        self.release.assert_awaited_once_with()
        self.assert_closed()

    async def test_a_connect_that_fails_leaves_no_password_on_the_control(self):
        self.connect_effect = TimeoutError("no answer")
        dialog = await self.ask_for_password()
        dialog.content.value = SECRET
        await self.answer("Connect")
        terminal = self.terminals[0]
        self.assertEqual(terminal.created_with["password"], SECRET)
        self.assertIsNone(terminal.password)
        self.release.assert_awaited_once_with()
        self.assertEqual(self.app.ssh["error"], "no answer")
        self.assert_closed()


class Ending(TerminalCase):
    async def test_disconnect_hangs_up_and_lets_go_of_the_wifi(self):
        terminal = await self.open()
        await self.press("ssh-terminal-disconnect")
        terminal.disconnect.assert_awaited_once_with()
        self.release.assert_awaited_once_with()
        self.assertFalse(self.on_page(terminal))
        self.assert_closed()
        self.assertIn("SSH session ended.", words(self.view))

    async def test_disconnect_that_fails_still_lets_go(self):
        terminal = await self.open()
        terminal.disconnect.side_effect = TimeoutError("no answer")
        await self.press("ssh-terminal-disconnect")
        self.release.assert_awaited_once_with()
        self.assert_closed()

    async def test_the_event_after_disconnect_changes_nothing(self):
        terminal = await self.open()
        await self.press("ssh-terminal-disconnect")
        terminal.created_with["on_disconnected"](None)
        await self.settle()
        self.release.assert_awaited_once_with()

    async def test_the_session_ending_on_its_own_lets_go(self):
        terminal = await self.open()
        terminal.created_with["on_disconnected"](None)
        await self.settle()
        terminal.disconnect.assert_not_awaited()
        self.release.assert_awaited_once_with()
        self.assertFalse(self.on_page(terminal))
        self.assert_closed()

    async def test_an_error_is_shown_and_lets_go(self):
        terminal = await self.open()
        terminal.created_with["on_error"](SimpleNamespace(message="Host key mismatch"))
        await self.settle()
        self.release.assert_awaited_once_with()
        self.assertEqual(find(self.view, "ssh-error").content.controls[0].value, "Host key mismatch")
        self.assertEqual(self.app.ssh["log"][-1][1:], ("Host key mismatch", True))
        self.assert_closed()

    async def test_an_error_and_then_the_end_of_the_session_let_go_once(self):
        """A connection that dies sends both, in this order."""
        terminal = await self.open()
        terminal.created_with["on_error"](SimpleNamespace(message="Connection reset"))
        terminal.created_with["on_disconnected"](None)
        await self.settle()
        self.release.assert_awaited_once_with()
        self.assertEqual(self.app.ssh["error"], "Connection reset")

    async def test_it_stays_busy_until_the_wifi_has_been_let_go_of(self):
        """An action started sooner could be handed the Wi-Fi that's on its way out."""
        terminal = await self.open()
        busy = []

        async def release():
            busy.append(self.app.ssh["busy"])

        self.release.side_effect = release
        terminal.created_with["on_disconnected"](None)
        await self.settle()
        self.assertEqual(busy, [True])
        self.assertFalse(self.app.ssh["busy"])

    async def test_leaving_the_tab_lets_go_with_no_event_to_say_so(self):
        """Off the page the control hangs up silently, so nothing else would ever release the Wi-Fi."""
        terminal = await self.open()
        self.app.go("more", more_tab="lab")
        await self.settle()
        self.release.assert_awaited_once_with()
        self.assertFalse(self.on_page(terminal))
        self.assertIsNone(self.app.ssh["terminal"])
        self.assertFalse(self.app.ssh["busy"])

    async def test_going_to_the_tab_it_is_on_changes_nothing(self):
        terminal = await self.open()
        self.app.go("more", more_tab="ssh")
        await self.settle()
        self.release.assert_not_awaited()
        self.assertTrue(self.on_page(terminal))

    async def test_the_service_that_got_the_wifi_is_the_one_that_lets_go(self):
        terminal = await self.open()
        other = SimpleNamespace(cam=self.svc.cam, state=self.svc.state, release_http=mock.AsyncMock())
        self.app.svc = other
        terminal.created_with["on_disconnected"](None)
        await self.settle()
        self.release.assert_awaited_once_with()
        other.release_http.assert_not_awaited()

    async def test_it_can_be_opened_again(self):
        await self.open()
        await self.press("ssh-terminal-disconnect")
        await self.press("ssh-terminal")
        self.assertEqual(len(self.terminals), 2)
        self.assertTrue(self.on_page(self.terminals[1]))
        self.assertEqual(self.http.await_count, 2)
        self.release.assert_awaited_once_with()


class Failing(TerminalCase):
    async def test_credentials_it_cant_get_let_go_and_make_nothing(self):
        self.manager.terminal_credentials.side_effect = HostKeyError("the SSH host key is not trusted yet")
        await self.press("ssh-terminal")
        self.assertEqual(self.terminals, [])
        self.release.assert_awaited_once_with()
        self.assertEqual(self.app.ssh["error"], "the SSH host key is not trusted yet")
        self.assert_closed()

    async def test_a_connect_that_fails_lets_go_and_clears_the_screen(self):
        self.connect_effect = TimeoutError("no answer")
        await self.press("ssh-terminal")
        self.release.assert_awaited_once_with()
        self.assertFalse(self.on_page(self.terminals[0]))
        self.assertEqual(self.app.ssh["error"], "no answer")
        self.assert_closed()

    async def test_wifi_it_never_got_isnt_released_and_the_question_about_the_adapter_gets_through(self):
        self.http.side_effect = [AdapterInUse("wlan0"), None]
        await self.press("ssh-terminal")
        self.manager.terminal_credentials.assert_not_awaited()
        self.release.assert_not_awaited()
        self.assertEqual(self.app.ssh["error"], "")
        self.assertIn("wlan0", self.page.dialogs[-1].title.value)
        await self.answer("Use it")
        self.assertEqual(len(self.terminals), 1)
        self.terminals[0].connect.assert_awaited_once_with()

    async def test_leaving_while_it_gets_ready_makes_nothing_and_lets_go(self):
        gate = asyncio.Event()

        async def credentials(address):
            await gate.wait()
            return CREDENTIALS

        self.manager.terminal_credentials.side_effect = credentials
        find(self.view, "ssh-terminal").on_click(None)
        await self.until(lambda: self.manager.terminal_credentials.await_count)
        self.app.go("more", more_tab="lab")
        gate.set()
        await self.settle()
        self.assertEqual(self.terminals, [])
        self.release.assert_awaited_once_with()
        self.assertFalse(self.app.ssh["busy"])


class Routes(TerminalCase):
    """USB and host routes have no use for the camera's Wi-Fi, and joining it can take this computer off its network."""

    def assert_wifi_untouched(self):
        self.http.assert_not_awaited()
        self.release.assert_not_awaited()

    async def test_the_route_is_known_before_the_wifi_is_held(self):
        order = []
        self.manager.resolve.side_effect = lambda address: order.append("resolve") or WIFI
        self.http.side_effect = lambda: order.append("http")
        await self.open()
        self.manager.resolve.assert_awaited_once_with(ADDRESS)
        self.assertEqual(order, ["resolve", "http"])

    async def test_usb_and_host_routes_open_and_close_without_touching_the_wifi(self):
        for endpoint in (USB, LAN):
            with self.subTest(endpoint.transport):
                self.manager.resolve.return_value = endpoint
                await self.press("ssh-terminal")
                self.terminals[-1].connect.assert_awaited_once_with()
                self.assertTrue(self.app.ssh["busy"])
                await self.press("ssh-terminal-disconnect")
                self.terminals[-1].disconnect.assert_awaited_once_with()
                self.assert_closed()
                self.assert_wifi_untouched()
        self.assertEqual(len(self.terminals), 2)

    async def test_off_wifi_no_bluetooth_camera_is_needed(self):
        await self.svc.disconnect()
        self.release.reset_mock()  # disconnecting lets go of the Wi-Fi itself; what counts is what the terminal does
        self.manager.resolve.return_value = USB
        with mock.patch.dict(os.environ, {"QUAK_CAMERA_ADDRESS": CAMERA}):
            await self.press("ssh-terminal")
        self.manager.resolve.assert_awaited_once_with(CAMERA)
        self.assertEqual(len(self.terminals), 1)
        self.assert_wifi_untouched()

    async def test_off_wifi_every_way_of_ending_lets_go_of_nothing(self):
        endings = {
            "on its own": lambda t: t.created_with["on_disconnected"](None),
            "with an error": lambda t: t.created_with["on_error"](SimpleNamespace(message="Connection reset")),
            "by leaving the tab": lambda t: self.app.go("more", more_tab="lab"),
        }
        self.manager.resolve.return_value = USB
        for name, end in endings.items():
            with self.subTest(name):
                self.app.more_tab = "ssh"
                await self.press("ssh-terminal")
                end(self.terminals[-1])
                await self.settle()
                self.assertIsNone(self.app.ssh["terminal"])
                self.assertFalse(self.app.ssh["busy"])
                self.assert_wifi_untouched()

    async def test_off_wifi_a_failure_while_opening_lets_go_of_nothing(self):
        self.manager.resolve.return_value = LAN
        self.manager.terminal_credentials.side_effect = HostKeyError("not trusted yet")
        await self.press("ssh-terminal")
        self.assertEqual(self.app.ssh["error"], "not trusted yet")
        self.assert_closed()
        self.assert_wifi_untouched()

    async def test_off_wifi_a_connect_that_fails_lets_go_of_nothing(self):
        self.manager.resolve.return_value = LAN
        self.connect_effect = TimeoutError("no answer")
        await self.press("ssh-terminal")
        self.assertEqual(self.app.ssh["error"], "no answer")
        self.assert_closed()
        self.assert_wifi_untouched()

    async def test_off_wifi_cancelling_the_passphrase_lets_go_of_nothing(self):
        self.manager.resolve.return_value = USB
        await self.ask_for_passphrase()
        await self.answer("Cancel")
        self.assertEqual(self.terminals, [])
        self.assert_closed()
        self.assert_wifi_untouched()

    async def test_off_wifi_cancelling_the_password_lets_go_of_nothing(self):
        self.manager.resolve.return_value = USB
        await self.ask_for_password()
        await self.answer("Cancel")
        self.assertEqual(self.terminals, [])
        self.assert_closed()
        self.assert_wifi_untouched()

    async def test_a_route_that_cant_be_found_joins_nothing_and_says_why(self):
        self.manager.resolve.side_effect = NetworkError("no camera USB network found")
        await self.press("ssh-terminal")
        self.manager.terminal_credentials.assert_not_awaited()
        self.assertEqual(self.terminals, [])
        self.assertEqual(self.app.ssh["error"], "no camera USB network found")
        self.assert_closed()
        self.assert_wifi_untouched()


class NativeScan(Base):
    """What the app gives the manager to scan with. There's no ssh-keyscan, as on a phone, unless a test adds it."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.which = self.patch(mock.patch.object(gui_app.shutil, "which", return_value=None))

    def extension(self, **names):
        module = types.ModuleType("quak_native")
        vars(module).update(names)
        return mock.patch.dict(sys.modules, {"quak_native": module})

    def scan_made_by_the_app(self):
        self.app.ssh_manager  # noqa: B018 - make it
        return self.make_manager.call_args.kwargs["scan_async"]

    async def test_without_ssh_keyscan_it_scans_through_the_dart_client(self):
        found = {"key_type": "ssh-ed25519", "fingerprint": DART_FINGERPRINT}
        service = mock.Mock(scan_host_key=mock.AsyncMock(return_value=found))
        make_service = mock.Mock(return_value=service)
        with self.extension(QuakSsh=make_service):
            scan = self.scan_made_by_the_app()
        self.which.assert_called_once_with("ssh-keyscan")
        make_service.assert_called_once_with()
        self.assertEqual(await scan("10.5.5.9", 2222), HostKey("10.5.5.9", 2222, "ssh-ed25519", DART_FINGERPRINT))
        service.scan_host_key.assert_awaited_once_with("10.5.5.9", 2222)

    async def test_with_ssh_keyscan_the_dart_client_stays_out_of_it(self):
        """A desktop build: the system ssh needs the known_hosts line that only ssh-keyscan's scan brings."""
        self.which.return_value = "/usr/bin/ssh-keyscan"
        make_service = mock.Mock()
        with self.extension(QuakSsh=make_service):
            self.assertIsNone(self.scan_made_by_the_app())
        self.which.assert_called_once_with("ssh-keyscan")
        make_service.assert_not_called()

    async def test_without_the_extension_there_is_none(self):
        with mock.patch.dict(sys.modules, {"quak_native": None}):  # makes the import raise ImportError
            self.assertIsNone(self.scan_made_by_the_app())

    async def test_an_older_extension_without_the_service_has_none_either(self):
        with self.extension():
            self.assertIsNone(self.scan_made_by_the_app())

    async def test_a_failure_on_the_dart_side_fails_the_way_a_scan_does(self):
        service = mock.Mock(scan_host_key=mock.AsyncMock(side_effect=RuntimeError("Connection refused")))
        with self.extension(QuakSsh=mock.Mock(return_value=service)):
            scan = self.scan_made_by_the_app()
        with self.assertRaisesRegex(OSError, "Connection refused"):
            await scan("10.5.5.9", 22)

    async def test_its_timeout_is_left_as_it_is(self):
        service = mock.Mock(scan_host_key=mock.AsyncMock(side_effect=TimeoutError("no answer")))
        with self.extension(QuakSsh=mock.Mock(return_value=service)):
            scan = self.scan_made_by_the_app()
        with self.assertRaises(TimeoutError):
            await scan("10.5.5.9", 22)


class RealManager(TerminalCase):
    """The screen on a real ``SshManager`` with real stores and the app's own scan: only the Dart side is a fake.

    There's no ssh-keyscan, as on a phone, unless a test adds it.
    """

    USB_HOST = "172.21.145.51"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.which = self.patch(mock.patch.object(gui_app.shutil, "which", return_value=None))
        found = {"key_type": "ssh-ed25519", "fingerprint": DART_FINGERPRINT}
        self.native = mock.Mock(scan_host_key=mock.AsyncMock(return_value=found))
        sys.modules["quak_native"].QuakSsh = lambda: self.native
        self.app._ssh_manager = self.real_manager()

    def real_manager(self, **overrides) -> SshManager:
        return SshManager(
            network_context=self.app._ssh_network,
            scan_async=gui_app._native_host_key_scan(),
            banner=lambda host, port: (True, "SSH-2.0-dropbear"),
            reachable=lambda host, port: (host, port) == (self.USB_HOST, 80),
            find_nets=lambda: [transport.UsbNet("usb0", "172.21.145.50", transport.CAMERA_USB_VID, "0049")],
            **overrides,
        )

    def route(self, kind: str, auth: str = "none") -> str:
        """Save a profile for this route, and return the host it comes to."""
        host = LAN.host if kind == "host" else None
        profiles.save(SshProfile(address=ADDRESS, transport=kind, host=host, auth=auth))
        return {"host": LAN.host, "usb": self.USB_HOST, "wifi": transport.AP_HOST}[kind]

    def assert_only_the_fingerprint_is_pinned(self, host: str):
        self.assertEqual(hostkeys.stored_fingerprint(host, 22).fingerprint, DART_FINGERPRINT)
        self.assertFalse(hostkeys.known_hosts_path().exists())

    async def test_on_usb_and_host_routes_the_terminal_opens_on_the_pin_alone_and_stays_off_the_wifi(self):
        for kind in ("usb", "host"):
            with self.subTest(kind):
                host = self.route(kind)
                hostkeys.remember_fingerprint(host, 22, "ssh-ed25519", DART_FINGERPRINT)
                await self.press("ssh-terminal")
                made = self.terminals[-1].created_with
                self.assertEqual(made["host"], host)
                self.assertEqual(made["expected_fingerprint"], DART_FINGERPRINT)
                self.native.scan_host_key.assert_awaited_with(host, 22)
                await self.press("ssh-terminal-disconnect")
                self.assert_only_the_fingerprint_is_pinned(host)
        self.http.assert_not_awaited()
        self.release.assert_not_awaited()

    async def test_on_a_wifi_route_the_terminal_holds_the_wifi_until_it_ends(self):
        host = self.route("wifi")
        hostkeys.remember_fingerprint(host, 22, "ssh-ed25519", DART_FINGERPRINT)
        await self.press("ssh-terminal")
        self.assertEqual(self.terminals[0].created_with["host"], host)
        self.http.assert_awaited_once_with()
        self.release.assert_not_awaited()
        await self.press("ssh-terminal-disconnect")
        self.release.assert_awaited_once_with()

    async def test_a_key_that_changed_stops_the_terminal(self):
        host = self.route("host")
        hostkeys.remember_fingerprint(host, 22, "ssh-ed25519", "SHA256:the-old-one")
        await self.press("ssh-terminal")
        self.assertEqual(self.terminals, [])
        self.assertIn("has changed", self.app.ssh["error"])
        self.assert_closed()

    async def test_trusting_from_the_screen_needs_no_known_hosts_and_no_wifi(self):
        host = self.route("host")
        await self.press("ssh-refresh")
        self.assertIn("Not trusted", words(self.view))
        await self.press("ssh-trust")
        self.assertIn(DART_FINGERPRINT, words(self.page.dialogs[-1]))
        await self.answer("Trust")
        self.assertEqual(self.app.ssh["error"], "")
        self.assert_only_the_fingerprint_is_pinned(host)
        self.assertEqual(self.app.ssh["status"].host_key_state, "trusted")
        await self.press("ssh-terminal")
        self.assertEqual(self.terminals[0].created_with["expected_fingerprint"], DART_FINGERPRINT)
        self.http.assert_not_awaited()
        self.release.assert_not_awaited()

    async def test_trusting_over_wifi_holds_it_for_the_scan_and_lets_go(self):
        host = self.route("wifi")
        await self.app.ssh_manager.trust_host(ADDRESS, fingerprint=DART_FINGERPRINT)
        self.assert_only_the_fingerprint_is_pinned(host)
        self.http.assert_awaited_once_with()
        self.release.assert_awaited_once_with()

    async def test_a_failure_on_the_dart_side_lands_in_the_status(self):
        self.route("host")
        self.native.scan_host_key.side_effect = RuntimeError("Connection refused")
        await self.press("ssh-refresh")
        self.assertIn("Connection refused", self.app.ssh["status"].last_error)
        self.assertEqual(self.app.ssh["error"], "")

    async def test_a_password_profile_logs_in_with_what_was_typed_and_no_key(self):
        host = self.route("host", auth="password")
        hostkeys.remember_fingerprint(host, 22, "ssh-ed25519", DART_FINGERPRINT)
        find(self.view, "ssh-terminal").on_click(None)
        await self.until(lambda: self.page.dialogs)
        self.page.dialogs[-1].content.value = SECRET
        await self.answer("Connect")
        made = self.terminals[0].created_with
        self.assertEqual(made["password"], SECRET)
        self.assertIsNone(made["private_key_pem"])
        self.assertEqual(made["expected_fingerprint"], DART_FINGERPRINT)
        self.assertIsNone(self.terminals[0].password)

    async def test_on_a_desktop_ssh_keyscan_does_the_scan_and_known_hosts_gets_its_line(self):
        """A Dart scan would pin only a fingerprint, leaving the system ssh with nothing to check against."""
        self.which.return_value = "/usr/bin/ssh-keyscan"
        self.app._ssh_manager = self.real_manager(scan=scanned)  # stands in for hostkeys.scan: nothing real runs
        host = self.route("host")
        expected = scanned(host, 22).fingerprint
        await self.press("ssh-refresh")
        await self.press("ssh-trust")
        await self.answer("Trust")
        self.assertEqual(self.app.ssh["error"], "")
        self.assertEqual(hostkeys.lookup_known_hosts(host, 22).fingerprint, expected)
        await self.press("ssh-terminal")
        self.assertEqual(self.terminals[0].created_with["expected_fingerprint"], expected)
        self.native.scan_host_key.assert_not_awaited()


class Fallback(TerminalCase):
    def assert_used_system_ssh(self):
        self.manager.open_terminal.assert_awaited_once_with(ADDRESS)
        self.manager.terminal_credentials.assert_not_awaited()
        self.http.assert_not_awaited()  # the manager holds the Wi-Fi itself on this path
        self.release.assert_not_awaited()
        self.assertEqual(self.terminals, [])
        text = words(self.view)
        self.assertIn("The SSH session runs in the terminal Quak was started from.", text)
        self.assertIn("SSH session ended with exit code 0.", text)
        self.assert_closed()

    async def test_without_the_extension_it_uses_the_system_ssh(self):
        self.manager.open_terminal.return_value = 0
        with mock.patch.dict(sys.modules, {"quak_native": None}):  # makes the import raise ImportError
            await self.press("ssh-terminal")
        self.assert_used_system_ssh()

    async def test_an_older_extension_without_the_control_uses_it_too(self):
        self.manager.open_terminal.return_value = 0
        with mock.patch.dict(sys.modules, {"quak_native": types.ModuleType("quak_native")}):
            await self.press("ssh-terminal")
        self.assert_used_system_ssh()


@unittest.skipIf(ft is None, "GUI extra not installed")
class RealControl(unittest.TestCase):
    def test_it_has_every_field_the_screen_sets_and_the_methods_it_calls(self):
        """What keeps the fake honest. Loaded from its file: the extension isn't installed for these tests."""
        path = ROOT / "extensions/quak_native/src/quak_native/quak_terminal.py"
        spec = importlib.util.spec_from_file_location("quak_terminal_under_test", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
            self.assertLessEqual(FIELDS, {f.name for f in dataclasses.fields(module.QuakTerminal)})
            for method in ("connect", "disconnect"):
                self.assertTrue(callable(getattr(module.QuakTerminal, method)), method)

    def test_the_ssh_service_scans_by_host_and_port(self):
        """What keeps the fake service honest, loaded the same way."""
        path = ROOT / "extensions/quak_native/src/quak_native/quak_ssh.py"
        spec = importlib.util.spec_from_file_location("quak_ssh_under_test", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
            inspect.signature(module.QuakSsh.scan_host_key).bind(None, "10.5.5.9", 22)


class Credentials(unittest.IsolatedAsyncioTestCase):
    """``SshManager.terminal_credentials`` itself, with a real key store and pin store in a temporary directory."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (
            mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": str(Path(tmp.name) / "config")}),
            mock.patch("subprocess.run", side_effect=AssertionError("a test reached subprocess.run")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("a test reached subprocess.Popen")),
            mock.patch("socket.create_connection", side_effect=AssertionError("a test reached the network")),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.network = mock.Mock(side_effect=AssertionError("it must not join Wi-Fi"))
        self.manager = SshManager(
            network_context=self.network,
            scan=scanned,
            reachable=lambda host, port: False,
            find_nets=lambda: [],  # so a new profile's automatic route is the camera's Wi-Fi
        )
        self.host = transport.AP_HOST

    def save_profile(self, **changes) -> None:
        profiles.save(replace(profiles.for_camera(CAMERA), **changes))

    async def test_a_trusted_camera_gets_everything_and_wifi_isnt_joined(self):
        store_key("k_1")
        pin(self.host)
        got = await self.manager.terminal_credentials(CAMERA)
        self.assertEqual(got.host, self.host)
        self.assertEqual(got.ssh_port, 22)
        self.assertEqual(got.username, "root")
        self.assertEqual(got.private_key_pem, PEM)
        self.assertFalse(got.passphrase_required)
        self.assertFalse(got.password_required)
        self.assertEqual(got.expected_fingerprint, scanned(self.host, 22).fingerprint)
        self.assertEqual(got.profile.address, CAMERA)
        self.network.assert_not_called()

    async def test_the_route_and_the_login_come_from_the_profile(self):
        store_key("k_1")
        self.save_profile(transport="host", host="192.168.1.50", ssh_port=2200, user="admin")
        pin("192.168.1.50", 2200)
        got = await self.manager.terminal_credentials(CAMERA)
        self.assertEqual((got.host, got.ssh_port, got.username), ("192.168.1.50", 2200, "admin"))
        self.assertEqual(got.profile.transport, "host")

    async def test_a_key_that_isnt_trusted_yet_is_refused_before_the_private_key_is_read(self):
        store_key("k_1")
        with mock.patch.object(keys, "read_private") as read, self.assertRaisesRegex(HostKeyError, "not trusted yet"):
            await self.manager.terminal_credentials(CAMERA)
        read.assert_not_called()

    async def test_a_key_that_changed_is_refused_before_the_private_key_is_read(self):
        store_key("k_1")
        hostkeys.remember_fingerprint(self.host, 22, "ssh-ed25519", "SHA256:the-old-one")
        with mock.patch.object(keys, "read_private") as read, self.assertRaisesRegex(HostKeyError, "has changed"):
            await self.manager.terminal_credentials(CAMERA)
        read.assert_not_called()

    async def test_it_says_when_the_key_needs_a_passphrase(self):
        keys._put_key(replace(store_key("k_1"), encrypted=True), PEM)
        pin(self.host)
        self.assertTrue((await self.manager.terminal_credentials(CAMERA)).passphrase_required)

    async def test_the_profiles_key_comes_before_the_default(self):
        other = PEM.replace(PEM_BODY, "b3RoZXIta2V5")
        store_key("k_first", created="2026-01-01T00:00:00")
        chosen = keys._put_key(store_key("k_chosen", created="2026-02-01T00:00:00"), other)
        self.save_profile(key_id=chosen.id)
        pin(self.host)
        self.assertEqual((await self.manager.terminal_credentials(CAMERA)).private_key_pem, other)

    async def test_without_a_chosen_key_it_is_the_default(self):
        other = PEM.replace(PEM_BODY, "b3RoZXIta2V5")
        keys._put_key(store_key("k_later", created="2026-02-01T00:00:00"), other)
        store_key("k_first", created="2026-01-01T00:00:00")
        pin(self.host)
        self.assertEqual((await self.manager.terminal_credentials(CAMERA)).private_key_pem, PEM)

    async def test_no_key_at_all_says_how_to_get_one(self):
        pin(self.host)
        with self.assertRaisesRegex(SshKeyError, "no SSH key yet"):
            await self.manager.terminal_credentials(CAMERA)

    async def test_a_chosen_key_thats_gone_says_so(self):
        store_key("k_1")
        self.save_profile(key_id="k_gone")
        pin(self.host)
        with self.assertRaisesRegex(SshKeyError, "k_gone is not in"):
            await self.manager.terminal_credentials(CAMERA)

    async def test_a_missing_private_file_says_so(self):
        keys.key_path(store_key("k_1")).unlink()
        pin(self.host)
        with self.assertRaisesRegex(SshKeyError, "is missing"):
            await self.manager.terminal_credentials(CAMERA)

    async def test_a_private_file_that_cant_be_read_says_so_without_quoting_it(self):
        store_key("k_1")
        pin(self.host)
        with (
            mock.patch.object(keys, "read_private", side_effect=PermissionError(PEM_BODY)),
            self.assertRaises(SshKeyError) as caught,
        ):
            await self.manager.terminal_credentials(CAMERA)
        self.assertIn("could not read", str(caught.exception))
        self.assertNotIn(PEM_BODY, str(caught.exception))

    async def test_a_password_profile_gets_no_key_and_is_told_to_ask(self):
        store_key("k_1")  # there to be passed over: this profile doesn't log in with a key
        self.save_profile(auth="password")
        pin(self.host)
        with mock.patch.object(keys, "read_private") as read:
            got = await self.manager.terminal_credentials(CAMERA)
        read.assert_not_called()
        self.assertIsNone(got.private_key_pem)
        self.assertTrue(got.password_required)
        self.assertFalse(got.passphrase_required)
        self.assertEqual(got.expected_fingerprint, scanned(self.host, 22).fingerprint)
        self.network.assert_not_called()

    async def test_a_password_profile_needs_no_stored_key(self):
        self.save_profile(auth="password")
        pin(self.host)
        self.assertTrue((await self.manager.terminal_credentials(CAMERA)).password_required)

    async def test_a_profile_with_no_login_needs_no_key_and_no_password(self):
        self.save_profile(auth="none")
        pin(self.host)
        got = await self.manager.terminal_credentials(CAMERA)
        self.assertIsNone(got.private_key_pem)
        self.assertFalse(got.passphrase_required)
        self.assertFalse(got.password_required)

    async def test_the_private_key_stays_out_of_its_repr(self):
        store_key("k_1")
        pin(self.host)
        self.assertNotIn(PEM_BODY, repr(await self.manager.terminal_credentials(CAMERA)))

    def fingerprint_only_manager(self) -> SshManager:
        offered = HostKey(self.host, 22, "ssh-ed25519", DART_FINGERPRINT)
        return SshManager(
            network_context=self.network,
            scan_async=mock.AsyncMock(return_value=offered),
            reachable=lambda host, port: False,
            find_nets=lambda: [],
        )

    async def test_a_fingerprint_only_scan_needs_no_known_hosts_entry(self):
        store_key("k_1")
        hostkeys.remember_fingerprint(self.host, 22, "ssh-ed25519", DART_FINGERPRINT)
        got = await self.fingerprint_only_manager().terminal_credentials(CAMERA)
        self.assertEqual(got.expected_fingerprint, DART_FINGERPRINT)
        self.assertEqual(got.private_key_pem, PEM)
        self.assertFalse(hostkeys.known_hosts_path().exists())
        self.network.assert_not_called()

    async def test_a_fingerprint_only_scan_that_differs_is_refused_before_the_private_key_is_read(self):
        store_key("k_1")
        hostkeys.remember_fingerprint(self.host, 22, "ssh-ed25519", "SHA256:the-old-one")
        with mock.patch.object(keys, "read_private") as read, self.assertRaisesRegex(HostKeyError, "has changed"):
            await self.fingerprint_only_manager().terminal_credentials(CAMERA)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()

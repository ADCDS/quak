"""The ``quak ssh key`` command line.

No hardware, no real ``ssh``, no ``ssh-keygen``, no subprocess and no network: the ``keys`` module and
``SshManager`` are replaced with autospecced mocks, so a call that doesn't fit their real signatures
fails here. Everything else on disk runs against an isolated ``QUAK_CONFIG_DIR``. The CLI is driven
in-process through ``main()`` so exit codes and ``--json`` output can be checked.
"""

import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from quak.cli.main import build_parser, main
from quak.core.errors import SshKeyError
from quak.ssh import profiles
from quak.ssh.manager import SshManager
from quak.ssh.models import KeyInstallResult, SshKey, SshProfile

ADDRESS = "AA:BB:CC:DD:EE:FF"
HOST = "192.168.1.50"
SECRET = "correct horse battery"

KEY = SshKey(
    id="k_aaaa1111",
    label="laptop",
    type="ssh-ed25519",
    private_file="keys/k_aaaa1111.pem",
    public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAA quak@test",
    fingerprint="SHA256:aaaaaaaaaaaaaaaaaaaa",
    comment="quak@test",
    created="2026-01-01T00:00:00",
)
LOCKED = SshKey(
    id="k_bbbb2222",
    label="spare",
    type="ssh-ed25519",
    private_file="keys/k_bbbb2222.pem",
    public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBB quak@test",
    fingerprint="SHA256:bbbbbbbbbbbbbbbbbbbb",
    comment="quak@test",
    encrypted=True,
    created="2026-01-02T00:00:00",
)


class TerminalInput(io.StringIO):
    """Stdin as it looks with someone at a terminal."""

    def isatty(self):
        return True


class KeyCliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": str(Path(tmp.name) / "config")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("QUAK_CAMERA_ADDRESS", None)  # env.stop puts it back

        self.keys = self._start(mock.patch("quak.cli.main.keys", autospec=True))
        self.keys.list_keys.return_value = [KEY, LOCKED]
        self.keys.generate.return_value = KEY
        self.keys.import_key.return_value = KEY
        self.keys.get_key.return_value = KEY
        self.keys.delete_key.return_value = True

        self.base = SshProfile(address=ADDRESS, label="cam")
        self.manager = mock.create_autospec(SshManager, instance=True)
        self.manager.profile.return_value = self.base
        self.manager.install_key.return_value = KeyInstallResult(
            installed=[KEY.fingerprint], verified=True, message="installed and logs in"
        )
        self.manager.rotate_key.return_value = KeyInstallResult(
            installed=[LOCKED.fingerprint], removed=[KEY.fingerprint], verified=True, message="rotated"
        )
        self.manager.revoke_key.return_value = KeyInstallResult(
            removed=[KEY.fingerprint], verified=True, message="revoked"
        )
        self._start(mock.patch("quak.cli.main.SshManager", return_value=self.manager))

        # Anything that tries to leave this process fails the test, even where the CLI would swallow the error.
        popen = self._start(mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess in these tests")))
        connect = self._start(
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("no network in these tests"))
        )
        self.addCleanup(popen.assert_not_called)
        self.addCleanup(connect.assert_not_called)

    def _start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def run_cli(self, *argv, json_mode=True, stdin=None):
        """Run ``quak [--json] ssh ...`` in-process and return (exit_code, parsed-json, stdout, stderr)."""
        prefix = ["--json"] if json_mode else []
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch("sys.stdin", stdin if stdin is not None else io.StringIO()), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            try:
                main([*prefix, "ssh", *argv])
            except SystemExit as exc:
                code = exc.code or 0
        text = out.getvalue()
        data = json.loads(text) if json_mode and text.strip() else {}
        return code, data, text, err.getvalue()

    def assert_camera_untouched(self):
        self.manager.install_key.assert_not_awaited()
        self.manager.rotate_key.assert_not_awaited()
        self.manager.revoke_key.assert_not_awaited()


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parse = build_parser().parse_args

    def test_key_is_an_ssh_action_and_args_hold_the_subcommand_and_ids(self):
        a = self.parse(["ssh", "key", "rotate", "k_old", "k_new"])
        self.assertEqual((a.ssh_action, a.args), ("key", ["rotate", "k_old", "k_new"]))

    def test_key_options_are_accepted(self):
        a = self.parse(["ssh", "key", "generate", "--label", "L", "--comment", "C", "--passphrase-stdin", "--yes"])
        self.assertEqual((a.label, a.comment, a.passphrase_stdin, a.yes), ("L", "C", True, True))

    def test_key_option_defaults(self):
        a = self.parse(["ssh", "key", "list"])
        self.assertEqual((a.label, a.comment, a.passphrase_stdin, a.yes), ("", "", False, False))


class UsageTests(KeyCliCase):
    def test_a_missing_or_unknown_subcommand_is_a_usage_error(self):
        for argv in (["key"], ["key", "wobble"]):
            with self.subTest(argv=argv):
                code, data, _, _ = self.run_cli(*argv, "--address", ADDRESS)
                self.assertEqual(code, 1)
                self.assertIn("usage: quak ssh key", data["error"])

    def test_a_wrong_number_of_ids_is_a_usage_error_and_does_nothing(self):
        cases = (
            ["list", "extra"],
            ["generate", "extra"],
            ["import"],
            ["import", "a", "b"],
            ["show"],
            ["delete"],
            ["delete", "a", "b"],
            ["install", "a", "b"],
            ["rotate"],
            ["rotate", "only-old"],
            ["revoke"],
            ["revoke", "a", "b"],
        )
        for args in cases:
            with self.subTest(args=args):
                code, data, _, _ = self.run_cli("key", *args, "--yes", "--address", ADDRESS)
                self.assertEqual(code, 1)
                self.assertIn("usage: quak ssh key", data["error"])
        self.assertEqual(self.keys.mock_calls, [])
        self.assert_camera_untouched()


class LocalWithoutCameraTests(KeyCliCase):
    def test_the_local_subcommands_need_no_camera(self):
        cases = (["list"], ["generate"], ["import", "/keys/id"], ["show", KEY.id], ["delete", KEY.id, "--yes"])
        for args in cases:
            with self.subTest(args=args):
                code, data, _, _ = self.run_cli("key", *args)  # no --address, and no default camera
                self.assertEqual(code, 0, data)
        self.manager.profile.assert_not_called()
        self.assert_camera_untouched()


class ListTests(KeyCliCase):
    def test_list_reports_id_label_fingerprint_and_encrypted_and_nothing_else(self):
        code, data, _, _ = self.run_cli("key", "list")
        self.assertEqual(code, 0)
        self.assertEqual(
            data["keys"],
            [
                {"id": KEY.id, "label": "laptop", "fingerprint": KEY.fingerprint, "encrypted": False},
                {"id": LOCKED.id, "label": "spare", "fingerprint": LOCKED.fingerprint, "encrypted": True},
            ],
        )

    def test_list_text_has_a_line_per_key(self):
        _, _, text, _ = self.run_cli("key", "list", json_mode=False)
        first, second = text.strip().splitlines()
        for field in (KEY.id, KEY.label, KEY.fingerprint, "no passphrase"):
            self.assertIn(field, first)
        for field in (LOCKED.id, LOCKED.label, LOCKED.fingerprint, "encrypted"):
            self.assertIn(field, second)

    def test_an_empty_store_says_how_to_add_a_key(self):
        self.keys.list_keys.return_value = []
        code, data, _, _ = self.run_cli("key", "list")
        self.assertEqual((code, data["keys"]), (0, []))
        _, _, text, _ = self.run_cli("key", "list", json_mode=False)
        self.assertIn("quak ssh key generate", text)


class GenerateTests(KeyCliCase):
    def test_label_and_comment_reach_keys_generate(self):
        code, data, _, _ = self.run_cli("key", "generate", "--label", "laptop", "--comment", "me@host")
        self.assertEqual(code, 0)
        self.keys.generate.assert_called_once_with("laptop", comment="me@host", passphrase=None)
        self.assertEqual(data["id"], KEY.id)
        self.assertEqual(data["fingerprint"], KEY.fingerprint)

    def test_without_the_option_stdin_is_left_alone(self):
        stdin = io.StringIO("not for us\n")
        self.run_cli("key", "generate", stdin=stdin)
        self.keys.generate.assert_called_once_with("", comment="", passphrase=None)
        self.assertEqual(stdin.tell(), 0)

    def test_passphrase_stdin_reads_exactly_one_line(self):
        stdin = io.StringIO(f"{SECRET}\nnext line\n")
        code, _, _, _ = self.run_cli("key", "generate", "--passphrase-stdin", stdin=stdin)
        self.assertEqual(code, 0)
        self.keys.generate.assert_called_once_with("", comment="", passphrase=SECRET)
        self.assertEqual(stdin.read(), "next line\n")

    def test_only_the_line_ending_is_stripped(self):
        for line, expected in (("  spaced  \n", "  spaced  "), ("windows\r\n", "windows"), ("no newline", "no newline")):
            with self.subTest(line=line):
                self.keys.generate.reset_mock()
                self.run_cli("key", "generate", "--passphrase-stdin", stdin=io.StringIO(line))
                self.keys.generate.assert_called_once_with("", comment="", passphrase=expected)

    def test_an_empty_line_never_generates_an_unprotected_key(self):
        for stdin in ("", "\n"):
            with self.subTest(stdin=stdin):
                code, data, _, _ = self.run_cli("key", "generate", "--passphrase-stdin", stdin=io.StringIO(stdin))
                self.assertEqual(code, 10)  # SshKeyError
                self.assertIn("--passphrase-stdin", data["error"])
        self.keys.generate.assert_not_called()

    def test_the_passphrase_is_never_printed(self):
        for json_mode in (True, False):
            with self.subTest(json_mode=json_mode):
                stdin = io.StringIO(f"{SECRET}\n")
                _, _, text, err = self.run_cli("key", "generate", "--passphrase-stdin", json_mode=json_mode, stdin=stdin)
                self.assertNotIn(SECRET, text + err)

    def test_the_passphrase_is_not_printed_on_failure(self):
        self.keys.generate.side_effect = SshKeyError("ssh-keygen could not create the key")
        code, _, text, err = self.run_cli("key", "generate", "--passphrase-stdin", stdin=io.StringIO(f"{SECRET}\n"))
        self.assertEqual(code, 10)
        self.assertIn("could not create", text)
        self.assertNotIn(SECRET, text + err)

    def test_the_private_file_is_not_reported(self):
        _, data, _, _ = self.run_cli("key", "generate")
        self.assertNotIn("private_file", data)


class ImportTests(KeyCliCase):
    def test_the_path_and_label_reach_keys_import_key(self):
        code, data, _, _ = self.run_cli("key", "import", "/keys/id_ed25519", "--label", "old laptop")
        self.assertEqual(code, 0)
        self.keys.import_key.assert_called_once_with(Path("/keys/id_ed25519"), "old laptop", passphrase=None)
        self.assertEqual(data["id"], KEY.id)

    def test_passphrase_stdin_reaches_keys_import_key(self):
        stdin = io.StringIO(f"{SECRET}\n")
        _, _, text, err = self.run_cli("key", "import", "/keys/id", "--passphrase-stdin", stdin=stdin)
        self.keys.import_key.assert_called_once_with(Path("/keys/id"), "", passphrase=SECRET)
        self.assertNotIn(SECRET, text + err)

    def test_a_key_that_wont_import_is_a_key_error(self):
        self.keys.import_key.side_effect = SshKeyError("/keys/id is not an OpenSSH/PEM private key")
        code, data, _, _ = self.run_cli("key", "import", "/keys/id")
        self.assertEqual(code, 10)
        self.assertIn("not an OpenSSH/PEM private key", data["error"])


class ShowTests(KeyCliCase):
    def test_show_reports_the_public_key_and_fingerprint(self):
        code, data, _, _ = self.run_cli("key", "show", KEY.id)
        self.assertEqual(code, 0)
        self.keys.get_key.assert_called_once_with(KEY.id)
        self.assertEqual(data["public_key"], KEY.public_key)
        self.assertEqual(data["fingerprint"], KEY.fingerprint)
        self.assertNotIn("private_file", data)

    def test_show_text_prints_both(self):
        _, _, text, _ = self.run_cli("key", "show", KEY.id, json_mode=False)
        self.assertEqual(text.strip().splitlines(), [KEY.public_key, KEY.fingerprint])

    def test_an_unknown_key_is_an_actionable_key_error(self):
        self.keys.get_key.return_value = None
        code, data, _, _ = self.run_cli("key", "show", "k_nope")
        self.assertEqual(code, 10)
        self.assertIn("k_nope", data["error"])
        self.assertIn("quak ssh key list", data["error"])


class DeleteTests(KeyCliCase):
    def test_delete_needs_yes(self):
        code, data, _, _ = self.run_cli("key", "delete", KEY.id)
        self.assertEqual(code, 8)
        self.assertIn("--yes", data["error"])
        self.keys.delete_key.assert_not_called()

    def test_delete_needs_yes_even_at_a_terminal(self):
        code, _, _, _ = self.run_cli("key", "delete", KEY.id, json_mode=False, stdin=TerminalInput("yes\n"))
        self.assertEqual(code, 8)
        self.keys.delete_key.assert_not_called()

    def test_delete_with_yes_calls_keys_delete_key(self):
        code, data, _, _ = self.run_cli("key", "delete", KEY.id, "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(data["deleted"])
        self.keys.delete_key.assert_called_once_with(KEY.id)

    def test_deleting_an_unknown_key_says_so(self):
        self.keys.delete_key.return_value = False
        code, data, _, _ = self.run_cli("key", "delete", "k_nope", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(data["deleted"])
        _, _, text, _ = self.run_cli("key", "delete", "k_nope", "--yes", json_mode=False)
        self.assertIn("no SSH key k_nope", text)


class InstallTests(KeyCliCase):
    def test_install_passes_the_key_id_and_needs_no_yes(self):
        code, data, _, _ = self.run_cli("key", "install", KEY.id, "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.manager.install_key.assert_awaited_once_with(ADDRESS, KEY.id, profile=self.base)
        self.assertEqual(data["installed"], [KEY.fingerprint])
        self.assertTrue(data["verified"])
        self.assertEqual(data["message"], "installed and logs in")

    def test_without_an_id_the_manager_picks_the_key(self):
        self.run_cli("key", "install", "--address", ADDRESS)
        self.manager.install_key.assert_awaited_once_with(ADDRESS, None, profile=self.base)

    def test_text_output_is_the_managers_message(self):
        _, _, text, _ = self.run_cli("key", "install", KEY.id, "--address", ADDRESS, json_mode=False)
        self.assertEqual(text.strip(), "installed and logs in")

    def test_no_camera_is_an_actionable_error(self):
        code, data, _, _ = self.run_cli("key", "install", KEY.id)
        self.assertEqual(code, 8)
        self.assertIn("--address", data["error"])
        self.assert_camera_untouched()

    def test_a_refusal_from_the_manager_keeps_its_exit_code(self):
        self.manager.install_key.side_effect = SshKeyError("Quak has no SSH key yet")
        code, data, _, _ = self.run_cli("key", "install", "--address", ADDRESS)
        self.assertEqual(code, 10)
        self.assertIn("no SSH key yet", data["error"])


class RotateTests(KeyCliCase):
    ARGS = ("key", "rotate", KEY.id, LOCKED.id, "--address", ADDRESS)

    def test_rotate_with_yes_calls_the_manager(self):
        code, data, _, _ = self.run_cli(*self.ARGS, "--yes")
        self.assertEqual(code, 0)
        self.manager.rotate_key.assert_awaited_once_with(ADDRESS, KEY.id, LOCKED.id, profile=self.base)
        self.assertEqual(data["installed"], [LOCKED.fingerprint])
        self.assertEqual(data["removed"], [KEY.fingerprint])

    def test_non_interactive_rotate_needs_yes(self):
        code, data, _, _ = self.run_cli(*self.ARGS)
        self.assertEqual(code, 8)
        self.assertIn("--yes", data["error"])
        self.assert_camera_untouched()

    def test_a_piped_answer_does_not_count_as_confirmation(self):
        stdin = io.StringIO("yes\n")
        code, _, _, _ = self.run_cli(*self.ARGS, json_mode=False, stdin=stdin)
        self.assertEqual(code, 8)
        self.assertEqual(stdin.tell(), 0)
        self.assert_camera_untouched()

    def test_json_mode_never_prompts_even_at_a_terminal(self):
        stdin = TerminalInput("yes\n")
        code, _, _, _ = self.run_cli(*self.ARGS, stdin=stdin)
        self.assertEqual(code, 8)
        self.assertEqual(stdin.tell(), 0)
        self.assert_camera_untouched()

    def test_at_a_terminal_it_asks_and_goes_ahead_on_yes(self):
        for answer in ("y\n", "YES\n"):
            with self.subTest(answer=answer):
                self.manager.rotate_key.reset_mock()
                code, _, _, err = self.run_cli(*self.ARGS, json_mode=False, stdin=TerminalInput(answer))
                self.assertEqual(code, 0)
                self.assertIn("Go ahead?", err)
                self.manager.rotate_key.assert_awaited_once()

    def test_at_a_terminal_anything_else_cancels(self):
        for answer in ("n\n", "\n", ""):  # the last one is Ctrl-D
            with self.subTest(answer=answer):
                code, _, _, err = self.run_cli(*self.ARGS, json_mode=False, stdin=TerminalInput(answer))
                self.assertEqual(code, 8)
                self.assertIn("cancelled", err)
        self.assert_camera_untouched()


class RevokeTests(KeyCliCase):
    ARGS = ("key", "revoke", KEY.id, "--address", ADDRESS)

    def test_revoke_with_yes_calls_the_manager(self):
        code, data, _, _ = self.run_cli(*self.ARGS, "--yes")
        self.assertEqual(code, 0)
        self.manager.revoke_key.assert_awaited_once_with(ADDRESS, KEY.id, profile=self.base)
        self.assertEqual(data["removed"], [KEY.fingerprint])

    def test_non_interactive_revoke_needs_yes(self):
        code, data, _, _ = self.run_cli(*self.ARGS)
        self.assertEqual(code, 8)
        self.assertIn("--yes", data["error"])
        self.assert_camera_untouched()

    def test_at_a_terminal_it_asks_first(self):
        code, _, _, err = self.run_cli(*self.ARGS, json_mode=False, stdin=TerminalInput("y\n"))
        self.assertEqual(code, 0)
        self.assertIn(KEY.id, err)
        self.manager.revoke_key.assert_awaited_once()

    def test_declining_at_a_terminal_leaves_the_camera_alone(self):
        code, _, _, _ = self.run_cli(*self.ARGS, json_mode=False, stdin=TerminalInput("n\n"))
        self.assertEqual(code, 8)
        self.assert_camera_untouched()

    def test_the_managers_last_key_refusal_reaches_the_user(self):
        self.manager.revoke_key.side_effect = SshKeyError("that is the last key on the camera")
        code, data, _, _ = self.run_cli(*self.ARGS, "--yes")
        self.assertEqual(code, 10)
        self.assertIn("last key", data["error"])


class OverrideTests(KeyCliCase):
    OVERRIDES = ("--address", ADDRESS, "--host", HOST, "--transport", "host", "--yes")

    def setUp(self):
        super().setUp()
        self.expected = replace(self.base, host=HOST, transport="host")

    def test_install_gets_the_per_run_overrides(self):
        self.run_cli("key", "install", KEY.id, *self.OVERRIDES)
        self.manager.install_key.assert_awaited_once_with(ADDRESS, KEY.id, profile=self.expected)

    def test_rotate_gets_the_per_run_overrides(self):
        self.run_cli("key", "rotate", KEY.id, LOCKED.id, *self.OVERRIDES)
        self.manager.rotate_key.assert_awaited_once_with(ADDRESS, KEY.id, LOCKED.id, profile=self.expected)

    def test_revoke_gets_the_per_run_overrides(self):
        self.run_cli("key", "revoke", KEY.id, *self.OVERRIDES)
        self.manager.revoke_key.assert_awaited_once_with(ADDRESS, KEY.id, profile=self.expected)

    def test_overrides_are_not_persisted(self):
        self.run_cli("key", "install", KEY.id, *self.OVERRIDES)
        self.assertIsNone(profiles.load(ADDRESS))


if __name__ == "__main__":
    unittest.main()

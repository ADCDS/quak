"""The ``quak ssh`` command line.

No hardware, no real ``ssh`` and no network: ``SshManager`` is replaced with a fake that records
how the CLI calls it, and everything on disk runs against an isolated ``QUAK_CONFIG_DIR``. The CLI
is driven in-process through ``main()`` so exit codes and ``--json`` output can be checked.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from quak.cli.main import build_parser, main
from quak.ssh import profiles, system_ssh
from quak.ssh.models import ArmResult, Endpoint, ExecResult, HostKey, SshProfile, SshStatus, TestResult

ADDRESS = "AA:BB:CC:DD:EE:FF"
HOST = "192.168.1.50"
BANNER = "SSH-2.0-dropbear_2022.83"
FINGERPRINT = "SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFX"
PASSWORD = "correct horse battery"


class FakeManager:
    """Stands in for ``SshManager``: records each call (and the ``profile=`` override it was given)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.passwords: list[str | None] = []  # what each test/exec was given, kept apart from what gets compared
        self.base = SshProfile(address=ADDRESS, label="cam", transport="auto")
        self.status_result = SshStatus(
            profile=self.base,
            endpoint=Endpoint(HOST, 22, 80, "host"),
            reachable=True,
            ssh_up=True,
            banner=BANNER,
            host_key=HostKey(HOST, 22, "ssh-ed25519", FINGERPRINT),
            host_key_state="trusted",
        )
        self.arm_result = ArmResult(http_status=200, accepted=True, ssh_up=True, banner=BANNER, message="up")
        self.test_result = TestResult(ok=True, banner=BANNER, uid="uid=0(root)", uname="Linux 4.9 armv7l")
        self.exec_result = ExecResult(exit_code=0, stdout=b"hello\n", stderr=b"")
        self.terminal_exit = 0
        self.trust_result = HostKey(HOST, 22, "ssh-ed25519", FINGERPRINT)
        self.forget_result = True

    def profile(self, address):
        self.calls.append(("profile", address, None))
        return self.base

    def _record(self, name, address, profile):
        self.calls.append((name, address, profile))

    async def status(self, address, *, profile=None):
        self._record("status", address, profile)
        return self.status_result

    async def arm(self, address, *, profile=None):
        self._record("arm", address, profile)
        return self.arm_result

    async def test(self, address, *, profile=None, password=None):
        self._record("test", address, profile)
        self.passwords.append(password)
        return self.test_result

    async def exec(self, address, command, *, profile=None, password=None):
        self.calls.append(("exec", address, profile, command))
        self.passwords.append(password)
        return self.exec_result

    async def open_terminal(self, address, *, profile=None):
        self._record("open_terminal", address, profile)
        return self.terminal_exit

    async def trust_host(self, address, *, fingerprint=None, profile=None):
        self._record("trust_host", address, profile)
        return self.trust_result

    async def forget_host(self, address, *, profile=None):
        self._record("forget_host", address, profile)
        return self.forget_result

    def last(self, name):
        return next(c for c in reversed(self.calls) if c[0] == name)


class SshCliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_dir = Path(tmp.name) / "config"
        patch = mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": str(self.config_dir)})
        patch.start()
        self.addCleanup(patch.stop)
        self.manager = FakeManager()

    def run_cli(self, *argv, json_mode=True):
        """Run ``quak [--json] ssh ...`` in-process and return (exit_code, parsed-json, stdout, stderr)."""
        prefix = ["--json"] if json_mode else []
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch("quak.cli.main.SshManager", return_value=self.manager), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            try:
                main([*prefix, "ssh", *argv])
            except SystemExit as exc:
                code = exc.code or 0
        text = out.getvalue()
        data = json.loads(text) if json_mode and text.strip() else {}
        return code, data, text, err.getvalue()


class ParserTests(SshCliCase):
    def test_every_action_parses(self):
        parse = build_parser().parse_args
        for action in ("status", "arm", "test", "exec", "open", "host", "profile"):
            self.assertEqual(parse(["ssh", action]).ssh_action, action)

    def test_address_and_overrides_are_accepted(self):
        a = build_parser().parse_args(["ssh", "status", "--address", ADDRESS, "--host", HOST, "--transport", "host"])
        self.assertEqual((a.address, a.host, a.transport), (ADDRESS, HOST, "host"))

    def test_exec_keeps_the_remote_command_after_the_separator(self):
        a = build_parser().parse_args(["ssh", "exec", "--", "id", "-u"])
        self.assertEqual((a.ssh_action, a.args), ("exec", ["id", "-u"]))

    def test_dashed_remote_options_survive_the_separator(self):
        a = build_parser().parse_args(["ssh", "exec", "--", "ls", "--color", "/tmp"])
        self.assertEqual(a.args, ["ls", "--color", "/tmp"])

    def test_password_stdin_is_off_unless_asked_for(self):
        parse = build_parser().parse_args
        self.assertFalse(parse(["ssh", "test"]).password_stdin)
        self.assertTrue(parse(["ssh", "test", "--password-stdin"]).password_stdin)
        self.assertFalse(parse(["ssh", "test"]).blank_password)
        self.assertTrue(parse(["ssh", "test", "--blank-password"]).blank_password)


class StatusTests(SshCliCase):
    def test_status_reports_endpoint_banner_and_host_key_state(self):
        code, data, _, _ = self.run_cli("status", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertEqual(data["endpoint"], f"{HOST}:22")
        self.assertEqual(data["banner"], BANNER)
        self.assertTrue(data["ssh_up"])
        self.assertEqual(data["host_key"], FINGERPRINT)
        self.assertEqual(data["host_key_state"], "trusted")
        self.assertEqual(self.manager.last("status")[1], ADDRESS)

    def test_status_text_names_the_state(self):
        _, _, text, _ = self.run_cli("status", "--address", ADDRESS, json_mode=False)
        self.assertIn(HOST, text)
        self.assertIn("host key trusted", text)


class ArmTests(SshCliCase):
    def test_arm_reports_the_result_fields(self):
        code, data, _, _ = self.run_cli("arm", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertEqual(data["http_status"], 200)
        self.assertTrue(data["accepted"])
        self.assertTrue(data["ssh_up"])
        self.assertEqual(data["banner"], BANNER)

    def test_a_refused_arm_raises_armfailed(self):
        self.manager.arm_result = ArmResult(http_status=500, accepted=False, ssh_up=False, message="gpNet busy")
        code, data, _, err = self.run_cli("arm", "--address", ADDRESS)
        self.assertEqual(code, 11)  # ArmFailed
        self.assertFalse(data["accepted"])
        self.assertIn("gpNet busy", data["error"])

    def test_an_accepted_arm_that_never_opened_ssh_does_not_raise(self):
        self.manager.arm_result = ArmResult(http_status=200, accepted=True, ssh_up=False, message="no banner")
        code, data, _, _ = self.run_cli("arm", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertFalse(data["ssh_up"])


class TestActionTests(SshCliCase):
    def test_test_prints_uid_uname_and_banner(self):
        code, data, _, _ = self.run_cli("test", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertEqual(data["uid"], "uid=0(root)")
        self.assertEqual(data["uname"], "Linux 4.9 armv7l")
        self.assertEqual(data["banner"], BANNER)

    def test_a_failed_login_is_an_ssh_error(self):
        self.manager.test_result = TestResult(ok=False, error="Permission denied (publickey)")
        code, data, _, _ = self.run_cli("test", "--address", ADDRESS)
        self.assertEqual(code, 8)  # SshError
        self.assertIn("Permission denied", data["error"])


class ExecTests(SshCliCase):
    def test_exec_joins_the_args_into_one_command(self):
        self.run_cli("exec", "--address", ADDRESS, "--", "id", "-u")
        self.assertEqual(self.manager.last("exec")[3], "id -u")

    def test_exec_quotes_arguments_that_contain_spaces(self):
        self.run_cli("exec", "--address", ADDRESS, "--", "echo", "hello world")
        self.assertEqual(self.manager.last("exec")[3], "echo 'hello world'")

    def test_exec_propagates_the_remote_exit_code(self):
        self.manager.exec_result = ExecResult(exit_code=3, stdout=b"", stderr=b"boom\n")
        code, data, _, _ = self.run_cli("exec", "--address", ADDRESS, "--", "false")
        self.assertEqual(code, 3)
        self.assertEqual(data["exit_code"], 3)

    def test_exec_prints_stdout_in_plain_mode(self):
        self.manager.exec_result = ExecResult(exit_code=0, stdout=b"hello\n", stderr=b"")
        code, _, text, _ = self.run_cli("exec", "--address", ADDRESS, "--", "echo", "hi", json_mode=False)
        self.assertEqual(code, 0)
        self.assertIn("hello", text)

    def test_an_empty_command_is_an_error(self):
        code, data, _, _ = self.run_cli("exec", "--address", ADDRESS)
        self.assertEqual(code, 8)
        self.assertIn("remote command", data["error"])


class OpenTests(SshCliCase):
    def test_open_runs_the_terminal_and_propagates_its_exit_code(self):
        self.manager.terminal_exit = 130
        code, _, _, _ = self.run_cli("open", "--address", ADDRESS)
        self.assertEqual(code, 130)
        self.assertEqual(self.manager.last("open_terminal")[1], ADDRESS)


class HostTests(SshCliCase):
    def test_host_show_reports_the_fingerprint_and_state(self):
        code, data, _, _ = self.run_cli("host", "show", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertEqual(data["host_key"], FINGERPRINT)
        self.assertEqual(data["host_key_state"], "trusted")

    def test_host_trust_pins_the_key(self):
        code, data, _, _ = self.run_cli("host", "trust", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertTrue(data["trusted"])
        self.assertEqual(data["host_key"], FINGERPRINT)
        self.assertEqual(self.manager.last("trust_host")[1], ADDRESS)

    def test_host_forget_needs_yes(self):
        code, data, _, _ = self.run_cli("host", "forget", "--address", ADDRESS)
        self.assertEqual(code, 8)
        self.assertIn("--yes", data["error"])
        self.assertFalse(any(c[0] == "forget_host" for c in self.manager.calls))

    def test_host_forget_with_yes_calls_the_manager(self):
        code, data, _, _ = self.run_cli("host", "forget", "--yes", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertTrue(data["forgotten"])
        self.assertEqual(self.manager.last("forget_host")[1], ADDRESS)

    def test_an_unknown_host_subcommand_is_an_error(self):
        code, data, _, _ = self.run_cli("host", "wobble", "--address", ADDRESS)
        self.assertEqual(code, 1)
        self.assertIn("host show|trust|forget", data["error"])


class ProfileTests(SshCliCase):
    def test_profile_show_prints_the_saved_fields(self):
        profiles.save(SshProfile(address=ADDRESS, transport="host", host=HOST))
        self.manager.base = profiles.load(ADDRESS)
        code, data, _, _ = self.run_cli("profile", "show", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertEqual(data["profile"]["transport"], "host")
        self.assertEqual(data["profile"]["host"], HOST)

    def test_profile_forget_needs_yes(self):
        profiles.save(SshProfile(address=ADDRESS, transport="host", host=HOST))
        code, data, _, _ = self.run_cli("profile", "forget", "--address", ADDRESS)
        self.assertEqual(code, 8)
        self.assertIn("--yes", data["error"])
        self.assertIsNotNone(profiles.load(ADDRESS))  # still there

    def test_profile_forget_with_yes_deletes_it(self):
        profiles.save(SshProfile(address=ADDRESS, transport="host", host=HOST))
        code, data, _, _ = self.run_cli("profile", "forget", "--yes", "--address", ADDRESS)
        self.assertEqual(code, 0)
        self.assertTrue(data["forgotten"])
        self.assertIsNone(profiles.load(ADDRESS))

    def test_profile_show_includes_auth(self):
        self.manager.base = SshProfile(address=ADDRESS, auth="password")
        _, data, _, _ = self.run_cli("profile", "show", "--address", ADDRESS)
        self.assertEqual(data["profile"]["auth"], "password")
        _, _, text, _ = self.run_cli("profile", "show", "--address", ADDRESS, json_mode=False)
        self.assertIn("auth: password", text)

    def test_an_unknown_profile_subcommand_shows_the_usage(self):
        code, data, _, _ = self.run_cli("profile", "wobble", "--address", ADDRESS)
        self.assertEqual(code, 1)
        self.assertIn("profile show|forget|set --auth", data["error"])


class ProfileSetTests(SshCliCase):
    def test_it_saves_the_auth_and_user(self):
        code, data, _, _ = self.run_cli("profile", "set", "--auth", "password", "--user", "admin", "--address", ADDRESS)
        self.assertEqual(code, 0)
        saved = profiles.load(ADDRESS)
        self.assertEqual((saved.auth, saved.user), ("password", "admin"))
        self.assertEqual((data["profile"]["auth"], data["profile"]["user"]), ("password", "admin"))

    def test_every_auth_mode_is_accepted(self):
        for auth in profiles.AUTHS:
            with self.subTest(auth):
                code, _, _, _ = self.run_cli("profile", "set", "--auth", auth, "--address", ADDRESS)
                self.assertEqual(code, 0)
                self.assertEqual(profiles.load(ADDRESS).auth, auth)

    def test_the_text_output_says_what_was_saved(self):
        _, _, text, _ = self.run_cli("profile", "set", "--auth", "none", "--address", ADDRESS, json_mode=False)
        self.assertIn("auth none", text)
        self.assertIn(ADDRESS, text)

    def test_it_changes_nothing_else(self):
        saved = SshProfile(address=ADDRESS, transport="host", host=HOST, user="admin", key_id="k_main", ssh_port=2222)
        profiles.save(saved)
        self.manager.base = profiles.load(ADDRESS)
        self.run_cli("profile", "set", "--auth", "password", "--address", ADDRESS)
        self.assertEqual(profiles.load(ADDRESS), replace(saved, auth="password"))

    def test_the_key_is_still_there_to_switch_back_to(self):
        profiles.save(SshProfile(address=ADDRESS, key_id="k_main"))
        for auth in ("password", "key"):
            self.manager.base = profiles.load(ADDRESS)
            self.run_cli("profile", "set", "--auth", auth, "--address", ADDRESS)
            self.assertEqual((profiles.load(ADDRESS).auth, profiles.load(ADDRESS).key_id), (auth, "k_main"))

    def test_this_runs_overrides_are_not_saved(self):
        self.run_cli(
            "profile", "set", "--auth", "password", "--address", ADDRESS, "--host", "10.0.0.9", "--transport", "host"
        )
        saved = profiles.load(ADDRESS)
        self.assertEqual((saved.auth, saved.host, saved.transport), ("password", None, "auto"))

    def test_no_password_field_is_saved_or_shown(self):
        _, data, _, _ = self.run_cli("profile", "set", "--auth", "password", "--address", ADDRESS)
        self.assertNotIn("password", data["profile"])
        self.assertNotIn("password", profiles.to_dict(profiles.load(ADDRESS)))

    def test_it_needs_auth(self):
        code, data, _, _ = self.run_cli("profile", "set", "--user", "admin", "--address", ADDRESS)
        self.assertEqual(code, 1)
        self.assertIn("--auth", data["error"])
        self.assertIsNone(profiles.load(ADDRESS))

    def test_an_unknown_auth_is_refused(self):
        code, _, _, err = self.run_cli("profile", "set", "--auth", "wobble", "--address", ADDRESS)
        self.assertEqual(code, 2)  # argparse
        self.assertIn("invalid choice", err)
        self.assertIsNone(profiles.load(ADDRESS))

    def test_extra_arguments_are_refused(self):
        code, _, _, _ = self.run_cli("profile", "set", "extra", "--auth", "key", "--address", ADDRESS)
        self.assertEqual(code, 1)
        self.assertIsNone(profiles.load(ADDRESS))

    def test_a_user_that_ssh_would_misread_is_refused(self):
        for user in ("-oProxyCommand=x", "", "a b", "a\nb"):
            with self.subTest(user):
                code, data, _, _ = self.run_cli("profile", "set", "--auth", "key", f"--user={user}", "--address", ADDRESS)
                self.assertEqual(code, 1)
                self.assertIn("login name", data["error"])
                self.assertIsNone(profiles.load(ADDRESS))


class PasswordStdinTests(SshCliCase):
    def setUp(self):
        super().setUp()
        self.manager.base = SshProfile(address=ADDRESS, transport="host", host=HOST, auth="password")

    def run_piped(self, text, action, *argv, **kwargs):
        """``run_cli`` with ``text`` waiting on stdin: its results, then whatever was left unread."""
        stdin = io.StringIO(text)
        with mock.patch("sys.stdin", stdin):
            result = self.run_cli(action, "--address", ADDRESS, *argv, **kwargs)  # before any `--` in argv
        return result, stdin.read()

    def test_test_reads_one_line_and_passes_it_on(self):
        (code, data, _, _), left = self.run_piped(f"{PASSWORD}\nnext line\n", "test", "--password-stdin")
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(self.manager.passwords, [PASSWORD])
        self.assertEqual(left, "next line\n")

    def test_exec_reads_one_line_and_passes_it_on(self):
        (code, _, _, _), left = self.run_piped(f"{PASSWORD}\nnext line\n", "exec", "--password-stdin", "--", "id", "-u")
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.passwords, [PASSWORD])
        self.assertEqual(self.manager.last("exec")[3], "id -u")
        self.assertEqual(left, "next line\n")

    def test_only_the_line_ending_is_taken_off(self):
        for ending in ("\n", "\r\n", ""):
            with self.subTest(ending=ending):
                self.manager.passwords.clear()
                self.run_piped(f" {PASSWORD} {ending}", "test", "--password-stdin")
                self.assertEqual(self.manager.passwords, [f" {PASSWORD} "])

    def test_it_is_never_shown(self):
        failed = TestResult(ok=False, error="Permission denied (password)")
        for json_mode in (True, False):
            for argv in (("test", "--password-stdin"), ("exec", "--password-stdin", "--", "id")):
                for test_result in (self.manager.test_result, failed):
                    with self.subTest(json_mode=json_mode, argv=argv, ok=test_result.ok):
                        self.manager.test_result = test_result
                        self.manager.passwords.clear()
                        (_, _, text, err), _ = self.run_piped(f"{PASSWORD}\n", *argv, json_mode=json_mode)
                        self.assertEqual(self.manager.passwords, [PASSWORD])
                        self.assertNotIn(PASSWORD, text)
                        self.assertNotIn(PASSWORD, err)

    def test_it_is_never_saved(self):
        profiles.save(self.manager.base)
        self.run_piped(f"{PASSWORD}\n", "test", "--password-stdin")
        self.run_piped(f"{PASSWORD}\n", "exec", "--password-stdin", "--", "id")
        self.assertEqual(self.manager.passwords, [PASSWORD, PASSWORD])
        written = [path for path in self.config_dir.rglob("*") if path.is_file()]
        self.assertTrue(written)
        for path in written:
            self.assertNotIn(PASSWORD.encode(), path.read_bytes(), path)

    def test_an_empty_line_is_refused_before_anything_connects(self):
        for text in ("", "\n", "\r\n"):
            for argv in (("test", "--password-stdin"), ("exec", "--password-stdin", "--", "id")):
                with self.subTest(text=text, argv=argv):
                    (code, data, _, _), _ = self.run_piped(text, *argv)
                    self.assertEqual(code, 8)  # SshError
                    self.assertIn("--password-stdin", data["error"])
        self.assertEqual(self.manager.passwords, [])

    def test_blank_password_passes_an_empty_password(self):
        self.manager.base = replace(self.manager.base, auth="password")
        code, data, _, _ = self.run_cli("test", "--address", ADDRESS, "--blank-password")
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(self.manager.passwords, [""])

    def test_blank_password_and_stdin_together_are_refused(self):
        self.manager.base = replace(self.manager.base, auth="password")
        code, data, _, _ = self.run_cli("test", "--address", ADDRESS, "--blank-password", "--password-stdin")
        self.assertEqual(code, 8)
        self.assertIn("not both", data["error"])
        self.assertEqual(self.manager.passwords, [])

    def test_blank_password_is_ignored_for_a_key_profile(self):
        self.manager.base = replace(self.manager.base, auth="key")
        code, data, _, _ = self.run_cli("test", "--address", ADDRESS, "--blank-password")
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.passwords, [None])

    def test_without_the_option_stdin_is_left_alone(self):
        for argv in (("test",), ("exec", "--", "id")):
            with self.subTest(argv=argv):
                self.manager.passwords.clear()
                _, left = self.run_piped(f"{PASSWORD}\n", *argv)
                self.assertEqual(left, f"{PASSWORD}\n")
                self.assertEqual(self.manager.passwords, [None])

    def test_a_key_or_no_auth_profile_leaves_stdin_alone_and_gets_no_password(self):
        for auth in ("key", "none"):
            self.manager.base = SshProfile(address=ADDRESS, transport="host", host=HOST, auth=auth)
            for argv in (("test", "--password-stdin"), ("exec", "--password-stdin", "--", "id")):
                with self.subTest(auth=auth, argv=argv):
                    self.manager.passwords.clear()
                    _, left = self.run_piped(f"{PASSWORD}\n", *argv)
                    self.assertEqual(left, f"{PASSWORD}\n")
                    self.assertEqual(self.manager.passwords, [None])

    def test_an_empty_remote_command_is_refused_before_stdin_is_read(self):
        (code, data, _, _), left = self.run_piped(f"{PASSWORD}\n", "exec", "--password-stdin")
        self.assertEqual(code, 8)
        self.assertIn("remote command", data["error"])
        self.assertEqual(left, f"{PASSWORD}\n")


class OverrideTests(SshCliCase):
    def test_host_and_transport_reach_the_manager_as_a_profile_override(self):
        self.run_cli("status", "--address", ADDRESS, "--host", "10.0.0.9", "--transport", "host")
        passed = self.manager.last("status")[2]
        self.assertEqual(passed.host, "10.0.0.9")
        self.assertEqual(passed.transport, "host")

    def test_an_override_is_not_persisted(self):
        self.run_cli("status", "--address", ADDRESS, "--host", "10.0.0.9", "--transport", "host")
        self.assertIsNone(profiles.load(ADDRESS))  # nothing written to the store

    def test_without_overrides_the_saved_profile_is_passed_through_unchanged(self):
        self.run_cli("status", "--address", ADDRESS)
        passed = self.manager.last("status")[2]
        self.assertIs(passed, self.manager.base)


class MissingAddressTests(SshCliCase):
    def test_no_address_and_no_default_is_an_actionable_error(self):
        code, data, _, _ = self.run_cli("status")
        self.assertEqual(code, 8)
        self.assertIn("--address", data["error"])


class SshArgvTests(SshCliCase):
    def test_built_ssh_argv_pins_the_host_key(self):
        """The CLI hands off to ``system_ssh``; its argv must keep strict host-key checking on."""
        profile = SshProfile(address=ADDRESS, transport="host", host=HOST)
        endpoint = Endpoint(HOST, 2222, 80, "host")
        argv = system_ssh._base_args(profile, endpoint, None)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertTrue(any(arg.startswith("UserKnownHostsFile=") for arg in argv))
        self.assertIn("2222", argv)  # -p <port>


if __name__ == "__main__":
    unittest.main()

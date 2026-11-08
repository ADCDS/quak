"""``SshManager.install_key``, ``rotate_key`` and ``revoke_key`` against a fake camera.

The fake is shaped like ``system_ssh`` and keeps its files in memory. It runs the shell steps the
manager sends and logs in the way the camera does: every call is a new connection, admitted only if
the runtime file lists the key at that moment. A step that ends its own login fails here as it would
on the camera. ``subprocess.run`` and ``socket.create_connection`` fail the test if anything reaches them.
"""

import inspect
import os
import shlex
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from quak.core.errors import HostKeyError, NetworkError, SshError, SshKeyError
from quak.ssh import hostkeys, keys, manager, profiles, system_ssh, transport
from quak.ssh.manager import BACKUP_SUFFIX, CARD_KEYS, DROPBEAR_CONF, RUNTIME_KEYS, SshManager
from quak.ssh.models import ExecResult, HostKey, SshKey, SshProfile, TestResult

ADDRESS = "AA:BB:CC:DD:EE:FF"
HOST = "192.168.1.50"
AP_HOST = transport.AP_HOST
PEM_BODY = "c2VjcmV0LWtleS1ib2R5"
PEM = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{PEM_BODY}\n-----END OPENSSH PRIVATE KEY-----\n"
DENIED = "Permission denied (publickey)."

CARD_BAK = CARD_KEYS + BACKUP_SUFFIX
RUNTIME_BAK = RUNTIME_KEYS + BACKUP_SUFFIX
CARD_TEMP = CARD_KEYS + ".quak.tmp"
RUNTIME_TEMP = RUNTIME_KEYS + ".quak.tmp"


def line(key: SshKey) -> bytes:
    return key.public_key.encode() + b"\n"


def public(name: str) -> str:
    return keys.public_line("ssh-ed25519", name.encode(), f"{name}@elsewhere")


# Everything an edit has to leave alone, byte for byte: a comment that isn't UTF-8, a blank line, options
# with quoted spaces, a CRLF line and a line that isn't a key.
PRESERVED = (
    b"# camera keys caf\xe9\n"
    b"\n"
    + f'command="echo hi there",no-pty {public("restricted")}\n'.encode()
    + f"{public('crlf')}\r\n".encode()
    + b"not a key line\n"
)


def store_key(key_id: str, *, public_key: str | None = None, created: str = "2026-01-01T00:00:00") -> SshKey:
    blob = key_id.encode()
    return keys._put_key(
        SshKey(
            id=key_id,
            label=key_id,
            type="ssh-ed25519",
            private_file=f"keys/{key_id}.pem",
            public_key=public_key or keys.public_line("ssh-ed25519", blob, "me@laptop"),
            fingerprint=hostkeys.fingerprint(blob),
            created=created,
        ),
        PEM,
    )


def scanned(host: str, port: int) -> HostKey:
    blob = b"camera-host-key"
    encoded = keys.public_line("ssh-ed25519", blob).split()[1]
    return HostKey(host, port, "ssh-ed25519", hostkeys.fingerprint(blob), line=f"{host} ssh-ed25519 {encoded}")


def pin(host: str = HOST, port: int = 22) -> None:
    key = scanned(host, port)
    hostkeys.add_host_key(key)
    hostkeys.remember_fingerprint(host, port, key.key_type, key.fingerprint)


class FakeNetwork:
    def __init__(self):
        self.held = False

    def __call__(self, profile):
        return self

    async def __aenter__(self):
        self.held = True

    async def __aexit__(self, *exc):
        self.held = False


@dataclass
class Connection:
    call: str
    key_path: Path | None
    admitted: bool
    network_held: bool
    thread: threading.Thread


@dataclass
class Fault:
    step: str
    skip: int = 0
    exit_code: int = 1


class FakeCamera:
    """Stands in for the ``system_ssh`` module and holds calls to its real signatures."""

    def __init__(self, network: FakeNetwork):
        self.network = network
        self.identities: dict[Path, str] = {}
        self.files: dict[str, bytes] = {}
        self.modes: dict[str, int] = {}
        self.blank_login = False
        self.refused: set[Path] = set()
        self.connections: list[Connection] = []
        self.trace: list[str] = []  # every file operation and shell step, in the order they ran
        self.commands: list[str] = []
        self.faults: list[Fault] = []
        self.garbled: dict[str, bytes] = {}  # what lands at this path instead, next time it is written or moved to
        self.on_test = lambda key_path: None

    def seed(self, path: str, data: bytes) -> None:
        self.files[path] = data
        self.modes[path] = 0o644

    def admits(self, key_path: Path | None) -> bool:
        if key_path is None:
            return self.blank_login
        if key_path in self.refused:
            return False
        runtime = self.files.get(RUNTIME_KEYS, b"").decode("utf-8", "surrogateescape")
        return keys.has_public_key(runtime, self.identities[key_path])

    @property
    def denied(self) -> list[Connection]:
        return [c for c in self.connections if not c.admitted]

    def _connect(self, call, profile, endpoint, key_path, *args, **kwargs) -> bool:
        inspect.signature(getattr(system_ssh, call)).bind(profile, endpoint, key_path, *args, **kwargs)
        admitted = self.admits(key_path)
        self.connections.append(Connection(call, key_path, admitted, self.network.held, threading.current_thread()))
        return admitted

    def _fault(self, step: str) -> Fault | None:
        for fault in self.faults:
            if fault.step == step:
                if fault.skip:
                    fault.skip -= 1
                    return None
                self.faults.remove(fault)
                return fault
        return None

    def test(self, profile, endpoint, key_path, *, timeout=20):
        admitted = self._connect("test", profile, endpoint, key_path, timeout=timeout)
        self.trace.append("test")
        self.on_test(key_path)
        if not admitted:
            return TestResult(ok=False, error=DENIED)
        return TestResult(ok=True, uid="uid=0(root)", uname="Linux 4.9 armv7l")

    def read_file(self, profile, endpoint, key_path, remote):
        if not self._connect("read_file", profile, endpoint, key_path, remote):
            raise NetworkError(DENIED)
        step = f"read {remote}"
        self.trace.append(step)
        if self._fault(step):
            raise NetworkError("Connection reset by peer")
        if remote not in self.files:
            raise NetworkError(f"cat: can't open '{remote}': No such file or directory")
        return self.files[remote]

    def write_file(self, profile, endpoint, key_path, remote, data, *, mode="600"):
        if not self._connect("write_file", profile, endpoint, key_path, remote, data, mode=mode):
            raise NetworkError(DENIED)
        step = f"write {remote}"
        self.trace.append(step)
        if self._fault(step):
            raise NetworkError("No space left on device")
        self.files[remote] = self.garbled.pop(remote, data)
        self.modes[remote] = int(mode, 8)

    def exec_command(self, profile, endpoint, key_path, command, *, input=None, timeout=120):
        admitted = self._connect("exec_command", profile, endpoint, key_path, command, input=input, timeout=timeout)
        self.commands.append(command)
        if not admitted:
            return ExecResult(exit_code=255, stderr=DENIED.encode())
        return self._shell(command)

    def _shell(self, command: str) -> ExecResult:
        """Run the few commands the manager is meant to send. Anything else fails the test."""
        umask = command.startswith("umask 077; ")
        stdout = b""
        for step in command.removeprefix("umask 077; ").split(" && "):
            self.trace.append(step)
            fault = self._fault(step)
            if fault:
                return ExecResult(exit_code=fault.exit_code, stdout=stdout, stderr=b"it broke")
            match shlex.split(step):
                case ["test", "-e", path]:
                    if path not in self.files:
                        return ExecResult(exit_code=1, stdout=stdout)
                case ["cp", source, target] if source in self.files:
                    self.files[target] = self.garbled.pop(target, self.files[source])
                    self.modes[target] = 0o600 if umask else 0o644
                case ["chmod", "600", path] if path in self.files:
                    self.modes[path] = 0o600
                case ["mv", "-f", source, target] if source in self.files:
                    moved = self.files.pop(source)
                    self.files[target] = self.garbled.pop(target, moved)
                    self.modes[target] = self.modes.pop(source)
                case ["rm", "-f", *paths]:
                    for path in paths:
                        self.files.pop(path, None)
                        self.modes.pop(path, None)
                case ["sync"]:
                    pass
                case ["cat", path] if path in self.files:
                    stdout += self.files[path]
                case _:
                    raise AssertionError(f"the camera was sent a step it can't run: {step!r}")
        return ExecResult(exit_code=0, stdout=stdout)


class KeysCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (
            mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": str(Path(tmp.name) / "config")}),
            mock.patch("subprocess.run", side_effect=AssertionError("a test reached subprocess.run")),
            mock.patch("socket.create_connection", side_effect=AssertionError("a test reached the network")),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.network = FakeNetwork()
        self.camera = FakeCamera(self.network)
        self.manager = SshManager(
            network_context=self.network,
            ssh=self.camera,
            scan=scanned,
            banner=lambda host, port: (True, "SSH-2.0-dropbear"),
            reachable=lambda host, port: False,
            find_nets=lambda: [],
        )
        self.old = self.store("k_old", created="2026-01-01T00:00:00")
        self.new = self.store("k_new", created="2026-01-02T00:00:00")
        self.other = self.store("k_other", created="2026-01-03T00:00:00")
        self.old_path, self.new_path, self.other_path = map(keys.key_path, (self.old, self.new, self.other))
        self.profile = SshProfile(address=ADDRESS, transport="host", host=HOST, key_id=self.old.id)
        profiles.save(self.profile)
        pin()

    def store(self, key_id: str, **fields) -> SshKey:
        key = store_key(key_id, **fields)
        self.camera.identities[keys.key_path(key)] = key.public_key
        return key

    def seed_both(self, data: bytes) -> None:
        self.camera.seed(CARD_KEYS, data)
        self.camera.seed(RUNTIME_KEYS, data)

    def assert_both(self, data: bytes) -> None:
        self.assertEqual(self.camera.files.get(CARD_KEYS), data)
        self.assertEqual(self.camera.files.get(RUNTIME_KEYS), data)

    def assert_no_temp(self) -> None:
        self.assertNotIn(CARD_TEMP, self.camera.files)
        self.assertNotIn(RUNTIME_TEMP, self.camera.files)

    def assert_nothing_written(self) -> None:
        wrote = [step for step in self.camera.trace if step.startswith(("write ", "cp ", "mv ", "rm ", "chmod "))]
        self.assertEqual(wrote, [])

    def assert_in_order(self, steps: list[str]) -> None:
        at = 0
        for step in steps:
            try:
                at = self.camera.trace.index(step, at) + 1
            except ValueError:
                self.fail(f"{step!r} is missing or out of order in {self.camera.trace}")


class InstallTests(KeysCase):
    def setUp(self):
        super().setUp()
        self.before = PRESERVED + line(self.old)
        self.seed_both(self.before)

    async def test_adds_the_key_to_both_files_and_keeps_every_other_line(self):
        result = await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_both(self.before + line(self.new))
        self.assertEqual(result.installed, [self.new.fingerprint])
        self.assertEqual(result.already, [])
        self.assertTrue(result.verified)
        self.assertEqual(self.camera.denied, [])

    async def test_the_steps_run_in_the_safe_order(self):
        await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_in_order(
            [
                f"read {CARD_KEYS}",
                f"read {RUNTIME_KEYS}",
                f"write {CARD_TEMP}",
                f"read {CARD_TEMP}",
                f"cp {CARD_KEYS} {CARD_BAK}",
                f"read {CARD_BAK}",
                f"cp {RUNTIME_KEYS} {RUNTIME_BAK}",
                f"read {RUNTIME_BAK}",
                f"chmod 600 {CARD_TEMP}",
                f"mv -f {CARD_TEMP} {CARD_KEYS}",
                "sync",
                f"read {CARD_KEYS}",
                f"cp {CARD_KEYS} {RUNTIME_TEMP}",
                f"chmod 600 {RUNTIME_TEMP}",
                f"read {RUNTIME_TEMP}",
                f"mv -f {RUNTIME_TEMP} {RUNTIME_KEYS}",
                f"cat {RUNTIME_KEYS}",
                "test",
            ]
        )
        self.assert_no_temp()

    async def test_every_file_it_leaves_behind_is_0600(self):
        await self.manager.install_key(ADDRESS, self.new.id)
        for path in (CARD_KEYS, RUNTIME_KEYS, CARD_BAK, RUNTIME_BAK):
            self.assertEqual(self.camera.modes[path], 0o600, path)

    async def test_the_backups_stay_and_hold_what_was_there(self):
        await self.manager.install_key(ADDRESS, self.new.id)
        self.assertEqual(self.camera.files[CARD_BAK], self.before)
        self.assertEqual(self.camera.files[RUNTIME_BAK], self.before)

    async def test_the_new_key_is_proven_on_its_own_connection(self):
        await self.manager.install_key(ADDRESS, self.new.id)
        tests = [c for c in self.camera.connections if c.call == "test"]
        self.assertEqual([c.key_path for c in tests], [self.new_path])
        self.assertTrue(tests[0].admitted)

    async def test_without_a_key_id_it_installs_the_profile_key(self):
        result = await self.manager.install_key(ADDRESS)
        self.assertEqual(result.already, [self.old.fingerprint])
        self.assert_nothing_written()

    async def test_a_key_already_on_both_files_is_only_proven(self):
        self.seed_both(self.before + line(self.new))
        result = await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_nothing_written()
        self.assertEqual(result.already, [self.new.fingerprint])
        self.assertEqual(result.installed, [])
        self.assertEqual(self.camera.trace[-1], "test")

    async def test_a_key_already_there_that_cannot_log_in_is_an_error(self):
        self.seed_both(self.before + line(self.new))
        self.camera.refused.add(self.new_path)
        with self.assertRaisesRegex(SshKeyError, "did not accept a login"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_nothing_written()

    async def test_the_same_key_with_another_comment_counts_as_installed(self):
        encoded = self.new.public_key.split()[1]
        with_options = f"no-pty ssh-ed25519 {encoded} another comment\n".encode()
        self.seed_both(self.before + with_options)
        result = await self.manager.install_key(ADDRESS, self.new.id)
        self.assertEqual(result.already, [self.new.fingerprint])
        self.assert_both(self.before + with_options)

    async def test_a_key_on_the_card_but_not_in_the_runtime_copy_is_only_copied_across(self):
        self.camera.seed(CARD_KEYS, self.before + line(self.new))
        result = await self.manager.install_key(ADDRESS, self.new.id)
        self.assertNotIn(f"write {CARD_TEMP}", self.camera.trace)
        self.assert_both(self.before + line(self.new))
        self.assertEqual(result.installed, [self.new.fingerprint])

    async def test_with_no_card_file_it_starts_from_the_runtime_copy(self):
        del self.camera.files[CARD_KEYS]
        await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_both(self.before + line(self.new))
        self.assertNotIn(CARD_BAK, self.camera.files)
        self.assertEqual(self.camera.files[RUNTIME_BAK], self.before)

    async def test_with_no_files_at_all_it_creates_them(self):
        self.camera.files.clear()
        self.camera.blank_login = True
        profile = SshProfile(address=ADDRESS, transport="host", host=HOST, auth="none")
        await self.manager.install_key(ADDRESS, self.new.id, profile=profile)
        self.assert_both(line(self.new))
        self.assertIsNone(self.camera.connections[0].key_path)

    async def test_a_runtime_only_key_is_dropped_and_said_so(self):
        self.camera.seed(RUNTIME_KEYS, self.before + line(self.other))
        result = await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_both(self.before + line(self.new))
        self.assertIn("1 other key(s) only in the runtime copy", result.message)

    async def test_a_login_only_in_the_runtime_copy_stops_the_edit(self):
        self.camera.seed(CARD_KEYS, line(self.other))
        self.camera.seed(RUNTIME_KEYS, line(self.old) + line(self.other))
        with self.assertRaisesRegex(SshKeyError, "runtime copy but not on the card.*Nothing was changed"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_nothing_written()

    async def test_installing_that_runtime_only_login_is_how_to_fix_it(self):
        self.camera.seed(CARD_KEYS, line(self.other))
        self.camera.seed(RUNTIME_KEYS, line(self.old) + line(self.other))
        await self.manager.install_key(ADDRESS, self.old.id)
        self.assert_both(line(self.other) + line(self.old))
        self.assertEqual(self.camera.denied, [])

    async def test_nothing_runs_for_a_password_profile(self):
        profile = SshProfile(address=ADDRESS, transport="host", host=HOST, auth="password")
        with self.assertRaisesRegex(SshError, "interactive terminal"):
            await self.manager.install_key(ADDRESS, self.new.id, profile=profile)
        self.assertEqual(self.camera.connections, [])

    async def test_nothing_runs_for_a_key_that_is_not_stored(self):
        with self.assertRaisesRegex(SshKeyError, "k_gone is not in Quak's key store"):
            await self.manager.install_key(ADDRESS, "k_gone")
        self.assertEqual(self.camera.connections, [])

    async def test_nothing_runs_without_the_private_file_to_prove_it_with(self):
        self.new_path.unlink()
        with self.assertRaisesRegex(SshKeyError, "private key file of k_new is missing"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assertEqual(self.camera.connections, [])

    async def test_nothing_runs_against_an_unpinned_host_key(self):
        hostkeys.forget_fingerprint(HOST, 22)
        with self.assertRaises(HostKeyError):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assertEqual(self.camera.connections, [])

    async def test_the_wifi_is_held_throughout_and_calls_stay_off_the_event_loop(self):
        pin(AP_HOST)
        profile = SshProfile(address=ADDRESS, transport="wifi", key_id=self.old.id)
        await self.manager.install_key(ADDRESS, self.new.id, profile=profile)
        self.assertFalse(self.network.held)
        self.assertTrue(all(c.network_held for c in self.camera.connections))
        self.assertNotIn(threading.current_thread(), {c.thread for c in self.camera.connections})


class ReadFailureTests(KeysCase):
    """A read that fails is not an empty file: treating it as one would write the other keys away."""

    def setUp(self):
        super().setUp()
        self.before = PRESERVED + line(self.old)
        self.seed_both(self.before)

    async def test_a_failed_card_read_stops_before_anything_is_written(self):
        self.camera.faults.append(Fault(f"read {CARD_KEYS}"))
        with self.assertRaisesRegex(SshKeyError, "could not read /tmp/fuse_d/authorized_keys"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_nothing_written()
        self.assert_both(self.before)

    async def test_a_failed_runtime_read_stops_before_anything_is_written(self):
        self.camera.faults.append(Fault(f"read {RUNTIME_KEYS}"))
        with self.assertRaisesRegex(SshKeyError, "could not read /dev/dropbear/authorized_keys"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_nothing_written()

    async def test_a_connection_lost_while_checking_for_the_file_is_not_a_missing_file(self):
        del self.camera.files[CARD_KEYS]
        self.camera.faults.append(Fault(f"test -e {CARD_KEYS}", exit_code=255))
        with self.assertRaisesRegex(SshKeyError, "could not read"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_nothing_written()

    async def test_an_unusable_login_reports_the_read_and_writes_nothing(self):
        self.seed_both(line(self.other))
        with self.assertRaisesRegex(SshKeyError, "Permission denied"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_nothing_written()


class FailureBeforeTheSwapTests(KeysCase):
    def setUp(self):
        super().setUp()
        self.before = PRESERVED + line(self.old)
        self.seed_both(self.before)

    async def check_untouched(self, message: str) -> None:
        with self.assertRaisesRegex(SshKeyError, message + ".*key files were not changed"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_both(self.before)
        self.assert_no_temp()
        self.assertNotIn(f"mv -f {CARD_TEMP} {CARD_KEYS}", self.camera.trace)

    async def test_a_failed_temp_write(self):
        self.camera.faults.append(Fault(f"write {CARD_TEMP}"))
        await self.check_untouched("No space left")

    async def test_a_temp_file_that_does_not_read_back(self):
        self.camera.garbled[CARD_TEMP] = b"garbage"
        await self.check_untouched("did not read back")

    async def test_a_failed_backup(self):
        self.camera.faults.append(Fault(f"cp {CARD_KEYS} {CARD_BAK}"))
        await self.check_untouched("could not back up")

    async def test_a_backup_that_does_not_read_back(self):
        self.camera.faults.append(Fault(f"read {RUNTIME_BAK}"))
        await self.check_untouched("could not read")


class RollbackTests(KeysCase):
    def setUp(self):
        super().setUp()
        self.before = PRESERVED + line(self.old)
        self.seed_both(self.before)

    async def check_restored(self, message: str, call=None) -> None:
        call = call or (lambda: self.manager.install_key(ADDRESS, self.new.id))
        with self.assertRaisesRegex(SshKeyError, message + ".*restored from the backup"):
            await call()
        self.assert_both(self.before)
        self.assert_no_temp()
        self.assertTrue(self.camera.admits(self.old_path))

    async def test_a_new_key_that_cannot_log_in_is_taken_back_out(self):
        self.camera.refused.add(self.new_path)
        await self.check_restored("did not accept a login")
        self.assertNotIn(line(self.new), self.camera.files[RUNTIME_KEYS])

    async def test_a_lost_connection_during_the_proof(self):
        def lost(key_path):
            raise NetworkError("Connection timed out")

        self.camera.on_test = lost
        await self.check_restored("Connection timed out")

    async def test_a_failed_move_onto_the_card(self):
        self.camera.faults.append(Fault(f"mv -f {CARD_TEMP} {CARD_KEYS}", exit_code=255))
        await self.check_restored("it broke")

    async def test_a_card_file_that_does_not_read_back(self):
        self.camera.garbled[CARD_KEYS] = b"garbage"
        await self.check_restored(f"{CARD_KEYS} did not read back")

    async def test_a_failed_copy_to_the_runtime_file(self):
        self.camera.faults.append(Fault(f"cp {CARD_KEYS} {RUNTIME_TEMP}"))
        await self.check_restored("could not copy")

    async def test_a_bad_runtime_copy_is_caught_before_it_can_end_the_login(self):
        self.camera.garbled[RUNTIME_TEMP] = b"garbage"
        await self.check_restored(f"{RUNTIME_TEMP} did not read back")
        self.assertEqual(self.camera.trace.count(f"mv -f {RUNTIME_TEMP} {RUNTIME_KEYS}"), 1)  # the restore's own
        self.assertEqual(self.camera.denied, [])

    async def test_a_swap_that_ends_the_login_does_not_claim_a_restore(self):
        self.camera.garbled[RUNTIME_KEYS] = b"garbage"
        with self.assertRaises(SshKeyError) as caught:
            await self.manager.install_key(ADDRESS, self.new.id)
        message = str(caught.exception)
        self.assertIn(f"{RUNTIME_KEYS} did not read back", message)
        self.assertIn("failed too", message)
        self.assertIn(RUNTIME_BAK, message)
        self.assertNotIn("restored from the backup", message)

    async def test_a_bug_still_restores_and_surfaces_as_itself(self):
        def boom(key_path):
            raise RuntimeError("a bug")

        self.camera.on_test = boom
        with self.assertRaisesRegex(RuntimeError, "a bug"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assert_both(self.before)

    async def test_a_file_that_was_not_there_goes_away_again_not_back_to_an_old_backup(self):
        del self.camera.files[CARD_KEYS]
        self.camera.seed(CARD_BAK, b"# a backup from some other day\n")
        self.camera.refused.add(self.new_path)
        with self.assertRaisesRegex(SshKeyError, "restored from the backup"):
            await self.manager.install_key(ADDRESS, self.new.id)
        self.assertNotIn(CARD_KEYS, self.camera.files)
        self.assertEqual(self.camera.files[RUNTIME_KEYS], self.before)
        self.assert_no_temp()

    async def test_a_failed_restore_says_so_and_where_the_backups_are(self):
        self.camera.refused.add(self.new_path)
        self.camera.faults.append(Fault(f"cp {CARD_BAK} {CARD_TEMP}"))
        with self.assertRaises(SshKeyError) as caught:
            await self.manager.install_key(ADDRESS, self.new.id)
        message = str(caught.exception)
        self.assertIn("failed too", message)
        self.assertIn(CARD_BAK, message)
        self.assertIn(RUNTIME_BAK, message)
        self.assertNotIn("restored from the backup", message)

    async def test_a_restore_that_does_not_read_back_counts_as_failed(self):
        def garble_the_restore(key_path):
            self.camera.garbled[CARD_KEYS] = b"garbage"

        self.camera.refused.add(self.new_path)
        self.camera.on_test = garble_the_restore
        with self.assertRaisesRegex(SshKeyError, "failed too.*did not read back as it was"):
            await self.manager.install_key(ADDRESS, self.new.id)


class RotateTests(KeysCase):
    def setUp(self):
        super().setUp()
        self.before = PRESERVED + line(self.old)
        self.seed_both(self.before)

    def rotate(self, **kwargs):
        return self.manager.rotate_key(ADDRESS, self.old.id, self.new.id, **kwargs)

    async def test_swaps_the_key_and_keeps_every_other_line(self):
        result = await self.rotate()
        self.assert_both(PRESERVED + line(self.new))
        self.assertEqual(result.installed, [self.new.fingerprint])
        self.assertEqual(result.removed, [self.old.fingerprint])
        self.assertTrue(result.verified)
        self.assertFalse(self.camera.admits(self.old_path))
        self.assertTrue(self.camera.admits(self.new_path))

    async def test_the_old_key_is_still_there_when_the_new_one_is_proven(self):
        seen = {}

        def at_the_test(key_path):
            seen["key_path"] = key_path
            seen["card"] = self.camera.files[CARD_KEYS]
            seen["runtime"] = self.camera.files[RUNTIME_KEYS]

        self.camera.on_test = at_the_test
        await self.rotate()
        self.assertEqual(seen["key_path"], self.new_path)
        self.assertEqual(seen["card"], self.before + line(self.new))
        self.assertEqual(seen["runtime"], self.before + line(self.new))

    async def test_no_login_is_ever_refused_along_the_way(self):
        await self.rotate()
        self.assertEqual(self.camera.denied, [])

    async def test_the_removal_logs_in_with_the_new_key(self):
        await self.rotate()
        at = next(i for i, c in enumerate(self.camera.connections) if c.call == "test")
        self.assertEqual({c.key_path for c in self.camera.connections[at:]}, {self.new_path})

    async def test_the_backup_holds_the_state_from_before_the_rotation(self):
        await self.rotate()
        self.assertEqual(self.camera.files[CARD_BAK], self.before)
        self.assertEqual(self.camera.files[RUNTIME_BAK], self.before)

    async def test_a_new_key_that_cannot_log_in_keeps_the_old_one(self):
        self.camera.refused.add(self.new_path)
        with self.assertRaisesRegex(SshKeyError, "did not accept a login.*restored from the backup"):
            await self.rotate()
        self.assert_both(self.before)
        self.assertTrue(self.camera.admits(self.old_path))
        self.assertEqual(profiles.load(ADDRESS).key_id, self.old.id)

    async def test_a_failed_removal_goes_all_the_way_back(self):
        # The second runtime swap is the one that takes the old key out.
        self.camera.faults.append(Fault(f"mv -f {RUNTIME_TEMP} {RUNTIME_KEYS}", skip=1))
        with self.assertRaisesRegex(SshKeyError, "could not update.*restored from the backup"):
            await self.rotate()
        self.assert_both(self.before)
        self.assert_no_temp()
        self.assertTrue(self.camera.admits(self.old_path))
        self.assertEqual(self.camera.denied, [])
        self.assertEqual(profiles.load(ADDRESS).key_id, self.old.id)

    async def test_a_removal_that_fails_after_the_old_key_is_gone_can_still_be_undone(self):
        def garble_the_removal(key_path):
            self.camera.garbled[RUNTIME_KEYS] = b"# wrong\n" + line(self.new)

        self.camera.on_test = garble_the_removal
        with self.assertRaisesRegex(SshKeyError, "did not read back.*restored from the backup"):
            await self.rotate()
        self.assert_both(self.before)
        self.assertEqual(self.camera.denied, [])

    async def test_the_same_id_is_refused(self):
        with self.assertRaisesRegex(SshKeyError, "same key"):
            await self.manager.rotate_key(ADDRESS, self.old.id, self.old.id)
        self.assertEqual(self.camera.connections, [])

    async def test_the_same_key_under_another_id_is_refused(self):
        twin = self.store("k_twin", public_key=keys.public_line("ssh-ed25519", b"k_old", "another comment"))
        with self.assertRaisesRegex(SshKeyError, "same key"):
            await self.manager.rotate_key(ADDRESS, self.old.id, twin.id)
        self.assertEqual(self.camera.connections, [])

    async def test_running_it_again_after_it_landed_only_proves_the_key(self):
        await self.rotate()
        self.camera.trace.clear()
        again = await self.rotate()
        self.assert_nothing_written()
        self.assertEqual(again.already, [self.new.fingerprint])
        self.assertEqual(again.removed, [])
        self.assertIn(f"{self.old.fingerprint} was not on the camera", again.message)

    async def test_an_old_key_that_is_not_there_makes_it_an_install(self):
        profiles.save(SshProfile(address=ADDRESS, transport="host", host=HOST, key_id=self.other.id))
        self.seed_both(line(self.other))
        result = await self.rotate()
        self.assert_both(line(self.other) + line(self.new))
        self.assertEqual(result.removed, [])

    async def test_a_login_only_in_the_runtime_copy_stops_the_edit(self):
        self.camera.seed(CARD_KEYS, line(self.other))
        self.camera.seed(RUNTIME_KEYS, line(self.old) + line(self.other))
        with self.assertRaisesRegex(SshKeyError, "runtime copy but not on the card"):
            await self.rotate()
        self.assert_nothing_written()

    async def test_the_last_key_can_be_rotated_on_a_keyonly_camera(self):
        self.camera.seed(DROPBEAR_CONF, b"AUTH=keyonly\n")
        self.seed_both(line(self.old))
        await self.rotate()
        self.assert_both(line(self.new))
        self.assertNotIn(f"read {DROPBEAR_CONF}", self.camera.trace)

    async def test_the_saved_profile_follows_to_the_new_key(self):
        await self.rotate()
        self.assertEqual(profiles.load(ADDRESS).key_id, self.new.id)

    async def test_a_profile_logging_in_with_another_key_stays_on_it(self):
        profiles.save(SshProfile(address=ADDRESS, transport="host", host=HOST, key_id=self.other.id))
        self.seed_both(line(self.old) + line(self.other))
        await self.rotate()
        self.assertEqual(profiles.load(ADDRESS).key_id, self.other.id)
        self.assert_both(line(self.other) + line(self.new))

    async def test_a_profile_on_the_default_key_is_pinned_to_the_new_one(self):
        # The old key is the first stored, so without this the profile would keep picking it.
        profiles.save(SshProfile(address=ADDRESS, transport="host", host=HOST))
        await self.rotate()
        self.assertEqual(profiles.load(ADDRESS).key_id, self.new.id)

    async def test_a_one_off_route_override_is_not_written_back(self):
        profiles.delete(ADDRESS)
        await self.rotate(profile=self.profile)
        saved = profiles.load(ADDRESS)
        self.assertEqual(saved.key_id, self.new.id)
        self.assertEqual(saved.transport, "auto")
        self.assertIsNone(saved.host)

    async def test_a_profile_that_cannot_be_saved_does_not_undo_a_finished_rotation(self):
        with mock.patch.object(profiles, "save", side_effect=OSError("disk full")):
            result = await self.rotate()
        self.assert_both(PRESERVED + line(self.new))
        self.assertIn("point it at k_new", result.message)


class RevokeTests(KeysCase):
    def setUp(self):
        super().setUp()
        self.camera.seed(DROPBEAR_CONF, b"AUTH=keyonly\n")
        self.before = PRESERVED + line(self.old) + line(self.other)
        self.seed_both(self.before)

    async def test_removes_the_key_and_keeps_every_other_line(self):
        result = await self.manager.revoke_key(ADDRESS, self.other.id)
        self.assert_both(PRESERVED + line(self.old))
        self.assertEqual(result.removed, [self.other.fingerprint])
        self.assertEqual(result.installed, [])
        self.assertTrue(result.verified)

    async def test_it_matches_the_blob_whatever_the_options_or_comment(self):
        encoded = self.other.public_key.split()[1]
        self.seed_both(
            PRESERVED
            + line(self.old)
            + f'no-pty,command="a b" ssh-ed25519 {encoded} one comment\n'.encode()
            + f"ssh-ed25519 {encoded} another comment\n".encode()
        )
        await self.manager.revoke_key(ADDRESS, self.other.id)
        self.assert_both(PRESERVED + line(self.old))

    async def test_it_leaves_a_commented_out_copy_of_the_key_alone(self):
        comment = b"# " + line(self.other)
        self.seed_both(comment + line(self.old) + line(self.other))
        await self.manager.revoke_key(ADDRESS, self.other.id)
        self.assert_both(comment + line(self.old))

    async def test_a_key_only_in_the_runtime_copy_is_removed_from_it(self):
        self.camera.seed(CARD_KEYS, line(self.old))
        self.camera.seed(RUNTIME_KEYS, line(self.old) + line(self.other))
        result = await self.manager.revoke_key(ADDRESS, self.other.id)
        self.assert_both(line(self.old))
        self.assertEqual(result.removed, [self.other.fingerprint])
        self.assertNotIn(f"write {CARD_TEMP}", self.camera.trace)
        self.assertNotIn("other key(s)", result.message)

    async def test_a_key_that_is_not_there_changes_nothing(self):
        self.seed_both(PRESERVED + line(self.old))
        result = await self.manager.revoke_key(ADDRESS, self.other.id)
        self.assert_nothing_written()
        self.assertEqual(result.removed, [])
        self.assertIn("was not on the camera", result.message)

    async def test_revoking_the_key_it_logged_in_with_finishes_and_says_so(self):
        result = await self.manager.revoke_key(ADDRESS, self.old.id)
        self.assert_both(PRESERVED + line(self.other))
        self.assertEqual(self.camera.denied, [])
        self.assertFalse(self.camera.admits(self.old_path))
        self.assertIn("point the profile at another one", result.message)

    async def test_revoking_a_runtime_only_login_is_allowed(self):
        self.camera.seed(CARD_KEYS, line(self.other))
        self.camera.seed(RUNTIME_KEYS, line(self.old) + line(self.other))
        await self.manager.revoke_key(ADDRESS, self.old.id)
        self.assert_both(line(self.other))

    async def test_a_failure_restores_the_key(self):
        self.camera.faults.append(Fault(f"mv -f {CARD_TEMP} {CARD_KEYS}"))
        with self.assertRaisesRegex(SshKeyError, "restored from the backup"):
            await self.manager.revoke_key(ADDRESS, self.other.id)
        self.assert_both(self.before)
        self.assert_no_temp()

    async def test_nothing_needs_the_private_file_of_the_key_being_revoked(self):
        self.other_path.unlink()
        await self.manager.revoke_key(ADDRESS, self.other.id)
        self.assert_both(PRESERVED + line(self.old))


class LastKeyTests(KeysCase):
    def setUp(self):
        super().setUp()
        self.before = line(self.old)
        self.seed_both(self.before)

    async def check_refused(self, message: str) -> None:
        with self.assertRaisesRegex(SshKeyError, message):
            await self.manager.revoke_key(ADDRESS, self.old.id)
        self.assert_nothing_written()
        self.assert_both(self.before)

    async def test_refused_on_a_keyonly_camera(self):
        self.camera.seed(DROPBEAR_CONF, b"PORT=22\nAUTH=keyonly\n")
        await self.check_refused("last key.*only accepts keys.*Nothing was changed")

    async def test_refused_when_the_auth_mode_is_unknown(self):
        for conf in (b"", b"PORT=22\n", b"#AUTH=password\n", b"AUTH=whatever\n", b"AUTH=password\nAUTH=keyonly\n"):
            with self.subTest(conf=conf):
                self.camera.seed(DROPBEAR_CONF, conf)
                await self.check_refused("last key.*could not be read")

    async def test_refused_when_there_is_no_conf_file(self):
        await self.check_refused("last key.*could not be read")

    async def test_refused_when_the_conf_file_cannot_be_read(self):
        self.camera.seed(DROPBEAR_CONF, b"AUTH=password\n")
        self.camera.faults.append(Fault(f"read {DROPBEAR_CONF}"))
        await self.check_refused("last key.*could not be read")

    async def test_refused_when_the_key_is_only_in_the_runtime_copy_and_the_card_has_none(self):
        self.camera.seed(DROPBEAR_CONF, b"AUTH=keyonly\n")
        self.camera.seed(CARD_KEYS, b"# nothing here\n")
        with self.assertRaisesRegex(SshKeyError, "last key"):
            await self.manager.revoke_key(ADDRESS, self.old.id)
        self.assert_nothing_written()

    async def test_a_comment_or_a_broken_line_is_not_another_key(self):
        self.camera.seed(DROPBEAR_CONF, b"AUTH=keyonly\n")
        self.before = b"# " + line(self.other) + b"ssh-ed25519 not*base64\n" + line(self.old)
        self.seed_both(self.before)
        await self.check_refused("last key")

    async def test_the_same_key_twice_is_still_the_last_key(self):
        self.camera.seed(DROPBEAR_CONF, b"AUTH=keyonly\n")
        self.before = line(self.old) + line(self.old)
        self.seed_both(self.before)
        await self.check_refused("last key")

    async def test_allowed_when_a_password_or_blank_login_remains(self):
        for conf in (b"AUTH=password\n", b'AUTH="password"\n', b"AUTH=blank\n", b"AUTH=\n"):
            with self.subTest(conf=conf):
                self.camera.seed(DROPBEAR_CONF, conf)
                self.seed_both(self.before)
                result = await self.manager.revoke_key(ADDRESS, self.old.id)
                self.assert_both(b"")
                self.assertEqual(result.removed, [self.old.fingerprint])

    async def test_a_key_that_is_not_the_last_is_fine_on_a_keyonly_camera(self):
        self.camera.seed(DROPBEAR_CONF, b"AUTH=keyonly\n")
        self.seed_both(line(self.old) + line(self.other))
        await self.manager.revoke_key(ADDRESS, self.other.id)
        self.assert_both(line(self.old))

    async def test_dropbear_conf_is_only_ever_read(self):
        conf = b"AUTH=password\n"
        self.camera.seed(DROPBEAR_CONF, conf)
        await self.manager.revoke_key(ADDRESS, self.old.id)
        self.assertEqual(self.camera.files[DROPBEAR_CONF], conf)
        self.assertEqual({step for step in self.camera.trace if DROPBEAR_CONF in step}, {f"read {DROPBEAR_CONF}"})
        self.assertFalse([command for command in self.camera.commands if DROPBEAR_CONF in command])

    def test_reading_the_auth_mode(self):
        cases = {
            "AUTH=keyonly\n": "keyonly",
            "AUTH=KeyOnly\r\n": "keyonly",
            "export AUTH=keyonly\n": "keyonly",
            'AUTH="keyonly"  # no passwords\n': "keyonly",
            "  AUTH='password'\n": "password",
            "AUTH=blank\n": "blank",
            "AUTH=\n": "blank",
            "AUTH=keyonly\nAUTH=keyonly\n": "keyonly",
            "AUTH=keyonly\nAUTH=password\n": "unknown",
            "AUTH=none\n": "unknown",
            "# AUTH=keyonly\n": "unknown",
            "NOAUTH=keyonly\n": "unknown",
            "PORT=22\n": "unknown",
            "": "unknown",
        }
        for conf, mode in cases.items():
            with self.subTest(conf=conf):
                self.assertEqual(manager._auth_mode(conf), mode)
        self.assertEqual(manager._auth_mode(None), "unknown")


class SecretsTests(KeysCase):
    def secrets(self) -> list[str]:
        encoded = [key.public_key.split()[1] for key in (self.old, self.new, self.other)]
        return [PEM_BODY, "PRIVATE KEY", *encoded]

    async def test_no_key_text_on_any_command_line(self):
        self.seed_both(line(self.old))
        await self.manager.rotate_key(ADDRESS, self.old.id, self.new.id)
        for command in self.camera.commands:
            for secret in self.secrets():
                self.assertNotIn(secret, command)

    async def test_no_key_text_in_any_result(self):
        self.seed_both(line(self.old))
        result = await self.manager.rotate_key(ADDRESS, self.old.id, self.new.id)
        for secret in self.secrets():
            self.assertNotIn(secret, repr(result))

    async def test_no_key_text_in_any_error(self):
        scenarios = {
            "the proof": lambda: self.camera.refused.add(self.new_path),
            "a bad readback": lambda: self.camera.garbled.update({CARD_KEYS: line(self.other)}),
            "a failed temp write": lambda: self.camera.faults.append(Fault(f"write {CARD_TEMP}")),
            "a failed restore": lambda: (
                self.camera.refused.add(self.new_path),
                self.camera.faults.append(Fault(f"cp {CARD_BAK} {CARD_TEMP}")),
            ),
        }
        for name, arrange in scenarios.items():
            with self.subTest(name):
                self.camera.refused.clear()
                self.camera.faults.clear()
                self.camera.garbled.clear()
                self.seed_both(line(self.old))
                arrange()
                with self.assertRaises(SshKeyError) as caught:
                    await self.manager.install_key(ADDRESS, self.new.id)
                for secret in self.secrets():
                    self.assertNotIn(secret, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    async def test_no_key_text_in_the_last_key_refusal(self):
        self.camera.seed(DROPBEAR_CONF, b"AUTH=keyonly\n")
        self.seed_both(line(self.old))
        with self.assertRaises(SshKeyError) as caught:
            await self.manager.revoke_key(ADDRESS, self.old.id)
        for secret in self.secrets():
            self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

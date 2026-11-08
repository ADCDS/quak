"""The SSH facade the CLI uses: turn "do this on camera X" into one checked OpenSSH call.

It resolves the route to the camera, holds the camera's Wi-Fi only when that route needs it,
enforces the host-key pin, picks the key, and hands off to ``system_ssh``. Blocking work (probes,
ssh-keyscan, ssh) runs in a thread: a BLE session held open for Wi-Fi has to keep its keep-alive
going for as long as the SSH operation lasts.

Key generation lives in ``keys``. Installing, rotating and revoking keys on the camera lives here, on
top of the text helpers in ``keys``; ``dropbear.conf`` is only ever read.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncContextManager

from quak.connect import Target, open_camera
from quak.core.errors import HostKeyError, NetworkError, QuakError, SshError, SshKeyError
from quak.net.wifi import camera_wifi
from quak.ssh import arm as sc_arm
from quak.ssh import hostkeys, keys, profiles, system_ssh, transport
from quak.ssh.models import (
    ArmResult,
    Endpoint,
    ExecResult,
    HostKey,
    KeyInstallResult,
    SshKey,
    SshProfile,
    SshStatus,
    TestResult,
)

NetworkContext = Callable[[SshProfile], AsyncContextManager[Any]]

_USB_HTTP_PORT = 8080  # transport accepts a USB camera that answers here rather than on the profile's port

# Fixed paths with nothing a shell would interpret, so the commands below use them unquoted.
CARD_KEYS = "/tmp/fuse_d/authorized_keys"  # authoritative: survives a reboot
RUNTIME_KEYS = "/dev/dropbear/authorized_keys"  # what the running SSH server checks a login against
DROPBEAR_CONF = "/tmp/fuse_d/dropbear.conf"
BACKUP_SUFFIX = ".quak.bak"
_TEMP_SUFFIX = ".quak.tmp"
_CARD_TEMP = CARD_KEYS + _TEMP_SUFFIX

_AUTH_LINE = re.compile(r"\s*(?:export\s+)?AUTH=(.*)")
_AUTH_MODES = ("keyonly", "password", "blank")
_LAST_KEY_REMOVABLE = ("password", "blank")  # the camera still has a way in without any key


@contextlib.asynccontextmanager
async def camera_network(profile: SshProfile) -> AsyncIterator[None]:
    """Hold the camera's Wi-Fi: a BLE session to switch it on, and this computer joined to it."""
    async with open_camera(Target(address=profile.address)) as cam, camera_wifi(cam):
        yield


def _login_key(profile: SshProfile) -> SshKey | None:
    """The stored key this profile logs in with, if it logs in with one."""
    if profile.auth != "key":
        return None
    return keys.get_key(profile.key_id) if profile.key_id else keys.default_key()


def _private_file(key: SshKey) -> Path:
    path = keys.key_path(key)
    if not path.is_file():
        raise SshKeyError(f"the private key file of {key.id} is missing. Run `quak ssh key delete` and add it again")
    return path


def _required_key(profile: SshProfile) -> SshKey | None:
    """``_login_key``, except that a profile which logs in with a key has to have one."""
    key = _login_key(profile)
    if key is None and profile.auth == "key":
        if profile.key_id:
            raise SshKeyError(
                f"SSH key {profile.key_id} is not in Quak's key store. Run `quak ssh key list`, "
                "then point the profile at a key that is there"
            )
        raise SshKeyError("Quak has no SSH key yet. Run `quak ssh key generate` or `quak ssh key import`")
    return key


def _key_path(profile: SshProfile) -> Path | None:
    """The private key file this profile logs in with. No key for ``auth=none`` or ``auth=password``."""
    key = _required_key(profile)
    return _private_file(key) if key else None


def _private_pem(key: SshKey) -> str:
    _private_file(key)  # says what to do about a missing file
    try:
        return keys.read_private(key)
    except (OSError, UnicodeDecodeError):
        raise SshKeyError(f"could not read the private key file of {key.id}") from None


def _batch_key_path(profile: SshProfile, password: str | None = None) -> Path | None:
    """``_key_path`` for commands that run with nobody at the keyboard, so a password has to come with the call."""
    if profile.auth == "password" and password is None:
        raise SshError(
            "this profile logs in with a password, and this command was not given one. `quak ssh test`, "
            "`quak ssh exec` and `quak lab` read it with --password-stdin. Otherwise use the interactive terminal "
            "(`quak ssh open`), or switch the profile to a key"
        )
    return _key_path(profile)


def _login_password(profile: SshProfile, password: str | None) -> str | None:
    """The password to log in with. Only ``auth=password`` uses one: a key login stays a key login."""
    return password if profile.auth == "password" else None


def _password_option(password: str | None) -> dict[str, str]:
    """``password=`` for a call on ``ssh``, only when there is one, so every other login's calls stay as they were."""
    return {} if password is None else {"password": password}


def _changed_error(seen: HostKey) -> HostKeyError:
    pinned = hostkeys.known(seen.host, seen.port)
    return HostKeyError(
        f"the SSH host key of {seen.host}:{seen.port} has changed: pinned {pinned.fingerprint}, the camera now "
        f"offers {seen.fingerprint}. Someone may be intercepting the connection, or the camera's keys were "
        "regenerated. If you expected this, run `quak ssh host forget`, then `quak ssh host trust`"
    )


def _stored_key(key_id: str) -> SshKey:
    key = keys.get_key(key_id)
    if key is None:
        raise SshKeyError(f"SSH key {key_id} is not in Quak's key store. Run `quak ssh key list` to see what is there")
    return key


def _text(data: bytes) -> str:
    """Decode without losing anything: lines that aren't UTF-8 have to go back on the camera byte for byte."""
    return data.decode("utf-8", "surrogateescape")


def _auth_mode(conf: str | None) -> str:
    """``keyonly``, ``password`` or ``blank`` from the text of dropbear.conf, else ``unknown``.

    Whatever can't be read with confidence is unknown: no file, no ``AUTH`` line, a value not listed
    here, or lines that disagree. An ``AUTH=`` with nothing after it is blank.
    """
    values = set()
    for line in (conf or "").splitlines():
        if match := _AUTH_LINE.fullmatch(line):
            values.add(match[1].split("#", 1)[0].strip().strip("\"'").lower() or "blank")
    mode = values.pop() if len(values) == 1 else ""
    return mode if mode in _AUTH_MODES else "unknown"


def _refuse_dropping_login(
    login_key: SshKey | None, runtime: str, first: str, add: SshKey | None, remove: SshKey | None
) -> None:
    """Refuse an edit whose first publish would end the login it needs for what comes after.

    A key listed only in the runtime copy doesn't survive that copy being replaced by the card's. If
    the login is such a key, a failed step could no longer be rolled back. Revoking that very key is
    the one case with nothing left to do afterwards.
    """
    if login_key is None or not keys.has_public_key(runtime, login_key.public_key):
        return
    if keys.has_public_key(first, login_key.public_key):
        return
    if not add and remove and keys.has_public_key(login_key.public_key, remove.public_key):
        return
    raise SshKeyError(
        f"the key this login uses, {login_key.fingerprint}, is in the camera's runtime copy but not on the card, "
        "and the card decides, so this change would end the login partway. Install that key on the camera first. "
        "Nothing was changed"
    )


def _replace_from(source: str, target: str) -> str:
    """Shell steps that swap in a copy of ``source`` with one rename, so ``target`` is never half written."""
    temp = target + _TEMP_SUFFIX
    return f"cp {source} {temp} && chmod 600 {temp} && mv -f {temp} {target}"


class _CameraKeyFiles:
    """One edit of the camera's two authorized_keys files, from the first read to a readback or a rollback.

    Every ``ssh`` call is a new connection, and the camera checks each login against the runtime file
    as it stands then. So a command that can take the login key out of that file has to finish the job
    itself: replacing the runtime file reads it back in the same command, and a restore is one command.
    Key text only ever travels in ``write_file``'s stdin, never on a command line.
    """

    def __init__(self, ssh: Any, profile: SshProfile, endpoint: Endpoint, login: Path | None) -> None:
        self._ssh = ssh
        self._profile = profile
        self._endpoint = endpoint
        self._first_login = login
        self.login = login  # what the next connection logs in with; a rotation moves it to the proven new key
        self.card: bytes | None = None  # each file as it was found, None if it wasn't there
        self.runtime: bytes | None = None
        self._on_card: bytes | None = None
        self._on_runtime: bytes | None = None
        self._backed_up = False
        self._touched = False

    async def load(self) -> None:
        self.card = self._on_card = await self._read_if_present(CARD_KEYS)
        self.runtime = self._on_runtime = await self._read_if_present(RUNTIME_KEYS)

    async def auth_mode(self) -> str:
        try:
            return _auth_mode(_text(await self._read(DROPBEAR_CONF)))
        except SshKeyError:
            return "unknown"

    async def publish(self, text: str) -> None:
        """Make both files hold ``text``: temp, verify, back up, rename and sync on the card, then copy to runtime."""
        data = text.encode("utf-8", "surrogateescape")
        card_changes = data != self._on_card
        if card_changes:
            await self._call(f"write {_CARD_TEMP}", self._ssh.write_file, _CARD_TEMP, data)
            await self._expect(_CARD_TEMP, data)
        await self._back_up()
        if card_changes:
            self._touched = True
            # The first sync gets the new bytes onto the card before the rename can: the camera can lose power.
            await self._run(
                f"replace {CARD_KEYS}",
                f"chmod 600 {_CARD_TEMP} && sync && mv -f {_CARD_TEMP} {CARD_KEYS} && sync",
            )
            await self._expect(CARD_KEYS, data)
            self._on_card = data
        if data != self._on_runtime:
            self._touched = True
            # Check the copy before it replaces the file logins are checked against. A bad one in
            # that place would end this login, and with it any way to roll back.
            temp = RUNTIME_KEYS + _TEMP_SUFFIX
            await self._run(f"copy the card's keys to {temp}", f"umask 077; cp {CARD_KEYS} {temp} && chmod 600 {temp}")
            await self._expect(temp, data)
            swapped = await self._run(f"update {RUNTIME_KEYS}", f"mv -f {temp} {RUNTIME_KEYS} && cat {RUNTIME_KEYS}")
            if swapped.stdout != data:
                raise SshKeyError(f"{RUNTIME_KEYS} did not read back as written")
            self._on_runtime = data

    async def roll_back(self) -> str:
        """Undo whatever got as far as the camera, and say in a sentence where that leaves it."""
        if not self._touched:
            with contextlib.suppress(SshError):
                await self._run("remove the temporary file", f"rm -f {_CARD_TEMP}")
            return "The camera's key files were not changed"
        try:
            await self._restore()
        except SshError as exc:
            return (
                f"Restoring the camera's key files failed too ({exc}), so they may be left half changed. "
                f"What was there before is in {CARD_KEYS}{BACKUP_SUFFIX} and {RUNTIME_KEYS}{BACKUP_SUFFIX}: "
                "log in with a key that still works and copy them back"
            )
        return "The camera's key files were restored from the backup"

    def _as_found(self) -> tuple[tuple[str, bytes | None], ...]:
        return (CARD_KEYS, self.card), (RUNTIME_KEYS, self.runtime)

    async def _back_up(self) -> None:
        """Save what was found, once per edit, so a rollback always returns to the very start."""
        if self._backed_up:
            return
        for path, found in self._as_found():
            if found is not None:
                backup = path + BACKUP_SUFFIX
                await self._run(f"back up {path}", f"umask 077; cp {path} {backup} && chmod 600 {backup} && sync")
                await self._expect(backup, found)
        self._backed_up = True

    async def _restore(self) -> None:
        # One command: restoring can end the login in use, as it does for a rotation's new key.
        # A file that wasn't there goes away again; any older backup of it describes some other state.
        steps = []
        for path, found in self._as_found():
            if found is None:
                steps.append(f"rm -f {path} {path}{_TEMP_SUFFIX}")
            else:
                steps.append(_replace_from(path + BACKUP_SUFFIX, path))
        await self._run("restore the key files", f"umask 077; {' && '.join(steps)} && sync")
        self.login = self._first_login
        for path, found in self._as_found():
            if await self._read_if_present(path) != found:
                raise SshKeyError(f"{path} did not read back as it was")

    async def _call(self, doing: str, function: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(function, self._profile, self._endpoint, self.login, *args)
        except NetworkError as exc:
            raise SshKeyError(f"could not {doing}: {exc}") from None

    async def _run(self, doing: str, command: str) -> ExecResult:
        result = await self._call(doing, self._ssh.exec_command, command)
        if result.exit_code != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip() or f"exit code {result.exit_code}"
            raise SshKeyError(f"could not {doing}: {detail}")
        return result

    async def _read(self, path: str) -> bytes:
        return await self._call(f"read {path}", self._ssh.read_file, path)

    async def _read_if_present(self, path: str) -> bytes | None:
        """The file's bytes, or None only if the camera says it isn't there.

        ``read_file`` fails the same way for a missing file and a lost connection, and taking a failed
        read for an empty file would write every other key out of authorized_keys.
        """
        try:
            return await self._read(path)
        except SshKeyError:
            probe = await self._call(f"check {path}", self._ssh.exec_command, f"test -e {path}")
            if probe.exit_code != 1:
                raise
        return None

    async def _expect(self, path: str, data: bytes) -> None:
        if await self._read(path) != data:
            raise SshKeyError(f"{path} did not read back as written")


@dataclass(frozen=True)
class SshRoute:
    """A checked way to the camera, good for as long as ``SshManager.route`` holds it."""

    ssh: Any  # anything shaped like the ``system_ssh`` module
    profile: SshProfile
    endpoint: Endpoint
    key_path: Path | None
    password: str | None = field(default=None, repr=False)

    @property
    def login_options(self) -> dict[str, str]:
        """What to add to each call on ``ssh`` for this route's login."""
        return _password_option(self.password)


@dataclass(frozen=True)
class TerminalCredentials:
    """What the Dart SSH client needs to log in: the embedded terminal, or the app's login test on a phone.

    It holds the private key, so don't log it or keep it around.
    """

    host: str
    ssh_port: int
    username: str
    private_key_pem: str | None = field(repr=False)  # None for a profile with ``auth=none`` or ``auth=password``
    passphrase_required: bool
    expected_fingerprint: str
    profile: SshProfile
    password_required: bool = False  # ``auth=password``: someone has to type it, so it's never in here


class SshManager:
    """SSH operations on one camera at a time, addressed by its BLE address.

    Every collaborator that touches the network or a subprocess can be injected. ``ssh`` is anything
    shaped like the ``system_ssh`` module: ``test``, ``exec_command``, ``read_file``, ``write_file``
    and ``open_terminal``.

    ``scan_async``, when given, takes the place of ``scan`` and runs on the event loop. It fails the way
    ``scan`` does, with an ``OSError``, and its key may be fingerprint-only (``line=None``). Such a key
    can be pinned and verified, but gives known_hosts nothing to add for the system ssh to check against.
    """

    def __init__(
        self,
        *,
        network_context: NetworkContext | None = None,
        ssh: Any = None,
        scan: Callable[[str, int], HostKey] | None = None,
        scan_async: Callable[[str, int], Awaitable[HostKey]] | None = None,
        arm_request: Callable[..., Any] | None = None,
        banner: Callable[[str, int], tuple[bool, str]] | None = None,
        reachable: Callable[[str, int], bool] | None = None,
        find_nets: Callable[[], list[transport.UsbNet]] | None = None,
    ) -> None:
        self._network_context = network_context or camera_network
        self._ssh = ssh or system_ssh
        self._scan = scan or hostkeys.scan
        self._scan_async = scan_async
        self._arm_request = arm_request or sc_arm.arm
        self._banner = banner or sc_arm.ssh_banner
        self._reachable = reachable or transport.is_reachable
        self._find_nets = find_nets

    def profile(self, address: str) -> SshProfile:
        """The saved profile, or a new unsaved one for this camera."""
        try:
            return profiles.for_camera(address)
        except ValueError as exc:
            raise SshError(
                f"the saved SSH profile of {address} is not valid: {exc}. "
                "Run `quak ssh profile forget` and set it up again"
            ) from None

    async def resolve(self, address: str, profile: SshProfile | None = None) -> Endpoint:
        """The route to this camera right now, the one an operation would take. Joins nothing and pins nothing.

        ``profile`` overrides the saved one for this call only; it is used as given and never written back.
        """
        return await self._resolve(profile or self.profile(address))

    async def status(self, address: str, *, profile: SshProfile | None = None) -> SshStatus:
        """Report reachability, banner and host-key state. Never joins Wi-Fi and never raises for a dead route.

        Over an unjoined Wi-Fi route this reports what can be seen from here, usually nothing. With SSH
        down there is no key to compare, so ``host_key_state`` only says whether one is pinned.

        ``profile`` overrides the saved one for this call only (the CLI's ``--host``/``--transport``); it
        is used as given and never written back.
        """
        profile = profile or self.profile(address)
        status = SshStatus(profile=profile)
        try:
            endpoint = await self._resolve(profile)
        except NetworkError as exc:
            status.last_error = str(exc)
            return status
        status.endpoint = endpoint
        host, port = endpoint.host, endpoint.ssh_port
        status.ssh_up, answer = await asyncio.to_thread(self._banner, host, port)
        status.banner = answer if status.ssh_up else ""
        status.reachable = bool(answer) or await asyncio.to_thread(self._answers_http, endpoint)
        seen = None
        if status.ssh_up:
            try:
                seen = await self._scan_host(endpoint)
            except SshError as exc:
                status.last_error = str(exc)
        status.host_key = seen or hostkeys.known(host, port)
        status.host_key_state = hostkeys.state(host, port, seen)
        return status

    async def arm(self, address: str, *, profile: SshProfile | None = None) -> ArmResult:
        """Send the SC request. A refusal or a silent port 22 comes back in the result, for the caller to report."""
        profile = profile or self.profile(address)
        # SSH is down until the camera is armed, so resolve's probe of the SSH port would pass over a
        # working explicit host and fall through to Wi-Fi. Its HTTP port is the one that can answer.
        http_up = (
            profile.transport == "auto"
            and profile.host
            and await asyncio.to_thread(self._reachable, profile.host, profile.http_port)
        )
        async with self._connected(profile, probe_custom_host=not http_up) as endpoint:
            result = await self._arm_request(endpoint.host, endpoint.http_port, endpoint.ssh_port)
        if result.accepted:
            profiles.touch_arm(profile.address)
        return result

    async def test(
        self, address: str, *, profile: SshProfile | None = None, password: str | None = None
    ) -> TestResult:
        """Log in and identify the camera. A failed login comes back in the result; a bad host key raises.

        ``password`` is the login of a profile with ``auth=password``. Any other profile doesn't use it.
        """
        profile = profile or self.profile(address)
        key_path = _batch_key_path(profile, password)
        options = _password_option(_login_password(profile, password))
        async with self._connected(profile) as endpoint:
            seen = await self._verified_host_key(endpoint)
            _, banner = await asyncio.to_thread(self._banner, endpoint.host, endpoint.ssh_port)
            result = await asyncio.to_thread(self._ssh.test, profile, endpoint, key_path, **options)
        return replace(result, banner=banner, host_key=seen)

    async def exec(
        self, address: str, command: str, *, profile: SshProfile | None = None, password: str | None = None
    ) -> ExecResult:
        """Run one remote command. Its exit code is in the result, not raised. ``password`` works as in ``test``."""
        if not command.strip():
            raise SshError("no remote command given: pass one after `--`")
        profile = profile or self.profile(address)
        key_path = _batch_key_path(profile, password)
        options = _password_option(_login_password(profile, password))
        async with self._connected(profile) as endpoint:
            await self._verified_host_key(endpoint)
            return await asyncio.to_thread(self._ssh.exec_command, profile, endpoint, key_path, command, **options)

    @contextlib.asynccontextmanager
    async def route(
        self, address: str, *, profile: SshProfile | None = None, password: str | None = None
    ) -> AsyncIterator[SshRoute]:
        """A route held open for several commands: Wi-Fi joined once, the host key verified once.

        Each command on it is still its own ``ssh`` connection, with strict host-key checking. Those
        calls block, so make them from a worker thread: a held Wi-Fi's BLE session needs this loop free.
        ``password`` works as in ``test``, and the route carries it for those calls.
        """
        profile = profile or self.profile(address)
        key_path = _batch_key_path(profile, password)
        async with self._connected(profile) as endpoint:
            await self._verified_host_key(endpoint)
            yield SshRoute(self._ssh, profile, endpoint, key_path, _login_password(profile, password))

    async def open_terminal(self, address: str, *, profile: SshProfile | None = None) -> int:
        """Run the system ssh in the current terminal and return its exit code."""
        profile = profile or self.profile(address)
        key_path = _key_path(profile)
        async with self._connected(profile) as endpoint:
            await self._verified_host_key(endpoint)
            return await asyncio.to_thread(self._ssh.open_terminal, profile, endpoint, key_path)

    async def terminal_credentials(self, address: str) -> TerminalCredentials:
        """What a Dart login needs (``TerminalCredentials``), once the camera has offered its pinned host key.

        This never joins Wi-Fi: over that route (``resolve`` says which it is) the caller has to hold it
        already, and keep holding it for as long as the login lasts. The private key is only read
        after the host key checks out. The Dart client checks the fingerprint itself, so a pin that isn't in
        known_hosts is enough. A profile with ``auth=password`` gets no key, and ``password_required`` tells
        the caller to ask for the password.
        """
        profile = self.profile(address)
        key = _required_key(profile)
        endpoint = await self._resolve(profile)
        seen = await self._verified_host_key(endpoint)
        return TerminalCredentials(
            host=endpoint.host,
            ssh_port=endpoint.ssh_port,
            username=profile.user,
            private_key_pem=_private_pem(key) if key else None,
            passphrase_required=bool(key and key.encrypted),
            expected_fingerprint=seen.fingerprint,
            profile=profile,
            password_required=profile.auth == "password",
        )

    async def scan_host_key(self, address: str, *, profile: SshProfile | None = None) -> HostKey:
        """The key the camera offers right now. Pins nothing."""
        async with self._connected(profile or self.profile(address)) as endpoint:
            return await self._scan_host(endpoint)

    async def trust_host(
        self, address: str, *, fingerprint: str | None = None, profile: SshProfile | None = None
    ) -> HostKey:
        """Pin the key the camera offers now, in the store and, if the scan brought a line for it, in known_hosts.

        Pass the ``fingerprint`` that was reviewed so a different key can't slip in between looking and
        trusting. A pin that no longer matches is never replaced here: it has to be forgotten first.
        """
        async with self._connected(profile or self.profile(address)) as endpoint:
            seen = await self._scan_host(endpoint)
        if fingerprint is not None and seen.fingerprint != fingerprint:
            raise HostKeyError(
                f"{seen.host}:{seen.port} offers {seen.fingerprint}, not the {fingerprint} you asked to trust. "
                "Nothing was pinned"
            )
        if hostkeys.state(seen.host, seen.port, seen) == "changed":
            raise _changed_error(seen)
        pinned = hostkeys.add_host_key(seen) if seen.line else seen
        hostkeys.remember_fingerprint(seen.host, seen.port, seen.key_type, seen.fingerprint)
        return pinned

    async def forget_host(self, address: str, *, profile: SshProfile | None = None) -> bool:
        """Drop the pin and the known_hosts entry of this camera's route. True if there was either.

        It's a local edit, so Wi-Fi is not joined and an explicit host counts whether it answers or not.
        """
        endpoint = await self._resolve(profile or self.profile(address), probe_custom_host=False)
        in_known_hosts = hostkeys.remove_known_host(endpoint.host, endpoint.ssh_port)
        pinned = hostkeys.forget_fingerprint(endpoint.host, endpoint.ssh_port)
        return in_known_hosts or pinned

    async def install_key(
        self, address: str, key_id: str | None = None, *, profile: SshProfile | None = None
    ) -> KeyInstallResult:
        """Add a stored key to the camera and prove it logs in. Without ``key_id``: the profile's key, or the first.

        Logging in to do this takes a key the camera already accepts, or ``auth=none``.
        """
        profile = profile or self.profile(address)
        chosen = key_id or profile.key_id
        key = _stored_key(chosen) if chosen else keys.default_key()
        if key is None:
            raise SshKeyError("Quak has no SSH key yet. Run `quak ssh key generate` or `quak ssh key import`")
        return await self._change_camera_keys(profile, add=key)

    async def rotate_key(
        self, address: str, old_key_id: str, new_key_id: str, *, profile: SshProfile | None = None
    ) -> KeyInstallResult:
        """Replace one stored key with another, removing the old one only once the new one has logged in.

        The saved profile follows if it was logging in with the old key. Safe to run again after a
        lost connection: a rotation that already landed just proves the new key again.
        """
        profile = profile or self.profile(address)
        old, new = _stored_key(old_key_id), _stored_key(new_key_id)
        if keys.has_public_key(old.public_key, new.public_key):
            raise SshKeyError(f"{old.id} and {new.id} are the same key, so this would remove it. Pick a different one")
        result = await self._change_camera_keys(profile, add=new, remove=old)
        result.message += self._follow_rotation(address, old, new)
        return result

    async def revoke_key(self, address: str, key_id: str, *, profile: SshProfile | None = None) -> KeyInstallResult:
        """Remove a stored key from the camera. Refuses to remove the last one unless a password can still log in."""
        profile = profile or self.profile(address)
        return await self._change_camera_keys(profile, remove=_stored_key(key_id))

    async def _change_camera_keys(
        self, profile: SshProfile, *, add: SshKey | None = None, remove: SshKey | None = None
    ) -> KeyInstallResult:
        """Add and/or remove a key by its blob. It all lands and reads back, or the files go back as they were."""
        login = _batch_key_path(profile)
        login_key = _login_key(profile)
        new_file = _private_file(add) if add else None
        async with self._connected(profile) as endpoint:
            await self._verified_host_key(endpoint)
            files = _CameraKeyFiles(self._ssh, profile, endpoint, login)
            await files.load()
            card, runtime = _text(files.card or b""), _text(files.runtime or b"")
            # The card decides. With no file on it, the runtime copy is all there is to start from.
            base = card if files.card is not None else runtime
            with_new = keys.add_public_key(base, add.public_key) if add else base
            final = keys.remove_public_key(with_new, remove.public_key) if remove else with_new
            adding = add is not None and not (
                keys.has_public_key(card, add.public_key) and keys.has_public_key(runtime, add.public_key)
            )
            removing = remove is not None and (
                keys.has_public_key(card, remove.public_key) or keys.has_public_key(runtime, remove.public_key)
            )
            notes = []
            if adding or removing:
                if removing and keys.key_count(final) == 0:
                    await self._refuse_lockout(files, remove)
                first = with_new if add else final
                _refuse_dropping_login(login_key, runtime, first, add, remove)
                try:
                    await files.publish(first)
                    if add:
                        # A fresh connection with this key alone, while the old key is still there to fall back on.
                        await self._prove_login(profile, endpoint, add, new_file)
                        files.login = new_file  # the old key's login ends partway through the next step
                        await files.publish(final)
                except Exception as exc:
                    note = await files.roll_back()
                    if not isinstance(exc, QuakError):
                        raise
                    raise SshKeyError(f"{exc}. {note}") from None
                gone = keys.authorized_blobs(runtime) - keys.authorized_blobs(final)
                if remove:
                    gone -= keys.authorized_blobs(remove.public_key)
                if gone:
                    notes.append(f"{len(gone)} other key(s) only in the runtime copy are gone: the card decides.")
            elif add:
                await self._prove_login(profile, endpoint, add, new_file)
        if add:
            notes.insert(0, f"{add.fingerprint} {'is now' if adding else 'was already'} on the camera and logs in.")
        if remove:
            notes.append(f"{remove.fingerprint} {'is removed' if removing else 'was not on the camera'}.")
        if removing and not add and login_key and keys.has_public_key(login_key.public_key, remove.public_key):
            notes.append("Quak was logging in with that key: point the profile at another one.")
        return KeyInstallResult(
            installed=[add.fingerprint] if adding else [],
            already=[add.fingerprint] if add and not adding else [],
            removed=[remove.fingerprint] if removing else [],
            verified=True,
            message=" ".join(notes),
        )

    async def _refuse_lockout(self, files: _CameraKeyFiles, key: SshKey) -> None:
        mode = await files.auth_mode()
        if mode in _LAST_KEY_REMOVABLE:
            return
        why = "only accepts keys" if mode == "keyonly" else "may only accept keys (its login mode could not be read)"
        raise SshKeyError(
            f"{key.fingerprint} is the last key on the camera, which {why}, so removing it could lock you out. "
            "Install another key first. Nothing was changed"
        )

    async def _prove_login(self, profile: SshProfile, endpoint: Endpoint, key: SshKey, path: Path | None) -> None:
        try:
            result = await asyncio.to_thread(self._ssh.test, profile, endpoint, path)
        except NetworkError as exc:
            raise SshKeyError(f"could not log in with {key.fingerprint}: {exc}") from None
        if not result.ok:
            reason = result.error or "no reason given"
            raise SshKeyError(f"the camera did not accept a login with {key.fingerprint}: {reason}")

    def _follow_rotation(self, address: str, old: SshKey, new: SshKey) -> str:
        """Point the saved profile at the new key if it logged in with the old one, which no longer works."""
        try:
            saved = self.profile(address)
            current = _login_key(saved)
            if current is not None and keys.has_public_key(current.public_key, old.public_key):
                profiles.save(replace(saved, key_id=new.id))
        except (SshError, ValueError, OSError):
            return f" Quak's saved profile could not be updated: point it at {new.id}."
        return ""

    async def _resolve(self, profile: SshProfile, *, probe_custom_host: bool = True) -> Endpoint:
        return await asyncio.to_thread(
            transport.resolve,
            profile,
            probe_custom_host=probe_custom_host,
            reachable=self._reachable,
            find_nets=self._find_nets,
        )

    @contextlib.asynccontextmanager
    async def _connected(self, profile: SshProfile, *, probe_custom_host: bool = True) -> AsyncIterator[Endpoint]:
        """The endpoint to use inside the block, with the camera's Wi-Fi held throughout if that's the route."""
        endpoint = await self._resolve(profile, probe_custom_host=probe_custom_host)
        if endpoint.transport == "wifi":
            async with self._network_context(profile):
                yield endpoint
        else:
            yield endpoint

    def _answers_http(self, endpoint: Endpoint) -> bool:
        return any(self._reachable(endpoint.host, port) for port in (endpoint.http_port, _USB_HTTP_PORT))

    async def _scan_host(self, endpoint: Endpoint) -> HostKey:
        try:
            if self._scan_async is not None:
                return await self._scan_async(endpoint.host, endpoint.ssh_port)
            return await asyncio.to_thread(self._scan, endpoint.host, endpoint.ssh_port)
        except OSError as exc:  # hostkeys.scan's missing tool, timeout and no-key failures are all OSErrors
            raise SshError(
                f"could not read the SSH host key of {endpoint.host}:{endpoint.ssh_port}: {exc}. "
                "Check that the camera is reachable, and run `quak ssh arm` if SSH is not up"
            ) from None

    async def _verified_host_key(self, endpoint: Endpoint) -> HostKey:
        """Scan the camera and refuse anything but its pinned key, before any login is attempted."""
        seen = await self._scan_host(endpoint)
        state = hostkeys.state(seen.host, seen.port, seen)
        if state == "unknown":
            raise HostKeyError(
                f"the SSH host key of {seen.host}:{seen.port} is not trusted yet: {seen.key_type} {seen.fingerprint}. "
                "Check the fingerprint, then run `quak ssh host trust`"
            )
        if state == "changed":
            raise _changed_error(seen)
        # A pin can exist without a usable known_hosts line (the app only stores a fingerprint), and
        # ssh's strict checking reads that file. The scan just matched the pin, so the line is safe to write.
        # A fingerprint-only scan has no line to write, and the Dart terminal doesn't read the file.
        if seen.line:
            listed = hostkeys.lookup_known_hosts(seen.host, seen.port)
            if listed is None or listed.fingerprint != seen.fingerprint:
                hostkeys.add_host_key(seen)
        return seen

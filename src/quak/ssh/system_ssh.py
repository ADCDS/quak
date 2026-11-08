"""The command-line SSH transport: the system OpenSSH client, ssh-keygen and ssh-keyscan.

The app uses the Flutter SSH client instead, but both transports share profiles, keys, host
key pinning and the camera-side key-file logic.

Keys are the default. A call given ``password=`` logs in with that password instead, answering ssh's
prompt through a throwaway ``SSH_ASKPASS`` helper, so the password is only ever in ssh's environment.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from quak.core.errors import NetworkError
from quak.ssh import hostkeys
from quak.ssh.models import Endpoint, ExecResult, HostKey, SshProfile, TestResult

_ASKPASS_SCRIPT = "#!/bin/sh\nprintf '%s\\n' \"$QUAK_SSH_PASSWORD\"\n"


def ssh_binary() -> str:
    return shutil.which("ssh") or "ssh"


def _base_args(
    profile: SshProfile,
    endpoint: Endpoint,
    key_path: Path | None,
    *,
    batch: bool = True,
    tty: bool = False,
    password_login: bool = False,
) -> list[str]:
    args = [
        ssh_binary(),
        "-p",
        str(endpoint.ssh_port),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"UserKnownHostsFile={hostkeys.known_hosts_path()}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if password_login:
        # In place of the key and batch options: ssh keeps the first value it sees for an option, and
        # PasswordAuthentication=no or BatchMode=yes would end this login before it asks for the password.
        args += [
            "-o",
            "BatchMode=no",
            "-o",
            "PreferredAuthentications=password,keyboard-interactive",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
        ]
    else:
        if key_path:
            args += ["-i", str(key_path), "-o", "PasswordAuthentication=no"]
        if batch:
            args += ["-o", "BatchMode=yes"]
    if tty:
        args += ["-tt"]
    args.append(f"{profile.user}@{endpoint.host}")
    return args


@contextlib.contextmanager
def _askpass_env(password: str) -> Iterator[dict[str, str]]:
    """The environment for one ssh run that gets its password from a helper, which is removed afterwards.

    The helper holds no secret. It prints what this environment hands it, so the password is never
    written to disk or put on a command line.
    """
    fd, path = tempfile.mkstemp(prefix="quak-askpass-")
    try:
        with os.fdopen(fd, "w") as helper:
            helper.write(_ASKPASS_SCRIPT)
        os.chmod(path, 0o700)
        yield {**os.environ, "SSH_ASKPASS": path, "SSH_ASKPASS_REQUIRE": "force", "QUAK_SSH_PASSWORD": password}
    finally:
        Path(path).unlink(missing_ok=True)


def _run(
    args: list[str],
    *,
    input: bytes | None = None,
    timeout: float = 60,
    password: str | None = None,
) -> subprocess.CompletedProcess:
    try:
        if password is None:
            return subprocess.run(args, input=input, capture_output=True, timeout=timeout)
        with _askpass_env(password) as env:
            return subprocess.run(
                args,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,  # with input, stdin is the pipe that carries it
                capture_output=True,
                timeout=timeout,
                env=env,
                start_new_session=True,  # no controlling terminal for ssh to prompt on
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkError(f"{args[0]}: {exc}") from None


def test(
    profile: SshProfile,
    endpoint: Endpoint,
    key_path: Path | None,
    *,
    timeout: float = 20,
    password: str | None = None,
) -> TestResult:
    args = _base_args(profile, endpoint, key_path, batch=True, password_login=password is not None)
    proc = _run(args + ["id; uname -srm"], timeout=timeout, password=password)
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode:
        return TestResult(ok=False, error=stderr or stdout or f"ssh exited {proc.returncode}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    uid = lines[0] if lines else ""
    uname = lines[1] if len(lines) > 1 else ""
    return TestResult(ok=True, uid=uid, uname=uname)


def exec_command(
    profile: SshProfile,
    endpoint: Endpoint,
    key_path: Path | None,
    command: str,
    *,
    input: bytes | None = None,
    timeout: float = 120,
    password: str | None = None,
) -> ExecResult:
    args = _base_args(profile, endpoint, key_path, batch=True, password_login=password is not None)
    proc = _run(args + [command], input=input, timeout=timeout, password=password)
    return ExecResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def read_file(
    profile: SshProfile,
    endpoint: Endpoint,
    key_path: Path | None,
    remote: str,
    *,
    password: str | None = None,
) -> bytes:
    result = exec_command(profile, endpoint, key_path, f"cat -- {shlex.quote(remote)}", password=password)
    if result.exit_code:
        raise NetworkError(result.stderr.decode("utf-8", errors="replace").strip() or f"cat {remote} failed")
    return result.stdout


def write_file(
    profile: SshProfile,
    endpoint: Endpoint,
    key_path: Path | None,
    remote: str,
    data: bytes,
    *,
    mode: str = "600",
    password: str | None = None,
) -> None:
    quoted = shlex.quote(remote)
    command = f"umask 077; cat > {quoted} && chmod {mode} {quoted}"
    result = exec_command(profile, endpoint, key_path, command, input=data, password=password)
    if result.exit_code:
        raise NetworkError(result.stderr.decode("utf-8", errors="replace").strip() or f"write {remote} failed")


def open_terminal(profile: SshProfile, endpoint: Endpoint, key_path: Path | None) -> int:
    """Run the system ssh in the current terminal and return its exit code."""
    args = _base_args(profile, endpoint, key_path, batch=False, tty=True)
    try:
        return subprocess.run(args).returncode
    except OSError as exc:
        raise NetworkError(f"ssh: {exc}") from None


def scan_host_key(host: str, port: int) -> HostKey:
    return hostkeys.scan(host, port)

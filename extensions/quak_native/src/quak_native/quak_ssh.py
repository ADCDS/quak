"""Python side of the QuakSsh service (Dart side: src/flutter/quak_native/lib/src/quak_ssh_service.dart)."""

from typing import Any, Optional

import flet as ft


@ft.control("QuakSsh")
class QuakSsh(ft.Service):
    """SSH on the phone: host key scans, key generation, and one-off logins.

    ``test`` and ``exec`` log in only to a server that offers ``expected_fingerprint`` (SHA256:<base64>, unpadded,
    as quak.ssh.hostkeys has it), and refuse to start without one. Each call is its own connection, closed before it
    returns. A password or a private key, with its passphrase, logs in; with neither it's ``none`` authentication.
    """

    async def scan_host_key(self, host: str, port: int = 22) -> dict[str, str]:
        """{key_type, fingerprint}. Hangs up at the server's host key, before authentication."""
        return await self._invoke_method("scan_host_key", {"host": host, "port": port}, timeout=45)

    async def generate_key(self, comment: str = "", passphrase: Optional[str] = None) -> dict[str, str]:
        """{private_pem, public_key, fingerprint} for a new Ed25519 key, encrypted if a passphrase is given."""
        try:
            return await self._invoke_method("generate_key", {"comment": comment, "passphrase": passphrase}, timeout=60)
        except TimeoutError:
            # from None: Flet's message quotes the arguments, passphrase included.
            raise TimeoutError("generating the SSH key timed out") from None

    async def fingerprint(self, public_key_line: str) -> str:
        """SHA256 fingerprint, without padding, of a `<type> <base64 blob> [comment]` line."""
        return await self._invoke_method("fingerprint", {"public_key_line": public_key_line}, timeout=15)

    async def test(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        *,
        password: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        passphrase: Optional[str] = None,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """{ok, uid, uname, error}: logs in and runs ``id; uname -srm``. Every failure, a host key mismatch
        included, comes back in ``error`` rather than raising."""
        login = _login(host, port, username, password, private_key_pem, passphrase, expected_fingerprint)
        try:
            # Past every Dart-side limit: key decoding, connect 10 s, handshake 20 s, login 30 s, command 20 s.
            return await self._invoke_method("test", login, timeout=120)
        except TimeoutError:
            # from None: Flet's message quotes the arguments, the password and the private key included.
            raise TimeoutError("testing the SSH login timed out") from None

    async def exec(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        *,
        command: str,
        password: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        passphrase: Optional[str] = None,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """{exit_code, stdout, stderr} of one remote command, its output decoded as UTF-8 with any invalid byte
        replaced. ``exit_code`` is None when the server reported none. A failure to log in, or a command that runs
        past two minutes, raises RuntimeError with what went wrong."""
        login = _login(host, port, username, password, private_key_pem, passphrase, expected_fingerprint)
        try:
            # Past every Dart-side limit: key decoding, connect 10 s, handshake 20 s, login 30 s, command 120 s.
            return await self._invoke_method("exec", {**login, "command": command}, timeout=240)
        except TimeoutError:
            # from None: Flet's message quotes the arguments, the password and the private key included.
            raise TimeoutError("running the SSH command timed out") from None


def _login(
    host: str,
    port: int,
    username: str,
    password: Optional[str],
    private_key_pem: Optional[str],
    passphrase: Optional[str],
    expected_fingerprint: str,
) -> dict[str, Any]:
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "private_key_pem": private_key_pem,
        "passphrase": passphrase,
        "expected_fingerprint": expected_fingerprint,
    }

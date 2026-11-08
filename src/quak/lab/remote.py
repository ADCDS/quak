"""How the Lab runs things on the camera: a small ``Remote`` protocol and its SSH implementation.

The Lab takes any ``Remote``, so tests hand it a fake. ``SshRemote`` runs on a route that
``SshManager.route`` holds open, and its calls block. Make them from a worker thread, because a route
over Wi-Fi keeps a BLE session alive on the event loop::

    async with ssh_remote(manager, address) as remote:
        result = await asyncio.to_thread(hostmode.enable, remote)
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol

from quak.core.errors import NetworkError
from quak.ssh.models import Endpoint, ExecResult, SshProfile

if TYPE_CHECKING:
    from quak.ssh.manager import SshManager, SshRoute

DEFAULT_TIMEOUT = 120.0
_SSH_FAILED = 255  # what ssh itself exits with when the connection or the login fails


class Remote(Protocol):
    """Commands and files on the camera. A link that fails raises ``NetworkError``, as does an unreadable file."""

    def exec(self, command: str, input: bytes | None = None, timeout: float = DEFAULT_TIMEOUT) -> ExecResult:
        """Run one shell command. A non-zero exit from the command itself is in the result, not raised."""
        ...

    def read_file(self, path: str) -> bytes: ...

    def write_file(self, path: str, data: bytes) -> None: ...


class SshRemote:
    """A ``Remote`` over the route's SSH transport: the system OpenSSH client, or whatever a test injected."""

    def __init__(self, route: SshRoute) -> None:
        self._route = route

    @property
    def endpoint(self) -> Endpoint:
        """Where this link goes, for ``hostmode.requires_network_switch``."""
        return self._route.endpoint

    def exec(self, command: str, input: bytes | None = None, timeout: float = DEFAULT_TIMEOUT) -> ExecResult:
        route = self._route
        result = route.ssh.exec_command(
            route.profile, route.endpoint, route.key_path, command, input=input, timeout=timeout, **route.login_options
        )
        if result.exit_code == _SSH_FAILED:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise NetworkError(detail or "ssh lost the connection to the camera")
        return result

    def read_file(self, path: str) -> bytes:
        route = self._route
        return route.ssh.read_file(route.profile, route.endpoint, route.key_path, path, **route.login_options)

    def write_file(self, path: str, data: bytes) -> None:
        route = self._route
        route.ssh.write_file(route.profile, route.endpoint, route.key_path, path, data, **route.login_options)


@contextlib.asynccontextmanager
async def ssh_remote(
    manager: SshManager, address: str, *, profile: SshProfile | None = None, password: str | None = None
) -> AsyncIterator[SshRemote]:
    """A ``Remote`` on this camera for the length of the block: one Wi-Fi join and one host-key check.

    ``password`` is the login of a profile with ``auth=password``. Any other profile doesn't use it.
    """
    async with manager.route(address, profile=profile, password=password) as route:
        yield SshRemote(route)

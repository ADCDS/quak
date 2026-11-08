"""The Camera Lab as one object, for the CLI and, later, the app.

Each action holds its own route to the camera for as long as it takes, so a ``LabService`` keeps no connection: one
action is one Wi-Fi join and one host-key check. The Lab's functions block, and a route over Wi-Fi keeps a BLE session
alive on the event loop, so they run on a worker thread.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from quak.core.errors import HostModeError, LabError
from quak.lab import devices, hostmode, recording, toolkit
from quak.lab.capabilities import audio, display, network, serial, storage, system
from quak.lab.capabilities.audio import Recording
from quak.lab.capabilities.storage import CopyResult, DirEntry
from quak.lab.models import (
    AudioDevice,
    Framebuffer,
    HostModeResult,
    LabStatus,
    NetworkInterface,
    PackState,
    SerialPort,
    UsbDevice,
    Volume,
)
from quak.lab.remote import Remote, SshRemote, ssh_remote
from quak.ssh.models import SshProfile

if TYPE_CHECKING:
    from quak.ssh.manager import SshManager

T = TypeVar("T")
RemoteFactory = Callable[..., AbstractAsyncContextManager[SshRemote]]


def _core_modules() -> list[str]:
    """The bundled pack's modules in load order, which is how ``hostmode`` wants them for unloading in reverse."""
    return [module.name for module in toolkit.load_order(toolkit.core_manifest())]


def _status(remote: Remote) -> LabStatus:
    """What the camera says, over one route. ``battery`` stays empty: nothing in the Lab reads it yet."""
    status = LabStatus(host_mode=hostmode.state(remote), toolkit=toolkit.toolkit_state(remote))
    try:
        status.devices = devices.list_devices(remote)
    except LabError as exc:
        status.error = str(exc)
    return status


def _host_on(remote: SshRemote) -> HostModeResult:
    if hostmode.requires_network_switch(remote.endpoint):
        raise HostModeError(
            "this connection runs through the camera's USB port, which host mode takes away, leaving nothing to reach "
            "the camera through. Connect over Wi-Fi or Ethernet first. Nothing was changed"
        )
    return hostmode.enable(remote)


def _host_off(remote: Remote) -> HostModeResult:
    return hostmode.disable(remote, modules=_core_modules())


def _host_recover(remote: Remote) -> HostModeResult:
    return hostmode.recover(remote, modules=_core_modules())


class LabService:
    """The Lab's actions on one camera. A lost link raises ``NetworkError``; the rest raise ``LabError`` or a subclass."""

    def __init__(
        self,
        manager: SshManager,
        address: str,
        profile: SshProfile | None = None,
        remote_factory: RemoteFactory = ssh_remote,
        password: str | None = None,
    ) -> None:
        self._manager = manager
        self._address = address
        self._profile = profile
        self._remote_factory = remote_factory
        self._password = password

    async def _run(self, work: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """``work(remote, ...)`` on a worker thread, with a route held open until it returns."""
        login = {} if self._password is None else {"password": self._password}  # a key login's calls stay as they were
        async with self._remote_factory(self._manager, self._address, profile=self._profile, **login) as remote:
            return await asyncio.to_thread(work, remote, *args, **kwargs)

    async def status(self) -> LabStatus:
        """The port, the bus and the toolkit. Changes nothing, and a device list that fails is in ``error``."""
        return await self._run(_status)

    async def host_on(self) -> HostModeResult:
        """Switch the port to host mode. Over the camera's own USB-NCM it refuses, before sending the camera anything."""
        return await self._run(_host_on)

    async def host_off(self) -> HostModeResult:
        """Unmount Lab volumes, unload the pack's modules and put the port back in device mode."""
        return await self._run(_host_off)

    async def host_recover(self) -> HostModeResult:
        """Finish what an interrupted switch left."""
        return await self._run(_host_recover)

    async def devices(self) -> list[UsbDevice]:
        return await self._run(devices.list_devices)

    async def toolkit_status(self) -> PackState:
        """What the card says is installed. This doesn't ask the kernel, so ``loaded`` stays False."""
        return await self._run(toolkit.toolkit_state)

    async def toolkit_install(self) -> PackState:
        return await self._run(toolkit.install_core)

    async def toolkit_install_pack(self, pack_dir: str, allowed_signers: str) -> PackState:
        """Install a signed pack from this computer. The signature is checked here, before the camera is touched."""
        return await self._run(toolkit.install_pack, pack_dir, allowed_signers)

    async def toolkit_install_pack_url(self, url: str, sha256: str, allowed_signers: str) -> PackState:
        """Download a signed pack from an https:// or file:// URL, hold it to ``sha256``, then install it as above."""
        return await self._run(toolkit.install_pack_url, url, sha256, allowed_signers)

    async def toolkit_load(self) -> PackState:
        return await self._run(toolkit.load_core)

    async def toolkit_unload(self) -> PackState:
        return await self._run(toolkit.unload_core)

    async def toolkit_remove(self) -> PackState:
        return await self._run(toolkit.remove_core)

    async def storage_list(self) -> list[Volume]:
        return await self._run(storage.list_volumes)

    async def storage_mount(self, device: str, mountpoint: str | None = None, read_only: bool = True) -> Volume:
        return await self._run(storage.mount, device, mountpoint, read_only)

    async def storage_unmount(self, mountpoint: str) -> None:
        await self._run(storage.unmount, mountpoint)

    async def storage_list_dir(self, path: str) -> list[DirEntry]:
        return await self._run(storage.list_dir, path)

    async def storage_copy(self, src: str, dst: str, *, timeout: float = storage.COPY_TIMEOUT) -> CopyResult:
        return await self._run(storage.copy, src, dst, timeout=timeout)

    async def storage_eject(self, mountpoint: str) -> None:
        await self._run(storage.eject, mountpoint)

    async def audio_list(self) -> list[AudioDevice]:
        return await self._run(audio.list_cards)

    async def audio_record(self, card: int, seconds: int) -> Recording:
        """Capture to the camera's RAM. This holds the route for as long as the capture takes."""
        return await self._run(audio.record, card, seconds)

    async def audio_copy(self, src: str, dest: str) -> CopyResult:
        return await self._run(audio.copy_to_sd, src, dest)

    async def audio_save(self, card: int, seconds: int, dest_dir: str = audio.SAVE_DIR) -> dict[str, object]:
        """Capture and keep it on the SD card, all over one route. Gives the ``path`` it ended up at, and ``size``."""
        return await self._run(audio.save, card, seconds, dest_dir)

    async def audio_merge(
        self,
        clip: str | os.PathLike[str],
        wav: str | os.PathLike[str],
        out: str | os.PathLike[str],
        offset_seconds: float = 0.0,
        mix: bool = False,
    ) -> Path:
        """Merge files on this computer. The camera has no part in it, so this opens no route to it."""
        return await asyncio.to_thread(recording.merge, clip, wav, out, offset_seconds, mix)

    async def network_list(self) -> list[NetworkInterface]:
        return await self._run(network.list_interfaces)

    async def network_configure(
        self,
        name: str,
        dhcp: bool = True,
        address: str | None = None,
        prefix: int = 24,
        gateway: str | None = None,
        dns: str | Sequence[str] | None = None,
    ) -> dict[str, object]:
        """Acts on the interface it is told to. If this route runs over it, ``NetworkError`` can follow a success."""
        return await self._run(network.configure, name, dhcp, address, prefix, gateway, dns)

    async def display_list(self) -> list[Framebuffer]:
        return await self._run(display.list_framebuffers)

    async def serial_list(self) -> list[SerialPort]:
        return await self._run(serial.list_ports)

    async def system_status(self) -> dict[str, Any]:
        return await self._run(system.status)

    async def system_logs(self, tail: int = 200) -> str:
        return await self._run(system.logs, tail)

    async def system_processes(self) -> str:
        return await self._run(system.processes)

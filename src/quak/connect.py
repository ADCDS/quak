"""Open a camera session: the real camera over BLE, or the simulator."""

from __future__ import annotations

import contextlib
import socket
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from quak.camera import Camera
from quak.config import default_address, sim_state_file
from quak.core.errors import NotFound
from quak.i18n import tr
from quak.session import Session, Trace


@dataclass
class Target:
    address: str | None = None
    sim: bool = False
    hci: str = "hci0"
    retries: int = 3
    scan_timeout: float = 45.0  # a HERO11 can advertise as rarely as every 10-20 s; the scan ends at the first advert
    connect_timeout: float = 20.0
    wait_pairing: bool = False
    client_name: str = field(default_factory=socket.gethostname)
    log: Callable[[str], None] = lambda s: None
    trace: Callable[[Trace], None] | None = None
    keepalive: float | None = 3.0
    sim_camera: object | None = None  # an in-process SimCamera to reuse (tests, GUI)
    link_factory: Callable[["Target", str], object] | None = None  # platform link (e.g. the phone's bridge)


@contextlib.asynccontextmanager
async def open_camera(t: Target) -> AsyncIterator[Camera]:
    sim_cam, save_sim = None, False
    if t.sim:
        from quak.transport.sim import SimCamera, SimLink

        sim_cam = t.sim_camera
        if sim_cam is None:
            sim_cam, save_sim = SimCamera.load_or_new(sim_state_file()), True
            if t.wait_pairing:
                sim_cam.pairing_mode = True
        link = SimLink(sim_cam)
    else:
        address = t.address or default_address()
        if not address:
            raise NotFound(tr("no camera paired yet: open the camera's pairing screen and run `quak pair`"))
    if not t.sim and t.link_factory:
        link = t.link_factory(t, address)
    elif not t.sim:
        from quak.transport.ble import BleakLink

        link = BleakLink(
            address,
            hci=t.hci,
            scan_timeout=t.scan_timeout,
            connect_timeout=t.connect_timeout,
            retries=t.retries,
            wait_pairing=t.wait_pairing,
            log=t.log,
        )
    new_bond = await link.connect()
    session = Session(link, trace=t.trace, keepalive_interval=t.keepalive)
    try:
        await session.start(new_bond=new_bond, client_name=t.client_name)
        cam = Camera(session)
        cam.new_bond = new_bond
        cam.link = link
        yield cam
    finally:
        await session.close()
        if save_sim:
            sim_cam.save(sim_state_file())
            await sim_cam.close()

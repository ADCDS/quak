"""The pairing wizard."""

from __future__ import annotations

import asyncio

import flet as ft

from quak.gui import kit
from quak.gui.screens.common import _centered, _header, _scroll
from quak.i18n import N_, term, tr, trn


def add_camera(app) -> ft.Control:
    t, step = app.t, app.add["step"]
    bars = {"instructions": 1, "scanning": 2, "error": 2, "pairing": 3, "done": 3}[step]
    progress = ft.Row(
        [
            ft.Container(height=4, expand=True, border_radius=2, bgcolor=t.accent if i < bars else t.line)
            for i in range(3)
        ],
        spacing=6,
    )
    body = [_header(app, tr("Add camera"), back="cameras"), progress]
    footer: list[ft.Control] = []
    device = (app.add.get("device") or {}).get("name")
    if step == "instructions":
        bold = ft.TextStyle(weight=ft.FontWeight.W_600)
        before, after = tr("Swipe down, then left, and tap {path}.").split("{path}")
        steps = [
            [ft.TextSpan(tr("Turn the camera on."))],
            [
                ft.TextSpan(before),
                ft.TextSpan(tr("Preferences › Connections › Connect Device › GoPro Quik App"), style=bold),
                ft.TextSpan(after),
            ],
            [ft.TextSpan(tr("Leave that screen open until Quak says it's done."))],
        ]
        body += [
            kit.title(tr("Open the pairing screen on the camera"), t, 26),
            kit.text(
                tr("A HERO11 ignores devices it doesn't know until this screen is open. You only do this once."),
                t,
                15,
                t.text2,
            ),
        ]
        body += [
            kit.card(
                ft.Row(
                    [
                        ft.Container(
                            ft.Text(str(i + 1), size=14, weight=ft.FontWeight.W_600, color=t.text),
                            width=28,
                            height=28,
                            border_radius=14,
                            bgcolor=t.surface2,
                            alignment=ft.Alignment.CENTER,
                        ),
                        ft.Text(spans=spans, size=15, color=t.text, expand=True),
                    ],
                    spacing=14,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                t,
                padding=14,
            )
            for i, spans in enumerate(steps)
        ]
        footer = [kit.primary(tr("Start scanning"), lambda e: app.run(_scan, app), t, height=52)]
    elif step == "scanning":
        scanning, found = app.add.get("scanning"), app.add["found"]
        body.append(
            kit.title(
                tr("Looking for cameras") if scanning else tr("Pick your camera") if found else tr("No camera found"),
                t,
                26,
            )
        )
        if scanning:
            body.append(_radar(t))
        if found:
            body.append(kit.label(tr("Found nearby"), t))
        elif not scanning:
            seen = app.add.get("seen")
            body += [
                kit.text(
                    (
                        trn(
                            "Saw {n} Bluetooth device nearby; none was a GoPro.",
                            "Saw {n} Bluetooth devices nearby; none was a GoPro.",
                            seen,
                        )
                        + " "
                        if seen is not None
                        else ""
                    )
                    + tr(
                        "Check that the camera is on and close by. A camera that is connected to another "
                        "phone or computer stops advertising, so disconnect it there first."
                    ),
                    t,
                    15,
                    t.text2,
                )
            ]
        for dev in found:
            ready = dev["pairing"]
            body.append(
                kit.card(
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    kit.text(
                                        term(dev["name"]),
                                        t,
                                        16,
                                        weight=ft.FontWeight.W_600,
                                        color=t.text if ready else t.text2,
                                    ),
                                    kit.text(
                                        tr("HERO11 Black · pairing screen open")
                                        if ready
                                        else tr("HERO11 Black · not in pairing mode"),
                                        t,
                                        13,
                                        t.text2 if ready else t.text3,
                                    ),
                                ],
                                spacing=3,
                                expand=True,
                            ),
                            kit.primary(
                                tr("Pair"),
                                lambda e, d=dev: app.run(_pair, app, d),
                                t,
                                height=44,
                                text_size=15,
                                radius=12,
                            )
                            if ready
                            else kit.text(tr("Open its pairing screen"), t, 13, t.text3),
                        ]
                    ),
                    t,
                    padding=14,
                )
            )
        if not scanning:
            footer = [kit.secondary(tr("Scan again"), lambda e: app.run(_scan, app), t, height=52)]
    elif step == "pairing":

        def item(caption, state, note=None):
            mark = (
                ft.Container(
                    ft.Icon(ft.Icons.CHECK, color=t.ok, size=16),
                    width=26,
                    height=26,
                    border_radius=13,
                    bgcolor=t.ok_tint,
                    alignment=ft.Alignment.CENTER,
                )
                if state == "done"
                else ft.Container(
                    width=26,
                    height=26,
                    border_radius=13,
                    border=ft.Border.all(2, t.accent if state == "now" else t.line),
                )
            )
            row = [mark, ft.Text(caption, size=15, color=t.text if state != "todo" else t.text3, expand=True)]
            if note:
                row.append(ft.Text(note, size=13, color=t.text3))
            return ft.Container(ft.Row(row, spacing=12), padding=16)

        phase = app.add.get("phase", 0)
        names = [
            (tr("Connected"), None),
            (tr("Bonded"), tr("no code needed")),
            (tr("Telling the camera pairing is finished"), None),
            (tr("Syncing the camera clock"), None),
        ]
        body += [
            kit.title(tr("Pairing with {name}", name=term(device)) if device else tr("Pairing with the camera"), t, 26),
            kit.text(tr("Keep the camera's pairing screen open."), t, 15, t.text2),
            kit.card(
                ft.Column(
                    [
                        item(n, "done" if i < phase else "now" if i == phase else "todo", note)
                        for i, (n, note) in enumerate(names)
                    ],
                    spacing=2,
                ),
                t,
                padding=0,
            ),
        ]
    elif step == "done":
        return _centered(
            app,
            body,
            [
                ft.Container(
                    ft.Icon(ft.Icons.CHECK, color=t.ok, size=44),
                    width=96,
                    height=96,
                    border_radius=48,
                    bgcolor=t.ok_tint,
                    alignment=ft.Alignment.CENTER,
                ),
                kit.title(
                    tr("Paired with {name}", name=term(device)) if device else tr("Paired with the camera"),
                    t,
                    26,
                    text_align=ft.TextAlign.CENTER,
                ),
                kit.text(
                    tr("You can close the camera's pairing screen. Next time Quak connects without it."),
                    t,
                    15,
                    t.text2,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
            [kit.primary(tr("Open remote"), lambda e: app.go("remote"), t, height=52)],
        )
    else:
        before, after = tr(
            "It was seen, but its pairing screen is closed, so it ignored the connection. "
            "Open {path} on the camera and try again."
        ).split("{path}")
        body += [
            kit.card(
                ft.Column(
                    [
                        kit.title(tr("The camera isn't accepting new devices"), t, 20, t.danger_text),
                        kit.text(app.add["error"], t, 15, t.danger_body)
                        if app.add["error"]
                        else ft.Text(
                            spans=[
                                ft.TextSpan(before),
                                ft.TextSpan(
                                    tr("Connect Device › GoPro Quik App"),
                                    style=ft.TextStyle(weight=ft.FontWeight.W_600),
                                ),
                                ft.TextSpan(after),
                            ],
                            size=15,
                            color=t.danger_body,
                        ),
                    ],
                    spacing=14,
                ),
                t,
                padding=18,
                bg=t.danger_tint,
            ),
            kit.card(
                ft.Column(
                    [
                        kit.text(tr("Other things to check"), t, 13, t.text2, ft.FontWeight.W_600),
                        kit.text(
                            tr(
                                "Bluetooth is on · only one app is connected to the camera · the camera isn't recording"
                            ),
                            t,
                            14,
                            t.text2,
                        ),
                    ],
                    spacing=6,
                ),
                t,
                padding=14,
            ),
        ]
        footer = [kit.primary(tr("Try again"), lambda e: app.run(_scan, app), t, height=52)]
    return _scroll(body, padding=20, spacing=18, footer=footer)


def _radar(t) -> ft.Control:
    ring = lambda size, color, inner: ft.Container(
        inner,
        width=size,
        height=size,
        border_radius=size / 2,  # noqa: E731
        border=ft.Border.all(1, color),
        alignment=ft.Alignment.CENTER,
    )
    core = ft.Container(
        ft.Icon(ft.Icons.BLUETOOTH, color=t.accent, size=26),
        width=56,
        height=56,
        border_radius=28,
        bgcolor=t.accent_tint,
        alignment=ft.Alignment.CENTER,
    )
    return ft.Container(ring(168, t.line, ring(112, t.ring2, core)), height=180, alignment=ft.Alignment.CENTER)


async def _scan(app) -> None:
    app.add.update(step="scanning", found=[], error="", scanning=True, seen=None)
    app.render()
    try:
        await _scan_for(app)
    finally:
        app.add["scanning"] = False


async def _scan_for(app) -> None:
    if app.sim:
        sim = app.svc.target.sim_camera
        await asyncio.sleep(0.6)
        app.add["found"] = [
            {"address": "SIM", "name": N_("GoPro (simulated)"), "pairing": sim.pairing_mode or sim.bonded}
        ]
        return
    ble = getattr(app.svc, "ble", None)
    if ble is not None:  # phone: scan through the native bridge
        from quak.transport.android import gopros_in

        await ble.permissions()
        devices = await ble.scan(8)
        app.add["seen"] = len(devices)
        app.add["found"] = [
            {
                "address": d["address"],
                "name": d["name"] or d["address"],
                "pairing": bool(d["advert"] and d["advert"].pairing_mode),
            }
            for d in gopros_in(devices)
        ]
        return
    from bleak import BleakScanner

    from quak.core import codec

    found: dict[str, dict] = {}

    def seen(dev, adv):
        app.add["seen"] = len(scanner.discovered_devices) if scanner else None
        mfr = adv.manufacturer_data.get(codec.GOPRO_COMPANY_ID)
        info = codec.decode_advert(mfr) if mfr else None
        if info:
            found[dev.address] = {
                "address": dev.address,
                "name": dev.name or adv.local_name or dev.address,
                "pairing": info.pairing_mode,
            }
            app.add["found"] = list(found.values())
            app.render_soon()

    scanner = None
    scanner = BleakScanner(seen)
    await scanner.start()
    await asyncio.sleep(8)
    await scanner.stop()


async def _pair(app, dev: dict) -> None:
    app.add.update(step="pairing", device=dev, phase=0)
    app.render()
    try:
        await app.svc.disconnect()
        if not app.sim:
            app.svc.target.address, app.svc.target.wait_pairing = dev["address"], True
        await app.connect()
        app.svc.target.wait_pairing = False
        app.add["step"] = "done"
    except Exception as e:
        app.add.update(step="error", error=str(e))
        raise

"""The home screen: the camera, its state, quick actions."""

from __future__ import annotations

import flet as ft

from quak import config
from quak.core import registry as R
from quak.core.registry import Status
from quak.gui import kit
from quak.gui import theme as th
from quak.gui.screens.common import _camera_name, _fw, _hms, _offset, _scroll, _short_duration
from quak.i18n import tr


def cameras(app) -> ft.Control:
    t, st = app.t, app.svc.state
    add = kit.icon_button(ft.Icons.ADD, tr("Add camera"), lambda e: app.go("add_camera"), t, bgcolor=t.surface2)
    if app.wide:  # the rail already carries the mark
        body = [ft.Row([kit.title(tr("Your cameras"), t), add], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)]
    else:
        body = [
            ft.Container(
                ft.Row(
                    [ft.Row([kit.mark(32), kit.title("Quak", t, 22)], spacing=10), add],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                padding=ft.Padding.symmetric(horizontal=4),
            ),
            kit.page_title(tr("Your cameras"), t),
        ]
    note = R.model_note(st.model)
    if note:
        body.append(kit.banner(note[0].upper() + note[1:], t, "warn"))
    if st.link == "ble":
        body += _camera_card(app) + _quick_actions(app)
    elif st.link == "connecting":
        body.append(
            kit.card(
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Container(
                                    ft.Icon(ft.Icons.PHOTO_CAMERA_OUTLINED, color=t.text2, size=30),
                                    width=56,
                                    height=56,
                                    border_radius=16,
                                    bgcolor=t.surface2,
                                    alignment=ft.Alignment.CENTER,
                                ),
                                ft.Column(
                                    [
                                        kit.title(st.model or "HERO11 Black", t, 18),
                                        kit.text(tr("Connecting over Bluetooth…"), t, 14, t.text2),
                                    ],
                                    spacing=2,
                                ),
                            ],
                            spacing=14,
                        ),
                        ft.ProgressBar(color=t.accent, bgcolor=t.surface2, bar_height=6, border_radius=3),
                        kit.text(
                            tr(
                                "The camera has to be on. If headphones are streaming on this device, scans can be slow."
                            ),
                            t,
                            14,
                            t.text2,
                        ),
                    ],
                    spacing=16,
                ),
                t,
                padding=20,
                radius=th.R_SECTION,
            )
        )
    elif app.sim or config.default_address():
        name = tr("Simulated HERO11") if app.sim else (config.default_address() or "")
        body.append(
            kit.card(
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Container(
                                    ft.Icon(ft.Icons.PHOTO_CAMERA_OUTLINED, color=t.text2, size=30),
                                    width=56,
                                    height=56,
                                    border_radius=16,
                                    bgcolor=t.surface2,
                                    alignment=ft.Alignment.CENTER,
                                ),
                                ft.Column(
                                    [kit.title(st.model or "HERO11 Black", t, 18), kit.text(name, t, 13, t.text3)],
                                    spacing=2,
                                ),
                            ],
                            spacing=14,
                        ),
                        *([kit.banner(st.error, t, "danger")] if st.error else []),
                        kit.primary(tr("Connect"), lambda e: app.run(app.connect), t, expand=False),
                    ],
                    spacing=14,
                ),
                t,
                padding=20,
                radius=th.R_SECTION,
            )
        )
    else:
        body.append(
            ft.Container(
                ft.Column(
                    [
                        ft.Container(
                            ft.Icon(ft.Icons.PHOTO_CAMERA_OUTLINED, color=t.text2, size=48),
                            width=104,
                            height=104,
                            border_radius=52,
                            bgcolor=t.surface,
                            alignment=ft.Alignment.CENTER,
                        ),
                        kit.title(tr("No cameras yet"), t, 22),
                        kit.text(
                            tr("Pair a HERO11 once. After that, Quak finds it and connects on its own."),
                            t,
                            15,
                            t.text2,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        kit.primary(tr("Add camera"), lambda e: app.go("add_camera"), t),
                        ft.TextButton(
                            tr("How pairing works"),
                            on_click=lambda e: _how_pairing(app),
                            style=ft.ButtonStyle(color=t.accent),
                        ),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=16,
                ),
                padding=24,
                alignment=ft.Alignment.CENTER,
            )
        )
    return _scroll(body, spacing=20)


def _how_pairing(app) -> None:
    t = app.t
    app.dialog(
        tr("How pairing works"),
        ft.Text(
            tr(
                "The camera only talks to devices it knows. Pairing introduces this device once, while the camera's "
                "Connect Device › GoPro Quik App screen is open. After that Quak connects over Bluetooth whenever the "
                "camera is on, and joins the camera's Wi-Fi only for preview and media."
            ),
            color=t.text2,
        ),
        [ft.TextButton(tr("OK"), on_click=lambda e: app.close_dialog())],
    )


def _camera_card(app) -> list[ft.Control]:
    t, st = app.t, app.svc.state
    sched = st.schedule
    offset = st.clock_offset
    pills = ft.Row(
        [
            kit.pill("Bluetooth", t, dot=t.ok, bg=t.pill_bg),
            kit.pill(
                tr("Wi-Fi on") if st.http else tr("Wi-Fi off"),
                t,
                dot=t.ok if st.http else t.muted,
                bg=t.pill_bg,
                fg=t.text if st.http else t.text2,
            ),
        ],
        spacing=6,
    )
    visual = ft.Container(
        ft.Stack(
            [
                ft.Container(
                    ft.Icon(ft.Icons.PHOTO_CAMERA_OUTLINED, color=t.muted, size=56),
                    alignment=ft.Alignment.CENTER,
                    expand=True,
                ),
                ft.Container(pills, left=10, top=10),
            ]
        ),
        height=132,
        border_radius=14,
        bgcolor=t.surface2,
    )
    name = _camera_name(app)
    items = [
        visual,
        ft.Column(
            [
                kit.title(st.model, t, 20),
                kit.text(
                    tr("{name} · firmware {version}", name=name, version=_fw(st.firmware))
                    if name
                    else tr("firmware {version}", version=_fw(st.firmware)),
                    t,
                    14,
                    t.text2,
                ),
            ],
            spacing=2,
        ),
        ft.Row(
            [
                kit.stat(tr("Battery"), f"{st.s(Status.BATTERY_PERCENT, '?')}%", t),
                kit.stat(tr("SD card"), _hms(st.s(Status.REMAINING_VIDEO)), t),
                kit.stat(tr("Clock"), _offset(offset), t),
            ],
            spacing=8,
        ),
    ]
    if sched["armed"]:
        items.append(
            kit.banner(
                tr(
                    "Scheduled {time} today · {duration} video",
                    time=sched["time"],
                    duration=_short_duration(sched["duration"]),
                ),
                t,
                icon=ft.Icons.SCHEDULE,
                on_click=lambda e: app.go("schedule"),
                key="schedule-banner",
            )
        )
    items.append(
        ft.Row(
            [
                kit.primary(tr("Open remote"), lambda e: app.go("remote"), t, expand=True, text_size=15),
                kit.secondary(
                    tr("Sync clock"),
                    lambda e: app.run(app.svc.sync_clock, ok=tr("Clock synced")),
                    t,
                    text_size=15,
                    weight=ft.FontWeight.W_500,
                ),
            ],
            spacing=10,
        )
    )
    return [kit.card(ft.Column(items, spacing=14), t, radius=th.R_SECTION)]


def _quick_actions(app) -> list[ft.Control]:
    t, st = app.t, app.svc.state

    def tile(glyph, caption, on_click):
        return ft.Container(
            ft.Column(
                [glyph, ft.Text(caption, size=13, color=t.text)],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=8,
            ),
            height=84,
            expand=True,
            bgcolor=t.surface,
            border_radius=16,
            alignment=ft.Alignment.CENTER,
            border=ft.Border.all(1, t.card_border) if t.card_border else None,
            on_click=on_click,
            ink=True,
            tooltip=caption,
        )

    def icon(name):
        return ft.Icon(name, color=t.text, size=22)

    rec = st.recording
    return [
        ft.Column(
            [
                kit.section(tr("Quick actions"), t),
                ft.Row(
                    [
                        tile(
                            kit.record_icon(t, rec),
                            tr("Stop") if rec else tr("Record"),
                            lambda e: app.run(app.svc.shutter, not rec),
                        ),
                        tile(
                            icon(ft.Icons.FLAG_OUTLINED),
                            "HiLight",
                            lambda e: app.run(app.svc.hilight, ok=tr("HiLight tagged")),
                        ),
                        tile(icon(ft.Icons.SCHEDULE), tr("Schedule"), lambda e: app.go("schedule")),
                        tile(icon(ft.Icons.BEDTIME_OUTLINED), tr("Sleep"), lambda e: _confirm_sleep(app)),
                    ],
                    spacing=8,
                ),
            ],
            spacing=10,
        )
    ]


def _confirm_sleep(app) -> None:
    t = app.t

    def yes(e):
        app.close_dialog()
        app.run(app.svc.sleep_camera, ok=tr("Camera going to sleep"))

    app.dialog(
        tr("Put the camera to sleep?"),
        ft.Text(tr("Quak disconnects. Wake the camera to connect again."), color=t.text2),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()), ft.TextButton(tr("Sleep"), on_click=yes)],
    )

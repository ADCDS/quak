"""Scheduled capture."""

from __future__ import annotations

import datetime as dt

import flet as ft

from quak.core import registry as R
from quak.core.registry import Status
from quak.gui import kit
from quak.gui import theme as th
from quak.gui.screens.cameras import _confirm_sleep
from quak.gui.screens.common import DURATIONS, _header, _hms, _offset, _scroll, _short_duration
from quak.gui.screens.remote import _preset_name


def schedule(app) -> ft.Control:
    t, st = app.t, app.svc.state
    if not st.connected:
        return _scroll([_header(app, "Scheduled capture", back="cameras"), kit.banner("Not connected.", t, "warn")])
    f = app.form.setdefault("schedule", {})
    if "when" in f and f["when"] <= dt.datetime.now() + dt.timedelta(seconds=30):
        del f["when"]  # a time that has passed (or is about to) isn't useful
    if "when" not in f:
        f.update(when=R.next_schedule(dt.datetime.now(), 120), duration="15s", preset="video", sleep=False)
    sched = st.schedule
    body = [_header(app, "Scheduled capture", back="cameras")]
    if sched["armed"]:
        now = dt.datetime.now()
        h, m = map(int, sched["time"].split(":"))
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if at < now:
            at += dt.timedelta(days=1)
        synced = st.clock_offset is not None and abs(st.clock_offset) < 2
        body += [
            kit.card(
                ft.Column(
                    [
                        kit.pill("ARMED", t, bg=t.accent, fg=t.on_accent, bold=True),
                        kit.title(sched["time"], t, 56, color=t.armed_time),
                        app.live_text(
                            lambda: (
                                f"{'Today' if at.date() == dt.date.today() else 'Tomorrow'} · "
                                f"starts in {_hms((at - dt.datetime.now()).total_seconds())}"
                            ),
                            size=16,
                            color=t.accent_ink,
                        ),
                    ],
                    spacing=6,
                ),
                t,
                padding=20,
                radius=th.R_SECTION,
                bg=t.accent_tint,
            ),
            kit.card(
                ft.Column(
                    [
                        kit.setting_row(
                            "Preset", f"Video · {_preset_name(app, int(sched['preset'] or 0))}", t, chevron=False
                        ),
                        kit.setting_row("Record for", _short_duration(sched["duration"]), t, chevron=False),
                        kit.setting_row(
                            "Camera clock",
                            "Synced" if synced else _offset(st.clock_offset),
                            t,
                            chevron=False,
                            value_color=t.ok if synced else t.warn,
                        ),
                        kit.setting_row(
                            "Camera", f"On · {st.s(Status.BATTERY_PERCENT, '?')}% battery", t, last=True, chevron=False
                        ),
                    ],
                    spacing=0,
                ),
                t,
                padding=0,
            ),
            kit.text("You can close Quak. The camera starts and stops the recording by itself.", t, 14, t.text2),
        ]
        footer = [
            ft.Row(
                [
                    kit.destructive(
                        "Disarm",
                        lambda e: app.run(app.svc.disarm_schedule, ok="Schedule off"),
                        t,
                        expand=True,
                        outlined=True,
                        height=52,
                    ),
                    kit.secondary("Sleep camera", lambda e: _confirm_sleep(app), t, expand=True, height=52),
                ],
                spacing=10,
            )
        ]
        return _scroll(body, spacing=14, footer=footer)

    def bump(minutes: int):
        f["when"] = f["when"] + dt.timedelta(minutes=minutes)
        if f["when"] <= dt.datetime.now():
            f["when"] += dt.timedelta(days=1)
        if f["when"] - dt.datetime.now() > dt.timedelta(hours=24):
            f["when"] -= dt.timedelta(days=1)
        app.render()

    def spinner(value: str, up, down, name):
        return ft.Column(
            [
                kit.icon_button(
                    ft.Icons.KEYBOARD_ARROW_UP, f"{name} up", up, t, color=t.text2, height=32, icon_size=20
                ),
                kit.title(value, t, 64, style=ft.TextStyle(height=1.0)),
                kit.icon_button(
                    ft.Icons.KEYBOARD_ARROW_DOWN, f"{name} down", down, t, color=t.text2, height=32, icon_size=20
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=0,
        )

    when = f["when"]
    colon = ft.Column(
        [
            ft.Container(width=8, height=8, border_radius=4, bgcolor=t.muted),
            ft.Container(width=8, height=8, border_radius=4, bgcolor=t.muted),
        ],
        spacing=14,
    )
    body.append(
        kit.card(
            ft.Column(
                [
                    ft.Row(
                        [
                            spinner(f"{when:%H}", lambda e: bump(60), lambda e: bump(-60), "Hour"),
                            colon,
                            spinner(f"{when:%M}", lambda e: bump(1), lambda e: bump(-1), "Minute"),
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=8,
                    ),
                    app.live_text(
                        lambda: (
                            f"{'Today' if when.date() == dt.date.today() else 'Tomorrow'} · in "
                            f"{_hms((when - dt.datetime.now()).total_seconds())} · up to 24 h ahead"
                        ),
                        size=14,
                        color=t.text2,
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            t,
            radius=th.R_SECTION,
        )
    )

    def set_duration(d):
        f["duration"] = d
        app.render()

    def no_limit(e):
        def yes(e):
            app.close_dialog()
            set_duration(None)

        app.dialog(
            "Record with no limit?",
            ft.Text("The camera records until the card or battery runs out.", color=t.text2),
            [ft.TextButton("Cancel", on_click=lambda e: app.close_dialog()), ft.TextButton("No limit", on_click=yes)],
        )

    chips = [
        kit.chip(
            d.replace("m", " min").replace("h", " h").replace("s", " s"),
            t,
            selected=f["duration"] == d,
            on_click=lambda e, d=d: set_duration(d),
            key=f"dur-{d}",
        )
        for d in DURATIONS
    ]
    chips.append(kit.chip("No limit…", t, selected=f["duration"] is None, dashed=True, on_click=no_limit))
    body += [kit.section("Record for", t), ft.Row(chips, wrap=True, spacing=8, run_spacing=8)]
    video_presets = next((g["preset_array"] for g in st.presets if g["id"] == 1000), [])

    def choose_preset(e):
        def pick_p(v):
            app.close_dialog()
            f["preset"] = v
            app.render()

        app.dialog(
            "Preset",
            ft.Column(
                [
                    ft.ListTile(
                        title=ft.Text("Video (current video preset)", color=t.text), on_click=lambda e: pick_p("video")
                    )
                ]
                + [
                    ft.ListTile(
                        title=ft.Text(_preset_name(app, p["id"]), color=t.text), on_click=lambda e, i=p["id"]: pick_p(i)
                    )
                    for p in video_presets
                ],
                tight=True,
            ),
            [ft.TextButton("Cancel", on_click=lambda e: app.close_dialog())],
        )

    if f["preset"] == "video":
        cur = int(st.s(Status.PRESET, 0))
        preset_label = f"Video · {_preset_name(app, cur)}" if R.preset_group_of(cur) == "video" else "Video · last used"
    else:
        preset_label = f"Video · {_preset_name(app, f['preset'])}"

    def toggle_sleep(e):
        f["sleep"] = e.control.value

    body.append(
        kit.card(
            ft.Column(
                [
                    kit.setting_row("Preset", preset_label, t, on_click=choose_preset),
                    kit.check_row(
                        "Sleep camera after arming",
                        f["sleep"],
                        toggle_sleep,
                        t,
                        sub="Saves battery until the capture",
                        key="sleep-after",
                    ),
                ],
                spacing=0,
            ),
            t,
            padding=0,
        )
    )
    offset = st.clock_offset
    body.append(
        kit.card(
            ft.Row(
                [
                    ft.Icon(ft.Icons.SYNC, color=t.text2),
                    ft.Column(
                        [
                            kit.text(
                                "Camera clock is "
                                + (
                                    "in sync"
                                    if offset is not None and abs(offset) < 1
                                    else f"{abs(offset or 0):.0f} s {'ahead' if (offset or 0) > 0 else 'behind'}"
                                ),
                                t,
                                15,
                            ),
                            kit.text("Quak syncs it when you arm", t, 12, t.text3),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    kit.secondary(
                        "Sync now",
                        lambda e: app.run(app.svc.sync_clock, ok="Clock synced"),
                        t,
                        height=40,
                        text_size=15,
                        weight=ft.FontWeight.W_500,
                    ),
                ],
                spacing=12,
            ),
            t,
        )
    )
    if st.settings.get(59) == 0:
        body.append(
            ft.Container(
                ft.Text(
                    spans=[
                        ft.TextSpan(
                            "Auto power off is set to Never, so the camera stays on after the capture until its battery "
                            "runs out. "
                        ),
                        ft.TextSpan(
                            "Change",
                            style=ft.TextStyle(decoration=ft.TextDecoration.UNDERLINE, color=t.accent_ink),
                            on_click=lambda e: app.go("settings"),
                        ),
                    ],
                    size=13,
                    color=t.warn_text,
                ),
                bgcolor=t.warn_tint,
                border_radius=12,
                padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            )
        )

    async def arm():
        r = await app.svc.arm_schedule(
            f["when"], f["duration"], preset=f["preset"], sleep_after=f["sleep"], allow_no_limit=f["duration"] is None
        )
        lasting = [w for w in r.warnings if "Duration" in w or "Pro controls" in w]  # Auto Power Off has its own banner
        app.toast(
            ". ".join([f"Armed for {r.at:%H:%M}" + (" · camera asleep" if r.slept else ""), *lasting]),
            duration=10000 if lasting else 4000,
        )
        if r.slept:
            app.go("cameras")

    return _scroll(
        body,
        spacing=14,
        footer=[kit.primary(f"Arm for {when:%H:%M}", lambda e: app.run(arm), t, height=54, text_size=17)],
    )

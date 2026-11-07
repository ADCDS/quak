"""Scheduled capture."""

from __future__ import annotations

import datetime as dt

import flet as ft

from quak import flows
from quak.core import registry as R
from quak.core.registry import Status
from quak.gui import kit
from quak.gui import theme as th
from quak.gui.screens.cameras import _confirm_sleep
from quak.gui.screens.common import DURATIONS, _header, _hms, _offset, _scroll, _short_duration
from quak.gui.screens.remote import _preset_name
from quak.i18n import term, tr


def _duration_chip(duration: str) -> str:
    """15s -> 15 s, 1m -> 1 min, 1h -> 1 h."""
    number, unit = duration[:-1], duration[-1]
    if unit == "s":
        return tr("{seconds} s", seconds=number)
    if unit == "m":
        return tr("{minutes} min", minutes=number)
    return tr("{hours} h", hours=number)


def schedule(app) -> ft.Control:
    t, st = app.t, app.svc.state
    if not st.connected:
        return _scroll(
            [_header(app, tr("Scheduled capture"), back="cameras"), kit.banner(tr("Not connected."), t, "warn")]
        )
    f = app.form.setdefault("schedule", {})
    if "when" in f and f["when"] <= dt.datetime.now() + dt.timedelta(seconds=30):
        del f["when"]  # a time that has passed (or is about to) isn't useful
    if "when" not in f:
        f.update(when=R.next_schedule(dt.datetime.now(), 120), duration="15s", preset="video", sleep=False)
    sched = st.schedule
    body = [_header(app, tr("Scheduled capture"), back="cameras")]
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
                        kit.pill(tr("ARMED"), t, bg=t.accent, fg=t.on_accent, bold=True),
                        kit.title(sched["time"], t, 56, color=t.armed_time),
                        app.live_text(
                            lambda: (
                                tr("Today · starts in {time}", time=_hms((at - dt.datetime.now()).total_seconds()))
                                if at.date() == dt.date.today()
                                else tr(
                                    "Tomorrow · starts in {time}", time=_hms((at - dt.datetime.now()).total_seconds())
                                )
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
                            tr("Preset"),
                            tr("Video · {preset}", preset=_preset_name(app, int(sched["preset"] or 0))),
                            t,
                            chevron=False,
                        ),
                        kit.setting_row(tr("Record for"), _short_duration(sched["duration"]), t, chevron=False),
                        kit.setting_row(
                            tr("Camera clock"),
                            tr("Synced") if synced else _offset(st.clock_offset),
                            t,
                            chevron=False,
                            value_color=t.ok if synced else t.warn,
                        ),
                        kit.setting_row(
                            tr("Camera"),
                            tr("On · {percent}% battery", percent=st.s(Status.BATTERY_PERCENT, "?")),
                            t,
                            last=True,
                            chevron=False,
                        ),
                    ],
                    spacing=0,
                ),
                t,
                padding=0,
            ),
            kit.text(tr("You can close Quak. The camera starts and stops the recording by itself."), t, 14, t.text2),
        ]
        footer = [
            ft.Row(
                [
                    kit.destructive(
                        tr("Disarm"),
                        lambda e: app.run(app.svc.disarm_schedule, ok=tr("Schedule off")),
                        t,
                        expand=True,
                        outlined=True,
                        height=52,
                    ),
                    kit.secondary(tr("Sleep camera"), lambda e: _confirm_sleep(app), t, expand=True, height=52),
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

    def spinner(value: str, up, down, up_label, down_label):
        return ft.Column(
            [
                kit.icon_button(ft.Icons.KEYBOARD_ARROW_UP, up_label, up, t, color=t.text2, height=32, icon_size=20),
                kit.title(value, t, 64, style=ft.TextStyle(height=1.0)),
                kit.icon_button(
                    ft.Icons.KEYBOARD_ARROW_DOWN, down_label, down, t, color=t.text2, height=32, icon_size=20
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
                            spinner(
                                f"{when:%H}", lambda e: bump(60), lambda e: bump(-60), tr("Hour up"), tr("Hour down")
                            ),
                            colon,
                            spinner(
                                f"{when:%M}", lambda e: bump(1), lambda e: bump(-1), tr("Minute up"), tr("Minute down")
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=8,
                    ),
                    app.live_text(
                        lambda: (
                            tr(
                                "Today · in {time} · up to 24 h ahead",
                                time=_hms((when - dt.datetime.now()).total_seconds()),
                            )
                            if when.date() == dt.date.today()
                            else tr(
                                "Tomorrow · in {time} · up to 24 h ahead",
                                time=_hms((when - dt.datetime.now()).total_seconds()),
                            )
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
            tr("Record with no limit?"),
            ft.Text(tr("The camera records until the card or battery runs out."), color=t.text2),
            [
                ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()),
                ft.TextButton(tr("No limit"), on_click=yes),
            ],
        )

    chips = [
        kit.chip(
            _duration_chip(d),
            t,
            selected=f["duration"] == d,
            on_click=lambda e, d=d: set_duration(d),
            key=f"dur-{d}",
        )
        for d in DURATIONS
    ]
    chips.append(kit.chip(tr("No limit…"), t, selected=f["duration"] is None, dashed=True, on_click=no_limit))
    body += [kit.section(tr("Record for"), t), ft.Row(chips, wrap=True, spacing=8, run_spacing=8)]
    video_presets = next((g["preset_array"] for g in st.presets if g["id"] == 1000), [])

    def choose_preset(e):
        def pick_p(v):
            app.close_dialog()
            f["preset"] = v
            app.render()

        app.dialog(
            tr("Preset"),
            ft.Column(
                [
                    ft.ListTile(
                        title=ft.Text(tr("Video (current video preset)"), color=t.text),
                        on_click=lambda e: pick_p("video"),
                    )
                ]
                + [
                    ft.ListTile(
                        title=ft.Text(_preset_name(app, p["id"]), color=t.text),
                        on_click=lambda e, i=p["id"]: pick_p(i),
                    )
                    for p in video_presets
                ],
                tight=True,
            ),
            [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog())],
        )

    if f["preset"] == "video":
        cur = int(st.s(Status.PRESET, 0))
        preset_label = (
            tr("Video · {preset}", preset=_preset_name(app, cur))
            if R.preset_group_of(cur) == "video"
            else tr("Video · last used")
        )
    else:
        preset_label = tr("Video · {preset}", preset=_preset_name(app, f["preset"]))

    def toggle_sleep(e):
        f["sleep"] = e.control.value

    body.append(
        kit.card(
            ft.Column(
                [
                    kit.setting_row(tr("Preset"), preset_label, t, on_click=choose_preset),
                    kit.check_row(
                        tr("Sleep camera after arming"),
                        f["sleep"],
                        toggle_sleep,
                        t,
                        sub=tr("Saves battery until the capture"),
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
    drift = f"{abs(offset or 0):.0f}"
    if offset is not None and abs(offset) < 1:
        clock_text = tr("Camera clock is in sync")
    elif (offset or 0) > 0:
        clock_text = tr("Camera clock is {seconds} s ahead", seconds=drift)
    else:
        clock_text = tr("Camera clock is {seconds} s behind", seconds=drift)
    body.append(
        kit.card(
            ft.Row(
                [
                    ft.Icon(ft.Icons.SYNC, color=t.text2),
                    ft.Column(
                        [
                            kit.text(clock_text, t, 15),
                            kit.text(tr("Quak syncs it when you arm"), t, 12, t.text3),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    kit.secondary(
                        tr("Sync now"),
                        lambda e: app.run(app.svc.sync_clock, ok=tr("Clock synced")),
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
                            tr(
                                "Auto power off is set to Never, so the camera stays on after the capture until its "
                                "battery runs out."
                            )
                            + " "
                        ),
                        ft.TextSpan(
                            tr("Change"),
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
        lasting = [  # Auto Power Off has its own banner
            w for w in r.warnings if w != term(flows.AUTO_POWER_OFF_NEVER)
        ]
        armed = (
            tr("Armed for {time} · camera asleep", time=f"{r.at:%H:%M}")
            if r.slept
            else tr("Armed for {time}", time=f"{r.at:%H:%M}")
        )
        app.toast(". ".join([armed, *lasting]), duration=10000 if lasting else 4000)
        if r.slept:
            app.go("cameras")

    return _scroll(
        body,
        spacing=14,
        footer=[
            kit.primary(tr("Arm for {time}", time=f"{when:%H:%M}"), lambda e: app.run(arm), t, height=54, text_size=17)
        ],
    )

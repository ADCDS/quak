"""Remote control: preview, shutter, modes and presets."""

from __future__ import annotations

import math
import time

import flet as ft

from quak.core import registry as R
from quak.core import tables
from quak.core.registry import Status
from quak.gui import kit
from quak.gui.screens.common import (
    GROUP_CHIPS,
    VIDEO_CHIPS,
    _camera_name,
    _elapsed,
    _header,
    _hms,
    _label,
    _offset,
    _scroll,
    _short_duration,
    _value,
    pick,
)
from quak.i18n import N_, term, tr, trn

GROUP_PRESET = {
    "video": N_("Video preset"),
    "photo": N_("Photo preset"),
    "timelapse": N_("Timelapse preset"),
    "other": N_("Other preset"),
}
GROUP_PRESETS = {
    "video": N_("Video presets"),
    "photo": N_("Photo presets"),
    "timelapse": N_("Timelapse presets"),
    "other": N_("Other presets"),
}
INSPECTOR_TITLES = {
    "video": N_("Video presets"),
    "photo": N_("Photo presets"),
    "timelapse": N_("Time lapse presets"),
    "other": N_("other presets"),
}


def _chip_label(app, sid: int) -> str:
    """Some values alone would be unclear on a chip: "Low" or "1 Minute" of what?"""
    value = _value(app, sid)
    if sid == R.VIDEO_DURATION or sid == R.MULTI_SHOT_DURATION:
        return tr("Duration {value}", value=value)
    return tr("HyperSmooth {value}", value=value) if sid == 135 else value


def remote(app) -> ft.Control:
    t, st = app.t, app.svc.state
    if not st.connected:
        return _scroll(
            [
                _header(app, tr("Remote")),
                kit.banner(
                    tr("Not connected. Connect from the Cameras tab."), t, "warn", on_click=lambda e: app.go("cameras")
                ),
            ]
        )
    group = R.preset_group_of(int(st.s(Status.PRESET, 0)))
    preset_name = _preset_name(app, int(st.s(Status.PRESET, 0)))
    tabs = kit.segmented(
        [("video", tr("Video")), ("photo", tr("Photo")), ("timelapse", tr("Time Lapse"))],
        group,
        lambda g: app.run(app.svc.load_group, g),
        t,
        height=46,
        key="mode",
        accent=True,
    )
    rec = st.recording
    elapsed = time.time() - st.encoding_since if rec and st.encoding_since else 0
    clock = f"{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}"
    if app.wide:
        return _remote_desktop(app, group, tabs, rec, clock)
    status = ft.Row(
        [
            ft.Icon(ft.Icons.BATTERY_5_BAR, color=t.text2, size=18, rotate=ft.Rotate(angle=math.pi / 2)),
            kit.text(f"{st.s(Status.BATTERY_PERCENT, '?')}%", t, 13, t.text2),
            kit.text(_hms(st.s(Status.REMAINING_VIDEO)), t, 13, t.text2),
            ft.Icon(ft.Icons.BLUETOOTH, color=t.ok, size=18, tooltip=tr("Bluetooth connected")),
            ft.Icon(
                ft.Icons.WIFI,
                color=t.ok if st.http else t.muted,
                size=18,
                tooltip=tr("Wi-Fi connected") if st.http else tr("Wi-Fi off"),
            ),
        ],
        spacing=8,
    )
    back = ft.Container(
        ft.Row([ft.Icon(ft.Icons.CHEVRON_LEFT, color=t.text, size=28), kit.title(st.model, t, 18)], spacing=0),
        on_click=lambda e: app.go("cameras"),
        ink=True,
        border_radius=12,
        padding=ft.Padding.only(right=8, top=6, bottom=6),
        tooltip=tr("Back"),
    )
    header = ft.Row([back, status], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    preset_btn = ft.Container(
        ft.Row(
            [
                ft.Column(
                    [
                        kit.text(preset_name, t, 16, weight=ft.FontWeight.W_600),
                        kit.text(
                            " · ".join(
                                (
                                    term(GROUP_PRESET[group]),
                                    tr("Pro controls") if st.settings.get(175) else tr("Easy controls"),
                                )
                            ),
                            t,
                            13,
                            t.text2,
                        ),
                    ],
                    spacing=2,
                    expand=True,
                ),
                ft.Icon(ft.Icons.EXPAND_MORE, color=t.text),
            ]
        ),
        bgcolor=t.surface,
        border_radius=14,
        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
        on_click=lambda e: _preset_sheet(app, group),
        ink=True,
        tooltip=tr("Choose preset"),
    )
    chips = ft.Row(
        [
            kit.chip(
                _chip_label(app, sid),
                t,
                on_click=lambda e, s=sid: pick(app, s),
                key=f"chip-{sid}",
            )
            for sid in GROUP_CHIPS.get(group, VIDEO_CHIPS)
            if st.caps.get(sid)
        ],
        wrap=True,
        spacing=8,
        run_spacing=8,
    )
    duration = st.settings.get(156, 100)
    if rec:
        hint = (
            tr("Recording {clock} · stops by itself at {duration}", clock=clock, duration=_short_duration(duration))
            if duration != 100
            else tr("Recording {clock}", clock=clock)
        )
    elif group == "photo":
        photos = st.s(Status.REMAINING_PHOTOS)
        hint = tr("? photos left") if photos is None else trn("{n} photo left", "{n} photos left", photos)
    else:
        hint = tr("{time} of video left", time=_hms(st.s(Status.REMAINING_VIDEO)))
    controls = ft.Row(
        [
            kit.round_icon(
                ft.Icons.FLAG_OUTLINED,
                lambda e: app.run(app.svc.hilight, ok=tr("HiLight tagged")),
                t,
                tr("Tag a HiLight"),
                key="hilight",
            ),
            kit.shutter(
                rec, lambda e: app.run(app.svc.shutter, not rec), t, disabled=bool(st.busy_action == "shutter")
            ),
            kit.round_icon(ft.Icons.TUNE, lambda e: app.go("settings"), t, tr("Camera settings"), key="settings"),
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        spacing=40,
    )
    hint_text = (
        app.live_text(
            lambda: (
                tr(
                    "Recording {clock} · stops by itself at {duration}",
                    clock=_elapsed(app),
                    duration=_short_duration(duration),
                )
                if duration != 100
                else tr("Recording {clock}", clock=_elapsed(app))
            ),
            size=13,
            color=t.text2,
            key="shutter-hint",
        )
        if rec
        else ft.Text(hint, size=13, color=t.text2, key="shutter-hint")
    )
    hint_row = ft.Row([hint_text], alignment=ft.MainAxisAlignment.CENTER)
    return _scroll(
        [header, _preview(app), tabs, preset_btn, chips], spacing=12, padding=12, footer=[controls, hint_row]
    )


def _remote_desktop(app, group: str, tabs: ft.Control, rec: bool, clock: str) -> ft.Control:
    """Desktop Remote (design: Desktop · Remote): header with status pills, big preview, one control row, inspector."""
    t, st = app.t, app.svc.state
    offset = st.clock_offset
    synced = offset is not None and abs(offset) < 2
    name = _camera_name(app)
    link = tr("Bluetooth and Wi-Fi connected") if st.http else tr("Bluetooth connected")
    header = ft.Row(
        [
            ft.Column(
                [kit.title(st.model, t, 24), kit.text(f"{name + ' · ' if name else ''}{link}", t, 14, t.text2)],
                spacing=2,
                expand=True,
            ),
            ft.Row(
                [
                    _status_pill(t, tr("Battery {percent}%", percent=st.s(Status.BATTERY_PERCENT, "?"))),
                    _status_pill(t, tr("SD {time}", time=_hms(st.s(Status.REMAINING_VIDEO)))),
                    _status_pill(
                        t,
                        tr("Clock synced") if synced else tr("Clock {offset}", offset=_offset(offset)),
                        dot=t.ok if synced else t.warn,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
        ]
    )
    hint_text = (
        app.live_text(
            lambda: tr("Recording {clock} · Space to stop", clock=_elapsed(app)),
            size=14,
            color=t.text2,
            key="shutter-hint",
        )
        if rec
        else ft.Text(tr("Space to record · H for HiLight"), size=14, color=t.text2, key="shutter-hint")
    )
    controls = ft.Row(
        [
            ft.Container(tabs, width=330),
            ft.Row(
                [
                    kit.round_icon(
                        ft.Icons.FLAG_OUTLINED,
                        lambda e: app.run(app.svc.hilight, ok=tr("HiLight tagged")),
                        t,
                        tr("Tag a HiLight"),
                        size=52,
                        key="hilight",
                    ),
                    kit.shutter(rec, lambda e: app.run(app.svc.shutter, not rec), t, size=80),
                    hint_text,
                ],
                spacing=20,
                tight=True,
            ),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
    )
    left = ft.Column(
        [_preview(app, radius=18), controls],
        spacing=16,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    inspector = ft.Container(_inspector(app), width=300)
    return ft.Container(
        ft.Column(
            [header, ft.Row([left, inspector], spacing=20, vertical_alignment=ft.CrossAxisAlignment.START)],
            spacing=20,
            scroll=ft.ScrollMode.AUTO,
        ),
        padding=ft.Padding.only(left=24, right=24, top=20, bottom=24),
        expand=True,
    )


def _status_pill(t, text: str, dot: str | None = None) -> ft.Container:
    row = ([ft.Container(width=7, height=7, border_radius=4, bgcolor=dot)] if dot else []) + [
        ft.Text(text, size=13, color=t.text)
    ]
    return ft.Container(
        ft.Row(row, spacing=6, tight=True),
        height=32,
        border_radius=16,
        bgcolor=t.surface,
        padding=ft.Padding.symmetric(horizontal=12),
        alignment=ft.Alignment.CENTER,
    )


def _preview(app, radius: int = 16) -> ft.Control:
    t, st, pv = app.t, app.svc.state, app.preview
    pv["image"] = None
    if pv["on"] and pv.get("player"):  # phones: the stream plays in a video player (no ffmpeg there)
        if pv.get("video") is None:
            import flet_video as fv

            pv["video"] = fv.Video(
                playlist=[fv.VideoMedia(pv["player"])],
                autoplay=True,
                muted=True,
                controls=None,
                fit=ft.BoxFit.COVER,
                fill_color="#22303F",
                wakelock=True,
                expand=True,
            )
        content = pv["video"]
    elif pv["on"] and pv["frame"]:
        content = ft.Image(src=pv["frame"], fit=ft.BoxFit.COVER, gapless_playback=True, expand=True)
        pv["image"] = content
    elif pv["on"] and st.recording:  # it can't start until the recording ends: the caption below says so
        content = None
    elif pv["on"]:
        content = ft.Column(
            [ft.ProgressRing(color=t.accent), kit.text(tr("Starting preview…"), t, 14, t.text2)],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        )
    else:
        content = ft.Container(
            ft.Column(
                [
                    ft.Icon(ft.Icons.WIFI, color=t.text2, size=30),
                    kit.text(tr("Preview uses the camera's Wi-Fi"), t, 16, weight=ft.FontWeight.W_600),
                    kit.text(
                        term(pv["error"])
                        or tr("Controls keep working over Bluetooth. Preview drains the battery faster."),
                        t,
                        13,
                        t.text2,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    kit.secondary(
                        tr("Start preview"), lambda e: app.run(app.start_preview), t, height=40, text_size=14, radius=12
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
            ),
            padding=ft.Padding.symmetric(horizontal=28),
        )
    overlays = []
    if st.recording and pv["on"]:  # the camera can't stream while it records; its preview returns afterwards
        overlays.append(
            ft.Container(
                ft.Container(
                    kit.text(tr("Preview pauses while recording"), t, 13, "#FFFFFF"),
                    bgcolor="#BF0B0D10",
                    border_radius=14,
                    padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                ),
                alignment=ft.Alignment.CENTER,
                left=0,
                top=0,
                right=0,
                bottom=0,
            )
        )
    if st.recording:
        rec_text = app.live_text(
            lambda: tr("REC {clock}", clock=_elapsed(app)), size=12, color="#FFFFFF", weight=ft.FontWeight.W_700
        )
        overlays.append(
            ft.Container(
                ft.Container(
                    ft.Row(
                        [ft.Container(width=7, height=7, border_radius=4, bgcolor="#FFFFFF"), rec_text],
                        spacing=6,
                        tight=True,
                    ),
                    bgcolor=t.rec,
                    border_radius=14,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                ),
                left=14,
                top=14,
            )
        )
    elif pv["on"] and (pv["frame"] or pv.get("player")):  # only once frames are coming
        overlays.append(
            ft.Container(kit.pill(tr("LIVE PREVIEW"), t, dot=t.ok, bg="#B80B0D10", bold=True), left=14, top=14)
        )
    if pv["on"]:
        group = R.preset_group_of(int(st.s(Status.PRESET, 0)))
        fps = st.settings.get(3)
        summary = (
            f"{_value(app, 121)} · {_value(app, 2)} · {'—' if fps is None else R.value_label(3, fps)}"
            if group == "video"
            else " · ".join(_value(app, s) for s in GROUP_CHIPS.get(group, [])[:2])
        )
        overlays.append(ft.Container(kit.pill(summary, t, bg="#B80B0D10", fg="#D5DAE1"), right=14, bottom=14))
        if not st.recording:
            overlays.append(
                ft.Container(
                    ft.Container(
                        ft.Icon(ft.Icons.CLOSE, color=t.text, size=18),
                        width=32,
                        height=32,
                        border_radius=16,
                        bgcolor="#B80B0D10",
                        alignment=ft.Alignment.CENTER,
                        on_click=lambda e: app.run(app.stop_preview),
                        ink=True,
                        tooltip=tr("Stop preview"),
                    ),
                    right=12,
                    top=12,
                )
            )
    fill = ft.Container(
        content,
        left=0,
        top=0,
        right=0,
        bottom=0,
        alignment=None if pv["image"] is not None or pv.get("player") else ft.Alignment.CENTER,
    )
    return ft.Container(
        ft.Stack([fill, *overlays], expand=True),
        aspect_ratio=16 / 9,
        border_radius=radius,
        bgcolor="#22303F" if pv["on"] else t.surface,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
    )


def _inspector(app) -> ft.Control:
    t, st = app.t, app.svc.state
    group = R.preset_group_of(int(st.s(Status.PRESET, 0)))
    gid = R.PRESET_GROUPS[group] if group in R.PRESET_GROUPS else 1000
    presets = next((g["preset_array"] for g in st.presets if g["id"] == gid), [])
    cur = int(st.s(Status.PRESET, 0))
    rows = [ft.Container(kit.label(term(INSPECTOR_TITLES[group]), t), padding=ft.Padding.only(bottom=4))]
    for p in presets:
        on = p["id"] == cur
        rows.append(
            ft.Container(
                ft.Text(
                    _preset_name(app, p["id"]),
                    size=14,
                    color=t.accent_ink if on else t.text,
                    weight=ft.FontWeight.W_600 if on else None,
                ),
                bgcolor=t.accent_tint if on else None,
                border_radius=10,
                height=44,
                alignment=ft.Alignment.CENTER_LEFT,
                padding=ft.Padding.symmetric(horizontal=12),
                on_click=lambda e, i=p["id"]: app.run(app.svc.load_preset, i),
                ink=True,
            )
        )
    ids = [s for s in GROUP_CHIPS.get(group, VIDEO_CHIPS) if s in st.settings]
    table = [
        kit.table_row(
            _label(s),
            _value(app, s) if st.caps.get(s) else "—",
            t,
            value_width=84,
            on_click=(lambda e, s=s: pick(app, s)) if st.caps.get(s) else None,
            last=i == len(ids) - 1,
            key=f"inspect-{s}",
        )
        for i, s in enumerate(ids)
    ]
    table_card = kit.card(ft.Column(table, spacing=0), t)
    table_card.padding = ft.Padding.symmetric(horizontal=14, vertical=6)
    items = [
        kit.card(ft.Column(rows, spacing=6, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), t),
        table_card,
        ft.Row(
            [
                ft.TextButton(
                    tr("All settings"), on_click=lambda e: app.go("settings"), style=ft.ButtonStyle(color=t.accent)
                )
            ],
            alignment=ft.MainAxisAlignment.END,
        ),
    ]
    if st.schedule["armed"]:
        items.append(
            kit.banner(
                tr(
                    "Scheduled {time} · {duration}",
                    time=st.schedule["time"],
                    duration=_short_duration(st.schedule["duration"]),
                ),
                t,
                icon=ft.Icons.SCHEDULE,
                on_click=lambda e: app.go("schedule"),
            )
        )
    return ft.Column(items, spacing=14, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)


def _preset_name(app, preset_id: int) -> str:
    for g in app.svc.state.presets:
        for p in g["preset_array"]:
            if p["id"] == preset_id:
                return p.get("custom_name") or term(
                    tables.PRESET_TITLES.get(p.get("title_id"), tr("Preset {id}", id=f"{preset_id:#x}"))
                )
    return tr("Preset {id}", id=f"{preset_id:#x}")


def _preset_sheet(app, group: str) -> None:
    t, st = app.t, app.svc.state
    gid = R.PRESET_GROUPS.get(group, 1000)
    presets = next((g["preset_array"] for g in st.presets if g["id"] == gid), [])
    cur = int(st.s(Status.PRESET, 0))

    def choose(pid):
        app.close_dialog()
        app.run(app.svc.load_preset, pid, ok=tr("{preset} loaded", preset=_preset_name(app, pid)))

    app.dialog(
        term(GROUP_PRESETS[group]),
        ft.Column(
            [
                ft.ListTile(
                    title=ft.Text(_preset_name(app, p["id"]), color=t.accent if p["id"] == cur else t.text),
                    on_click=lambda e, i=p["id"]: choose(i),
                )
                for p in presets
            ],
            tight=True,
        ),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog())],
    )

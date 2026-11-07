"""Pieces several screens share."""

from __future__ import annotations

import re
import time

import flet as ft

from quak.core import registry as R
from quak.gui import kit
from quak.gui import theme as th

VIDEO_CHIPS = [2, 3, 121, 135, 156]

GROUP_CHIPS = {"video": VIDEO_CHIPS, "photo": [122, 125, 151], "timelapse": [5, 123, 179, 156, 157]}

SETTING_GROUPS = [
    ("Video", [2, 3, 121, 135, 150, 156, 167, 168, 180]),
    ("Photo", [122, 125, 151]),
    ("Time Lapse", [5, 30, 32, 123, 157, 179]),
    ("General", [83, 91, 88, 134, 128]),
    ("Power", [59]),
    ("Connections", [178]),
]

DURATIONS = ["15s", "30s", "1m", "5m", "15m", "1h", "3h"]

# friendly names, as in the design (the camera's own names are in tables.SETTINGS)
LABELS = {
    2: "Resolution",
    3: "Frame rate",
    121: "Lens",
    135: "HyperSmooth",
    150: "Horizon leveling",
    156: "Duration",
    167: "HindSight",
    168: "Scheduled capture",
    180: "Video mode",
    122: "Lens",
    125: "Output",
    151: "Horizon leveling",
    5: "Interval",
    30: "Interval",
    32: "Interval",
    123: "Lens",
    157: "Duration",
    179: "Star trails",
    83: "GPS",
    91: "LEDs",
    88: "Screen brightness",
    134: "Anti-flicker",
    128: "Media format",
    59: "Auto power off",
    178: "Camera Wi-Fi band",
    175: "Controls",
}

GROUP_TITLES = {"video": "Video", "photo": "Photo", "timelapse": "Time lapse"}

CHIP_PREFIX = {135: "HyperSmooth ", 156: "Duration ", 157: "Duration "}  # chips whose value alone would be unclear


def _scroll(controls, padding=16, spacing=16, footer: list | None = None) -> ft.Container:
    """A scrolling screen body; `footer` stays pinned at the bottom (primary actions, like the design)."""
    col = ft.Column(
        controls,
        spacing=spacing,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    if footer:
        col = ft.Column([col, *footer], spacing=12, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
    return ft.Container(col, padding=ft.Padding.symmetric(horizontal=padding, vertical=12), expand=True)


def _offset(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    n = round(seconds)
    return f"{n:+d} s" if n else "0 s"


def _header(app, title: str, back: str | None = None, right: ft.Control | None = None) -> ft.Row:
    t = app.t
    left = []
    if back:
        left.append(kit.icon_button(ft.Icons.CHEVRON_LEFT, "Back", lambda e: app.go(back), t))
    left.append(kit.title(title, t, th.TITLE))
    return ft.Row([ft.Row(left, spacing=4), right or ft.Container()], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)


def _value(app, sid: int) -> str:
    v = app.svc.state.settings.get(sid)
    if v is None:
        return "—"
    text = re.sub(r"(\d)(G?Hz)\b", r"\1 \2", R.value_label(sid, v))
    return f"{text} fps" if sid == 3 else text


def _hms(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    return f"{s // 3600} h {s % 3600 // 60} m" if s >= 3600 else f"{s // 60} m {s % 60} s" if s >= 60 else f"{s} s"


def pick(app, sid: int) -> None:
    """Choose a value for a setting from what the camera allows right now."""
    st, t = app.svc.state, app.t
    allowed = st.caps.get(sid, [])
    current = st.settings.get(sid)

    def choose(v):
        app.close_dialog()
        app.run(app.svc.set_setting, sid, v, ok=f"{_label(sid)} = {R.value_label(sid, v)}")

    options = [
        ft.ListTile(
            title=ft.Text(R.value_label(sid, v), color=t.accent if v == current else t.text),
            trailing=ft.Icon(ft.Icons.CHECK, color=t.accent) if v == current else None,
            on_click=lambda e, v=v: choose(v),
        )
        for v in allowed
    ]
    app.dialog(
        _label(sid),
        ft.Column(options, tight=True, scroll=ft.ScrollMode.AUTO, height=min(420, 56 * len(options))),
        [ft.TextButton("Cancel", on_click=lambda e: app.close_dialog())],
    )


def _reason(app, sid: int) -> str | None:
    st = app.svc.state
    if st.caps.get(sid):
        return None
    if sid in R.PRO_ONLY and st.settings.get(R.CONTROL_MODE) == 0:
        return "Needs Pro controls"
    return "Not available in this mode"


def _label(sid: int) -> str:
    return LABELS.get(sid) or R.setting_name(sid)


def _fw(firmware: str) -> str:
    """H22.01.02.20.00 -> 2.20 (what the camera shows in its menus)."""
    parts = (firmware or "").split(".")
    if len(parts) >= 4 and parts[2].isdigit():
        return f"{int(parts[2])}.{parts[3]}"
    return firmware or "?"


def _camera_name(app) -> str:
    return "Simulated" if app.sim else (app.svc.state.name or "")


def _centered(app, top: list, middle: list, footer: list) -> ft.Control:
    """Header, then content centred in the free space, then pinned actions (Add camera › done)."""
    return ft.Container(
        ft.Column(
            [
                *top,
                ft.Container(
                    ft.Column(middle, horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=16, tight=True),
                    expand=True,
                    alignment=ft.Alignment.CENTER,
                ),
                *footer,
            ],
            spacing=18,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
        padding=ft.Padding.symmetric(horizontal=16, vertical=12),
        expand=True,
    )


def _short_duration(value) -> str:
    """15 Seconds -> 15 s, 1 Minute -> 1 min, as the design writes durations."""
    text = R.value_label(156, value or 100)
    for long, short in (
        ("Seconds", "s"),
        ("Second", "s"),
        ("Minutes", "min"),
        ("Minute", "min"),
        ("Hours", "h"),
        ("Hour", "h"),
    ):
        text = text.replace(long, short)
    return text.replace("No Limit", "No limit")


def _elapsed(app) -> str:
    st = app.svc.state
    s = int(time.time() - st.encoding_since) if st.recording and st.encoding_since else 0
    return f"{s // 60:02d}:{s % 60:02d}"


def _size(n: int) -> str:
    """4.2 MB, 39 MB, 183.2 GB"""
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    return f"{n / 1e6:.1f} MB" if n < 10e6 else f"{n / 1e6:.0f} MB"

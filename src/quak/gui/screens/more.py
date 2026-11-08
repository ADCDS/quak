"""More: this camera, the app's preferences, the Camera Lab, SSH and Diagnostics."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import flet as ft

from quak import config, i18n
from quak.core import registry as R
from quak.core.registry import Status
from quak.gui import kit
from quak.gui import theme as th
from quak.gui.screens.cameras import _confirm_sleep
from quak.gui.screens.common import _camera_name, _fw, pick
from quak.gui.screens.lab import lab
from quak.gui.screens.ssh import ssh
from quak.i18n import N_, term, tr

LOG_KINDS = {
    "commands": (N_("command"), N_("command reply")),
    "settings": (N_("settings"), N_("settings reply")),
    "keepalive": (N_("keep-alive"), N_("keep-alive reply")),
    "queries": (N_("query"), N_("query reply")),
    "network": (N_("network"), N_("network")),
}


def more(app) -> ft.Control:
    t = app.t
    tabs = kit.segmented(
        [("camera", tr("Camera")), ("lab", tr("Lab")), ("ssh", tr("SSH")), ("diagnostics", tr("Diagnostics"))],
        app.more_tab,
        lambda v: app.go("more", more_tab=v),
        t,
        height=40,
        key="more-tab",
    )
    inner = {"camera": _more_camera, "lab": lab, "ssh": ssh, "diagnostics": _diagnostics}[app.more_tab](app)
    return ft.Container(
        ft.Column(
            [kit.page_title(tr("More"), t), tabs, inner],
            spacing=8,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        ),
        padding=ft.Padding.only(left=16, right=16, top=16, bottom=12),
        expand=True,
    )


def _more_camera(app) -> ft.Control:
    t, st = app.t, app.svc.state
    if not st.connected:
        return ft.Column(
            [
                kit.banner(tr("Not connected."), t, "warn", on_click=lambda e: app.go("cameras")),
                _demo_switch(app),
                _about(app),
            ],
            spacing=12,
        )
    offset = st.clock_offset
    clock = (
        "—"
        if offset is None
        else tr("in sync")
        if abs(offset) < 1
        else tr("{seconds} s ahead", seconds=f"{abs(offset):.0f}")
        if offset > 0
        else tr("{seconds} s behind", seconds=f"{abs(offset):.0f}")
    )
    tz = (
        tr("UTC{offset}", offset=f"{(st.tz_minutes or 0) / 60:+g}".replace("-", "−"))
        if st.tz_minutes is not None
        else ""
    )
    band = st.settings.get(178)
    bands = st.caps.get(178) or [0, 1]
    never = st.settings.get(59) == 0
    rows = [
        kit.section(tr("This camera"), t),
        kit.card(
            ft.Column(
                [
                    kit.setting_row(tr("Name"), _camera_name(app) or "—", t, chevron=False),
                    kit.setting_row(tr("Model"), st.model, t, chevron=False),
                    kit.setting_row(tr("Firmware"), _fw(st.firmware), t, chevron=False, last=True),
                ],
                spacing=0,
            ),
            t,
            padding=0,
        ),
        kit.section(tr("Clock"), t),
        kit.card(
            ft.Row(
                [
                    ft.Column(
                        [
                            kit.text(tr("Camera clock"), t, 15),
                            kit.text(" · ".join(x for x in (clock, tz) if x), t, 12, t.text3),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    kit.secondary(
                        tr("Sync"),
                        lambda e: app.run(app.svc.sync_clock, ok=tr("Clock synced")),
                        t,
                        height=40,
                        text_size=15,
                        weight=ft.FontWeight.W_500,
                    ),
                ]
            ),
            t,
        ),
        kit.section(tr("Wireless"), t),
        kit.card(
            ft.Column(
                [
                    kit.check_row(
                        tr("Camera Wi-Fi"),
                        bool(st.s(Status.AP_MODE)),
                        lambda e: app.run(app.svc.set_ap, e.control.value),
                        t,
                        sub=tr("Turned on automatically for preview and media"),
                        key="camera-ap",
                        last=False,
                    ),
                    ft.Container(
                        ft.Row(
                            [
                                kit.text(tr("Wi-Fi band"), t, 15, expand=True),
                                ft.Container(
                                    kit.segmented(
                                        [(str(v), _band(v)) for v in bands],
                                        str(band),
                                        lambda v: app.run(app.svc.set_setting, 178, int(v)),
                                        t,
                                        height=36,
                                        key="band",
                                        text_size=13,
                                        inset=True,
                                    ),
                                    width=160,
                                ),
                            ]
                        ),
                        padding=ft.Padding.only(left=14, right=10, top=6, bottom=6),
                    ),
                ],
                spacing=0,
            ),
            t,
            padding=0,
        ),
        kit.section(tr("Power"), t),
        kit.card(
            ft.Column(
                [
                    kit.setting_row(
                        tr("Put camera to sleep"),
                        "",
                        t,
                        on_click=lambda e: _confirm_sleep(app),
                        chevron=False,
                        icon=ft.Icons.BEDTIME_OUTLINED,
                    ),
                    kit.setting_row(
                        tr("Auto power off"),
                        term(R.value_label(59, st.settings.get(59, 0))),
                        t,
                        on_click=lambda e: pick(app, 59),
                        value_color=t.warn if never else None,
                        last=True,
                    ),
                ],
                spacing=0,
            ),
            t,
            padding=0,
        ),
        ft.Row(
            [
                kit.destructive(
                    tr("Forget this camera"),
                    lambda e: _forget(app),
                    t,
                    outlined=True,
                    expand=True,
                    text_size=15,
                    height=52,
                    radius=16,
                )
            ]
        ),
        _demo_switch(app),
        _about(app),
    ]
    return ft.Column(rows, spacing=10, scroll=ft.ScrollMode.AUTO)


def _band(value: int) -> str:
    return re.sub(r"(\d)(G?Hz)\b", r"\1 \2", term(R.value_label(178, value)))


def _demo_switch(app) -> ft.Control:
    t = app.t
    appearance = ft.Container(
        ft.Row(
            [
                kit.text(tr("Appearance"), t, 15, expand=True),
                ft.Container(
                    kit.segmented(
                        [("dark", tr("Dark")), ("light", tr("Light")), ("system", tr("System"))],
                        app.theme_pref,
                        app.set_theme,
                        t,
                        height=36,
                        key="theme",
                        text_size=13,
                        inset=True,
                    ),
                    width=220,
                ),
            ]
        ),
        padding=ft.Padding.only(left=14, right=10, top=6, bottom=6),
        border=ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )
    return kit.card(
        ft.Column(
            [
                appearance,
                kit.setting_row(
                    tr("Language"),
                    tr("System") if app.language_pref == "system" else i18n.LANGUAGES.get(app.language_pref, ""),
                    t,
                    on_click=lambda e: _choose_language(app),
                    key="language",
                ),
                kit.check_row(
                    tr("Demo mode"),
                    app.sim,
                    lambda e: app.run(app.set_demo, e.control.value),
                    t,
                    sub=tr("Use a simulated camera to try Quak without one"),
                    key="demo",
                ),
            ],
            spacing=0,
        ),
        t,
        padding=0,
    )


def _choose_language(app) -> None:
    def pick(code: str):
        def chosen(e):
            app.close_dialog()
            app.set_language(code)

        return chosen

    choices = [("system", tr("System")), *i18n.LANGUAGES.items()]
    app.dialog(
        tr("Language"),
        ft.Column(
            [
                kit.setting_row(
                    name,
                    "✓" if code == app.language_pref else "",
                    app.t,
                    on_click=pick(code),
                    key=f"language-{code}",
                    chevron=False,
                )
                for code, name in choices
            ],
            spacing=0,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
        ),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog())],
    )


def _about(app) -> ft.Control:
    from quak import __version__

    return ft.Column(
        [
            kit.text(
                tr(
                    "Quak {version} · open source, MIT. Not affiliated with GoPro, Inc. GoPro, HERO and Quik "
                    "are trademarks of GoPro, Inc.",
                    version=__version__,
                ),
                app.t,
                12,
                app.t.text3,
            ),
            ft.TextButton(
                tr("Licences"), on_click=lambda e: _show_licences(app), style=ft.ButtonStyle(color=app.t.accent)
            ),
        ],
        spacing=2,
        horizontal_alignment=ft.CrossAxisAlignment.START,
    )


def _show_licences(app) -> None:
    """The notices the software inside this app asks to be shown (mpv and FFmpeg among them)."""
    assets = Path(__file__).parent.parent / "assets"
    body = [ft.Markdown((assets / "THIRD_PARTY_NOTICES.md").read_text(), selectable=True, code_theme=None)]
    for name in ("LGPL-3.0", "LGPL-2.1", "GPL-3.0"):
        body += [
            kit.title(name, app.t, 16),
            ft.Text(
                (assets / "licenses" / f"{name}.txt").read_text(),
                size=11,
                selectable=True,
                font_family=th.MONO_FONT,
                color=app.t.text2,
            ),
        ]
    app.dialog(
        tr("Licences"),
        ft.Container(ft.Column(body, scroll=ft.ScrollMode.AUTO, spacing=12), width=560, height=480),
        [ft.TextButton(tr("Close"), on_click=lambda e: app.close_dialog())],
    )


def _forget(app) -> None:
    t = app.t

    async def forget():
        address = app.svc.cam.link.address if app.svc.cam else None
        await app.svc.disconnect()
        if address and not app.sim:
            config.forget_camera(address)
        app.go("cameras")

    def yes(e):
        app.close_dialog()
        app.run(forget)

    app.dialog(
        tr("Forget this camera?"),
        ft.Text(
            tr(
                "Quak disconnects and won't reconnect automatically. To remove the bond "
                "completely, also remove this device on the camera."
            ),
            color=t.text2,
        ),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()), ft.TextButton(tr("Forget"), on_click=yes)],
    )


def _diagnostics(app) -> ft.Control:
    t, d = app.t, app.diag
    # the self-test records clips, changes settings and takes the Wi-Fi adapter for half an hour: not one click away
    self_test = app.sim or config.developer_mode()
    tabs = kit.underline_tabs(
        [("log", tr("Log"))] + ([("selftest", tr("Self-test"))] if self_test else []),
        d["tab"],
        lambda v: (d.update(tab=v), app.render()),
        t,
        key="diag-tab",
    )
    if d["tab"] == "log" or not self_test:
        hide = d.get("hide_keepalive", False)
        filters = ft.Row(
            [
                kit.filter_chip(
                    caption,
                    t,
                    selected=d["filter"] == c,
                    on_click=lambda e, c=c: (d.update(filter=c), app.render()),
                )
                for c, caption in (
                    ("all", tr("All")),
                    ("commands", tr("Commands")),
                    ("settings", tr("Settings")),
                    ("queries", tr("Queries")),
                )
            ]
            + [
                kit.filter_chip(
                    tr("Hide keep-alive"),
                    t,
                    selected=hide,
                    on_click=lambda e: (d.update(hide_keepalive=not hide), app.render()),
                )
            ],
            wrap=True,
            spacing=6,
            run_spacing=6,
        )
        d["log_view"] = ft.Column(log_rows(app), spacing=10, scroll=ft.ScrollMode.AUTO, auto_scroll=True, expand=True)
        return ft.Column(
            [
                tabs,
                filters,
                ft.Container(
                    d["log_view"],
                    bgcolor=t.log_bg,
                    border_radius=14,
                    padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    border=ft.Border.all(1, t.card_border) if t.card_border else None,
                    expand=True,
                ),
                ft.Row(
                    [
                        kit.secondary(
                            tr("Resume") if d["paused"] else tr("Pause"),
                            lambda e: (d.update(paused=not d["paused"]), app.render()),
                            t,
                            expand=True,
                            height=46,
                            text_size=14,
                            radius=12,
                        ),
                        kit.secondary(
                            tr("Export log"),
                            lambda e: _export_log(app),
                            t,
                            expand=True,
                            height=46,
                            text_size=14,
                            radius=12,
                        ),
                    ],
                    spacing=10,
                ),
            ],
            spacing=10,
            expand=True,
        )
    from quak.e2e import runner, scenarios  # noqa: F401  (registers the scenarios)

    done = {r["id"]: r for r in d.get("results", [])}
    results = [
        done.get(sc.id) or {"id": sc.id, "title": sc.title, "status": "human" if "human" in sc.needs else "todo"}
        for sc in runner.SCENARIOS
    ]
    look = {
        "pass": (t.ok_tint, t.ok, tr("Passed")),
        "fail": (t.danger_tint, t.danger_text, tr("Failed")),
        "skip": (t.surface2, t.text2, tr("Skipped")),
        "running": (t.accent_tint, t.accent_ink, tr("Running")),
        "todo": (t.surface2, t.text2, tr("Not run")),
        "human": (t.warn_tint, t.warn_text, tr("Needs you")),
    }
    rows = [
        ft.Container(
            ft.Row(
                [
                    ft.Container(kit.mono(r["id"], t, 12, t.text3), width=34),
                    kit.text(r["title"], t, 14, expand=True),
                    kit.pill(look[r["status"]][2], t, bg=look[r["status"]][0], fg=look[r["status"]][1], bold=True),
                ],
                spacing=10,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            border=ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
        )
        for r in results
    ]
    running = d["e2e"] is not None and d["e2e"].returncode is None
    before, after = tr(
        "Runs the same checks as {command} against the connected camera, for about 30 minutes. Settings are restored "
        "afterwards, and only clips the test records are deleted."
    ).split("{command}")
    intro = ft.Text(
        spans=[
            ft.TextSpan(before),
            ft.TextSpan("python -m quak.e2e", style=ft.TextStyle(font_family=th.MONO_FONT, size=13, color=t.soft_text)),
            ft.TextSpan(after),
        ],
        size=14,
        color=t.text2,
    )
    return ft.Column(
        [
            tabs,
            intro,
            kit.card(ft.Column(rows, spacing=0, scroll=ft.ScrollMode.AUTO), t, padding=0, expand=True),
            ft.Row(
                [
                    kit.secondary(
                        tr("Stop") if running else tr("Run self-test"),
                        lambda e: _stop_selftest(app) if running else app.run(_selftest, app),
                        t,
                        expand=True,
                        text_size=15,
                    ),
                    kit.secondary(tr("Export results"), lambda e: _export_results(app), t, expand=True, text_size=15),
                ],
                spacing=10,
            ),
        ],
        spacing=12,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


def log_rows(app) -> list[ft.Control]:
    """The Diagnostics log list: the newest lines the filter chips let through."""
    t, d = app.t, app.diag
    hide = d.get("hide_keepalive", False)
    wanted = {"all": None, "commands": {"commands"}, "settings": {"settings", "keepalive"}, "queries": {"queries"}}[
        d["filter"]
    ]
    lines = [
        l
        for l in list(app.svc.log)[-300:]
        if (wanted is None or l.category in wanted) and not (hide and l.category == "keepalive")
    ][-80:]
    return [
        ft.Column(
            [
                ft.Row(
                    [
                        kit.mono(
                            time.strftime("%H:%M:%S", time.localtime(l.t)) + f".{int(l.t * 1000) % 1000:03d}",
                            t,
                            11,
                            t.text3,
                        ),
                        kit.mono(
                            tr("sent") if l.direction == "tx" else tr("received"),
                            t,
                            11,
                            t.accent if l.direction == "tx" else t.ok,
                        ),
                        kit.mono(_kind(l), t, 11, t.text3),
                    ],
                    spacing=8,
                ),
                kit.mono(l.hex if len(l.hex) <= 150 else l.hex[:150] + " …", t, 12, t.soft_text),
                *([kit.text(l.meaning, t, 12, t.text2)] if l.meaning else []),
            ],
            spacing=2,
        )
        for l in lines
    ]


def _kind(line) -> str:
    """What the log calls a message: the design's 'settings', 'query reply', ..."""
    plain, reply = LOG_KINDS.get(line.category, (line.channel, tr("{channel} reply", channel=line.channel)))
    return term(reply if line.direction == "rx" else plain)


def _export_log(app) -> None:
    path = app.downloads_dir() / f"quak-log-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    path.write_text(
        "\n".join(
            f"{time.strftime('%H:%M:%S', time.localtime(l.t))} {l.direction} {l.channel} {l.hex}  {l.meaning}"
            for l in app.svc.log
        )
    )
    app.toast(tr("Saved {path}", path=path))


def _export_results(app) -> None:
    results = app.diag.get("results", [])
    path = app.downloads_dir() / f"quak-selftest-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in results))
    app.toast(tr("Saved {path}", path=path))


async def _selftest(app) -> None:
    """Desktop: run the E2E runner (it talks to the camera itself, so Quak disconnects first)."""
    d = app.diag
    d["results"] = []
    await app.svc.disconnect()
    env = dict(os.environ)
    args = [sys.executable, "-m", "quak.e2e"] + (["--sim"] if app.sim else [])
    if app.sim:
        app.svc.target.sim_camera.save(config.sim_state_file())
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    d["e2e"] = proc
    current = None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if "] == " in line:
            sid, _, title = line.split("] == ", 1)[1].partition(" ")
            current = {"id": sid, "title": title, "status": "running"}
            d["results"].append(current)
        elif current and ("   PASS " in line or "   FAIL " in line or "   SKIP " in line):
            current["status"] = "pass" if "PASS" in line else "fail" if "FAIL" in line else "skip"
        app.render_soon()
    await proc.wait()
    if app.sim:
        from quak.transport.sim import SimCamera

        app.svc.target.sim_camera = SimCamera.load_or_new(config.sim_state_file())
    await app.connect()


def _stop_selftest(app) -> None:
    proc = app.diag.get("e2e")
    if proc and proc.returncode is None:
        proc.terminate()

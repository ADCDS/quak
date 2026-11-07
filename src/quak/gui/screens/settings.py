"""Every setting of the current preset."""

from __future__ import annotations

import flet as ft

from quak.core import registry as R
from quak.core.registry import Status
from quak.gui import kit
from quak.gui.screens.common import SETTING_GROUPS, _header, _label, _reason, _scroll, _value, pick
from quak.gui.screens.remote import _preset_name


def settings(app) -> ft.Control:
    t, st = app.t, app.svc.state
    if not st.connected:
        return _scroll([_header(app, "Camera settings", back="remote"), kit.banner("Not connected.", t, "warn")])
    pro = st.settings.get(R.CONTROL_MODE) == 1
    body = [
        _header(
            app,
            "Camera settings",
            back="remote",
            right=kit.text(_preset_name(app, int(st.s(Status.PRESET, 0))) + " preset", t, 13, t.text2),
        ),
        ft.Row(
            [
                kit.text("Controls", t, 14, t.text2),
                ft.Container(
                    kit.segmented(
                        [("easy", "Easy"), ("pro", "Pro")],
                        "pro" if pro else "easy",
                        lambda v: app.run(app.svc.set_setting, R.CONTROL_MODE, 1 if v == "pro" else 0),
                        t,
                        height=40,
                        key="controls",
                        accent=True,
                    ),
                    expand=True,
                ),
            ],
            spacing=12,
        ),
    ]
    if not pro:
        body.append(
            kit.banner(
                "In Easy controls the camera hides duration, HindSight and scheduled capture. "
                "Switch to Pro to use them.",
                t,
                size=13,
            )
        )
    for group, ids in SETTING_GROUPS:
        ids = [i for i in ids if i in R.SETTINGS and i in st.settings]
        if not ids:
            continue
        rows = []
        for i, sid in enumerate(ids):
            warn = "Camera stays on after a scheduled capture" if sid == 59 and st.settings.get(59) == 0 else None
            if sid == R.SCHEDULED_CAPTURE:
                rows.append(
                    kit.setting_row(
                        "Scheduled capture",
                        st.schedule["time"] if st.schedule["armed"] else "Off",
                        t,
                        on_click=lambda e: app.go("schedule"),
                        reason=_reason(app, sid),
                        last=i == len(ids) - 1,
                    )
                )
                continue
            rows.append(
                kit.setting_row(
                    _label(sid),
                    _value(app, sid),
                    t,
                    on_click=lambda e, s=sid: pick(app, s),
                    reason=_reason(app, sid),
                    warn=warn,
                    key=f"setting-{sid}",
                    last=i == len(ids) - 1,
                )
            )
        body += [kit.section(group, t), kit.card(ft.Column(rows, spacing=0), t, padding=0)]
    return _scroll(body, spacing=10)

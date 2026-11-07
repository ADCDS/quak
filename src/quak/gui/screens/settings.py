"""Every setting of the current preset."""

from __future__ import annotations

import flet as ft

from quak.core import registry as R
from quak.core.registry import Status
from quak.gui import kit
from quak.gui.screens.common import SETTING_GROUPS, _header, _label, _reason, _scroll, _value, pick
from quak.gui.screens.remote import _preset_name
from quak.i18n import term, tr


def settings(app) -> ft.Control:
    t, st = app.t, app.svc.state
    if not st.connected:
        return _scroll(
            [_header(app, tr("Camera settings"), back="remote"), kit.banner(tr("Not connected."), t, "warn")]
        )
    pro = st.settings.get(R.CONTROL_MODE) == 1
    body = [
        _header(
            app,
            tr("Camera settings"),
            back="remote",
            right=kit.text(
                tr("{name} preset", name=term(_preset_name(app, int(st.s(Status.PRESET, 0))))),
                t,
                13,
                t.text2,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
                text_align=ft.TextAlign.RIGHT,
            ),
        ),
        ft.Row(
            [
                kit.text(tr("Controls"), t, 14, t.text2),
                ft.Container(
                    kit.segmented(
                        [("easy", tr("Easy")), ("pro", tr("Pro"))],
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
                tr(
                    "In Easy controls the camera hides duration, HindSight and scheduled capture. "
                    "Switch to Pro to use them."
                ),
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
            warn = tr("Camera stays on after a scheduled capture") if sid == 59 and st.settings.get(59) == 0 else None
            if sid == R.SCHEDULED_CAPTURE:
                rows.append(
                    kit.setting_row(
                        tr("Scheduled capture"),
                        st.schedule["time"] if st.schedule["armed"] else tr("Off"),
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
        body += [kit.section(term(group), t), kit.card(ft.Column(rows, spacing=0), t, padding=0)]
    return _scroll(body, spacing=10)

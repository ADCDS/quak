"""The card, and what's been downloaded."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import flet as ft

from quak import i18n
from quak.core.registry import Status
from quak.gui import kit
from quak.gui import theme as th
from quak.gui.screens.common import _scroll, _size
from quak.i18n import tr, trn


def media(app) -> ft.Control:
    t, st, m = app.t, app.svc.state, app.media
    if not st.connected:
        return _scroll(
            [
                kit.title(tr("Media"), t),
                kit.banner(tr("Not connected."), t, "warn", on_click=lambda e: app.go("cameras")),
            ]
        )
    m.setdefault("source", "camera")
    if not m["loading"] and not m["error"] and (m.get("unfinished") or (not m["files"] and not m.get("loaded"))):
        app.run(_load_media, app, redraw=False)
    files = m["files"]
    flt = m["filter"]
    m["tiles"] = {}  # refilled as the tiles are built
    hilights = m.get("hilights", {})
    shown = [
        f
        for f in files
        if flt == "all"
        or (flt == "videos" and f.is_video)
        or (flt == "photos" and not f.is_video)
        or (flt == "hilights" and hilights.get(f.path))
    ]
    summary = trn("{n} item · {size}", "{n} items · {size}", len(files), size=_size(sum(f.size for f in files)))
    turbo = bool(st.s(Status.TURBO))
    filters = [
        kit.filter_chip(
            caption,
            t,
            selected=flt == key,
            on_click=lambda e, k=key: (m.update(filter=k), app.render()),
            key=f"filter-{key}",
        )
        for key, caption in (
            ("all", tr("All")),
            ("videos", tr("Videos")),
            ("photos", tr("Photos")),
            ("hilights", tr("HiLights")),
        )
    ]
    if app.wide:
        return _media_desktop(app, shown, summary, turbo, filters)
    head = ft.Row(
        [
            kit.page_title(tr("Media"), t),
            ft.TextButton(
                tr("Cancel") if m["selecting"] else tr("Select"),
                on_click=lambda e: _toggle_select(app),
                style=ft.ButtonStyle(color=t.accent),
            ),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
    )
    source = kit.segmented(
        [("camera", tr("On camera")), ("device", tr("On this device"))],
        m["source"],
        lambda v: (m.update(source=v), app.render()),
        t,
        height=40,
        key="media-source",
    )
    if m["source"] == "device":
        return ft.Container(
            ft.Column([head, source, _device_files(app)], spacing=12, expand=True),
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            expand=True,
        )
    status = ft.Row(
        [
            ft.Container(width=7, height=7, border_radius=4, bgcolor=t.ok if st.http else t.muted),
            kit.text(
                (tr("Camera Wi-Fi connected") if st.http else tr("Connecting to the camera's Wi-Fi…"))
                + (f" · {summary}" if files else ""),
                t,
                13,
                t.text2,
                expand=True,
            ),
            ft.Container(
                ft.Text(tr("Turbo on") if turbo else tr("Turbo off"), size=12, color=t.soft_text),
                height=24,
                border_radius=12,
                bgcolor=t.surface2,
                padding=ft.Padding.symmetric(horizontal=8),
                alignment=ft.Alignment.CENTER,
                on_click=lambda e: app.run(_load_media, app, redraw=False),
                tooltip=tr("Refresh"),
            ),
        ],
        spacing=8,
    )
    items: list[ft.Control] = [head, source, status, ft.Row(filters, spacing=8, scroll=ft.ScrollMode.AUTO)]
    if m["error"]:
        items.append(kit.banner(m["error"], t, "danger"))
    width = (app.page.width or 412) - 32
    tw = (width - 12) / 3
    grid: list[ft.Control] = []
    if m["loading"] and not files:
        grid.append(ft.Row([ft.ProgressRing(color=t.accent, width=28, height=28)]))
    elif not files:
        grid.append(kit.text(tr("No media on the card."), t, 15, t.text2))
    for title, group in _sections(shown):
        grid += [
            ft.Container(
                ft.Text(title, size=13, weight=ft.FontWeight.W_600, color=t.text2), padding=ft.Padding.only(top=10)
            ),
            ft.Row([_tile(app, f, tw, 120) for f in group], wrap=True, spacing=6, run_spacing=6),
        ]
    bottom = _media_bottom(app, files)
    return ft.Container(
        ft.Column(
            items + [ft.Column(grid, spacing=8, scroll=ft.ScrollMode.AUTO, expand=True)] + bottom,
            spacing=12,
            expand=True,
        ),
        padding=ft.Padding.symmetric(horizontal=16, vertical=12),
        expand=True,
    )


def _media_desktop(app, shown, summary: str, turbo: bool, filters) -> ft.Control:
    """Desktop Media (design: Desktop · Media): header with filters, grid by day, the selected file on the right."""
    t, st, m = app.t, app.svc.state, app.media
    files = m["files"]
    over = (
        (tr("over the camera's Wi-Fi, turbo on") if turbo else tr("over the camera's Wi-Fi"))
        if st.http
        else tr("connecting to the camera's Wi-Fi…")
    )
    header = ft.Row(
        [
            ft.Column(
                [
                    kit.title(tr("Camera media"), t, 24),
                    kit.text(f"{summary} · {over}" if files else over, t, 14, t.text2),
                ],
                spacing=2,
                expand=True,
            ),
            ft.Row(
                filters
                + [
                    ft.TextButton(
                        tr("Cancel") if m["selecting"] else tr("Select"),
                        on_click=lambda e: _toggle_select(app),
                        style=ft.ButtonStyle(color=t.accent),
                    )
                ],
                spacing=8,
                tight=True,
            ),
        ]
    )
    avail = (app.page.width or 1280) - 88 - 48 - 20 - 300
    cols = max(2, int((avail + 10) // 160))
    tw = (avail - (cols - 1) * 10) / cols
    grid: list[ft.Control] = []
    if m["error"]:
        grid.append(kit.banner(m["error"], t, "danger"))
    if m["loading"] and not files:
        grid.append(ft.Row([ft.ProgressRing(color=t.accent, width=28, height=28)]))
    elif not files:
        grid.append(kit.text(tr("No media on the card."), t, 15, t.text2))
    for title, group in _sections(shown):
        grid += [
            ft.Text(title, size=13, weight=ft.FontWeight.W_600, color=t.text2),
            ft.Row([_tile(app, f, tw, tw * 3 / 4) for f in group], wrap=True, spacing=10, run_spacing=10),
        ]
    left = ft.Column(grid + _media_bottom(app, files), spacing=10, expand=True, scroll=ft.ScrollMode.AUTO)
    current = m.get("detail") if m.get("detail") in shown else (shown[0] if shown else None)
    m["detail"] = current  # the selected tile gets the amber edge, as in the design
    side = ft.Container(_detail(app, current) if current else None, width=300)
    return ft.Container(
        ft.Column(
            [header, ft.Row([left, side], spacing=20, expand=True, vertical_alignment=ft.CrossAxisAlignment.START)],
            spacing=20,
            expand=True,
        ),
        padding=ft.Padding.only(left=24, right=24, top=20, bottom=24),
        expand=True,
    )


def _sections(files) -> list[tuple[str, list]]:
    """Group by the day the camera recorded them ("cre" is camera-local wall time)."""
    today = dt.date.today()
    out: dict[str, list] = {}
    for f in files:
        day = dt.datetime.fromtimestamp(f.created, dt.timezone.utc).date()
        title = (
            tr("Today")
            if day == today
            else tr("Yesterday")
            if day == today - dt.timedelta(days=1)
            else i18n.weekday_day_month(day)
        )
        out.setdefault(title, []).append(f)
    return list(out.items())


def _media_bottom(app, files) -> list[ft.Control]:
    t, m = app.t, app.media
    if m["progress"]:
        name, done, total = m["progress"]
        return [
            kit.card(
                ft.Column(
                    [
                        kit.text(tr("Downloading {name}", name=name), t, 15, weight=ft.FontWeight.W_600),
                        ft.ProgressBar(value=done / total if total else None, color=t.accent, bgcolor=t.ground),
                        kit.text(tr("{done} of {total}", done=_size(done), total=_size(total)), t, 13, t.text2),
                    ],
                    spacing=8,
                ),
                t,
                bg=t.surface2,
            )
        ]
    if m["selecting"] and m["selected"]:
        sel = [f for f in files if f.path in m["selected"]]
        return [
            kit.card(
                ft.Row(
                    [
                        kit.text(
                            trn(
                                "{n} selected · {size}",
                                "{n} selected · {size}",
                                len(sel),
                                size=_size(sum(f.size for f in sel)),
                            ),
                            t,
                            15,
                            weight=ft.FontWeight.W_600,
                            expand=True,
                        ),
                        kit.primary(
                            tr("Download"), lambda e: app.run(_download, app, sel), t, height=44, icon=ft.Icons.DOWNLOAD
                        ),
                        kit.icon_button(
                            ft.Icons.DELETE_OUTLINE,
                            tr("Delete from camera"),
                            lambda e: _confirm_delete(app, sel),
                            t,
                            bgcolor=t.danger_tint,
                            color=t.danger_text,
                        ),
                    ],
                    spacing=8,
                ),
                t,
                bg=t.surface2,
            )
        ]
    return []


def _device_files(app) -> ft.Control:
    """Files Quak downloaded to this device."""
    t, m = app.t, app.media
    d = app.downloads_dir()
    files = sorted(
        (p for p in d.iterdir() if p.is_file() and p.suffix.upper() in (".MP4", ".JPG", ".LRV")),
        key=lambda p: -p.stat().st_mtime,
    )
    if not files:
        phone = app.page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS)
        message = (
            tr("Nothing downloaded yet. They stay inside Quak until you tap one to share or save it.")
            if phone
            else tr("Nothing downloaded yet. Downloads go to {folder}.", folder=d)
        )
        return kit.text(message, t, 15, t.text2)
    thumbs = {path.rsplit("/", 1)[-1]: data for path, data in m["thumbs"].items() if data}
    tw = ((app.page.width or 412) - 32 - 12) / 3
    tiles = []
    for p in files:
        thumb = thumbs.get(p.name)
        tiles.append(
            ft.Container(
                ft.Stack(
                    [
                        ft.Container(
                            ft.Image(src=thumb, fit=ft.BoxFit.COVER, width=tw, height=120)
                            if thumb
                            else ft.Icon(
                                ft.Icons.MOVIE_OUTLINED if p.suffix.upper() == ".MP4" else ft.Icons.IMAGE_OUTLINED,
                                color=t.text3,
                                size=30,
                            ),
                            bgcolor=t.surface2,
                            alignment=ft.Alignment.CENTER,
                            left=0,
                            top=0,
                            right=0,
                            bottom=0,
                        ),
                        ft.Container(kit.pill(p.name, t, bg="#BF0B0D10"), left=6, bottom=6),
                    ]
                ),
                width=tw,
                height=120,
                border_radius=10,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                tooltip=str(p),
                on_click=lambda e, p=p: app.run(_open_local, app, p, redraw=False),
            )
        )
    return ft.Column([ft.Row(tiles, wrap=True, spacing=6, run_spacing=6)], scroll=ft.ScrollMode.AUTO, expand=True)


async def _open_local(app, path: Path) -> None:
    if app.page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS):
        # a phone keeps these inside the app, where nothing else can reach them: sharing is how they get out
        await ft.Share().share_files([ft.ShareFile.from_path(str(path))], title=path.name)
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opens the user's own download with its default app


async def _load_media(app) -> None:
    m = app.media

    def show(redraw) -> None:  # a rebuild can swallow a tap, so leave other screens alone while the card loads
        if app.screen == "media":
            redraw()

    m.update(loading=True, error="")
    show(app.render)
    try:
        try:
            files = await app.svc.media_list()
        except Exception:  # a stale camera-Wi-Fi link (e.g. after a livestream): join again once
            await app.svc.release_http()
            files = await app.svc.media_list()
        m.update(files=sorted(files, key=lambda f: -f.created), loaded=True, loading=False)
        show(app.render)
        m["unfinished"] = False
        for f in m["files"][:60]:
            if app.screen != "media":  # carry on when the user comes back: this would hold the camera's Wi-Fi open
                m["unfinished"] = True
                break
            if f.path not in m["thumbs"]:
                try:
                    m["thumbs"][f.path] = await app.svc.thumbnail(f)
                    info = await asyncio.to_thread((await app.svc.http()).media_info, f.path)
                    m.setdefault("info", {})[f.path] = info
                    m.setdefault("hilights", {})[f.path] = int(info.get("hc", 0) or 0)
                except Exception:
                    m["thumbs"][f.path] = b""
                _refresh_tile(app, f)
    except Exception as e:
        m.update(error=tr("Couldn't read the card: {error}", error=e), loading=False)
        show(app.render)
        raise


def _refresh_tile(app, f) -> None:
    """A new thumbnail changes one tile. Rebuilding the page for it makes every tile redraw, which flickers."""
    m = app.media
    if app.screen != "media":
        return
    if app.wide and m.get("detail") is f:  # the side panel shows it too
        app.render_soon()
        return
    stack, width, height = m.get("tiles", {}).get(f.path, (None, 0, 0))
    if stack is None:  # not on screen (filtered out)
        return
    try:
        stack.controls = _tile_layers(app, f, width, height)
        stack.update()
    except Exception:  # a rebuild replaced the tile meanwhile, and that one already has the thumbnail
        pass


def _tile_layers(app, f, width: float, height: float) -> list[ft.Control]:
    t, m = app.t, app.media
    thumb = m["thumbs"].get(f.path)
    fill = dict(left=0, top=0, right=0, bottom=0)
    layers = [
        ft.Container(
            ft.Image(src=thumb, fit=ft.BoxFit.COVER, width=width, height=height) if thumb else None,
            bgcolor=t.surface2,
            alignment=ft.Alignment.CENTER,
            **fill,
        )
    ]
    if f.is_video:
        dur = int((m.get("info", {}).get(f.path) or {}).get("dur", 0) or 0)
        badge = ft.Row(
            [
                ft.Icon(ft.Icons.PLAY_ARROW, color=t.text, size=12),
                ft.Text(
                    f"{dur // 60}:{dur % 60:02d}" if dur else tr("Video"),
                    size=12,
                    color=t.text,
                    weight=ft.FontWeight.W_500,
                ),
            ],
            spacing=3,
            tight=True,
        )
        layers.append(
            ft.Container(
                badge,
                height=22,
                border_radius=11,
                bgcolor="#BF0B0D10",
                padding=ft.Padding.symmetric(horizontal=7),
                left=6,
                bottom=6,
            )
        )
    if m.get("hilights", {}).get(f.path):
        layers.append(
            ft.Container(
                ft.Icon(ft.Icons.FLAG, color=t.on_accent, size=14),
                width=24,
                height=24,
                border_radius=12,
                bgcolor=t.accent,
                alignment=ft.Alignment.CENTER,
                right=6,
                bottom=6,
                tooltip=tr("Has HiLights"),
            )
        )
    if m["selecting"]:
        layers.append(
            ft.Container(
                ft.Icon(
                    ft.Icons.CHECK_CIRCLE if f.path in m["selected"] else ft.Icons.RADIO_BUTTON_UNCHECKED,
                    color=t.accent if f.path in m["selected"] else t.text,
                    size=24,
                ),
                right=6,
                top=6,
            )
        )
    return layers


def _tile(app, f, width: float = 120, height: float = 120) -> ft.Control:
    t, m = app.t, app.media
    selected = f.path in m["selected"] or (app.wide and not m["selecting"] and m.get("detail") is f)
    stack = ft.Stack(_tile_layers(app, f, width, height), expand=True)
    m.setdefault("tiles", {})[f.path] = (stack, width, height)

    def click(e):
        if m["selecting"]:
            (m["selected"].discard if f.path in m["selected"] else m["selected"].add)(f.path)
        else:
            m["detail"] = f
            if not app.wide:
                _detail_dialog(app, f)
        app.render()

    return ft.Semantics(
        label=f.name,
        content=ft.Container(
            stack,
            width=width,
            height=height,
            border_radius=12 if app.wide else 10,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            border=ft.Border.all(3, t.accent) if selected else None,
            on_click=click,
            ink=True,
            tooltip=f.name,
        ),
    )


def _detail(app, f) -> ft.Control:
    t, m = app.t, app.media
    thumb = m["thumbs"].get(f.path)
    info = m.get("info", {}).get(f.path) or {}
    created = dt.datetime.fromtimestamp(f.created, dt.timezone.utc)
    day = tr("Today") if created.date() == dt.date.today() else i18n.day_month(created)
    dur = int(info.get("dur", 0) or 0)
    hl = m.get("hilights", {}).get(f.path, 0)
    meta = (
        [f"{day} {created:%H:%M}"]
        + ([f"{dur // 60}:{dur % 60:02d}"] if f.is_video and dur else [])
        + [_size(f.size)]
        + ([trn("{n} HiLight", "{n} HiLights", hl)] if hl else [])
    )
    layers = [
        ft.Container(
            ft.Image(src=thumb, fit=ft.BoxFit.COVER) if thumb else None,
            bgcolor="#2C3A48",
            left=0,
            top=0,
            right=0,
            bottom=0,
        )
    ]
    if f.is_video and app.wide:
        layers.append(
            ft.Container(
                ft.Container(
                    ft.Icon(ft.Icons.PLAY_ARROW, color=t.text, size=26),
                    width=56,
                    height=56,
                    border_radius=28,
                    bgcolor="#B80B0D10",
                    alignment=ft.Alignment.CENTER,
                    on_click=lambda e: app.run(_play, app, f),
                    ink=True,
                    tooltip=tr("Play"),
                ),
                alignment=ft.Alignment.CENTER,
                left=0,
                top=0,
                right=0,
                bottom=0,
            )
        )
    rows = []
    w, h = info.get("w"), info.get("h")
    if w and h:
        rows.append(
            (
                tr("Resolution"),
                _resolution(int(w), int(h))
                + (
                    " · " + tr("{fps} fps", fps=round(int(info["fps"]) / max(1, int(info.get("fps_denom", 1) or 1))))
                    if info.get("fps")
                    else ""
                ),
            )
        )
    rows.append((tr("Size"), _size(f.size)))
    rows.append((tr("File"), f.path.split("/")[0]))
    table = kit.card(
        ft.Column(
            [kit.table_row(a, b, t, last=i == len(rows) - 1, value_width=110) for i, (a, b) in enumerate(rows)],
            spacing=0,
        ),
        t,
    )
    table.padding = ft.Padding.symmetric(horizontal=14, vertical=4)
    return ft.Column(
        [
            ft.Container(
                ft.Stack(layers), aspect_ratio=16 / 9, border_radius=14, clip_behavior=ft.ClipBehavior.HARD_EDGE
            ),
            ft.Column(
                [kit.text(f.name, t, 16, weight=ft.FontWeight.W_600), kit.text(" · ".join(meta), t, 13, t.text2)],
                spacing=2,
            ),
            table,
            ft.Row(
                [
                    kit.primary(
                        tr("Download"),
                        lambda e: (app.close_all_dialogs(), app.run(_download, app, [f])),
                        t,
                        expand=True,
                        icon=ft.Icons.DOWNLOAD,
                        height=46,
                        text_size=14,
                        radius=12,
                    ),
                    _button_danger(app, tr("Delete"), lambda e: _confirm_delete(app, [f])),
                ],
                spacing=10,
            ),
        ],
        spacing=14,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


def _button_danger(app, caption: str, on_click) -> ft.Control:
    t = app.t
    return ft.Button(
        content=caption,
        on_click=on_click,
        height=46,
        style=ft.ButtonStyle(
            bgcolor=t.danger_tint,
            color=t.danger_text,
            shape=ft.RoundedRectangleBorder(radius=12),
            text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600, font_family=th.body_font()),
            padding=ft.Padding.symmetric(horizontal=16),
        ),
    )


def _resolution(w: int, h: int) -> str:
    for width, name in ((5312, "5.3K"), (4096, "4K"), (3840, "4K"), (2704, "2.7K"), (1920, "1080p"), (1280, "720p")):
        if w >= width:
            return name
    return f"{w}×{h}"


async def _play(app, f) -> None:
    """Desktop: download the clip (if needed) and open it in the system's player."""
    dest = app.downloads_dir() / f.name
    if not dest.exists() or dest.stat().st_size != f.size:
        await _download(app, [f])
    await _open_local(app, dest)


def _detail_dialog(app, f) -> None:
    app.dialog(
        f.name,
        ft.Container(_detail(app, f), width=360),
        [ft.TextButton(tr("Close"), on_click=lambda e: app.close_dialog())],
    )


def _toggle_select(app) -> None:
    app.media.update(selecting=not app.media["selecting"], selected=set())
    app.render()


async def _download(app, files) -> None:
    dest = app.downloads_dir()
    for f in files:

        def progress(done, total, name=f.name):
            app.media["progress"] = (name, done, total)
            app.render_soon()

        await app.svc.download(f, dest, progress)
    app.media.update(progress=None, selecting=False, selected=set())
    app.toast(trn("Saved {n} file to {dest}", "Saved {n} files to {dest}", len(files), dest=dest))
    app.release_wifi_soon()


def _confirm_delete(app, files) -> None:
    t = app.t

    async def delete():
        for f in files:
            await app.svc.delete(f)
        app.media.update(selected=set(), selecting=False, detail=None, files=[], loaded=False)
        app.toast(trn("Deleted {n} file from the camera", "Deleted {n} files from the camera", len(files)))

    def yes(e):
        app.close_all_dialogs()
        app.run(delete)

    app.dialog(
        tr("Delete {name} from the camera?", name=files[0].name)
        if len(files) == 1
        else trn("Delete {n} file from the camera?", "Delete {n} files from the camera?", len(files)),
        ft.Text(tr("This can't be undone. Download first if you want to keep them."), color=t.text2),
        [
            ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()),
            ft.TextButton(tr("Delete"), on_click=yes, style=ft.ButtonStyle(color=t.danger_text)),
        ],
    )

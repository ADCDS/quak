"""Reusable Quak components (buttons, chips, pills, cards, setting rows, shutter)."""

from __future__ import annotations

from typing import Callable

import flet as ft

from quak.gui import theme as th
from quak.gui.theme import Tokens

Handler = Callable | None


def text(value: str, t: Tokens, size: int = th.BODY, color: str | None = None, weight=None, **kw) -> ft.Text:
    return ft.Text(value, size=size, color=color or t.text, weight=weight, **kw)


def title(value: str, t: Tokens, size: int = th.DISPLAY, color: str | None = None, **kw) -> ft.Text:
    """Headings: Space Grotesk, like the design (which tightens the big ones by 0.02 em)."""
    if 24 <= size <= 30 and "style" not in kw:
        kw["style"] = ft.TextStyle(letter_spacing=size * -0.02)
    return ft.Text(value, size=size, weight=ft.FontWeight.W_600, color=color or t.text, font_family=th.HEAD_FONT, **kw)


def page_title(value: str, t: Tokens) -> ft.Control:
    """A phone screen's title: the design sets it in from the cards by 4 px, like section labels."""
    return ft.Container(title(value, t), padding=ft.Padding.only(left=4))


def mono(value: str, t: Tokens, size: int = 12, color: str | None = None, **kw) -> ft.Text:
    return ft.Text(value, size=size, color=color or t.text, font_family=th.MONO_FONT, **kw)


def label(value: str, t: Tokens) -> ft.Text:
    return ft.Text(
        value.upper(), size=th.LABEL, weight=ft.FontWeight.W_600, color=t.text3, style=ft.TextStyle(letter_spacing=1.0)
    )


def section(value: str, t: Tokens) -> ft.Control:
    """A label above cards: set in by 4 px, as in the design (a label inside a card isn't)."""
    return ft.Container(label(value, t), padding=ft.Padding.only(left=4))


def card(
    content: ft.Control, t: Tokens, padding: int = 14, radius: int = th.R_CARD, bg: str | None = None, **kw
) -> ft.Container:
    border = ft.Border.all(1, t.card_border) if t.card_border and bg is None else None
    return ft.Container(
        content=content,
        bgcolor=bg or t.surface,
        border_radius=radius,
        border=border,
        padding=ft.Padding.all(padding),
        **kw,
    )


def _button(
    content: str,
    on_click: Handler,
    bg: str,
    fg: str,
    t: Tokens,
    icon=None,
    expand=False,
    height=50,
    border: str | None = None,
    key: str | None = None,
    disabled: bool = False,
    text_size: int = 16,
    weight=ft.FontWeight.W_600,
    radius: int = th.R_BUTTON,
) -> ft.Control:
    style = ft.ButtonStyle(
        bgcolor=bg,
        color=fg,
        shape=ft.RoundedRectangleBorder(radius=radius),
        side=ft.BorderSide(1, border) if border else None,
        padding=ft.Padding.symmetric(horizontal=18),
        # a button's text style replaces the theme's, so the family has to be named here
        text_style=ft.TextStyle(size=text_size, weight=weight, font_family=th.BODY_FONT),
        elevation=0,
        shadow_color=ft.Colors.TRANSPARENT,
    )
    return ft.Button(
        content=content,
        icon=icon,
        on_click=on_click,
        style=style,
        height=height,
        expand=expand,
        key=key,
        disabled=disabled,
    )


def primary(content: str, on_click: Handler, t: Tokens, **kw) -> ft.Control:
    return _button(content, on_click, t.accent, t.on_accent, t, **kw)


def secondary(content: str, on_click: Handler, t: Tokens, **kw) -> ft.Control:
    return _button(content, on_click, t.surface2, t.text, t, **kw)


def destructive(content: str, on_click: Handler, t: Tokens, outlined: bool = False, **kw) -> ft.Control:
    """Filled red-tint button, or (outlined) a transparent one with a red edge, as in More > Camera."""
    if outlined:
        return _button(content, on_click, ft.Colors.TRANSPARENT, t.danger_text, t, border=t.danger_line, **kw)
    return _button(content, on_click, t.danger_tint, t.danger_text, t, border=t.danger_line, **kw)


def live_button(content: str, on_click: Handler, t: Tokens, **kw) -> ft.Control:
    return _button(content, on_click, t.rec, "#FFFFFF", t, **kw)


def filter_chip(
    value: str, t: Tokens, selected: bool = False, on_click: Handler = None, key: str | None = None
) -> ft.Container:
    """A filter (Media, Diagnostics): selected is light on dark, unlike choice chips."""
    return ft.Container(
        content=ft.Text(
            value, size=13, color=t.ground if selected else t.text, weight=ft.FontWeight.W_600 if selected else None
        ),
        bgcolor=t.text if selected else None,
        border=ft.Border.all(1, t.text if selected else t.line),
        border_radius=17,
        padding=ft.Padding.symmetric(horizontal=14, vertical=7),
        on_click=on_click,
        ink=True,
        key=key,
    )


def check(
    value: bool, on_change: Handler, t: Tokens, key: str | None = None, tooltip: str | None = None
) -> ft.Checkbox:
    """The design's on/off control in a row: an amber checkbox."""
    return ft.Checkbox(
        value=value,
        on_change=on_change,
        active_color=t.accent,
        check_color=t.on_accent,
        border_side=ft.BorderSide(2, t.text2),
        shape=ft.RoundedRectangleBorder(radius=4),
        key=key,
        tooltip=tooltip,
    )


def check_row(
    title_text: str,
    value: bool,
    on_change: Handler,
    t: Tokens,
    sub: str | None = None,
    key: str | None = None,
    last: bool = True,
) -> ft.Container:
    left = [ft.Text(title_text, size=15, color=t.text)]
    if sub:
        left.append(ft.Text(sub, size=12, color=t.text3))
    return ft.Container(
        ft.Row([ft.Column(left, spacing=2, expand=True), check(value, on_change, t, key=key)]),
        padding=ft.Padding.only(left=14, right=6, top=6, bottom=6),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def underline_tabs(
    options: list[tuple[str, str]], selected: str, on_select: Callable[[str], None], t: Tokens, key: str | None = None
) -> ft.Container:
    cells = []
    for value, caption in options:
        on = value == selected
        cells.append(
            ft.Container(
                ft.Text(caption, size=15, color=t.text if on else t.text2, weight=ft.FontWeight.W_600 if on else None),
                padding=ft.Padding.only(left=2, right=2, top=8, bottom=8),
                border=ft.Border.only(bottom=ft.BorderSide(2, t.accent)) if on else None,
                on_click=lambda e, v=value: on_select(v),
                key=f"{key}-{value}" if key else None,
            )
        )
    return ft.Container(ft.Row(cells, spacing=18), border=ft.Border.only(bottom=ft.BorderSide(1, t.line)))


def table_row(
    name: str,
    value: str,
    t: Tokens,
    on_click: Handler = None,
    last: bool = False,
    value_width: int = 104,
    key: str | None = None,
) -> ft.Container:
    """Two-column inspector row (desktop Remote/Media): label, then the value in its own column."""
    line = None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2))
    return ft.Container(
        ft.Row(
            [
                ft.Text(name, size=14, color=t.text, expand=True),
                ft.Container(ft.Text(value, size=14, color=t.text2, no_wrap=True), width=value_width),
            ],
            spacing=12,
        ),
        padding=ft.Padding.symmetric(vertical=10),
        border=line,
        on_click=on_click,
        ink=bool(on_click),
        key=key,
    )


def record_icon(t: Tokens, recording: bool = False) -> ft.Control:
    """The quick-action record glyph: a ring with a red dot (a red square while recording)."""
    return ft.Container(
        ft.Container(width=10, height=10, border_radius=2 if recording else 5, bgcolor=t.rec),
        width=22,
        height=22,
        border_radius=11,
        border=ft.Border.all(2, t.text),
        alignment=ft.Alignment.CENTER,
    )


def chip(
    value: str,
    t: Tokens,
    selected: bool = False,
    on_click: Handler = None,
    dashed: bool = False,
    key: str | None = None,
) -> ft.Container:
    return ft.Container(
        content=ft.Text(
            value,
            size=14,
            color=t.on_accent if selected else (t.text2 if dashed else t.text),
            weight=ft.FontWeight.W_600 if selected else None,
        ),
        bgcolor=t.accent if selected else t.ground,
        border=ft.Border.all(1, t.accent if selected else (t.muted if dashed else t.line)),
        border_radius=19,
        padding=ft.Padding.symmetric(horizontal=14, vertical=8),
        on_click=on_click,
        ink=True,
        key=key,
    )


def pill(
    value: str, t: Tokens, dot: str | None = None, bg: str | None = None, fg: str | None = None, bold: bool = False
) -> ft.Container:
    row = [ft.Container(width=7, height=7, border_radius=4, bgcolor=dot)] if dot else []
    row.append(ft.Text(value, size=12, color=fg or t.text, weight=ft.FontWeight.W_700 if bold else ft.FontWeight.W_500))
    return ft.Container(
        content=ft.Row(row, spacing=6, tight=True),
        bgcolor=bg or t.surface,
        border_radius=14,
        padding=ft.Padding.symmetric(horizontal=10, vertical=5),
    )


def stat(name: str, value: str, t: Tokens) -> ft.Container:
    return ft.Container(
        content=ft.Column([text(name, t, 12, t.text2), text(value, t, 16, weight=ft.FontWeight.W_600)], spacing=2),
        bgcolor=t.surface2,
        border_radius=12,
        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        expand=True,
    )


def banner(
    message: str,
    t: Tokens,
    kind: str = "accent",
    on_click: Handler = None,
    icon=None,
    key: str | None = None,
    size: int = 14,
) -> ft.Container:
    bg, fg = {
        "accent": (t.accent_tint, t.accent_ink),
        "warn": (t.warn_tint, t.warn_text),
        "danger": (t.danger_tint, t.danger_text),
        "ok": (t.ok_tint, t.ok),
    }[kind]
    row = ([ft.Icon(icon, color=fg, size=20)] if icon else []) + [ft.Text(message, size=size, color=fg, expand=True)]
    if on_click:
        row.append(ft.Icon(ft.Icons.CHEVRON_RIGHT, color=fg, size=18))
    return ft.Container(
        content=ft.Row(row, spacing=10),
        bgcolor=bg,
        border_radius=12,
        padding=ft.Padding.symmetric(horizontal=12, vertical=12),
        on_click=on_click,
        ink=bool(on_click),
        key=key,
    )


def setting_row(
    name: str,
    value: str,
    t: Tokens,
    on_click: Handler = None,
    reason: str | None = None,
    warn: str | None = None,
    key: str | None = None,
    last: bool = False,
    chevron: bool = True,
    value_color: str | None = None,
    icon=None,
) -> ft.Container:
    disabled = reason is not None
    left = [ft.Text(name, size=15, color=t.text3 if disabled else t.text)]
    if reason:
        left.append(ft.Text(reason, size=12, color=t.text3))
    if warn:
        left.append(ft.Text(warn, size=12, color=t.warn))
    row = [ft.Column(left, spacing=2, expand=True)]
    if icon:
        row.insert(0, ft.Icon(icon, color=t.text, size=20))
    if value or disabled:
        row.append(
            ft.Text("—" if disabled else value, size=14, color=t.text3 if disabled else (value_color or t.text2))
        )
    if chevron:
        row.append(ft.Icon(ft.Icons.CHEVRON_RIGHT, color=t.muted, size=18))
    return ft.Container(
        content=ft.Row(row, spacing=10),
        padding=ft.Padding.symmetric(horizontal=14, vertical=14 if not (reason or warn) else 9),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
        on_click=None if disabled else on_click,
        ink=not disabled,
        key=key,
    )


def segmented(
    options: list[tuple[str, str]],
    selected: str,
    on_select: Callable[[str], None],
    t: Tokens,
    height: int = 42,
    key: str | None = None,
    accent: bool = False,
    text_size: int = 14,
    inset: bool = False,
) -> ft.Container:
    """`inset` is the smaller variant for inside a card: a darker track, so it still reads as a switch."""
    cells = []
    for value, caption in options:
        on = value == selected
        cells.append(
            ft.Container(
                content=ft.Text(
                    caption,
                    size=text_size,
                    weight=ft.FontWeight.W_600 if on else None,
                    color=(t.on_accent if accent else t.text) if on else t.text2,
                ),
                bgcolor=(t.accent if accent else t.surface2) if on else None,
                border_radius=8 if inset else 10,
                alignment=ft.Alignment.CENTER,
                expand=True,
                on_click=lambda e, v=value: on_select(v),
                ink=True,
                key=f"{key}-{value}" if key else None,
            )
        )
    return ft.Container(
        content=ft.Row(cells, spacing=3),
        bgcolor=t.ground if inset else t.surface,
        border_radius=10 if inset else 13,
        height=height,
        padding=ft.Padding.all(3),
        key=key,
    )


def shutter(recording: bool, on_click: Handler, t: Tokens, disabled: bool = False, size: int = 88) -> ft.Container:
    ring = t.muted if disabled else (t.rec if recording else t.text)
    core = (
        ft.Container(width=size * 0.36, height=size * 0.36, border_radius=8, bgcolor=t.rec)
        if recording
        else ft.Container(
            width=size * 0.75, height=size * 0.75, border_radius=size, bgcolor=t.muted if disabled else t.rec
        )
    )
    return ft.Container(
        content=core,
        width=size,
        height=size,
        border_radius=size,
        border=ft.Border.all(4, ring),
        alignment=ft.Alignment.CENTER,
        on_click=None if disabled else on_click,
        ink=True,
        tooltip="Stop recording" if recording else "Start recording",
        key="shutter",
    )


def icon_button(
    icon,
    label_text: str,
    on_click: Handler,
    t: Tokens,
    bgcolor: str | None = None,
    color: str | None = None,
    height: int = 44,
    icon_size: int = 24,
) -> ft.Control:
    """An icon-only button that screen readers (and UI tests) can find and press by its label.
    (A Semantics wrapper around IconButton is labelled but not pressable through accessibility.)"""
    return ft.Container(
        content=ft.Icon(icon, color=color or t.text, size=icon_size),
        width=44,
        height=height,
        border_radius=22,
        bgcolor=bgcolor,
        alignment=ft.Alignment.CENTER,
        on_click=on_click,
        ink=True,
        tooltip=label_text,
    )


def round_icon(
    icon, on_click: Handler, t: Tokens, tooltip: str, size: int = 56, key: str | None = None
) -> ft.Container:
    return ft.Container(
        content=ft.Icon(icon, color=t.text, size=24),
        width=size,
        height=size,
        border_radius=size,
        bgcolor=t.surface2,
        alignment=ft.Alignment.CENTER,
        on_click=on_click,
        ink=True,
        tooltip=tooltip,
        key=key,
    )


def mark(size: int = 32) -> ft.Control:
    """The Quak logo: a yellow duck with a camera lens for an eye (made by tools/make_icons.py)."""
    return ft.Image(src="logo.png", width=size, height=size, fit=ft.BoxFit.CONTAIN)

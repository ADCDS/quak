"""More > Lab: what is on a rooted camera's USB-C port, and the few things the Lab can do with it.

Building this screen does no I/O: whatever reaches the camera runs from a button, through ``app.run`` and
``app.lab_service``. Actions run one at a time, SSH's included: they share the camera's Wi-Fi, and the first to finish
would let it go. Each action is its own trip to the camera, which over Wi-Fi means joining it again, so an action only
reads back what it can't work out from its own answer.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import flet as ft

from quak.core.errors import AdapterInUse, LabError, NetworkError
from quak.gui import kit
from quak.gui.screens.common import _camera_name, _size
from quak.gui.screens.ssh import SMALL_BUTTON, _address, _ask_secret, _buttons, _card_head, _padded
from quak.i18n import N_, term, tr
from quak.lab.capabilities.audio import MAX_SECONDS
from quak.lab.capabilities.network import _LOOPBACK_NAME, _dns_servers, _gateway, _static_address
from quak.lab.capabilities.serial import microcom_command
from quak.lab.devices import classify
from quak.lab.hostmode import LAB_MOUNTS
from quak.lab.models import LabStatus
from quak.lab.toolkit import core_manifest

SUB_TABS = (
    ("overview", N_("Overview")),
    ("usb", N_("USB")),
    ("audio", N_("Audio")),
    ("storage", N_("Storage")),
    ("network", N_("Network")),
    ("display", N_("Display")),
    ("serial", N_("Serial")),
    ("system", N_("System")),
)
KINDS = {
    "audio": N_("Audio"),
    "storage": N_("Storage"),
    "network": N_("Network"),
    "hid": N_("Input device"),
    "video": N_("Video"),
    "hub": N_("Hub"),
    "serial": N_("Serial"),
    "vendor": N_("Vendor-specific"),
    "other": N_("Other"),
}
INTERFACE_KINDS = {
    "ethernet": N_("Ethernet"),
    "wifi": N_("Wi-Fi"),
    "usb": N_("USB"),
    "other": N_("Other"),
}
DISPLAY_SUPPORT = (
    (N_("Media Mod HDMI"), N_("Supported"), "ok"),
    (N_("USB DisplayLink"), N_("Experimental"), "warn"),
    (N_("USB-C HDMI/DP"), N_("Not supported"), "danger"),
)
FRAMEBUFFER_STATES = {"running": N_("Running"), "suspended": N_("Suspended")}
DEFAULT_SECONDS = 30
DEFAULT_PREFIX = 24


def lab(app) -> ft.Control:
    t = app.t
    address = _address(app)
    error = _found(app, "error")
    if app.lab["tab"] == "overview" and error == _found(app, "toolkit_error"):
        error = None  # the Toolkit card on this tab says it
    tabs = {
        "overview": _overview,
        "usb": _usb,
        "audio": _audio,
        "storage": _storage,
        "network": _network,
        "display": _display,
        "serial": _serial,
        "system": _system,
    }
    return ft.Column(
        [
            _header(app, address),
            _tabs(app),
            *_no_camera(app, address),
            *([kit.banner(error, t, "danger", key="lab-error")] if error else []),
            *tabs[app.lab["tab"]](app),
        ],
        spacing=10,
    )


def _found(app, name: str):
    """What was read from the camera in hand: what was found out about another one doesn't count."""
    s = app.lab
    return s[name] if s["address"] == _address(app) else None


def _usable(app) -> bool:
    return bool(_address(app)) and not (app.lab["busy"] or app.ssh["busy"])


def _header(app, address: str | None) -> ft.Control:
    t = app.t
    return ft.Container(
        ft.Column(
            [kit.title(tr("Camera Lab"), t, 20), kit.text(_camera_name(app) or address or "—", t, 13, t.text2)],
            spacing=2,
        ),
        padding=ft.Padding.only(left=4),
    )


def _tabs(app) -> ft.Control:
    s = app.lab
    options = [(value, term(name)) for value, name in SUB_TABS]
    rows = [options] if app.wide else [options[:4], options[4:]]  # eight don't fit across a phone
    return ft.Column(
        [
            ft.Container(  # so that the rows, which share a key, aren't siblings
                kit.segmented(
                    row,
                    s["tab"],
                    lambda v: (s.update(tab=v), app.render()),
                    app.t,
                    height=36,
                    key="lab-tab",
                    text_size=13,
                )
            )
            for row in rows
        ],
        spacing=4,
    )


def _no_camera(app, address: str | None) -> list[ft.Control]:
    t = app.t
    if app.sim:
        return [
            kit.banner(
                tr("The Camera Lab needs a real camera: the simulated one in demo mode doesn't have it."), t, "warn"
            )
        ]
    if address is None:
        return [kit.banner(tr("Not connected."), t, "warn", on_click=lambda e: app.go("cameras"))]
    return []


def _message(value: str, t) -> ft.Control:
    return kit.card(kit.text(value, t, 14, t.text2), t)


def _button(app, make, caption: str, fn, key: str, *args) -> ft.Control:
    return make(caption, lambda e: _start(app, fn, *args), app.t, key=key, disabled=not _usable(app), **SMALL_BUTTON)


def _row_of(*buttons: ft.Control) -> ft.Control:
    return ft.Row(list(buttons), spacing=8)


# ---------------------------------------------------------------- Overview


def _port_look(t, state) -> tuple[str, str, str]:
    if state is None:
        return tr("Not checked"), t.surface2, t.text2
    if not state.supported:  # no switch on this camera, or its state couldn't be read: state.error says which
        return tr("Unavailable"), t.danger_tint, t.danger_text
    if state.enabled:
        return tr("On"), t.ok_tint, t.ok
    if state.partial:
        return tr("Between modes"), t.warn_tint, t.warn_text
    return tr("Off"), t.surface2, t.text2


def _toolkit_value(pack) -> str:
    """What the card says. Whether the modules are loaded stays out of it: reading the card doesn't ask the kernel."""
    if pack is None:
        return "—"
    if pack.error:
        return tr("Problem")
    return tr("Installed") if pack.installed else tr("Not installed")


def _overview(app) -> list[ft.Control]:
    t = app.t
    status, devices = _found(app, "status"), _found(app, "devices")
    state, pack = (status.host_mode, status.toolkit) if status else (None, None)
    value, bg, fg = (tr("Running"), t.accent_tint, t.accent_ink) if app.lab["busy"] else _port_look(t, state)
    plugged_in = sum(classify(device) != "hub" for device in devices) if devices is not None else None
    unfinished = state and (state.marker == "switching" or (state.marker and state.partial))
    problems = [
        (state.error if state else "", "lab-port-error"),
        (
            tr("A host-mode switch did not finish. To finish it, run: quak lab host recover") if unfinished else "",
            "lab-port-unfinished",
        ),
        (status.error if status else "", "lab-devices-error"),
    ]
    switchable = bool(state and state.supported)
    return [
        kit.card(
            ft.Column(
                [
                    _card_head(tr("USB-C port"), kit.pill(value, t, bg=bg, fg=fg, bold=True), t),
                    kit.setting_row(
                        tr("Devices (hubs not counted)"),
                        "—" if plugged_in is None else str(plugged_in),
                        t,
                        chevron=False,
                    ),
                    kit.setting_row(tr("Toolkit"), _toolkit_value(pack), t, chevron=False),
                    kit.setting_row(tr("Battery"), (status.battery if status else "") or "—", t, chevron=False),
                    *[_padded(kit.banner(text, t, "danger", key=key)) for text, key in problems if text],
                    _buttons(
                        _button(app, kit.secondary, tr("Refresh"), _refresh, "lab-refresh"),
                        kit.primary(
                            tr("Enable host"),
                            lambda e: _ask_host_on(app),
                            t,
                            key="lab-host-on",
                            # a port between modes has to come back first, and only once its state has been seen
                            disabled=not (_usable(app) and switchable and not state.enabled and not state.partial),
                            **SMALL_BUTTON,
                        ),
                        kit.destructive(
                            tr("Exit host"),
                            lambda e: _ask_host_off(app),
                            t,
                            outlined=True,
                            key="lab-host-off",
                            disabled=not (_usable(app) and switchable and (state.enabled or state.partial)),
                            **SMALL_BUTTON,
                        ),
                    ),
                ],
                spacing=0,
            ),
            t,
            padding=0,
        ),
        _toolkit_card(app, pack),
    ]


def _toolkit_look(app, pack) -> tuple[str, str, str]:
    t = app.t
    if app.lab["busy"]:
        return tr("Running"), t.accent_tint, t.accent_ink
    if pack is None:
        return tr("Not checked"), t.surface2, t.text2
    if pack.error:
        return tr("Problem"), t.danger_tint, t.danger_text
    if not pack.installed:
        return tr("Not installed"), t.surface2, t.text2
    if _found(app, "toolkit_loaded"):
        return tr("Loaded"), t.ok_tint, t.ok
    return tr("Installed"), t.surface2, t.text2


def _toolkit_card(app, pack) -> ft.Control:
    """The pack on the camera's card and in its kernel. Each button asks first: they all change the camera."""
    t = app.t
    value, bg, fg = _toolkit_look(app, pack)
    installed = bool(pack and pack.installed and not pack.error)
    loaded = _found(app, "toolkit_loaded")
    result, failed = _found(app, "toolkit_result"), _found(app, "toolkit_error")
    shown = [
        (pack.error if pack else "", "danger", "lab-toolkit-error"),
        (failed or "", "danger", "lab-toolkit-failed"),
        (result or "", "ok", "lab-toolkit-result"),
    ]
    modules = tr("Not checked") if loaded is None else tr("Loaded") if loaded else tr("Not loaded")
    usable = _usable(app)
    return kit.card(
        ft.Column(
            [
                _card_head(tr("Toolkit"), kit.pill(value, t, bg=bg, fg=fg, bold=True), t),
                *([kit.setting_row(tr("Kernel modules"), modules, t, chevron=False)] if installed else []),
                *[_padded(kit.banner(text, t, kind, key=key)) for text, kind, key in shown if text],
                _buttons(
                    kit.primary(
                        tr("Install"),
                        lambda e: _ask_toolkit_install(app),
                        t,
                        key="lab-toolkit-install",
                        disabled=not usable,
                        **SMALL_BUTTON,
                    ),
                    kit.secondary(
                        tr("Load"),
                        lambda e: _ask_toolkit_load(app),
                        t,
                        key="lab-toolkit-load",
                        disabled=not (usable and installed),
                        **SMALL_BUTTON,
                    ),
                    kit.secondary(
                        tr("Unload"),
                        lambda e: _ask_toolkit_unload(app),
                        t,
                        key="lab-toolkit-unload",
                        disabled=not usable,  # the pack's modules can be in the kernel whatever the card says
                        **SMALL_BUTTON,
                    ),
                    kit.destructive(
                        tr("Remove"),
                        lambda e: _ask_toolkit_remove(app),
                        t,
                        outlined=True,
                        key="lab-toolkit-remove",
                        disabled=not (usable and installed),
                        **SMALL_BUTTON,
                    ),
                ),
            ],
            spacing=0,
        ),
        t,
        padding=0,
    )


def _kernel_note() -> str:
    """What the toolkit was built for, which is all that decides whether it can be loaded. Not worth failing a dialog."""
    try:
        manifest = core_manifest()
    except LabError:
        return ""
    return tr(
        "The toolkit is built for kernel {kernel} (vermagic {vermagic}).",
        kernel=manifest.kernel,
        vermagic=manifest.vermagic,
    )


def _ask_toolkit_install(app) -> None:
    _ask(
        app,
        tr("Install the toolkit?"),
        tr("This copies the toolkit onto the camera's SD card. Nothing is loaded into the kernel until you load it."),
        tr("Install"),
        _toolkit_install,
    )


def _ask_toolkit_load(app) -> None:
    _ask(
        app,
        tr("Load the toolkit?"),
        " ".join(
            filter(
                None,
                [
                    tr("This loads the toolkit's modules into the camera's running kernel."),
                    _kernel_note(),
                    tr(
                        "A module built for another kernel can crash the camera, so Quak refuses to load the toolkit "
                        "unless the camera reports exactly that kernel."
                    ),
                ],
            )
        ),
        tr("Load"),
        _toolkit_load,
    )


def _ask_toolkit_unload(app) -> None:
    _ask(
        app,
        tr("Unload the toolkit?"),
        " ".join(
            filter(
                None,
                [
                    tr("This takes the toolkit's modules out of the camera's running kernel."),
                    _kernel_note(),
                    tr("USB devices that need them stop working until the toolkit is loaded again."),
                ],
            )
        ),
        tr("Unload"),
        _toolkit_unload,
    )


def _ask_toolkit_remove(app) -> None:
    _ask(
        app,
        tr("Remove the toolkit?"),
        tr("This unloads the toolkit's modules and deletes the toolkit from the camera's SD card."),
        tr("Remove"),
        _toolkit_remove,
    )


def _ask_host_on(app) -> None:
    _ask(
        app,
        tr("Enable host mode?"),
        tr("This switches the camera's USB-C port to host mode, which cuts off a computer connected to it."),
        tr("Enable host"),
        _host_on,
    )


def _ask_host_off(app) -> None:
    _ask(
        app,
        tr("Exit host mode?"),
        tr("This unmounts Lab volumes, unloads the toolkit and puts the USB-C port back in device mode."),
        tr("Exit host"),
        _host_off,
    )


def _ask(app, title: str, body: str, caption: str, fn, *args) -> None:
    def yes(e):
        app.close_dialog()
        _start(app, fn, *args)

    app.dialog(
        title,
        ft.Text(body, color=app.t.text2),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()), ft.TextButton(caption, on_click=yes)],
    )


# ---------------------------------------------------------------- USB devices


def _device_row(device, t, last: bool) -> ft.Control:
    kind = classify(device)
    drivers = sorted({interface.driver for interface in device.interfaces if interface.driver})
    facts = (
        term(KINDS.get(kind, KINDS["other"])),
        ", ".join(drivers) or device.driver or tr("No driver"),
        tr("{speed} Mb/s", speed=device.speed) if device.speed else "—",
    )
    return ft.Container(
        ft.Column(
            [
                ft.Row(
                    [
                        kit.text(device.product or device.manufacturer or "—", t, 15, expand=True),
                        kit.mono(device.id, t, 12, t.text3),
                    ]
                ),
                kit.text(" · ".join(facts), t, 12, t.text3),
            ],
            spacing=2,
        ),
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def _device_list(app, kind: str | None = None, nothing: str = "") -> ft.Control:
    t = app.t
    found = _found(app, "devices")
    if found is None:
        return _message(tr("Press Rescan to list what's on the camera's USB port."), t)
    if not found:  # not even a root hub, which the bus always has in host mode
        return _message(tr("The camera only has USB devices in host mode. Enable it in Overview."), t)
    listed = [device for device in found if kind is None or classify(device) == kind]
    if not listed:
        return _message(term(nothing), t)
    return kit.card(
        ft.Column([_device_row(device, t, device is listed[-1]) for device in listed], spacing=0), t, padding=0
    )


def _rescan_button(app) -> ft.Control:
    return _row_of(_button(app, kit.secondary, tr("Rescan"), _rescan, "lab-rescan"))


def _usb(app) -> list[ft.Control]:
    return [_device_list(app), _rescan_button(app)]


# ---------------------------------------------------------------- Display


def _framebuffer_row(framebuffer, t, last: bool) -> ft.Control:
    state = FRAMEBUFFER_STATES.get(framebuffer.state)
    bits = framebuffer.bits_per_pixel
    facts = (
        framebuffer.virtual_size or "—",
        tr("{bpp} bpp", bpp=bits) if bits else "—",
        term(state) if state else framebuffer.state or "—",
    )
    return ft.Container(
        ft.Column(
            [kit.text(framebuffer.name or "—", t, 15), kit.text(" · ".join(facts), t, 12, t.text3)],
            spacing=2,
        ),
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def _framebuffer_list(app) -> ft.Control:
    t = app.t
    found = _found(app, "framebuffers")
    if found is None:
        return _message(tr("Press Refresh to list the camera's framebuffers."), t)
    if not found:
        return _message(tr("No framebuffers found."), t)
    return kit.card(
        ft.Column([_framebuffer_row(framebuffer, t, framebuffer is found[-1]) for framebuffer in found], spacing=0),
        t,
        padding=0,
    )


def _display(app) -> list[ft.Control]:
    t = app.t
    looks = {"ok": (t.ok_tint, t.ok), "warn": (t.warn_tint, t.warn_text), "danger": (t.danger_tint, t.danger_text)}
    rows = [
        ft.Container(
            ft.Row(
                [
                    kit.text(term(name), t, 15, expand=True),
                    kit.pill(term(level), t, bg=looks[look][0], fg=looks[look][1], bold=True),
                ]
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            border=None if look == DISPLAY_SUPPORT[-1][2] else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
        )
        for name, level, look in DISPLAY_SUPPORT
    ]
    return [
        kit.card(ft.Column(rows, spacing=0), t, padding=0),
        kit.section(tr("Framebuffers"), t),
        _framebuffer_list(app),
        kit.text(tr("A framebuffer can be written only with a matching Linux driver."), t, 12, t.text3),
        _row_of(_button(app, kit.secondary, tr("Refresh"), _list_framebuffers, "lab-display-refresh")),
        kit.section(tr("Video devices"), t),
        _device_list(app, "video", N_("No USB video devices found.")),
        _rescan_button(app),
    ]


# ---------------------------------------------------------------- Serial


def _serial_row(app, port, selected: bool, last: bool) -> ft.Control:
    t = app.t
    facts = (port.driver or tr("No driver"), port.usb_ids or tr("Not on USB"))
    return ft.Container(
        ft.Column([kit.mono(port.device, t, 14), kit.text(" · ".join(facts), t, 12, t.text3)], spacing=2),
        key=f"lab-serial-{port.device}",
        on_click=lambda e: (app.lab.update(serial_port=port.device), app.render()),
        bgcolor=t.surface2 if selected else None,
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def _terminal_hint(port, t) -> list[ft.Control]:
    """Text to copy and nothing to press: opening a port can reset what's on it, so that's for the user to do."""
    return [
        kit.section(tr("Use in terminal"), t),
        kit.card(
            ft.Column(
                [
                    kit.mono(
                        microcom_command(port.device), t, 13, t.soft_text, selectable=True, key="lab-serial-command"
                    ),
                    kit.text(
                        tr(
                            "Run this in the SSH terminal: Quak won't run it for you. "
                            "Change -s if the device uses another speed. Ctrl-X exits."
                        ),
                        t,
                        12,
                        t.text3,
                    ),
                ],
                spacing=6,
            ),
            t,
        ),
    ]


def _serial(app) -> list[ft.Control]:
    t = app.t
    ports = _found(app, "serial")
    hint = []
    if ports is None:
        body = _message(tr("Press Refresh to list the camera's serial ports."), t)
    elif not ports:
        body = _message(
            tr("No serial ports found. A USB adapter needs host mode on and a driver for it on the camera."), t
        )
    else:
        selected = next((port for port in ports if port.device == app.lab["serial_port"]), ports[0])
        body = kit.card(
            ft.Column([_serial_row(app, port, port is selected, port is ports[-1]) for port in ports], spacing=0),
            t,
            padding=0,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,  # the selected row's colour would show past the rounded corners
        )
        hint = _terminal_hint(selected, t)
    return [body, *hint, _row_of(_button(app, kit.secondary, tr("Refresh"), _list_serial, "lab-serial-refresh"))]


# ---------------------------------------------------------------- Audio


def _capture_cards(found) -> list[list]:
    """The entries of each card that can capture. A card has one per format, but a capture is from the whole card."""
    cards: dict[int, list] = {}
    for device in found:
        if device.capture_format:
            cards.setdefault(device.card, []).append(device)
    return list(cards.values())


def _sound_card_row(app, entries: list, last: bool) -> ft.Control:
    t, first = app.t, entries[0]
    card = first.card
    facts = (
        tr("Card {card}, device {device}", card=card, device=first.device),
        ", ".join(dict.fromkeys(device.capture_format for device in entries)),
    )
    return ft.Container(
        ft.Column(
            [
                kit.text(first.name or "—", t, 15),
                kit.text(" · ".join(facts), t, 12, t.text3),
                _row_of(
                    kit.secondary(
                        tr("Record mic"),
                        lambda e: _ask_seconds(app, tr("Record mic"), _record_mic, card),
                        t,
                        key=f"lab-audio-mic-{card}",
                        disabled=not _usable(app),
                        **SMALL_BUTTON,
                    ),
                    kit.secondary(
                        tr("Record with camera"),
                        lambda e: _ask_record_with_camera(app, card),
                        t,
                        key=f"lab-audio-camera-{card}",
                        disabled=not _usable(app),
                        **SMALL_BUTTON,
                    ),
                ),
            ],
            spacing=4,
        ),
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def _saved_banner(saved: dict, t) -> ft.Control:
    where = f"{saved['path']} ({_size(saved['size'])})"
    return kit.banner(tr("Saved {path}", path=where), t, "ok", key="lab-audio-saved")


def _audio(app) -> list[ft.Control]:
    t = app.t
    found, saved = _found(app, "audio"), _found(app, "audio_saved")
    cards = _capture_cards(found or [])
    if found is None:
        body = _message(tr("Press Refresh to list the camera's sound cards."), t)
    elif not cards:
        body = _message(
            tr(
                "No USB audio cards that can record. They need host mode on and the toolkit loaded "
                "(quak lab toolkit load)."
            ),
            t,
        )
    else:
        body = kit.card(
            ft.Column([_sound_card_row(app, entries, entries is cards[-1]) for entries in cards], spacing=0),
            t,
            padding=0,
        )
    return [
        body,
        *([_saved_banner(saved, t)] if saved else []),
        _row_of(
            _button(app, kit.secondary, tr("Refresh"), _list_audio, "lab-audio-refresh"),
            *([kit.pill(tr("Running"), t, bg=t.accent_tint, fg=t.accent_ink, bold=True)] if app.lab["busy"] else []),
        ),
    ]


def _typed_seconds(value: str | None) -> int | None:
    """What was typed, if a capture can last that long."""
    try:
        seconds = int(value or "")
    except ValueError:
        return None
    return seconds if 1 <= seconds <= MAX_SECONDS else None


def _ask_seconds(app, title: str, fn, card: int) -> None:
    allowed = tr("Seconds (1 to {max})", max=MAX_SECONDS)

    def record(e):
        seconds = _typed_seconds(field.value)
        if seconds is None:
            field.error = allowed
            app.page.update()
            return
        app.close_dialog()
        _start(app, fn, card, seconds)

    field = ft.TextField(
        value=str(DEFAULT_SECONDS),
        label=allowed,
        keyboard_type=ft.KeyboardType.NUMBER,
        autofocus=True,
        on_submit=record,
    )
    app.dialog(
        title,
        field,
        [
            ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()),
            ft.TextButton(tr("Record"), on_click=record),
        ],
    )


def _camera_refusal(app) -> str:
    """Why the camera can't record alongside the mic, if it can't. A recording already running isn't ours to stop."""
    state = app.svc.state
    if not state.connected:
        return tr("Not connected. Connect from the Cameras tab.")
    return tr("The camera is already recording. Stop it first.") if state.recording else ""


def _ask_record_with_camera(app, card: int) -> None:
    refusal = _camera_refusal(app)
    if refusal:
        app.toast(refusal, error=True)
    else:
        _ask_seconds(app, tr("Record with camera"), _record_with_camera, card)


# ---------------------------------------------------------------- Storage


def _volume_row(app, volume, last: bool) -> ft.Control:
    t = app.t
    facts = (volume.device, volume.fs or tr("Unknown filesystem"), _size(volume.size))
    button = None
    if not volume.mounted:
        where = tr("Not mounted")
        button = _button(app, kit.secondary, tr("Mount read-only"), _mount, f"lab-mount-{volume.device}", volume.device)
    else:
        where = (
            tr("Mounted at {path} (read-only)", path=volume.mountpoint)
            if volume.read_only
            else tr("Mounted at {path} (read-write)", path=volume.mountpoint)
        )
        if volume.mountpoint.startswith(LAB_MOUNTS):  # anything else, the SD card included, the backend would refuse
            button = _button(
                app, kit.secondary, tr("Unmount"), _unmount, f"lab-unmount-{volume.device}", volume.mountpoint
            )
    return ft.Container(
        ft.Column(
            [
                kit.text(volume.label or volume.device, t, 15),
                kit.text(" · ".join(facts), t, 12, t.text3),
                kit.text(where, t, 12, t.text2),
                *([button] if button else []),
            ],
            spacing=4,
        ),
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def _storage(app) -> list[ft.Control]:
    t = app.t
    volumes = _found(app, "volumes")
    if volumes is None:
        body = _message(tr("Press Refresh to list the camera's volumes."), t)
    elif not volumes:
        body = _message(tr("No volumes found."), t)
    else:
        body = kit.card(
            ft.Column([_volume_row(app, volume, volume is volumes[-1]) for volume in volumes], spacing=0), t, padding=0
        )
    return [body, _row_of(_button(app, kit.secondary, tr("Refresh"), _list_volumes, "lab-storage-refresh"))]


# ---------------------------------------------------------------- Network


def _interface_row(app, interface, last: bool) -> ft.Control:
    t, name = app.t, interface.name
    state, bg, fg = (tr("Up"), t.ok_tint, t.ok) if interface.up else (tr("Down"), t.surface2, t.text2)
    buttons = _row_of(
        kit.secondary(
            tr("Use DHCP"),
            lambda e: _ask_dhcp(app, name),
            t,
            key=f"lab-network-dhcp-{name}",
            disabled=not _usable(app),
            **SMALL_BUTTON,
        ),
        kit.secondary(
            tr("Static…"),
            lambda e: _ask_static(app, name),
            t,
            key=f"lab-network-static-{name}",
            disabled=not _usable(app),
            **SMALL_BUTTON,
        ),
    )
    return ft.Container(
        ft.Column(
            [
                ft.Row([kit.text(name, t, 15, expand=True), kit.pill(state, t, bg=bg, fg=fg, bold=True)]),
                kit.text(term(INTERFACE_KINDS.get(interface.kind, INTERFACE_KINDS["other"])), t, 12, t.text3),
                kit.mono(", ".join(interface.addresses), t, 12, t.text2, selectable=True)
                if interface.addresses
                else kit.text(tr("No address"), t, 12, t.text2),
                *([buttons] if name != _LOOPBACK_NAME else []),  # the backend would refuse loopback
            ],
            spacing=4,
        ),
        key=f"lab-network-row-{name}",
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def _network_result_text(result: dict) -> str:
    name, addresses = result["interface"], ", ".join(result["addresses"])
    lines = [
        tr(
            "{name} set up by DHCP: {addresses}. The address lasts until the camera restarts.",
            name=name,
            addresses=addresses,
        )
        if result["mode"] == "dhcp"
        else tr(
            "{name} set up by hand: {addresses}. The address lasts until the camera restarts.",
            name=name,
            addresses=addresses,
        ),
        tr("Gateway {gateway}", gateway=result["gateway"]) if result["gateway"] else "",
        tr("DNS {dns}", dns=", ".join(result["dns"])) if result["dns"] else "",
    ]
    return "\n".join(filter(None, lines))


def _network(app) -> list[ft.Control]:
    t = app.t
    found, result = _found(app, "network"), _found(app, "network_result")
    if found is None:
        body = _message(tr("Press Refresh to list the camera's network interfaces."), t)
    elif not found:
        body = _message(tr("No network interfaces found."), t)
    else:
        body = kit.card(
            ft.Column([_interface_row(app, interface, interface is found[-1]) for interface in found], spacing=0),
            t,
            padding=0,
        )
    return [
        kit.banner(
            tr("Changing the interface that carries this SSH link can drop the link. Quak won't try again on its own."),
            t,
            "warn",
            key="lab-network-warning",
        ),
        body,
        *([kit.banner(_network_result_text(result), t, "ok", key="lab-network-result")] if result else []),
        _row_of(
            _button(app, kit.secondary, tr("Refresh"), _list_network, "lab-network-refresh"),
            *([kit.pill(tr("Running"), t, bg=t.accent_tint, fg=t.accent_ink, bold=True)] if app.lab["busy"] else []),
        ),
    ]


def _ask_dhcp(app, name: str) -> None:
    _ask(
        app,
        tr("Use DHCP on {name}?", name=name),
        tr(
            "{name} will ask its network for an address. If this SSH link runs over {name}, that can drop the link, "
            "and Quak won't try again on its own.",
            name=name,
        ),
        tr("Use DHCP"),
        _configure,
        name,
        {"dhcp": True},
    )


def _typed_prefix(value: str) -> int | None:
    """What was typed, if a subnet can have that prefix. ``int`` alone would also take 2_4, and other scripts' digits."""
    if not (value.isascii() and value.isdigit()):
        return None
    prefix = int(value)
    return prefix if 1 <= prefix <= 32 else None


def _static_settings(address: str, prefix: str, gateway: str, dns: str) -> tuple[dict, dict[str, str]]:
    """The arguments for ``network_configure``, and what's wrong with which field.

    These are the backend's own rules, so the answers can't drift apart. Asking them here keeps the dialog open to fix
    the one field, in the user's language, without a trip to the camera.
    """
    problems = {}
    length = _typed_prefix(prefix)
    if length is None:
        problems["prefix"] = tr("Enter a prefix from 1 to 32.")
    interface = None
    try:
        interface = _static_address(address, length or DEFAULT_PREFIX)
    except LabError:
        problems["address"] = tr("Enter an IPv4 address this interface can use.")
    if gateway and interface is not None and length is not None:
        try:
            _gateway(gateway, interface)
        except LabError:
            problems["gateway"] = tr("Enter an IPv4 address in the same subnet, other than the IP address.")
    servers = [server.strip() for server in dns.split(",")] if dns else None
    if servers:
        try:
            _dns_servers(servers)
        except LabError:
            problems["dns"] = tr("Enter IP addresses separated by commas.")
    return {"dhcp": False, "address": address, "prefix": length, "gateway": gateway or None, "dns": servers}, problems


def _ask_static(app, name: str) -> None:
    # None of these answers the dialog on Enter: that could send the address before the gateway was typed
    fields = {
        "address": ft.TextField(label=tr("IP address"), key="lab-static-address", autofocus=True),
        "prefix": ft.TextField(
            value=str(DEFAULT_PREFIX),
            label=tr("Prefix"),
            key="lab-static-prefix",
            keyboard_type=ft.KeyboardType.NUMBER,
        ),
        "gateway": ft.TextField(label=tr("Gateway (optional)"), key="lab-static-gateway"),
        "dns": ft.TextField(label=tr("DNS servers, comma-separated (optional)"), key="lab-static-dns"),
    }

    def apply(e):
        typed = {field: (control.value or "").strip() for field, control in fields.items()}
        settings, problems = _static_settings(**typed)
        if problems:
            for field, control in fields.items():
                control.error = problems.get(field)
            app.page.update()
            return
        app.close_dialog()
        _start(app, _configure, name, settings)

    app.dialog(
        tr("Static address on {name}", name=name),
        ft.Column(
            [
                ft.Text(
                    tr(
                        "This replaces the addresses on {name}. If this SSH link runs over {name}, that can drop the "
                        "link, and Quak won't try again on its own.",
                        name=name,
                    ),
                    color=app.t.text2,
                ),
                *fields.values(),
            ],
            tight=True,
            spacing=12,
        ),
        [
            ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()),
            ft.TextButton(tr("Apply"), on_click=apply),
        ],
        scrollable=True,  # five rows and a phone's keyboard don't fit on its screen
    )


# ---------------------------------------------------------------- System


def _system_text(status: dict) -> str:
    """What the camera could say, which may be nothing at all: ``system.status`` leaves the rest empty."""
    mem, df = status["mem"], status["df"]
    available = mem.get("available", mem.get("free"))
    lines = [
        tr("Up {time}", time=str(dt.timedelta(seconds=int(status["uptime"])))) if status["uptime"] is not None else "",
        tr("Load {load}", load=" ".join(f"{value:g}" for value in status["load"])) if status["load"] else "",
        tr("Memory: {available} available of {total}", available=_size(available), total=_size(mem["total"]))
        if available is not None and "total" in mem
        else "",
        tr(
            "SD card: {free} free of {total} ({percent}% used)",
            free=_size(df["free"]),
            total=_size(df["total"]),
            percent=df["percent"],
        )
        if df
        else "",
        *(f"{zone['type'] or zone['zone']}: {zone['celsius']:.1f} °C" for zone in status["temp"]),
    ]
    return "\n".join(filter(None, lines)) or tr("The camera reported nothing.")


def _system(app) -> list[ft.Control]:
    t = app.t
    status, logs = _found(app, "system"), _found(app, "logs")
    return [
        kit.card(
            kit.text(tr("Press Refresh to read the camera's status."), t, 14, t.text2)
            if status is None
            else kit.mono(_system_text(status), t, 12, t.soft_text, selectable=True),
            t,
        ),
        kit.section(tr("Kernel log"), t),
        ft.Container(
            ft.Column(
                [
                    kit.text(tr("Press Read logs to read the camera's kernel log."), t, 12, t.text3)
                    if logs is None
                    else kit.mono(logs or tr("The kernel log is empty."), t, 11, t.soft_text, selectable=True)
                ],
                scroll=ft.ScrollMode.AUTO,
                auto_scroll=True,
            ),
            bgcolor=t.log_bg,
            border_radius=14,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            border=ft.Border.all(1, t.card_border) if t.card_border else None,
            height=240,
        ),
        _row_of(
            _button(app, kit.secondary, tr("Refresh"), _read_status, "lab-system-refresh"),
            _button(app, kit.secondary, tr("Read logs"), _read_logs, "lab-logs"),
        ),
    ]


# ---------------------------------------------------------------- actions


def _start(app, fn, *args) -> None:
    app.run(_action, app, fn, *args)


async def _action(app, fn, *args) -> None:
    s = app.lab
    if s["busy"] or app.ssh["busy"]:
        return
    service = app.lab_service
    address = _address(app)
    if s["address"] != address:
        s.update(address=address, status=None, devices=None, volumes=None, system=None, logs=None)
        s.update(audio=None, audio_saved=None, network=None, network_result=None)
        s.update(framebuffers=None, serial=None, serial_port=None, toolkit_loaded=None)
    s.update(busy=True, error="", toolkit_result="", toolkit_error="")
    app.render()
    try:
        if app.ssh_manager.profile(address).auth == "password":
            password = await _ask_secret(app, tr("SSH password"))
            if password is None:
                return
            from quak.lab.service import LabService

            service = LabService(app.ssh_manager, address, password=password)  # for this action: nothing keeps it
        await fn(app, service, *args)
    except AdapterInUse:
        raise  # not a failure yet: App.run asks whether to take the adapter, then runs this again
    except Exception as exc:
        s["error"] = str(exc) or type(exc).__name__
        raise  # App.run shows it too
    finally:
        s["busy"] = False
        app.release_wifi_soon()


async def _refresh(app, service) -> None:
    status = await service.status()
    app.lab.update(status=status, devices=None if status.error else status.devices)


async def _toolkit_do(app, service, call, message: str, loaded: bool | None) -> None:
    """One toolkit action, then the status read again. What it did stays on the card even if reading fails."""
    try:
        await call()
    except AdapterInUse:
        raise  # nothing has reached the camera. App.run asks, and only a yes runs this again
    except Exception as exc:
        app.lab["toolkit_error"] = str(exc) or type(exc).__name__
        raise
    app.lab["toolkit_result"] = message
    if loaded is not None:  # installing doesn't say what the kernel has
        app.lab["toolkit_loaded"] = loaded
    await _refresh(app, service)


async def _toolkit_install(app, service) -> None:
    await _toolkit_do(app, service, service.toolkit_install, tr("The toolkit is installed."), None)


async def _toolkit_load(app, service) -> None:
    await _toolkit_do(app, service, service.toolkit_load, tr("The toolkit is loaded."), True)


async def _toolkit_unload(app, service) -> None:
    await _toolkit_do(app, service, service.toolkit_unload, tr("The toolkit is unloaded."), False)


async def _toolkit_remove(app, service) -> None:
    await _toolkit_do(app, service, service.toolkit_remove, tr("The toolkit is removed."), False)


async def _rescan(app, service) -> None:
    app.lab["devices"] = await service.devices()


async def _list_framebuffers(app, service) -> None:
    app.lab["framebuffers"] = await service.display_list()


async def _list_serial(app, service) -> None:
    app.lab["serial"] = await service.serial_list()


def _switched(app, result) -> None:
    """The answer has the port's state, and what was on the bus or mounted before the switch can't be relied on."""
    s = app.lab
    s.update(status=replace(s["status"] or LabStatus(), host_mode=result.state), devices=None, volumes=None, audio=None)
    s.update(network=None, network_result=None)  # USB network adapters come and go with the port
    s.update(framebuffers=None, serial=None)  # and so do USB displays and serial adapters
    s["toolkit_loaded"] = None  # leaving host mode unloads the toolkit, but a switch that stopped partway can't say
    if result.message:
        app.toast(result.message)


async def _host_on(app, service) -> None:
    _switched(app, await service.host_on())


async def _host_off(app, service) -> None:
    _switched(app, await service.host_off())


async def _list_volumes(app, service) -> None:
    app.lab["volumes"] = await service.storage_list()


async def _mount(app, service, device: str) -> None:
    await service.storage_mount(device, read_only=True)
    await _list_volumes(app, service)


async def _unmount(app, service, mountpoint: str) -> None:
    await service.storage_unmount(mountpoint)
    await _list_volumes(app, service)


async def _list_audio(app, service) -> None:
    app.lab["audio"] = await service.audio_list()


async def _record_mic(app, service, card: int, seconds: int) -> None:
    app.lab["audio_saved"] = None  # a capture that fails shouldn't look like it gave the last one's file
    app.lab["audio_saved"] = await service.audio_save(card, seconds)


async def _record_with_camera(app, service, card: int, seconds: int) -> None:
    refusal = _camera_refusal(app)  # the dialog was open for a while
    if refusal:
        raise LabError(refusal)
    svc = app.svc  # demo mode can swap it during the action, and this is the camera to stop
    app.lab["audio_saved"] = None
    await svc.shutter(True)
    try:
        app.lab["audio_saved"] = await service.audio_save(card, seconds)  # kept even if the camera then won't stop
    finally:
        await svc.shutter(False)


async def _list_network(app, service) -> None:
    interfaces = await service.network_list()
    app.lab.update(network=interfaces, network_result=None)


async def _configure(app, service, name: str, settings: dict) -> None:
    """One attempt. A link this drops is reported, not retried: only the user knows how to get back to the camera."""
    s = app.lab
    s["network_result"] = None  # a change that fails shouldn't look like it gave the last one's address
    if not any(interface.name == name for interface in s["network"] or []):  # the dialog was open for a while
        raise LabError(tr("{name} isn't on this camera's list. Press Refresh and check it first.", name=name))
    try:
        result = await service.network_configure(name, **settings)
    except AdapterInUse:
        raise  # nothing has reached the camera. App.run asks, and only a yes runs this again
    except Exception as exc:
        s["network"] = None  # it may have stopped partway or gone through, so only reading it again can tell
        if isinstance(exc, NetworkError):
            message = tr(
                "{error}. If that link runs over {name}, the change may have gone through anyway. "
                "Reconnect and press Refresh to check.",
                error=exc,
                name=name,
            )
            exc.args = (message,)
        raise
    changed = [
        replace(interface, up=True, addresses=result["addresses"]) if interface.name == name else interface
        for interface in s["network"]
    ]
    s.update(network=changed, network_result=result)


async def _read_status(app, service) -> None:
    app.lab["system"] = await service.system_status()


async def _read_logs(app, service) -> None:
    app.lab["logs"] = await service.system_logs()

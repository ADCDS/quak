"""More > SSH: the route to a rooted camera, its host key, the stored keys, and what the last actions did.

Building this screen does no I/O: whatever touches the network, a subprocess or the key store runs from a button,
through ``app.run``. Actions run one at a time, the Lab's included: they share the camera's Wi-Fi, and the first to
finish would let it go. An embedded terminal counts as one action for as long as it's open.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import flet as ft

from quak import config
from quak.core.errors import AdapterInUse, SshError
from quak.gui import kit
from quak.gui.screens.common import _camera_name
from quak.i18n import N_, term, tr
from quak.ssh import keys, profiles

TRANSPORTS ={"auto": N_("Automatic"), "wifi": N_("Wi-Fi"), "usb": N_("USB"), "host": N_("Host or IP")}
HOST_KEY_STATES = {"trusted": N_("Trusted"), "unknown": N_("Not trusted"), "changed": N_("Changed")}
SMALL_BUTTON = {"height": 50, "text_size": 14, "radius": 12}
TERMINAL_HEIGHT = 420  # the screen scrolls, so the terminal can't just take what's left
TERMINAL_FONT_SIZE = 14


@dataclass(eq=False)
class TerminalSession:
    """An embedded terminal on screen. On a Wi-Fi route it holds the camera's Wi-Fi until ``end_terminal``."""

    control: ft.Control
    svc: Any | None  # the service to let go of the Wi-Fi (demo mode can swap app.svc); None if the route didn't take it
    state: ft.Text


def ssh(app) -> ft.Control:
    t, s = app.t, app.ssh
    if s.get("picker") is None:
        s["picker"] = ft.FilePicker(on_result=lambda e: _key_picked(app, e))
        services = getattr(app.page, "services", None)
        if services is not None:
            services.append(s["picker"])
    address = _address(app)
    status = s["status"] if s["address"] == address else None  # what was found out about another camera doesn't count
    return ft.Column(
        [
            _header(app, address),
            *_no_camera(app, address),
            *_terminal(app),
            _connection(app, address, status),
            _host_key(app, address, status),
            kit.section(tr("Keys"), t),
            _keys(app, address),
            kit.section(tr("Activity"), t),
            _activity(app),
        ],
        spacing=10,
    )


def _address(app) -> str | None:
    """The camera to act on. USB and host routes don't need Bluetooth, so with none connected it's the last one."""
    if app.sim:
        return None
    return app.svc.cam.link.address if app.svc.cam else config.default_address()


def _header(app, address: str | None) -> ft.Control:
    t = app.t
    return ft.Container(
        ft.Column(
            [kit.title(tr("SSH"), t, 20), kit.text(_camera_name(app) or address or "—", t, 13, t.text2)],
            spacing=2,
        ),
        padding=ft.Padding.only(left=4),
    )


def _no_camera(app, address: str | None) -> list[ft.Control]:
    t = app.t
    if app.sim:
        return [kit.banner(tr("SSH needs a real camera: the simulated one in demo mode doesn't have it."), t, "warn")]
    if address is None:
        return [kit.banner(tr("Not connected."), t, "warn", on_click=lambda e: app.go("cameras"))]
    return []


def _status_look(t, status) -> tuple[str, str, str]:
    if status is None:
        return tr("Not checked"), t.surface2, t.text2
    if status.ssh_up:
        return tr("SSH is up"), t.ok_tint, t.ok
    if status.reachable:
        return tr("SSH is off"), t.warn_tint, t.warn_text
    if status.endpoint and status.endpoint.transport == "wifi":  # checking never joins it, so this says nothing yet
        return tr("Wi-Fi not joined"), t.surface2, t.text2
    return tr("Not reachable"), t.danger_tint, t.danger_text


def _route(status) -> tuple[str, str]:
    """The transport and host to show: the route in use, else what the profile asks for."""
    endpoint = status.endpoint if status else None
    if endpoint:
        return term(TRANSPORTS.get(endpoint.transport, endpoint.transport)), f"{endpoint.host}:{endpoint.ssh_port}"
    profile = status.profile if status else None
    if profile:
        return term(TRANSPORTS.get(profile.transport, profile.transport)), profile.host or "—"
    return "—", "—"


def _card_head(name: str, pill: ft.Control | None, t) -> ft.Control:
    return ft.Container(
        ft.Row([kit.text(name, t, 15, expand=True), *([pill] if pill else [])]),
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border=ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def _padded(control: ft.Control) -> ft.Control:
    return ft.Container(control, padding=ft.Padding.symmetric(horizontal=14, vertical=8))


def _buttons(*buttons: ft.Control) -> ft.Control:
    # Flet 1.1/Android renders a wrapped Row of buttons as an empty gray box, and a single row overflows a
    # phone's width, so lay them out two to a row.
    rows = [ft.Row(list(buttons[i : i + 2]), spacing=8) for i in range(0, len(buttons), 2)]
    return _padded(ft.Column(rows, spacing=8))


def _terminal(app) -> list[ft.Control]:
    t, session = app.t, app.ssh["terminal"]
    if session is None:
        return []
    return [
        kit.card(
            ft.Column(
                [
                    _card_head(tr("Terminal"), session.state, t),
                    ft.Container(session.control, height=TERMINAL_HEIGHT),
                    _buttons(
                        kit.destructive(
                            tr("Disconnect"),
                            lambda e: app.run(_disconnect_terminal, app, session),
                            t,
                            outlined=True,
                            key="ssh-terminal-disconnect",
                            **SMALL_BUTTON,
                        )
                    ),
                ],
                spacing=0,
            ),
            t,
            padding=0,
        )
    ]


def _auth_choice(app, address: str | None, status) -> list[ft.Control]:
    """How this camera logs in. It waits for a Refresh to find the profile, because building the screen does no I/O."""
    profile = status.profile if status else None
    if profile is None:
        return []
    return [
        _padded(
            kit.segmented(
                [("key", tr("Key")), ("password", tr("Password"))],
                profile.auth,
                lambda auth: _start(app, _set_auth, address, auth),
                app.t,
                height=36,
                key="ssh-auth",
                text_size=13,
                inset=True,
            )
        )
    ]


def _connection(app, address: str | None, status) -> ft.Control:
    t, s = app.t, app.ssh
    idle = not (s["busy"] or app.lab["busy"])
    usable = idle and bool(address)
    value, bg, fg = (tr("Running"), t.accent_tint, t.accent_ink) if s["busy"] else _status_look(t, status)
    transport, host = _route(status)
    problem = s["error"] or (status.last_error if status else "")

    def button(make, caption: str, fn, key: str, enabled: bool) -> ft.Control:
        return make(caption, lambda e: _start(app, fn, address), t, key=key, disabled=not enabled, **SMALL_BUTTON)

    return kit.card(
        ft.Column(
            [
                _card_head(tr("Connection"), kit.pill(value, t, bg=bg, fg=fg, bold=True), t),
                kit.setting_row(tr("Transport"), transport, t, chevron=False),
                kit.setting_row(tr("Host"), host, t, chevron=False),
                *_auth_choice(app, address, status),
                *([_padded(kit.banner(problem, t, "danger", key="ssh-error"))] if problem else []),
                _buttons(
                    button(kit.primary, tr("Arm"), _arm, "ssh-arm", usable),
                    button(kit.secondary, tr("Test"), _test, "ssh-test", usable),
                    button(kit.secondary, tr("Open terminal"), _open_terminal, "ssh-terminal", usable),
                    button(kit.secondary, tr("Refresh"), _refresh, "ssh-refresh", idle),
                ),
            ],
            spacing=0,
        ),
        t,
        padding=0,
    )


def _host_key(app, address: str | None, status) -> ft.Control:
    t, idle = app.t, not (app.ssh["busy"] or app.lab["busy"])
    seen = status.host_key if status else None
    state = status.host_key_state if status else None
    bg, fg = {
        "trusted": (t.ok_tint, t.ok),
        "changed": (t.danger_tint, t.danger_text),
    }.get(state, (t.warn_tint, t.warn_text))
    pill = kit.pill(term(HOST_KEY_STATES[state]), t, bg=bg, fg=fg, bold=True) if state else None
    return kit.card(
        ft.Column(
            [
                _card_head(tr("Host key"), pill, t),
                _padded(
                    kit.mono(f"{seen.key_type} {seen.fingerprint}", t, 12, t.text2, selectable=True)
                    if seen
                    else kit.text(tr("No host key seen yet."), t, 13, t.text3)
                ),
                *(
                    [
                        _padded(
                            kit.banner(
                                tr(
                                    "This camera's host key has changed. Someone may be intercepting the connection, or "
                                    "the camera's keys were regenerated. Forget the old key only if you expected this."
                                ),
                                t,
                                "danger",
                                key="ssh-host-changed",
                            )
                        )
                    ]
                    if state == "changed"
                    else []
                ),
                _buttons(
                    kit.primary(
                        tr("Trust"),
                        lambda e: _ask_trust(app, address, seen) if seen else _start(app, _review_host_key, address),
                        t,
                        key="ssh-trust",
                        disabled=not (idle and state == "unknown"),  # a changed key has to be forgotten first
                        **SMALL_BUTTON,
                    ),
                    kit.destructive(
                        tr("Forget"),
                        lambda e: _ask_forget(app, address),
                        t,
                        outlined=True,
                        key="ssh-forget",
                        disabled=not (idle and state in ("trusted", "changed")),
                        **SMALL_BUTTON,
                    ),
                ),
            ],
            spacing=0,
        ),
        t,
        padding=0,
    )


def _key_row(app, address: str | None, key, last: bool) -> ft.Control:
    t, idle = app.t, not (app.ssh["busy"] or app.lab["busy"])
    return ft.Container(
        ft.Column(
            [
                kit.text(key.label or key.id, t, 15),
                kit.mono(f"{key.type} {key.fingerprint}", t, 12, t.text3, selectable=True),
                ft.Row(
                    [
                        kit.secondary(
                            tr("Install on camera"),
                            lambda e: _ask_install_key(app, address, key),
                            t,
                            key=f"ssh-key-install-{key.id}",
                            disabled=not (idle and address),
                            **SMALL_BUTTON,
                        ),
                        kit.destructive(
                            tr("Forget"),
                            lambda e: _ask_forget_key(app, address, key),
                            t,
                            outlined=True,
                            key=f"ssh-key-forget-{key.id}",
                            disabled=not idle,
                            **SMALL_BUTTON,
                        ),
                    ],
                    spacing=8,
                ),
            ],
            spacing=6,
        ),
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
    )


def _keys(app, address: str | None) -> ft.Control:
    t = app.t
    found = app.ssh["keys"]
    if not found:
        message = (
            tr("Press Refresh to list Quak's SSH keys.")
            if found is None
            else tr("Quak has no SSH keys yet. Generate one here, or import a private key file.")
        )
        body = kit.card(kit.text(message, t, 14, t.text2), t)
    else:
        body = kit.card(
            ft.Column([_key_row(app, address, key, key is found[-1]) for key in found], spacing=0),
            t,
            padding=0,
        )
    return ft.Column(
        [
            _buttons(
                kit.secondary(
                    tr("Generate key"),
                    lambda e: _start(app, _generate_key, None),
                    t,
                    key="ssh-key-generate",
                    **SMALL_BUTTON,
                ),
                kit.secondary(
                    tr("Import key"),
                    None,
                    t,
                    key="ssh-key-import",
                    action=ft.PickFiles(app.ssh["picker"], allow_multiple=False, with_data=True, initial_directory="/sdcard/Download"),
                    **SMALL_BUTTON,
                ),
            ),
            body,
        ],
        spacing=10,
    )


def _activity(app) -> ft.Control:
    t = app.t
    rows = [
        ft.Row(
            [
                kit.mono(time.strftime("%H:%M:%S", time.localtime(when)), t, 11, t.text3),
                kit.text(message, t, 12, t.danger_text if failed else t.soft_text, expand=True, selectable=True),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        for when, message, failed in app.ssh["log"]
    ]
    return ft.Container(
        ft.Column(
            rows or [kit.text(tr("Nothing yet."), t, 12, t.text3)],
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
            auto_scroll=True,
        ),
        bgcolor=t.log_bg,
        border_radius=14,
        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        border=ft.Border.all(1, t.card_border) if t.card_border else None,
        height=180,
    )


# ---------------------------------------------------------------- dialogs


def _ask_trust(app, address: str, host_key) -> None:
    t = app.t

    def yes(e):
        app.close_dialog()
        _start(app, _trust, address, host_key.fingerprint)

    app.dialog(
        tr("Trust this host key?"),
        ft.Column(
            [
                kit.text(
                    tr(
                        "From now on, Quak only logs in to this camera while it offers this key. "
                        "Trust it if this is your camera."
                    ),
                    t,
                    14,
                    t.text2,
                ),
                kit.mono(f"{host_key.key_type} {host_key.fingerprint}", t, 12, selectable=True),
            ],
            spacing=10,
            tight=True,
        ),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()), ft.TextButton(tr("Trust"), on_click=yes)],
    )


def _ask_forget(app, address: str) -> None:
    def yes(e):
        app.close_dialog()
        _start(app, _forget_host, address)

    app.dialog(
        tr("Forget this host key?"),
        ft.Text(tr("Quak won't log in to this camera again until you trust its key again."), color=app.t.text2),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()), ft.TextButton(tr("Forget"), on_click=yes)],
    )


def _ask_install_key(app, address: str | None, key) -> None:
    if not address:  # the button is off then too: this holds if a tap gets through anyway
        _fail(app, tr("There's no camera to install the key on. Connect to one first."))
        app.render()
        return

    def yes(e):
        app.close_dialog()
        _start(app, _install_key, address, key)

    app.dialog(
        tr("Install this key on the camera?"),
        ft.Column(
            [
                kit.text(
                    tr(
                        "Quak adds this key to the camera's authorized keys and checks that it logs in. "
                        "To get in, it needs a login the camera already accepts."
                    ),
                    app.t,
                    14,
                    app.t.text2,
                ),
                kit.mono(f"{key.type} {key.fingerprint}", app.t, 12, selectable=True),
            ],
            spacing=10,
            tight=True,
        ),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()), ft.TextButton(tr("Install"), on_click=yes)],
    )


def _ask_forget_key(app, address: str | None, key) -> None:
    def yes(e):
        app.close_dialog()
        _start(app, _forget_key, address, key)

    app.dialog(
        tr("Forget this key?"),
        ft.Column(
            [
                kit.text(
                    tr(
                        "Quak deletes this key from this device. A camera it's installed on keeps it until it's "
                        "removed there."
                    ),
                    app.t,
                    14,
                    app.t.text2,
                ),
                kit.mono(f"{key.type} {key.fingerprint}", app.t, 12, selectable=True),
            ],
            spacing=10,
            tight=True,
        ),
        [ft.TextButton(tr("Cancel"), on_click=lambda e: app.close_dialog()), ft.TextButton(tr("Forget"), on_click=yes)],
    )


async def _ask_secret(app, title: str) -> str | None:
    """What was typed, or None if the question was turned down. It goes to the login at hand: nothing here keeps it."""
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[str | None] = loop.create_future()

    def reply(value: str | None) -> None:
        # Flet can call these from another thread. Closing the dialog reports a dismissal too, which finds this answered.
        loop.call_soon_threadsafe(lambda: answer.done() or answer.set_result(value))

    def connect(e) -> None:
        reply(field.value or "")
        field.value = ""
        app.close_dialog()

    def cancel(e) -> None:
        reply(None)
        app.close_dialog()

    field = ft.TextField(password=True, can_reveal_password=True, autofocus=True, on_submit=connect)
    app.dialog(
        title,
        field,
        [ft.TextButton(tr("Cancel"), on_click=cancel), ft.TextButton(tr("Connect"), on_click=connect)],
        on_dismiss=lambda e: reply(None),
    )
    return await answer


# ---------------------------------------------------------------- actions


def _start(app, fn, *args) -> None:
    app.run(_action, app, fn, *args)


async def _action(app, fn, *args) -> None:
    s = app.ssh
    if s["busy"] or app.lab["busy"]:
        return
    s.update(busy=True, error="")
    app.render()
    try:
        await fn(app, *args)
    except AdapterInUse:
        raise  # not a failure yet: App.run asks whether to take the adapter, then runs this again
    except Exception as exc:
        _fail(app, str(exc) or type(exc).__name__)
        raise  # App.run shows it too
    finally:
        s["busy"] = s["terminal"] is not None  # everything else waits for a terminal that's still open
        app.release_wifi_soon()


def _note(app, message: str, failed: bool = False) -> None:
    app.ssh["log"].append((time.time(), message, failed))


def _fail(app, message: str) -> None:
    app.ssh["error"] = message
    _note(app, message, failed=True)


async def _refresh(app, address: str | None) -> None:
    if address:
        status = await app.ssh_manager.status(address)
        app.ssh.update(status=status, address=address)
        if status.last_error:
            _note(app, status.last_error, failed=True)
        else:
            _note(app, tr("Checked: {state}", state=_status_look(app.t, status)[0]))
    app.ssh["keys"] = await asyncio.to_thread(keys.list_keys)


def _native_quak_ssh():
    """The Dart SSH service, when this build has the extension."""
    try:
        from quak_native import QuakSsh
    except ImportError:
        return None
    return QuakSsh()


async def _generate_key(app, _address: str | None) -> None:
    label = tr("Quak key on this device")
    native = _native_quak_ssh()
    if native is not None:
        made = await native.generate_key(comment="quak@phone", passphrase=None)
        key = await asyncio.to_thread(keys.add_generated, made["private_pem"], made["public_key"], label, comment="quak@phone")
    else:
        key = await asyncio.to_thread(keys.generate, label)
    app.ssh["keys"] = await asyncio.to_thread(keys.list_keys)
    _note(app, tr("Generated {fingerprint}.", fingerprint=key.fingerprint))
    app.render()


def _key_picked(app, e) -> None:
    files = getattr(e, "files", None) or []
    if not files:
        return
    picked = files[0]
    data = getattr(picked, "bytes", None)
    app.run(_action, app, _import_key_data, data, getattr(picked, "name", "") or tr("Imported key"))


async def _import_key_data(app, data: bytes | None, name: str) -> None:
    if not data:
        raise ValueError(tr("That file could not be read."))
    key = await asyncio.to_thread(_import_key_bytes, data, name)
    app.ssh["keys"] = await asyncio.to_thread(keys.list_keys)
    _note(app, tr("Imported {fingerprint}.", fingerprint=key.fingerprint))
    app.render()


async def _install_key(app, address: str | None, key) -> None:
    if not address:
        raise SshError(tr("There's no camera to install the key on. Connect to one first."))
    result = await app.ssh_manager.install_key(address, key.id)
    if result.verified:
        _note(app, result.message or tr("{fingerprint} is on the camera.", fingerprint=key.fingerprint))
    else:
        _fail(app, result.message or tr("The camera didn't confirm the key."))
    await _refresh(app, address)


async def _forget_key(app, address: str | None, key) -> None:
    removed, cleared = await asyncio.to_thread(_delete_stored_key, key.id)
    if cleared:
        _note(app, tr("The saved SSH profile no longer names that key."))
    _note(app, tr("Forgot {fingerprint}.", fingerprint=key.fingerprint) if removed else tr("That key was already gone."))
    await _refresh(app, address)


def _delete_stored_key(key_id: str) -> tuple[bool, bool]:
    """Delete a key from Quak's store, and whether it was there and a saved profile had to stop naming it.

    Profiles that name it stop naming it first, so none can be left naming a missing key.
    """
    cleared = False
    for profile in profiles.list_profiles():
        if profile.key_id == key_id:
            profiles.save(replace(profile, key_id=None))
            cleared = True
    return keys.delete_key(key_id), cleared


def _import_key_bytes(data: bytes, label: str):
    with tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False) as fh:
        fh.write(data)
        path = Path(fh.name)
    try:
        return keys.import_key(path, label)
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


async def _arm(app, address: str) -> None:
    result = await app.ssh_manager.arm(address)
    if result.accepted and result.ssh_up:
        _note(app, result.message or tr("SSH is up"))
    else:
        _fail(app, result.message or tr("The camera didn't bring SSH up."))
    await _refresh(app, address)


async def _set_auth(app, address: str, auth: str) -> None:
    """Save how this camera logs in, onto the profile as it's saved now. A password is asked for when it's needed."""
    profile = app.ssh_manager.profile(address)
    await asyncio.to_thread(profiles.save, replace(profile, auth=auth))
    await _refresh(app, address)


async def _test(app, address: str) -> None:
    native = _native_login_test()
    if native is not None:
        await _test_natively(app, address, native)
        return
    login = {}
    if app.ssh_manager.profile(address).auth == "password":
        password = await _ask_secret(app, tr("SSH password"))
        if password is None:
            return
        login["password"] = password
    result = await app.ssh_manager.test(address, **login)
    if result.ok:
        _note(app, tr("Login works: {who}", who=" · ".join(x for x in (result.uid, result.uname) if x) or "—"))
    else:
        _fail(app, result.error or tr("The camera didn't accept the login."))


def _native_login_test():
    """The Dart SSH client to test a login with where there's no system ssh, as on a phone. Else None."""
    if shutil.which("ssh") is not None:
        return None
    return _native_quak_ssh()


async def _test_natively(app, address: str, native) -> None:
    """Log in through the Dart client, with what the embedded terminal gets: the same credentials and questions.

    The manager only joins Wi-Fi for its own calls, so this holds it for the login, and only on a Wi-Fi route.
    """
    service = app.svc
    route = await app.ssh_manager.resolve(address)
    svc = service if route.transport == "wifi" else None
    if svc is not None:
        await svc.http()
    try:
        credentials = await app.ssh_manager.terminal_credentials(address)
        secrets = await _login_secrets(app, credentials)
        if secrets is None:
            return
        passphrase, password = secrets
        result = await native.test(
            credentials.host,
            credentials.ssh_port,
            credentials.username,
            password=password,
            private_key_pem=credentials.private_key_pem,
            passphrase=passphrase,
            expected_fingerprint=credentials.expected_fingerprint,
        )
    finally:
        if svc is not None:
            await svc.release_http()
    if result.get("ok"):
        who = " · ".join(x for x in (result.get("uid"), result.get("uname")) if x) or "—"
        _note(app, tr("Login works: {who}", who=who))
    else:
        _fail(app, result.get("error") or tr("The camera didn't accept the login."))


async def _login_secrets(app, credentials) -> tuple[str | None, str | None] | None:
    """The passphrase and password a Dart login needs, each asked for only if it's needed. None if one was turned down."""
    passphrase = password = None
    if credentials.passphrase_required:
        passphrase = await _ask_secret(app, tr("Passphrase for this SSH key"))
        if passphrase is None:
            return None
    if credentials.password_required:
        login = f"{credentials.username}@{credentials.host}"
        password = await _ask_secret(app, tr("Password for {ssid}", ssid=login))  # the Wi-Fi question's wording
        if password is None:
            return None
    return passphrase, password


def _terminal_control():
    """The embedded terminal's control, or None in a build without it, where the system ssh does the job."""
    try:
        from quak_native import QuakTerminal
    except ImportError:
        return None
    return QuakTerminal


async def _open_terminal(app, address: str) -> None:
    control = _terminal_control()
    if control is not None:
        await _open_embedded_terminal(app, address, control)
        return
    _note(app, tr("The SSH session runs in the terminal Quak was started from."))
    app.render()
    code = await app.ssh_manager.open_terminal(address)
    _note(app, tr("SSH session ended with exit code {code}.", code=code))


async def _open_embedded_terminal(app, address: str, control) -> None:
    """Put a terminal on screen, holding the Wi-Fi first if that's the route. USB and host routes must not join it.

    From the moment connect() returns, ``end_terminal`` lets go of whatever is held.
    """
    s = app.ssh
    service = app.svc
    route = await app.ssh_manager.resolve(address)
    svc = service if route.transport == "wifi" else None
    if svc is not None:
        await svc.http()
    opened = False
    try:
        credentials = await app.ssh_manager.terminal_credentials(address)
        secrets = await _login_secrets(app, credentials)
        if secrets is None:
            return
        passphrase, password = secrets
        if not app.on_ssh_tab:  # left while this was getting ready, so there's nowhere to show it
            return
        session = s["terminal"] = _new_terminal(app, svc, control, credentials, passphrase, password)
        app.render()  # connect() needs the control on the page
        try:
            await session.control.connect()
        finally:
            # The Dart side read these as connect() arrived, and nothing connects this control again.
            session.control.passphrase = session.control.password = session.control.private_key_pem = None
            with contextlib.suppress(Exception):  # already off the page, if the session has ended
                session.control.update()
        opened = True
    finally:
        if not opened:
            s["terminal"] = None
            if svc is not None:
                await svc.release_http()


def _new_terminal(
    app, svc: Any | None, control, credentials, passphrase: str | None, password: str | None
) -> TerminalSession:
    # The handlers have to be set at creation: the Dart side only sends events a handler existed for then.
    session = TerminalSession(
        control(
            host=credentials.host,
            port=credentials.ssh_port,
            username=credentials.username,
            private_key_pem=credentials.private_key_pem,
            passphrase=passphrase,
            password=password,
            expected_fingerprint=credentials.expected_fingerprint,
            font_size=TERMINAL_FONT_SIZE,
            on_connected=lambda e: _terminal_connected(app, session),
            on_disconnected=lambda e: app.run(end_terminal, app, session),
            on_error=lambda e: app.run(end_terminal, app, session, e.message),
        ),
        svc,
        kit.text(tr("Connecting…"), app.t, 13, app.t.text2),
    )
    return session


def _terminal_connected(app, session: TerminalSession) -> None:
    if app.ssh["terminal"] is not session:
        return
    _note(app, tr("Connected"))
    session.state.value = tr("Connected")
    with contextlib.suppress(Exception):  # control gone: the next render replaces it
        session.state.update()


async def _disconnect_terminal(app, session: TerminalSession) -> None:
    if app.ssh["terminal"] is not session:
        return
    try:
        await session.control.disconnect()
    finally:
        await end_terminal(app, session)  # disconnect() only sends an event for a session that had connected


async def end_terminal(app, session: TerminalSession, error: str = "") -> None:
    """Take a terminal off the screen and let go of any Wi-Fi. Only the first call counts: an error brings a second."""
    s = app.ssh
    if s["terminal"] is not session:
        return
    s["terminal"] = None
    if error:
        _fail(app, error)
    else:
        _note(app, tr("SSH session ended."))
    app.render()
    try:
        if session.svc is not None:
            await session.svc.release_http()
    finally:
        s["busy"] = False  # only now: an action started sooner could get the Wi-Fi that's being let go of


async def _review_host_key(app, address: str) -> None:
    """Over a Wi-Fi route that isn't joined, checking can't see the key. Fetch it, pinning nothing, to show it first."""
    _ask_trust(app, address, await app.ssh_manager.scan_host_key(address))


async def _trust(app, address: str, fingerprint: str) -> None:
    pinned = await app.ssh_manager.trust_host(address, fingerprint=fingerprint)  # refuses any other key
    _note(app, tr("Trusted {fingerprint}.", fingerprint=pinned.fingerprint))
    await _refresh(app, address)


async def _forget_host(app, address: str) -> None:
    forgotten = await app.ssh_manager.forget_host(address)
    _note(app, tr("Host key forgotten.") if forgotten else tr("No host key was pinned."))
    await _refresh(app, address)

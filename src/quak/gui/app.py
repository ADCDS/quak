"""Quak GUI (Flet). Runs on Linux, Windows and Android; every action goes through
quak.service.CameraService, the same backend the CLI uses.

    quak-gui                 # desktop window, real camera
    QUAK_SIM=1 quak-gui      # simulated camera
    quak-gui --web --port 8550 [--sim]   # in a browser (used for screenshots and UI tests)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from pathlib import Path

import flet as ft

from quak import config
from quak.connect import Target
from quak.core.errors import AdapterInUse, QuakError
from quak.core.registry import Status
from quak.gui import kit
from quak.gui import theme as th
from quak.net.wifi import recover_network
from quak.service import CameraService

TABS = [
    ("cameras", "Cameras", ft.Icons.PHOTO_CAMERA_OUTLINED),
    ("remote", "Remote", ft.Icons.RADIO_BUTTON_CHECKED),
    ("media", "Media", ft.Icons.GRID_VIEW),
    ("live", "Live", ft.Icons.SENSORS),
    ("more", "More", ft.Icons.MORE_HORIZ),
]
SUBSCREEN_TAB = {"add_camera": "cameras", "schedule": "cameras", "settings": "remote"}
# desktop rail, as in the design: Diagnostics has its own item; More (camera settings, about) sits at the bottom
RAIL = [
    ("cameras", "Cameras", ft.Icons.PHOTO_CAMERA_OUTLINED, {}),
    ("remote", "Remote", ft.Icons.RADIO_BUTTON_CHECKED, {}),
    ("media", "Media", ft.Icons.GRID_VIEW, {}),
    ("live", "Live", ft.Icons.SENSORS, {}),
    ("more", "Diagnostics", ft.Icons.TERMINAL, {"more_tab": "diagnostics"}),
]
RAIL_BOTTOM = ("more", "More", ft.Icons.MORE_HORIZ, {"more_tab": "camera"})
PREVIEW_FAILED = "The camera didn't start its preview. Try again in a moment."
NO_PICTURE = "No picture came from the camera. Try again. If it keeps happening, check that a firewall isn't blocking UDP port 8554."
PICTURE_WAIT_SECONDS = 30
PICTURE_CHECK_SECONDS = 1
WIFI_IDLE_SECONDS = 60
BACK = {"settings": "remote"}  # where Android's Back goes, when it isn't the Cameras screen
DESKTOP_LAYOUTS = {"remote", "media"}  # screens with their own desktop artboard; the rest keep a phone-width column
PHONE_COLUMN = 720


class App:
    def __init__(self, page: ft.Page, service: CameraService, sim: bool) -> None:
        self.page, self.svc, self.sim = page, service, sim
        self.t = th.DARK
        self.screen = "cameras"
        self.more_tab = "camera"
        self.form: dict = {}
        self.media: dict = {
            "files": [],
            "thumbs": {},
            "selected": set(),
            "filter": "all",
            "progress": None,
            "loading": False,
            "error": "",
            "selecting": False,
            "detail": None,
        }
        self.preview = {"on": False, "frame": None, "decoder": None, "error": ""}
        self.live: dict = {"task": None, "log": [], "networks": [], "scanning": False}
        self.diag: dict = {"filter": "all", "paused": False, "tab": "log", "e2e": None, "e2e_lines": []}
        self.add: dict = {"step": "instructions", "found": [], "error": "", "scanning": False, "seen": None}
        self._render_pending = False
        self._log_pending = False
        self._live_texts: list = []
        self._rendered_view: tuple | None = None
        self._media_view: tuple | None = None
        self._wifi_wait = 0
        self._watch(service)
        page.on_resize = lambda e: self.render_soon()

    def _state_changed(self, _state) -> None:
        # Rebuilding the Media screen makes every thumbnail redraw, so only do it when something it shows changed.
        if self.screen == "media":
            st = self.svc.state
            view = (st.connected, st.link, st.http, st.s(Status.TURBO))
            if view == self._media_view:
                return
            self._media_view = view
        self.render_soon()

    async def on_android_back(self, e) -> None:
        """Android's Back goes where the app's own Back does; only on the first screen does it leave the app."""
        if self.screen == "cameras":
            await e.control.confirm_pop(True)
            return
        await e.control.confirm_pop(False)
        self.go(BACK.get(self.screen, "cameras"))

    def _watch(self, service: CameraService) -> None:
        service.subscribe(self._state_changed)
        service.subscribe_log(
            lambda line: (
                self.log_soon()
                if self.screen == "more" and self.more_tab == "diagnostics" and not self.diag["paused"]
                else None
            )
        )

    @property
    def theme_pref(self) -> str:
        return config.get_pref("theme", "dark")  # dark first, as designed; light and system are options

    def apply_theme(self) -> None:
        pref = self.theme_pref
        light = pref == "light" or (pref == "system" and self.page.platform_brightness == ft.Brightness.LIGHT)
        self.t = th.LIGHT if light else th.DARK
        self.page.theme_mode = ft.ThemeMode.LIGHT if light else ft.ThemeMode.DARK
        self.page.theme, self.page.dark_theme = _material_theme(th.LIGHT), _material_theme(th.DARK)
        self.page.bgcolor = self.t.ground

    def set_theme(self, pref: str) -> None:
        config.set_pref("theme", pref)
        self.apply_theme()
        self.render()

    async def set_demo(self, on: bool) -> None:
        """Switch between the simulated camera and the real one (remembered across launches)."""
        config.set_pref("demo", on)
        await self.svc.disconnect()
        self.svc = build_service(on, self.page)
        self.sim = on
        self._watch(self.svc)
        self.media.update(files=[], thumbs={}, loaded=False)
        self.go("cameras")
        if on or config.default_address():
            await self.connect()

    # ---------------------------------------------------------------- plumbing

    @property
    def wide(self) -> bool:
        return (self.page.width or 412) >= th.WIDE

    def go(self, screen: str, **kw) -> None:
        if screen != "remote" and self.preview["on"]:
            self.run(self.stop_preview)
        self.screen = screen
        for k, v in kw.items():
            setattr(self, k, v)
        self.release_wifi_soon()
        self.render()

    def release_wifi_soon(self) -> None:
        """Hand the camera's Wi-Fi back a while after the last thing that needs it: it may be this computer's
        only network, and the camera's radio uses battery. Waiting makes moving between screens quick."""
        self._wifi_wait += 1
        wait = self._wifi_wait

        async def later():
            await asyncio.sleep(WIFI_IDLE_SECONDS)
            if wait != self._wifi_wait or self.screen == "media" or self.preview["on"] or self.media.get("progress"):
                return
            await self.svc.release_http()

        if self.svc.state.http:
            self.page.run_task(later)

    def run(self, fn, *args, ok: str | None = None, redraw: bool = True) -> None:
        async def task():
            try:
                await fn(*args)
                if ok:
                    self.toast(ok)
            except AdapterInUse as e:
                self._ask_to_take_adapter(e.device, lambda: self.run(fn, *args, ok=ok, redraw=redraw))
            except QuakError as e:
                print(f"quak: {getattr(fn, '__name__', fn)} failed: {e}", file=sys.stderr, flush=True)
                self.toast(str(e), error=True)
            except Exception as e:  # noqa: BLE001 - show, don't crash the UI
                traceback.print_exc()
                self.toast(f"{type(e).__name__}: {e}", error=True)
            if redraw:
                self.render()

        self.page.run_task(task)

    def _ask_to_take_adapter(self, device: str, retry) -> None:
        def yes(e):
            self.svc.allow_busy_adapter = True
            self.close_dialog()
            retry()

        self.dialog(
            f"Use {device} for the camera?",
            ft.Text(
                f"This computer is online through {device}. Joining the camera's Wi-Fi disconnects it until "
                "about a minute after you leave Media or stop the preview.",
                color=self.t.text2,
            ),
            [ft.TextButton("Cancel", on_click=lambda e: self.close_dialog()), ft.TextButton("Use it", on_click=yes)],
        )

    def toast(self, message: str, error: bool = False, duration: int = 4000) -> None:
        t = self.t
        self.page.show_dialog(
            ft.SnackBar(
                ft.Text(message, color=t.danger_text if error else t.ground),
                bgcolor=t.danger_tint if error else t.text,
                duration=duration,
            )
        )

    def dialog(self, title: str, content: ft.Control, actions: list[ft.Control]) -> ft.AlertDialog:
        d = ft.AlertDialog(
            title=ft.Text(title, color=self.t.text), content=content, actions=actions, bgcolor=self.t.surface
        )
        self.page.show_dialog(d)
        return d

    def close_dialog(self) -> None:
        self.page.pop_dialog()

    def close_all_dialogs(self) -> None:
        for _ in range(4):
            try:
                if not self.page.pop_dialog():
                    break
            except Exception:
                break

    def render_soon(self) -> None:
        if self._render_pending:
            return
        self._render_pending = True

        async def later():
            await asyncio.sleep(0.05)
            self._render_pending = False
            self.render()

        try:
            self.page.run_task(later)
        except Exception:
            self._render_pending = False

    def log_soon(self) -> None:
        """A new log line refreshes the log list, not the page. A rebuild per line (the keep-alive logs every 3 s) drops
        any tap that lands mid-rebuild: Flet discards events for the controls the rebuild replaced."""
        if self._log_pending:
            return
        self._log_pending = True

        async def later():
            from quak.gui.screens.more import log_rows

            await asyncio.sleep(0.05)
            self._log_pending = False
            view = self.diag.get("log_view")
            if view is None:
                return
            try:
                view.controls = log_rows(self)
                view.update()
            except Exception:  # noqa: BLE001 - control gone: the next render replaces it
                self.render_soon()

        try:
            self.page.run_task(later)
        except Exception:
            self._log_pending = False

    # ---------------------------------------------------------------- layout

    def live_text(self, fn, **style) -> ft.Text:
        """A text that the 1 s ticker refreshes in place (recording timer, countdowns). Rebuilding the whole
        page every second instead would cancel taps that land mid-rebuild, like the shutter's stop."""
        ctl = ft.Text(fn(), **style)
        self._live_texts.append((ctl, fn))
        return ctl

    def tick(self) -> bool:
        if not self._live_texts:
            return False
        for ctl, fn in list(self._live_texts):
            try:
                ctl.value = fn()
                ctl.update()
            except Exception:  # noqa: BLE001 - control gone: the next render replaces it
                self.render_soon()
                return True
        return True

    def _player_view(self) -> tuple:
        """What the Remote screen shows while the phone's video player runs, apart from battery and card time."""
        st = self.svc.state
        return (
            self.screen,
            self.page.width,
            self.preview.get("player"),
            self.preview.get("on"),
            st.link,
            st.http,
            st.recording,
            st.busy_action,
            st.s(Status.PRESET),
            tuple(sorted(st.settings.items())),
            st.schedule["armed"],
        )

    def render(self) -> None:
        from quak.gui import screens

        # A rebuild replaces the whole widget tree, which destroys a playing video (the phone's decoder never
        # got to show a frame). While the player runs, only rebuild when something on screen really changed.
        if self.preview.get("player") and self.screen == "remote":
            view = self._player_view()
            if view == self._rendered_view:
                return
            self._rendered_view = view
        else:
            self._rendered_view = None
        t = self.t
        self._live_texts = []
        self.diag["log_view"] = None  # the log screen sets it again
        build = getattr(screens, self.screen)
        content = build(self)
        if self.wide and self.screen not in DESKTOP_LAYOUTS:
            content = ft.Row(
                [ft.Container(content, width=PHONE_COLUMN)], vertical_alignment=ft.CrossAxisAlignment.STRETCH
            )
        body = ft.Container(content=content, expand=True, bgcolor=t.ground)
        tab = SUBSCREEN_TAB.get(self.screen, self.screen)
        index = [k for k, *_ in TABS].index(tab) if tab in [k for k, *_ in TABS] else 0
        if self.wide:
            self.page.navigation_bar = None
            rail = ft.Container(
                ft.Column(
                    [
                        ft.Container(kit.mark(40), padding=ft.Padding.only(bottom=10)),
                        *[self._rail_item(item, tab) for item in RAIL],
                        ft.Container(expand=True),
                        self._rail_item(RAIL_BOTTOM, tab),
                    ],
                    spacing=6,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                width=88,
                bgcolor=t.nav,
                padding=ft.Padding.symmetric(horizontal=8, vertical=16),
                border=ft.Border.only(right=ft.BorderSide(1, t.surface2)),
            )
            root = ft.Row([rail, body], expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        elif self.screen in SUBSCREEN_TAB:  # pushed screens (Add camera, Settings, Schedule) have no tab bar
            self.page.navigation_bar = None
            root = body
        else:
            self.page.navigation_bar = ft.NavigationBar(
                selected_index=index,
                bgcolor=t.nav,
                indicator_color=t.accent_tint,
                destinations=[
                    ft.NavigationBarDestination(
                        icon=ft.Icon(icon, color=t.text2, size=22),
                        selected_icon=ft.Icon(icon, color=t.nav_selected, size=22),
                        label=name,
                    )
                    for _, name, icon in TABS
                ],
                on_change=lambda e: self.go(TABS[e.control.selected_index][0]),
            )
            root = body
        self.page.controls.clear()
        self.page.add(ft.SafeArea(root, expand=True))
        self.page.update()

    def _rail_item(self, item, tab: str) -> ft.Control:
        t = self.t
        screen, name, icon, kw = item
        on = tab == screen and all(getattr(self, k) == v for k, v in kw.items())
        return ft.Container(
            ft.Column(
                [
                    ft.Icon(icon, color=t.nav_selected if on else t.text2, size=22),
                    ft.Text(
                        name,
                        size=12,
                        color=t.nav_selected if on else t.text2,
                        no_wrap=True,
                        weight=ft.FontWeight.W_600 if on else None,
                    ),
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            width=72,
            height=64,
            border_radius=14,
            bgcolor=t.accent_tint if on else None,
            on_click=lambda e: self.go(screen, **kw),
            ink=True,
            tooltip=name,
        )

    def on_key(self, e) -> None:
        """Desktop shortcuts on the Remote screen, as the design's hint says: Space records, H tags a HiLight."""
        if self.screen != "remote" or not self.svc.state.connected or e.ctrl or e.alt or e.meta:
            return
        if e.key == " ":
            self.run(self.svc.shutter, not self.svc.state.recording)
        elif e.key.upper() == "H":
            self.run(self.svc.hilight, ok="HiLight tagged")

    # ---------------------------------------------------------------- shared actions

    async def connect(self) -> None:
        await self.svc.connect()
        if not self.sim and self.svc.cam:
            config.remember_camera(
                self.svc.cam.link.address, model=self.svc.state.model, firmware=self.svc.state.firmware
            )

    async def start_preview(self) -> None:
        from quak.gui.preview import FrameDecoder

        port = 8554
        self.preview.update(on=True, error="", frame=None)
        self.render()

        def frame(data: bytes) -> None:
            first = self.preview["frame"] is None
            self.preview["frame"] = data
            image = self.preview.get("image")
            if first or image is None:
                self.render_soon()  # swap the placeholder for the image once
                return
            try:  # afterwards only the image changes: no full re-render per frame
                image.src = data
                image.update()
            except Exception:
                self.render_soon()

        decoder = FrameDecoder(port, frame, on_failed=lambda: self.run(self._no_picture))
        if not decoder.available():  # phones: no ffmpeg, so the video player (media_kit) plays the stream itself
            if not _video_player_available():
                self.preview.update(on=False, error="ffmpeg is needed to show the preview in this build")
                return
            # media_kit on the phone never opened udp:// itself: relay the stream to it over local HTTP
            from quak.net.stream import UdpHttpRelay

            relay = UdpHttpRelay(port)
            url = relay.start()
            try:
                await self.svc.preview_start(port)
            except Exception:
                relay.stop()
                self.preview.update(on=False, relay=None, error=PREVIEW_FAILED)
                raise
            self.preview.update(player=url, relay=relay, frame=None)
            self.render()
            return
        await decoder.start()
        self.preview["decoder"] = decoder
        try:
            await self.svc.preview_start(port)
        except Exception:
            await decoder.stop()
            self.preview.update(on=False, decoder=None, error=PREVIEW_FAILED)
            raise
        self.page.run_task(self._watch_for_picture, decoder)

    async def _watch_for_picture(self, decoder) -> None:
        """Without this, a stream that never arrives would leave "Starting preview…" on screen for good."""
        waited = 0
        while self.preview.get("decoder") is decoder and self.preview["frame"] is None:
            if waited >= PICTURE_WAIT_SECONDS:
                self.run(self._no_picture)
                return
            await asyncio.sleep(PICTURE_CHECK_SECONDS)
            if not self.svc.state.recording:  # the camera can't stream while it records
                waited += PICTURE_CHECK_SECONDS

    async def _no_picture(self) -> None:
        try:
            await self.stop_preview()
        finally:
            self.preview["error"] = NO_PICTURE

    async def stop_preview(self) -> None:
        decoder = self.preview.get("decoder")
        relay = self.preview.get("relay")
        self.preview.update(on=False, frame=None, decoder=None, player=None, video=None, relay=None)
        if relay:
            relay.stop()
        await self.svc.preview_stop()
        if decoder:
            await decoder.stop()
        self.release_wifi_soon()

    def downloads_dir(self) -> Path:
        default = (
            Path(os.environ["FLET_APP_STORAGE_DATA"]) / "downloads"
            if os.environ.get("FLET_APP_STORAGE_DATA")
            else Path.home() / "Videos" / "Quak"
        )
        d = Path(os.environ.get("QUAK_DOWNLOADS", default))
        d.mkdir(parents=True, exist_ok=True)
        return d


def _material_theme(t: th.Tokens) -> ft.Theme:
    """Material defaults (dialogs, fields, the phone tab bar) in Quak's colours and type."""
    return ft.Theme(
        color_scheme=ft.ColorScheme(
            primary=t.accent,
            on_primary=t.on_accent,
            secondary=t.accent,
            surface=t.surface,
            on_surface=t.text,
            error=t.rec,
        ),
        font_family=th.BODY_FONT,
        navigation_bar_theme=ft.NavigationBarTheme(
            height=76,
            label_text_style={
                ft.ControlState.SELECTED: ft.TextStyle(size=12, weight=ft.FontWeight.W_600, color=t.nav_selected),
                ft.ControlState.DEFAULT: ft.TextStyle(size=12, weight=ft.FontWeight.W_500, color=t.text2),
            },
        ),
    )


def _video_player_available() -> bool:
    try:
        import flet_video  # noqa: F401

        return True
    except ImportError:
        return False


def _mobile_ble_available() -> bool:
    try:
        import quak.transport.android  # noqa: F401

        return True
    except ImportError as e:
        print(f"quak: phone Bluetooth bridge unavailable, using demo mode: {e!r}", flush=True)
        return False


def build_service(sim: bool, page: ft.Page | None = None) -> CameraService:
    if sim:
        from quak.transport.sim import SimCamera

        sim_cam = SimCamera.load_or_new(config.sim_state_file())
        return CameraService(Target(sim=True, sim_camera=sim_cam))
    if page is not None and page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS):
        from quak_native import QuakBle

        from quak.transport.android import AndroidLink

        # handlers must be set at creation: the Dart side only sends events a handler existed for then;
        # AndroidLink swaps in its own
        ble = QuakBle(on_notify=lambda e: None, on_disconnected=lambda e: None)  # registers with the page
        try:
            from quak_native import QuakWifi

            from quak.net.android_wifi import AndroidJoiner
            from quak.net.wifi import set_platform_joiner

            set_platform_joiner(AndroidJoiner(QuakWifi()))
        except ImportError as e:  # an older bridge without Wi-Fi: join the camera's Wi-Fi by hand
            print(f"quak: phone Wi-Fi bridge unavailable: {e!r}", flush=True)
        target = Target(
            link_factory=lambda t, address: AndroidLink(ble, address, wait_pairing=t.wait_pairing, log=t.log)
        )
        service = CameraService(target)
        service.ble = ble
        return service
    return CameraService(Target())


def main(page: ft.Page, sim: bool | None = None) -> None:
    if sim is None:
        sim = os.environ.get("QUAK_SIM") == "1" or bool(config.get_pref("demo", False))
        if page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS) and not _mobile_ble_available():
            sim = True  # demo mode until the phone's Bluetooth bridge is in the build
    page.title = "Quak"
    page.padding = 0
    page.spacing = 0
    page.fonts = dict(th.FONTS)
    service = build_service(sim, page)
    app = App(page, service, sim)
    app.apply_theme()
    page.data = app

    def brightness_changed(e):
        if app.theme_pref == "system":
            app.apply_theme()
            app.render()

    page.on_platform_brightness_change = brightness_changed
    page.on_keyboard_event = app.on_key
    if page.platform == ft.PagePlatform.WINDOWS:  # the only platform with a settable window icon
        page.window.icon = str(Path(__file__).parent / "assets" / "icon.ico")
    page.views[0].can_pop = False  # otherwise Android's Back leaves the app from any screen
    page.views[0].on_confirm_pop = app.on_android_back
    app.render()
    if not sim:
        recover_network()
    if sim or config.default_address():
        app.run(app.connect)

    async def ticker():  # recording timer, schedule countdown; the simulator's state is saved as it changes
        n = 0
        while True:
            await asyncio.sleep(1)
            n += 1
            st = app.svc.state
            if st.recording or (app.screen == "schedule" and st.connected):
                if not app.tick():
                    app.render()
            if app.sim and n % 2 == 0:
                app.svc.target.sim_camera.save(config.sim_state_file())

    page.run_task(ticker)

    from quak.gui import tour

    tour.setup(app)

    async def save_sim_on_close():
        if app.sim:
            app.svc.target.sim_camera.save(config.sim_state_file())

    page.on_close = lambda e: page.run_task(save_sim_on_close)


def run(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="quak-gui", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--sim", action="store_true", default=os.environ.get("QUAK_SIM") == "1")
    p.add_argument("--web", action="store_true", help="serve the UI in a browser")
    p.add_argument("--port", type=int, default=8550)
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to serve --web on. There is no login: anyone who can "
        "reach it can record on and delete from the camera",
    )
    a = p.parse_args(argv)
    if (
        not a.web
        and sys.platform.startswith("linux")
        and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    ):
        # without this Flet silently starts a web server instead, on every network interface
        p.exit(1, "quak-gui: no display found. Use --web to open it in a browser on this machine.\n")
    if a.web and a.host not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"quak-gui: serving on {a.host} with no login. Anyone who can reach it can control the camera.",
            file=sys.stderr,
        )
    os.environ["QUAK_SIM"] = "1" if a.sim else os.environ.get("QUAK_SIM", "0")
    ft.run(
        lambda page: main(page, a.sim),
        view=ft.AppView.WEB_BROWSER if a.web else ft.AppView.FLET_APP,
        host=a.host,
        port=a.port if a.web else 0,
        assets_dir=str(Path(__file__).parent / "assets"),
    )


if __name__ == "__main__":
    run()

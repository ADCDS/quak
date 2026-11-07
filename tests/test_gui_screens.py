"""Build every GUI screen (and its states) against a simulator-backed service, without a
Flet client: catches layout/API errors and checks the key texts are there."""

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quak.core.registry import Status

try:
    import flet as ft

    from quak.gui import app as gui_app
    from quak.gui import kit, screens, theme
    from quak.gui.screens.media import _load_media, _open_local
    from quak.gui.screens.more import _diagnostics, _show_licences
except ImportError:  # GUI extra not installed
    ft = None

from quak.connect import Target
from quak.service import CameraService
from quak.transport.sim import SimCamera


class FakePage:
    def __init__(self, width=412, platform=None):
        self.width, self.height = width, 915
        self.platform = platform or ft.PagePlatform.LINUX
        self.controls, self.dialogs, self.navigation_bar, self.on_resize = [], [], None, None

    def run_task(self, fn, *args):
        return asyncio.ensure_future(fn(*args))

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        pass

    def show_dialog(self, d):
        self.dialogs.append(d)

    def pop_dialog(self):
        return self.dialogs.pop() if self.dialogs else None


def texts(control) -> list[str]:
    """All strings in a control tree (Text values, button labels, tooltips)."""
    out, stack, seen = [], [control], set()
    while stack:
        c = stack.pop()
        if c is None or id(c) in seen:
            continue
        seen.add(id(c))
        if isinstance(c, (list, tuple)):
            stack.extend(c)
            continue
        for attr in ("value", "content", "tooltip", "label", "title"):
            v = getattr(c, attr, None)
            if isinstance(v, str):
                out.append(v)
            elif v is not None and not isinstance(v, (int, float, bool, bytes)):
                stack.append(v)
        for attr in ("controls", "destinations", "actions"):
            v = getattr(c, attr, None)
            if v:
                stack.extend(v)
    return out


@unittest.skipIf(ft is None, "GUI extra not installed")
class Screens(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sim = SimCamera(latency=0.0005)
        self.svc = CameraService(Target(sim=True, sim_camera=self.sim, keepalive=None), auto_reconnect=False)
        self.page = FakePage()
        self.app = gui_app.App(self.page, self.svc, sim=True)
        await self.svc.connect()

    async def asyncTearDown(self):
        await self.svc.disconnect()
        await self.sim.close()

    def build(self, name):
        fn = _diagnostics if name == "_diagnostics" else getattr(screens, name)
        return " | ".join(texts(fn(self.app)))

    async def test_a_failed_preview_start_offers_a_retry(self):
        """A failed start used to leave the phone on "Starting preview…" for good."""
        from quak.core.errors import Rejected

        await self.svc.load_group("video")
        with (
            mock.patch("quak.gui.preview.FrameDecoder.available", return_value=False),
            mock.patch.object(self.svc, "preview_start", mock.AsyncMock(side_effect=Rejected("stream", "409"))),
        ):
            with self.assertRaises(Rejected):
                await self.app.start_preview()
        self.assertFalse(self.app.preview["on"])
        remote = self.build("remote")
        self.assertIn("Start preview", remote)
        self.assertIn(gui_app.PREVIEW_FAILED, remote)
        self.assertNotIn("Starting preview…", remote)

    def _preview_with_no_picture(self):
        return (
            mock.patch("quak.gui.preview.FrameDecoder.available", return_value=True),
            mock.patch("quak.gui.preview.FrameDecoder.start", mock.AsyncMock()),
            mock.patch("quak.gui.preview.FrameDecoder.stop", mock.AsyncMock()),
            mock.patch.object(self.svc, "preview_start", mock.AsyncMock()),
            mock.patch.object(self.svc, "preview_stop", mock.AsyncMock()),
        )

    async def test_a_preview_with_no_picture_gives_up_and_says_why(self):
        """A stream that never arrived used to leave "Starting preview…" on screen for good."""
        await self.svc.load_group("video")
        available, start, stop, begin, end = self._preview_with_no_picture()
        with available, start, stop, begin, end, mock.patch.object(gui_app, "PICTURE_WAIT_SECONDS", 0):
            await self.app.start_preview()
            for _ in range(50):
                if not self.app.preview["on"]:
                    break
                await asyncio.sleep(0)
        self.assertFalse(self.app.preview["on"])
        remote = self.build("remote")
        self.assertIn(gui_app.NO_PICTURE, remote)
        self.assertNotIn("Starting preview…", remote)

    async def test_recording_doesnt_count_as_waiting_for_a_picture(self):
        await self.svc.load_group("video")
        self.svc.state.statuses[Status.ENCODING] = 1
        decoder = object()
        self.app.preview.update(on=True, frame=None, decoder=decoder)
        with (
            mock.patch.object(gui_app, "PICTURE_WAIT_SECONDS", 0.01),
            mock.patch.object(gui_app, "PICTURE_CHECK_SECONDS", 0.001),
            mock.patch.object(self.app, "run") as give_up,
        ):
            watching = asyncio.ensure_future(self.app._watch_for_picture(decoder))
            await asyncio.sleep(0.1)  # far longer than the wait
            give_up.assert_not_called()
            self.svc.state.statuses[Status.ENCODING] = 0  # the recording ends, and only now does the wait count
            await asyncio.wait_for(watching, 5)
        give_up.assert_called_once()
        self.app.preview.update(on=False, decoder=None)

    async def test_a_turbo_left_on_is_cleared_when_the_app_connects(self):
        """A connection that drops during a download leaves it on, and the camera then refuses nearly everything."""
        await self.svc.disconnect()
        self.sim.status[Status.TURBO] = 1
        await self.svc.connect()
        self.assertEqual(self.sim.status[Status.TURBO], 0)
        self.assertFalse(self.svc.state.statuses.get(Status.TURBO))

    async def test_tools_that_change_a_real_camera_a_lot_stay_out_of_the_way(self):
        self.app.sim = False  # a real camera, as far as the screens can tell
        with mock.patch.dict("os.environ", {"QUAK_DEV": ""}):
            self.assertNotIn("Self-test", self.build("_diagnostics"))
            self.assertNotIn("Test mode", self.build("live"))
        with mock.patch.dict("os.environ", {"QUAK_DEV": "1"}), mock.patch("sys.platform", "linux"):
            self.assertIn("Self-test", self.build("_diagnostics"))
            self.assertIn("Test mode", self.build("live"))

    async def test_a_phones_downloads_can_get_out_of_the_app(self):
        self.page.platform = ft.PagePlatform.ANDROID
        share = mock.AsyncMock()
        clip = Path("/data/GX010001.MP4")
        with mock.patch.object(ft, "Share", return_value=SimpleNamespace(share_files=share)):
            await _open_local(self.app, clip)
        self.assertEqual(share.await_args.args[0][0].path, str(clip))

    async def test_the_licences_can_be_read_in_the_app(self):
        shown = {}
        self.app.dialog = lambda title, content, actions: shown.update(title=title, content=content)
        _show_licences(self.app)
        self.assertEqual(shown["title"], "Licences")
        text = " ".join(getattr(c, "value", "") or "" for c in shown["content"].content.controls)
        for needed in ("FFmpeg", "mpv", "GNU LESSER GENERAL PUBLIC LICENSE", "GNU GENERAL PUBLIC LICENSE"):
            self.assertIn(needed, text)

    async def test_another_camera_model_gets_a_warning(self):
        self.assertNotIn("only tested", self.build("cameras"))
        self.svc.state = replace(self.svc.state, model="HERO10 Black")
        self.assertIn("only tested on a HERO11 Black", self.build("cameras"))

    async def test_preview_panel_while_recording(self):
        """The camera can't stream while it records: say so, instead of a spinner that never finishes."""
        await self.svc.load_group("video")
        self.app.preview.update(on=True, frame=None)
        idle = self.build("remote")
        self.assertIn("Starting preview…", idle)
        self.assertNotIn("Preview pauses while recording", idle)
        await self.svc.shutter(True)
        for _ in range(300):  # the state follows the camera's status push
            if self.svc.state.recording:
                break
            await asyncio.sleep(0.01)
        recording = self.build("remote")
        self.assertIn("Preview pauses while recording", recording)
        self.assertNotIn("Starting preview…", recording)
        await self.svc.shutter(False)

    async def test_phone_preview_uses_the_video_player(self):
        """Without ffmpeg (phones) the camera's UDP stream plays in flet-video instead of decoded frames."""
        from unittest import mock

        try:
            import flet_video  # noqa: F401
        except ImportError:
            self.skipTest("flet-video not installed")
        with mock.patch("quak.gui.preview.FrameDecoder.available", return_value=False):
            await self.app.start_preview()
        self.assertTrue(self.app.preview.get("player", "").startswith("http://127.0.0.1:"))
        self.app.screen = "remote"
        self.app.render()
        self.assertEqual(type(self.app.preview.get("video")).__name__, "Video")
        # status updates must not rebuild the page: that would destroy the playing video
        tree = list(self.page.controls)
        self.svc.state.statuses[Status.BATTERY_PERCENT] = 12
        self.app.render()
        self.assertEqual([id(c) for c in self.page.controls], [id(c) for c in tree])
        # ...but something the screen shows does
        self.svc.state.statuses[Status.ENCODING] = 1
        self.app.render()
        self.assertNotEqual([id(c) for c in self.page.controls], [id(c) for c in tree])
        self.svc.state.statuses[Status.ENCODING] = 0
        await self.app.stop_preview()
        self.assertIsNone(self.app.preview.get("video"))

    async def test_countdowns_refresh_in_place(self):
        """The 1 s ticker updates timer texts without rebuilding the page (a rebuild cancels taps)."""
        self.app.screen = "schedule"
        self.app.render()
        self.assertTrue(self.app._live_texts, "the schedule countdown should be a live text")
        before = len(self.page.controls)
        self.assertTrue(self.app.tick())
        self.assertEqual(len(self.page.controls), before)

    async def test_log_lines_refresh_the_list_in_place(self):
        """The keep-alive logs a line every 3 s: on Diagnostics that must not rebuild the page (a rebuild drops taps)."""
        from unittest import mock

        self.app.screen, self.app.more_tab = "more", "diagnostics"
        await asyncio.sleep(0.15)  # the render_soon() left over from connecting
        self.app.render()
        tree, view = list(self.page.controls), self.app.diag["log_view"]
        before = list(view.controls)
        with mock.patch.object(type(view), "update") as update:
            await self.svc.cam.statuses()  # logs the query and its reply
            await asyncio.sleep(0.15)
        update.assert_called()
        self.assertGreater(len(view.controls), len(before))
        self.assertEqual([id(c) for c in self.page.controls], [id(c) for c in tree])
        # paused: the list stays as it is
        self.app.diag["paused"] = True
        paused = list(view.controls)
        with mock.patch.object(type(view), "update") as update:
            await self.svc.cam.statuses()
            await asyncio.sleep(0.15)
        update.assert_not_called()
        self.assertEqual([id(c) for c in view.controls], [id(c) for c in paused])
        # another screen: nothing left to refresh
        self.app.diag["paused"] = False
        self.app.go("cameras")
        self.assertIsNone(self.app.diag["log_view"])

    async def test_every_screen_builds(self):
        cams = self.build("cameras")
        self.assertIn("Your cameras", cams)
        self.assertIn("Open remote", cams)
        for step in ("instructions", "scanning", "pairing", "done", "error"):
            self.app.add["step"] = step
            self.assertIn("Add camera", self.build("add_camera"))
        self.app.add.update(step="scanning", scanning=False, found=[], seen=3)
        empty = self.build("add_camera")
        self.assertIn("No camera found", empty)
        self.assertIn("Saw 3 Bluetooth devices", empty)
        self.assertIn("Scan again", empty)
        remote = self.build("remote")
        self.assertIn("Start preview", remote)
        self.assertIn("Start recording", remote)
        settings = self.build("settings")
        self.assertIn("Needs Pro controls", settings)  # the simulated camera starts in Easy, like the real one
        self.assertIn("Arm for", self.build("schedule"))
        self.app.media["loaded"] = True  # don't start an HTTP load
        self.assertIn("Media", self.build("media"))
        self.assertIn("Go live", self.build("live"))
        self.assertIn("Test mode", self.build("live"))  # desktop Linux: the lab hotspot is offered
        self.app.page.platform = ft.PagePlatform.ANDROID
        self.assertNotIn("Test mode", self.build("live"))  # a phone can't be the hotspot
        self.app.page.platform = ft.PagePlatform.LINUX
        self.assertIn("Firmware", self.build("more"))
        self.app.more_tab = "diagnostics"
        self.assertIn("Export log", self.build("more"))

    async def test_states_change_what_is_shown(self):
        await self.svc.set_setting(175, 1)
        self.assertNotIn("Needs Pro controls", self.build("settings"))
        await self.svc.load_group("video")
        await self.svc.shutter(True)
        await asyncio.sleep(0.2)
        self.assertIn("Stop recording", self.build("remote"))
        await self.svc.shutter(False)
        import datetime as dt

        from quak.core import registry as R

        await self.svc.arm_schedule(R.next_schedule(dt.datetime.now(), 120), "15s")
        self.assertIn("Disarm", self.build("schedule"))
        self.assertIn("Scheduled", self.build("cameras"))

    async def test_wide_layout_uses_a_rail(self):
        self.page.width = 1280
        self.app.render()
        self.assertIsNone(self.page.navigation_bar)
        self.page.width = 412
        self.app.render()
        self.assertIsNotNone(self.page.navigation_bar)


@unittest.skipIf(ft is None, "GUI extra not installed")
class MediaLoadingTests(unittest.IsolatedAsyncioTestCase):
    """A page rebuild can swallow a tap, so thumbnails must not rebuild a screen the user has moved on to."""

    def app(self, screen):
        files = [SimpleNamespace(path=f"100GOPRO/{i}.MP4", name=f"{i}.MP4", created=i, is_video=True) for i in range(3)]
        http = SimpleNamespace(media_info=lambda path: {})
        svc = SimpleNamespace(
            media_list=mock.AsyncMock(return_value=files),
            thumbnail=mock.AsyncMock(return_value=b"x"),
            http=mock.AsyncMock(return_value=http),
            release_http=mock.AsyncMock(),
        )
        return SimpleNamespace(
            screen=screen,
            media={"thumbs": {}, "selecting": False, "selected": set()},
            svc=svc,
            wide=False,
            t=theme.DARK,
            render=mock.Mock(),
            render_soon=mock.Mock(),
        )

    async def test_leaving_media_stops_loading_thumbnails_until_coming_back(self):
        """Each one needs the camera's Wi-Fi, which may be this computer's only network."""
        app = self.app("remote")
        await _load_media(app)
        self.assertEqual(app.media["thumbs"], {})
        self.assertTrue(app.media["unfinished"])
        app.render.assert_not_called()
        app.render_soon.assert_not_called()
        app.screen = "media"
        await _load_media(app)
        self.assertEqual(len(app.media["thumbs"]), 3)
        self.assertFalse(app.media["unfinished"])

    async def test_a_thumbnail_updates_its_own_tile_without_rebuilding_the_page(self):
        """Rebuilding the page per thumbnail made every tile redraw: the flicker seen on the phone."""
        app = self.app("media")
        stacks = {f.path: mock.Mock(controls=[]) for f in await app.svc.media_list()}
        app.media["tiles"] = {path: (stack, 100, 100) for path, stack in stacks.items()}
        await _load_media(app)
        app.render_soon.assert_not_called()
        for stack in stacks.values():
            stack.update.assert_called_once()
            self.assertTrue(stack.controls)

    async def test_loading_shows_an_empty_card_and_errors(self):
        seen = []
        app = self.app("media")
        app.svc.media_list = mock.AsyncMock(return_value=[])
        app.render = mock.Mock(side_effect=lambda: seen.append(app.media["loading"]))
        await _load_media(app)
        self.assertEqual(seen[-1], False)  # the last draw knows loading is over, so "No media" can show

        seen.clear()
        app = self.app("media")
        app.svc.media_list = mock.AsyncMock(side_effect=RuntimeError("no link"))
        app.render = mock.Mock(side_effect=lambda: seen.append(app.media["error"]))
        with self.assertRaises(RuntimeError):
            await _load_media(app)
        self.assertIn("no link", seen[-1])
        self.assertFalse(app.media["loading"])

    async def test_a_tile_that_is_gone_is_harmless(self):
        app = self.app("media")
        files = await app.svc.media_list()
        stack = mock.Mock(controls=[])
        stack.update.side_effect = RuntimeError("not on the page")
        app.media["tiles"] = {files[0].path: (stack, 100, 100)}
        await _load_media(app)
        self.assertEqual(len(app.media["thumbs"]), 3)


@unittest.skipIf(ft is None, "GUI extra not installed")
class AppNavigationTests(unittest.IsolatedAsyncioTestCase):
    def app(self, screen):
        app = gui_app.App.__new__(gui_app.App)
        app.screen, app._media_view = screen, None
        app.svc = SimpleNamespace(state=SimpleNamespace(connected=True, link="ble", http=False, s=lambda k, d=None: d))
        app.render_soon, app.go = mock.Mock(), mock.Mock()
        return app

    async def test_status_changes_dont_rebuild_media_unless_it_shows_them(self):
        app = self.app("media")
        app._state_changed(None)
        app._state_changed(None)
        self.assertEqual(app.render_soon.call_count, 1)
        app.svc.state.http = True
        app._state_changed(None)
        self.assertEqual(app.render_soon.call_count, 2)

    async def test_other_screens_still_follow_every_change(self):
        app = self.app("remote")
        app._state_changed(None)
        app._state_changed(None)
        self.assertEqual(app.render_soon.call_count, 2)

    async def test_a_task_can_finish_without_a_page_rebuild(self):
        for redraw, calls in ((True, 1), (False, 0)):
            app = self.app("media")
            app.render = mock.Mock()
            tasks = []
            app.page = SimpleNamespace(run_task=lambda fn: tasks.append(asyncio.ensure_future(fn())))
            app.run(mock.AsyncMock(), redraw=redraw)
            await tasks[0]
            self.assertEqual(app.render.call_count, calls)

    async def test_it_asks_before_taking_this_computers_wifi_and_retries_if_allowed(self):
        from quak.core.errors import AdapterInUse

        app = self.app("media")
        app.t, app.svc.allow_busy_adapter, app.render = theme.DARK, False, mock.Mock()
        app.dialog, app.close_dialog = mock.Mock(), mock.Mock()
        tasks = []
        app.page = SimpleNamespace(run_task=lambda fn: tasks.append(asyncio.ensure_future(fn())))
        job = mock.AsyncMock(side_effect=[AdapterInUse("wlan0"), None])
        app.run(job)
        await tasks[0]
        self.assertIn("wlan0", app.dialog.call_args.args[0])
        self.assertFalse(app.svc.allow_busy_adapter)  # nothing happens until they agree
        agree = next(b for b in app.dialog.call_args.args[2] if b.content == "Use it")
        agree.on_click(None)
        self.assertTrue(app.svc.allow_busy_adapter)
        await tasks[1]
        self.assertEqual(job.await_count, 2)

    async def wifi_app(self, screen="remote"):
        app = self.app(screen)
        app._wifi_wait, app.preview, app.media = 0, {"on": False}, {}
        app.svc.state.http, app.svc.release_http = True, mock.AsyncMock()
        self.tasks = []
        app.page = SimpleNamespace(run_task=lambda fn: self.tasks.append(asyncio.ensure_future(fn())))
        return app

    async def test_the_cameras_wifi_is_given_back_after_a_while(self):
        app = await self.wifi_app()
        with mock.patch.object(gui_app, "WIFI_IDLE_SECONDS", 0):
            app.release_wifi_soon()
            await self.tasks[0]
        app.svc.release_http.assert_awaited_once()

    async def test_coming_back_or_being_busy_keeps_the_cameras_wifi(self):
        for changes in ({"screen": "media"}, {"preview": {"on": True}}, {"media": {"progress": ("a", 1, 2)}}):
            app = await self.wifi_app()
            with mock.patch.object(gui_app, "WIFI_IDLE_SECONDS", 0):
                app.release_wifi_soon()
                for k, v in changes.items():
                    setattr(app, k, v)
                await self.tasks[0]
            app.svc.release_http.assert_not_called()

    async def test_a_newer_move_replaces_an_older_wait(self):
        app = await self.wifi_app()
        with mock.patch.object(gui_app, "WIFI_IDLE_SECONDS", 0):
            app.release_wifi_soon()
            app.release_wifi_soon()
            await asyncio.gather(*self.tasks)
        app.svc.release_http.assert_awaited_once()

    async def test_android_back(self):
        for screen, target in (
            ("schedule", "cameras"),
            ("settings", "remote"),
            ("add_camera", "cameras"),
            ("media", "cameras"),
        ):
            app = self.app(screen)
            e = SimpleNamespace(control=SimpleNamespace(confirm_pop=mock.AsyncMock()))
            await app.on_android_back(e)
            e.control.confirm_pop.assert_awaited_once_with(False)
            app.go.assert_called_once_with(target)

    async def test_android_back_leaves_from_the_first_screen(self):
        app = self.app("cameras")
        e = SimpleNamespace(control=SimpleNamespace(confirm_pop=mock.AsyncMock()))
        await app.on_android_back(e)
        e.control.confirm_pop.assert_awaited_once_with(True)
        app.go.assert_not_called()


@unittest.skipIf(ft is None, "GUI extra not installed")
class ButtonFontTests(unittest.TestCase):
    def test_buttons_name_the_design_font(self):
        """Flutter replaces a button's whole text style, so without a family it falls back to the platform's font."""
        t = theme.DARK
        for button in (kit.primary("x", None, t), kit.secondary("x", None, t), kit.destructive("x", None, t)):
            self.assertEqual(button.style.text_style.font_family, theme.BODY_FONT)


@unittest.skipIf(ft is None, "GUI extra not installed")
class LogoTests(unittest.TestCase):
    """Without these files `flet build` silently brands the app with Flet's own logo."""

    def test_every_icon_the_build_looks_for_exists(self):
        assets = Path(kit.__file__).parent / "assets"
        for name in (
            "logo.png",
            "logo.svg",
            "favicon.png",
            "icon.png",
            "icon.ico",
            "icon_windows.png",
            "icon_linux.png",
            "icon_web.png",
        ):
            self.assertGreater((assets / name).stat().st_size, 0, name)

    def test_the_app_shows_the_logo_file(self):
        self.assertEqual(kit.mark(40).src, "logo.png")


@unittest.skipIf(ft is None, "GUI extra not installed")
class ServingTests(unittest.TestCase):
    """quak-gui can record on and delete from the camera, and has no login."""

    def start(self, *argv, env=None):
        with (
            mock.patch.dict("os.environ", env or {"DISPLAY": ":0"}, clear=True),
            mock.patch.object(gui_app.ft, "run") as run,
        ):
            gui_app.run(list(argv))
        return run.call_args.kwargs

    def test_web_stays_on_this_machine_by_default(self):
        self.assertEqual(self.start("--web")["host"], "127.0.0.1")

    def test_serving_wider_takes_an_explicit_choice_and_warns(self):
        with mock.patch("sys.stderr") as err:
            self.assertEqual(self.start("--web", "--host", "0.0.0.0")["host"], "0.0.0.0")
        self.assertIn("no login", "".join(c.args[0] for c in err.write.call_args_list))

    def test_no_display_is_an_error_and_not_a_silent_web_server(self):
        with mock.patch("sys.platform", "linux"), mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            self.start(env={"HOME": "/nonexistent"})
        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()

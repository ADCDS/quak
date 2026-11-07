"""Camera, flows and SettingsGuard against the simulated HERO11."""

import asyncio
import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path

from quak import flows
from quak.connect import Target, open_camera
from quak.core import registry as R
from quak.core.errors import NotBonded, Rejected, Unsupported
from quak.core.registry import Status
from quak.guard import SettingsGuard
from quak.transport.sim import SimCamera


class FastClock:
    """Wall clock running `speed` times faster from now on (lets 15 s clips take 0.5 s)."""

    def __init__(self, speed: float):
        self.t0, self.speed = time.time(), speed

    def __call__(self) -> float:
        return self.t0 + (time.time() - self.t0) * self.speed


class ManualClock:
    """Wall clock that moves only when the test says so: what it reads can't depend on timers or a busy runner."""

    def __init__(self):
        self.now = float(int(time.time()))

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def sim(**kw) -> SimCamera:
    return SimCamera(latency=0.0005, **kw)


def target(cam: SimCamera, **kw) -> Target:
    return Target(sim=True, sim_camera=cam, keepalive=None, **kw)


class Basics(unittest.IsolatedAsyncioTestCase):
    async def test_easy_controls_hide_pro_settings_like_the_real_camera(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            caps = await c.capabilities([156, 157, 168, 175])
            self.assertEqual(caps, {156: [], 157: [], 168: [], 175: [0, 1]})
            with self.assertRaises(Unsupported):
                await c.set_setting(168, R.encode_schedule(12, 34))
            await c.set_setting(175, 1)
            await c.set_setting(168, R.encode_schedule(12, 34))
            self.assertEqual(await c.setting(168), 0x0C8B)

    async def test_rejected_value_lists_allowed_ones(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            with self.assertRaises(Rejected) as cm:
                await c.set_setting(91, 9)
            self.assertIn("All On", str(cm.exception))

    async def test_clock_sync(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            t, tz, dst = await c.clock()
            self.assertAlmostEqual((t - dt.datetime.now()).total_seconds(), 4, delta=1.5)
            await c.sync_clock()
            t, tz, dst = await c.clock()
            # the camera clock has 1 s resolution; leave room for a slow CI runner
            self.assertLessEqual(abs((t - dt.datetime.now()).total_seconds()), 1.5)
            self.assertEqual(tz, time.localtime().tm_gmtoff // 60)

    async def test_presets_and_groups(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            groups = await c.presets()
            self.assertEqual([g["id"] for g in groups], [1000, 1001, 1002])
            await c.load_group("video")
            self.assertEqual(await c.status(Status.PRESET_GROUP), 1000)
            await c.load_preset(0x00020001)
            self.assertEqual(await c.status(Status.PRESET), 0x00020001)
            with self.assertRaises(Rejected):
                await c.load_preset(0x00070000)  # no such preset

    async def test_record_clip_with_hilight(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            r = await flows.record_clip(c, 0.6, hilight_at=0.3)
        self.assertEqual(r.videos_after, r.videos_before + 1)
        self.assertEqual(len(cam.media), 1)
        self.assertEqual(len(cam.media[0].hilights), 1)

    async def test_duration_stops_recording_by_itself(self):
        clock = ManualClock()
        cam = sim(clock=clock)
        async with open_camera(target(cam)) as c:
            await c.set_setting(175, 1)
            await c.load_group("video")
            await c.set_setting(156, R.duration_value("15s"))
            await c.shutter(True)
            await c.wait_status(Status.ENCODING, lambda v: v == 1, 2, poll=0.05)
            clock.advance(14)
            await asyncio.sleep(0.2)  # several rounds of the simulator's 50 ms check
            self.assertEqual(await c.status(Status.ENCODING), 1, "stopped before the 15 s limit")
            clock.advance(1)
            await c.wait_status(Status.ENCODING, lambda v: v == 0, 3, poll=0.05)
        self.assertAlmostEqual(cam.media[-1].duration, 15, delta=1.5)

    async def test_status_push(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            current, updates = await c.watch_statuses([Status.ENCODING, Status.VIDEOS])
            self.assertEqual(current[Status.ENCODING], 0)
            await c.load_group("video")
            await c.shutter(True)
            self.assertEqual(await asyncio.wait_for(updates.get(), 1), {Status.ENCODING: 1})
            await c.shutter(False)
            seen = {}
            while len(seen) < 2:
                seen.update(await asyncio.wait_for(updates.get(), 1))
            self.assertEqual(seen[Status.ENCODING], 0)

    async def test_camera_control_released_on_close(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            await c.camera_control("external")
            self.assertEqual(cam.status[Status.CAMERA_CONTROL], 2)
        self.assertEqual(cam.status[Status.CAMERA_CONTROL], 0)

    async def test_wifi_ap_and_credentials(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            await c.set_ap(True)
            await c.wait_status(Status.AP_MODE, lambda v: v == 1, 2, poll=0.05)
            ssid, password = await c.wifi_credentials()
            self.assertEqual(ssid, await c.status(Status.AP_SSID))
            self.assertTrue(password)

    async def test_pairing_requires_pairing_mode(self):
        cam = sim(bonded=False)
        with self.assertRaises(NotBonded):
            async with open_camera(target(cam)):
                pass
        cam.pairing_mode = True
        async with open_camera(target(cam)) as c:
            self.assertTrue(c.new_bond)
        self.assertTrue(cam.bonded)
        self.assertTrue(any(m[:2] == bytes([0x03, 0x01]) for d, u, m in cam.log if d == "rx"))


class Schedule(unittest.IsolatedAsyncioTestCase):
    async def arm(self, cam, **kw):
        async with open_camera(target(cam)) as c:
            when = R.next_schedule(dt.datetime.now(), 60)
            r = await flows.arm_schedule(c, when, duration="15s", **kw)
        return when, r

    async def test_arm_does_the_whole_sequence(self):
        cam = sim()
        when, r = await self.arm(cam)
        self.assertEqual(cam.global_settings[175], 1)
        self.assertEqual(cam.preset_settings[0x00090000][156], R.duration_value("15s"))  # Standard
        self.assertEqual(cam.status[Status.SCHEDULE_ARMED], 1)
        self.assertEqual(R.preset_group_of(r.preset), "video")
        self.assertIn("Auto Power Off is Never", " ".join(r.warnings))
        self.assertLess(abs(cam.clock_offset), 1.0)  # clock was synced

    async def test_refuses_without_duration(self):
        cam = sim()
        async with open_camera(target(cam)) as c:
            with self.assertRaises(ValueError):
                await flows.arm_schedule(c, dt.datetime.now(), duration=None)

    async def jump_to(self, cam, when):
        cam.clock_offset = (when - dt.timedelta(seconds=0.3)).timestamp() - cam.clock()

    async def test_schedule_fires_on_the_camera_clock(self):
        cam = sim(clock=FastClock(20))  # the 15 s clip then takes under a second
        when, _ = await self.arm(cam)
        videos = cam.status[Status.VIDEOS]
        async with open_camera(target(cam)) as c:
            await self.jump_to(cam, when)
            await c.wait_status(Status.ENCODING, lambda v: v == 1, 3, poll=0.05)
            self.assertEqual(cam.status[Status.SCHEDULE_ARMED], 0)  # ASSUMED one-shot
            await c.wait_status(Status.VIDEOS, lambda v: v == videos + 1, 3, poll=0.05)
        self.assertTrue(cam.media[-1].by_schedule)

    async def test_schedule_survives_sleep_and_fires(self):
        cam = sim()
        when, r = await self.arm(cam, sleep_after=True)
        self.assertTrue(r.slept)
        await asyncio.sleep(0.4)
        self.assertTrue(cam.asleep)
        self.assertFalse(cam.connected)
        cam._ensure_scheduler()
        await self.jump_to(cam, when)
        await asyncio.sleep(0.6)
        self.assertEqual(cam.status[Status.ENCODING], 1)
        await cam.close()

    async def test_power_button_variant_is_configurable(self):
        cam = sim()
        cam.power_button_cancels_schedule = True
        await self.arm(cam)
        cam.press_power_button()
        self.assertEqual(cam.status[Status.SCHEDULE_ARMED], 0)

    async def test_persisted_sim_fires_missed_schedule(self):
        cam = sim()
        when, _ = await self.arm(cam)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sim.json"
            cam.save(path)
            data = cam.to_json()
            data["saved_at"] -= 3600  # an hour passed between CLI runs
            data["clock_offset"] += (when - dt.datetime.now()).total_seconds() + 30
            again = SimCamera.from_json(data)
        self.assertEqual(again.status[Status.SCHEDULE_ARMED], 0)
        self.assertTrue(again.media and again.media[-1].by_schedule)


class Guard(unittest.IsolatedAsyncioTestCase):
    async def test_restore_across_presets(self):
        cam = sim()
        before_global = dict(cam.global_settings)
        before_presets = {k: dict(v) for k, v in cam.preset_settings.items()}
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "run.ledger.json"
            async with open_camera(target(cam)) as c:
                g = SettingsGuard(c, ledger)
                await g.snapshot()
                await g.set_setting(175, 1)
                await g.load_group("video")
                await g.set_setting(156, R.duration_value("1m"))
                await g.set_setting(91, 4)
                await g.load_group("timelapse")
                await g.set_setting(121, 4)
                await c.set_ap(True)
                await asyncio.sleep(0.4)
                # a fresh guard from the ledger restores just as well (crash recovery)
                restored = await SettingsGuard.from_ledger(c, ledger).restore()
            self.assertTrue(restored)
        self.assertEqual(cam.global_settings, before_global)
        self.assertEqual(cam.preset_settings, before_presets)
        self.assertEqual(cam.preset, 0x00010000)
        self.assertEqual(cam.status[Status.AP_MODE], 0)


if __name__ == "__main__":
    unittest.main()

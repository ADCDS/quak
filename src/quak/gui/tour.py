"""Design check: walk every screen and state, save a screenshot of each as the app renders it, then quit.

    QUAK_SIM=1 QUAK_TOUR=/tmp/shots quak-gui            # desktop window (1280x800)
    QUAK_TOUR_SIZE=412x915 ...                           # phone-sized window

The PNGs come from Flutter itself (page.take_screenshot), so they show the real surface of
whatever platform runs the app, and can be laid next to the design artboards.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path

import flet as ft


def _add(step: str, **kw) -> dict:
    return {
        "add": {
            "step": step,
            "found": [],
            "error": "",
            "scanning": False,
            "seen": None,
            "device": {"name": "GoPro (simulated)"},
            **kw,
        }
    }


async def _arm(app) -> None:
    when = dt.datetime.now().replace(second=0, microsecond=0) + dt.timedelta(minutes=30)
    await app.svc.arm_schedule(when, "15s", preset="video")


async def _disarm(app) -> None:
    await app.svc.disarm_schedule()


STEPS = [  # (file name, screen, App attributes to set first, coroutine before, coroutine after)
    ("cameras", "cameras", {}, None, None),
    ("add-instructions", "add_camera", _add("instructions"), None, None),
    (
        "add-scanning",
        "add_camera",
        _add(
            "scanning", scanning=True, seen=4, found=[{"address": "SIM", "name": "GoPro (simulated)", "pairing": True}]
        ),
        None,
        None,
    ),
    ("add-pairing", "add_camera", _add("pairing", phase=2), None, None),
    ("add-done", "add_camera", _add("done"), None, None),
    ("add-error", "add_camera", _add("error"), None, None),
    ("remote", "remote", {}, None, None),
    ("remote-preview", "remote", {}, "preview", None),
    ("settings", "settings", {}, None, None),
    ("schedule", "schedule", {}, None, None),
    ("schedule-armed", "schedule", {}, _arm, _disarm),
    ("media", "media", {}, None, None),
    ("live", "live", {}, None, None),
    ("more-camera", "more", {"more_tab": "camera"}, None, None),
    ("more-diagnostics", "more", {"more_tab": "diagnostics"}, None, None),
    ("more-selftest", "more", {"more_tab": "diagnostics", "_diag_tab": "selftest"}, None, None),
]


async def run_tour(app, out: Path) -> None:
    page: ft.Page = app.page
    out.mkdir(parents=True, exist_ok=True)
    for _ in range(100):  # wait for the simulated camera
        if app.svc.state.connected:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)
    if app.sim and not app.svc.target.sim_camera.media:
        await _record_some(app)
    for name, screen, attrs, before, after in STEPS:
        app.diag["tab"] = attrs.get("_diag_tab", "log")
        for k, v in attrs.items():
            if not k.startswith("_"):
                setattr(app, k, v)
        if callable(before):
            await before(app)
        app.go(screen)
        if before == "preview":
            await app.start_preview()
            for _ in range(40):
                await asyncio.sleep(0.25)
                if app.preview.get("frame"):
                    break
            await asyncio.sleep(1.0)
        if screen == "media":
            for _ in range(60):
                await asyncio.sleep(0.25)
                if app.media.get("files") and len(app.media.get("thumbs", {})) >= min(6, len(app.media["files"])):
                    break
        await asyncio.sleep(1.2)
        png = await page.take_screenshot(pixel_ratio=1.0, delay=0.2)
        (out / f"{name}.png").write_bytes(png)
        print(f"tour: {name}", flush=True)
        if before == "preview":
            await app.stop_preview()
        if callable(after):
            await after(app)
    os._exit(0)


async def _record_some(app) -> None:
    """Give the simulated card something to show: a few short clips (two with a HiLight) and a photo."""
    svc = app.svc
    await svc.load_group("video")
    for i in range(7):
        await svc.shutter(True)
        await asyncio.sleep(1.2)
        if i in (0, 4):
            await svc.hilight()
        await svc.shutter(False)
    await svc.load_group("photo")
    await svc.shutter(True)
    await asyncio.sleep(0.5)
    await svc.load_group("video")


def setup(app) -> bool:
    """Start the tour if QUAK_TOUR is set (call from main once the app exists)."""
    out = os.environ.get("QUAK_TOUR")
    if not out:
        return False
    page = app.page
    page.enable_screenshots = True
    if os.environ.get("QUAK_TOUR_THEME"):
        app.set_theme(os.environ["QUAK_TOUR_THEME"])
    w, _, h = os.environ.get("QUAK_TOUR_SIZE", "1280x800").partition("x")
    if page.platform not in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS):
        page.window.width, page.window.height = int(w), int(h)
    page.update()
    page.run_task(run_tour, app, Path(out))
    return True

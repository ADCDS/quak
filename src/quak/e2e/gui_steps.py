"""The GUI E2E steps: every Quak screen and feature, driven by visible labels."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

MEDIA_NAME = re.compile(r"^(GX|GH|GOPR|GP|GL)\w*\.(MP4|JPG|LRV)$", re.IGNORECASE)
ITEM_COUNT = re.compile(r"\b(\d+) items? ·")
MAX_OWN_CLIPS = 5  # a run records two clips; finding more than this means the baseline can't be trusted


async def media_names(ui) -> set[str]:
    names = set()
    for label in await ui.labels():
        for token in label.split():  # a tile reads e.g. "GX010065.MP4 GX010065.MP4 Video"
            if MEDIA_NAME.match(token):
                names.add(token)
    return names


async def read_card(ui, timeout: float = 30) -> set[str]:
    """Every file on the card, or an AssertionError: never a partial list. G11 deletes what isn't in
    the baseline, so a baseline missing a file would put that file at risk."""
    names, count = set(), None
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        names = await media_names(ui)
        count = next((int(m[1]) for m in map(ITEM_COUNT.search, await ui.labels()) if m), None)
        if count == len(names) if names else await ui.has("No media on the card"):  # no summary on an empty card
            return names
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"couldn't read the whole card: the screen says {count} items, {len(names)} names were readable"
    )


def own_clips(names: set[str], run) -> list[str]:
    """What this run recorded: on the card now and not at G03. Without a baseline nothing is ours."""
    baseline = run.state.get("baseline")
    if baseline is None:
        raise AssertionError("no media baseline from G03; refusing to treat any file as this run's")
    return sorted(names - set(baseline))


async def nav(ui, tab: str, expect: str, timeout: float = 20) -> None:
    """Tap a navigation tab and make sure the screen actually changed (retrying).
    Pushed screens (Settings, Schedule, Add camera) have no tab bar, so go Back first; screens that
    re-render every second (the armed schedule's countdown) can swallow a tap, hence the retries."""
    for _ in range(6):
        try:
            if not await ui.has(tab) and await ui.has("Back"):
                await ui.click("Back", exact=True, settle=1.0, timeout=3)
                continue
            await ui.click(tab, exact=True, settle=1.0, timeout=5)
            await ui.wait_text(expect, timeout / 3)
            return
        except AssertionError:
            continue
    raise AssertionError(f"tab {tab!r} didn't show {expect!r}")


async def open_media(ui, run, refresh: bool = True) -> set[str]:
    await nav(ui, "Media", "Select")
    if refresh:
        await ui.click("Refresh", timeout=120)
    for _ in range(240):  # joining the camera's Wi-Fi can take a while on hardware
        if await ui.has("Camera Wi-Fi connected") and not await ui.has("Couldn't read"):
            break
        if await ui.has("Couldn't read the card"):
            raise AssertionError("the GUI couldn't read the card")
        await asyncio.sleep(0.5)
    else:
        raise AssertionError("the media screen never connected to the camera's Wi-Fi")
    await asyncio.sleep(2)  # thumbnails
    return await read_card(ui)


async def g01_home(ui, run):
    await ui.wait_text("HERO11 Black", 90)
    await ui.wait_text("Bluetooth", 30)
    await ui.wait_text("Open remote", 120)  # a slow scan is normal right after another connection
    return "camera card shows model, link, battery, card and clock"


async def g02_clock(ui, run):
    await ui.click("Sync clock")
    await ui.wait_text("Clock synced", 20)
    labels = await ui.labels()
    clock = next((l for l in labels if re.fullmatch(r"[+-]?\d+ s", l)), None)
    assert clock in ("0 s", "+1 s", "-1 s"), f"clock tile shows {clock!r} after sync"
    return f"clock tile: {clock}"


async def g03_media_baseline(ui, run):
    names = await open_media(ui, run, refresh=False)
    run.state["baseline"] = sorted(names)
    return f"{len(names)} files on the card before the GUI test"


async def g04_record(ui, run):
    await nav(ui, "Remote", "Start recording")
    await ui.click("Video", exact=True, settle=2)
    await ui.click("Start recording")
    await ui.wait_text("REC", 15)
    await asyncio.sleep(2)
    await ui.click("Tag a HiLight")
    await ui.wait_text("HiLight tagged", 10)
    await asyncio.sleep(2)
    await ui.click("Stop recording")
    await ui.wait_text("of video left", 30)
    return "recorded ~4 s with a HiLight from the Remote screen"


async def g05_settings(ui, run):
    await ui.click("Camera settings")
    await ui.wait_text("Controls", 10)
    await ui.click("Pro", exact=True, settle=2)
    await ui.click_first("Duration ")  # "Duration <value>": the first one is Video › Duration
    await ui.click("1 Minute", exact=True, settle=2)
    await ui.wait_text("Duration = 1 Minute", 15)
    await ui.click("Back", exact=True)
    await ui.wait_text("Start preview", 10)
    return "Pro controls on; video Duration set to 1 Minute through the picker"


async def g06_preview(ui, run):
    await ui.click("Start preview")
    await ui.wait_text("LIVE PREVIEW", 120)
    await asyncio.sleep(4)
    shot = await ui.shot("preview-live")
    await ui.click("Stop preview")
    await ui.wait_text("Start preview", 20)
    return f"live preview frames shown ({Path(shot).name})"


async def g07_media_download(ui, run):
    names = await open_media(ui, run)
    new = own_clips(names, run)
    assert new, (
        f"the clip recorded in G04 isn't on the card; labels: {[l for l in await ui.labels() if 'Video' in l][:6]}"
    )
    clip = new[-1]
    await ui.click(clip)
    await ui.click("Download", exact=True, settle=1)
    await ui.wait_text("Saved", 300)
    saved = Path(run.env["QUAK_DOWNLOADS"]) / clip
    assert saved.exists() and saved.stat().st_size > 0, f"{saved} missing"
    return f"{clip}: {saved.stat().st_size / 1e6:.1f} MB downloaded"


async def g08_schedule(ui, run):
    await nav(ui, "Cameras", "Your cameras")
    await ui.click("Schedule Schedule", exact=True)
    await ui.click("15 s", exact=True)
    armed_label = await ui.click_first("Arm for")
    await ui.wait_text("ARMED", 30)
    at = armed_label.removeprefix("Arm for ").strip()
    await nav(ui, "Remote", "Start recording")
    await ui.wait_text("Recording", 240)  # fires on the camera's own clock
    await ui.wait_text("of video left", 60)
    await nav(ui, "Cameras", "Your cameras")
    assert not await ui.has("Scheduled "), "schedule still shown as armed after firing"
    return f"armed for {at}; the camera recorded on its own and disarmed"


async def g09_live(ui, run):
    await nav(ui, "Live", "Go live")
    switched = await ui.js(
        "(() => { const s = quak.nodes().filter(e => e.getAttribute('role') === 'switch'"
        " || e.getAttribute('aria-checked') !== null); if (!s.length) return false;"
        " s[0].click(); return true; })()"
    )
    assert switched, "test-mode switch not found"
    await asyncio.sleep(1)
    await ui.click("Go live", exact=True)
    await ui.wait_text("LIVE · streaming", 180)
    await asyncio.sleep(8)
    await ui.click("End stream", exact=True)
    await ui.wait_text("Stream ended", 60)
    labels = " ".join(await ui.labels())
    m = re.search(r"received (\d+)x(\d+) (\w+), (\d+) frames", labels)
    assert m and m.group(3) == "h264" and int(m.group(4)) > 50, f"stream not received: {labels[:300]}"
    return f"streamed through the lab hotspot; receiver got {m.group(0)}"


async def g10_diagnostics(ui, run):
    await nav(ui, "More", "Diagnostics")
    await ui.click("Diagnostics", exact=True)
    await ui.wait_text("received", 10)
    await ui.click("Queries", exact=True)
    await ui.wait_text("Status changed", 15)
    await ui.click("Camera", exact=True)
    await ui.wait_text("Firmware", 10)
    return "byte log with decoded meanings; camera page"


async def g11_cleanup(ui, run):
    names = await open_media(ui, run)
    ours = own_clips(names, run)
    assert len(ours) <= MAX_OWN_CLIPS, (
        f"{len(ours)} files look new, more than this test records; refusing to delete any"
    )
    for name in ours:  # only files that weren't on the card when the GUI test started
        await ui.click(name)
        await ui.click("Delete", exact=True)
        await ui.wait_text(f"Delete {name} from the camera?", 10)  # a lost tap would leave another file selected
        await ui.click("Delete", exact=True)
        await ui.wait_text("Deleted", 60)
        await asyncio.sleep(1)
    left = await open_media(ui, run)
    assert left == set(run.state["baseline"]), (
        f"card differs from the baseline: {sorted(left ^ set(run.state['baseline']))}"
    )
    return f"deleted {len(ours)} clip(s) recorded by this test; the card matches the baseline"


STEPS = [
    ("G01 Home", g01_home),
    ("G02 Sync clock", g02_clock),
    ("G03 Media baseline", g03_media_baseline),
    ("G04 Record + HiLight", g04_record),
    ("G05 Settings picker", g05_settings),
    ("G06 Live preview", g06_preview),
    ("G07 Media download", g07_media_download),
    ("G08 Scheduled capture", g08_schedule),
    ("G09 Livestream (test mode)", g09_live),
    ("G10 Diagnostics + camera page", g10_diagnostics),
    ("G11 Delete this run's clips", g11_cleanup),
]

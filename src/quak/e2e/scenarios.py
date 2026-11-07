"""The end-to-end scenarios (see docs/testing.md). Order matters: snapshot first,
media cleanup and settings restore last."""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import subprocess
import sys
import time
from pathlib import Path

from quak.core import registry as R
from quak.e2e.runner import Fail, Skip, check, scenario
from quak.net.http import WINDOW_EARLY, camera_epoch

SAFE_SETTINGS = (91, 83, 59, 135, 121, 88)  # never 175, 178, 180, 162


def wait_for(fn, timeout: float, every: float = 2.0, what: str = "condition"):
    end = time.time() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.time() > end:
            raise Fail(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(every)


def statuses(ctx, *names: str) -> dict:
    return ctx.quak("status", *names)["statuses"]


def default_route() -> str | None:
    try:
        out = subprocess.run(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=5).stdout
    except OSError:
        return None
    parts = out.split()
    return parts[parts.index("dev") + 1] if "dev" in parts else None


# ---------------------------------------------------------------- setup


@scenario("S00", "Preflight")
def s00(ctx):
    notes = []
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            notes.append(f"{tool} missing: media/preview/livestream checks will be skipped")
    if not ctx.sim and sys.platform.startswith("linux") and shutil.which("pactl"):
        sinks = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True).stdout
        if any("bluez" in l and "RUNNING" in l for l in sinks.splitlines()):
            ctx.finding("Bluetooth audio is streaming; pause it for reliable BLE (shared radio)")
    info = ctx.quak("info")
    check(info["model"] == "HERO11 Black", f"unexpected model {info['model']}")
    ctx.state["firmware"] = info["firmware"]
    return "; ".join(notes) or f"camera reachable, firmware {info['firmware']}"


@scenario("S01", "Info and full settings snapshot", minutes=1)
def s01(ctx):
    ledger = ctx.session_dir / "settings.ledger.json"
    if not ledger.exists():
        r = ctx.quak("snapshot", "--ledger", str(ledger))
        check(r["presets"] >= 3, f"snapshot covered only {r['presets']} presets")
    ctx.state["ledger"] = str(ledger)
    settings = ctx.quak("setting", "get")["settings"]
    check(len(settings) >= 25, f"only {len(settings)} settings readable")
    if "baseline" not in ctx.state and shutil.which("ffprobe") is not None:
        try:
            ctx.state["baseline"] = [m["path"] for m in ctx.quak("media", "list", timeout=180)["media"]]
        except Fail as e:
            ctx.finding(f"no media baseline (camera Wi-Fi unavailable?): {e}")
    ctx.save_state()
    return f"{len(settings)} settings; media baseline {len(ctx.state.get('baseline', []))} files"


@scenario("S02", "Connection robustness and keep-alive", minutes=2)
def s02(ctx):
    t = []
    for _ in range(3):
        t0 = time.time()
        check(statuses(ctx, "ready")["ready"] == 1, "camera not ready")
        t.append(time.time() - t0)
    watch = 6 if ctx.sim else 60
    r = ctx.quak("status", "ready", "--watch", str(watch), timeout=watch + 90)
    ka = r["keepalive"]
    check(ka["sent"] >= watch / 3 - 2, f"too few keep-alives sent: {ka}")
    check(
        ka["acked"] >= 0.95 * (ka["sent"] - 1), f"keep-alives not acknowledged: {ka}"
    )  # one may be in flight at close
    return f"3/3 connects, median {sorted(t)[1]:.1f} s per command; keep-alive {ka['acked']}/{ka['sent']}"


@scenario("S03", "Clock sync", minutes=1.5)
def s03(ctx):
    before = ctx.quak("clock")["offset_s"]
    ctx.quak("clock", "sync")
    after = ctx.quak("clock")
    check(abs(after["offset_s"]) <= 1.2, f"offset after sync {after['offset_s']} s")
    time.sleep(5 if ctx.sim else 60)
    later = ctx.quak("clock")["offset_s"]
    check(abs(later) <= 1.5, f"offset drifted to {later} s")
    if after.get("tz_minutes") is None:
        ctx.finding("camera did not report its timezone (0x10 reply without tz/dst)")
    return f"offset {before:+.1f} s -> {after['offset_s']:+.1f} s -> {later:+.1f} s; tz {after.get('tz_minutes')}"


@scenario("S04", "Settings round trip (CLI set/get)", minutes=1.5)
def s04(ctx):
    caps = ctx.quak("setting", "caps", *map(str, SAFE_SETTINGS))["capabilities"]
    current = ctx.quak("setting", "get", *map(str, SAFE_SETTINGS))["settings"]
    done = []
    for sid in SAFE_SETTINGS:
        allowed = caps.get(str(sid), [])
        orig = current.get(str(sid), {}).get("value")
        other = next((v for v in allowed if v != orig), None)
        if other is None:
            continue
        ctx.quak("setting", "set", str(sid), str(other))
        got = ctx.quak("setting", "get", str(sid))["settings"][str(sid)]["value"]
        check(got == other, f"setting {sid}: wrote {other}, read {got}")
        ctx.quak("setting", "set", str(sid), str(orig))
        check(
            ctx.quak("setting", "get", str(sid))["settings"][str(sid)]["value"] == orig, f"setting {sid} not restored"
        )
        done.append(sid)
    check(len(done) >= 4, f"only {len(done)} settings could be round-tripped")
    ctx.quak("setting", "set", "led", "disco", expect=1)  # bad values are refused before reaching the camera
    return f"round-tripped {done}"


@scenario("S05", "Presets")
def s05(ctx):
    presets = ctx.quak("preset", "list")["presets"]
    original = next(p["id"] for p in presets if p["current"])
    ctx.quak("preset", "group", "photo")
    check(statuses(ctx, "preset_group")["preset_group"] == 1001, "photo group not active")
    ctx.quak("preset", "group", "video")
    check(statuses(ctx, "preset_group")["preset_group"] == 1000, "video group not active")
    other = next(p for p in presets if p["group"] == 1002)
    ctx.quak("preset", "load", hex(other["id"]))
    check(statuses(ctx, "preset")["preset"] == other["id"], f"preset {other['name']} not active")
    ctx.quak("preset", "load", hex(original))
    return f"{len(presets)} presets; groups and {other['name']} loaded; back on 0x{original:08x}"


@scenario("S06", "Record a clip with a HiLight")
def s06(ctx):
    start = ctx.camera_time()
    r = ctx.quak("record", "--seconds", "5", "--hilight-at", "2")
    ctx.window(start, ctx.camera_time())
    check(r["videos_after"] == r["videos_before"] + 1, f"video count {r['videos_before']} -> {r['videos_after']}")
    check(r["hilights"] == 1, "HiLight not tagged")
    return f"clip {r['seconds']:.1f} s; videos {r['videos_before']} -> {r['videos_after']}"


@scenario("S07", "Turbo transfer (BLE)")
def s07(ctx):
    ctx.quak("turbo", "on")
    try:
        on = statuses(ctx, "turbo_transfer")["turbo_transfer"]
        if on != 1:
            ctx.finding("turbo transfer status was 0 on the next connection (turbo may end with the BLE session)")
    finally:  # left on, the camera refuses nearly everything, and later scenarios would fail for that
        ctx.quak("turbo", "off")
    check(statuses(ctx, "turbo_transfer")["turbo_transfer"] == 0, "turbo still on")
    return f"turbo status after 'on': {on}"


@scenario("S08", "Camera Wi-Fi AP on/off and credentials")
def s08(ctx):
    was = statuses(ctx, "ap_mode")["ap_mode"]
    ctx.quak("ap", "on")
    creds = ctx.quak("ap", "creds", "--show-secret")
    ssid = statuses(ctx, "access_point_ssid")["access_point_ssid"]
    check(creds["ssid"] == ssid, f"SSID mismatch: characteristic {creds['ssid']!r} vs status {ssid!r}")
    check(bool(creds["password"]), "empty password")
    if not was:
        ctx.quak("ap", "off")
        check(statuses(ctx, "ap_mode")["ap_mode"] == 0, "AP still on")
    return f"SSID {ssid}; password read ({len(creds['password'])} chars, not logged)"


@scenario("S16", "Camera Wi-Fi scan")
def s16(ctx):
    nets = ctx.quak("wifi", "scan", timeout=90)["networks"]
    check(len(nets) >= 1, "camera saw no networks")
    return f"camera sees {len(nets)} networks"


# ---------------------------------------------------------------- scheduled capture


def _arm(ctx, *extra) -> dt.datetime:
    r = ctx.quak("schedule", "arm", "+2m", "--duration", "15s", *extra)
    if r.get("warnings"):
        ctx.state.setdefault("schedule_warnings", r["warnings"])
    return dt.datetime.fromisoformat(r["armed_at"])


def _fast_forward(ctx, at: dt.datetime) -> None:
    if ctx.sim:
        ctx.quak("sim", "advance-to-schedule")
    else:
        wait = (at - dt.datetime.now()).total_seconds() - 15
        if wait > 0:
            ctx.log(f"  waiting {wait:.0f} s until shortly before {at:%H:%M}")
            time.sleep(wait)


async def _observe_fire(ctx, at: dt.datetime, timeout: float) -> dict:
    from quak.connect import Target, open_camera
    from quak.core.registry import Status

    async with open_camera(Target(sim=ctx.sim, address=ctx.address, keepalive=2.0)) as cam:
        videos = int(await cam.status(Status.VIDEOS))
        current, updates = await cam.watch_statuses([Status.ENCODING, Status.VIDEOS, Status.SCHEDULE_ARMED])
        started = None
        end = time.time() + timeout
        while time.time() < end:
            try:
                change = await asyncio.wait_for(updates.get(), 1.0)
            except asyncio.TimeoutError:
                change = {}
            if change.get(Status.ENCODING) == 1 and started is None:
                started = (await cam.clock())[0]
            if started and int(await cam.status(Status.VIDEOS)) > videos:
                break
        st = await cam.statuses([Status.VIDEOS, Status.SCHEDULE_ARMED, Status.ENCODING])
        return {
            "started": started,
            "videos_before": videos,
            "videos_after": int(st[Status.VIDEOS]),
            "armed_after": st[Status.SCHEDULE_ARMED],
        }


@scenario("S13", "Scheduled capture fires (camera on)", minutes=3)
def s13(ctx):
    at = _arm(ctx)
    check(ctx.quak("schedule", "show")["armed"], "not armed")
    _fast_forward(ctx, at)
    r = asyncio.run(_observe_fire(ctx, at, 90))
    check(r["started"] is not None, "recording never started")
    delta = (r["started"] - at).total_seconds()
    check(-1 <= delta <= 3, f"started {delta:+.1f} s from the scheduled time (camera clock)")
    check(r["videos_after"] == r["videos_before"] + 1, f"videos {r['videos_before']} -> {r['videos_after']}")
    ctx.window(at - dt.timedelta(seconds=5), at + dt.timedelta(seconds=60))
    ctx.finding(
        f"after firing, scheduled capture status 108 = {r['armed_after']} "
        f"({'one-shot' if not r['armed_after'] else 'stays armed'})"
    )
    if r["armed_after"]:
        ctx.quak("schedule", "off")
    return f"fired {delta:+.1f} s after {at:%H:%M}; 15 s clip recorded"


def _classify_after(ctx, at: dt.datetime, videos_before: int) -> str:
    if ctx.sim:
        ctx.quak("sim", "advance-to-schedule")
        time.sleep(1)
    else:
        wait = (at - dt.datetime.now()).total_seconds() + 15 + 30
        ctx.log(f"  camera off/asleep; checking back in {max(wait, 0):.0f} s")
        time.sleep(max(wait, 0))
    st = statuses(ctx, "videos", "scheduled_capture", "encoding")
    if ctx.sim and st["videos"] == videos_before:  # the simulator only ticks while connected
        time.sleep(4)
        st = statuses(ctx, "videos", "scheduled_capture", "encoding")
    fired = st["videos"] > videos_before
    outcome = "FIRED" if fired else ("ARMED_NOT_FIRED" if st["scheduled_capture"] else "CANCELLED")
    if fired:
        ctx.window(at - dt.timedelta(seconds=5), at + dt.timedelta(seconds=90))
    if st["scheduled_capture"]:
        ctx.quak("schedule", "off")
    return outcome


@scenario("S14", "Scheduled capture after BLE sleep", minutes=4)
def s14(ctx):
    videos = statuses(ctx, "videos")["videos"]
    at = _arm(ctx, "--sleep")
    outcome = _classify_after(ctx, at, videos)
    ctx.finding(f"schedule + BLE sleep (0x05): {outcome}")
    ctx.state["sleep_outcome"] = outcome
    return outcome


@scenario("S15", "Scheduled capture after power-button off", needs=("human",), minutes=4)
def s15(ctx):
    videos = statuses(ctx, "videos")["videos"]
    at = _arm(ctx)
    if ctx.sim:
        ctx.quak("sim", "power-button")
    else:
        ctx.prompt(f"Armed for {at:%H:%M}. Press and hold the camera's MODE button until it turns OFF.")
    outcome = _classify_after(ctx, at, videos)
    ctx.finding(f"schedule + power button: {outcome}")
    ctx.state["power_button_outcome"] = outcome
    return outcome


# ---------------------------------------------------------------- network


@scenario("S17", "Livestream to the lab RTMP receiver", needs=("hotspot", "ffmpeg"), minutes=5)
def s17(ctx):
    seconds = 6 if ctx.sim else 20
    out = ctx.session_dir / "livestream.ts"
    r = ctx.quak("livestream", "--lab", "--seconds", str(seconds), "--res", "720", "--out", str(out), timeout=400)
    check("STREAMING" in r["states"], f"states {r['states']}")
    video = (r.get("probe") or {}).get("video")
    check(video and video["codec"] == "h264", f"received {r.get('probe')}")
    check(video["width"] and video["width"] >= 1200, f"width {video['width']}")
    frames = video.get("frames") or 0
    check(frames >= 0.8 * 10 * seconds, f"only {frames} frames for {seconds} s")
    if not ctx.sim and default_route() not in (None, ctx.state.get("route")):
        ctx.finding(f"default route changed to {default_route()}")
    return f"states {' → '.join(r['states'])}; received {video['width']}x{video['height']}, {frames} frames"


@scenario("S09", "Join the camera's Wi-Fi and use its HTTP API", needs=("wifi",), minutes=1.5)
def s09(ctx):
    route = default_route()
    ctx.state["route"] = route
    files = ctx.quak("media", "list", timeout=180)["media"]
    after = default_route()
    check(route == after, f"default route changed: {route} -> {after}")
    return f"{len(files)} files listed; default route stayed on {route}"


@scenario("S10", "Media lifecycle: list, info, download, thumbnail", needs=("wifi", "ffmpeg"), minutes=2.5)
def s10(ctx):
    from quak.net.stream import probe_file

    if "baseline" not in ctx.state:
        raise Skip("no media baseline from S01; refusing to identify files to delete")
    before = {f["path"] for f in ctx.quak("media", "list", timeout=180)["media"]}
    start = ctx.camera_time()
    ctx.quak("record", "--seconds", "3")
    end = ctx.camera_time()
    ctx.window(start, end)
    files = ctx.quak("media", "list", timeout=180)["media"]
    mine = [
        f
        for f in files
        if f["path"] not in ctx.state["baseline"]
        and f["path"] not in before
        and camera_epoch(start) - WINDOW_EARLY <= f["created"] <= camera_epoch(end) + 2
    ]
    check(len(mine) == 1, f"expected 1 new clip in the window, found {[f['path'] for f in mine]}")
    f = mine[0]
    info = ctx.quak("media", "info", f["path"])["info"]
    check(abs(float(info.get("dur", 0)) - 3) <= 1.5, f"duration {info.get('dur')}")
    dl = ctx.quak("media", "download", f["path"], "--out", str(ctx.session_dir / "media"), "--turbo", timeout=600)[
        "downloaded"
    ][0]
    check(dl["bytes"] == f["size"], f"downloaded {dl['bytes']} bytes, listed {f['size']}")
    probe = probe_file(Path(dl["file"]))
    check(abs(probe["duration"] - 3) <= 1.5, f"ffprobe duration {probe['duration']}")
    thumb = ctx.session_dir / "thumb.jpg"
    ctx.quak("media", "thumbnail", f["path"], "--out", str(thumb))
    check(thumb.read_bytes()[:2] == b"\xff\xd8", "thumbnail is not a JPEG")
    return f"{f['path']}: {f['size'] / 1e6:.1f} MB, {probe['duration']:.1f} s, sha256 {dl['sha256'][:12]}…"


@scenario("S11", "Live preview over Wi-Fi", needs=("wifi", "ffmpeg"), minutes=1.5)
def s11(ctx):
    r = ctx.quak("preview", "--probe", "--seconds", "12", timeout=120)
    video = r["preview"]["video"]
    check(video and video["codec"] and video["width"], f"no video in preview: {r['preview']}")
    return f"preview {video['codec']} {video['width']}x{video['height']}"


@scenario("S12", "Delete only this session's clips", needs=("wifi",), minutes=1)
def s12(ctx):
    if "baseline" not in ctx.state:
        raise Skip("no media baseline; leaving clips on the card")
    files = ctx.quak("media", "list", timeout=180)["media"]
    ours = [
        f
        for f in files
        if f["path"] not in ctx.state["baseline"] and any(a <= f["created"] <= b for a, b in ctx.windows)
    ]
    for f in ours:
        ctx.quak("media", "delete", f["path"], "--yes")
    remaining = {f["path"] for f in ctx.quak("media", "list", timeout=180)["media"]}
    check(
        remaining == set(ctx.state["baseline"]),
        f"card differs from the baseline: "
        f"extra {sorted(remaining - set(ctx.state['baseline']))}, "
        f"missing {sorted(set(ctx.state['baseline']) - remaining)}",
    )
    return f"deleted {len(ours)} test clips; the other {len(remaining)} files are untouched"


@scenario("S18", "Restore settings and verify", minutes=1)
def s18(ctx):
    ledger = ctx.state.get("ledger")
    if not ledger:
        raise Skip("no snapshot")
    restored = ctx.quak("restore", "--ledger", ledger, timeout=300)["restored"]
    check_ledger = ctx.session_dir / "after.ledger.json"
    check_ledger.unlink(missing_ok=True)
    ctx.quak("snapshot", "--ledger", str(check_ledger), timeout=300)
    import json

    before = json.loads(Path(ledger).read_text())
    after = json.loads(check_ledger.read_text())
    diffs = {
        pid: {
            s: (v, after["presets"].get(pid, {}).get(s))
            for s, v in vals.items()
            if after["presets"].get(pid, {}).get(s) != v
        }
        for pid, vals in before["presets"].items()
    }
    diffs = {k: v for k, v in diffs.items() if v}
    # settings missing from GoPro's published list (e.g. 31) are camera-internal: a HERO11 recomputed
    # one after a power cycle and refuses writes to it. Report them, fail only on documented ones.
    odd = {pid: {s: d for s, d in v.items() if int(s) not in R.SETTINGS} for pid, v in diffs.items()}
    odd = {k: v for k, v in odd.items() if v}
    if odd:
        ctx.finding(f"undocumented settings changed by the camera (not restorable): {odd}")
    diffs = {pid: {s: d for s, d in v.items() if int(s) in R.SETTINGS} for pid, v in diffs.items()}
    diffs = {k: v for k, v in diffs.items() if v}
    check(not diffs, f"settings differ from the snapshot: {diffs}")
    check(after["preset"] == before["preset"], "original preset not active")
    return f"restored {len(restored)} values; all {len(before['presets'])} presets match the snapshot" + (
        f" (undocumented, left alone: {odd})" if odd else ""
    )

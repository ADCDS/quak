"""`quak`: control a GoPro HERO11 from the command line.

Every command opens one Bluetooth session, does its job and disconnects. With --sim (or
QUAK_SIM=1) it talks to a simulated camera instead; its state is kept between runs in
$QUAK_SIM_STATE (default ~/.local/state/quak/sim-camera.json).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import getpass
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from quak import __version__, config, flows
from quak.camera import Camera
from quak.connect import Target, open_camera
from quak.core import proto, tables
from quak.core import registry as R
from quak.core.errors import NetworkError, NotFound, QuakError, Rejected
from quak.core.registry import Status
from quak.guard import SettingsGuard
from quak.net import stream
from quak.net.hotspot import lab_hotspot
from quak.net.http import PREVIEW_PORT, CameraHttp
from quak.net.wifi import camera_wifi, recover_network

# ---------------------------------------------------------------- output


# Its refusals don't give a reason, and a connection dropped during a download can leave it on
TURBO_HINT = "The camera is in Turbo Transfer mode, which blocks recording, settings and preview: run `quak turbo off`"


class Out:
    def __init__(self, as_json: bool, verbose: bool) -> None:
        self.as_json, self.verbose = as_json, verbose
        self.data: dict = {}

    def log(self, msg: str) -> None:
        if self.verbose or not self.as_json:
            print(msg, file=sys.stderr if self.as_json else sys.stdout, flush=True)

    def debug(self, msg: str) -> None:
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

    def result(self, data: dict, text: str | None = None) -> None:
        self.data.update(data)
        if not self.as_json and text is not None:
            print(text, flush=True)

    def finish(self) -> None:
        if self.as_json:
            print(json.dumps(self.data, default=str, indent=1))


def fmt_settings(values: dict[int, int]) -> str:
    return "\n".join(
        f"{sid:>3}  {R.setting_name(sid):<28} {R.value_label(sid, v)}" for sid, v in sorted(values.items())
    )


def fmt_statuses(values: dict) -> str:
    return "\n".join(f"{sid:>3}  {R.status_name(sid):<34} {v}" for sid, v in sorted(values.items()))


def parse_ids(items: list[str], kind: str) -> list[int]:
    out = []
    for item in items:
        if kind == "setting":
            out.append(R.setting_id(item))
        elif item.isdigit():
            out.append(int(item))
        else:
            ids = [k for k, v in R.STATUSES.items() if v == R.slug(item)]
            if not ids:
                raise KeyError(f"unknown status {item!r}")
            out.append(ids[0])
    return out


# ---------------------------------------------------------------- commands


async def cmd_pair(cam: Camera, a, out: Out) -> None:
    info = await cam.hardware_info()
    _warn_about_model(info.model_name, out)
    if not a.sim:  # a simulated camera must never replace the real default
        config.remember_camera(cam.link.address, model=info.model_name, firmware=info.firmware)
    out.result(
        {"address": cam.link.address, "new_bond": cam.new_bond, "model": info.model_name, "firmware": info.firmware},
        f"paired with {info.model_name} (firmware {info.firmware}); "
        f"{'new bond' if cam.new_bond else 'already bonded'}." + ("" if a.sim else " Saved as the default camera."),
    )


def _warn_about_model(model: str, out: Out) -> None:
    note = R.model_note(model)
    if note:
        out.log(f"warning: {note}")


async def _nearby_cameras(a) -> list[dict]:
    if a.sim:
        return [{"address": "SIM", "name": "GoPro (simulated)", "pairing": True}]
    from quak.transport.ble import scan_cameras

    return await scan_cameras(getattr(a, "seconds", 8.0), a.hci)


async def _camera_on_its_pairing_screen(a) -> str:
    cameras = await _nearby_cameras(a)
    ready = [c for c in cameras if c["pairing"]]
    if len(ready) == 1:
        return ready[0]["address"]
    if ready:
        raise NotFound(
            "more than one camera is on its pairing screen: choose one with --address "
            + " or ".join(c["address"] for c in ready)
        )
    if config.default_address():
        return config.default_address()
    raise NotFound(
        "no camera is on its pairing screen. On the camera, open Preferences > Connections > Connect Device > "
        "GoPro Quik App, then try again"
    )


async def cmd_info(cam: Camera, a, out: Out) -> None:
    info = await cam.hardware_info()
    _warn_about_model(info.model_name, out)
    st = await cam.statuses(
        [
            Status.BATTERY_PERCENT,
            Status.REMAINING_VIDEO,
            Status.VIDEOS,
            Status.PHOTOS,
            Status.PRESET,
            Status.SCHEDULE_ARMED,
            Status.AP_MODE,
        ]
    )
    t, tz, dst = await cam.clock()
    drift = (t - dt.datetime.now()).total_seconds()
    data = {
        "model": info.model_name,
        "firmware": info.firmware,
        "ap_ssid": info.ap_ssid,
        "battery_percent": st[Status.BATTERY_PERCENT],
        "video_left_s": st[Status.REMAINING_VIDEO],
        "videos": st[Status.VIDEOS],
        "photos": st[Status.PHOTOS],
        "preset": st[Status.PRESET],
        "schedule_armed": bool(st[Status.SCHEDULE_ARMED]),
        "camera_wifi": bool(st[Status.AP_MODE]),
        "camera_clock": t.isoformat(),
        "clock_offset_s": round(drift, 1),
        "tz_minutes": tz,
        "dst": dst,
    }
    out.result(
        data,
        "\n".join(
            [
                f"{info.model_name}, firmware {info.firmware}",
                f"battery {st[Status.BATTERY_PERCENT]}% · {st[Status.REMAINING_VIDEO] // 60} min of video left · "
                f"{st[Status.VIDEOS]} videos, {st[Status.PHOTOS]} photos",
                f"preset 0x{int(st[Status.PRESET]):08x} ({R.preset_group_of(int(st[Status.PRESET]))}) · "
                f"schedule {'armed' if st[Status.SCHEDULE_ARMED] else 'off'} · camera Wi-Fi "
                f"{'on' if st[Status.AP_MODE] else 'off'}",
                f"camera clock {t:%Y-%m-%d %H:%M:%S} ({drift:+.0f} s vs this machine)",
            ]
        ),
    )


async def cmd_status(cam: Camera, a, out: Out) -> None:
    ids = parse_ids(a.ids, "status")
    values = await cam.statuses(ids)
    out.result({"statuses": {R.status_name(k): v for k, v in values.items()}}, fmt_statuses(values))
    if a.watch:
        current, updates = await cam.watch_statuses(ids or sorted(values))
        end = time.time() + a.watch
        while time.time() < end:
            try:
                change = await asyncio.wait_for(updates.get(), end - time.time())
            except asyncio.TimeoutError:
                break
            out.log(f"{time.strftime('%H:%M:%S')}  " + ", ".join(f"{R.status_name(k)}={v}" for k, v in change.items()))
        out.result({"keepalive": {"sent": cam.s.keepalive_sent, "acked": cam.s.keepalive_acked}})


async def cmd_setting(cam: Camera, a, out: Out) -> None:
    if a.action == "get":
        values = await cam.settings(parse_ids(a.args, "setting"))
        out.result(
            {
                "settings": {
                    str(k): {"name": R.setting_name(k), "value": v, "label": R.value_label(k, v)}
                    for k, v in values.items()
                }
            },
            fmt_settings(values),
        )
    elif a.action == "caps":
        caps = await cam.capabilities(parse_ids(a.args, "setting"))
        out.result(
            {"capabilities": {str(k): v for k, v in caps.items()}},
            "\n".join(
                f"{k:>3}  {R.setting_name(k):<28} " + (", ".join(R.value_label(k, x) for x in v) or "(none right now)")
                for k, v in sorted(caps.items())
            ),
        )
    elif a.action == "set":
        if len(a.args) != 2:
            raise SystemExit("usage: quak setting set NAME VALUE")
        sid = R.setting_id(a.args[0])
        value = R.parse_value(sid, a.args[1])
        await cam.set_setting(sid, value)
        out.result({"setting": sid, "value": value}, f"{R.setting_name(sid)} = {R.value_label(sid, value)}")
    elif a.action == "raw":
        payload = bytes.fromhex("".join(a.args))
        if len(payload) < 2:
            raise ValueError("setting raw needs hex bytes: setting id, value length, value")
        status = await cam.s.write_setting(payload[0], payload[2 : 2 + payload[1]])
        out.result({"status": status}, f"camera answered status {status} ({R.TLV_STATUS.get(status, '?')})")
    elif a.action == "list":
        out.result(
            {"settings": {str(k): v for k, v in R.SETTINGS.items()}},
            "\n".join(
                f"{k:>3}  {R.slug(v['name']):<28} {', '.join(v['options'].values())}" for k, v in R.SETTINGS.items()
            ),
        )


async def cmd_clock(cam: Camera, a, out: Out) -> None:
    if a.action == "sync":
        await cam.sync_clock()
    t, tz, dst = await cam.clock()
    drift = (t - dt.datetime.now()).total_seconds()
    out.result(
        {"camera_clock": t.isoformat(), "offset_s": round(drift, 1), "tz_minutes": tz, "dst": dst},
        f"camera clock {t:%Y-%m-%d %H:%M:%S} ({drift:+.0f} s vs this machine"
        + (f", UTC{tz / 60:+g}{' DST' if dst else ''})" if tz is not None else ")"),
    )


async def cmd_shutter(cam: Camera, a, out: Out) -> None:
    await cam.shutter(a.state == "on")
    out.result({"shutter": a.state}, f"shutter {a.state}")


async def cmd_record(cam: Camera, a, out: Out) -> None:
    r = await flows.record_clip(cam, a.seconds, hilight_at=a.hilight_at, group=a.group)
    out.result(
        r.__dict__,
        f"recorded {r.seconds:.1f} s; videos {r.videos_before} -> {r.videos_after}"
        + (" (with a HiLight)" if r.hilights else ""),
    )


async def cmd_simple(cam: Camera, a, out: Out) -> None:
    if a.command == "hilight":
        await cam.hilight()
        out.result({"hilight": True}, "HiLight tagged")
    elif a.command == "sleep":
        await cam.sleep()
        out.result({"sleep": True}, "camera going to sleep")
    elif a.command == "turbo":
        await cam.turbo(a.state == "on")
        out.result({"turbo": a.state}, f"turbo transfer {a.state}")
    elif a.command == "control":
        await cam.camera_control(a.mode)
        out.result({"control": a.mode}, f"camera control: {a.mode}")


async def cmd_preset(cam: Camera, a, out: Out) -> None:
    if a.action == "list":
        groups = await cam.presets()
        current = int(await cam.status(Status.PRESET))
        lines, data = [], []
        for g in groups:
            lines.append(proto.PRESET_GROUP.get(g["id"], str(g["id"])))
            for p in g["preset_array"]:
                name = p.get("custom_name") or _title(p.get("title_id"))
                data.append({"group": g["id"], "id": p["id"], "name": name, "current": p["id"] == current})
                lines.append(f"  {'*' if p['id'] == current else ' '} 0x{p['id']:08x}  {name}")
        out.result({"presets": data}, "\n".join(lines))
    elif a.action == "load":
        if not a.value:
            raise ValueError("preset load needs a preset id, from `quak preset list`")
        await cam.load_preset(int(a.value, 0))
        out.result({"preset": int(a.value, 0)}, f"loaded preset {a.value}")
    elif a.action == "group":
        if not a.value:
            raise ValueError("preset group needs video, photo or timelapse")
        await cam.load_group(a.value)
        out.result({"group": a.value}, f"loaded the {a.value} group")


def _title(title_id) -> str:
    return tables.PRESET_TITLES.get(title_id, f"preset {title_id}")


async def cmd_ap(cam: Camera, a, out: Out) -> None:
    if a.action in ("on", "off"):
        await cam.set_ap(a.action == "on")
        value = await cam.wait_status(Status.AP_MODE, lambda v: bool(v) == (a.action == "on"), 15)
        out.result({"camera_wifi": bool(value)}, f"camera Wi-Fi {a.action}")
    else:
        ssid, password = await cam.wifi_credentials()
        shown = password if a.show_secret else "*" * len(password)
        out.result({"ssid": ssid, "password": password if a.show_secret else None}, f"SSID {ssid}\npassword {shown}")


async def cmd_schedule(cam: Camera, a, out: Out) -> None:
    if a.action == "arm":
        when = R.parse_schedule_time(a.when, dt.datetime.now())
        preset = int(a.preset, 0) if a.preset[:1].isdigit() else a.preset
        r = await flows.arm_schedule(
            cam,
            when,
            duration=a.duration,
            preset=preset,
            sleep_after=a.sleep,
            allow_no_limit=a.allow_no_limit,
            sync_clock=not a.no_sync_clock,
            log=out.log,
        )
        for w in r.warnings:
            out.log(f"warning: {w}")
        out.result(
            {
                "armed_at": r.at.isoformat(),
                "duration": r.duration,
                "preset": r.preset,
                "clock_synced": r.clock_synced,
                "slept": r.slept,
                "warnings": r.warnings,
            },
            f"armed for {r.at:%H:%M} ({'today' if r.at.date() == dt.date.today() else 'tomorrow'}) · "
            f"{r.duration} · preset 0x{r.preset:08x}" + (" · camera asleep" if r.slept else ""),
        )
    elif a.action == "off":
        await flows.disarm_schedule(cam)
        out.result({"armed": False}, "scheduled capture off")
    else:
        s = await flows.schedule_state(cam)
        out.result(
            s,
            f"scheduled capture {'armed for ' + s['time'] if s['armed'] else 'off'}"
            + (f" · preset 0x{int(s['preset']):08x}" if s["armed"] else ""),
        )


async def cmd_snapshot(cam: Camera, a, out: Out) -> None:
    guard = SettingsGuard(cam, Path(a.ledger))
    await guard.snapshot(all_presets=not a.current_only)
    out.result(
        {"ledger": a.ledger, "presets": len(guard.state["presets"])},
        f"snapshot of {len(guard.state['presets'])} preset(s) written to {a.ledger}",
    )


async def cmd_restore(cam: Camera, a, out: Out) -> None:
    guard = SettingsGuard.from_ledger(cam, Path(a.ledger))
    done = await guard.restore()
    refused = guard.refused
    text = "\n".join(done) or "nothing to restore"
    if refused:
        text += "\nnot restorable (the camera refuses to write these now):\n  " + "\n  ".join(refused)
    out.result({"restored": done, "refused": refused}, text)


# ---------------------------------------------------------------- Wi-Fi / HTTP commands


@contextlib.asynccontextmanager
async def http_api(cam: Camera, a, out: Out):
    """The camera's HTTP API: the simulator's in-process server, or the real camera's Wi-Fi."""
    if a.sim:
        from quak.transport.sim_http import SimHttpServer

        server = SimHttpServer(cam.link.camera, asyncio.get_running_loop())
        try:
            yield CameraHttp(server.start())
        finally:
            server.stop()
    else:
        async with camera_wifi(cam, base_url=a.http_url, mode=a.wifi, log=out.log) as api:
            yield api


def _when(created: int) -> str:
    return dt.datetime.fromtimestamp(created, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def cmd_media(cam: Camera, a, out: Out) -> None:
    async with http_api(cam, a, out) as api:
        if a.action == "list":
            files = await asyncio.to_thread(api.media_list)
            out.result(
                {"media": [f.__dict__ | {"path": f.path} for f in files]},
                "\n".join(f"{f.path:<24} {f.size / 1e6:8.1f} MB  {_when(f.created)}" for f in files)
                or "no media on the card",
            )
            return
        if not a.paths:
            raise ValueError(f"media {a.action} needs a file path, e.g. 100GOPRO/GX010001.MP4")
        if a.action == "info":
            info = await asyncio.to_thread(api.media_info, a.paths[0])
            out.result({"info": info}, json.dumps(info, indent=1))
        elif a.action == "thumbnail":
            data = await asyncio.to_thread(api.thumbnail, a.paths[0])
            dest = Path(a.out or Path(a.paths[0]).name + ".jpg")
            dest.write_bytes(data)
            out.result({"thumbnail": str(dest), "bytes": len(data)}, f"saved {dest} ({len(data)} bytes)")
        elif a.action == "download":
            dest_dir = Path(a.out or ".")
            if a.turbo:
                await cam.turbo(True)
            try:
                saved = []
                for path in a.paths:
                    dest = dest_dir / Path(path).name
                    last = [0.0]

                    def progress(done, total, path=path, last=last):
                        if time.time() - last[0] > 1 and total:
                            last[0] = time.time()
                            out.log(f"  {path}: {done * 100 // total}%")

                    size, digest, where = await asyncio.to_thread(api.download, path, dest, progress)
                    saved.append({"path": path, "file": str(where), "bytes": size, "sha256": digest})
                    out.log(f"saved {where} ({size / 1e6:.1f} MB)")
            finally:
                if a.turbo:
                    await cam.turbo(False)
            out.result({"downloaded": saved})
        elif a.action == "delete":
            if not a.yes:
                raise ValueError("deleting from the camera can't be undone; add --yes")
            for path in a.paths:
                await asyncio.to_thread(api.delete, path)
                out.log(f"deleted {path}")
            out.result({"deleted": a.paths})


async def cmd_preview(cam: Camera, a, out: Out) -> None:
    async with http_api(cam, a, out) as api:
        await asyncio.to_thread(api.preview_start, a.port if a.port != PREVIEW_PORT else None)
        out.log(f"preview streaming to UDP port {a.port}")
        try:
            if a.probe:
                info = await asyncio.to_thread(stream.probe_udp, a.port, min(a.seconds, 20))
                out.result({"preview": info}, f"preview: {info['video']}")
            elif a.play and not shutil.which("ffplay"):
                raise ValueError("--play needs ffplay, which comes with FFmpeg")
            elif a.play:
                proc = await asyncio.create_subprocess_exec(
                    "ffplay",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-fflags",
                    "nobuffer",
                    "-flags",
                    "low_delay",
                    "-window_title",
                    "Quak preview",
                    f"udp://@:{a.port}",
                )
                end = time.time() + a.seconds
                while time.time() < end and proc.returncode is None:
                    await asyncio.to_thread(api.keep_alive)
                    await asyncio.sleep(2.5)
                proc.terminate()
                out.result({"played_s": a.seconds})
            else:
                out.log(f"watch it with --play, or open udp://@:{a.port} in VLC or ffplay")
                end = time.time() + a.seconds
                while time.time() < end:
                    await asyncio.to_thread(api.keep_alive)
                    await asyncio.sleep(2.5)
                out.result({"streamed_s": a.seconds})
        finally:
            await asyncio.to_thread(api.preview_stop)


async def cmd_wifi(cam: Camera, a, out: Out) -> None:
    if a.action == "scan":
        nets = await cam.scan_networks()
        out.result(
            {"networks": nets},
            "\n".join(
                f"{n.get('ssid', ''):<32} {'▂▄▆█'[: max(1, n.get('signal_strength_bars', 0))]:<4} "
                f"{n.get('signal_frequency_mhz', 0)} MHz" + ("  remembered" if n.get("scan_entry_flags", 0) & 2 else "")
                for n in nets
            ),
        )
    elif a.action == "connect":
        if not a.ssid:
            raise ValueError("wifi connect needs --ssid")
        password = (
            None
            if a.open
            else (
                sys.stdin.readline().rstrip("\n") if a.password_stdin else getpass.getpass(f"password for {a.ssid}: ")
            )
        )
        state = await flows.join_camera_network(cam, a.ssid, password, log=out.log)
        out.result({"ssid": a.ssid, "state": state}, f"camera joined {a.ssid} ({state})")
    elif a.action == "release":
        await cam.release_network()
        out.result({"released": True}, "camera left the network")


async def cmd_livestream(cam: Camera, a, out: Out) -> None:
    try:
        await _livestream(cam, a, out)
    except flows.LivestreamStuck as e:
        with contextlib.suppress(QuakError):
            await cam.sleep()
        raise QuakError(f"{e}. Quak put the camera to sleep to reset it: run the command again") from None


async def _livestream(cam: Camera, a, out: Out) -> None:
    if not a.lab and not a.url:
        raise ValueError("livestream needs --url rtmp://... (with --ssid for the camera's network) or --lab")
    if not a.lab:
        password = None
        if a.ssid and not a.open:
            password = getpass.getpass(f"password for {a.ssid}: ")
        r = await flows.livestream(
            cam,
            a.url,
            a.seconds,
            resolution=a.res,
            encode=a.encode,
            lens=a.lens,
            network=(a.ssid, password) if a.ssid else None,
            log=out.log,
        )
        out.result(r.__dict__, f"streamed {r.streamed_s:.0f} s · states {' → '.join(r.states)}")
        return
    # lab mode: this PC is the access point and the RTMP server; no real credentials involved
    received = Path(a.out or config.state_dir() / "livestream-received.ts")
    receiver = stream.RtmpReceiver(received, timeout=int(a.seconds) + 90)
    try:
        if a.sim:
            receiver.start()
            from quak.net.hotspot import lab_credentials
            from quak.transport.sim import SimNetwork

            sim_cam = cam.link.camera
            ssid, password = lab_credentials()
            if not any(n.ssid == ssid for n in sim_cam.networks):
                sim_cam.networks.append(SimNetwork(ssid, password))
            pusher = stream.TestPattern()
            sim_cam.on_stream = lambda url, on: (
                pusher.start(url.replace("10.42.0.1", "127.0.0.1")) if on else pusher.stop()
            )
            url = receiver.url_for("10.42.0.1")
            r = await flows.livestream(
                cam,
                url,
                a.seconds,
                resolution=a.res,
                encode=a.encode,
                lens=a.lens,
                network=(ssid, password),
                log=out.log,
            )
            pusher.stop()
        else:
            async with lab_hotspot(log=out.log) as hs:
                receiver.start(hs.gateway)
                url = receiver.url_for(hs.gateway)
                r = await flows.livestream(
                    cam,
                    url,
                    a.seconds,
                    resolution=a.res,
                    encode=a.encode,
                    lens=a.lens,
                    network=(hs.ssid, hs.password),
                    log=out.log,
                )
    finally:
        errors = await asyncio.to_thread(receiver.stop)
    info = await asyncio.to_thread(stream.probe_file, received, True) if received.exists() else {"video": None}
    out.result(
        r.__dict__ | {"received": str(received), "probe": info, "receiver_errors": errors[-300:]},
        f"streamed {r.streamed_s:.0f} s · states {' → '.join(r.states)} · received {info.get('video')}",
    )


async def hotspot_command(a, out: Out) -> None:
    from quak.net.hotspot import lab_credentials
    from quak.net.nmcli import HOTSPOT_PROFILE, NetworkManager

    ssid, _ = lab_credentials()
    if a.action == "status":
        out.result({"ssid": ssid}, f"lab hotspot SSID {ssid} (credentials in {config.hotspot_file()})")
    elif a.action == "down":
        await asyncio.to_thread(NetworkManager().down, HOTSPOT_PROFILE)
        out.result({"down": True}, "lab hotspot removed")
    else:
        async with lab_hotspot(log=out.log) as hs:
            out.log(f"hotspot {hs.ssid} up at {hs.gateway} on {hs.device}; Ctrl-C to stop")
            await asyncio.sleep(a.seconds)
        out.result({"ssid": hs.ssid, "gateway": hs.gateway})


# ---------------------------------------------------------------- parser


def _add_global_options(p: argparse.ArgumentParser, after_command: bool = False) -> None:
    def default(value):  # after the command, an option that isn't given must not overwrite the one before it
        return {} if after_command else {"default": value}

    p.add_argument("--address", help="camera BLE address (default: the paired camera, or $QUAK_CAMERA_ADDRESS)")
    p.add_argument(
        "--sim", action="store_true", help="talk to the simulated camera", **default(os.environ.get("QUAK_SIM") == "1")
    )
    p.add_argument("--hci", help="Bluetooth adapter (Linux)", **default("hci0"))
    p.add_argument("--retries", type=int, **default(3))
    p.add_argument("--json", action="store_true", help="machine-readable output", **default(False))
    p.add_argument("-v", "--verbose", action="store_true", help="show progress and raw frames", **default(False))
    p.add_argument(
        "--wifi",
        choices=["auto", "assume"],
        help="auto: join the camera's Wi-Fi if needed; assume: a connection you set up yourself is already there",
        **default("auto"),
    )
    p.add_argument("--http-url", help="camera HTTP base URL (default http://10.5.5.9:8080)")
    p.add_argument(
        "--wifi-adapter",
        metavar="NAME",
        help="Wi-Fi adapter for the camera, even if this computer is online "
        "through it (default: one it isn't, or $QUAK_WIFI_ADAPTER)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quak", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"quak {__version__}")
    _add_global_options(p)
    # the same options after the command too (quak pair --address X), without resetting what came before it
    after = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    _add_global_options(after, after_command=True)
    commands = p.add_subparsers(dest="command", metavar="COMMAND")

    class sub:  # every command takes the global options
        @staticmethod
        def add_parser(name, **kw):
            return commands.add_parser(name, parents=[after], **kw)

    s = sub.add_parser("scan", help="look for cameras nearby and show their addresses")
    s.add_argument("--seconds", type=float, default=8.0)
    s = sub.add_parser("pair", help="bond with a camera (open its pairing screen first) and make it the default")
    s.add_argument("--wait-pairing-mode", action="store_true", help="wait until the camera shows its pairing screen")
    sub.add_parser("info", help="model, firmware, battery, card, preset, clock")
    s = sub.add_parser("status", help="read statuses (all, or by id/name)")
    s.add_argument("ids", nargs="*")
    s.add_argument("--watch", type=float, metavar="SECONDS", help="then print pushed changes for SECONDS")
    s = sub.add_parser("setting", help="get / caps / set / raw / list")
    s.add_argument("action", choices=["get", "caps", "set", "raw", "list"])
    s.add_argument("args", nargs="*")
    s = sub.add_parser("clock", help="show or sync the camera clock")
    s.add_argument("action", choices=["get", "sync"], nargs="?", default="get")
    s = sub.add_parser("shutter", help="start or stop capture")
    s.add_argument("state", choices=["on", "off"])
    s = sub.add_parser("record", help="record a clip and report it")
    s.add_argument("--seconds", type=float, default=5)
    s.add_argument("--hilight-at", type=float, metavar="S", help="tag a HiLight S seconds in")
    s.add_argument("--group", default="video", choices=["video", "timelapse"])
    sub.add_parser("hilight", help="tag a HiLight")
    sub.add_parser("sleep", help="put the camera to sleep")
    s = sub.add_parser("turbo", help="turbo transfer on/off")
    s.add_argument("state", choices=["on", "off"])
    s = sub.add_parser("control", help="claim or release camera control")
    s.add_argument("mode", choices=["idle", "external"])
    s = sub.add_parser("preset", help="list / load ID / group video|photo|timelapse")
    s.add_argument("action", choices=["list", "load", "group"])
    s.add_argument("value", nargs="?")
    s = sub.add_parser("ap", help="camera Wi-Fi access point: on / off / creds")
    s.add_argument("action", choices=["on", "off", "creds"])
    s.add_argument("--show-secret", action="store_true")
    s = sub.add_parser("schedule", help="show / off / arm WHEN --duration D")
    s.add_argument("action", choices=["show", "off", "arm"], nargs="?", default="show")
    s.add_argument("when", nargs="?", help="HH:MM or +2m")
    s.add_argument("--duration", help="15s, 30s, 1m, 5m, 15m, 30m, 1h, 2h, 3h")
    s.add_argument("--preset", default="video", help="video, timelapse, or a preset id")
    s.add_argument("--sleep", action="store_true", help="put the camera to sleep after arming")
    s.add_argument("--allow-no-limit", action="store_true", help="arm without a duration (records until stopped)")
    s.add_argument("--no-sync-clock", action="store_true")
    s = sub.add_parser("snapshot", help="save every preset's settings to a ledger (undo with restore)")
    s.add_argument("--ledger", required=True)
    s.add_argument("--current-only", action="store_true", help="only the active preset")
    s = sub.add_parser("restore", help="put back the settings saved by snapshot")
    s.add_argument("--ledger", required=True)
    s = sub.add_parser("media", help="camera media over Wi-Fi: list / info / download / thumbnail / delete")
    s.add_argument("action", choices=["list", "info", "download", "thumbnail", "delete"])
    s.add_argument("paths", nargs="*", help="e.g. 100GOPRO/GX010001.MP4")
    s.add_argument("--out", help="download directory / thumbnail file")
    s.add_argument("--turbo", action="store_true", help="turbo transfer while downloading")
    s.add_argument("--yes", action="store_true", help="confirm deletion")
    s = sub.add_parser("preview", help="live preview over the camera's Wi-Fi")
    s.add_argument("--seconds", type=float, default=15)
    s.add_argument("--port", type=int, default=PREVIEW_PORT)
    s.add_argument("--probe", action="store_true", help="check the stream with ffprobe instead of showing it")
    s.add_argument("--play", action="store_true", help="show it with ffplay")
    s = sub.add_parser("wifi", help="camera-side Wi-Fi: scan / connect --ssid S / release")
    s.add_argument("action", choices=["scan", "connect", "release"])
    s.add_argument("--ssid")
    s.add_argument("--open", action="store_true", help="the network has no password")
    s.add_argument("--password-stdin", action="store_true")
    s = sub.add_parser("livestream", help="RTMP livestream (--url ... or --lab)")
    s.add_argument("--url", help="rtmp://... destination")
    s.add_argument("--ssid", help="network for the camera to join first")
    s.add_argument("--open", action="store_true")
    s.add_argument("--lab", action="store_true", help="test mode: this PC runs a hotspot and receives the stream")
    s.add_argument("--seconds", type=float, default=20)
    s.add_argument("--res", type=int, choices=[480, 720, 1080], default=720)
    s.add_argument("--encode", action="store_true", help="also record to the SD card")
    s.add_argument("--out", help="where --lab saves the received stream")
    s.add_argument(
        "--lens",
        choices=["wide", "superview", "linear"],
        help="lens for the stream (default: keep the camera's; the camera stores it as setting 43)",
    )
    s = sub.add_parser("hotspot", help="lab hotspot on this PC (Linux): status / up / down")
    s.add_argument("action", choices=["status", "up", "down"])
    s.add_argument("--seconds", type=float, default=600)
    s = sub.add_parser("sim", help="simulated camera: show / reset / power-button / pairing-mode")
    s.add_argument("action", choices=["show", "reset", "power-button", "pairing-mode", "advance-to-schedule"])
    return p


COMMANDS = {
    "pair": cmd_pair,
    "info": cmd_info,
    "status": cmd_status,
    "setting": cmd_setting,
    "clock": cmd_clock,
    "shutter": cmd_shutter,
    "record": cmd_record,
    "hilight": cmd_simple,
    "sleep": cmd_simple,
    "turbo": cmd_simple,
    "control": cmd_simple,
    "preset": cmd_preset,
    "ap": cmd_ap,
    "schedule": cmd_schedule,
    "restore": cmd_restore,
    "media": cmd_media,
    "preview": cmd_preview,
    "wifi": cmd_wifi,
    "livestream": cmd_livestream,
    "snapshot": cmd_snapshot,
}


def sim_command(a, out: Out) -> None:
    from quak.transport.sim import SimCamera

    path = config.sim_state_file()
    if a.action == "reset":
        path.unlink(missing_ok=True)
        out.result({"reset": True}, f"simulated camera reset ({path})")
        return
    cam = SimCamera.load_or_new(path)
    if a.action == "power-button":
        cam.press_power_button()
    elif a.action == "pairing-mode":
        cam.bonded, cam.pairing_mode = False, True
    elif a.action == "advance-to-schedule":  # jump the camera clock to 3 s before the armed time
        import datetime as _dt

        s = R.decode_schedule(cam.global_settings[R.SCHEDULED_CAPTURE])
        now = cam.camera_now()
        fire = now.replace(hour=s["hour"], minute=s["minute"], second=0)
        if fire < now:
            fire += _dt.timedelta(days=1)
        cam.clock_offset += (fire - now).total_seconds() - 3
    cam.save(path)
    out.result({"state": cam.to_json()}, json.dumps(cam.to_json(), indent=1, default=str))


async def run(a, out: Out) -> None:
    if a.command == "scan":
        cameras = await _nearby_cameras(a)
        out.result(
            {"cameras": cameras},
            "\n".join(
                f"{c['address']}  {c['name']}" + ("  (on its pairing screen)" if c["pairing"] else "") for c in cameras
            )
            or "no cameras found. Is the camera on and nearby?",
        )
        return
    if a.command == "pair" and not a.address and not a.sim:
        a.address = await _camera_on_its_pairing_screen(a)
    target = Target(
        address=a.address,
        sim=a.sim,
        hci=a.hci,
        retries=a.retries,
        log=out.debug,
        wait_pairing=getattr(a, "wait_pairing_mode", False),
    )
    if a.verbose:
        target.trace = lambda t: out.debug(f"{'->' if t.direction == 'tx' else '<-'} {R.short_uuid(t.uuid)} {t.text}")
    async with open_camera(target) as cam:
        try:
            await COMMANDS[a.command](cam, a, out)
        except (Rejected, NetworkError) as e:
            if await _in_turbo(cam):
                e.args = (f"{e}. {TURBO_HINT}",)
            raise


async def _in_turbo(cam: Camera) -> bool:
    with contextlib.suppress(QuakError):
        return bool((await cam.statuses([Status.TURBO]))[Status.TURBO])
    return False


def _terminated(*_) -> None:
    raise KeyboardInterrupt  # the same cleanup as Ctrl-C: the camera and this computer's network get put back


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.command is None:
        parser.print_help()
        sys.exit(0)
    out = Out(a.json, a.verbose)
    signal.signal(signal.SIGTERM, _terminated)
    if a.wifi_adapter:
        os.environ["QUAK_WIFI_ADAPTER"] = a.wifi_adapter
    if not a.sim:
        recovered = recover_network()
        if recovered:
            out.log(f"put back what an interrupted Quak left on {', '.join(recovered)}")
    try:
        if a.command == "sim":
            sim_command(a, out)
        elif a.command == "hotspot":
            if a.sim:
                raise ValueError("the lab hotspot is real hardware; with --sim, livestream --lab fakes it")
            asyncio.run(hotspot_command(a, out))
        else:
            if a.command == "schedule" and a.action == "arm" and not a.when:
                parser.error("schedule arm needs a time, e.g. 12:34 or +2m")
            asyncio.run(run(a, out))
    except QuakError as e:
        out.data["error"] = str(e)
        out.finish() if a.json else print(f"error: {e}", file=sys.stderr)
        sys.exit(e.exit_code)
    except (KeyError, ValueError) as e:
        out.data["error"] = str(e)
        out.finish() if a.json else print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except OSError as e:  # a bad path for a ledger or output file
        out.data["error"] = str(e)
        out.finish() if a.json else print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - nobody needs a traceback, and it can hold private data; -v shows it
        if a.verbose:
            raise
        out.data["error"] = f"{type(e).__name__}: {e}"
        out.finish() if a.json else print(f"error: {type(e).__name__}: {e} (-v shows details)", file=sys.stderr)
        sys.exit(1)
    out.finish()

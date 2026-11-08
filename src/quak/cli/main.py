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
import shlex
import shutil
import signal
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from quak import __version__, config, flows
from quak.camera import Camera
from quak.connect import Target, open_camera
from quak.core import proto, tables
from quak.core import registry as R
from quak.core.errors import ArmFailed, LabError, NetworkError, NotFound, QuakError, Rejected, SshError, SshKeyError
from quak.core.registry import Status
from quak.guard import SettingsGuard
from quak.lab.capabilities.audio import DEFAULT_PATH, MAX_SECONDS
from quak.lab.devices import classify
from quak.lab.service import LabService
from quak.lab.toolkit import check_pack_source
from quak.net import stream
from quak.net.hotspot import lab_hotspot
from quak.net.http import PREVIEW_PORT, CameraHttp
from quak.net.wifi import camera_wifi, recover_network
from quak.ssh import keys, profiles
from quak.ssh.manager import SshManager, _password_option

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


# Each `quak lab` action: its usage, and how many arguments each of its subcommands takes. Extras are refused:
# `storage umount a b` must not quietly leave b mounted.
_LAB_ACTIONS = {
    "status": ("quak lab status", {"": (0,)}),
    "host": ("quak lab host on|off|recover", {"on": (0,), "off": (0,), "recover": (0,)}),
    "devices": ("quak lab devices", {"": (0,)}),
    "toolkit": (
        "quak lab toolkit list|install|load|unload|remove|install-pack PATH --signers PATH"
        "|install-pack-url URL --sha256 SHA --signers PATH",
        {
            "list": (0,),
            "install": (0,),
            "load": (0,),
            "unload": (0,),
            "remove": (0,),
            "install-pack": (1,),
            "install-pack-url": (1,),
        },
    ),
    "storage": (
        "quak lab storage list [PATH]|mount DEVICE [MOUNTPOINT]|umount MOUNTPOINT|copy SRC DST|eject MOUNTPOINT",
        {"list": (0, 1), "mount": (1, 2), "umount": (1,), "copy": (2,), "eject": (1,)},
    ),
    "audio": (
        "quak lab audio list|record CARD --seconds N|merge CLIP WAV OUT [--offset S] [--mix]",
        {"list": (0,), "record": (1,), "merge": (3,)},
    ),
    "network": (
        "quak lab network list|configure NAME [--dhcp | --ip ADDRESS [--prefix N] [--gateway IP]] [--dns IP]...",
        {"list": (0,), "configure": (1,)},
    ),
    "system": ("quak lab system [status|logs|processes]", {"": (0,), "status": (0,), "logs": (0,), "processes": (0,)}),
}

# What --yes confirms, for the actions that need it.
_LAB_CONFIRM = {
    ("host", "on"): "this switches the camera's USB-C port to host mode, which cuts off a computer connected to it",
    ("host", "off"): "this unmounts Lab volumes, unloads the toolkit and puts the USB-C port back in device mode",
    ("toolkit", "install"): "this writes the toolkit to the camera's SD card",
    ("toolkit", "install-pack"): "this writes a driver pack from this computer to the camera's SD card",
    ("toolkit", "install-pack-url"): "this downloads a driver pack and writes it to the camera's SD card",
    ("toolkit", "remove"): "this unloads the toolkit and deletes it from the camera's SD card",
    ("storage", "mount"): "this mounts a volume on the camera, read-only",
    ("storage", "umount"): "this unmounts a volume on the camera",
    ("storage", "copy"): "this writes a copy to the camera's SD card or a mounted volume",
    ("storage", "eject"): "this unmounts a volume on the camera, so it can be unplugged",
    ("audio", "record"): f"this records on the camera, replacing any capture already at {DEFAULT_PATH}",
    ("audio", "merge"): "this runs ffmpeg and writes a new file on this computer",
    ("network", "configure"): "this changes the interface's address on the camera, and can cut the link Quak is using",
}


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
    s = sub.add_parser(
        "ssh", help="authenticated SSH to the camera: status / arm / test / exec / open / host / profile / key"
    )
    s.add_argument("ssh_action", choices=["status", "arm", "test", "exec", "open", "host", "profile", "key"])
    s.add_argument(
        "args", nargs="*", help="host/profile/key subcommand and its key id or path, or the remote command after `--`"
    )
    s.add_argument("--host", help="reach the camera at this host/IP for this run (overrides the saved profile)")
    s.add_argument("--transport", choices=["auto", "wifi", "usb", "host"], help="route for this run (overrides it)")
    s.add_argument(
        "--yes",
        action="store_true",
        help="confirm forgetting a host key or a saved profile, or deleting, rotating or revoking an SSH key",
    )
    s.add_argument("--label", default="", help="name for a generated or imported SSH key")
    s.add_argument("--comment", default="", help="comment for a generated SSH key")
    s.add_argument(
        "--passphrase-stdin",
        action="store_true",
        help="read a generated or imported SSH key's passphrase from one line of stdin (default: no passphrase)",
    )
    s.add_argument(
        "--password-stdin",
        action="store_true",
        help="test/exec on a profile with auth=password: read the login password from one line of stdin (never saved)",
    )
    s.add_argument(
        "--blank-password",
        action="store_true",
        help="use an empty password (the camera's dropbear -B blank mode); never saved",
    )
    s.add_argument("--auth", choices=profiles.AUTHS, help="profile set: log in with a key, a password or neither")
    s.add_argument("--user", metavar="USER", help="profile set: the login user")
    s = sub.add_parser(
        "lab", help="Camera Lab over SSH: status / host / devices / toolkit / storage / audio / network / system"
    )
    s.add_argument("lab_action", choices=list(_LAB_ACTIONS))
    s.add_argument(
        "args",
        nargs="*",
        help="; ".join(usage.removeprefix("quak lab ") for usage, _ in _LAB_ACTIONS.values()),
    )
    s.add_argument("--host", help="reach the camera at this host/IP for this run (overrides the saved profile)")
    s.add_argument("--transport", choices=["auto", "wifi", "usb", "host"], help="route for this run (overrides it)")
    s.add_argument(
        "--password-stdin",
        action="store_true",
        help="on a profile with auth=password: read the login password from one line of stdin (never saved)",
    )
    s.add_argument(
        "--yes",
        action="store_true",
        help="confirm host on/off, toolkit install/install-pack/install-pack-url/remove, storage mount/umount/copy/eject, "
        "audio record/merge, network configure",
    )
    s.add_argument(
        "--signers",
        metavar="PATH",
        help="toolkit install-pack/install-pack-url: the allowed-signers file (ssh-keygen -Y verify) holding the keys "
        "you trust",
    )
    s.add_argument(
        "--sha256",
        metavar="SHA",
        help="toolkit install-pack-url: the SHA-256 the downloaded zip must have (64 hex digits)",
    )
    s.add_argument("--seconds", type=int, metavar="N", help=f"how long audio record captures (at most {MAX_SECONDS})")
    s.add_argument(
        "--offset",
        type=float,
        default=0.0,
        metavar="S",
        help="audio merge: start the WAV S seconds later against the clip, or earlier if negative",
    )
    s.add_argument("--mix", action="store_true", help="audio merge: keep the clip's own sound, mixed with the WAV")
    s.add_argument("--dhcp", action="store_true", help="network configure: get an address by DHCP (the default)")
    # Not --address: on every command that already picks the camera.
    s.add_argument("--ip", metavar="ADDRESS", help="network configure: set this IPv4 address instead of using DHCP")
    s.add_argument("--prefix", type=int, metavar="N", help="network configure: prefix length for --ip (default 24)")
    s.add_argument("--gateway", metavar="IP", help="network configure: default route for --ip, inside its subnet")
    s.add_argument(
        "--dns",
        action="append",
        metavar="IP",
        help="network configure: replace the camera's DNS servers with this one (repeat for more)",
    )
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


# ---------------------------------------------------------------- SSH (authenticated, over the system ssh)


def _write_bytes(stream, data: bytes) -> None:
    """Write remote output to a text stream, raw through its binary buffer when it has one."""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
    else:
        stream.write(data.decode("utf-8", errors="replace"))
    stream.flush()


def _ssh_address(a) -> str:
    address = a.address or profiles.default_address()
    if not address:
        raise SshError("no camera: pass --address, or pair a camera so it becomes the default")
    return address


def _ssh_profile(manager: SshManager, address: str, a):
    """The saved profile with this run's ``--host``/``--transport`` applied. The overrides are not saved."""
    profile = manager.profile(address)
    changes = {}
    if getattr(a, "host", None):
        changes["host"] = a.host
    if getattr(a, "transport", None):
        changes["transport"] = a.transport
    return replace(profile, **changes) if changes else profile


async def _ssh_host(a, out: Out, manager: SshManager, address: str, profile) -> int:
    sub = a.args[0] if a.args else ""
    if sub == "show":
        status = await manager.status(address, profile=profile)
        hk = status.host_key
        out.result(
            {"host_key": hk.fingerprint if hk else None, "key_type": hk.key_type if hk else None,
             "host_key_state": status.host_key_state},
            f"{hk.key_type} {hk.fingerprint} ({status.host_key_state})"
            if hk
            else f"no host key pinned ({status.host_key_state})",
        )
    elif sub == "trust":
        hk = await manager.trust_host(address, profile=profile)
        out.result({"trusted": True, "host_key": hk.fingerprint, "key_type": hk.key_type},
                   f"trusted {hk.key_type} {hk.fingerprint} for {address}")
    elif sub == "forget":
        if not a.yes:
            raise SshError("forgetting the pinned host key can't be undone; add --yes")
        forgotten = await manager.forget_host(address, profile=profile)
        out.result({"forgotten": forgotten}, "host key forgotten" if forgotten else "no host key was pinned")
    else:
        raise ValueError("usage: quak ssh host show|trust|forget")
    return 0


def _ssh_user(user: str) -> str:
    """A login name that's safe to save: ssh gets it as USER@HOST, where a leading dash would read as an option."""
    if not user or user.startswith("-") or any(char.isspace() for char in user):
        raise ValueError(f"{user!r} can't be an SSH login name")
    return user


def _ssh_profile_cmd(a, out: Out, manager: SshManager) -> int:
    usage = "usage: quak ssh profile show|forget|set --auth key|password|none [--user USER]"
    address = _ssh_address(a)
    sub = a.args[0] if a.args else ""
    if sub == "show":
        profile = _ssh_profile(manager, address, a)
        fields = profiles.to_dict(profile)
        out.result({"profile": fields}, "\n".join(f"{k}: {v}" for k, v in fields.items()))
    elif sub == "set":
        if a.auth is None or len(a.args) != 1:
            raise ValueError(usage)
        changes = {"auth": a.auth}
        if a.user is not None:
            changes["user"] = _ssh_user(a.user)
        # From the saved profile, not _ssh_profile's: --host and --transport are for one run and aren't saved.
        profile = replace(manager.profile(address), **changes)
        profiles.save(profile)
        out.result(
            {"profile": profiles.to_dict(profile)},
            f"profile for {address} saved: auth {profile.auth}, user {profile.user}",
        )
    elif sub == "forget":
        if not a.yes:
            raise SshError("forgetting the saved profile can't be undone; add --yes")
        removed = profiles.delete(address)
        out.result({"forgotten": removed},
                   f"profile for {address} forgotten" if removed else f"no saved profile for {address}")
    else:
        raise ValueError(usage)
    return 0


# How many ids/paths each `quak ssh key` subcommand takes. Extras are refused: `delete a b` must not quietly skip b.
_SSH_KEY_ARGS = {
    "list": (0,),
    "generate": (0,),
    "import": (1,),
    "show": (1,),
    "delete": (1,),
    "install": (0, 1),
    "rotate": (2,),
    "revoke": (1,),
}


def _ssh_key_row(key) -> dict:
    return {"id": key.id, "label": key.label, "fingerprint": key.fingerprint, "encrypted": key.encrypted}


def _ssh_key_line(key) -> str:
    return f"{key.id}  {key.label}  {key.fingerprint}  {'encrypted' if key.encrypted else 'no passphrase'}"


def _ssh_passphrase(a) -> str | None:
    """One line of stdin with --passphrase-stdin, otherwise no passphrase."""
    if not a.passphrase_stdin:
        return None
    passphrase = sys.stdin.readline().rstrip("\r\n")
    if not passphrase:  # a pipeline that failed upstream must not leave a private key unprotected by surprise
        raise SshKeyError("--passphrase-stdin got an empty line: send the passphrase, or leave the option off for none")
    return passphrase


def _ssh_password(a, profile) -> str | None:
    """The login password for an auth=password profile, or None. Never shown or saved."""
    if profile.auth != "password":
        return None
    if getattr(a, "blank_password", False):
        if getattr(a, "password_stdin", False):
            raise SshError("use --password-stdin or --blank-password, not both")
        return ""
    if not a.password_stdin:
        return None
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:  # a pipeline that failed upstream: don't spend a login attempt on it
        raise SshError("--password-stdin got an empty line: send the password, or pass --blank-password")
    return password


def _ssh_confirm(a, what: str) -> None:
    """--yes goes ahead. Without it, ask at a terminal; with nobody there to ask (or under --json), refuse."""
    if a.yes:
        return
    if a.json or not sys.stdin.isatty():
        raise SshError(f"{what}; add --yes")
    print(f"{what}. Go ahead? [y/N]", end=" ", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        raise SshError("cancelled: the camera was not changed")


async def _ssh_key_on_camera(a, out: Out, manager: SshManager, sub: str, ids: list[str]) -> None:
    address = _ssh_address(a)
    if sub == "rotate":
        _ssh_confirm(a, f"this replaces {ids[0]} with {ids[1]} on the camera {address}")
    elif sub == "revoke":
        _ssh_confirm(a, f"this removes {ids[0]} from the camera {address}")
    profile = _ssh_profile(manager, address, a)
    if sub == "install":
        result = await manager.install_key(address, ids[0] if ids else None, profile=profile)
    elif sub == "rotate":
        result = await manager.rotate_key(address, ids[0], ids[1], profile=profile)
    else:
        result = await manager.revoke_key(address, ids[0], profile=profile)
    out.result(asdict(result), result.message)


async def _ssh_key(a, out: Out, manager: SshManager) -> int:
    sub, *rest = a.args or [""]
    if len(rest) not in _SSH_KEY_ARGS.get(sub, ()):
        raise ValueError(
            "usage: quak ssh key list|generate|import PATH|show ID|delete ID|install [ID]|rotate OLD NEW|revoke ID"
        )
    if sub == "list":
        stored = keys.list_keys()
        out.result(
            {"keys": [_ssh_key_row(key) for key in stored]},
            "\n".join(_ssh_key_line(key) for key in stored)
            or "no SSH keys yet. Run `quak ssh key generate` or `quak ssh key import`",
        )
    elif sub == "generate":
        key = keys.generate(a.label, comment=a.comment, passphrase=_ssh_passphrase(a))
        out.result(_ssh_key_row(key), f"generated {_ssh_key_line(key)}")
    elif sub == "import":
        key = keys.import_key(Path(rest[0]), a.label, passphrase=_ssh_passphrase(a))
        out.result(_ssh_key_row(key), f"imported {_ssh_key_line(key)}")
    elif sub == "show":
        key = keys.get_key(rest[0])
        if key is None:
            raise SshKeyError(
                f"SSH key {rest[0]} is not in Quak's key store. Run `quak ssh key list` to see what is there"
            )
        out.result({**_ssh_key_row(key), "public_key": key.public_key}, f"{key.public_key}\n{key.fingerprint}")
    elif sub == "delete":
        if not a.yes:
            raise SshError(f"deleting the private key of {rest[0]} can't be undone; add --yes")
        removed = keys.delete_key(rest[0])
        out.result(
            {"deleted": removed},
            f"SSH key {rest[0]} deleted" if removed else f"no SSH key {rest[0]} in Quak's key store",
        )
    else:
        await _ssh_key_on_camera(a, out, manager, sub, rest)
    return 0


async def ssh_command(a, out: Out) -> int:
    """`quak ssh ...`: authenticated SSH to the camera, routed and host-key-checked by SshManager."""
    manager = SshManager()
    action = a.ssh_action
    if action == "profile":
        return _ssh_profile_cmd(a, out, manager)
    if action == "key":  # the key store is local: only install/rotate/revoke need a camera
        return await _ssh_key(a, out, manager)
    address = _ssh_address(a)
    profile = _ssh_profile(manager, address, a)
    if action == "status":
        status = await manager.status(address, profile=profile)
        ep = status.endpoint
        hk = status.host_key
        out.result(
            {
                "address": address,
                "transport": profile.transport,
                "endpoint": f"{ep.host}:{ep.ssh_port}" if ep else None,
                "reachable": status.reachable,
                "ssh_up": status.ssh_up,
                "banner": status.banner,
                "host_key": hk.fingerprint if hk else None,
                "host_key_state": status.host_key_state,
                "last_error": status.last_error,
            },
            "\n".join(
                filter(
                    None,
                    [
                        f"endpoint {ep.host}:{ep.ssh_port} ({ep.transport})" if ep else "endpoint: unresolved",
                        f"SSH {'up' if status.ssh_up else 'down'}" + (f" · {status.banner}" if status.banner else ""),
                        f"host key {status.host_key_state}" + (f" · {hk.fingerprint}" if hk else ""),
                        f"note: {status.last_error}" if status.last_error else "",
                    ],
                )
            ),
        )
        return 0
    if action == "arm":
        result = await manager.arm(address, profile=profile)
        out.result(
            {
                "http_status": result.http_status,
                "accepted": result.accepted,
                "ssh_up": result.ssh_up,
                "banner": result.banner,
                "message": result.message,
            },
            f"HTTP {result.http_status} · {'accepted' if result.accepted else 'refused'} · "
            f"SSH {'up' if result.ssh_up else 'down'}" + (f" · {result.banner}" if result.banner else "")
            + (f"\n{result.message}" if result.message else ""),
        )
        if not result.accepted:
            raise ArmFailed(result.message or "the camera did not accept the arm request")
        return 0
    if action == "test":
        result = await manager.test(address, profile=profile, password=_ssh_password(a, profile))
        out.result(
            {"ok": result.ok, "uid": result.uid, "uname": result.uname, "banner": result.banner, "error": result.error},
            "\n".join(filter(None, [result.banner, result.uid, result.uname])) if result.ok else "",
        )
        if not result.ok:
            raise SshError(result.error or "SSH login failed")
        return 0
    if action == "exec":
        command = shlex.join(a.args)
        if not command.strip():
            raise SshError("no remote command given: pass it after `--`, e.g. quak ssh exec -- uname -a")
        result = await manager.exec(address, command, profile=profile, password=_ssh_password(a, profile))
        code = result.exit_code if result.exit_code is not None else 0
        out.result(
            {
                "command": command,
                "exit_code": code,
                "stdout": result.text,
                "stderr": result.stderr.decode("utf-8", errors="replace"),
            }
        )
        if not out.as_json:
            _write_bytes(sys.stdout, result.stdout)
            if result.stderr:
                _write_bytes(sys.stderr, result.stderr)
        return code
    if action == "open":
        code = await manager.open_terminal(address, profile=profile)
        out.result({"address": address, "exit_code": code})
        return code
    if action == "host":
        return await _ssh_host(a, out, manager, address, profile)
    raise ValueError(f"unknown ssh action {action!r}")


# ---------------------------------------------------------------- Camera Lab (over authenticated SSH)


def _lab_size(size: int) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def _lab_port_text(state) -> str:
    if state.error:
        return f"host mode: {state.error}"
    unfinished = state.marker == "switching" or (state.marker and state.partial)
    return " · ".join(
        filter(
            None,
            [
                f"host mode: {'on' if state.enabled else 'between modes' if state.partial else 'off'}",
                f"root hubs {', '.join(state.devices)}" if state.devices else "",
                f"gadget on {state.udc}" if state.udc else "",
                f"paused {', '.join(state.paused)}" if state.paused else "",
                "a switch did not finish: run `quak lab host recover`" if unfinished else "",
            ],
        )
    )


def _lab_pack_text(pack) -> str:
    """What the card says. It leaves out whether the modules are loaded: reading the card doesn't ask the kernel."""
    if pack.error:
        return f"toolkit: {pack.error}"
    return f"toolkit: installed at {pack.path}" if pack.installed else "toolkit: not installed"


def _lab_device_row(device) -> dict:
    return {**asdict(device), "kind": classify(device)}


def _lab_device_line(device) -> str:
    drivers = sorted({interface.driver for interface in device.interfaces if interface.driver})
    return " · ".join(
        filter(
            None,
            [
                f"{device.id}  {device.vid or '----'}:{device.pid or '----'}  {classify(device)}",
                " ".join(filter(None, [device.manufacturer, device.product])),
                f"{device.speed} Mb/s" if device.speed else "",
                ", ".join(drivers) or device.driver,
            ],
        )
    )


def _lab_volume_line(volume) -> str:
    mode = "read-only" if volume.read_only else "read-write"
    return "  ".join(
        filter(
            None,
            [
                volume.device,
                volume.fs or "unknown filesystem",
                f'"{volume.label}"' if volume.label else "",
                _lab_size(volume.size),
                f"mounted at {volume.mountpoint} ({mode})" if volume.mounted else "not mounted",
            ],
        )
    )


def _lab_system_text(status: dict) -> str:
    mem, df = status["mem"], status["df"]
    memory = ", ".join(f"{name} {_lab_size(mem[name])}" for name in ("total", "available", "free") if name in mem)
    lines = [
        f"up {dt.timedelta(seconds=int(status['uptime']))}" if status["uptime"] is not None else "",
        "load " + " ".join(f"{value:g}" for value in status["load"]) if status["load"] else "",
        f"memory: {memory}" if memory else "",
        f"SD card: {_lab_size(df['free'])} free of {_lab_size(df['total'])} ({df['percent']}% used)" if df else "",
        *(f"{zone['type'] or zone['zone']}: {zone['celsius']:.1f} °C" for zone in status["temp"]),
    ]
    return "\n".join(filter(None, lines)) or "the camera reported nothing"


async def _lab_status(lab: LabService, out: Out) -> None:
    status = await lab.status()
    data = asdict(status) | {"devices": [_lab_device_row(device) for device in status.devices]}
    if data["toolkit"]:
        data["toolkit"]["loaded"] = None  # read from the card, which can't say: null rather than a false "not loaded"
    lines = [
        _lab_port_text(status.host_mode) if status.host_mode else "",
        _lab_pack_text(status.toolkit) if status.toolkit else "",
        f"battery: {status.battery}" if status.battery else "",
        "USB devices:" if status.devices else "" if status.error else "no USB devices",
        *(f"  {_lab_device_line(device)}" for device in status.devices),
        f"note: {status.error}" if status.error else "",
    ]
    out.result(data, "\n".join(filter(None, lines)))


async def _lab_host(lab: LabService, out: Out, sub: str) -> None:
    switch = {"on": lab.host_on, "off": lab.host_off, "recover": lab.host_recover}[sub]
    result = await switch()
    out.result(asdict(result), f"{result.message}\n{_lab_port_text(result.state)}")


async def _lab_toolkit(
    lab: LabService, out: Out, sub: str, rest: list[str], signers: str | None, sha256: str | None
) -> None:
    action, done = {
        "list": (lab.toolkit_status, ""),
        "install": (lab.toolkit_install, "toolkit installed; no modules were loaded"),
        "install-pack": (
            lambda: lab.toolkit_install_pack(rest[0], signers),
            "pack signature verified and pack installed; no modules were loaded",
        ),
        "install-pack-url": (
            lambda: lab.toolkit_install_pack_url(rest[0], sha256, signers),
            "pack downloaded, its signature verified and pack installed; no modules were loaded",
        ),
        "load": (lab.toolkit_load, "toolkit modules loaded"),
        "unload": (lab.toolkit_unload, "toolkit modules unloaded"),
        "remove": (lab.toolkit_remove, "toolkit removed"),
    }[sub]
    pack = await action()
    data = asdict(pack)
    # These go by the card, which can't say: null rather than a false "not loaded".
    if sub in ("list", "install", "install-pack", "install-pack-url"):
        data["loaded"] = None
    out.result({"toolkit": data}, "\n".join(filter(None, [done, _lab_pack_text(pack)])))


async def _lab_storage(lab: LabService, out: Out, sub: str, rest: list[str]) -> None:
    if sub == "list" and rest:
        entries = await lab.storage_list_dir(rest[0])
        out.result(
            {"path": rest[0], "entries": [entry._asdict() for entry in entries]},
            "\n".join(
                f"{entry.type:<5} {_lab_size(entry.size) if entry.type == 'file' else '':>9}  {entry.name}"
                for entry in entries
            )
            or f"{rest[0]} is empty",
        )
    elif sub == "list":
        volumes = await lab.storage_list()
        out.result(
            {"volumes": [asdict(volume) for volume in volumes]},
            "\n".join(_lab_volume_line(volume) for volume in volumes) or "no volumes found",
        )
    elif sub == "mount":
        volume = await lab.storage_mount(*rest)
        out.result(  # no label or size: only `storage list` knows those
            {"device": volume.device, "fs": volume.fs, "mountpoint": volume.mountpoint, "read_only": volume.read_only},
            f"mounted {volume.device} at {volume.mountpoint} ({'read-only' if volume.read_only else 'read-write'})",
        )
    elif sub == "umount":
        await lab.storage_unmount(rest[0])
        out.result({"unmounted": rest[0]}, f"unmounted {rest[0]}")
    elif sub == "copy":
        out.log(f"copying {rest[0]} to {rest[1]}: a big file takes a while, with nothing to show until it's done")
        result = await lab.storage_copy(*rest)
        checked = f"sha256 {result.sha256} on both ends" if result.sha256 else "a folder's contents aren't verified"
        out.result(asdict(result), f"copied to {result.target} ({checked})")
    else:
        await lab.storage_eject(rest[0])
        out.result({"ejected": rest[0]}, f"ejected {rest[0]}")


def _lab_audio_line(device) -> str:
    if not device.capture_format:
        return f"card {device.card}  {device.name}  can't capture"
    rates = ", ".join(str(rate) for rate in device.rates)
    return "  ".join(
        filter(
            None,
            [
                f"card {device.card}",
                device.name,
                device.capture_format,
                f"{device.channels} ch" if device.channels else "",
                f"{rates} Hz" if rates else "",
            ],
        )
    )


def _lab_record_args_ok(a, rest: list[str]) -> bool:
    return rest[0].isdecimal() and a.seconds is not None and a.seconds >= 1


async def _lab_audio(lab: LabService, out: Out, a, sub: str, rest: list[str]) -> None:
    if sub == "list":
        cards = await lab.audio_list()
        out.result(
            {"cards": [asdict(card) for card in cards]},
            "\n".join(_lab_audio_line(card) for card in cards)
            or "no sound cards: they need host mode on and the toolkit loaded (`quak lab toolkit load`)",
        )
        return
    seconds = min(a.seconds, MAX_SECONDS)
    limit = " (the most one capture can hold)" if a.seconds > MAX_SECONDS else ""
    out.log(f"recording {seconds} s{limit} from sound card {rest[0]}")
    result = await lab.audio_record(int(rest[0]), a.seconds)
    out.result(
        {**result._asdict(), "seconds": seconds},
        f"recorded {result.path} on the camera ({_lab_size(result.size)}). It is in RAM: a power cycle loses it",
    )


async def _lab_audio_merge(lab: LabService, out: Out, a, rest: list[str]) -> None:
    out.log(f"merging {rest[1]} with {rest[0]}")
    merged = await lab.audio_merge(*rest, offset_seconds=a.offset, mix=a.mix)
    sound = "the WAV mixed with the clip's sound" if a.mix else "the WAV in place of the clip's sound"
    shift = f", offset {a.offset:+g} s" if a.offset else ""
    out.result({"merged": str(merged), "offset_seconds": a.offset, "mix": a.mix}, f"wrote {merged} ({sound}{shift})")


def _lab_network_args_ok(a, sub: str) -> bool:
    """``--dhcp`` excludes ``--ip``, and what only means something with ``--ip`` isn't dropped without it."""
    by_hand = a.ip is not None  # not truthiness: an empty --ip "$UNSET" must not turn into DHCP
    needs_ip = a.prefix is not None or a.gateway is not None
    if sub == "list":
        return not (a.dhcp or by_hand or needs_ip or a.dns is not None)
    return not (a.dhcp and by_hand) and (by_hand or not needs_ip)


def _lab_interface_line(interface) -> str:
    return "  ".join(
        filter(
            None,
            [
                interface.name,
                interface.kind,
                "up" if interface.up else "down",
                interface.mac,
                ", ".join(interface.addresses) or "no address",
                interface.driver,
            ],
        )
    )


async def _lab_network(lab: LabService, out: Out, a, sub: str, rest: list[str]) -> None:
    if sub == "list":
        found = await lab.network_list()
        out.result(
            {"interfaces": [asdict(interface) for interface in found]},
            "\n".join(_lab_interface_line(interface) for interface in found) or "no network interfaces found",
        )
        return
    given = {"address": a.ip, "prefix": a.prefix, "gateway": a.gateway, "dns": a.dns}
    settings = {name: value for name, value in given.items() if value is not None}
    try:
        result = await lab.network_configure(rest[0], dhcp=a.ip is None, **settings)
    except NetworkError as e:
        e.args = (
            f"{e}. If that link runs over {rest[0]}, the change may have gone through anyway: "
            "reconnect and check with `quak lab network list`",
        )
        raise
    lines = [
        f"{result['interface']} set up {'by DHCP' if result['mode'] == 'dhcp' else 'by hand'}: "
        f"{', '.join(result['addresses'])}. The address lasts until the camera restarts",
        f"gateway {result['gateway']}" if result["gateway"] else "",
        f"DNS {', '.join(result['dns'])}" if result["dns"] else "",
    ]
    out.result(result, "\n".join(filter(None, lines)))


async def _lab_system(lab: LabService, out: Out, sub: str) -> None:
    if sub == "logs":
        logs = await lab.system_logs()
        out.result({"logs": logs}, logs)
    elif sub == "processes":
        processes = await lab.system_processes()
        out.result({"processes": processes}, processes)
    else:
        status = await lab.system_status()
        out.result(status, _lab_system_text(status))


async def lab_command(a, out: Out) -> None:
    """`quak lab ...`: the Camera Lab over authenticated SSH. ``LabService`` holds a route for each action."""
    if a.sim:  # nothing here is simulated, so --sim must not end up changing a real camera
        raise ValueError("the Camera Lab is real hardware: it has no simulated camera")
    action = a.lab_action
    usage, subcommands = _LAB_ACTIONS[action]
    sub, *rest = a.args or [""]
    if len(rest) not in subcommands.get(sub, ()):
        raise ValueError(f"usage: {usage}")
    # --signers is for the two pack installs, which can't do without it: no signers file means no pack is trusted.
    # --sha256 is for the one that downloads, which can't do without it either.
    if bool(a.signers) != (action == "toolkit" and sub in ("install-pack", "install-pack-url")):
        raise ValueError(f"usage: {usage}")
    if bool(a.sha256) != ((action, sub) == ("toolkit", "install-pack-url")):
        raise ValueError(f"usage: {usage}")
    # A URL that isn't https:// or file://, or a hash that isn't one, is refused here: before --yes, and before
    # any route to the camera is opened for a download that was never going to be allowed.
    if (action, sub) == ("toolkit", "install-pack-url"):
        check_pack_source(rest[0], a.sha256)
    if (action, sub) == ("audio", "record") and not _lab_record_args_ok(a, rest):
        raise ValueError(f"usage: {usage}")
    if action == "network" and not _lab_network_args_ok(a, sub):
        raise ValueError(f"usage: {usage}")
    # A camera's address has no dots, so this is an IP in the wrong option. Taken for a camera, it would run DHCP.
    if (action, sub) == ("network", "configure") and a.address and "." in a.address:
        raise ValueError(
            f"--address picks the camera, and {a.address} isn't one. Give the interface's address with --ip"
        )
    if (action, sub) in _LAB_CONFIRM and not a.yes:
        raise LabError(f"{_LAB_CONFIRM[action, sub]}; add --yes")
    manager = SshManager()
    if (action, sub) == ("audio", "merge"):  # happens on this computer, so no camera has to be known
        await _lab_audio_merge(LabService(manager, a.address or ""), out, a, rest)
        return
    address = _ssh_address(a)
    profile = _ssh_profile(manager, address, a)
    lab = LabService(manager, address, profile=profile, **_password_option(_ssh_password(a, profile)))
    if action == "status":
        await _lab_status(lab, out)
    elif action == "host":
        await _lab_host(lab, out, sub)
    elif action == "devices":
        found = await lab.devices()
        out.result(
            {"devices": [_lab_device_row(device) for device in found]},
            "\n".join(_lab_device_line(device) for device in found)
            or "no USB devices: the camera only has them in host mode (`quak lab host on --yes`)",
        )
    elif action == "toolkit":
        await _lab_toolkit(lab, out, sub, rest, a.signers, a.sha256)
    elif action == "storage":
        await _lab_storage(lab, out, sub, rest)
    elif action == "audio":
        await _lab_audio(lab, out, a, sub, rest)
    elif action == "network":
        await _lab_network(lab, out, a, sub, rest)
    else:
        await _lab_system(lab, out, sub)


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
    if not a.sim and a.command not in ("ssh", "lab"):  # these hold no BLE/Wi-Fi of their own to clean up after
        recovered = recover_network()
        if recovered:
            out.log(f"put back what an interrupted Quak left on {', '.join(recovered)}")
    ssh_exit = 0
    try:
        if a.command == "ssh":
            ssh_exit = asyncio.run(ssh_command(a, out))
        elif a.command == "lab":
            asyncio.run(lab_command(a, out))
        elif a.command == "sim":
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
    if ssh_exit:  # quak ssh exec/open propagate the remote command's (or ssh's) exit code
        sys.exit(ssh_exit)

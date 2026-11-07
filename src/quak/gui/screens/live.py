"""Livestream setup and progress."""

from __future__ import annotations

import asyncio
import sys

import flet as ft

from quak import config
from quak.gui import kit
from quak.gui.screens.common import _scroll


def live(app) -> ft.Control:
    t, st, lv = app.t, app.svc.state, app.live
    if not st.connected:
        return _scroll(
            [kit.title("Live", t), kit.banner("Not connected.", t, "warn", on_click=lambda e: app.go("cameras"))]
        )
    f = app.form.setdefault(
        "live",
        {
            "dest": "youtube",
            "key": "",
            "url": "",
            "ssid": None,
            "password": "",
            "res": 720,
            "encode": False,
            "lab": False,
        },
    )
    if lv["task"] and not lv["task"].done():
        state = st.live.get("state", "CONFIG")
        steps = ft.Row(
            [
                ft.Column(
                    [
                        ft.Container(
                            height=4,
                            border_radius=2,
                            bgcolor=(t.rec if s == "STREAMING" else t.ok) if _reached(state, s) else t.line,
                        ),
                        kit.text(s.capitalize(), t, 13, t.text if s == state else t.text2),
                    ],
                    spacing=6,
                    expand=True,
                )
                for s in ("CONFIG", "READY", "STREAMING")
            ],
            spacing=8,
        )
        return _scroll(
            [
                kit.title("Live", t),
                ft.Container(
                    ft.Stack(
                        [
                            ft.Container(bgcolor="#22303F", expand=True),
                            ft.Container(
                                kit.pill(
                                    f"LIVE · {state.lower()}", t, dot="#FFFFFF", bg=t.rec, fg="#FFFFFF", bold=True
                                ),
                                left=10,
                                top=10,
                            ),
                        ]
                    ),
                    aspect_ratio=16 / 9,
                    border_radius=16,
                    clip_behavior=ft.ClipBehavior.HARD_EDGE,
                ),
                steps,
                *[kit.text(line, t, 13, t.text2) for line in lv["log"][-6:]],
            ],
            spacing=14,
            footer=[kit.destructive("End stream", lambda e: lv["task"].cancel(), t, height=54, text_size=17)],
        )

    def field(value, on_change, password=True, hint=None):
        return ft.TextField(
            value=value,
            password=password,
            can_reveal_password=password,
            hint_text=hint,
            on_change=on_change,
            bgcolor=t.surface,
            border_color=t.line,
            focused_border_color=t.accent,
            border_radius=12,
            color=t.text,
            text_size=15,
            height=48,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        )

    dest = kit.segmented(
        [("youtube", "YouTube"), ("twitch", "Twitch"), ("custom", "Custom RTMP")],
        f["dest"],
        lambda v: (f.update(dest=v), app.render()),
        t,
        key="dest",
    )
    custom = f["dest"] == "custom"
    target = [
        kit.text("RTMP URL" if custom else "Stream key", t, 13, t.text2),
        field(
            f["url"] if custom else f["key"],
            lambda e: f.update({"url" if custom else "key": e.control.value}),
            password=not custom,
            hint="rtmp://…" if custom else None,
        ),
    ]

    def network_row(n, last):
        on = f["ssid"] == n["ssid"]
        mhz = n.get("signal_frequency_mhz", 0) or 0
        band = "5 GHz" if mhz >= 4900 else "2.4 GHz"
        sub = "Open network" if n.get("scan_entry_flags", 0) & 1 else f"Found by the camera · {band}"
        bars = n.get("signal_strength_bars", 0) or 0
        radio = ft.Container(
            ft.Container(width=10, height=10, border_radius=5, bgcolor=t.on_accent) if on else None,
            width=22,
            height=22,
            border_radius=11,
            bgcolor=t.accent if on else None,
            border=None if on else ft.Border.all(2, t.text2),
            alignment=ft.Alignment.CENTER,
        )
        return ft.Container(
            ft.Row(
                [
                    radio,
                    ft.Column([kit.text(n["ssid"], t, 15), kit.text(sub, t, 12, t.text3)], spacing=2, expand=True),
                    kit.text("Strong" if bars >= 3 else "Fair" if bars == 2 else "Weak", t, 13, t.text2),
                ],
                spacing=12,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            border=None if last else ft.Border.only(bottom=ft.BorderSide(1, t.surface2)),
            on_click=lambda e, s=n["ssid"]: (f.update(ssid=s), app.render()),
            ink=True,
        )

    phone = app.page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS)
    # the lab hotspot takes this PC's Wi-Fi adapter and leaves a network saved on the camera: for people working on Quak
    lab = not phone and (app.sim or (config.developer_mode() and sys.platform.startswith("linux")))
    body = [kit.page_title("Live", t), kit.section("Stream to", t), dest, *target, kit.label("Camera joins", t)]
    if lab:
        body.append(
            kit.card(
                kit.check_row(
                    "Test mode",
                    f["lab"],
                    lambda e: (f.update(lab=e.control.value), app.render()),
                    t,
                    sub="This PC is the hotspot and receives the stream",
                    key="lab",
                ),
                t,
                padding=0,
            )
        )
    if not f["lab"]:
        nets = [network_row(n, i == len(lv["networks"]) - 1) for i, n in enumerate(lv["networks"])]
        body.append(
            kit.card(
                ft.Column(
                    nets or [kit.text("Scan to see the networks the camera can reach.", t, 14, t.text2)], spacing=0
                ),
                t,
                padding=0 if nets else 14,
            )
        )
        body.append(
            kit.secondary(
                "Scanning…" if lv["scanning"] else ("Scan again" if nets else "Scan for networks"),
                lambda e: app.run(_scan_networks, app),
                t,
                disabled=lv["scanning"],
            )
        )
        if f["ssid"]:
            body += [
                kit.text(f"Password for {f['ssid']}", t, 13, t.text2),
                field(f["password"], lambda e: f.update(password=e.control.value)),
                kit.text("Sent to the camera only. Quak doesn't save it.", t, 12, t.text3),
            ]
    body += [
        kit.section("Quality", t),
        kit.segmented(
            [("480", "480p"), ("720", "720p"), ("1080", "1080p")],
            str(f["res"]),
            lambda v: (f.update(res=int(v)), app.render()),
            t,
            key="res",
        ),
        kit.card(
            kit.check_row(
                "Also save to the SD card", f["encode"], lambda e: f.update(encode=e.control.value), t, key="encode"
            ),
            t,
            padding=0,
        ),
    ]
    return _scroll(
        body,
        spacing=12,
        footer=[kit.live_button("Go live", lambda e: _go_live(app), t, icon=ft.Icons.SENSORS, height=54, text_size=17)],
    )


def _reached(state: str, step: str) -> bool:
    order = ["IDLE", "CONFIG", "READY", "STREAMING"]
    return state in order and order.index(state) >= order.index(step)


async def _scan_networks(app) -> None:
    app.live["scanning"] = True
    app.render()
    try:
        app.live["networks"] = await app.svc.scan_networks()
    finally:
        app.live["scanning"] = False


def _go_live(app) -> None:
    f, lv = app.form["live"], app.live
    lv["log"] = []

    def log(msg):
        lv["log"].append(msg)
        app.render_soon()

    async def stream():
        from quak.net import stream as st_mod

        # the camera can't keep its own Wi-Fi up while it joins another network, and on Linux the lab
        # hotspot needs the adapter the camera-Wi-Fi link uses: let go of preview and media first
        if app.preview.get("on"):
            await app.stop_preview()
        await app.svc.release_http()
        app.media.update(files=[], thumbs={}, loaded=False)
        if f["lab"]:
            received = config.state_dir() / "gui-livestream.ts"
            receiver = st_mod.RtmpReceiver(received, timeout=3600)
            try:
                if app.sim:
                    receiver.start()
                    from quak.net.hotspot import lab_credentials
                    from quak.transport.sim import SimNetwork

                    sim = app.svc.target.sim_camera
                    ssid, pw = lab_credentials()
                    if not any(n.ssid == ssid for n in sim.networks):
                        sim.networks.append(SimNetwork(ssid, pw))
                    pusher = st_mod.TestPattern()
                    sim.on_stream = lambda url, on: (
                        pusher.start(url.replace("10.42.0.1", "127.0.0.1")) if on else pusher.stop()
                    )
                    try:
                        await app.svc.livestream(
                            receiver.url_for("10.42.0.1"),
                            3600,
                            network=(ssid, pw),
                            resolution=f["res"],
                            encode=f["encode"],
                            on_log=log,
                        )
                    finally:
                        pusher.stop()
                else:
                    from quak.net.hotspot import lab_hotspot

                    async with lab_hotspot(log=log) as hs:
                        receiver.start(hs.gateway)
                        await app.svc.livestream(
                            receiver.url_for(hs.gateway),
                            3600,
                            network=(hs.ssid, hs.password),
                            resolution=f["res"],
                            encode=f["encode"],
                            on_log=log,
                        )
            finally:
                errors = await asyncio.to_thread(receiver.stop)
                if errors:
                    print(f"quak: RTMP receiver: {errors[-500:]}", file=sys.stderr, flush=True)
                if not received.exists():
                    print(f"quak: RTMP receiver saved nothing ({received})", file=sys.stderr, flush=True)
                if received.exists():
                    v = (await asyncio.to_thread(st_mod.probe_file, received, True)).get("video") or {}
                    lv["received"] = (
                        f"received {v.get('width')}x{v.get('height')} {v.get('codec')}, {v.get('frames')} frames"
                    )
            return
        url = {"youtube": "rtmp://a.rtmp.youtube.com/live2/", "twitch": "rtmp://live.twitch.tv/app/"}.get(f["dest"])
        url = (url + f["key"]) if url else f["url"]
        network = (f["ssid"], f["password"] or None) if f["ssid"] else None
        await app.svc.livestream(url, 3600, network=network, resolution=f["res"], encode=f["encode"], on_log=log)

    async def runner():
        try:
            await stream()
        except asyncio.CancelledError:
            app.toast("Stream ended" + (f" · {lv.pop('received')}" if lv.get("received") else ""))
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            app.toast(str(e), error=True)
        finally:
            app.svc.clear_live_status()
            app.render()

    lv["task"] = asyncio.ensure_future(runner())
    app.render()

"""Camera operations on top of a Session, returning Python values."""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from quak.core import codec, proto
from quak.core import registry as R
from quak.core.errors import Rejected, Timeout, Unsupported
from quak.core.registry import Action, Cmd, Feature, Query
from quak.session import Session


def local_tz() -> tuple[int, bool]:
    """(UTC offset in minutes, DST active) for this machine right now."""
    lt = time.localtime()
    return lt.tm_gmtoff // 60, bool(lt.tm_isdst > 0)


class Camera:
    def __init__(self, session: Session) -> None:
        self.s = session
        self._claimed = False
        self._status_push: set[int] = set()

    # ---------------------------------------------------------------- info and state

    async def hardware_info(self) -> codec.HardwareInfo:
        return codec.decode_hardware_info(await self.s.command(Cmd.HARDWARE_INFO))

    async def statuses(self, ids=()) -> dict[int, int | str]:
        body = await self.s.query(Query.STATUSES, ids)
        return {k: R.decode_status_value(k, v) for k, v in codec.parse_tlv(body)}

    async def status(self, status_id: int) -> int | str:
        return (await self.statuses([status_id]))[status_id]

    async def settings(self, ids=()) -> dict[int, int]:
        body = await self.s.query(Query.SETTINGS, ids)
        return {k: codec.to_int(v) for k, v in codec.parse_tlv(body)}

    async def setting(self, setting_id: int) -> int:
        return (await self.settings([setting_id]))[setting_id]

    async def capabilities(self, ids=()) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for k, v in codec.parse_tlv(await self.s.query(Query.CAPABILITIES, ids)):
            out.setdefault(k, [])
            if v:
                out[k].append(codec.to_int(v))
        return out

    async def set_setting(self, setting_id: int, value: int, verify: bool = True) -> None:
        name = R.setting_name(setting_id)
        status = await self.s.write_setting(setting_id, R.encode_setting_value(setting_id, value))
        if status:
            allowed = (await self.capabilities([setting_id])).get(setting_id, [])
            if not allowed:
                hint = " (needs Pro controls)" if setting_id in R.PRO_ONLY else ""
                raise Unsupported(f"{name} can't be changed in the camera's current mode{hint}")
            choices = ", ".join(R.value_label(setting_id, v) for v in allowed)
            raise Rejected(
                f"set {name} = {R.value_label(setting_id, value)}",
                f"{R.TLV_STATUS.get(status, status)}; allowed now: {choices}",
            )
        if verify:
            got = await self.setting(setting_id)
            if got != value:
                raise Rejected(
                    f"set {name}",
                    f"read back {R.value_label(setting_id, got)}, expected {R.value_label(setting_id, value)}",
                )

    # ---------------------------------------------------------------- clock

    async def clock(self) -> tuple[dt.datetime, int | None, bool | None]:
        """Camera local time (naive), plus tz offset and DST when the camera reports them."""
        try:
            body = await self.s.command(Cmd.GET_LOCAL_DATETIME)
        except Rejected:
            body = await self.s.command(Cmd.GET_DATETIME)
        return codec.decode_datetime(body[1 : 1 + body[0]])

    async def sync_clock(self, when: dt.datetime | None = None) -> dt.datetime:
        """Set the camera clock to this machine's local time (with UTC offset and DST)."""
        tz, dst = local_tz()
        if when is None:  # land on the second boundary: the camera keeps whole seconds
            frac = time.time() % 1
            await asyncio.sleep((1 - frac) % 1)
            when = dt.datetime.now().replace(microsecond=0)
        await self.s.command(Cmd.SET_LOCAL_DATETIME, codec.encode_datetime(when, tz, dst))
        return when

    # ---------------------------------------------------------------- capture

    async def shutter(self, on: bool) -> None:
        await self.s.command(Cmd.SHUTTER, b"\x01" if on else b"\x00")

    async def hilight(self) -> None:
        await self.s.command(Cmd.HILIGHT)

    async def sleep(self) -> None:
        """Put the camera to sleep. The camera usually drops the BLE link right after."""
        await self.s.command(Cmd.SLEEP)

    async def load_group(self, group: str | int) -> None:
        gid = R.PRESET_GROUPS[group] if isinstance(group, str) else group
        await self.s.command(Cmd.LOAD_PRESET_GROUP, gid.to_bytes(2, "big"))

    async def load_preset(self, preset_id: int) -> None:
        await self.s.command(Cmd.LOAD_PRESET, preset_id.to_bytes(4, "big"))

    async def presets(self, include_hidden: bool = False) -> list[dict]:
        r = await self.s.protobuf(
            Feature.QUERY,
            Action.PRESET_STATUS,
            "RequestGetPresetStatus",
            {"include_hidden": include_hidden},
            "NotifyPresetStatus",
        )
        return r["preset_group_array"]

    async def camera_control(self, mode: str) -> None:
        r = await self.s.protobuf(
            Feature.COMMAND,
            Action.CAMERA_CONTROL,
            "RequestSetCameraControlStatus",
            {"camera_control_status": proto.CAMERA_CONTROL[mode]},
            "ResponseGeneric",
        )
        self._check(r, f"camera control {mode}")
        if mode == "external" and not self._claimed:
            self._claimed = True
            self.s.add_cleanup(self._release_control)
        elif mode == "idle":
            self._claimed = False

    async def _release_control(self) -> None:
        if self._claimed:
            await self.camera_control("idle")

    async def turbo(self, on: bool) -> None:
        r = await self.s.protobuf(
            Feature.COMMAND, Action.TURBO, "RequestSetTurboActive", {"active": on}, "ResponseGeneric"
        )
        self._check(r, "turbo transfer")

    @staticmethod
    def _check(r: dict, what: str) -> None:
        result = r.get("result")
        if result == proto.RESULT_NOT_SUPPORTED:
            raise Unsupported(f"{what}: not supported by this camera")
        if result != proto.RESULT_SUCCESS:
            raise Rejected(what, proto.RESULT.get(result, result))

    # ---------------------------------------------------------------- pushes

    async def watch_statuses(self, ids) -> tuple[dict[int, int | str], asyncio.Queue]:
        """Register for status pushes. Returns current values and a queue of {id: value} updates."""
        q = self.s.subscribe((R.CQ_QUERY_RESP, Query.STATUS_PUSH))
        body = await self.s.query(Query.REGISTER_STATUSES, ids)
        if not self._status_push:
            self.s.add_cleanup(self._unregister_statuses)
        self._status_push.update(ids)
        updates: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            while True:
                _, msg = await q.get()
                updates.put_nowait({k: R.decode_status_value(k, v) for k, v in codec.parse_tlv(msg[2:])})

        task = asyncio.create_task(pump())
        self.s.add_cleanup(lambda: _cancel(task))
        return {k: R.decode_status_value(k, v) for k, v in codec.parse_tlv(body)}, updates

    async def _unregister_statuses(self) -> None:
        if self._status_push:
            await self.s.query(Query.UNREGISTER_STATUSES, sorted(self._status_push))
            self._status_push.clear()

    async def wait_status(self, status_id: int, predicate, timeout: float, poll: float = 0.5) -> int | str:
        """Poll a status until predicate(value) holds."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            value = await self.status(status_id)
            if predicate(value):
                return value
            if asyncio.get_running_loop().time() > deadline:
                raise Timeout(f"{R.status_name(status_id)} stayed {value!r} for {timeout:.0f} s")
            await asyncio.sleep(poll)

    # ---------------------------------------------------------------- camera Wi-Fi AP

    async def set_ap(self, on: bool) -> None:
        await self.s.command(Cmd.AP_CONTROL, b"\x01" if on else b"\x00")

    async def wifi_credentials(self) -> tuple[str, str]:
        ssid = (await self.s.read(R.WAP_SSID)).decode("utf-8", errors="replace")
        password = (await self.s.read(R.WAP_PASSWORD)).decode("utf-8", errors="replace")
        return ssid, password

    # ---------------------------------------------------------------- network provisioning

    async def scan_networks(self, timeout: float = 30.0) -> list[dict]:
        q = self.s.subscribe((R.NET_MGMT_RESP, Feature.NETWORK, Action.NOTIF_SCAN))
        try:
            r = await self.s.protobuf(
                Feature.NETWORK, Action.SCAN_NETWORKS, "RequestStartScan", {}, "ResponseStartScanning"
            )
            self._check(r, "Wi-Fi scan")
            while True:
                _, msg = await asyncio.wait_for(q.get(), timeout)
                n = proto.decode("NotifStartScanning", msg[2:])
                if n.get("scanning_state") == proto.SCANNING_SUCCESS:
                    break
                if n.get("scanning_state") in (3, 4):
                    raise Rejected("Wi-Fi scan", proto.SCANNING[n["scanning_state"]])
        except asyncio.TimeoutError:
            raise Timeout(f"the camera's Wi-Fi scan didn't finish within {timeout:.0f} s") from None
        finally:
            self.s.unsubscribe(q)
        r = await self.s.protobuf(
            Feature.NETWORK,
            Action.GET_AP_ENTRIES,
            "RequestGetApEntries",
            {"start_index": 0, "max_entries": 100, "scan_id": n.get("scan_id", 0)},
            "ResponseGetApEntries",
        )
        self._check(r, "Wi-Fi scan results")
        return r["entries"]

    async def join_network(self, ssid: str, password: str | None, configured: bool, timeout: float = 60.0) -> int:
        """Ask the camera to join a Wi-Fi network. Returns the final provisioning state (5 or 6)."""
        q = self.s.subscribe((R.NET_MGMT_RESP, Feature.NETWORK, Action.NOTIF_PROVISIONING))
        try:
            state = await self._provision(q, ssid, password, configured, timeout)
            if state == 8 and configured and password is not None:  # remembered entry with a stale password
                state = await self._provision(q, ssid, password, False, timeout)
        finally:
            self.s.unsubscribe(q)
        if state not in proto.PROVISIONING_DONE:
            raise Rejected(f"join {ssid!r}", proto.PROVISIONING.get(state, state))
        return state

    async def _provision(
        self, q: asyncio.Queue, ssid: str, password: str | None, configured: bool, timeout: float
    ) -> int:
        if configured:
            r = await self.s.protobuf(
                Feature.NETWORK, Action.CONNECT, "RequestConnect", {"ssid": ssid}, "ResponseConnect"
            )
        else:
            r = await self.s.protobuf(
                Feature.NETWORK,
                Action.CONNECT_NEW,
                "RequestConnectNew",
                {"ssid": ssid, "password": password or "", "bypass_eula_check": True},
                "ResponseConnectNew",
            )
        self._check(r, f"join {ssid!r}")
        state = r.get("provisioning_state", 0)
        deadline = asyncio.get_running_loop().time() + timeout
        while state in (0, 1, 2):
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise Timeout(f"the camera didn't finish joining {ssid!r} within {timeout:.0f} s")
            try:
                _, msg = await asyncio.wait_for(q.get(), left)
            except asyncio.TimeoutError:
                continue
            state = proto.decode("NotifProvisioningState", msg[2:]).get("provisioning_state", 0)
        return state

    async def release_network(self) -> None:
        r = await self.s.protobuf(
            Feature.COMMAND, Action.RELEASE_NETWORK, "RequestReleaseNetwork", {}, "ResponseGeneric"
        )
        self._check(r, "release network")

    # ---------------------------------------------------------------- livestream

    async def livestream_status(self, register: bool = False) -> dict:
        req = {"register_live_stream_status": list(proto.REGISTER_LIVE_STREAM_STATUS) if register else []}
        return await self.s.protobuf(
            Feature.QUERY, Action.LIVESTREAM_STATUS, "RequestGetLiveStreamStatus", req, "NotifyLiveStreamStatus"
        )

    async def set_livestream_mode(
        self,
        url: str,
        *,
        encode: bool = False,
        resolution: int = 720,
        min_kbps: int = 800,
        max_kbps: int = 8000,
        start_kbps: int = 4000,
        lens: str = "wide",
    ) -> None:
        values = (
            {
                "url": url,
                "encode": encode,
                "window_size": proto.WINDOW_SIZE[resolution],
                "minimum_bitrate": min_kbps,
                "maximum_bitrate": max_kbps,
                "starting_bitrate": start_kbps,
                "lens": proto.LENS[lens],
            }
            if url
            else {"url": ""}
        )
        r = await self.s.protobuf(
            Feature.COMMAND, Action.LIVESTREAM_MODE, "RequestSetLiveStreamMode", values, "ResponseGeneric"
        )
        self._check(r, "set livestream mode")


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()

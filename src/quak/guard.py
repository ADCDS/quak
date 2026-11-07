"""SettingsGuard: snapshot the camera before changing it, put everything back afterwards.

Settings can belong to a preset, so the guard snapshots every preset it is about to touch
(right after loading it, before any write). The snapshot is written to a JSON ledger first,
so `quak restore --ledger FILE` can replay it after a crash.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from quak.camera import Camera
from quak.core import registry as R
from quak.core.errors import QuakError
from quak.core.registry import Status

RESTORE_FIRST = (180, 2, 3, 121)  # mode and resolution/fps/lens constrain the rest


class SettingsGuard:
    def __init__(self, camera: Camera, ledger: Path | None = None) -> None:
        self.cam = camera
        self.ledger = ledger
        self.state: dict = {
            "preset": None,
            "group": None,
            "statuses": {},
            "control_mode": None,
            "changed_control": False,
            "presets": {},
        }

    @classmethod
    def from_ledger(cls, camera: Camera, ledger: Path) -> "SettingsGuard":
        g = cls(camera, ledger)
        g.state = json.loads(ledger.read_text())
        return g

    def _save(self) -> None:
        if self.ledger:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger.write_text(json.dumps(self.state, indent=1))

    async def snapshot(self, all_presets: bool = False) -> None:
        """Snapshot the current preset; with all_presets, every visible preset (then reload the original)."""
        st = await self.cam.statuses(
            [Status.PRESET_GROUP, Status.PRESET, Status.AP_MODE, Status.TURBO, Status.CAMERA_CONTROL]
        )
        self.state.update(
            preset=st[Status.PRESET],
            group=st[Status.PRESET_GROUP],
            statuses={str(k): st[k] for k in (Status.AP_MODE, Status.TURBO, Status.CAMERA_CONTROL)},
            control_mode=await self.cam.setting(R.CONTROL_MODE),
            changed_control=True,
        )
        original = int(st[Status.PRESET])
        await self._snap(original)
        if all_presets:
            for group in await self.cam.presets():
                for preset in group["preset_array"]:
                    if str(preset["id"]) not in self.state["presets"]:
                        await self._load_preset_retrying(preset["id"])
                        await self._snap(await self._wait_preset(lambda p, i=preset["id"]: p == i))
            await self.cam.load_preset(original)
            await self._wait_preset(lambda p: p == original)

    async def _snap(self, preset_id: int) -> None:
        if str(preset_id) not in self.state["presets"]:
            self.state["presets"][str(preset_id)] = {str(k): v for k, v in (await self.cam.settings()).items()}
            self._save()

    async def _current_preset(self) -> int:
        return int(await self.cam.status(Status.PRESET))

    async def _wait_preset(self, check, timeout: float = 5.0) -> int:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            pid = await self._current_preset()
            if check(pid) or asyncio.get_running_loop().time() > deadline:
                return pid
            await asyncio.sleep(0.2)

    async def load_preset(self, preset_id: int) -> None:
        await self.cam.load_preset(preset_id)
        await self._snap(await self._wait_preset(lambda p: p == preset_id))

    async def load_group(self, group: str) -> int:
        await self.cam.load_group(group)
        pid = await self._wait_preset(lambda p: R.preset_group_of(p) == group)
        await self._snap(pid)
        return pid

    async def set_setting(self, setting_id: int, value: int, verify: bool = True) -> None:
        if setting_id == R.CONTROL_MODE:
            self.state["changed_control"] = True
            self._save()
        await self._snap(await self._current_preset())
        await self.cam.set_setting(setting_id, value, verify=verify)

    async def _load_preset_retrying(self, preset_id: int) -> None:
        """A HERO11 refuses a preset load for a few seconds after Slo-Mo (it reconfigures the sensor),
        seen walking all presets for a snapshot; the same request is accepted a moment later."""
        for attempt in range(4):
            try:
                await self.cam.load_preset(preset_id)
                return
            except QuakError:
                if attempt == 3:
                    raise
                await asyncio.sleep(2.0)

    async def restore(self) -> list[str]:
        """Write back everything that differs from the snapshot. Returns what changed."""
        done: list[str] = []
        self.refused: list[str] = []
        presets = {int(k): {int(s): v for s, v in vals.items()} for k, vals in self.state["presets"].items()}
        # Newest snapshot first, the original preset last: a global setting changed before a later
        # preset's snapshot is then put back by the original (earliest, pre-change) snapshot.
        for pid, snap in reversed(list(presets.items())):
            if await self._current_preset() != pid:
                await self.cam.load_preset(pid)
                await self._wait_preset(lambda p, pid=pid: p == pid)
            now = await self.cam.settings()
            order = [s for s in RESTORE_FIRST if s in snap] + sorted(s for s in snap if s not in RESTORE_FIRST)
            order = [s for s in order if s != R.CONTROL_MODE]
            failed = []
            for sid in order:
                if now.get(sid) != snap[sid]:
                    try:
                        await self.cam.set_setting(sid, snap[sid], verify=False)
                        done.append(f"{R.setting_name(sid)} = {R.value_label(sid, snap[sid])}")
                    except QuakError:
                        failed.append(sid)
            for sid in failed:  # a dependent setting may only be accepted after the others
                try:
                    await self.cam.set_setting(sid, snap[sid], verify=False)
                    done.append(f"{R.setting_name(sid)} = {R.value_label(sid, snap[sid])}")
                except QuakError as e:
                    # e.g. the webcam settings: readable in every mode, writable only in webcam mode
                    self.refused.append(f"{R.setting_name(sid)} (preset 0x{pid:08x}): {e}")
        if self.state["changed_control"] and self.state["control_mode"] is not None:
            if await self.cam.setting(R.CONTROL_MODE) != self.state["control_mode"]:
                await self.cam.set_setting(R.CONTROL_MODE, self.state["control_mode"], verify=False)
                done.append(f"Control Mode = {R.value_label(R.CONTROL_MODE, self.state['control_mode'])}")
        if self.state["preset"] is not None and await self._current_preset() != self.state["preset"]:
            await self.cam.load_preset(int(self.state["preset"]))
            done.append(f"preset 0x{int(self.state['preset']):08x}")
        st = await self.cam.statuses([Status.AP_MODE, Status.TURBO])
        want = self.state["statuses"]
        if str(Status.AP_MODE) in want and st[Status.AP_MODE] != want[str(Status.AP_MODE)]:
            target = bool(want[str(Status.AP_MODE)])
            await self.cam.set_ap(target)
            await self.cam.wait_status(Status.AP_MODE, lambda v: bool(v) == target, 15, poll=0.3)
            done.append("camera Wi-Fi " + ("on" if target else "off"))
        if st[Status.TURBO] and not want.get(str(Status.TURBO)):
            await self.cam.turbo(False)
            done.append("turbo off")
        return done

    async def diff(self) -> dict[int, dict[int, tuple[int, int]]]:
        """{preset: {setting: (snapshot, now)}} for the current preset only (cheap check)."""
        pid = await self._current_preset()
        snap = {int(s): v for s, v in self.state["presets"].get(str(pid), {}).items()}
        now = await self.cam.settings()
        return {pid: {s: (v, now.get(s)) for s, v in snap.items() if now.get(s) != v}}

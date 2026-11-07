"""GUI end-to-end test: drive the real Quak GUI (web mode) like a person, against the
simulated or the real camera.

It starts `quak-gui --web`, a headless Chrome, and clicks controls by their visible labels
through Flutter's accessibility tree (Chrome DevTools protocol). Every step saves a
screenshot. Around the run it uses the CLI: a settings snapshot before (the GUI isn't
connected yet) and restore + verify after, so the camera ends up as it started.

    python -m quak.e2e.gui --sim
    python -m quak.e2e.gui            # real camera (it must be free: one BLE client at a time)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Some USB Bluetooth dongles crash on a scan that follows another connection too closely
BLUETOOTH_REST_SECONDS = 30

STARTUP_TIMEOUT = 120  # seconds for Chrome, then the page, to come up on a cold machine

JS_HELPERS = r"""
window.quak = {
  label: e => { const l = (e.getAttribute('aria-label') || e.innerText || '').trim(); const p = l.split('\n');
                 return p.length > 1 && p.every(x => x === p[0]) ? p[0] : l; },  // "Back\nBack" = label + tooltip
  nodes: () => [...document.querySelectorAll('flt-semantics')],
  labels: () => quak.nodes().map(quak.label).filter(Boolean),
  find: (label, exact) => {
    const nodes = quak.nodes().reverse();
    return nodes.find(e => quak.label(e) === label) ||
           (exact ? null : nodes.find(e => quak.label(e).startsWith(label + ' ') || quak.label(e).startsWith(label + '\n')));
  },
  click: (label, exact) => { const el = quak.find(label, exact); if (!el) return false; el.click(); return true; },
  has: text => quak.labels().some(l => l.includes(text)),
  enable: () => { const p = document.querySelector('flt-semantics-placeholder'); if (p) p.click(); return !!p; },
};
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Ui:
    """A tiny Chrome DevTools protocol client for one page."""

    def __init__(self, ws, shots: Path) -> None:
        self.ws, self.shots, self._id, self.step = ws, shots, 0, 0

    async def call(self, method: str, **params) -> dict:
        self._id += 1
        my = self._id
        await self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        return r.get("result", {}).get("value")

    async def ready(self, timeout: float = 30) -> None:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if await self.js("!!document.querySelector('flt-semantics-placeholder, flt-semantics')"):
                    await self.js(JS_HELPERS)
                    await self.js("quak.enable()")
                    await asyncio.sleep(1)
                    if await self.js("quak.labels().length > 3"):
                        return
            except RuntimeError as e:  # the page is still loading (no execution context yet)
                if "execution context" not in str(e):
                    raise
            await asyncio.sleep(0.5)
        raise AssertionError("the GUI never rendered")

    @staticmethod
    def _short(labels: list[str]) -> list[str]:
        """Leaf labels only (containers repeat their children's text), each cut short, for error messages."""
        leaves = [l for l in labels if "\n" not in l]
        return [l[:60] for l in leaves[:30]]

    async def labels(self) -> list[str]:
        return await self.js("quak.labels()") or []

    async def has(self, text: str) -> bool:
        return bool(await self.js(f"quak.has({json.dumps(text)})"))

    async def wait_text(self, text: str, timeout: float = 15) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if await self.has(text):
                return
            await asyncio.sleep(0.3)
        raise AssertionError(f"{text!r} never appeared; visible: {self._short(await self.labels())}")

    async def click(self, label: str, exact: bool = False, timeout: float = 10, settle: float = 0.8) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if await self.js(f"quak.click({json.dumps(label)}, {json.dumps(exact)})"):
                await asyncio.sleep(settle)
                return
            await asyncio.sleep(0.3)
        raise AssertionError(f"no control labelled {label!r}; visible: {self._short(await self.labels())}")

    async def click_first(self, prefix: str) -> str:
        labels = await self.labels()
        match = next((l for l in labels if l.startswith(prefix)), None)
        if not match:
            raise AssertionError(f"nothing starting with {prefix!r}")
        await self.click(match, exact=True)
        return match

    async def shot(self, name: str) -> Path:
        self.step += 1
        data = (await self.call("Page.captureScreenshot", format="png"))["data"]
        path = self.shots / f"{self.step:02d}-{name}.png"
        path.write_bytes(base64.b64decode(data))
        return path


class GuiRun:
    def __init__(self, sim: bool, session: Path, chrome_port: int | None, keep: bool) -> None:
        self.sim, self.session, self.keep = sim, session, keep
        self.chrome_port = chrome_port
        self.procs: list[subprocess.Popen] = []
        self.env = dict(os.environ)
        if sim:
            self.env.update(
                QUAK_SIM="1",
                QUAK_SIM_STATE=str(session / "sim-camera.json"),
                QUAK_CONFIG_DIR=str(session / "config"),
                QUAK_STATE_DIR=str(session / "state"),
            )
        self.env["QUAK_DOWNLOADS"] = str(session / "downloads")
        self.results: list[dict] = []

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.session / "gui-run.log", "a") as fh:
            fh.write(line + "\n")

    def quak(self, *args: str, timeout: float = 300) -> dict:
        cmd = [sys.executable, "-m", "quak", "--json"] + (["--sim"] if self.sim else []) + list(args)
        p = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=timeout)
        if p.returncode:
            raise AssertionError(f"quak {' '.join(args)} failed: {(p.stdout + p.stderr)[-400:]}")
        return json.loads(p.stdout or "{}")

    def start_gui(self, port: int) -> None:
        args = [sys.executable, "-m", "quak.gui.app", "--web", "--port", str(port)] + (["--sim"] if self.sim else [])
        # no desktop browser; the test mode steps need developer mode
        env = dict(self.env, BROWSER="true", QUAK_DEV="1", QUAK_LANG=os.environ.get("QUAK_LANG", "en"))
        self.procs.append(
            subprocess.Popen(
                args,
                env=env,
                stdout=open(self.session / "gui-server.log", "w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                return
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("quak-gui --web did not start")

    def start_chrome(self) -> int:
        if self.chrome_port:
            return self.chrome_port
        chrome = next(
            (shutil.which(b) for b in ("google-chrome", "chromium", "chromium-browser") if shutil.which(b)), None
        )
        if not chrome:
            raise RuntimeError("Chrome/Chromium is needed for the GUI test")
        port = free_port()
        profile = tempfile.mkdtemp(prefix="quak-chrome-")
        log = self.session / "chrome.log"
        with open(log, "w") as out:
            proc = subprocess.Popen(
                [
                    chrome,
                    "--headless=new",
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--enable-unsafe-swiftshader",
                    "about:blank",
                ],
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.procs.append(proc)
        # Wait for what the test needs, DevTools answering, or for Chrome to die. How long that takes depends on the
        # machine: loading Chrome from a cold disk is far slower than from a warm one.
        t0 = time.time()
        while time.time() - t0 < STARTUP_TIMEOUT:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"headless Chrome exited with {proc.returncode}: {log.read_text(errors='replace')[-800:]}"
                )
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                self.log(f"Chrome answered after {time.time() - t0:.1f} s")
                return port
            except OSError:
                time.sleep(0.25)
        raise RuntimeError(
            f"headless Chrome didn't answer within {STARTUP_TIMEOUT} s: {log.read_text(errors='replace')[-800:]}"
        )

    def stop(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)

    async def step(self, ui: Ui, name: str, fn) -> None:
        self.log(f"== {name}")
        t0 = time.time()
        for _ in range(3):  # a dialog left open by a failed step must not break the next one
            if not await ui.js("quak.click('Close', true) || quak.click('Cancel', true)"):
                break
            await asyncio.sleep(0.5)
        try:
            detail = await fn(ui) or ""
            status = "pass"
        except Exception as e:  # noqa: BLE001
            status, detail = "fail", f"{type(e).__name__}: {e}"
        shot = await ui.shot(name.split()[0].lower())
        self.results.append(
            {
                "step": name,
                "status": status,
                "detail": detail,
                "seconds": round(time.time() - t0, 1),
                "screenshot": str(shot),
            }
        )
        self.log(f"   {status.upper()} {name}" + (f": {detail}" if detail else ""))


async def gui_steps(run: GuiRun, ui: Ui, only: set[str] | None = None) -> None:
    from quak.e2e.gui_steps import STEPS

    for name, fn in STEPS:
        if only and name.split()[0] not in only:
            continue
        await run.step(ui, name, lambda u, fn=fn: fn(u, run))
        if name.startswith("G01") and run.results[-1]["status"] == "fail":
            run.log("   stopping: the app never reached the camera, so the other steps can't run")
            break


async def main_async(a) -> int:
    import websockets

    from quak import config

    session = Path(a.session) if a.session else config.state_dir() / "e2e-gui" / time.strftime("%Y%m%d-%H%M%S")
    (session / "shots").mkdir(parents=True, exist_ok=True)
    run = GuiRun(a.sim, session, a.chrome_port, a.keep)
    run.log(f"GUI E2E session {session} ({'simulator' if a.sim else 'real camera'})")
    ledger = session / "settings.ledger.json"
    run.quak("snapshot", "--ledger", str(ledger))
    run.state = {"ledger": str(ledger), "baseline": None}
    if not a.sim:
        await asyncio.sleep(BLUETOOTH_REST_SECONDS)
    gui_port = free_port()
    try:
        run.start_gui(gui_port)
        cport = run.start_chrome()
        target = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{cport}/json/new?http://127.0.0.1:{gui_port}/", method="PUT")
            ).read()
        )
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            ui = Ui(ws, session / "shots")
            await ui.call("Page.enable")
            await ui.call("Emulation.setDeviceMetricsOverride", width=412, height=915, deviceScaleFactor=1, mobile=True)
            await ui.ready(STARTUP_TIMEOUT)
            await gui_steps(run, ui, {s.strip().upper() for s in a.only.split(",")} if a.only else None)
    finally:
        if not a.keep:
            run.stop()
            if not a.sim and sys.platform.startswith("linux"):  # the GUI was killed with its Wi-Fi link open
                from quak.net.nmcli import NetworkManager

                for device in NetworkManager().recover():
                    run.log(f"restored auto-connect on {device}")
        await asyncio.sleep(1 if a.sim else BLUETOOTH_REST_SECONDS)
    try:
        restored = run.quak("restore", "--ledger", str(ledger))["restored"]
        after = session / "after.ledger.json"
        run.quak("snapshot", "--ledger", str(after))
        before_d, after_d = json.loads(ledger.read_text()), json.loads(after.read_text())
        diffs = {
            pid: {
                s: (v, after_d["presets"].get(pid, {}).get(s))
                for s, v in vals.items()
                if after_d["presets"].get(pid, {}).get(s) != v
            }
            for pid, vals in before_d["presets"].items()
        }
        from quak.core import registry as R

        # undocumented settings (e.g. 31) are camera-internal and not writable: noted, not a failure (as in S18)
        odd = {k: {s: d for s, d in v.items() if int(s) not in R.SETTINGS} for k, v in diffs.items()}
        odd = {k: v for k, v in odd.items() if v}
        diffs = {k: {s: d for s, d in v.items() if int(s) in R.SETTINGS} for k, v in diffs.items()}
        diffs = {k: v for k, v in diffs.items() if v}
        run.results.append(
            {
                "step": "Restore settings (CLI)",
                "status": "fail" if diffs else "pass",
                "detail": f"restored {len(restored)} values"
                + (f"; differences {diffs}" if diffs else "")
                + (f"; undocumented, left alone: {odd}" if odd else ""),
            }
        )
    except AssertionError as e:
        run.results.append({"step": "Restore settings (CLI)", "status": "fail", "detail": str(e)})
    (session / "gui-results.json").write_text(json.dumps(run.results, indent=1))
    print()
    for r in run.results:
        print(f"  {r['status'].upper():<5} {r['step']}")
    print(f"\nscreenshots: {session / 'shots'}")
    return sum(r["status"] == "fail" for r in run.results)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m quak.e2e.gui", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--sim", action="store_true")
    p.add_argument("--session")
    p.add_argument("--chrome-port", type=int, help="use an already running Chrome with remote debugging")
    p.add_argument("--keep", action="store_true", help="leave the GUI and Chrome running afterwards")
    p.add_argument("--only", help="comma-separated steps, e.g. G03,G09,G11 (G11 needs G03's baseline)")
    a = p.parse_args()
    sys.exit(1 if asyncio.run(main_async(a)) else 0)


if __name__ == "__main__":
    main()

"""Hardware end-to-end runner. Every feature is exercised through the real `quak` CLI
(subprocesses, JSON output) and checked independently; the same scenarios run against the
simulated camera (--sim) so the runner itself is tested before it touches the camera.

Safety: settings are snapshotted first and restored at the end (SettingsGuard ledger);
only media recorded by this session is deleted (baseline + time windows); the camera
never gets home Wi-Fi credentials (livestream uses the lab hotspot).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from quak.net.http import WINDOW_EARLY, camera_epoch

SCENARIOS: list["Scenario"] = []


@dataclass
class Scenario:
    id: str
    title: str
    fn: Callable
    needs: tuple[str, ...] = ()  # "wifi", "hotspot", "human", "ffmpeg"
    minutes: float = 0.5


def scenario(id: str, title: str, needs: tuple[str, ...] = (), minutes: float = 0.5):
    def wrap(fn):
        SCENARIOS.append(Scenario(id, title, fn, needs, minutes))
        return fn

    return wrap


class Fail(AssertionError):
    pass


class Skip(Exception):
    pass


def check(cond, msg: str) -> None:
    if not cond:
        raise Fail(msg)


@dataclass
class Context:
    session_dir: Path
    sim: bool
    human: bool
    address: str | None = None
    verbose: bool = False
    env: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    windows: list[tuple[int, int]] = field(default_factory=list)  # camera-local epoch seconds
    state: dict = field(default_factory=dict)
    rest: float = 0  # seconds to wait before each quak command

    # ---------------------------------------------------------------- CLI

    def quak(self, *args: str, expect: int = 0, timeout: float = 240, json_out: bool = True) -> dict:
        if self.rest:
            time.sleep(self.rest)
        cmd = [sys.executable, "-m", "quak"] + (["--json"] if json_out else []) + (["--sim"] if self.sim else [])
        if self.address:
            cmd += ["--address", self.address]
        cmd += list(args)
        t0 = time.time()
        p = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=timeout)
        self.log(f"  $ quak {' '.join(args)}  -> {p.returncode} ({time.time() - t0:.1f} s)")
        if self.verbose and p.stderr.strip():
            self.log("    " + p.stderr.strip().replace("\n", "\n    ")[-1500:])
        if p.returncode != expect:
            raise Fail(
                f"quak {' '.join(args)} exited {p.returncode} (expected {expect}): "
                f"{(p.stdout + p.stderr).strip()[-600:]}"
            )
        if not json_out:
            return {"stdout": p.stdout}
        try:
            return json.loads(p.stdout) if p.stdout.strip() else {}
        except ValueError:
            raise Fail(f"quak {' '.join(args)}: output is not JSON: {p.stdout[:300]}") from None

    def camera_time(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.quak("clock")["camera_clock"])

    def window(self, start: dt.datetime, end: dt.datetime) -> None:
        self.windows.append((camera_epoch(start) - WINDOW_EARLY, camera_epoch(end) + 2))
        self.save_state()

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.session_dir / "run.log", "a") as fh:
            fh.write(line + "\n")

    def finding(self, msg: str) -> None:
        self.findings.append(msg)
        self.log(f"  FINDING: {msg}")

    def prompt(self, msg: str) -> str:
        if not self.human:
            raise Skip("needs a person (run with --human)")
        print(f"\n>>> {msg}\n", flush=True)
        return input("press Enter when done (or type a note): ")

    def save_state(self) -> None:
        data = {"windows": self.windows, "state": self.state, "findings": self.findings}
        (self.session_dir / "session.json").write_text(json.dumps(data, indent=1, default=str))

    def load_state(self) -> None:
        f = self.session_dir / "session.json"
        if f.exists():
            data = json.loads(f.read_text())
            self.windows = [tuple(w) for w in data.get("windows", [])]
            self.state = data.get("state", {})
            self.findings = data.get("findings", [])


def run_scenarios(ctx: Context, only: set[str] | None, stop_on_fail: bool = False) -> int:
    from quak.e2e import scenarios  # noqa: F401  registers SCENARIOS

    results_file = ctx.session_dir / "results.jsonl"
    failures = 0
    have = {
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "human": ctx.human or ctx.sim,
        "wifi": True,
        "hotspot": ctx.sim or sys.platform.startswith("linux"),
    }
    for sc in SCENARIOS:
        if only and sc.id not in only:
            continue
        ctx.log(f"== {sc.id} {sc.title}")
        t0 = time.time()
        missing = [n for n in sc.needs if not have.get(n, True)]
        status, detail = "pass", ""
        if missing:
            status, detail = "skip", f"needs {', '.join(missing)}"
        else:
            for attempt in (1, 2):
                try:
                    detail = sc.fn(ctx) or ""
                    status = "pass"
                    break
                except Skip as e:
                    status, detail = "skip", str(e)
                    break
                except Fail as e:
                    status, detail = "fail", str(e)
                except Exception as e:  # noqa: BLE001 - report, don't crash the run
                    status, detail = "fail", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
                if attempt == 1 and status == "fail" and not ctx.sim:
                    ctx.log(f"  attempt 1 failed ({detail[:200]}); retrying once")
                    time.sleep(3)
                    continue
                break
        elapsed = time.time() - t0
        ctx.log(f"   {status.upper()} {sc.id} in {elapsed:.0f} s" + (f": {detail[:500]}" if detail else ""))
        with open(results_file, "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "id": sc.id,
                        "title": sc.title,
                        "status": status,
                        "seconds": round(elapsed, 1),
                        "detail": detail,
                        "sim": ctx.sim,
                    }
                )
                + "\n"
            )
        if status == "fail":
            failures += 1
            if stop_on_fail:
                break
    ctx.save_state()
    return failures


def main(argv: list[str] | None = None) -> None:
    import argparse

    from quak.e2e import scenarios  # noqa: F401

    p = argparse.ArgumentParser(
        prog="python -m quak.e2e", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--sim", action="store_true", help="run against the simulated camera")
    p.add_argument("--only", help="comma-separated scenario ids, e.g. S01,S13")
    p.add_argument("--list", action="store_true")
    p.add_argument("--human", action="store_true", help="include scenarios that need a person")
    p.add_argument("--resume", help="session directory to continue (keeps media windows)")
    p.add_argument("--address")
    p.add_argument("--stop-on-fail", action="store_true")
    p.add_argument(
        "--rest",
        type=float,
        default=0,
        metavar="SECONDS",
        help="wait before each quak command, for a Bluetooth adapter that crashes when it scans often",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    if a.list:
        for sc in SCENARIOS:
            print(f"{sc.id}  {sc.title:<44} ~{sc.minutes:g} min  {', '.join(sc.needs)}")
        return
    from quak import config

    session = Path(a.resume) if a.resume else config.state_dir() / "e2e" / time.strftime("%Y%m%d-%H%M%S")
    session.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    if a.sim:
        env["QUAK_SIM_STATE"] = str(session / "sim-camera.json")
        env["QUAK_CONFIG_DIR"] = str(session / "config")
        env["QUAK_STATE_DIR"] = str(session / "state")
    os.environ.update({k: env[k] for k in ("QUAK_SIM_STATE", "QUAK_CONFIG_DIR", "QUAK_STATE_DIR") if k in env})
    ctx = Context(session, a.sim, a.human, a.address, a.verbose, env)
    ctx.rest = a.rest
    ctx.load_state()
    ctx.log(f"session {session} ({'simulator' if a.sim else 'real camera'})")
    only = {s.strip().upper() for s in a.only.split(",")} if a.only else None
    failures = run_scenarios(ctx, only, a.stop_on_fail)
    print()
    for line in (session / "results.jsonl").read_text().splitlines():
        r = json.loads(line)
        print(f"  {r['status'].upper():<5} {r['id']}  {r['title']}")
    if ctx.findings:
        print("\nfindings:")
        for f in ctx.findings:
            print(f"  - {f}")
    print(f"\nresults: {session / 'results.jsonl'}")
    sys.exit(1 if failures else 0)

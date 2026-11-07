"""The `quak` command line, run as a subprocess against the simulated camera."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from quak.cli.main import build_parser

SRC = Path(__file__).resolve().parents[1] / "src"


class CliAgainstSim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(
            os.environ,
            PYTHONPATH=str(SRC),
            QUAK_SIM="1",
            QUAK_SIM_STATE=str(Path(self.tmp.name) / "sim.json"),
            QUAK_CONFIG_DIR=str(Path(self.tmp.name) / "cfg"),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def quak(self, *args, expect=0):
        p = subprocess.run(
            [sys.executable, "-m", "quak", "--json", *args], env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(p.returncode, expect, f"quak {' '.join(args)}\n{p.stdout}\n{p.stderr}")
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def test_a_refusal_caused_by_turbo_says_so(self):
        self.plain("turbo", "on")
        p = self.plain("record", "--seconds", "1")
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertIn("quak turbo off", p.stderr)
        self.plain("turbo", "off")
        p = self.plain("record", "--seconds", "1")
        self.assertEqual(p.returncode, 0, p.stderr)

    def plain(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "quak", "--sim", *args], env=self.env, capture_output=True, text=True, timeout=60
        )

    def test_options_work_before_or_after_the_command(self):
        parse = build_parser().parse_args
        self.assertEqual(parse(["pair", "--address", "B"]).address, "B")
        self.assertEqual(parse(["--address", "A", "pair"]).address, "A")  # not overwritten by the command's default
        self.assertEqual(parse(["media", "list", "--wifi", "assume"]).wifi, "assume")
        self.assertEqual(parse(["--wifi", "assume", "media", "list"]).wifi, "assume")
        self.assertEqual(parse(["info"]).wifi, "auto")
        self.assertEqual(parse(["info", "--wifi-adapter", "wlan1"]).wifi_adapter, "wlan1")

    def test_mistakes_give_a_message_and_not_a_traceback(self):
        for args in (
            ["preset", "load"],
            ["preset", "group"],
            ["setting", "raw"],
            ["setting", "raw", "02"],
            ["restore", "--ledger", "/nonexistent/ledger.json"],
        ):
            p = self.plain(*args)
            self.assertEqual(p.returncode, 1, args)
            self.assertNotIn("Traceback", p.stderr, args)
            self.assertTrue(p.stderr.startswith("error:"), (args, p.stderr))

    def test_a_simulated_pairing_leaves_the_real_default_alone(self):
        self.quak("pair", "--address", "SIM")
        cameras = Path(self.tmp.name) / "cfg" / "cameras.json"
        self.assertFalse(cameras.exists())

    def test_scan_lists_cameras(self):
        self.assertEqual(self.quak("scan")["cameras"][0]["address"], "SIM")

    @unittest.skipUnless(os.name == "posix", "POSIX signals")
    def test_sigterm_cleans_up_like_ctrl_c(self):
        p = subprocess.Popen(
            [sys.executable, "-m", "quak", "--sim", "record", "--seconds", "60"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        state = Path(self.env["QUAK_SIM_STATE"])
        for _ in range(100):  # the simulator saves on exit, so give the recording time to start
            time.sleep(0.05)
        p.terminate()
        self.assertEqual(p.wait(30), 130)
        self.assertTrue(state.exists())
        self.assertEqual(self.quak("status", "encoding")["statuses"]["encoding"], 0)

    def test_full_command_tour(self):
        info = self.quak("info")
        self.assertEqual(info["model"], "HERO11 Black")
        self.assertAlmostEqual(info["clock_offset_s"], 4, delta=1.5)
        self.assertEqual(self.quak("setting", "get", "control_mode")["settings"]["175"]["label"], "Easy")
        self.assertEqual(self.quak("setting", "caps", "scheduled_capture")["capabilities"]["168"], [])
        armed = self.quak("schedule", "arm", "+3m", "--duration", "15s")
        self.assertEqual(armed["duration"], "15 Seconds")
        self.assertTrue(self.quak("schedule", "show")["armed"])  # state persisted between runs
        self.assertEqual(self.quak("setting", "get", "175")["settings"]["175"]["label"], "Pro")
        self.assertLessEqual(abs(self.quak("clock", "sync")["offset_s"]), 1)
        rec = self.quak("record", "--seconds", "0.5", "--hilight-at", "0.2")
        self.assertEqual(rec["videos_after"], rec["videos_before"] + 1)
        presets = self.quak("preset", "list")["presets"]
        self.assertTrue(any(p["name"] == "Standard" and p["current"] for p in presets))
        self.quak("preset", "group", "timelapse")
        self.assertEqual(self.quak("status", "preset_group")["statuses"]["preset_group"], 1002)
        self.assertEqual(self.quak("ap", "on")["camera_wifi"], True)
        self.assertIsNone(self.quak("ap", "creds")["password"])  # hidden unless --show-secret
        self.assertTrue(self.quak("ap", "creds", "--show-secret")["password"])
        self.quak("turbo", "on")
        self.quak("turbo", "off")
        self.quak("hilight")
        self.quak("schedule", "off")
        self.assertFalse(self.quak("schedule", "show")["armed"])
        self.quak("sleep")

    def test_errors_map_to_exit_codes(self):
        self.quak("schedule", "arm", "+3m", expect=1)  # no duration
        self.quak("setting", "set", "led", "disco", expect=1)
        self.quak("setting", "set", "scheduled_capture", "3211", expect=7)  # Easy controls: unsupported
        self.quak("sim", "pairing-mode")
        self.assertTrue(self.quak("pair")["new_bond"])
        self.assertFalse(self.quak("pair")["new_bond"])


if __name__ == "__main__":
    unittest.main()

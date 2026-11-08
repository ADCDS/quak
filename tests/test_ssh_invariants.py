"""The Camera Lab must not become a payload installer or a server: only the one SC request leaves Quak.

These are static checks over the SSH/Lab code, so they hold even when no test exercises a path.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE = (ROOT / "src" / "quak" / "ssh", ROOT / "src" / "quak" / "lab")
FORBIDDEN_NAMES = ("dropbearmulti", "scheduled_capture.sh", "autoexec.sh", "busybox")
FORBIDDEN_SERVER = (re.compile(r"\bhttp\.server\b"), re.compile(r"\bHTTPServer\b"), re.compile(r"\.listen\("))
SC = "/gp/gpCamera?cmd=SC&p=%02"


class NoPayloadOrServer(unittest.TestCase):
    def files(self):
        for folder in CODE:
            yield from folder.rglob("*.py")

    def test_no_payload_files_are_named(self):
        found = []
        for path in self.files():
            text = path.read_text(encoding="utf-8")
            for name in FORBIDDEN_NAMES:
                if name in text:
                    found.append(f"{path.relative_to(ROOT)}: {name}")
        self.assertEqual(found, [])

    def test_quak_does_not_run_or_serve_a_staging_server(self):
        found = []
        for path in self.files():
            text = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN_SERVER:
                if pattern.search(text):
                    found.append(f"{path.relative_to(ROOT)}: {pattern.pattern}")
        self.assertEqual(found, [])

    def test_stage_script_is_never_opened_or_run(self):
        found = []
        for path in self.files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"(open|Path|subprocess|run|Popen).*stage\.sh", line):
                    found.append(f"{path.relative_to(ROOT)}:{n}")
        self.assertEqual(found, [])

    def test_gp_camera_is_used_only_for_sc(self):
        calls = []
        for path in self.files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "/gp/gpCamera" in line:
                    calls.append((path.relative_to(ROOT).as_posix(), n, line.strip()))
        self.assertTrue(calls)
        self.assertEqual({path for path, _, _ in calls}, {"src/quak/ssh/arm.py"})
        self.assertTrue(all(SC in line for _, _, line in calls), calls)

    def test_the_sc_path_is_exact(self):
        from quak.ssh.arm import SC_PATH

        self.assertEqual(SC_PATH, SC)

    def test_no_other_camera_http_mutation_is_defined(self):
        """The SSH/Lab code may read sysfs and run commands over SSH; it must not POST to the camera's HTTP API."""
        found = []
        for path in self.files():
            text = path.read_text(encoding="utf-8")
            for method in ("conn.request(\"POST\"", "conn.request('POST'", "urlopen("):
                if method in text:
                    found.append(f"{path.relative_to(ROOT)}: {method}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()

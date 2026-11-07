"""Guards that keep the repo safe to publish and the code safe to run on someone's camera.

1. No personal identifiers (BLE/MAC addresses, camera serials, adapter names) in tracked files.
2. No destructive camera endpoints (format SD, delete all, factory reset) in the code.
3. If ~/.config/quak/private-tokens.txt exists (one token per line, kept outside the repo),
   none of those tokens may appear in tracked files or in git history.
"""

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TOKENS = Path(os.environ.get("QUAK_PRIVATE_TOKENS", Path.home() / ".config/quak/private-tokens.txt"))

MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
MAC_PLACEHOLDERS = {"AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66", "00:00:00:00:00:00"}
IDENTIFIERS = {
    "camera serial": re.compile(r"\bC3\d{12}\b"),
    "camera BLE name": re.compile(r"\bGoPro \d{4}\b"),
    "USB Wi-Fi adapter name": re.compile(r"\bwlx[0-9a-f]{12}\b"),
}
DANGEROUS = re.compile(r"delete/all|storage/delete|factory.?reset|format.?sd", re.IGNORECASE)
CODE_DIRS = ("src/", "legacy/", "extensions/")


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def tracked_files() -> list[Path]:
    out = git("ls-files", "--cached", "--others", "--exclude-standard")
    if out is not None:
        return [ROOT / line for line in out.splitlines() if line and (ROOT / line).is_file()]
    skip = {".git", ".venv", "build", "dist", "__pycache__", "logs"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.relative_to(ROOT).parts)]


def text_of(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


class SafetyTests(unittest.TestCase):
    def test_no_personal_identifiers(self):
        problems = []
        for path in tracked_files():
            text = text_of(path)
            if text is None:
                continue
            rel = path.relative_to(ROOT).as_posix()
            for mac in MAC.findall(text):
                if mac.upper() not in MAC_PLACEHOLDERS:
                    problems.append(f"{rel}: address {mac}")
            for what, pattern in IDENTIFIERS.items():
                for hit in pattern.findall(text):
                    problems.append(f"{rel}: {what} {hit}")
        self.assertEqual(problems, [], "personal identifiers found:\n" + "\n".join(problems))

    def test_no_destructive_camera_endpoints_in_code(self):
        problems = []
        for path in tracked_files():
            rel = path.relative_to(ROOT).as_posix()
            if not rel.startswith(CODE_DIRS):
                continue
            text = text_of(path) or ""
            for n, line in enumerate(text.splitlines(), 1):
                if DANGEROUS.search(line):
                    problems.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(problems, [], "destructive endpoints found:\n" + "\n".join(problems))

    @unittest.skipUnless(PRIVATE_TOKENS.is_file(), "no local private-token list")
    def test_private_tokens_absent_from_tree_and_history(self):
        tokens = [t.strip() for t in PRIVATE_TOKENS.read_text().splitlines() if t.strip()]
        blobs = {p.relative_to(ROOT).as_posix(): (text_of(p) or "") for p in tracked_files()}
        history = git("log", "-p", "--all") or ""
        problems = []
        for i, token in enumerate(tokens, 1):  # report by line number, never echo the token
            low = token.lower()
            problems += [f"token #{i} in {rel}" for rel, text in blobs.items() if low in text.lower()]
            if low in history.lower():
                problems.append(f"token #{i} in git history")
        self.assertEqual(problems, [], "private tokens found:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()

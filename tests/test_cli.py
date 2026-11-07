import os
import subprocess
import sys
import unittest
from pathlib import Path

import quak
from quak.cli.main import main

SRC = Path(__file__).resolve().parents[1] / "src"


class CliTests(unittest.TestCase):
    def test_version_flag(self):
        with self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_module_entry_point(self):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        out = subprocess.run(
            [sys.executable, "-m", "quak", "--version"], env=env, capture_output=True, text=True, check=True
        ).stdout
        self.assertIn(quak.__version__, out)


if __name__ == "__main__":
    unittest.main()

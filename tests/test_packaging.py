"""Facts about what ships, which only a mistake would change."""

import re
import unittest
from pathlib import Path

import quak
from quak import i18n

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_every_version_agrees(self):
        for path in ("app/pyproject.toml", "extensions/quak_native/pyproject.toml"):
            found = re.search(r'^version = "(.+)"', (ROOT / path).read_text(), re.M)[1]
            self.assertEqual(found, quak.__version__, path)
        self.assertIn('attr = "quak.__version__"', (ROOT / "pyproject.toml").read_text())

    def test_the_translations_ship_inside_the_package(self):
        self.assertIn('quak = ["locale/*.json"]', (ROOT / "pyproject.toml").read_text())
        catalogs = {p.stem for p in (ROOT / "src/quak/locale").glob("*.json")}
        self.assertEqual(catalogs, set(i18n.LANGUAGES) - {i18n.DEFAULT})

    def test_the_licence_notices_ship_inside_the_app(self):
        public = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertEqual((ROOT / "src/quak/gui/assets/THIRD_PARTY_NOTICES.md").read_text(), public)
        for needed in (
            "mpv",
            "FFmpeg",
            "LGPL",
            "Inter",
            "Space Grotesk",
            "JetBrains Mono",
            "Flutter",
            "Flet",
            "bleak",
            "universal_ble",
            "wifi_iot",
            "GoPro, Inc.",
        ):
            self.assertIn(needed, public)


if __name__ == "__main__":
    unittest.main()

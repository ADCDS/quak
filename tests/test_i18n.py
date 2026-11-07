"""Translations stay complete and correct, the code has no text left out of them, and the command line stays in English."""

import ast
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from quak import i18n

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("i18n_sync", ROOT / "tools" / "i18n_sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

PLACEHOLDER = re.compile(r"\{(\w*)\}")
# Text on screen that's the same in every language: names, and what the camera's own hardware is called
NOT_TRANSLATED = {
    "Quak",
    "GoPro",
    "HERO11 Black",
    "HyperSmooth",
    "HiLight",
    "Bluetooth",
    "Wi-Fi",
    "RTMP",
    "YouTube",
    "Twitch",
    "MIT",
    "FFmpeg",
    "HTTP",
    "SSID",
}
# Keyword arguments and calls whose strings are never shown to a person
NOT_ON_SCREEN_KEYWORDS = {
    "font_family",
    "src",
    "key",
    "data",
    "semantics_label",
    "route",
    "keyboard_type",
    "prog",
    "metavar",
}
NOT_ON_SCREEN_CALLS = {
    "print",
    "add_argument",
    "ArgumentParser",
    "getattr",
    "get",
    "get_pref",
    "set_pref",
    "pop",
    "setdefault",
}


def _catalog(code: str) -> dict:
    return json.loads(sync.catalog_path(code).read_text(encoding="utf-8"))


def _forms(value) -> list[str]:
    return list(value.values()) if isinstance(value, dict) else [value]


def _looks_like_words(text: str) -> bool:
    """Words a person reads: a capital letter to start, or a space between words. Not identifiers, paths or formats."""
    text = text.strip()
    if not re.search(r"[A-Za-z]{2}", text) or text in NOT_TRANSLATED:
        return False
    if re.fullmatch(r"[a-z0-9_./:%-]+", text) or text.startswith(("#", "%", "http", "udp:", "rtmp:", "fonts/")):
        return False
    return " " in text or text[0].isupper()


def untranslated_text(paths) -> list[str]:
    """Strings in the app's code that a person would read, and that don't go through tr(), trn() or N_()."""
    found = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
        }

        def exempt(node) -> bool:
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, ast.Call):
                    name = (
                        parent.func.attr if isinstance(parent.func, ast.Attribute) else getattr(parent.func, "id", "")
                    )
                    if name in ("tr", "trn", "N_", "term") or name in NOT_ON_SCREEN_CALLS:
                        return True
                if isinstance(parent, ast.keyword) and parent.arg in NOT_ON_SCREEN_KEYWORDS:
                    return True
                if isinstance(parent, ast.Compare):  # comparing with a value, not showing it
                    return True
                if isinstance(parent, ast.Subscript) and parent.slice is node:
                    return True
                parent = parents.get(parent)
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings or isinstance(parents.get(node), ast.JoinedStr):
                    continue
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(v.value if isinstance(v, ast.Constant) else "x" for v in node.values)
            else:
                continue
            if _looks_like_words(text) and not exempt(node):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}: {text[:70]!r}")
    return found


class Catalogs(unittest.TestCase):
    def test_the_catalogs_match_the_code(self):
        messages = sync.wanted()
        _, problems = sync.code_messages()
        self.assertEqual(problems, [])
        for code in sync.languages():
            have = _catalog(code)
            self.assertEqual(sorted(have), sorted(messages), f"{code}: run tools/i18n_sync.py")

    def test_everything_is_translated(self):
        for code in sync.languages():
            missing = [key for key, value in _catalog(code).items() if not all(_forms(value))]
            self.assertEqual(missing[:20], [], f"{code}: {len(missing)} missing")

    def test_translations_keep_their_placeholders(self):
        for code in sync.languages():
            for key, value in _catalog(code).items():
                for form in _forms(value):
                    # A plural's "one" form may name the number or say it in words, as long as nothing new appears
                    want = set(PLACEHOLDER.findall(key))
                    got = set(PLACEHOLDER.findall(form))
                    if isinstance(value, dict):
                        self.assertLessEqual(got - {"n"}, want - {"n"}, f"{code}: {key!r} -> {form!r}")
                    else:
                        self.assertEqual(got, want, f"{code}: {key!r} -> {form!r}")

    def test_translations_format_without_errors(self):
        for code in sync.languages():
            for key, value in _catalog(code).items():
                names = {name: "x" for name in PLACEHOLDER.findall(key) if name}
                for form in _forms(value):
                    form.format(n=2, **names)

    def test_names_arent_translated(self):
        """Quak, GoPro and the camera's model read the same everywhere, including in Japanese or Russian text."""
        for code in sync.languages():
            for key, value in _catalog(code).items():
                for name in ("Quak", "GoPro", "HERO11", "HiLight", "HyperSmooth"):
                    if name in key:
                        for form in _forms(value):
                            self.assertIn(name, form, f"{code}: {key!r} -> {form!r}")


class NoTextLeftOut(unittest.TestCase):
    def test_the_app_has_no_text_outside_the_catalogs(self):
        paths = sorted((ROOT / "src/quak/gui").rglob("*.py"))
        paths = [p for p in paths if p.name != "tour.py"]
        found = untranslated_text(paths)
        self.assertEqual(found, [], f"{len(found)} strings aren't translated")


class Language(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.set_language, i18n.DEFAULT)

    def test_english_is_the_text_itself(self):
        i18n.set_language("en")
        self.assertEqual(i18n.tr("Delete {name}?", name="X"), "Delete X?")
        self.assertEqual(i18n.trn("{n} item", "{n} items", 1), "1 item")
        self.assertEqual(i18n.trn("{n} item", "{n} items", 2), "2 items")

    def test_an_unknown_language_is_english(self):
        i18n.set_language("xx")
        self.assertEqual(i18n.language(), "en")

    def test_text_without_a_translation_shows_in_english(self):
        i18n.set_language("de")
        self.assertEqual(i18n.tr("Nothing translates this {x}", x=1), "Nothing translates this 1")

    def test_a_bad_translation_doesnt_crash(self):
        i18n.set_language("de")
        i18n._catalog["Delete {name}?"] = "{wrong} löschen?"
        self.assertEqual(i18n.tr("Delete {name}?", name="X"), "Delete X?")

    def test_plural_forms(self):
        cases = {
            "en": {0: "other", 1: "one", 2: "other"},
            "de": {1: "one", 2: "other"},
            "fr": {0: "one", 1: "one", 2: "other"},
            "pt-BR": {0: "one", 1: "one", 2: "other"},
            "ru": {
                1: "one",
                21: "one",
                2: "few",
                4: "few",
                22: "few",
                5: "many",
                11: "many",
                12: "many",
                14: "many",
                100: "many",
            },
            "ja": {1: "other", 5: "other"},
            "zh-CN": {1: "other", 5: "other"},
        }
        for code, numbers in cases.items():
            for n, form in numbers.items():
                self.assertEqual(i18n.plural_form(code, n), form, f"{code} {n}")

    def test_matching_the_systems_language(self):
        cases = {
            ("pt-BR",): "pt-BR",
            ("pt_PT",): "pt-BR",
            ("de_AT.UTF-8",): "de",
            ("es-MX",): "es",
            ("zh-CN",): "zh-CN",
            ("zh-Hans-SG",): "zh-CN",
            ("zh-TW", "ja"): "ja",
            ("zh-Hant-HK",): "en",
            ("ko-KR", "fr-CA"): "fr",
            ("ko-KR",): "en",
            (): "en",
            ("C",): "en",
        }
        for tags, code in cases.items():
            self.assertEqual(i18n.match_language(tags), code, tags)

    def test_dates(self):
        day = dt.date(2026, 11, 4)
        self.assertEqual(i18n.weekday_day_month(day), "Wed 04 Nov")
        self.assertEqual(i18n.day_month(day), "04 Nov")


class CommandLine(unittest.TestCase):
    def test_it_stays_in_english_whatever_the_system_says(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(
                os.environ,
                LANG="de_DE.UTF-8",
                LANGUAGE="de",
                LC_ALL="de_DE.UTF-8",
                QUAK_CONFIG_DIR=tmp,
                QUAK_STATE_DIR=tmp,
                QUAK_SIM_STATE=str(Path(tmp) / "sim.json"),
            )
            p = subprocess.run(
                [sys.executable, "-m", "quak", "--sim", "info"], env=env, capture_output=True, text=True, timeout=60
            )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("battery", p.stdout)


if __name__ == "__main__":
    unittest.main()

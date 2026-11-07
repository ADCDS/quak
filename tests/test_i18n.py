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
import unicodedata
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
    "LGPL-3.0",
    "LGPL-2.1",
    "GPL-3.0",
    "Inter",
    "Space Grotesk",
    "JetBrains Mono",
    "Microsoft YaHei",
    "Noto Sans CJK SC",
    "Yu Gothic UI",
    "Noto Sans CJK JP",
    "THIRD_PARTY_NOTICES.md",
    "python -m quak.e2e",
    "Videos",  # the folder in the home directory, whose name the system chooses
    "LC_ALL",
    "LC_MESSAGES",
    "LANG",
    "SIM",  # internal: the simulated camera's address
    "IDLE",  # the camera's state names, which are compared rather than shown
    "CONFIG",
    "READY",
    "STREAMING",
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
    "exit",
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
                    form.format(**{**names, "n": 2})

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


class TranslatedWhenShown(unittest.TestCase):
    def test_nothing_is_translated_while_a_module_loads(self):
        """That would freeze one language in place for good. Constants use N_ and get translated with term()."""
        found = []
        for path in sync.source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or sync._called(node) not in ("tr", "trn", "term"):
                    continue
                parent, inside_a_function = parents.get(node), False
                while parent is not None:
                    if isinstance(parent, ast.Lambda) or (
                        isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and not any(
                            node is d or node in ast.walk(d)
                            for d in parent.args.defaults + parent.args.kw_defaults
                            if d
                        )
                    ):
                        inside_a_function = True
                        break
                    parent = parents.get(parent)
                if not inside_a_function:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [])


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

    def test_every_language_has_a_font_with_its_letters(self):
        from quak.gui import theme as th  # needs the GUI extra's flet only through the theme's own imports

        for code, head, body in (("en", th.HEAD_FONT, th.BODY_FONT), ("ru", th.BODY_FONT, th.BODY_FONT)):
            i18n.set_language(code)
            self.assertEqual((th.head_font(), th.body_font()), (head, body), code)
        for code in ("zh-CN", "ja"):
            i18n.set_language(code)
            self.assertEqual(th.head_font(), th.SYSTEM_FONTS[code])
            self.assertEqual(th.body_font(), th.SYSTEM_FONTS[code])

    def test_decimals(self):
        self.assertEqual(i18n.decimal("183.2"), "183.2")
        i18n.set_language("de")
        self.assertEqual(i18n.decimal("183.2"), "183,2")
        i18n.set_language("ja")
        self.assertEqual(i18n.decimal("183.2"), "183.2")

    def test_dates(self):
        day = dt.date(2026, 11, 4)
        self.assertEqual(i18n.weekday_day_month(day), "Wed 04 Nov")
        self.assertEqual(i18n.day_month(day), "04 Nov")


class TranslatedDocs(unittest.TestCase):
    PAGES = ("README.md", "install.md", "safety.md", "troubleshooting.md")

    def english(self, page: str) -> str:
        return (ROOT / ("README.md" if page == "README.md" else f"docs/{page}")).read_text(encoding="utf-8")

    def translated(self, code: str, page: str) -> Path:
        return ROOT / "docs" / "i18n" / code / page

    def test_every_language_has_every_page(self):
        for code in sync.languages():
            for page in self.PAGES:
                self.assertTrue(self.translated(code, page).exists(), f"{code}/{page}")

    def test_they_keep_the_structure_of_the_english(self):
        """A rough check that a translation hasn't fallen behind: the same headings, code and pictures."""

        def shape(text: str) -> tuple:
            headings = [len(h) for h in re.findall(r"^(#+) ", text, re.M)]
            code = re.findall(r"^```.*?^```", text, re.M | re.S)
            pictures = re.findall(r'<img src="([^"]+)"', text)
            return headings, code, pictures

        for code in sync.languages():
            for page in self.PAGES:
                got = shape(self.translated(code, page).read_text(encoding="utf-8"))
                self.assertEqual(got, shape(self.english(page)), f"{code}/{page}")

    def test_they_say_theyre_translations(self):
        for code in sync.languages():
            for page in self.PAGES:
                text = self.translated(code, page).read_text(encoding="utf-8")
                self.assertIn(
                    f"blob/main/{'README.md' if page == 'README.md' else 'docs/' + page}", text[:800], f"{code}/{page}"
                )

    def test_their_bold_renders(self):
        """Markdown ignores ** after punctuation unless a space or more punctuation follows: common in Japanese and Chinese."""

        def kind(char: str) -> str:
            return "Z" if not char or char.isspace() else unicodedata.category(char)[0]

        for code in sync.languages():
            for page in self.PAGES:
                text = re.sub(
                    r"^```.*?^```|`[^`\n]*`",
                    "x",
                    self.translated(code, page).read_text(encoding="utf-8"),
                    flags=re.M | re.S,
                )
                for m in re.finditer(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", text):
                    before = text[m.start() - 1] if m.start() else ""
                    after = text[m.end()] if m.end() < len(text) else ""
                    inner = m.group(1)
                    bad = (kind(inner[-1]) in "PS" and kind(after) not in "ZPS") or (
                        kind(inner[0]) in "PS" and kind(before) not in "ZPS"
                    )
                    self.assertFalse(bad, f"{code}/{page}: {m.group(0)[:50]}")

    def test_their_links_lead_somewhere(self):
        for code in sync.languages():
            for page in self.PAGES:
                path = self.translated(code, page)
                for target in re.findall(r"\]\((?!https?:|#)([^)#]+)", path.read_text(encoding="utf-8")):
                    self.assertTrue((path.parent / target).exists(), f"{code}/{page}: {target}")

    def test_the_english_readme_links_to_every_language(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for code in sync.languages():
            self.assertIn(f"docs/i18n/{code}/README.md", readme)
            self.assertIn(i18n.LANGUAGES[code], readme)


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

#!/usr/bin/env python3
"""Keep src/quak/locale/*.json in step with the code.

    python tools/i18n_sync.py           # add new text (empty, so it shows in English) and drop text that's gone
    python tools/i18n_sync.py --check   # only report what's out of step, and exit 1 if anything is

Text comes from the `tr`, `trn` and `N_` calls in src/quak, and from the camera's own words in core/tables.py.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "quak"
sys.path.insert(0, str(ROOT / "src"))

from quak import i18n  # noqa: E402
from quak.core import tables  # noqa: E402

SKIP = ("e2e", "cli", "transport/sim", "i18n.py")
PLURAL_FORMS = {"ru": ("one", "few", "many"), "ja": ("other",), "zh-CN": ("other",)}
DEFAULT_FORMS = ("one", "other")


def source_files() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if not any(p.relative_to(SRC).as_posix().startswith(s) for s in SKIP)]


def _called(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


def code_messages() -> tuple[dict[str, tuple[str, str] | None], list[str]]:
    """Every message in the code, as {text: None} or, for plurals, {one: (one, other)}, and any call that can't be read."""
    found: dict[str, tuple[str, str] | None] = {}
    problems = []
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or _called(node) not in ("tr", "trn", "N_") or not node.args:
                continue
            first = node.args[0]
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                problems.append(f"{where}: {_called(node)}() needs a literal string; use term() for a variable")
                continue
            if _called(node) == "trn":
                second = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(second, ast.Constant) and isinstance(second.value, str)):
                    problems.append(f"{where}: trn() needs two literal strings")
                    continue
                found[first.value] = (first.value, second.value)
            else:
                found.setdefault(first.value, None)
    return found, problems


def camera_terms() -> set[str]:
    """The camera's words that Quak shows: setting names and values, and preset titles. Numbers alone aren't words."""
    words = set(tables.PRESET_TITLES.values())
    for setting in tables.SETTINGS.values():
        words.add(setting["name"])
        words.update(setting.get("options", {}).values())
    return {w for w in words if re.search(r"[A-Za-z]{2}", w)}


def fixed_terms() -> set[str]:
    return set(i18n.WEEKDAYS) | set(i18n.MONTHS) | {"{day} {month}", "{weekday} {day} {month}"}


def wanted() -> dict[str, tuple[str, str] | None]:
    messages, _ = code_messages()
    for word in sorted(camera_terms() | fixed_terms()):
        messages.setdefault(word, None)
    return messages


def synced(code: str, have: dict, messages: dict) -> dict:
    forms = PLURAL_FORMS.get(code, DEFAULT_FORMS)
    out = {}
    for key in sorted(messages):
        old = have.get(key)
        if messages[key] is None:
            out[key] = old if isinstance(old, str) else ""
        else:
            old = old if isinstance(old, dict) else {}
            out[key] = {form: old.get(form, "") for form in forms}
    return out


def catalog_path(code: str) -> Path:
    return i18n.LOCALE_DIR / f"{code}.json"


def languages() -> list[str]:
    return [code for code in i18n.LANGUAGES if code != i18n.DEFAULT]


def main() -> int:
    check = "--check" in sys.argv
    messages = wanted()
    _, problems = code_messages()
    for problem in problems:
        print(problem)
    stale = 0
    i18n.LOCALE_DIR.mkdir(exist_ok=True)
    for code in languages():
        path = catalog_path(code)
        have = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        new = synced(code, have, messages)
        if new != have:
            stale += 1
            print(f"{code}: {len(set(new) - set(have))} new, {len(set(have) - set(new))} gone")
            if not check:
                path.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(messages)} messages, {len(languages())} languages")
    return 1 if problems or (check and stale) else 0


if __name__ == "__main__":
    sys.exit(main())

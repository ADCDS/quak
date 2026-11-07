"""The app's translations.

English is the source: `locale/<language>.json` maps the English text to its translation, and anything missing shows
in English. Only the app sets a language, so the command line and its logs stay in English, which scripts and bug
reports rely on. `tools/i18n_sync.py` keeps the catalogs in step with the code.

    tr("Delete {name} from the camera?", name=name)    # one sentence, with its variables named
    trn("{n} item", "{n} items", count)                # chooses the plural form the language needs
    term(setting_name)                                 # a word that isn't written in the code, such as a camera setting
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from pathlib import Path

DEFAULT = "en"
LANGUAGES = {  # each in its own language, because that's how people look for theirs
    "en": "English",
    "pt-BR": "Português (Brasil)",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "ru": "Русский",
    "zh-CN": "简体中文",
    "ja": "日本語",
}
DECIMAL_COMMA = ("pt-BR", "es", "fr", "de", "ru")
LOCALE_DIR = Path(__file__).parent / "locale"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_language = DEFAULT
_catalog: dict[str, str | dict[str, str]] = {}


def N_(text: str) -> str:
    """Marks text for translation that gets translated later, with `term`: a constant, for example."""
    return text


def language() -> str:
    return _language


def set_language(code: str) -> None:
    global _language, _catalog
    _language = code if code in LANGUAGES else DEFAULT
    _catalog = {}
    if _language != DEFAULT:
        try:
            _catalog = json.loads((LOCALE_DIR / f"{_language}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):  # a damaged catalog shouldn't stop the app: it just shows English
            _language = DEFAULT


def _fill(text: str, source: str, values: dict) -> str:
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):  # a mistake in a translation shouldn't crash a screen
        return source.format(**values)


def tr(text: str, **values) -> str:
    return _fill(_catalog.get(text) or text, text, values)


def term(text: str) -> str:
    """Translates text the code only has in a variable. Anything without a translation stays as it is."""
    return tr(text) if isinstance(text, str) else text


def plural_form(code: str, n: int) -> str:
    """The CLDR plural category of a whole number. English, German and Spanish only need `one` and `other`."""
    if code in ("ja", "zh-CN"):
        return "other"
    if code == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        return "few" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "many"
    if code in ("fr", "pt-BR"):
        return "one" if n in (0, 1) else "other"
    return "one" if n == 1 else "other"


def trn(one: str, other: str, n: int, **values) -> str:
    source = one if n == 1 else other
    forms = _catalog.get(one)
    text = source
    if isinstance(forms, dict):
        text = forms.get(plural_form(_language, n)) or forms.get("other") or source
    return _fill(text, source, {"n": n, **values})


def decimal(number: str) -> str:
    """183.2 is written 183,2 in most of these languages."""
    return number.replace(".", ",") if _language in DECIMAL_COMMA else number


def weekday(day: dt.date) -> str:
    return term(WEEKDAYS[day.weekday()])


def _day_number(day: dt.date) -> str:
    return f"{day.day:02d}" if _language == DEFAULT else str(day.day)  # the zero only suits the English design


def day_month(day: dt.date) -> str:
    """04 Nov, in the order and with the names the language uses."""
    return tr("{day} {month}", day=_day_number(day), month=term(MONTHS[day.month - 1]))


def weekday_day_month(day: dt.date) -> str:
    """Wed 04 Nov, likewise."""
    return tr("{weekday} {day} {month}", weekday=weekday(day), day=_day_number(day), month=term(MONTHS[day.month - 1]))


def match_language(tags: Iterable[str]) -> str:
    """The first of the system's preferred languages that Quak has, such as pt-PT or de_AT for pt-BR or de."""
    for tag in tags:
        parts = tag.replace("_", "-").split(".")[0].split("@")[0].split("-")
        base = parts[0].lower()
        region = {p.upper() for p in parts[1:]}
        if base == "zh":
            # Traditional Chinese readers aren't well served by Simplified: they get English until it's added
            if not region & {"TW", "HK", "MO", "HANT"}:
                return "zh-CN"
            continue
        for code in LANGUAGES:
            if code.split("-")[0].lower() == base:
                return code
    return DEFAULT

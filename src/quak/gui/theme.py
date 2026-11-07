"""Design tokens from the approved Claude Design canvas (docs/design.md, "Tokens + components")."""

from __future__ import annotations

from dataclasses import dataclass

from quak import i18n


@dataclass(frozen=True)
class Tokens:
    ground: str
    surface: str
    surface2: str
    line: str
    text: str
    text2: str
    text3: str
    muted: str
    accent: str
    on_accent: str
    accent_tint: str
    accent_ink: str
    ok: str
    ok_tint: str
    rec: str
    warn: str
    warn_tint: str
    warn_text: str
    danger_tint: str
    danger_text: str
    danger_line: str
    nav: str
    # derived roles (the light artboard: white cards with a hairline, white pills, amber-ink selection)
    pill_bg: str = "#0B0D10"
    card_border: str | None = None
    log_bg: str = "#101317"
    soft_text: str = "#D5DAE1"
    danger_body: str = "#E8C9CA"
    ring2: str = "#3A404A"
    armed_time: str = "#FFE7A3"
    nav_selected: str = "#FFC531"
    dark: bool = True


DARK = Tokens(
    ground="#0B0D10",
    surface="#15181D",
    surface2="#1E2228",
    line="#2A2F37",
    text="#F2F4F7",
    text2="#A7AFBA",
    text3="#8A93A0",
    muted="#5C6470",
    accent="#FFC531",
    on_accent="#1A1300",
    accent_tint="#2A2310",
    accent_ink="#FFD86B",
    ok="#3DDC97",
    ok_tint="#123526",
    rec="#FF4D4F",
    warn="#FF9F43",
    warn_tint="#2B1C0E",
    warn_text="#FFC38A",
    danger_tint="#2A1416",
    danger_text="#FF8A8C",
    danger_line="#5A2A2C",
    nav="#0F1114",
)

LIGHT = Tokens(
    ground="#F6F5F2",
    surface="#FFFFFF",
    surface2="#EEECE7",
    line="#DDD9D1",
    text="#121418",
    text2="#4A515C",
    text3="#5E6672",
    muted="#9AA0A8",
    accent="#FFC531",
    on_accent="#1A1300",
    accent_tint="#FFF1C7",
    accent_ink="#8A5B00",
    ok="#167A50",
    ok_tint="#DDF3E8",
    rec="#D92D2F",
    warn="#B85C00",
    warn_tint="#FDEBD8",
    warn_text="#8A4500",
    danger_tint="#FBE3E3",
    danger_text="#B42326",
    danger_line="#E7B5B6",
    nav="#FFFFFF",
    pill_bg="#FFFFFF",
    card_border="#DDD9D1",
    log_bg="#FFFFFF",
    soft_text="#4A515C",
    danger_body="#7A2E30",
    ring2="#CFCAC0",
    armed_time="#8A5B00",
    nav_selected="#8A5B00",
    dark=False,
)

# fonts (bundled under quak/gui/assets/fonts, SIL Open Font License)
BODY_FONT, HEAD_FONT, MONO_FONT = "Inter", "Space Grotesk", "JetBrains Mono"
# Space Grotesk has Latin letters only. Inter has Cyrillic, and Chinese and Japanese fall back to the system's fonts
HEAD_FONT_LANGUAGES = ("en", "pt-BR", "es", "fr", "de")


def head_font() -> str:
    return HEAD_FONT if i18n.language() in HEAD_FONT_LANGUAGES else BODY_FONT


FONTS = {BODY_FONT: "fonts/Inter.ttf", HEAD_FONT: "fonts/SpaceGrotesk.ttf", MONO_FONT: "fonts/JetBrainsMono.ttf"}

# type scale (px) and radii
DISPLAY, TITLE, BODY, SECONDARY, LABEL = 28, 20, 15, 13, 12
R_CHIP, R_BUTTON, R_CARD, R_SECTION = 10, 14, 16, 20
WIDE = 700  # side rail at or above this width

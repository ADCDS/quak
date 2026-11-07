#!/usr/bin/env python3
"""Generate every logo and icon file from the one drawing below. Needs rsvg-convert and ImageMagick.

    python tools/make_icons.py

What `flet build` does with the files it writes to src/quak/gui/assets:
- icon.png: a transparent duck. Flet frames it for each platform and puts `icon_background`
  (app/pyproject.toml) behind it. It becomes Android's adaptive icon and the splash screen.
- icon_windows.png, icon_linux.png, icon_web.png: finished tiles, used as they are.
"""

import pathlib
import subprocess

ASSETS = pathlib.Path(__file__).resolve().parent.parent / "src" / "quak" / "gui" / "assets"

INK, AMBER, SHADE, BILL = "#0B0D10", "#FFC531", "#E9A81B", "#FF8A1E"
BODY_PATH = (
    "M16 50 C20 60 28 66 40 66 C50 66 58 63 64 57 C59 49 57 40 59 32 C62 18 75 12 87 15 C100 18 107 30 105 43 "
    "C104 50 100 56 96 60 C105 68 110 79 106 91 C101 105 88 112 72 112 L52 112 C32 112 18 100 15 80 "
    "C14 69 14 59 16 50 Z"
)
WING_PATH = "M38 80 C50 71 69 73 79 85 C69 98 48 98 38 80 Z"
BILL_PATH = "M102 29 C111 28 121 32 123 38 C123.5 40 122.5 41.5 121 42.5 C114 46 106 47 100 46 C102 41 103 35 102 29 Z"
LENS = (
    f'<circle cx="84" cy="33" r="8.5" fill="{INK}"/>'
    '<circle cx="84" cy="33" r="4.9" fill="none" stroke="#5B6B7A" stroke-width="1.4"/>'
    '<circle cx="81.3" cy="30.3" r="1.7" fill="#FFFFFF"/>'
)
DUCK = (
    f'<path d="{BODY_PATH}" fill="{AMBER}"/><path d="{WING_PATH}" fill="{SHADE}"/>'
    f'<path d="{BILL_PATH}" fill="{BILL}"/>{LENS}'
)


def svg(content: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128" role="img" '
        f'aria-label="Quak logo: a yellow duck with a camera lens for an eye">{content}</svg>\n'
    )


def silhouette(colour: str, eye: str) -> str:
    return (
        f'<g transform="translate(9 7) scale(.86)"><path d="{BODY_PATH}" fill="{colour}"/>'
        f'<path d="{BILL_PATH}" fill="{colour}"/><circle cx="84" cy="33" r="7" fill="{eye}"/></g>'
    )


TILE = f'<rect width="128" height="128" rx="34" fill="{INK}"/><g transform="translate(9 7) scale(.86)">{DUCK}</g>'
# the duck alone, centred on its own square so Flet's framing keeps it optically centred
ALONE = f'<g transform="translate(-4.75 1)">{DUCK}</g>'

SOURCES = {
    "logo.svg": svg(TILE),
    "logo-duck.svg": svg(ALONE),
    "logo-mono-black.svg": svg(silhouette("#000000", "#FFFFFF")),
    "logo-mono-white.svg": svg(silhouette("#FFFFFF", "#000000")),
}
PNGS = {  # output: (source, pixels)
    "logo.png": ("logo.svg", 256),
    "favicon.png": ("logo.svg", 64),
    "icon.png": ("logo-duck.svg", 1024),
    "icon_windows.png": ("logo.svg", 1024),
    "icon_linux.png": ("logo.svg", 1024),
    "icon_web.png": ("logo.svg", 1024),
}


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, text in SOURCES.items():
        (ASSETS / name).write_text(text)
    for name, (source, pixels) in PNGS.items():
        subprocess.run(
            ["rsvg-convert", "-w", str(pixels), "-h", str(pixels), str(ASSETS / source), "-o", str(ASSETS / name)],
            check=True,
        )
    # the window icon of an unpackaged run on Windows, which only takes .ico
    subprocess.run(
        [
            "magick",
            str(ASSETS / "icon_windows.png"),
            "-define",
            "icon:auto-resize=256,64,48,32,16",
            str(ASSETS / "icon.ico"),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()

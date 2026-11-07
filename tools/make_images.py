#!/usr/bin/env python3
"""Make the pictures in docs/images, from the simulator. Never from a real camera: its screens show the serial and
nearby network names. Needs kwin_wayland (see tools/screenshots.sh), Chrome, rsvg-convert and ImageMagick.

    python tools/make_images.py
"""

import base64
import html
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
QUAK = ROOT / ".venv" / "bin" / "quak"
INK, TEXT, MUTED, AMBER = "#0B0D10", "#F2F4F7", "#A7AFBA", "#FFC531"


def data_url(path: pathlib.Path) -> str:
    kind = "svg+xml" if path.suffix == ".svg" else "png"
    return f"data:image/{kind};base64,{base64.b64encode(path.read_bytes()).decode()}"


def chrome_shot(page: str, width: int, height: int, out: pathlib.Path, work: pathlib.Path) -> None:
    fonts = ROOT / "src" / "quak" / "gui" / "assets" / "fonts"
    source = work / "page.html"
    source.write_text(f'''<!doctype html><meta charset="utf-8"><style>
      @font-face {{ font-family: Inter; src: url("{(fonts / "Inter.ttf").as_uri()}"); font-weight: 100 900 }}
      @font-face {{ font-family: "Space Grotesk"; src: url("{(fonts / "SpaceGrotesk.ttf").as_uri()}"); font-weight: 300 700 }}
      @font-face {{ font-family: "JetBrains Mono"; src: url("{(fonts / "JetBrainsMono.ttf").as_uri()}"); font-weight: 100 800 }}
      body {{ margin: 0; width: {width}px; height: {height}px; overflow: hidden; background: {INK}; font-family: Inter }}
      </style>{page}''')
    # the window size includes Chrome's own toolbar even when headless: ask for more and crop to what was meant
    subprocess.run(
        [
            "google-chrome",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--hide-scrollbars",
            f"--user-data-dir={work / 'chrome'}",
            f"--window-size={width},{height + 200}",
            f"--screenshot={out}",
            source.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(["convert", str(out), "-crop", f"{width}x{height}+0+0", "+repage", str(out)], check=True)


def phone(src: pathlib.Path, width: int = 330) -> str:
    return (
        f'<img src="{data_url(src)}" style="width:{width}px;display:block;border-radius:30px;border:6px solid #2A2F37">'
    )


def tour(name: str, size: str, work: pathlib.Path, picture: pathlib.Path, theme: str = "") -> pathlib.Path:
    env = dict(
        os.environ, QUAK_LANG="en", QUAK_TOUR_OUT=str(work), QUAK_SIM_PICTURE=str(picture), QUAK_TOUR_THEME=theme
    )
    subprocess.run([str(ROOT / "tools" / "screenshots.sh"), name, size], env=env, check=True)
    return work / name / "shots"


def terminal(work: pathlib.Path) -> None:
    """A short recording of the command line against the simulated camera, with its real output."""
    env = dict(
        os.environ,
        QUAK_SIM="1",
        QUAK_CONFIG_DIR=str(work / "cli"),
        QUAK_STATE_DIR=str(work / "cli"),
        QUAK_SIM_STATE=str(work / "cli" / "sim.json"),
    )
    commands = ["quak --sim info", "quak --sim schedule arm +2m --duration 15s", "quak --sim schedule show"]
    screen, frames = "", []

    def frame(text: str, hold: int) -> None:
        body = html.escape(text).replace("$ ", f'<span style="color:{AMBER}">$ </span>')
        path = work / f"term{len(frames):03}.png"
        chrome_shot(
            f'<pre style="margin:0;padding:22px 26px;color:{TEXT};font:17px/1.5 JetBrains Mono,monospace;white-space:pre-wrap">{body}'
            "</pre>",
            960,
            400,
            path,
            work,
        )
        frames.append((path, hold))

    frame("$ ", 60)
    for command in commands:
        args = [str(QUAK), *command.split()[1:]]
        output = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60).stdout
        for stop in range(0, len(command) + 1, 6):
            frame(screen + "$ " + command[:stop], 8)
        frame(screen + "$ " + command, 25)
        screen += "$ " + command + "\n" + output
        frame(screen + "$ ", 220)
    subprocess.run(
        ["convert", "-loop", "0"]
        + [a for path, hold in frames for a in ("-delay", str(hold), str(path))]
        + ["-layers", "Optimize", "-colors", "32", str(OUT / "terminal.gif")],
        check=True,
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quak-images-") as tmp:
        work = pathlib.Path(tmp)
        picture = work / "scene.png"
        subprocess.run(["rsvg-convert", str(ROOT / "tools" / "demo-scene.svg"), "-o", str(picture)], check=True)
        desktop = tour("desktop", "1280x800", work, picture)
        mobile = tour("phone", "412x915", work, picture)
        light = tour("light", "412x915", work, picture, theme="light")
        for name, source in {
            "phone-cameras": mobile / "cameras.png",
            "phone-remote": mobile / "remote-preview.png",
            "phone-schedule": mobile / "schedule-armed.png",
            "phone-add-camera": mobile / "add-instructions.png",
            "phone-settings": mobile / "settings.png",
            "phone-media": mobile / "media.png",
            "phone-light": light / "cameras.png",
            "desktop-remote": desktop / "remote-preview.png",
            "desktop-media": desktop / "media.png",
        }.items():
            (OUT / f"{name}.png").write_bytes(source.read_bytes())
        logo = data_url(ROOT / "src" / "quak" / "gui" / "assets" / "logo.svg")
        chrome_shot(
            f"""<div style="position:relative;width:1560px;height:900px;background:{INK}">
          <img src="{data_url(desktop / "remote-preview.png")}" style="position:absolute;left:40px;top:50px;width:1200px;
            border-radius:14px;border:1px solid #2A2F37">
          <div style="position:absolute;left:1180px;top:60px">{phone(mobile / "remote-preview.png", 340)}</div></div>""",
            1560,
            900,
            OUT / "hero.png",
            work,
        )
        chrome_shot(
            f"""<div style="position:relative;width:1280px;height:640px;background:{INK};overflow:hidden">
          <img src="{logo}" style="position:absolute;left:70px;top:150px;width:150px">
          <div style="position:absolute;left:70px;top:320px;color:{TEXT};font-family:'Space Grotesk';font-size:92px;font-weight:600;letter-spacing:-2px">Quak</div>
          <div style="position:absolute;left:70px;top:445px;width:560px;color:{MUTED};font-size:32px;line-height:1.3">An open-source remote<br>
            for GoPro HERO11 cameras</div>
          <div style="position:absolute;left:800px;top:50px">{phone(mobile / "remote-preview.png", 340)}</div></div>""",
            1280,
            640,
            OUT / "social-preview.png",
            work,
        )
        terminal(work)


if __name__ == "__main__":
    sys.exit(main())

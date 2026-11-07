# Quak's design

The screens were designed in Claude Design and then built in Flet. The tokens below are the source of `src/quak/gui/theme.py`.

Phones get a bottom bar: Cameras, Remote, Media, Live, More. At 700 pixels wide and above, a side rail replaces it, and Remote and
Media get a layout of their own.

## Rules the design follows

- **Own identity.** The name, logo (a yellow duck with a camera lens for an eye) and amber accent are Quak's. No GoPro or Quik logos, icons, colors or fonts. The camera's own menu labels (for example "Connect Device › GoPro Quik App") are quoted only where the user has to find them on the camera.
- **What the camera actually does.**
  - Pro-only settings are greyed in Easy controls.
  - Scheduled capture always shows the camera clock and a duration.
  - "No limit" is hidden behind a warning.
  - Auto Power Off = Never gets a warning.
- **Passwords:** network passwords for livestream go to the camera only.
- **Accessibility:** touch targets ≥ 44 px, text contrast ≥ 4.5:1, real buttons, links and inputs, icon-only buttons labelled.

## Tokens

| Token | Dark | Light |
|---|---|---|
| ground | `#0B0D10` | `#F6F5F2` |
| surface | `#15181D` | `#FFFFFF` |
| surface-2 | `#1E2228` | `#EEECE7` |
| line | `#2A2F37` | `#DDD9D1` |
| text | `#F2F4F7` | `#121418` |
| text-2 | `#A7AFBA` | `#4A515C` |
| text-3 | `#8A93A0` | `#5E6672` |
| accent (fill) | `#FFC531` | `#FFC531` |
| on-accent | `#1A1300` | `#1A1300` |
| accent-tint | `#2A2310` | `#FFF1C7` |
| accent-ink (text/links) | `#FFD86B` | `#8A5B00` |
| ok | `#3DDC97` | `#167A50` |
| rec / live | `#FF4D4F` | `#D92D2F` |
| warn | `#FF9F43` | (use accent-ink) |
| danger-tint / danger-text | `#2A1416` / `#FF8A8C` | |

- **Type:**
  - Space Grotesk 600 for display (28) and titles (20);
  - Inter for body (15), secondary (13) and section labels (12, uppercase, +0.08 em);
  - JetBrains Mono 12 for bytes in Diagnostics.
- **Radius:** 10 (chips inside rows), 12–14 (buttons, cards), 16–20 (sections), pill (chips, status).
- **Spacing:** 4 · 8 · 12 · 16 · 24.
- **Components:**
  - buttons: primary, secondary, destructive, go live;
  - chips: default, selected, dashed "No limit…";
  - status pills: Bluetooth, Wi-Fi, ARMED, REC, test results;
  - shutter: idle, recording, busy;
  - setting rows: normal, disabled with reason, warning;
  - toast.

## Fonts

The fonts are bundled (`src/quak/gui/assets/fonts`, SIL OFL), so every platform renders Inter, Space Grotesk and
JetBrains Mono as designed. Space Grotesk only has Latin letters, so Russian titles use Inter, and Chinese and Japanese use the system's
fonts, because bundling those would add many megabytes. A button's text style has to name its font, because Flutter replaces a button's whole
style rather than merging it with the theme.

## Checking a change

`tools/screenshots.sh desktop 1280x800` and `tools/screenshots.sh phone 412x915` walk every screen on the simulator
and save a picture of each.

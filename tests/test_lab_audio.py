"""Sound on the camera: no hardware, no ssh, no real sound card.

Most of this looks at the commands sent and feeds canned output back. That says nothing about whether the
scripts work, so ``RealShellRuns`` also runs them in a local shell, over temporary directories, with a stub
where tinycap gets installed. Whether the camera's own BusyBox and tinycap agree is still for a real camera to show.
"""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quak.cli.main import build_parser, main
from quak.core.errors import LabError, NetworkError, ToolkitError
from quak.lab.capabilities import audio
from quak.lab.capabilities.audio import Recording
from quak.lab.capabilities.storage import CopyResult
from quak.lab.models import AudioDevice
from quak.lab.service import LabService
from quak.ssh.models import ExecResult

ADDRESS = "AA:BB:CC:DD:EE:FF"
SHA_A = "a" * 64
SHA_B = "b" * 64
TINYCAP = "/tmp/fuse_d/quak/toolkit/packs/core/tools/tinycap"  # spelled out, so moving it fails a test

# Same check as test_lab_hostmode: substrings on purpose, and only dd is short enough to need a word boundary.
FORBIDDEN = re.compile(r"mkfs|mke2fs|format|flash|update|\bdd\b|fdisk|parted|wipefs", re.IGNORECASE)

# The cards file is as a real fifine mic showed it (my-gopro raw/31). The stream file is not a real capture: raw/30
# only noted this mic's values down, so the layout around them is how snd-usb-audio is known to print one.
MIC_CARD = """\
 0 [Microphone     ]: USB-Audio - fifine Microphone
                      fifine Microphone at usb-xhci-hcd.0.auto-1.4, full speed
"""
MIC_STREAM = """\
fifine Microphone at usb-xhci-hcd.0.auto-1.4, full speed : USB Audio

Capture:
  Status: Stop
  Interface 2
    Altset 1
    Format: S16_LE
    Channels: 2
    Endpoint: 2 IN (ASYNC)
    Rates: 48000, 44100
"""
MIC = AudioDevice(card=0, device=0, name="fifine Microphone", capture_format="S16_LE", rates=[48000, 44100], channels=2)

# Invented, in the same shape.
HEADSET_CARD = """\
 1 [Headset        ]: USB-Audio - Some Headset
                      Some Headset at usb-xhci-hcd.0.auto-1.2, high speed
"""
HEADSET_STREAM = """\
Some Headset at usb-xhci-hcd.0.auto-1.2, high speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: S24_3LE
    Channels: 6
    Endpoint: 1 OUT (ADAPTIVE)
    Rates: 96000
    Data packet interval: 1000 us

Capture:
  Status: Running
    Interface = 2
    Altset = 1
    Packet Size = 200
    Momentary freq = 16000 Hz (0x10.0000)
  Interface 2
    Altset 1
    Format: S16_LE
    Channels: 1
    Endpoint: 2 IN (ASYNC)
    Rates: 8000 - 48000 (continuous)
    Data packet interval: 1000 us
  Interface 2
    Altset 2
    Format: S24_3LE
    Channels: 2
    Endpoint: 2 IN (ASYNC)
    Rates: 48000
    Data packet interval: 1000 us
  Interface 2
    Altset 3
    Format: S24_3LE
    Channels: 2
    Endpoint: 3 IN (ASYNC)
    Rates: 48000
    Data packet interval: 1000 us
"""
SPEAKER_CARD = """\
 2 [Speaker        ]: USB-Audio - Some - Speaker
                      Some - Speaker at usb-xhci-hcd.0.auto-1.3, full speed
"""
SPEAKER_STREAM = """\
Some - Speaker at usb-xhci-hcd.0.auto-1.3, full speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: S16_LE
    Channels: 2
    Endpoint: 1 OUT (ADAPTIVE)
    Rates: 48000
"""

# Each is still a .wav directly in /tmp. RealShellRuns really runs these on this computer, so what a path quoted
# wrong would run is a touch, which leaves a file to find, and nothing that could do harm.
NASTY_CAPTURES = (
    "/tmp/a b.wav",
    "/tmp/it's.wav",
    "/tmp/$(touch ran).wav",
    "/tmp/`touch ran`.wav",
    "/tmp/x; touch ran #.wav",
    '/tmp/q"q.wav',
    "/tmp/new\nline.wav",
    "/tmp/*.wav",
)
BAD_CAPTURES = (
    "/tmp/fuse_d/take.wav",  # the one that matters: tinycap reports success and leaves an empty WAV
    "/tmp/fuse_d/quak/take.wav",
    "/tmp/fuse_d",
    "/tmp/quak/mnt/sda1/take.wav",
    "/tmp/sub/take.wav",
    "/etc/passwd",
    "/etc/take.wav",
    "/tmp/passwd",
    "/tmp/take.wav/../../etc/passwd",
    "/tmp/../etc/take.wav",
    "//tmp/take.wav",
    "take.wav",
    "-rf",
    "",
    "/tmp/a\0b.wav",
)
BAD_DESTINATIONS = (
    "/",
    "/etc/take.wav",
    "/tmp",
    "/tmp/take.wav",
    "/tmp/fuse_dx/take.wav",
    "/tmp/fuse_d/../take.wav",
    "/tmp/fuse_d/../../etc/take.wav",
    "/tmp/quak/mnt/sda1/take.wav",
    "//tmp/fuse_d/take.wav",
    "take.wav",
    "-rf",
    "",
    "/tmp/fuse_d/a\0b.wav",
)


def answer(text: str | bytes = "", exit_code: int = 0, stderr: bytes = b"") -> ExecResult:
    stdout = text.encode() if isinstance(text, str) else text
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def listing(cards: str, streams: dict[int, str] | None = None) -> ExecResult:
    """What the list script prints."""
    framed = "".join(f"\0/proc/asound/card{card}/stream0\0{text}" for card, text in (streams or {}).items())
    return answer(cards + framed)


class FakeRemote:
    """A ``Remote`` with canned answers, in order. An exception as the answer is raised. A command too many fails."""

    def __init__(self, *replies: ExecResult | Exception) -> None:
        self.replies = list(replies)
        self.commands: list[str] = []
        self.timeouts: list[float] = []

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        self.timeouts.append(timeout)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"audio runs commands, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"audio runs commands, but wrote {path}")


def capturing(size: int = 1933356, *, stderr: bytes = b"") -> FakeRemote:
    """A camera with tinycap installed, whose capture comes out at ``size`` bytes."""
    return FakeRemote(answer(), answer(f"{size}\n", stderr=stderr))


def tinycap_argv(command: str) -> list[str]:
    """What a shell would run tinycap with, in the command that captures."""
    return shlex.split(command.split(" >/dev/null")[0])


def assignment(command: str, name: str) -> str:
    """What a shell would assign to ``name`` in the script's first lines."""
    prefix = f"{name}="
    return next(token.removeprefix(prefix) for token in shlex.split(command) if token.startswith(prefix))


class ListCards(unittest.TestCase):
    def cards(self, cards: str, streams: dict[int, str] | None = None) -> list[AudioDevice]:
        return audio.list_cards(FakeRemote(listing(cards, streams)))

    def test_a_real_microphone(self):
        self.assertEqual(self.cards(MIC_CARD, {0: MIC_STREAM}), [MIC])

    def test_each_way_to_capture_gets_an_entry(self):
        devices = self.cards(HEADSET_CARD, {1: HEADSET_STREAM})
        self.assertEqual([(d.capture_format, d.channels) for d in devices], [("S16_LE", 1), ("S24_3LE", 2)])

    def test_the_same_capture_twice_is_one_entry(self):
        devices = self.cards(HEADSET_CARD, {1: HEADSET_STREAM})
        self.assertEqual(sum(d.capture_format == "S24_3LE" for d in devices), 1)

    def test_playback_is_not_mistaken_for_capture(self):
        for device in self.cards(HEADSET_CARD, {1: HEADSET_STREAM}):
            self.assertNotEqual(device.channels, 6)
            self.assertNotIn(96000, device.rates)

    def test_a_running_stream_is_read_the_same(self):
        self.assertNotIn(16000, self.cards(HEADSET_CARD, {1: HEADSET_STREAM})[1].rates)

    def test_a_continuous_range_lists_the_usual_rates_inside_it(self):
        self.assertEqual(
            self.cards(HEADSET_CARD, {1: HEADSET_STREAM})[0].rates, [8000, 11025, 16000, 22050, 32000, 44100, 48000]
        )

    def test_a_card_that_cannot_capture_is_still_listed(self):
        self.assertEqual(self.cards(SPEAKER_CARD, {2: SPEAKER_STREAM}), [AudioDevice(card=2, name="Some - Speaker")])

    def test_a_card_without_a_stream_file_is_still_listed(self):
        self.assertEqual(self.cards(MIC_CARD), [AudioDevice(card=0, name="fifine Microphone")])

    def test_each_stream_goes_to_its_own_card(self):
        devices = self.cards(MIC_CARD + HEADSET_CARD, {1: HEADSET_STREAM, 0: MIC_STREAM})
        self.assertEqual(devices[0], MIC)
        self.assertEqual({d.card for d in devices[1:]}, {1})
        self.assertEqual({d.name for d in devices[1:]}, {"Some Headset"})

    def test_no_sound_cards(self):
        for cards in ("", "--- no soundcards ---\n"):
            with self.subTest(cards):
                self.assertEqual(self.cards(cards), [])

    def test_a_device_cannot_put_control_characters_on_a_terminal(self):
        devices = self.cards(" 0 [Evil           ]: USB-Audio - E\x1b[31mvil\x07\n")
        self.assertEqual(devices, [AudioDevice(card=0, name="E[31mvil")])

    def test_a_failure_says_what_failed(self):
        with self.assertRaisesRegex(LabError, "list the camera's sound cards: nope"):
            audio.list_cards(FakeRemote(answer(exit_code=1, stderr=b"nope")))

    def test_a_lost_link_stays_a_network_error(self):
        with self.assertRaises(NetworkError):
            audio.list_cards(FakeRemote(NetworkError("gone")))


class Record(unittest.TestCase):
    def test_tinycap_argv(self):
        remote = capturing()
        audio.record(remote, 1, 10)
        self.assertEqual(
            tinycap_argv(remote.commands[1]),
            [TINYCAP, "/tmp/quak-audio.wav", "-D", "1", "-d", "0", "-c", "2", "-r", "48000", "-b", "16", "-t", "10"],
        )

    def test_returns_the_path_and_size(self):
        self.assertEqual(audio.record(capturing(1933356), 0, 10), Recording("/tmp/quak-audio.wav", 1933356))
        self.assertEqual(audio.record(capturing(100), 0, 1, "/tmp/take.wav"), Recording("/tmp/take.wav", 100))

    def test_checks_for_the_tool_first(self):
        remote = capturing()
        audio.record(remote, 0, 1)
        self.assertEqual(shlex.split(remote.commands[0]), ["[", "-x", TINYCAP, "]"])

    def test_a_missing_tool_raises_and_captures_nothing(self):
        remote = FakeRemote(answer(exit_code=1))
        with self.assertRaisesRegex(ToolkitError, "core toolkit"):
            audio.record(remote, 0, 10)
        self.assertEqual(len(remote.commands), 1)

    def test_duration_is_capped(self):
        for asked, sent in ((1, "1"), (299, "299"), (300, "300"), (301, "300"), (100000, "300")):
            with self.subTest(asked):
                remote = capturing()
                audio.record(remote, 0, asked)
                self.assertEqual(tinycap_argv(remote.commands[1])[-2:], ["-t", sent])

    def test_the_link_outlasts_the_capture(self):
        remote = capturing()
        audio.record(remote, 0, 100000)
        self.assertGreater(remote.timeouts[1], audio.MAX_SECONDS)
        self.assertLess(remote.timeouts[1], audio.MAX_SECONDS + 60)

    def test_a_length_that_is_no_recording_sends_nothing(self):
        for seconds in (0, -1, 0.5):
            with self.subTest(seconds):
                remote = FakeRemote()
                with self.assertRaises(LabError):
                    audio.record(remote, 0, seconds)
                self.assertEqual(remote.commands, [])

    def test_a_negative_card_sends_nothing(self):
        remote = FakeRemote()
        with self.assertRaises(LabError):
            audio.record(remote, -1, 10)
        self.assertEqual(remote.commands, [])

    def test_a_card_that_is_not_a_number_can_not_reach_a_shell(self):
        remote = FakeRemote()
        with self.assertRaises(TypeError):
            audio.record(remote, "0; reboot", 10)
        self.assertEqual(remote.commands, [])

    def test_only_a_wav_directly_in_tmp_is_allowed(self):
        for path in BAD_CAPTURES:
            with self.subTest(path):
                remote = FakeRemote()
                with self.assertRaises(LabError):
                    audio.record(remote, 0, 10, path)
                self.assertEqual(remote.commands, [])

    def test_a_path_is_only_ever_data(self):
        for path in NASTY_CAPTURES:
            with self.subTest(path):
                remote = capturing()
                self.assertEqual(audio.record(remote, 0, 1, path).path, path)
                self.assertEqual(tinycap_argv(remote.commands[1])[1], path)

    def test_a_header_with_no_audio_is_a_failure_even_though_tinycap_exited_0(self):
        for size in (0, 44):
            with self.subTest(size):
                with self.assertRaisesRegex(LabError, "wrote no audio"):
                    audio.record(capturing(size), 0, 10)
        self.assertEqual(audio.record(capturing(45), 0, 10).size, 45)

    def test_an_empty_capture_says_what_tinycap_said(self):
        with self.assertRaisesRegex(LabError, r"wrote no audio \(Unable to open PCM device\)"):
            audio.record(capturing(44, stderr=b"Unable to open PCM device\n"), 0, 10)

    def test_a_size_that_can_not_be_read_is_a_failure(self):
        for text in ("", "total\n", "-1\n"):
            with self.subTest(text):
                with self.assertRaisesRegex(LabError, "wrote no audio"):
                    audio.record(FakeRemote(answer(), answer(text)), 0, 10)

    def test_tinycap_failing_says_so(self):
        with self.assertRaisesRegex(LabError, "sound card 3: no device"):
            audio.record(FakeRemote(answer(), answer(exit_code=1, stderr=b"no device")), 3, 10)

    def test_a_lost_link_stays_a_network_error(self):
        with self.assertRaises(NetworkError):
            audio.record(FakeRemote(answer(), NetworkError("gone")), 0, 10)


class CopyToSd(unittest.TestCase):
    def remote(self, *, sha: str = SHA_A, other: str | None = None, into: bool = False) -> FakeRemote:
        head = "into=1\n" if into else ""
        return FakeRemote(answer(f"{head}source={sha}\ncopy={other or sha}\n"))

    def test_copies_with_cp_and_names_both_ends(self):
        remote = self.remote()
        audio.copy_to_sd(remote, "/tmp/quak-audio.wav", "/tmp/fuse_d/take.wav")
        self.assertIn('cp "$src" "$part"', remote.commands[0])
        self.assertEqual(assignment(remote.commands[0], "src"), "/tmp/quak-audio.wav")
        self.assertEqual(assignment(remote.commands[0], "dst"), "/tmp/fuse_d/take.wav")

    def test_checks_that_the_card_is_mounted(self):
        remote = self.remote()
        audio.copy_to_sd(remote, "/tmp/quak-audio.wav", "/tmp/fuse_d/take.wav")
        self.assertEqual(assignment(remote.commands[0], "vol"), "/tmp/fuse_d")

    def test_returns_where_it_went_and_its_checksum(self):
        result = audio.copy_to_sd(self.remote(), "/tmp/quak-audio.wav", "/tmp/fuse_d/take.wav")
        self.assertEqual(result, CopyResult("/tmp/fuse_d/take.wav", SHA_A))

    def test_copying_into_a_folder_keeps_the_name(self):
        result = audio.copy_to_sd(self.remote(into=True), "/tmp/quak-audio.wav", "/tmp/fuse_d/MISC")
        self.assertEqual(result, CopyResult("/tmp/fuse_d/MISC/quak-audio.wav", SHA_A))

    def test_a_dest_is_normalised_before_it_is_checked(self):
        remote = self.remote()
        audio.copy_to_sd(remote, "/tmp/quak-audio.wav", "/tmp/fuse_d/a/../take.wav")
        self.assertEqual(assignment(remote.commands[0], "dst"), "/tmp/fuse_d/take.wav")

    def test_only_the_sd_card_is_a_destination(self):
        for dest in BAD_DESTINATIONS:
            with self.subTest(dest):
                remote = FakeRemote()
                with self.assertRaises(LabError):
                    audio.copy_to_sd(remote, "/tmp/quak-audio.wav", dest)
                self.assertEqual(remote.commands, [])

    def test_a_source_has_to_be_an_absolute_path(self):
        for src in ("quak-audio.wav", "-rf", "", "/tmp/a\0b.wav"):
            with self.subTest(src):
                remote = FakeRemote()
                with self.assertRaises(LabError):
                    audio.copy_to_sd(remote, src, "/tmp/fuse_d/take.wav")
                self.assertEqual(remote.commands, [])

    def test_a_path_is_only_ever_data(self):
        for path in NASTY_CAPTURES:
            dest = path.replace("/tmp/", "/tmp/fuse_d/")
            with self.subTest(path):
                remote = self.remote()
                audio.copy_to_sd(remote, path, dest)
                self.assertEqual(assignment(remote.commands[0], "src"), path)
                self.assertEqual(assignment(remote.commands[0], "dst"), dest)

    def test_an_unverified_copy_is_a_failure(self):
        for remote in (self.remote(other=SHA_B), self.remote(sha="short"), FakeRemote(answer())):
            with self.assertRaisesRegex(LabError, "could not be verified"):
                audio.copy_to_sd(remote, "/tmp/quak-audio.wav", "/tmp/fuse_d/take.wav")

    def test_a_failure_says_what_failed(self):
        remote = FakeRemote(answer(exit_code=1, stderr=b"/tmp/fuse_d/take.wav already exists"))
        with self.assertRaisesRegex(LabError, "already exists"):
            audio.copy_to_sd(remote, "/tmp/quak-audio.wav", "/tmp/fuse_d/take.wav")


class NothingDestructive(unittest.TestCase):
    def test_nothing_sent_contains_a_forbidden_command(self):
        recording = capturing()
        audio.record(recording, 0, 10)
        copying = FakeRemote(answer(f"source={SHA_A}\ncopy={SHA_A}\n"))
        audio.copy_to_sd(copying, "/tmp/quak-audio.wav", "/tmp/fuse_d/take.wav")
        for command in (audio.LIST_SCRIPT, *recording.commands, *copying.commands):
            self.assertNotRegex(command, FORBIDDEN)


WRITES_AUDIO = r"""#!/bin/sh
printf '%s\n' "$@" > "$(dirname "$0")/argv"
echo "Captured 1000 frames"
printf '%4044s' '' > "$1"
"""
# What raw/31 saw on the SD card, and what a busy or absent device is expected to look like: no error, no audio.
WRITES_ONLY_A_HEADER = r"""#!/bin/sh
echo "Unable to open PCM device" >&2
echo "Captured 0 frames"
printf '%44s' '' > "$1"
"""
FAILS = r"""#!/bin/sh
echo "Unable to create file" >&2
exit 1
"""


class LocalShell:
    """A ``Remote`` that is a shell on this computer, with temporary directories where the camera's paths are."""

    PATHS = re.compile(r"/proc/asound|/proc/mounts|/tmp/fuse_d|/tmp/")
    LOCAL = {"/proc/asound": "asound", "/proc/mounts": "mounts", "/tmp/fuse_d": "fuse_d", "/tmp/": "tmp/"}

    def __init__(self, shell: list[str], root: Path) -> None:
        self.shell = shell
        self.root = root
        self.overrides = ""

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        script = self.PATHS.sub(lambda match: f"{self.root}/{self.LOCAL[match[0]]}", command)
        done = subprocess.run(
            [*self.shell, "-c", "sync() { :; }\n" + self.overrides + script],
            cwd=self.root / "cwd",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        return ExecResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"audio runs commands, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"audio runs commands, but wrote {path}")


class RealShellRuns:
    """The real scripts in a real shell. Subclasses say which shell."""

    shell: list[str]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("asound", "cwd", "fuse_d", "tmp"):
            (self.root / name).mkdir()
        self.sd = self.root / "fuse_d"
        self.tmp = self.root / "tmp"
        self.tool = self.sd / "quak/toolkit/packs/core/tools/tinycap"
        self.mount_sd()
        self.remote = LocalShell(self.shell, self.root)

    def mount_sd(self):
        (self.root / "mounts").write_text(f"/mnt {self.sd} ipcufs rw,relatime 0 0\n", encoding="utf-8")

    def install_tinycap(self, script: str = WRITES_AUDIO, mode: int = 0o755):
        self.tool.parent.mkdir(parents=True)
        self.tool.write_text(script, encoding="utf-8")
        self.tool.chmod(mode)

    def add_card(self, number: int, stream: str):
        folder = self.root / "asound" / f"card{number}"
        folder.mkdir()
        (folder / "stream0").write_text(stream, encoding="utf-8")

    def test_lists_cards_and_their_streams(self):
        (self.root / "asound" / "cards").write_text(MIC_CARD + HEADSET_CARD + SPEAKER_CARD, encoding="utf-8")
        self.add_card(0, MIC_STREAM)
        self.add_card(1, HEADSET_STREAM)
        (self.root / "asound" / "card2").mkdir()  # no stream0: this isn't a USB card
        devices = audio.list_cards(self.remote)
        self.assertEqual(devices[0], MIC)
        self.assertEqual([d.card for d in devices], [0, 1, 1, 2])
        self.assertEqual(devices[-1], AudioDevice(card=2, name="Some - Speaker"))

    def test_lists_nothing_when_the_sound_modules_are_not_loaded(self):
        (self.root / "asound").rmdir()
        self.assertEqual(audio.list_cards(self.remote), [])

    def test_record_runs_tinycap_with_these_arguments(self):
        self.install_tinycap()
        self.assertEqual(audio.record(self.remote, 1, 10), Recording("/tmp/quak-audio.wav", 4044))
        self.assertEqual((self.tmp / "quak-audio.wav").stat().st_size, 4044)
        self.assertEqual(
            (self.tool.parent / "argv").read_text(encoding="utf-8").splitlines(),
            [str(self.tmp / "quak-audio.wav"), "-D", "1", "-d", "0", "-c", "2", "-r", "48000", "-b", "16", "-t", "10"],
        )

    def test_record_without_the_tool_raises(self):
        with self.assertRaisesRegex(ToolkitError, "core toolkit"):
            audio.record(self.remote, 0, 10)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_record_with_a_tool_it_can_not_run_raises(self):
        self.install_tinycap(mode=0o644)
        with self.assertRaisesRegex(ToolkitError, "core toolkit"):
            audio.record(self.remote, 0, 10)

    def test_record_refuses_a_file_with_only_a_header(self):
        self.install_tinycap(WRITES_ONLY_A_HEADER)
        with self.assertRaisesRegex(LabError, r"wrote no audio \(Unable to open PCM device\)"):
            audio.record(self.remote, 0, 10)

    def test_record_reports_tinycap_failing(self):
        self.install_tinycap(FAILS)
        with self.assertRaisesRegex(LabError, "Unable to create file"):
            audio.record(self.remote, 0, 10)

    def test_a_nasty_path_is_a_file_name_and_runs_nothing(self):
        self.install_tinycap()
        for path in NASTY_CAPTURES:
            with self.subTest(path):
                self.assertEqual(audio.record(self.remote, 0, 1, path).size, 4044)
                self.assertTrue((self.tmp / path.removeprefix("/tmp/")).is_file())
        self.assertEqual(list((self.root / "cwd").iterdir()), [], "a name was run as a command")

    def test_copy_puts_a_verified_file_on_the_card(self):
        data = b"the recording"
        (self.tmp / "take.wav").write_bytes(data)
        result = audio.copy_to_sd(self.remote, "/tmp/take.wav", "/tmp/fuse_d/take.wav")
        self.assertEqual(result, CopyResult("/tmp/fuse_d/take.wav", hashlib.sha256(data).hexdigest()))
        self.assertEqual((self.sd / "take.wav").read_bytes(), data)
        self.assertEqual((self.tmp / "take.wav").read_bytes(), data)
        self.assertEqual([path.name for path in self.sd.iterdir()], ["take.wav"])

    def test_copy_into_a_folder_keeps_the_name(self):
        (self.tmp / "take.wav").write_bytes(b"x")
        (self.sd / "MISC").mkdir()
        result = audio.copy_to_sd(self.remote, "/tmp/take.wav", "/tmp/fuse_d/MISC")
        self.assertEqual(result.target, "/tmp/fuse_d/MISC/take.wav")
        self.assertEqual((self.sd / "MISC" / "take.wav").read_bytes(), b"x")

    def test_copy_never_replaces_a_file(self):
        (self.tmp / "take.wav").write_bytes(b"new")
        (self.sd / "take.wav").write_bytes(b"old")
        with self.assertRaisesRegex(LabError, "already exists"):
            audio.copy_to_sd(self.remote, "/tmp/take.wav", "/tmp/fuse_d/take.wav")
        self.assertEqual((self.sd / "take.wav").read_bytes(), b"old")

    def test_copy_that_corrupts_leaves_nothing_behind(self):
        (self.tmp / "take.wav").write_bytes(b"the recording")
        self.remote.overrides = 'cp() { printf corrupt > "$2"; }\n'
        with self.assertRaisesRegex(LabError, "checksums differ"):
            audio.copy_to_sd(self.remote, "/tmp/take.wav", "/tmp/fuse_d/take.wav")
        self.assertEqual(list(self.sd.iterdir()), [])

    def test_copy_with_no_card_mounted_never_lands_in_ram(self):
        (self.tmp / "take.wav").write_bytes(b"x")
        (self.root / "mounts").write_text("proc /proc proc rw 0 0\n", encoding="utf-8")
        with self.assertRaisesRegex(LabError, "is not mounted"):
            audio.copy_to_sd(self.remote, "/tmp/take.wav", "/tmp/fuse_d/take.wav")
        self.assertEqual(list(self.sd.iterdir()), [])

    def test_copy_of_something_missing_says_so(self):
        with self.assertRaisesRegex(LabError, "does not exist"):
            audio.copy_to_sd(self.remote, "/tmp/nope.wav", "/tmp/fuse_d/take.wav")
        self.assertEqual(list(self.sd.iterdir()), [])

    def test_copy_of_a_folder_is_refused(self):
        (self.tmp / "folder").mkdir()
        with self.assertRaisesRegex(LabError, "is a folder"):
            audio.copy_to_sd(self.remote, "/tmp/folder", "/tmp/fuse_d/folder")
        self.assertEqual(list(self.sd.iterdir()), [])


@unittest.skipUnless(shutil.which("sh"), "no sh on this computer")
class SystemShell(RealShellRuns, unittest.TestCase):
    shell = ["sh"]


@unittest.skipUnless(shutil.which("busybox"), "no busybox on this computer")
class BusyBoxShell(RealShellRuns, unittest.TestCase):
    shell = ["busybox", "sh"]


class ServiceDelegation(unittest.TestCase):
    """The real ``LabService`` over a ``FakeRemote``: what reaches the camera, and what doesn't open a route at all."""

    def service(self, remote: FakeRemote | None = None) -> LabService:
        self.routes = 0

        @contextlib.asynccontextmanager
        async def factory(manager, address, *, profile=None):
            self.routes += 1
            yield remote

        return LabService(mock.sentinel.manager, ADDRESS, remote_factory=factory)

    def test_list_runs_the_list_script(self):
        remote = FakeRemote(listing(MIC_CARD, {0: MIC_STREAM}))
        self.assertEqual(asyncio.run(self.service(remote).audio_list()), [MIC])
        self.assertEqual(remote.commands, [audio.LIST_SCRIPT])
        self.assertEqual(self.routes, 1)

    def test_record_captures_from_that_card_for_that_long(self):
        remote = capturing(1933356)
        result = asyncio.run(self.service(remote).audio_record(1, 10))
        self.assertEqual(result, Recording(audio.DEFAULT_PATH, 1933356))
        argv = tinycap_argv(remote.commands[1])
        self.assertEqual(argv[argv.index("-D") + 1], "1")
        self.assertEqual(argv[-2:], ["-t", "10"])
        self.assertEqual(self.routes, 1)

    def test_a_missing_tool_comes_through_as_a_toolkit_error(self):
        with self.assertRaises(ToolkitError):
            asyncio.run(self.service(FakeRemote(answer(exit_code=1))).audio_record(0, 10))

    def test_copy_goes_to_the_card(self):
        remote = FakeRemote(answer(f"source={SHA_A}\ncopy={SHA_A}\n"))
        result = asyncio.run(self.service(remote).audio_copy("/tmp/quak-audio.wav", "/tmp/fuse_d/take.wav"))
        self.assertEqual(result, CopyResult("/tmp/fuse_d/take.wav", SHA_A))
        self.assertEqual(assignment(remote.commands[0], "src"), "/tmp/quak-audio.wav")
        self.assertEqual(assignment(remote.commands[0], "dst"), "/tmp/fuse_d/take.wav")

    def test_merge_hands_everything_to_recording_and_opens_no_route(self):
        with mock.patch("quak.lab.recording.merge", return_value=Path("/x/out.mp4")) as merge:
            result = asyncio.run(self.service().audio_merge("clip", "wav", "out", offset_seconds=1.5, mix=True))
        self.assertEqual(result, Path("/x/out.mp4"))
        merge.assert_called_once_with("clip", "wav", "out", 1.5, True)
        self.assertEqual(self.routes, 0)

    def test_merge_defaults_to_no_offset_and_no_mix(self):
        with mock.patch("quak.lab.recording.merge") as merge:
            asyncio.run(self.service().audio_merge("clip", "wav", "out"))
        merge.assert_called_once_with("clip", "wav", "out", 0.0, False)


class AudioCliCase(unittest.TestCase):
    """``main()`` in-process, with ``LabService`` replaced by an autospec, so a call it wouldn't accept fails here."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": temp.name, "QUAK_SIM": "0"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("QUAK_CAMERA_ADDRESS", None)  # patch.dict puts it back
        self.root = Path(temp.name)

        self.lab = mock.create_autospec(LabService, instance=True)
        self.lab.audio_list.return_value = [MIC]
        self.lab.audio_record.return_value = Recording(audio.DEFAULT_PATH, 1_933_356)
        self.lab.audio_merge.return_value = Path("/home/someone/out.mp4")
        self.service = self._patch("quak.cli.main.LabService", return_value=self.lab)
        self._patch("quak.cli.main.recover_network", return_value=[])
        self._patch("socket.create_connection", side_effect=AssertionError("a test opened a real connection"))

    def _patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_cli(self, *argv, json_mode=True, address=ADDRESS):
        """Run ``quak [--json] lab audio ...`` and return (exit_code, parsed-json, stdout, stderr)."""
        prefix = ["--json"] if json_mode else []
        suffix = ["--address", address] if address else []
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main([*prefix, "lab", "audio", *argv, *suffix])
            except SystemExit as exc:
                code = exc.code or 0
        text = out.getvalue()
        return code, json.loads(text) if json_mode and text.strip() else {}, text, err.getvalue()


class CliList(AudioCliCase):
    def test_needs_no_yes_and_calls_the_service(self):
        code, data, _, _ = self.run_cli("list")
        self.assertEqual(code, 0, data)
        self.lab.audio_list.assert_awaited_once_with()
        self.assertEqual(self.service.call_args.args[1], ADDRESS)

    def test_json_has_each_card(self):
        _, data, _, _ = self.run_cli("list")
        self.assertEqual(
            data,
            {"cards": [{"card": 0, "device": 0, "name": "fifine Microphone", "capture_format": "S16_LE",
                        "rates": [48000, 44100], "channels": 2}]},
        )

    def test_text_is_one_line_per_card(self):
        _, _, text, _ = self.run_cli("list", json_mode=False)
        self.assertEqual(text, "card 0  fifine Microphone  S16_LE  2 ch  48000, 44100 Hz\n")

    def test_a_card_that_cannot_capture_says_so(self):
        self.lab.audio_list.return_value = [AudioDevice(card=2, name="Some - Speaker")]
        _, _, text, _ = self.run_cli("list", json_mode=False)
        self.assertEqual(text, "card 2  Some - Speaker  can't capture\n")

    def test_none_points_at_what_they_need(self):
        self.lab.audio_list.return_value = []
        _, data, _, _ = self.run_cli("list")
        self.assertEqual(data, {"cards": []})
        _, _, text, _ = self.run_cli("list", json_mode=False)
        self.assertIn("quak lab toolkit load", text)


class CliRecord(AudioCliCase):
    def test_without_yes_nothing_happens(self):
        code, data, _, _ = self.run_cli("record", "1", "--seconds", "10")
        self.assertEqual(code, 12)
        self.assertIn("--yes", data["error"])
        self.assertIn(audio.DEFAULT_PATH, data["error"])
        self.service.assert_not_called()

    def test_with_yes_records_from_that_card_for_that_long(self):
        code, data, _, _ = self.run_cli("record", "1", "--seconds", "10", "--yes")
        self.assertEqual(code, 0, data)
        self.lab.audio_record.assert_awaited_once_with(1, 10)
        self.assertEqual(data, {"path": audio.DEFAULT_PATH, "size": 1_933_356, "seconds": 10})

    def test_text_says_where_it_went_and_that_it_is_in_ram(self):
        _, _, text, _ = self.run_cli("record", "1", "--seconds", "10", "--yes", json_mode=False)
        self.assertIn("recording 10 s from sound card 1\n", text)
        self.assertIn(f"recorded {audio.DEFAULT_PATH} on the camera (1.9 MB)", text)
        self.assertIn("a power cycle loses it", text)

    def test_says_when_it_will_record_less_than_asked(self):
        _, data, _, _ = self.run_cli("record", "0", "--seconds", "100000", "--yes")
        self.assertEqual(data["seconds"], audio.MAX_SECONDS)
        _, _, text, _ = self.run_cli("record", "0", "--seconds", "100000", "--yes", json_mode=False)
        self.assertIn(f"recording {audio.MAX_SECONDS} s (the most one capture can hold)", text)

    def test_bad_arguments_show_usage_and_reach_nothing(self):
        cases = [
            ("record",),
            ("record", "--seconds", "10"),
            ("record", "1"),
            ("record", "mic", "--seconds", "10"),
            ("record", "-1", "--seconds", "10"),
            ("record", "1", "--seconds", "0"),
            ("record", "1", "--seconds", "-5"),
            ("record", "1", "2", "--seconds", "10"),
            ("list", "extra"),
            ("merge", "clip", "wav"),
            ("merge", "clip", "wav", "out", "extra"),
            ("delete",),
            (),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, data, _, _ = self.run_cli(*argv, "--yes")
                self.assertEqual(code, 1)
                self.assertIn("usage: quak lab audio", data["error"])
        self.service.assert_not_called()

    def test_a_length_that_is_not_whole_seconds_is_refused_by_the_parser(self):
        code, _, _, err = self.run_cli("record", "1", "--seconds", "1.5", "--yes", json_mode=False)
        self.assertEqual(code, 2)
        self.assertIn("--seconds", err)
        self.service.assert_not_called()

    def test_progress_stays_out_of_json(self):
        _, _, text, _ = self.run_cli("record", "1", "--seconds", "10", "--yes")
        self.assertNotIn("recording", text)

    def test_each_failure_keeps_its_own_exit_code(self):
        for error, expected in ((LabError("lab"), 12), (ToolkitError("toolkit"), 13), (NetworkError("lost"), 6)):
            with self.subTest(error=type(error).__name__):
                self.lab.audio_record.side_effect = error
                code, data, _, _ = self.run_cli("record", "1", "--seconds", "10", "--yes")
                self.assertEqual(code, expected)
                self.assertEqual(data["error"], str(error))


class CliMerge(AudioCliCase):
    def test_the_options_parse_in_either_place(self):
        parse = build_parser().parse_args
        a = parse(["lab", "audio", "merge", "c", "w", "o", "--offset", "-1.5", "--mix", "--yes"])
        self.assertEqual((a.args, a.offset, a.mix, a.yes), (["merge", "c", "w", "o"], -1.5, True, True))
        a = parse(["lab", "--mix", "--offset", "2", "audio", "merge", "c", "w", "o"])
        self.assertEqual((a.args, a.offset, a.mix, a.yes), (["merge", "c", "w", "o"], 2.0, True, False))

    def test_without_yes_nothing_happens(self):
        code, data, _, _ = self.run_cli("merge", "clip", "wav", "out")
        self.assertEqual(code, 12)
        self.assertIn("--yes", data["error"])
        self.service.assert_not_called()

    def test_with_yes_hands_over_the_files_and_defaults(self):
        code, data, _, _ = self.run_cli("merge", "clip", "wav", "out", "--yes")
        self.assertEqual(code, 0, data)
        self.lab.audio_merge.assert_awaited_once_with("clip", "wav", "out", offset_seconds=0.0, mix=False)
        self.assertEqual(data, {"merged": "/home/someone/out.mp4", "offset_seconds": 0.0, "mix": False})

    def test_hands_over_the_offset_and_mix(self):
        _, data, _, _ = self.run_cli("merge", "clip", "wav", "out", "--offset", "-1.5", "--mix", "--yes")
        self.lab.audio_merge.assert_awaited_once_with("clip", "wav", "out", offset_seconds=-1.5, mix=True)
        self.assertEqual((data["offset_seconds"], data["mix"]), (-1.5, True))

    def test_text_says_what_was_written(self):
        _, _, text, _ = self.run_cli("merge", "clip", "wav", "out", "--yes", json_mode=False)
        self.assertIn("merging wav with clip\n", text)
        self.assertIn("wrote /home/someone/out.mp4 (the WAV in place of the clip's sound)", text)
        _, _, text, _ = self.run_cli("merge", "clip", "wav", "out", "--mix", "--offset", "0.5", "--yes", json_mode=False)
        self.assertIn("wrote /home/someone/out.mp4 (the WAV mixed with the clip's sound, offset +0.5 s)", text)

    def test_needs_no_camera(self):
        code, data, _, _ = self.run_cli("merge", "clip", "wav", "out", "--yes", address=None)
        self.assertEqual(code, 0, data)
        self.lab.audio_merge.assert_awaited_once()

    def test_a_refusal_is_a_lab_error(self):
        self.lab.audio_merge.side_effect = LabError("refusing to overwrite out")
        code, data, _, _ = self.run_cli("merge", "clip", "wav", "out", "--yes")
        self.assertEqual(code, 12)
        self.assertEqual(data["error"], "refusing to overwrite out")

    def test_the_real_service_runs_ffmpeg_here_and_opens_no_route(self):
        def no_route(*args, **kwargs):
            raise AssertionError("a merge opened a route to the camera")

        def writes(argv, **kwargs):
            Path(argv[-1]).write_bytes(b"merged")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        self.service.side_effect = lambda manager, address, profile=None: LabService(manager, address, profile, no_route)
        clip, wav, out = self.root / "clip.mp4", self.root / "take.wav", self.root / "out.mp4"
        clip.write_bytes(b"clip")
        wav.write_bytes(b"wav")
        with (
            mock.patch("quak.lab.recording.shutil.which", return_value="/usr/bin/ffmpeg"),
            mock.patch("quak.lab.recording.subprocess.run", side_effect=writes) as run,
        ):
            code, data, _, _ = self.run_cli(
                "merge", str(clip), str(wav), str(out), "--offset", "-0.25", "--yes", address=None
            )
        self.assertEqual(code, 0, data)
        self.assertEqual(data["merged"], str(out))
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/ffmpeg")
        self.assertEqual(argv[argv.index("-itsoffset") : argv.index("-itsoffset") + 4],
                         ["-itsoffset", "-0.250000", "-i", str(wav)])


if __name__ == "__main__":
    unittest.main()

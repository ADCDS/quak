"""The host-side merge: ``subprocess.run`` is faked, so this checks what ffmpeg is told, not what it makes of it."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quak.core.errors import LabError
from quak.lab import recording

FFMPEG = "/usr/bin/ffmpeg"
QUIET = ["-hide_banner", "-loglevel", "error"]
ENCODE = ["-c:v", "copy", "-c:a", "aac", "-shortest"]


def finishes(argv, **kwargs):
    Path(argv[-1]).write_bytes(b"merged")
    return subprocess.CompletedProcess(argv, 0, b"", b"")


def fails(stderr=b""):
    def run(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(argv, 1, b"", stderr)

    return run


class MergeCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clip = self.root / "GX010001.MP4"
        self.wav = self.root / "take.wav"
        self.out = self.root / "merged.mp4"
        self.clip.write_bytes(b"clip")
        self.wav.write_bytes(b"wav")
        self.run_ffmpeg = self._patch("quak.lab.recording.subprocess.run", side_effect=finishes)
        self.which = self._patch("quak.lab.recording.shutil.which", return_value=FFMPEG)

    def _patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def merge(self, **kwargs):
        return recording.merge(self.clip, self.wav, self.out, **kwargs)

    def argv(self):
        return self.run_ffmpeg.call_args.args[0]

    def assert_refused(self, regex, *args, **kwargs):
        with self.assertRaisesRegex(LabError, regex):
            recording.merge(*args, **kwargs)
        self.run_ffmpeg.assert_not_called()


class FfmpegPath(MergeCase):
    def test_is_what_the_path_finds(self):
        self.assertEqual(recording.ffmpeg_path(), FFMPEG)
        self.which.assert_called_once_with("ffmpeg")

    def test_is_none_without_one(self):
        self.which.return_value = None
        self.assertIsNone(recording.ffmpeg_path())


class Merge(MergeCase):
    def test_replaces_the_clips_sound_with_the_wav(self):
        self.assertEqual(self.merge(), self.out)
        self.assertEqual(
            self.argv(),
            [FFMPEG, *QUIET, "-y", "-i", str(self.clip), "-i", str(self.wav), "-map", "0:v", "-map", "1:a", *ENCODE,
             str(self.out)],
        )

    def test_runs_an_argv_list_with_no_shell_and_no_stdin(self):
        self.merge()
        self.assertIsInstance(self.argv(), list)
        kwargs = self.run_ffmpeg.call_args.kwargs
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_a_path_is_only_ever_one_argument(self):
        for name in ("a b.wav", "it's.wav", "$(touch ran).wav", "x; touch ran #.wav", 'q"q.wav', "new\nline.wav"):
            with self.subTest(name):
                self.run_ffmpeg.reset_mock()
                wav = self.root / name
                wav.write_bytes(b"wav")
                out = self.root / f"{name}.mp4"
                recording.merge(self.clip, wav, out)
                self.assertIn(str(wav), self.argv())
                self.assertEqual(self.argv()[-1], str(out))

    def test_a_relative_name_cannot_pass_for_an_option_or_a_protocol(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.root)
        for name in ("-x.mp4", "pipe:1"):
            with self.subTest(name):
                self.assertEqual(recording.merge(self.clip.name, self.wav.name, name), Path.cwd() / name)
                self.assertEqual(self.argv()[-1], str(Path.cwd() / name))
                self.assertEqual(self.argv()[self.argv().index("-i") + 1], str(Path.cwd() / self.clip.name))


class Offset(MergeCase):
    def test_goes_right_before_the_wav_because_it_applies_to_the_next_input(self):
        self.merge(offset_seconds=1.5)
        argv = self.argv()
        at = argv.index("-itsoffset")
        self.assertEqual(argv[at - 2 : at], ["-i", str(self.clip)])
        self.assertEqual(argv[at : at + 4], ["-itsoffset", "1.500000", "-i", str(self.wav)])

    def test_can_be_negative(self):
        self.merge(offset_seconds=-0.25)
        self.assertEqual(self.argv()[self.argv().index("-itsoffset") + 1], "-0.250000")

    def test_never_comes_out_in_a_form_ffmpeg_cannot_read(self):
        self.merge(offset_seconds=0.00001)
        self.assertEqual(self.argv()[self.argv().index("-itsoffset") + 1], "0.000010")

    def test_none_leaves_the_option_out(self):
        self.merge()
        self.assertNotIn("-itsoffset", self.argv())

    def test_that_is_no_number_is_refused(self):
        for offset in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(offset):
                self.assert_refused("not an offset", self.clip, self.wav, self.out, offset_seconds=offset)
        self.assertFalse(self.out.exists())


class Mix(MergeCase):
    def graph(self):
        return self.argv()[self.argv().index("-filter_complex") + 1]

    def test_mixes_the_clips_sound_with_the_wav(self):
        self.merge(mix=True)
        self.assertEqual(
            self.argv(),
            [FFMPEG, *QUIET, "-y", "-i", str(self.clip), "-i", str(self.wav),
             "-filter_complex", "[0:a][1:a]amix=inputs=2[mixed]", "-map", "0:v", "-map", "[mixed]", *ENCODE,
             str(self.out)],
        )

    def test_an_offset_goes_in_the_graph_because_amix_ignores_timestamps(self):
        self.merge(mix=True, offset_seconds=1.5)
        self.assertNotIn("-itsoffset", self.argv())
        self.assertEqual(self.graph(), "[1:a]adelay=delays=1500:all=1[wav];[0:a][wav]amix=inputs=2[mixed]")

    def test_a_negative_offset_skips_the_start_of_the_wav(self):
        self.merge(mix=True, offset_seconds=-0.25)
        self.assertNotIn("-itsoffset", self.argv())
        self.assertEqual(self.graph(), "[1:a]atrim=start=0.250000,asetpts=PTS-STARTPTS[wav];[0:a][wav]amix=inputs=2[mixed]")


class Refusals(MergeCase):
    def test_never_overwrites(self):
        self.out.write_bytes(b"precious")
        self.assert_refused("refusing to overwrite", self.clip, self.wav, self.out)
        self.assertEqual(self.out.read_bytes(), b"precious")

    def test_never_overwrites_an_input(self):
        self.assert_refused("refusing to overwrite", self.clip, self.wav, self.clip)
        self.assert_refused("refusing to overwrite", self.clip, self.wav, self.wav)
        self.assertEqual(self.clip.read_bytes(), b"clip")
        self.assertEqual(self.wav.read_bytes(), b"wav")

    def test_never_writes_through_a_dangling_link(self):
        elsewhere = self.root / "elsewhere.mp4"
        self.out.symlink_to(elsewhere)
        self.assert_refused("refusing to overwrite", self.clip, self.wav, self.out)
        self.assertFalse(elsewhere.exists())

    def test_a_folder_that_is_not_there_says_so(self):
        self.assert_refused("can't write", self.clip, self.wav, self.root / "nope" / "merged.mp4")

    def test_a_missing_input(self):
        nope = self.root / "nope"
        self.assert_refused("is not a file", nope, self.wav, self.out)
        self.assert_refused("is not a file", self.clip, nope, self.out)
        self.assertFalse(self.out.exists())

    def test_a_folder_as_an_input(self):
        self.assert_refused("is not a file", self.root, self.wav, self.out)
        self.assert_refused("is not a file", self.clip, self.root, self.out)

    def test_no_ffmpeg(self):
        self.which.return_value = None
        self.assert_refused("ffmpeg is not installed", self.clip, self.wav, self.out)
        self.assertFalse(self.out.exists())


class Failures(MergeCase):
    def test_says_what_ffmpeg_said_and_leaves_nothing_behind(self):
        self.run_ffmpeg.side_effect = fails(b"Stream map '0:a' matches no streams\n")
        with self.assertRaisesRegex(LabError, "GX010001.MP4: Stream map '0:a' matches no streams"):
            self.merge(mix=True)
        self.assertFalse(self.out.exists())

    def test_with_nothing_said_gives_the_exit_code(self):
        self.run_ffmpeg.side_effect = fails()
        with self.assertRaisesRegex(LabError, "exit code 1"):
            self.merge()

    def test_only_the_end_of_a_long_complaint_is_kept(self):
        self.run_ffmpeg.side_effect = fails(b"first" + b"x" * 5000 + b"last")
        with self.assertRaises(LabError) as caught:
            self.merge()
        self.assertTrue(str(caught.exception).endswith("last"))
        self.assertNotIn("first", str(caught.exception))

    def test_ffmpeg_cannot_put_control_characters_on_a_terminal(self):
        self.run_ffmpeg.side_effect = fails(b"bad\x1b[31m name\x07")
        with self.assertRaises(LabError) as caught:
            self.merge()
        self.assertIn("bad[31m name", str(caught.exception))

    def test_ffmpeg_exiting_0_with_nothing_written_is_a_failure(self):
        self.run_ffmpeg.side_effect = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b"")
        with self.assertRaisesRegex(LabError, "wrote nothing"):
            self.merge()
        self.assertFalse(self.out.exists())

    def test_ffmpeg_not_starting_leaves_nothing_behind(self):
        self.run_ffmpeg.side_effect = PermissionError(13, "Permission denied")
        with self.assertRaises(PermissionError):
            self.merge()
        self.assertFalse(self.out.exists())

    def test_a_failure_does_not_block_the_next_try(self):
        self.run_ffmpeg.side_effect = fails()
        with self.assertRaises(LabError):
            self.merge()
        self.run_ffmpeg.side_effect = finishes
        self.assertEqual(self.merge(), self.out)
        self.assertEqual(self.out.read_bytes(), b"merged")


if __name__ == "__main__":
    unittest.main()

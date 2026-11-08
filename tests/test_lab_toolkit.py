"""The toolkit's manifests, gate, state, install, load, unload and remove: temp files and a fake camera, no hardware.

The fake records each command and answers from a queue, so these tests pin what Quak asks the camera and
how it reads the answers. Commands and paths are literals rather than the module's own constants: a wrong
path has to fail here. Nothing runs the scripts, so whether BusyBox does what the text says is still for
a real camera to show.
"""

import hashlib
import http.client
import io
import json
import re
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from quak.core.errors import NetworkError, ToolkitError
from quak.lab import toolkit
from quak.lab.models import PackManifest, PackModule, PackState, PackTool
from quak.lab.toolkit import (
    check_compatible,
    check_pack_source,
    core_manifest,
    download_pack,
    extract_pack,
    install_core,
    install_pack,
    install_pack_url,
    load_core,
    load_manifest,
    manifest_to_dict,
    remove_core,
    sha256_file,
    toolkit_state,
    unload_core,
    validate_manifest,
    verify_pack_signature,
)
from quak.ssh.models import ExecResult

KERNEL = "4.19.124"
VERMAGIC = "4.19.124 SMP preempt mod_unload modversions aarch64"
SHA = "0123456789abcdef" * 4
OTHER_SHA = "fedcba9876543210" * 4

# As the real modules declare themselves in their depends= strings, in their own mixed spellings.
REAL_DEPENDS = {
    "gp_symdump": [],
    "mii": [],
    "r8152": ["mii"],
    "snd-hwdep": ["snd"],
    "snd": ["soundcore"],
    "snd-pcm": ["snd", "snd-timer"],
    "snd-rawmidi": ["snd"],
    "snd-timer": ["snd"],
    "snd-usb-audio": ["snd-usbmidi-lib", "snd-pcm", "snd", "snd-hwdep"],
    "snd-usbmidi-lib": ["snd-rawmidi", "snd"],
    "soundcore": [],
    "usb-storage": [],
}

BAD_NAMES = ["x; reboot", "$(reboot)", "`reboot`", "a b", "../x", "x/y", "x\n", "", "-f", "x.ko"]
BAD_PATHS = ["../x", "ko/../../x", "/bin/sh", "ko//x", "ko/", "", ".", "-rf", "a b", "x; reboot", "$(reboot)", "x\n"]

READ_STATE = "[ -e /tmp/fuse_d/quak/toolkit/state.json ] || exit 3\ncat /tmp/fuse_d/quak/toolkit/state.json\n"
PACK_PATH = "/tmp/fuse_d/quak/toolkit/packs/core"
CHECK_FILES = (
    "root=/tmp/fuse_d/quak/toolkit/packs/core\n"
    'for f in manifest.json ko/snd-pcm.ko tinycap; do [ -e "$root/$f" ] || echo "missing=$f"; done\n'
)
STATE_FILES = ["manifest.json", "ko/snd-pcm.ko", "tinycap"]


def module(name="snd", deps=(), sha256=SHA) -> dict:
    return {"name": name, "deps": list(deps), "sha256": sha256}


def manifest_data(**overrides) -> dict:
    data = {
        "id": "core",
        "version": "1.0",
        "kernel": KERNEL,
        "vermagic": VERMAGIC,
        "config_sha256": None,
        "modules": [module("soundcore"), module("snd", ["soundcore"], OTHER_SHA)],
        "tools": ["tinycap", "tinymix"],
        "firmware": ["rtl_nic/rtl8153a-2.fw"],
        "license": "GPL-2.0",
        "source": "Linux 4.19.124",
    }
    return {**data, **overrides}


def pack(**overrides) -> PackManifest:
    return PackManifest(**{"id": "core", "kernel": KERNEL, "vermagic": VERMAGIC, **overrides})


def answer(output: str | bytes = b"", exit_code: int = 0, stderr: bytes = b"") -> ExecResult:
    stdout = output.encode() if isinstance(output, str) else output
    return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def state_json(**overrides) -> str:
    return json.dumps({"version": 1, "pack": "core", "files": STATE_FILES, **overrides})


class FakeRemote:
    """A ``Remote`` that records each command and answers from a queue. An exception in the queue is raised."""

    def __init__(self, *answers: ExecResult | Exception) -> None:
        self.answers = list(answers)
        self.commands: list[str] = []

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        if not self.answers:
            raise AssertionError(f"a command nobody expected: {command[:80]!r}")
        reply = self.answers.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"reading the state should only use exec, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"reading the state should change nothing, but wrote {path}")


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, data: dict | list | bytes, name: str = "manifest.json") -> Path:
        path = self.dir / name
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return path

    def assert_refused(self, data: dict | list | bytes, expected: str = "") -> None:
        path = self.write(data)
        with self.assertRaises(ToolkitError) as caught:
            load_manifest(path)
        self.assertIn(expected, str(caught.exception))
        self.assertIn(str(path), str(caught.exception))


class LoadManifestTests(TempDirTestCase):
    def test_every_field(self):
        self.assertEqual(
            load_manifest(self.write(manifest_data(config_sha256=SHA))),
            PackManifest(
                id="core",
                version="1.0",
                kernel=KERNEL,
                vermagic=VERMAGIC,
                config_sha256=SHA,
                modules=[
                    PackModule(name="soundcore", deps=[], sha256=SHA),
                    PackModule(name="snd", deps=["soundcore"], sha256=OTHER_SHA),
                ],
                tools=["tinycap", "tinymix"],
                firmware=["rtl_nic/rtl8153a-2.fw"],
                license="GPL-2.0",
                source="Linux 4.19.124",
            ),
        )

    def test_a_path_as_a_string(self):
        self.assertEqual(load_manifest(str(self.write(manifest_data()))).id, "core")

    def test_only_what_the_gate_needs_is_required(self):
        data = {"id": "core", "version": "1", "kernel": KERNEL, "vermagic": VERMAGIC, "modules": []}
        self.assertEqual(load_manifest(self.write(data)), pack(version="1"))

    def test_a_module_without_deps(self):
        manifest = load_manifest(self.write(manifest_data(modules=[{"name": "mii", "sha256": SHA}])))
        self.assertEqual(manifest.modules, [PackModule(name="mii", deps=[], sha256=SHA)])

    def test_unknown_fields_are_ignored(self):
        expected = load_manifest(self.write(manifest_data()))
        self.assertEqual(load_manifest(self.write(manifest_data(later="x"))), expected)

    def test_round_trip_through_a_dict(self):
        manifest = load_manifest(self.write(manifest_data(config_sha256=SHA)))
        data = manifest_to_dict(manifest)
        self.assertEqual(data, manifest_data(config_sha256=SHA))
        self.assertEqual(load_manifest(self.write(data)), manifest)

    def test_the_dict_shares_nothing_with_the_manifest(self):
        manifest = pack(modules=[PackModule("snd", [], SHA)])
        manifest_to_dict(manifest)["modules"][0]["deps"].append("x")
        self.assertEqual(manifest.modules[0].deps, [])

    def test_no_such_file(self):
        with self.assertRaisesRegex(ToolkitError, "could not read the pack manifest .*absent.json"):
            load_manifest(self.dir / "absent.json")

    def test_a_directory(self):
        with self.assertRaisesRegex(ToolkitError, "could not read the pack manifest"):
            load_manifest(self.dir)

    def test_bad_json(self):
        cases = {
            "empty": b"",
            "truncated": b'{"id": "co',
            "not json": b"id: core\n",
            "binary": b"\xff\xfe\x00",
            "bad utf-8": b'{"id": "\xff"}',
            "absurdly nested": b"[" * 200_000,
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.assert_refused(raw, "not valid JSON")

    def test_not_an_object(self):
        for raw in (b"[]", b'"core"', b"1", b"null", b"true"):
            with self.subTest(raw):
                self.assert_refused(raw, "not a JSON object")

    def test_fields_of_the_wrong_type(self):
        cases = [
            ({"id": 1}, "id must be a string"),
            ({"id": None}, "id must be a string"),
            ({"version": 1}, "version must be a string"),
            ({"kernel": 4.19}, "kernel must be a string"),
            ({"kernel": None}, "kernel must be a string"),
            ({"vermagic": []}, "vermagic must be a string"),
            ({"config_sha256": 7}, "config_sha256 must be a string or null"),
            ({"modules": {}}, "modules must be a list"),
            ({"modules": "snd"}, "modules must be a list"),
            ({"modules": None}, "modules must be a list"),
            ({"modules": ["snd"]}, "modules[0] must be an object"),
            ({"modules": [module(), 1]}, "modules[1] must be an object"),
            ({"modules": [{"name": 1, "sha256": SHA}]}, "modules[0].name must be a string"),
            ({"modules": [{"name": "snd", "deps": "soundcore", "sha256": SHA}]}, "modules[0].deps must be a list"),
            ({"modules": [{"name": "snd", "deps": [1], "sha256": SHA}]}, "modules[0].deps must be a list"),
            ({"modules": [{"name": "snd", "sha256": 1}]}, "modules[0].sha256 must be a string"),
            ({"tools": "tinycap"}, "tools must be a list of strings"),
            ({"tools": [1]}, "tools must be a list of strings"),
            ({"firmware": {}}, "firmware must be a list of strings"),
            ({"license": 1}, "license must be a string"),
            ({"source": []}, "source must be a string"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides):
                self.assert_refused(manifest_data(**overrides), expected)

    def test_required_fields(self):
        for key, expected in (
            ("id", "can't be a pack id"),
            ("version", "version is missing"),
            ("kernel", "kernel is missing"),
            ("vermagic", "vermagic is missing"),
            ("modules", "modules must be a list"),
        ):
            with self.subTest(key):
                data = manifest_data()
                del data[key]
                self.assert_refused(data, expected)

    def test_blank_fields_count_as_missing(self):
        for key in ("version", "kernel", "vermagic"):
            for blank in ("", "  \n"):
                with self.subTest(key=key, blank=blank):
                    self.assert_refused(manifest_data(**{key: blank}), f"{key} is missing")

    def test_bad_pack_ids(self):
        for bad in ("../x", "a/b", "", ".", "..", ".hidden", "-x", "x\n", "a b", "x; reboot"):
            with self.subTest(bad):
                self.assert_refused(manifest_data(id=bad), "can't be a pack id")

    def test_a_vermagic_for_another_kernel(self):
        self.assert_refused(manifest_data(kernel="4.19.125"), "is not for kernel")

    def test_config_sha256_is_null_or_a_digest(self):
        for good in (None, SHA):
            with self.subTest(good):
                self.assertEqual(load_manifest(self.write(manifest_data(config_sha256=good))).config_sha256, good)
        for bad in ("", "abc", SHA.upper(), SHA + "0", SHA + "\n"):
            with self.subTest(bad):
                self.assert_refused(manifest_data(config_sha256=bad), "config_sha256")

    def test_tools_and_firmware_stay_inside_the_pack(self):
        for key in ("tools", "firmware"):
            for bad in BAD_PATHS:
                with self.subTest(key=key, bad=bad):
                    self.assert_refused(manifest_data(**{key: [bad]}), "not a path inside the pack")


class ModuleValidationTests(TempDirTestCase):
    def test_the_real_packs_dependencies_pass(self):
        modules = [module(name, deps) for name, deps in REAL_DEPENDS.items()]
        self.assertEqual(len(load_manifest(self.write(manifest_data(modules=modules))).modules), 12)

    def test_names_keep_their_spelling(self):
        modules = [module("snd-timer"), module("snd_pcm", ["snd-timer"])]
        loaded = load_manifest(self.write(manifest_data(modules=modules))).modules
        spellings = [(entry.name, entry.deps) for entry in loaded]
        self.assertEqual(spellings, [("snd-timer", []), ("snd_pcm", ["snd-timer"])])

    def test_a_dependency_in_the_kernels_spelling_is_the_same_module(self):
        for name, dep in (("snd-timer", "snd_timer"), ("snd_timer", "snd-timer")):
            with self.subTest(name=name, dep=dep):
                load_manifest(self.write(manifest_data(modules=[module(name), module("snd-pcm", [dep])])))

    def test_bad_module_names(self):
        for bad in BAD_NAMES:
            with self.subTest(bad):
                self.assert_refused(manifest_data(modules=[module(bad)]), "not a kernel module name")

    def test_bad_dependency_names(self):
        for bad in BAD_NAMES:
            with self.subTest(bad):
                self.assert_refused(manifest_data(modules=[module("snd", [bad])]), "not a kernel module name")

    def test_a_module_needs_a_good_sha256(self):
        for bad in ("", "abc", SHA.upper(), SHA + "0", SHA[:-1] + "g", SHA + "\n"):
            with self.subTest(bad):
                self.assert_refused(manifest_data(modules=[module(sha256=bad)]), "module snd needs a sha256")

    def test_a_module_without_a_sha256(self):
        self.assert_refused(manifest_data(modules=[{"name": "snd"}]), "module snd needs a sha256")

    def test_the_same_module_twice(self):
        for first, second in (("snd", "snd"), ("snd-pcm", "snd_pcm")):
            with self.subTest(first=first, second=second):
                self.assert_refused(manifest_data(modules=[module(first), module(second)]), "listed twice")

    def test_a_dependency_outside_the_pack(self):
        modules = [module("snd-pcm", ["snd", "snd-timer"])]
        self.assert_refused(manifest_data(modules=modules), "depends on snd, snd_timer, which the pack doesn't have")

    def test_a_module_depending_on_itself(self):
        self.assert_refused(manifest_data(modules=[module("snd", ["snd"])]), "in a circle")

    def test_a_dependency_circle(self):
        modules = [module("a", ["b"]), module("b", ["c-d"]), module("c_d", ["a"])]
        self.assert_refused(manifest_data(modules=modules), "in a circle")

    def test_validating_a_manifest_built_in_code(self):
        validate_manifest(pack(version="1", modules=[PackModule("snd", [], SHA)]))
        for bad in (PackManifest(id="core"), pack(), pack(version="1", modules=[PackModule("x; reboot", [], SHA)])):
            with self.subTest(bad):
                with self.assertRaises(ToolkitError):
                    validate_manifest(bad)


class Sha256FileTests(TempDirTestCase):
    def test_known_digests(self):
        self.assertEqual(
            sha256_file(self.write(b"abc")),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(
            sha256_file(self.write(b"")),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_a_file_longer_than_any_read_buffer(self):
        data = bytes(range(256)) * 8192
        self.assertEqual(sha256_file(self.write(data)), hashlib.sha256(data).hexdigest())

    def test_a_path_as_a_string(self):
        self.assertEqual(sha256_file(str(self.write(b""))), sha256_file(self.dir / "manifest.json"))

    def test_unreadable_paths(self):
        for path in (self.dir / "absent", self.dir):
            with self.subTest(path):
                with self.assertRaisesRegex(ToolkitError, "could not read"):
                    sha256_file(path)


class CoreManifestTests(TempDirTestCase):
    def test_where_the_core_pack_lives(self):
        self.assertEqual(toolkit.CORE_DIR.parts[-4:], ("quak", "lab", "toolkit", "core"))
        self.assertEqual(toolkit.CORE_DIR, Path(toolkit.__file__).parent / "toolkit" / "core")

    def test_reads_the_manifest_in_the_core_dir(self):
        self.write(manifest_data())
        with mock.patch.object(toolkit, "CORE_DIR", self.dir):
            self.assertEqual(core_manifest().id, "core")

    def test_a_core_pack_that_is_not_there(self):
        with mock.patch.object(toolkit, "CORE_DIR", self.dir / "absent"):
            with self.assertRaisesRegex(ToolkitError, "could not read the pack manifest"):
                core_manifest()

    def test_a_bad_core_manifest_is_refused(self):
        self.write(b"{")
        with mock.patch.object(toolkit, "CORE_DIR", self.dir):
            with self.assertRaisesRegex(ToolkitError, "bad pack manifest"):
                core_manifest()


class CheckCompatibleTests(unittest.TestCase):
    def test_an_exact_match_passes(self):
        self.assertIsNone(check_compatible(pack(), KERNEL, VERMAGIC))

    def test_shell_output_and_trailing_spaces_dont_matter(self):
        check_compatible(pack(), KERNEL + "\n", VERMAGIC + " \n")
        check_compatible(pack(vermagic=VERMAGIC + " "), KERNEL, VERMAGIC)

    def test_another_kernel_is_refused(self):
        for running in ("4.19.125", "4.19.12", "4.19.124-rc1", "4.19", "5.4.0"):
            with self.subTest(running):
                with self.assertRaises(ToolkitError) as caught:
                    check_compatible(pack(), running, VERMAGIC)
                self.assertIn(f"kernel {KERNEL}", str(caught.exception))
                self.assertIn(running, str(caught.exception))

    def test_another_vermagic_is_refused(self):
        for running in (
            "4.19.124 SMP preempt mod_unload aarch64",
            "4.19.124 SMP mod_unload modversions aarch64",
            "4.19.124 SMP preempt mod_unload modversions x86_64",
            "4.19.125 SMP preempt mod_unload modversions aarch64",
            KERNEL,
        ):
            with self.subTest(running):
                with self.assertRaisesRegex(ToolkitError, "is for vermagic"):
                    check_compatible(pack(), KERNEL, running)

    def test_the_comparison_is_exact_about_case(self):
        with self.assertRaises(ToolkitError):
            check_compatible(pack(), KERNEL, VERMAGIC.upper())

    def test_not_knowing_is_a_refusal(self):
        for manifest, kernel, vermagic in (
            (pack(), "", VERMAGIC),
            (pack(), " \n", VERMAGIC),
            (pack(), KERNEL, ""),
            (pack(), KERNEL, "\n"),
            (pack(kernel=""), KERNEL, VERMAGIC),
            (pack(vermagic=""), KERNEL, VERMAGIC),
            (pack(kernel=""), "", VERMAGIC),
            (pack(vermagic=""), KERNEL, ""),
            (PackManifest(id="core"), "", ""),
        ):
            with self.subTest(kernel=kernel, vermagic=vermagic, manifest=manifest):
                with self.assertRaisesRegex(ToolkitError, "unknown"):
                    check_compatible(manifest, kernel, vermagic)


class ToolkitStateTests(unittest.TestCase):
    def test_installed(self):
        remote = FakeRemote(answer(state_json()), answer())
        self.assertEqual(toolkit_state(remote), PackState(installed=True, path=PACK_PATH))
        self.assertEqual(remote.commands, [READ_STATE, CHECK_FILES])

    def test_no_state_file_is_simply_not_installed(self):
        remote = FakeRemote(answer(exit_code=3))
        self.assertEqual(toolkit_state(remote), PackState())
        self.assertEqual(remote.commands, [READ_STATE])

    def test_a_file_gone_from_the_card(self):
        remote = FakeRemote(answer(state_json()), answer("missing=ko/snd-pcm.ko\nmissing=tinycap\n"))
        state = toolkit_state(remote)
        self.assertFalse(state.installed)
        self.assertEqual(state.path, PACK_PATH)
        self.assertIn(f"{PACK_PATH} is missing ko/snd-pcm.ko, tinycap", state.error)
        self.assertIn("quak lab toolkit install", state.error)

    def test_only_missing_lines_count(self):
        remote = FakeRemote(answer(state_json()), answer("a login notice\n\nnot missing=x\n"))
        self.assertEqual(toolkit_state(remote), PackState(installed=True, path=PACK_PATH))

    def test_a_state_file_that_cant_be_read(self):
        remote = FakeRemote(answer(exit_code=1, stderr=b"cat: read error: I/O error\n"))
        state = toolkit_state(remote)
        self.assertFalse(state.installed)
        self.assertIn("could not read /tmp/fuse_d/quak/toolkit/state.json: cat: read error: I/O error", state.error)
        self.assertEqual(remote.commands, [READ_STATE])

    def test_a_failed_read_without_a_message(self):
        self.assertIn("exit code 1", toolkit_state(FakeRemote(answer(exit_code=1))).error)

    def test_files_that_cant_be_checked(self):
        state = toolkit_state(FakeRemote(answer(state_json()), answer(exit_code=2, stderr=b"sh: no\n")))
        self.assertFalse(state.installed)
        self.assertEqual(state.path, PACK_PATH)
        self.assertIn(f"could not check the files in {PACK_PATH}: sh: no", state.error)

    def assert_corrupt(self, raw: str | bytes, expected: str) -> None:
        remote = FakeRemote(answer(raw))
        state = toolkit_state(remote)
        self.assertFalse(state.installed)
        self.assertEqual(state.path, "")
        self.assertIn("/tmp/fuse_d/quak/toolkit/state.json is corrupt", state.error)
        self.assertIn(expected, state.error)
        self.assertIn("quak lab toolkit install", state.error)
        self.assertEqual(remote.commands, [READ_STATE], "a corrupt state file must not reach a second command")

    def test_state_that_isnt_json(self):
        for raw in (b"", b'{"version": 1, "pack": "co', b"\xff\xfe\x00", b"[" * 200_000):
            with self.subTest(raw[:20]):
                self.assert_corrupt(raw, "not valid JSON")

    def test_state_that_isnt_an_object(self):
        for raw in ("[]", "null", "1", '"core"'):
            with self.subTest(raw):
                self.assert_corrupt(raw, "not a JSON object")

    def test_state_of_another_version(self):
        for version in (0, 2, "1", None):
            with self.subTest(version):
                self.assert_corrupt(state_json(version=version), "version is not 1")
        self.assert_corrupt(json.dumps({"pack": "core", "files": STATE_FILES}), "version is not 1")

    def test_state_naming_no_usable_pack(self):
        for bad in ("../..", "a/b", "", ".", "..", "x; reboot", "$(reboot)", "x\n", 1, None, ["core"]):
            with self.subTest(bad):
                self.assert_corrupt(state_json(pack=bad), "usable pack")
        self.assert_corrupt(json.dumps({"version": 1, "files": STATE_FILES}), "usable pack")

    def test_state_without_a_file_list(self):
        for bad in ([], "tinycap", None, {}, 1):
            with self.subTest(bad):
                self.assert_corrupt(state_json(files=bad), "doesn't list")
        self.assert_corrupt(json.dumps({"version": 1, "pack": "core"}), "doesn't list")

    def test_state_with_files_outside_the_pack(self):
        for bad in (*BAD_PATHS, "`reboot`", "x\nmissing=forged", 1, None, ["x"]):
            with self.subTest(bad):
                self.assert_corrupt(state_json(files=["manifest.json", bad]), "outside the pack")

    def test_a_lost_link_raises_at_either_step(self):
        for answers in ((NetworkError("gone"),), (answer(state_json()), NetworkError("gone"))):
            with self.subTest(len(answers)):
                with self.assertRaisesRegex(NetworkError, "gone"):
                    toolkit_state(FakeRemote(*answers))


def tool(name="tinycap", sha256=SHA) -> dict:
    return {"name": name, "sha256": sha256}


class ToolEntryTests(TempDirTestCase):
    def test_a_tool_with_its_hash(self):
        manifest = load_manifest(self.write(manifest_data(tools=[tool()])))
        self.assertEqual(manifest.tools, [PackTool(name="tinycap", sha256=SHA)])

    def test_a_round_trip_keeps_the_hash(self):
        data = manifest_data(tools=[tool()])
        self.assertEqual(manifest_to_dict(load_manifest(self.write(data))), data)

    def test_a_tool_needs_a_good_sha256(self):
        for bad in ("", "abc", SHA.upper(), SHA + "0", SHA + "\n"):
            with self.subTest(bad):
                self.assert_refused(manifest_data(tools=[tool(sha256=bad)]), "tool tinycap needs a sha256")
        self.assert_refused(manifest_data(tools=[{"name": "tinycap"}]), "tool tinycap needs a sha256")

    def test_a_tool_stays_inside_the_pack(self):
        for bad in BAD_PATHS:
            with self.subTest(bad):
                self.assert_refused(manifest_data(tools=[tool(bad)]), "not a path inside the pack")

    def test_fields_of_the_wrong_type(self):
        for entry, expected in (
            ({"name": 1, "sha256": SHA}, "tools[0].name must be a string"),
            ({"name": "tinycap", "sha256": 1}, "tools[0].sha256 must be a string"),
        ):
            with self.subTest(entry):
                self.assert_refused(manifest_data(tools=[entry]), expected)


class RecordingRemote(FakeRemote):
    """A ``FakeRemote`` that keeps what is written, logs every step in order, and can lose the link on one path."""

    def __init__(self, *answers: ExecResult | Exception, lost_at: str = "") -> None:
        super().__init__(*answers)
        self.written: dict[str, bytes] = {}
        self.steps: list[str] = []
        self.lost_at = lost_at

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.steps.append(command)
        return super().exec(command, input, timeout)

    def write_file(self, path: str, data: bytes) -> None:
        if path == self.lost_at:
            raise NetworkError("gone")
        self.steps.append(path)
        self.written[path] = data


class BundledPackTests(unittest.TestCase):
    """The real files in ``CORE_DIR``: what ships has to pass, since nothing else would catch it before a camera."""

    def test_its_manifest_loads_and_round_trips(self):
        manifest = core_manifest()
        self.assertEqual(manifest.id, "core")
        self.assertEqual(len(manifest.modules), 11)
        self.assertEqual([entry.name for entry in manifest.tools], ["tinycap", "tinymix", "tinypcminfo"])
        on_disk = json.loads((toolkit.CORE_DIR / "manifest.json").read_bytes())
        self.assertEqual(manifest_to_dict(manifest), on_disk)

    def test_it_installs(self):
        names = ["manifest.json"]
        names += [f"modules/{entry.name}.ko" for entry in core_manifest().modules]
        names += [f"tools/{entry.name}" for entry in core_manifest().tools]
        sums = "".join(f"{sha256_file(toolkit.CORE_DIR / name)}  {name}\n" for name in names)
        remote = RecordingRemote(answer(), answer(sums))

        self.assertEqual(install_core(remote), PackState(installed=True, path=PACK_PATH))

        written = {f"{PACK_PATH}/{name}" for name in names} | {"/tmp/fuse_d/quak/toolkit/state.json"}
        self.assertEqual(set(remote.written), written)
        self.assertIn(f"{PACK_PATH}/modules/snd-usb-audio.ko", remote.written)
        self.assertIn(f"{PACK_PATH}/tools/tinypcminfo", remote.written)
        self.assertIn("chmod 755 tools/tinycap tools/tinymix tools/tinypcminfo && ", remote.commands[1])

    def test_it_loads_in_its_manifests_order_and_unloads_in_reverse(self):
        order = [
            "soundcore",
            "snd",
            "snd-timer",
            "snd-pcm",
            "snd-hwdep",
            "snd-rawmidi",
            "snd-usbmidi-lib",
            "snd-usb-audio",
            "usb-storage",
            "mii",
            "r8152",
        ]
        modules = core_manifest().modules
        sums = "".join(f"{entry.sha256}  {entry.name}.ko\n" for entry in modules)
        remote = FakeRemote(answer(state_json()), answer(), answer(f"kernel={KERNEL}\n"), answer(sums), answer())
        remote.answers += [answer() for _ in order]

        self.assertTrue(load_core(remote).loaded)

        self.assertEqual(loaded_files(remote), order)
        remote = FakeRemote(answer(), answer(exit_code=3))
        unload_core(remote)
        self.assertEqual(remote.commands[0], unload_script(*[name.replace("-", "_") for name in reversed(order)]))


STATE_PATH = "/tmp/fuse_d/quak/toolkit/state.json"
INSTALLED = ["manifest.json", "modules/soundcore.ko", "modules/snd-pcm.ko", "tools/tinycap"]
PREPARE = (
    "rm -f /tmp/fuse_d/quak/toolkit/state.json && mkdir -p /tmp/fuse_d/quak/toolkit/packs/core "
    "/tmp/fuse_d/quak/toolkit/packs/core/modules /tmp/fuse_d/quak/toolkit/packs/core/tools\n"
)
VERIFY = (
    "cd /tmp/fuse_d/quak/toolkit/packs/core && chmod 755 tools/tinycap && "
    "sha256sum manifest.json modules/soundcore.ko modules/snd-pcm.ko tools/tinycap\n"
)
SOUNDCORE, SND_PCM, TINYCAP = b"soundcore module", b"snd-pcm module", b"tinycap tool"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InstallCoreTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(toolkit, "CORE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.dir / "modules").mkdir()
        (self.dir / "tools").mkdir()
        self.write(SOUNDCORE, "modules/soundcore.ko")
        self.write(SND_PCM, "modules/snd-pcm.ko")
        self.write(TINYCAP, "tools/tinycap")
        self.write_manifest()

    def write_manifest(self, **overrides) -> None:
        data = manifest_data(
            modules=[module("soundcore", sha256=digest(SOUNDCORE)), module("snd-pcm", ["soundcore"], digest(SND_PCM))],
            tools=[tool(sha256=digest(TINYCAP))],
            firmware=[],
        )
        self.manifest_bytes = self.write({**data, **overrides}).read_bytes()

    def sums(self) -> str:
        """What ``sha256sum`` prints on a camera that got every file intact."""
        hashes = {
            "manifest.json": digest(self.manifest_bytes),
            "modules/soundcore.ko": digest(SOUNDCORE),
            "modules/snd-pcm.ko": digest(SND_PCM),
            "tools/tinycap": digest(TINYCAP),
        }
        return "".join(f"{hash_}  {name}\n" for name, hash_ in hashes.items())

    def assert_refused_before_touching_the_camera(self, expected: str) -> None:
        remote = RecordingRemote()
        with self.assertRaisesRegex(ToolkitError, expected):
            install_core(remote)
        self.assertEqual(remote.steps, [])

    def test_uploads_every_file_to_its_path(self):
        remote = RecordingRemote(answer(), answer(self.sums()))
        install_core(remote)
        self.assertEqual(
            {path: data for path, data in remote.written.items() if path != STATE_PATH},
            {
                "/tmp/fuse_d/quak/toolkit/packs/core/manifest.json": self.manifest_bytes,
                "/tmp/fuse_d/quak/toolkit/packs/core/modules/soundcore.ko": SOUNDCORE,
                "/tmp/fuse_d/quak/toolkit/packs/core/modules/snd-pcm.ko": SND_PCM,
                "/tmp/fuse_d/quak/toolkit/packs/core/tools/tinycap": TINYCAP,
            },
        )

    def test_reports_the_pack_as_installed_and_not_loaded(self):
        state = install_core(RecordingRemote(answer(), answer(self.sums())))
        self.assertEqual(state, PackState(installed=True, loaded=False, path=PACK_PATH))

    def test_the_commands_are_these_and_no_others(self):
        remote = RecordingRemote(answer(), answer(self.sums()))
        install_core(remote)
        self.assertEqual(remote.commands, [PREPARE, VERIFY])

    def test_directories_first_then_uploads_then_verify_then_state(self):
        remote = RecordingRemote(answer(), answer(self.sums()))
        install_core(remote)
        self.assertEqual(
            remote.steps,
            [
                PREPARE,
                f"{PACK_PATH}/manifest.json",
                f"{PACK_PATH}/modules/soundcore.ko",
                f"{PACK_PATH}/modules/snd-pcm.ko",
                f"{PACK_PATH}/tools/tinycap",
                VERIFY,
                STATE_PATH,
            ],
        )

    def test_the_state_file(self):
        remote = RecordingRemote(answer(), answer(self.sums()))
        install_core(remote)
        self.assertEqual(json.loads(remote.written[STATE_PATH]), {"version": 1, "pack": "core", "files": INSTALLED})

    def test_toolkit_state_accepts_the_state_file(self):
        remote = RecordingRemote(answer(), answer(self.sums()))
        install_core(remote)
        reader = FakeRemote(answer(remote.written[STATE_PATH]), answer())
        self.assertEqual(toolkit_state(reader), PackState(installed=True, path=PACK_PATH))
        self.assertEqual(
            reader.commands[1],
            "root=/tmp/fuse_d/quak/toolkit/packs/core\n"
            "for f in manifest.json modules/soundcore.ko modules/snd-pcm.ko tools/tinycap; "
            'do [ -e "$root/$f" ] || echo "missing=$f"; done\n',
        )

    def test_a_login_notice_around_the_hashes_is_ignored(self):
        output = f"Welcome to the camera\n\n{self.sums()}goodbye  friend\n"
        state = install_core(RecordingRemote(answer(), answer(output)))
        self.assertTrue(state.installed)

    def test_no_core_dir(self):
        with mock.patch.object(toolkit, "CORE_DIR", self.dir / "absent"):
            self.assert_refused_before_touching_the_camera("could not read the pack manifest")

    def test_no_manifest(self):
        (self.dir / "manifest.json").unlink()
        self.assert_refused_before_touching_the_camera("could not read the pack manifest")

    def test_a_bad_manifest(self):
        self.write(b"{")
        self.assert_refused_before_touching_the_camera("bad pack manifest")

    def test_a_bundled_file_that_is_missing(self):
        for name in ("modules/snd-pcm.ko", "tools/tinycap"):
            with self.subTest(name):
                path = self.dir / name
                data = path.read_bytes()
                path.unlink()
                self.assert_refused_before_touching_the_camera(f"could not read .*{name}")
                path.write_bytes(data)

    def test_a_bundled_file_that_doesnt_match_its_manifest(self):
        for name in ("modules/snd-pcm.ko", "tools/tinycap"):
            with self.subTest(name):
                path = self.dir / name
                data = path.read_bytes()
                path.write_bytes(data + b"tampered")
                self.assert_refused_before_touching_the_camera(f"{name} doesn't match the sha256")
                path.write_bytes(data)

    def test_a_tool_without_a_hash_cant_be_verified(self):
        self.write_manifest(tools=["tinycap"])
        self.assert_refused_before_touching_the_camera("tool tinycap has no sha256")

    def test_a_hash_mismatch_on_the_camera(self):
        for name in INSTALLED:
            with self.subTest(name):
                output = "".join(
                    f"{OTHER_SHA}  {line.split()[1]}\n" if line.split()[1] == name else f"{line}\n"
                    for line in self.sums().splitlines()
                )
                remote = RecordingRemote(answer(), answer(output))
                with self.assertRaises(ToolkitError) as caught:
                    install_core(remote)
                self.assertIn(name, str(caught.exception))
                self.assertIn("failed the sha256 check", str(caught.exception))
                self.assertIn("quak lab toolkit install", str(caught.exception))
                self.assertNotIn(STATE_PATH, remote.written)

    def test_every_bad_file_is_named(self):
        output = f"{OTHER_SHA}  modules/soundcore.ko\n{OTHER_SHA}  tools/tinycap\n"
        with self.assertRaises(ToolkitError) as caught:
            install_core(RecordingRemote(answer(), answer(output)))
        for name in INSTALLED:
            with self.subTest(name):
                self.assertIn(name, str(caught.exception))

    def test_a_file_the_camera_doesnt_report_is_a_mismatch(self):
        output = "".join(f"{line}\n" for line in self.sums().splitlines() if not line.endswith("tools/tinycap"))
        remote = RecordingRemote(answer(), answer(output))
        with self.assertRaisesRegex(ToolkitError, "tools/tinycap in .* failed the sha256 check"):
            install_core(remote)
        self.assertNotIn(STATE_PATH, remote.written)

    def test_empty_output_is_a_mismatch(self):
        remote = RecordingRemote(answer(), answer())
        with self.assertRaisesRegex(ToolkitError, "failed the sha256 check"):
            install_core(remote)
        self.assertNotIn(STATE_PATH, remote.written)

    def test_a_directory_that_cant_be_made_stops_before_any_upload(self):
        remote = RecordingRemote(answer(exit_code=1, stderr=b"mkdir: Read-only file system\n"))
        with self.assertRaisesRegex(ToolkitError, f"could not prepare {PACK_PATH}: mkdir: Read-only file system"):
            install_core(remote)
        self.assertEqual(remote.written, {})

    def test_sha256sum_failing_writes_no_state(self):
        remote = RecordingRemote(answer(), answer(self.sums(), exit_code=1, stderr=b"sha256sum: tools/tinycap: I/O\n"))
        with self.assertRaisesRegex(ToolkitError, f"could not verify the files in {PACK_PATH}: sha256sum: tools"):
            install_core(remote)
        self.assertNotIn(STATE_PATH, remote.written)

    def test_no_link_at_any_step_raises_and_writes_no_state(self):
        ok, sums = answer(), answer(self.sums())
        for name, remote in (
            ("preparing", RecordingRemote(NetworkError("gone"))),
            ("uploading a module", RecordingRemote(ok, lost_at=f"{PACK_PATH}/modules/snd-pcm.ko")),
            ("uploading a tool", RecordingRemote(ok, lost_at=f"{PACK_PATH}/tools/tinycap")),
            ("verifying", RecordingRemote(ok, NetworkError("gone"))),
            ("writing the state", RecordingRemote(ok, sums, lost_at=STATE_PATH)),
        ):
            with self.subTest(name):
                with self.assertRaisesRegex(NetworkError, "gone"):
                    install_core(remote)
                self.assertNotIn(STATE_PATH, remote.written)

    def test_it_never_starts_a_process_of_its_own(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started a process")) as popen:
            install_core(RecordingRemote(answer(), answer(self.sums())))
        popen.assert_not_called()

    def test_every_tool_is_made_executable(self):
        tinymix = b"tinymix tool"
        self.write(tinymix, "tools/tinymix")
        self.write_manifest(tools=[tool(sha256=digest(TINYCAP)), tool("tinymix", digest(tinymix))])
        remote = RecordingRemote(answer(), answer(f"{self.sums()}{digest(tinymix)}  tools/tinymix\n"))
        install_core(remote)
        self.assertIn("&& chmod 755 tools/tinycap tools/tinymix && sha256sum ", remote.commands[1])

    def test_a_pack_without_tools_has_nothing_to_chmod(self):
        self.write_manifest(tools=[])
        remote = RecordingRemote(answer(), answer(self.sums()))
        self.assertTrue(install_core(remote).installed)
        self.assertNotIn("chmod", remote.commands[1])

    def test_a_chmod_that_fails_writes_no_state(self):
        remote = RecordingRemote(answer(), answer(exit_code=1, stderr=b"chmod: tools/tinycap: Read-only\n"))
        with self.assertRaisesRegex(ToolkitError, "chmod: tools/tinycap: Read-only"):
            install_core(remote)
        self.assertNotIn(STATE_PATH, remote.written)


READ_KERNEL = 'echo "kernel=$(uname -r)"\n'
STAGE = (
    "mkdir -p -m 0700 /dev/quak-toolkit && cd /tmp/fuse_d/quak/toolkit/packs/core/modules && "
    "cp soundcore.ko snd.ko snd-pcm.ko /dev/quak-toolkit/ && "
    "cd /dev/quak-toolkit && sha256sum soundcore.ko snd.ko snd-pcm.ko\n"
)
STAGED_SUMS = f"{SHA}  soundcore.ko\n{OTHER_SHA}  snd.ko\n{SHA}  snd-pcm.ko\n"
SET_FIRMWARE = (
    "[ ! -d /tmp/fuse_d/quak/toolkit/packs/core/firmware ] || "
    "printf %s /tmp/fuse_d/quak/toolkit/packs/core/firmware > /sys/module/firmware_class/parameters/path\n"
)
DELETE = "rm -f /tmp/fuse_d/quak/toolkit/state.json && rm -rf /tmp/fuse_d/quak/toolkit/packs/core\n"
BEFORE_INSMOD = [READ_STATE, CHECK_FILES, READ_KERNEL, STAGE, SET_FIRMWARE]


def insmod_script(file: str, kernel: str) -> str:
    return (
        f"[ ! -d /sys/module/{kernel} ] || exit 0\n"
        f"insmod /dev/quak-toolkit/{file}.ko || exit 1\n"
        f"[ -d /sys/module/{kernel} ] || {{ echo 'insmod ran, but /sys/module/{kernel} is not there' >&2; exit 1; }}\n"
    )


INSMOD_SOUNDCORE = insmod_script("soundcore", "soundcore")
INSMOD_SND = insmod_script("snd", "snd")
INSMOD_SND_PCM = insmod_script("snd-pcm", "snd_pcm")


def unload_script(*names: str) -> str:
    unload = "".join(f"[ ! -d /sys/module/{name} ] || rmmod {name} || echo failed={name}\n" for name in names)
    return f"trap '' HUP PIPE\n{unload}rm -rf /dev/quak-toolkit 2>/dev/null || true\n"


UNLOAD_ALL = unload_script("snd_pcm", "snd", "soundcore")


def loaded_files(remote: FakeRemote) -> list[str]:
    """The modules insmod was asked for, in order, however the commands around them are split up."""
    return re.findall(r"^insmod /dev/quak-toolkit/(\S+)\.ko", "".join(remote.commands), re.MULTILINE)


def up_to_insmod(sums: str = STAGED_SUMS) -> list[ExecResult]:
    """The answers of a healthy camera, up to the first insmod."""
    return [answer(state_json()), answer(), answer(f"kernel={KERNEL}\n"), answer(sums), answer()]


class ModulePackTestCase(TempDirTestCase):
    """The bundled pack is three modules in a chain, as far as these tests are concerned."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(toolkit, "CORE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_modules(module("soundcore"), module("snd", ["soundcore"], OTHER_SHA), module("snd-pcm", ["snd"]))

    def write_modules(self, *modules: dict) -> None:
        self.write(manifest_data(modules=list(modules), tools=[], firmware=[]))


class LoadCoreTests(ModulePackTestCase):
    def assert_nothing_loaded(self, remote: FakeRemote, commands: list[str]) -> None:
        self.assertEqual(remote.commands, commands)
        self.assertEqual(loaded_files(remote), [])

    def test_the_commands_are_these_in_this_order(self):
        remote = FakeRemote(*up_to_insmod(), answer(), answer(), answer())
        load_core(remote)
        self.assertEqual(remote.commands, [*BEFORE_INSMOD, INSMOD_SOUNDCORE, INSMOD_SND, INSMOD_SND_PCM])

    def test_reports_the_pack_as_loaded(self):
        state = load_core(FakeRemote(*up_to_insmod(), answer(), answer(), answer()))
        self.assertEqual(state, PackState(installed=True, loaded=True, path=PACK_PATH))

    def test_dependencies_come_first_and_the_manifest_orders_the_rest(self):
        self.write_modules(
            module("snd-pcm", ["snd_timer"]),
            module("usb-storage"),
            module("snd-timer", ["snd"]),
            module("snd"),
            module("mii"),
        )
        sums = "".join(f"{SHA}  {name}.ko\n" for name in ("usb-storage", "snd", "snd-timer", "snd-pcm", "mii"))
        remote = FakeRemote(*up_to_insmod(sums), *[answer() for _ in range(5)])
        load_core(remote)
        self.assertEqual(loaded_files(remote), ["usb-storage", "snd", "snd-timer", "snd-pcm", "mii"])

    def test_a_manifest_in_a_working_order_keeps_it(self):
        self.write_modules(module("b"), module("a"), module("d", ["b"]), module("c"))
        sums = "".join(f"{SHA}  {name}.ko\n" for name in "badc")
        remote = FakeRemote(*up_to_insmod(sums), *[answer() for _ in range(4)])
        load_core(remote)
        self.assertEqual(loaded_files(remote), ["b", "a", "d", "c"])

    def test_no_state_on_the_card(self):
        remote = FakeRemote(answer(exit_code=3))
        with self.assertRaisesRegex(ToolkitError, "not installed. Run `quak lab toolkit install`"):
            load_core(remote)
        self.assert_nothing_loaded(remote, [READ_STATE])

    def test_a_corrupt_state_file(self):
        remote = FakeRemote(answer("{"))
        with self.assertRaisesRegex(ToolkitError, "state.json is corrupt"):
            load_core(remote)
        self.assert_nothing_loaded(remote, [READ_STATE])

    def test_a_file_gone_from_the_card(self):
        remote = FakeRemote(answer(state_json()), answer("missing=ko/snd-pcm.ko\n"))
        with self.assertRaisesRegex(ToolkitError, "is missing ko/snd-pcm.ko"):
            load_core(remote)
        self.assert_nothing_loaded(remote, [READ_STATE, CHECK_FILES])

    def test_another_kernel_is_refused_before_anything_is_copied(self):
        remote = FakeRemote(answer(state_json()), answer(), answer("kernel=4.19.125\n"))
        with self.assertRaisesRegex(ToolkitError, "is for kernel 4.19.124, and the camera reports 4.19.125"):
            load_core(remote)
        self.assert_nothing_loaded(remote, [READ_STATE, CHECK_FILES, READ_KERNEL])

    def test_a_kernel_it_cant_learn_is_refused(self):
        for reply in (answer("kernel=\n"), answer(), answer(exit_code=1, stderr=b"sh: uname: not found\n")):
            with self.subTest(reply):
                remote = FakeRemote(answer(state_json()), answer(), reply)
                with self.assertRaisesRegex(ToolkitError, "unknown"):
                    load_core(remote)
                self.assert_nothing_loaded(remote, [READ_STATE, CHECK_FILES, READ_KERNEL])

    def test_a_login_notice_around_the_kernel_is_ignored(self):
        answers = up_to_insmod()
        answers[2] = answer(f"Welcome to the camera\nkernel={KERNEL}\ngoodbye\n")
        self.assertTrue(load_core(FakeRemote(*answers, answer(), answer(), answer())).loaded)

    def test_a_copy_that_fails_loads_nothing(self):
        answers = up_to_insmod()
        answers[3] = answer(exit_code=1, stderr=b"cp: write error: No space left on device\n")
        remote = FakeRemote(*answers)
        with self.assertRaisesRegex(ToolkitError, "could not copy the modules .* No space left on device"):
            load_core(remote)
        self.assert_nothing_loaded(remote, BEFORE_INSMOD[:4])

    def test_a_module_changed_since_the_install_loads_nothing(self):
        for name, sums in (
            ("a wrong hash", f"{SHA}  soundcore.ko\n{OTHER_SHA}  snd.ko\n{OTHER_SHA}  snd-pcm.ko\n"),
            ("no hash", f"{SHA}  soundcore.ko\n{OTHER_SHA}  snd.ko\n"),
        ):
            with self.subTest(name):
                remote = FakeRemote(*up_to_insmod(sums)[:4])
                with self.assertRaises(ToolkitError) as caught:
                    load_core(remote)
                self.assertIn(f"snd-pcm.ko from {PACK_PATH} failed the sha256 check", str(caught.exception))
                self.assertIn("quak lab toolkit install", str(caught.exception))
                self.assert_nothing_loaded(remote, BEFORE_INSMOD[:4])

    def test_a_firmware_path_that_cant_be_set_loads_nothing(self):
        answers = up_to_insmod()
        answers[4] = answer(exit_code=1, stderr=b"sh: can't create path: Permission denied\n")
        remote = FakeRemote(*answers)
        with self.assertRaisesRegex(ToolkitError, "could not set the firmware path.*Permission denied"):
            load_core(remote)
        self.assert_nothing_loaded(remote, BEFORE_INSMOD)

    def test_a_failed_insmod_unloads_in_reverse(self):
        failed = answer(exit_code=1, stderr=b"insmod: can't insert 'snd-pcm.ko': unknown symbol in module\n")
        remote = FakeRemote(*up_to_insmod(), answer(), answer(), failed, answer())
        with self.assertRaises(ToolkitError) as caught:
            load_core(remote)
        self.assertEqual(remote.commands, [*BEFORE_INSMOD, INSMOD_SOUNDCORE, INSMOD_SND, INSMOD_SND_PCM, UNLOAD_ALL])
        self.assertIn("could not load snd-pcm: insmod: can't insert", str(caught.exception))
        self.assertIn("Nothing was left loaded", str(caught.exception))

    def test_rolling_back_only_touches_what_the_load_reached(self):
        remote = FakeRemote(*up_to_insmod(), answer(), answer(exit_code=1), answer())
        with self.assertRaisesRegex(ToolkitError, "could not load snd: exit code 1"):
            load_core(remote)
        self.assertEqual(remote.commands[-1], unload_script("snd", "soundcore"))

    def test_a_module_the_kernel_doesnt_show_counts_as_failed(self):
        failed = answer(exit_code=1, stderr=b"insmod ran, but /sys/module/soundcore is not there\n")
        remote = FakeRemote(*up_to_insmod(), failed, answer())
        with self.assertRaisesRegex(ToolkitError, "/sys/module/soundcore is not there. Nothing was left loaded"):
            load_core(remote)
        self.assertEqual(remote.commands[-1], unload_script("soundcore"))

    def test_a_module_that_wont_come_back_out_is_named(self):
        stuck = answer("failed=soundcore\n", stderr=b"rmmod: can't unload 'soundcore': in use\n")
        remote = FakeRemote(*up_to_insmod(), answer(), answer(exit_code=1), stuck)
        with self.assertRaises(ToolkitError) as caught:
            load_core(remote)
        self.assertIn("could not unload soundcore: rmmod: can't unload", str(caught.exception))
        self.assertIn("quak lab toolkit unload", str(caught.exception))
        self.assertNotIn("Nothing was left loaded", str(caught.exception))

    def test_a_rollback_that_cant_run_doesnt_claim_it_worked(self):
        remote = FakeRemote(*up_to_insmod(), answer(), answer(exit_code=1), answer(exit_code=2, stderr=b"sh: no\n"))
        with self.assertRaises(ToolkitError) as caught:
            load_core(remote)
        self.assertIn("could not unload the modules: sh: no", str(caught.exception))
        self.assertNotIn("Nothing was left loaded", str(caught.exception))

    def test_a_link_that_comes_back_still_gets_the_rollback(self):
        remote = FakeRemote(*up_to_insmod(), answer(), NetworkError("timed out"), answer())
        with self.assertRaisesRegex(ToolkitError, "timed out. Nothing was left loaded"):
            load_core(remote)
        self.assertEqual(remote.commands[-1], unload_script("snd", "soundcore"))

    def test_a_link_lost_for_good_says_modules_may_remain(self):
        remote = FakeRemote(*up_to_insmod(), answer(), NetworkError("gone"), NetworkError("still gone"))
        with self.assertRaises(NetworkError) as caught:
            load_core(remote)
        self.assertIn("modules may still be loaded (still gone)", str(caught.exception))
        self.assertIn("quak lab toolkit unload", str(caught.exception))

    def test_a_link_lost_before_any_insmod_raises_with_no_rollback(self):
        for steps in range(5):
            with self.subTest(steps):
                remote = FakeRemote(*up_to_insmod()[:steps], NetworkError("gone"))
                with self.assertRaisesRegex(NetworkError, "^gone$"):
                    load_core(remote)
                self.assert_nothing_loaded(remote, BEFORE_INSMOD[: steps + 1])

    def test_a_bad_bundle_is_refused_before_touching_the_camera(self):
        self.write(b"{")
        remote = FakeRemote()
        with self.assertRaisesRegex(ToolkitError, "bad pack manifest"):
            load_core(remote)
        self.assertEqual(remote.commands, [])


class UnloadCoreTests(ModulePackTestCase):
    def test_last_loaded_goes_first(self):
        remote = FakeRemote(answer(), answer(state_json()), answer())
        self.assertEqual(unload_core(remote), PackState(installed=True, loaded=False, path=PACK_PATH))
        self.assertEqual(remote.commands, [UNLOAD_ALL, READ_STATE, CHECK_FILES])

    def test_a_module_already_gone_is_skipped(self):
        remote = FakeRemote(answer(), answer(state_json()), answer())
        unload_core(remote)
        for name in ("snd_pcm", "snd", "soundcore"):
            with self.subTest(name):
                self.assertIn(f"[ ! -d /sys/module/{name} ] || rmmod {name} ||", remote.commands[0])

    def test_the_camera_finishes_even_if_the_link_goes(self):
        remote = FakeRemote(NetworkError("gone"))
        with self.assertRaisesRegex(NetworkError, "gone"):
            unload_core(remote)
        self.assertTrue(remote.commands[0].startswith("trap '' HUP PIPE\n"))

    def test_it_works_without_an_install(self):
        remote = FakeRemote(answer(), answer(exit_code=3))
        self.assertEqual(unload_core(remote), PackState())
        self.assertEqual(remote.commands, [UNLOAD_ALL, READ_STATE])

    def test_a_corrupt_card_doesnt_stop_it_and_is_reported(self):
        remote = FakeRemote(answer(), answer("{"))
        state = unload_core(remote)
        self.assertFalse(state.loaded)
        self.assertIn("state.json is corrupt", state.error)
        self.assertEqual(remote.commands, [UNLOAD_ALL, READ_STATE])

    def test_modules_that_stay_are_named(self):
        stuck = answer("failed=snd_pcm\nfailed=snd\n", stderr=b"rmmod: can't unload 'snd_pcm': in use\n")
        remote = FakeRemote(stuck)
        with self.assertRaisesRegex(ToolkitError, "could not unload snd_pcm, snd: rmmod: can't unload 'snd_pcm'"):
            unload_core(remote)
        self.assertEqual(remote.commands, [UNLOAD_ALL])

    def test_a_script_that_cant_run_is_not_success(self):
        with self.assertRaisesRegex(ToolkitError, "could not unload the modules: sh: no"):
            unload_core(FakeRemote(answer(exit_code=2, stderr=b"sh: no\n")))


class RemoveCoreTests(ModulePackTestCase):
    def test_unloads_then_deletes_the_state_then_the_pack(self):
        remote = FakeRemote(answer(), answer())
        self.assertEqual(remove_core(remote), PackState())
        self.assertEqual(remote.commands, [UNLOAD_ALL, DELETE])

    def test_a_module_that_stays_stops_the_delete(self):
        remote = FakeRemote(answer("failed=snd\n", stderr=b"rmmod: in use\n"))
        with self.assertRaisesRegex(ToolkitError, "could not unload snd: rmmod: in use"):
            remove_core(remote)
        self.assertEqual(remote.commands, [UNLOAD_ALL])

    def test_a_delete_that_fails(self):
        remote = FakeRemote(answer(), answer(exit_code=1, stderr=b"rm: Read-only file system\n"))
        with self.assertRaisesRegex(ToolkitError, f"could not delete {PACK_PATH}: rm: Read-only file system"):
            remove_core(remote)

    def test_a_lost_link_raises_at_either_step(self):
        for answers in ((NetworkError("gone"),), (answer(), NetworkError("gone"))):
            with self.subTest(len(answers)):
                with self.assertRaisesRegex(NetworkError, "gone"):
                    remove_core(FakeRemote(*answers))


VENDOR_PATH = "/tmp/fuse_d/quak/toolkit/packs/vendor"
FIRMWARE = b"rtl8153 firmware"
BAD_SIGNATURE = b"Could not verify signature.\n"


class FakeVerifier:
    """Stands in for ssh-keygen: accepts exactly the manifest bytes it was told were signed, and logs when it ran."""

    def __init__(self, signed: bytes, log: list[str] | None = None, reply: ExecResult | None = None) -> None:
        self.signed = signed
        self.log = log if log is not None else []
        self.reply = reply
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv: list[str], data: bytes) -> ExecResult:
        self.calls.append((argv, data))
        self.log.append("verify")
        if self.reply is not None:
            return self.reply
        if data != self.signed:
            return answer(exit_code=255, stderr=BAD_SIGNATURE)
        return answer('Good "quak-pack" signature for quak-pack with ED25519 key SHA256:abc\n')


class SignedPackTestCase(TempDirTestCase):
    """A pack folder on disk with a signature beside its manifest, a signers file elsewhere, and a fake camera."""

    def setUp(self):
        super().setUp()
        self.pack = self.dir / "pack"
        for folder in ("modules", "tools", "firmware/rtl_nic"):
            (self.pack / folder).mkdir(parents=True)
        self.signers = self.dir / "allowed_signers"
        self.signers.write_text("quak-pack ssh-ed25519 AAAA\n")
        self.put("modules/soundcore.ko", SOUNDCORE)
        self.put("modules/snd-pcm.ko", SND_PCM)
        self.put("tools/tinycap", TINYCAP)
        self.put("firmware/rtl_nic/rtl8153a-2.fw", FIRMWARE)
        self.write_manifest()
        self.log: list[str] = []
        self.verifier = FakeVerifier(self.manifest_bytes, self.log)

    def put(self, name: str, data: bytes) -> None:
        (self.pack / name).write_bytes(data)

    def write_manifest(self, **overrides) -> None:
        data = manifest_data(
            id="vendor",
            modules=[module("soundcore", sha256=digest(SOUNDCORE)), module("snd-pcm", ["soundcore"], digest(SND_PCM))],
            tools=[tool(sha256=digest(TINYCAP))],
            firmware=["rtl_nic/rtl8153a-2.fw"],
            firmware_sha256={"rtl_nic/rtl8153a-2.fw": digest(FIRMWARE)},
        )
        if "firmware" in overrides:
            data["firmware_sha256"] = {name: digest(FIRMWARE) for name in overrides["firmware"]}
        self.manifest_bytes = json.dumps({**data, **overrides}).encode()
        self.put("manifest.json", self.manifest_bytes)
        self.put("manifest.sig", b"-----BEGIN SSH SIGNATURE-----\n")

    def sums(self) -> str:
        """What ``sha256sum`` prints on a camera that got every file intact."""
        hashes = {
            "manifest.json": digest(self.manifest_bytes),
            "modules/soundcore.ko": digest(SOUNDCORE),
            "modules/snd-pcm.ko": digest(SND_PCM),
            "tools/tinycap": digest(TINYCAP),
            "firmware/rtl_nic/rtl8153a-2.fw": digest(FIRMWARE),
        }
        return "".join(f"{hash_}  {name}\n" for name, hash_ in hashes.items())

    def remote(self, *answers: ExecResult | Exception) -> RecordingRemote:
        """A camera on the right kernel that takes the pack and reports ``self.sums()``, unless told otherwise."""
        remote = RecordingRemote(*(answers or (answer(f"kernel={KERNEL}\n"), answer(), answer(self.sums()))))
        original = remote.exec

        def logged(command, input=None, timeout=120.0):
            self.log.append("camera")
            return original(command, input, timeout)

        remote.exec = logged
        return remote

    def install(self, remote: RecordingRemote, **kwargs) -> PackState:
        return install_pack(remote, self.pack, self.signers, verifier=kwargs.pop("verifier", self.verifier), **kwargs)

    def assert_refused_before_touching_the_camera(self, expected: str, **kwargs) -> ToolkitError:
        remote = self.remote()
        with self.assertRaisesRegex(ToolkitError, expected) as caught:
            self.install(remote, **kwargs)
        self.assertEqual(remote.steps, [])
        self.assertEqual(remote.written, {})
        self.assertNotIn("camera", self.log)
        return caught.exception


class VerifyPackSignatureTests(SignedPackTestCase):
    def test_asks_ssh_keygen_to_verify_the_manifest_against_the_signature(self):
        raw = verify_pack_signature(self.pack, self.signers, verifier=self.verifier)
        self.assertEqual(raw, self.manifest_bytes)
        (argv, data), = self.verifier.calls
        self.assertEqual(
            argv,
            [
                "ssh-keygen", "-Y", "verify",
                "-f", str(self.signers.resolve()),
                "-I", "quak-pack",
                "-n", "quak-pack",
                "-s", str(self.pack.resolve() / "manifest.sig"),
            ],
        )  # fmt: skip
        self.assertEqual(data, self.manifest_bytes)

    def test_identity_and_namespace_can_be_named(self):
        verify_pack_signature(self.pack, self.signers, identity="me", namespace="mine", verifier=self.verifier)
        argv, _ = self.verifier.calls[0]
        self.assertEqual(argv[argv.index("-I") + 1], "me")
        self.assertEqual(argv[argv.index("-n") + 1], "mine")

    def test_identity_and_namespace_cant_be_empty(self):
        for kwargs in ({"identity": ""}, {"namespace": ""}):
            with self.subTest(kwargs):
                with self.assertRaisesRegex(ToolkitError, "can't be empty"):
                    verify_pack_signature(self.pack, self.signers, verifier=self.verifier, **kwargs)
        self.assertEqual(self.verifier.calls, [])

    def test_a_signature_ssh_keygen_rejects_is_refused_with_its_reason(self):
        with self.assertRaisesRegex(ToolkitError, r"not accepted.*Could not verify signature\..*pack was refused"):
            verify_pack_signature(self.pack, self.signers, verifier=FakeVerifier(b"another manifest"))

    def test_a_rejection_without_a_message_still_says_why(self):
        verifier = FakeVerifier(b"", reply=answer(exit_code=1))
        with self.assertRaisesRegex(ToolkitError, r"exit code 1"):
            verify_pack_signature(self.pack, self.signers, verifier=verifier)

    def test_a_missing_signature_is_refused_without_asking_ssh_keygen(self):
        (self.pack / "manifest.sig").unlink()
        with self.assertRaisesRegex(ToolkitError, "no manifest.sig.*unsigned pack is refused"):
            verify_pack_signature(self.pack, self.signers, verifier=self.verifier)
        self.assertEqual(self.verifier.calls, [])

    def test_a_missing_manifest_is_refused(self):
        (self.pack / "manifest.json").unlink()
        with self.assertRaisesRegex(ToolkitError, "could not read.*manifest.json"):
            verify_pack_signature(self.pack, self.signers, verifier=self.verifier)
        self.assertEqual(self.verifier.calls, [])

    def test_a_signers_file_that_is_not_there_is_refused(self):
        with self.assertRaisesRegex(ToolkitError, "allowed signers file .* was not found"):
            verify_pack_signature(self.pack, self.dir / "nobody", verifier=self.verifier)
        self.assertEqual(self.verifier.calls, [])

    def test_paths_may_be_strings(self):
        verify_pack_signature(str(self.pack), str(self.signers), verifier=self.verifier)
        self.assertEqual(len(self.verifier.calls), 1)


class RunSshKeygenTests(SignedPackTestCase):
    """The default verifier, with ``subprocess.run`` and ``shutil.which`` replaced: nothing real starts."""

    def run_verify(self, run: mock.Mock, which: str | None = "/usr/bin/ssh-keygen") -> bytes:
        with (
            mock.patch.object(toolkit.shutil, "which", return_value=which),
            mock.patch.object(toolkit.subprocess, "run", run),
            mock.patch("subprocess.Popen", side_effect=AssertionError("started a real process")),
        ):
            return verify_pack_signature(self.pack, self.signers)

    def test_runs_an_argument_list_without_a_shell_in_its_own_session_with_a_timeout(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=b"Good\n", stderr=b""))
        self.assertEqual(self.run_verify(run), self.manifest_bytes)
        run.assert_called_once()
        (argv,), kwargs = run.call_args
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:4], ["/usr/bin/ssh-keygen", "-Y", "verify", "-f"])
        self.assertEqual(kwargs["input"], self.manifest_bytes)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertGreater(kwargs["timeout"], 0)
        self.assertIs(kwargs["capture_output"], True)
        self.assertNotIn("shell", kwargs)

    def test_a_failing_ssh_keygen_refuses_the_pack(self):
        run = mock.Mock(return_value=mock.Mock(returncode=255, stdout=b"", stderr=BAD_SIGNATURE))
        with self.assertRaisesRegex(ToolkitError, "Could not verify signature"):
            self.run_verify(run)

    def test_an_ssh_keygen_that_takes_too_long_refuses_the_pack(self):
        run = mock.Mock(side_effect=toolkit.subprocess.TimeoutExpired("ssh-keygen", 20))
        with self.assertRaisesRegex(ToolkitError, "did not finish"):
            self.run_verify(run)

    def test_an_ssh_keygen_that_will_not_start_refuses_the_pack(self):
        run = mock.Mock(side_effect=PermissionError(13, "Permission denied"))
        with self.assertRaisesRegex(ToolkitError, "could not run ssh-keygen: Permission denied"):
            self.run_verify(run)

    def test_no_ssh_keygen_at_all_refuses_the_pack(self):
        run = mock.Mock()
        with self.assertRaisesRegex(ToolkitError, "ssh-keygen was not found"):
            self.run_verify(run, which=None)
        run.assert_not_called()


def vendor_prepare() -> str:
    return (
        "rm -f /tmp/fuse_d/quak/toolkit/state.json && mkdir -p "
        f"{VENDOR_PATH} {VENDOR_PATH}/firmware/rtl_nic {VENDOR_PATH}/modules {VENDOR_PATH}/tools\n"
    )


def vendor_verify() -> str:
    return (
        f"cd {VENDOR_PATH} && chmod 755 tools/tinycap && sha256sum manifest.json modules/soundcore.ko "
        "modules/snd-pcm.ko tools/tinycap firmware/rtl_nic/rtl8153a-2.fw\n"
    )


class InstallPackTests(SignedPackTestCase):
    def test_a_signed_pack_installs_and_reports_installed_not_loaded(self):
        state = self.install(self.remote())
        self.assertEqual(state, PackState(installed=True, loaded=False, path=VENDOR_PATH))

    def test_the_signature_is_checked_before_the_camera_is_asked_anything(self):
        self.install(self.remote())
        self.assertEqual(self.log[0], "verify")
        self.assertEqual(self.log.count("verify"), 1)
        self.assertEqual(set(self.log[1:]), {"camera"})

    def test_the_commands_are_these_and_no_others(self):
        remote = self.remote()
        self.install(remote)
        self.assertEqual(remote.commands, [READ_KERNEL, vendor_prepare(), vendor_verify()])

    def test_uploads_every_file_to_the_packs_folder(self):
        remote = self.remote()
        self.install(remote)
        self.assertEqual(
            {path: data for path, data in remote.written.items() if path != STATE_PATH},
            {
                f"{VENDOR_PATH}/manifest.json": self.manifest_bytes,
                f"{VENDOR_PATH}/modules/soundcore.ko": SOUNDCORE,
                f"{VENDOR_PATH}/modules/snd-pcm.ko": SND_PCM,
                f"{VENDOR_PATH}/tools/tinycap": TINYCAP,
                f"{VENDOR_PATH}/firmware/rtl_nic/rtl8153a-2.fw": FIRMWARE,
            },
        )

    def test_the_signature_file_stays_on_this_computer(self):
        remote = self.remote()
        self.install(remote)
        self.assertFalse([path for path in remote.written if path.endswith(".sig")])

    def test_the_state_file_is_written_last_and_toolkit_state_accepts_it(self):
        remote = self.remote()
        self.install(remote)
        self.assertEqual(remote.steps[-1], STATE_PATH)
        state = json.loads(remote.written[STATE_PATH])
        self.assertEqual(
            state,
            {
                "version": 1,
                "pack": "vendor",
                "files": [
                    "manifest.json",
                    "modules/soundcore.ko",
                    "modules/snd-pcm.ko",
                    "tools/tinycap",
                    "firmware/rtl_nic/rtl8153a-2.fw",
                ],
            },
        )
        found = toolkit_state(FakeRemote(answer(json.dumps(state)), answer()))
        self.assertEqual(found, PackState(installed=True, path=VENDOR_PATH))

    def test_it_loads_nothing(self):
        remote = self.remote()
        self.install(remote)
        for command in remote.commands:
            for word in ("insmod", "rmmod", "modprobe", "firmware_class", "/dev/quak-toolkit"):
                self.assertNotIn(word, command)

    def test_the_manifest_is_read_from_the_bytes_that_were_verified(self):
        # What gets installed is what was signed, even if the file changes while ssh-keygen runs.
        signed = self.manifest_bytes

        def swap_manifest_after_verifying(argv, data):
            result = self.verifier(argv, data)
            self.put("manifest.json", json.dumps(manifest_data(id="evil")).encode())
            return result

        remote = self.remote(answer(f"kernel={KERNEL}\n"), answer(), answer(self.sums()))
        state = install_pack(remote, self.pack, self.signers, verifier=swap_manifest_after_verifying)
        self.assertEqual(state.path, VENDOR_PATH)
        self.assertEqual(remote.written[f"{VENDOR_PATH}/manifest.json"], signed)

    def test_the_default_identity_and_namespace_are_used(self):
        self.install(self.remote())
        argv, _ = self.verifier.calls[0]
        self.assertEqual((argv[argv.index("-I") + 1], argv[argv.index("-n") + 1]), ("quak-pack", "quak-pack"))

    def test_a_pack_without_firmware(self):
        (self.pack / "firmware" / "rtl_nic" / "rtl8153a-2.fw").unlink()
        self.write_manifest(firmware=[])
        self.verifier.signed = self.manifest_bytes
        remote = self.remote()
        remote.answers[-1] = answer(
            "".join(line + "\n" for line in self.sums().splitlines() if "firmware" not in line)
        )
        self.install(remote)
        self.assertNotIn("firmware", remote.commands[-1])

    def test_a_pack_without_tools_has_nothing_to_chmod(self):
        self.write_manifest(tools=[])
        self.verifier.signed = self.manifest_bytes
        remote = self.remote()
        remote.answers[-1] = answer(self.sums())
        self.install(remote)
        self.assertNotIn("chmod", remote.commands[-1])


class InstallPackRefusalTests(SignedPackTestCase):
    def test_a_bad_signature_is_refused_before_anything_else(self):
        self.verifier.signed = b"something else was signed"
        self.assert_refused_before_touching_the_camera("signature .* was not accepted")
        self.assertEqual(len(self.verifier.calls), 1)

    def test_a_manifest_changed_after_signing_is_refused(self):
        self.put("manifest.json", json.dumps(manifest_data(id="vendor", version="9.9")).encode())
        self.assert_refused_before_touching_the_camera("Could not verify signature")

    def test_a_missing_signature_is_refused(self):
        (self.pack / "manifest.sig").unlink()
        self.assert_refused_before_touching_the_camera("no manifest.sig")
        self.assertEqual(self.verifier.calls, [])

    def test_a_missing_manifest_is_refused(self):
        (self.pack / "manifest.json").unlink()
        self.assert_refused_before_touching_the_camera("could not read")

    def test_a_missing_signers_file_is_refused(self):
        self.signers.unlink()
        self.assert_refused_before_touching_the_camera("allowed signers file")
        self.assertEqual(self.verifier.calls, [])

    def test_no_pack_folder_is_refused(self):
        remote = self.remote()
        with self.assertRaisesRegex(ToolkitError, "no manifest.sig"):
            install_pack(remote, self.dir / "nowhere", self.signers, verifier=self.verifier)
        self.assertEqual(remote.steps, [])

    def test_a_signed_but_invalid_manifest_is_refused(self):
        self.write_manifest(kernel="")
        self.verifier.signed = self.manifest_bytes
        self.assert_refused_before_touching_the_camera("bad pack manifest .*kernel is missing")

    def test_a_signed_manifest_that_is_not_json_is_refused(self):
        self.put("manifest.json", b"not json")
        self.verifier.signed = b"not json"
        self.assert_refused_before_touching_the_camera("not valid JSON")

    def test_a_module_that_doesnt_match_the_manifest_is_refused(self):
        self.put("modules/snd-pcm.ko", b"a different module")
        error = self.assert_refused_before_touching_the_camera("snd-pcm.ko doesn't match the sha256")
        self.assertIn(str(self.pack), str(error))

    def test_a_tool_that_doesnt_match_the_manifest_is_refused(self):
        self.put("tools/tinycap", b"a different tool")
        self.assert_refused_before_touching_the_camera("tinycap doesn't match the sha256")

    def test_a_file_the_pack_lacks_is_refused(self):
        (self.pack / "modules" / "soundcore.ko").unlink()
        self.assert_refused_before_touching_the_camera("could not read .*soundcore.ko")

    def test_firmware_the_pack_lacks_is_refused(self):
        (self.pack / "firmware" / "rtl_nic" / "rtl8153a-2.fw").unlink()
        self.assert_refused_before_touching_the_camera("could not read .*rtl8153a-2.fw")

    def test_a_tool_without_a_hash_is_refused(self):
        self.write_manifest(tools=["tinycap"])
        self.verifier.signed = self.manifest_bytes
        self.assert_refused_before_touching_the_camera("tool tinycap has no sha256")

    def test_a_pack_for_another_kernel_is_refused_before_anything_is_copied(self):
        remote = self.remote(answer("kernel=5.15.0\n"))
        with self.assertRaisesRegex(ToolkitError, f"is for kernel {KERNEL}, and the camera reports 5.15.0"):
            self.install(remote)
        self.assertEqual(remote.commands, [READ_KERNEL])
        self.assertEqual(remote.written, {})

    def test_a_camera_that_doesnt_say_its_kernel_is_a_refusal(self):
        for reply in (answer(""), answer("kernel=\n"), answer("something else\n")):
            with self.subTest(reply.stdout):
                remote = self.remote(reply)
                with self.assertRaisesRegex(ToolkitError, "camera reports \\(unknown\\)"):
                    self.install(remote)
                self.assertEqual(remote.written, {})

    def test_a_pack_whose_vermagic_is_not_its_kernels_is_refused(self):
        self.write_manifest(vermagic="5.15.0 SMP mod_unload aarch64")
        self.verifier.signed = self.manifest_bytes
        self.assert_refused_before_touching_the_camera("vermagic .* is not for kernel")

    def test_a_hash_that_doesnt_match_on_the_camera_writes_no_state(self):
        sums = self.sums().replace(digest(SND_PCM), OTHER_SHA)
        remote = self.remote(answer(f"kernel={KERNEL}\n"), answer(), answer(sums))
        with self.assertRaisesRegex(ToolkitError, r"modules/snd-pcm.ko in .*failed the sha256 check.*install-pack"):
            self.install(remote)
        self.assertNotIn(STATE_PATH, remote.written)

    def test_a_file_the_camera_doesnt_report_is_a_mismatch(self):
        sums = "".join(line + "\n" for line in self.sums().splitlines() if "firmware" not in line)
        remote = self.remote(answer(f"kernel={KERNEL}\n"), answer(), answer(sums))
        with self.assertRaisesRegex(ToolkitError, "firmware/rtl_nic/rtl8153a-2.fw in .*failed the sha256 check"):
            self.install(remote)
        self.assertNotIn(STATE_PATH, remote.written)

    def test_a_directory_that_cant_be_made_stops_before_any_upload(self):
        remote = self.remote(answer(f"kernel={KERNEL}\n"), answer(exit_code=1, stderr=b"mkdir: read-only\n"))
        with self.assertRaisesRegex(ToolkitError, f"could not prepare {VENDOR_PATH}: mkdir: read-only"):
            self.install(remote)
        self.assertEqual(remote.written, {})

    def test_sha256sum_failing_writes_no_state(self):
        remote = self.remote(answer(f"kernel={KERNEL}\n"), answer(), answer(exit_code=1, stderr=b"sha256sum: gone\n"))
        with self.assertRaisesRegex(ToolkitError, "could not verify the files.*sha256sum: gone"):
            self.install(remote)
        self.assertNotIn(STATE_PATH, remote.written)

    def test_a_lost_link_raises_and_writes_no_state(self):
        for answers in ((NetworkError("gone"),), (answer(f"kernel={KERNEL}\n"), NetworkError("gone"))):
            with self.subTest(len(answers)):
                remote = self.remote(*answers)
                with self.assertRaisesRegex(NetworkError, "gone"):
                    self.install(remote)
                self.assertNotIn(STATE_PATH, remote.written)
        remote = self.remote()
        remote.lost_at = f"{VENDOR_PATH}/tools/tinycap"
        with self.assertRaisesRegex(NetworkError, "gone"):
            self.install(remote)
        self.assertNotIn(STATE_PATH, remote.written)

    def test_nothing_real_runs_with_a_fake_verifier(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started a process")) as popen:
            self.install(self.remote())
        popen.assert_not_called()


class FakeResponse:
    """What an opener returns: bytes to read, and a record of whether it was closed."""

    def __init__(self, data: bytes = b"") -> None:
        self.stream = io.BytesIO(data)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def close(self) -> None:
        self.closed = True


class EndlessResponse(FakeResponse):
    """A server that never stops sending, and a count of what it was asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.served = 0

    def read(self, size: int = -1) -> bytes:
        self.served += size
        return b"x" * size


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> FakeResponse:
        self.calls.append((url, timeout))
        return self.response


def refusing_opener(url: str, timeout: float):
    raise AssertionError(f"tried to fetch {url}")


def zip_info(name: str, mode: int = 0) -> zipfile.ZipInfo:
    """A zip entry with the Unix mode a maker would record in it: ``stat.S_IFLNK | 0o777`` makes a symlink."""
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    return info


def make_zip(path: Path, entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> Path:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # one test repeats a name on purpose
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zipped:
            for name, data in entries:
                zipped.writestr(name, data)
    return path


GOOD_ENTRIES: list[tuple[str | zipfile.ZipInfo, bytes]] = [
    ("manifest.json", b"{}"),
    ("manifest.sig", b"-----BEGIN SSH SIGNATURE-----\n"),
    ("modules/soundcore.ko", SOUNDCORE),
    ("tools/tinycap", TINYCAP),
    ("firmware/rtl_nic/rtl8153a-2.fw", FIRMWARE),
]


class CheckPackSourceTests(unittest.TestCase):
    def test_https_and_file_urls_are_accepted(self):
        for url in ("https://packs.example/vendor.zip", "HTTPS://packs.example/v.zip", "file:///home/me/vendor.zip"):
            with self.subTest(url):
                self.assertEqual(check_pack_source(url, SHA), SHA)
        check_pack_source("file://localhost/home/me/vendor.zip", SHA)

    def test_http_and_everything_else_is_refused(self):
        for url in (
            "http://packs.example/vendor.zip",
            "HTTP://packs.example/vendor.zip",
            "ftp://packs.example/vendor.zip",
            "data:text/plain,hi",
            "javascript:alert(1)",
            "//packs.example/vendor.zip",
            "packs.example/vendor.zip",
            "/home/me/vendor.zip",
            "",
        ):
            with self.subTest(url):
                with self.assertRaises(ToolkitError):
                    check_pack_source(url, SHA)
        with self.assertRaisesRegex(ToolkitError, "only fetched from an https:// or file:// URL"):
            check_pack_source("http://packs.example/vendor.zip", SHA)

    def test_a_url_that_is_not_what_will_be_fetched_is_refused(self):
        for url in (" https://packs.example/v.zip", "https://packs.example/v.zip\n", "https://packs.example/a b.zip"):
            with self.subTest(url):
                with self.assertRaisesRegex(ToolkitError, "not a usable URL"):
                    check_pack_source(url, SHA)

    def test_a_url_without_a_host_or_for_another_computer_is_refused(self):
        with self.assertRaisesRegex(ToolkitError, "names no host"):
            check_pack_source("https:///vendor.zip", SHA)
        with self.assertRaisesRegex(ToolkitError, "not on this computer"):
            check_pack_source("file://packs.example/vendor.zip", SHA)

    def test_the_hash_is_64_hex_digits_and_comes_back_in_lowercase(self):
        self.assertEqual(check_pack_source("https://packs.example/v.zip", SHA.upper() + "\n"), SHA)
        for bad in ("", "abc", SHA + "0", SHA[:-1] + "g", "sha256:" + SHA):
            with self.subTest(bad):
                with self.assertRaisesRegex(ToolkitError, "64 hex digits"):
                    check_pack_source("https://packs.example/v.zip", bad)


class DownloadPackTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.dest = self.dir / "download"
        self.data = bytes(range(256)) * 300  # more than one read of the download
        self.sha = digest(self.data)

    def files(self) -> list[str]:
        return sorted(path.name for path in self.dest.iterdir()) if self.dest.exists() else []

    def test_a_file_url_is_downloaded_into_the_folder(self):
        source = self.write(self.data, "source.zip")
        got = download_pack(source.as_uri(), self.sha, self.dest)
        self.assertEqual(got, self.dest / f"pack-{self.sha[:16]}.zip")
        self.assertEqual(got.read_bytes(), self.data)
        self.assertEqual(self.files(), [got.name], "no temporary file may stay")

    def test_the_name_never_comes_from_the_url(self):
        source = self.write(self.data, "evil;name.zip")
        self.assertEqual(download_pack(source.as_uri(), self.sha, self.dest).parent, self.dest)

    def test_an_https_url_goes_through_the_opener_with_the_timeout(self):
        opener = FakeOpener(FakeResponse(self.data))
        got = download_pack("https://packs.example/v.zip", self.sha, self.dest, opener=opener)
        self.assertEqual(opener.calls, [("https://packs.example/v.zip", 30)])
        self.assertEqual(got.read_bytes(), self.data)
        self.assertTrue(opener.response.closed)
        again = FakeOpener(FakeResponse(self.data))
        download_pack("https://packs.example/v.zip", self.sha, self.dest, timeout=5, opener=again)
        self.assertEqual(again.calls[-1][1], 5)

    def test_an_uppercase_hash_is_the_same_hash(self):
        got = download_pack("https://packs.example/v.zip", self.sha.upper(), self.dest, opener=FakeOpener(FakeResponse(self.data)))
        self.assertEqual(got.read_bytes(), self.data)

    def test_a_hash_that_doesnt_match_is_refused_and_leaves_nothing(self):
        source = self.write(self.data, "source.zip")
        with self.assertRaisesRegex(ToolkitError, f"has sha256 {self.sha}, not the {OTHER_SHA} that was asked for"):
            download_pack(source.as_uri(), OTHER_SHA, self.dest)
        self.assertEqual(self.files(), [])

    def test_a_refused_download_leaves_an_earlier_one_alone(self):
        first = download_pack("https://packs.example/v.zip", self.sha, self.dest, opener=FakeOpener(FakeResponse(self.data)))
        with self.assertRaises(ToolkitError):
            download_pack("https://packs.example/w.zip", OTHER_SHA, self.dest, opener=FakeOpener(FakeResponse(b"other")))
        self.assertEqual(self.files(), [first.name])
        self.assertEqual(first.read_bytes(), self.data)

    def test_http_is_refused_without_fetching_anything(self):
        for url in ("http://packs.example/v.zip", "ftp://packs.example/v.zip", "data:text/plain,hi"):
            with self.subTest(url):
                with self.assertRaisesRegex(ToolkitError, "https:// or file://"):
                    download_pack(url, self.sha, self.dest, opener=refusing_opener)
        self.assertFalse(self.dest.exists())

    def test_a_bad_hash_is_refused_without_fetching_anything(self):
        with self.assertRaisesRegex(ToolkitError, "64 hex digits"):
            download_pack("https://packs.example/v.zip", "abc", self.dest, opener=refusing_opener)

    def test_a_download_of_exactly_the_limit_is_accepted(self):
        data = b"y" * 1000
        got = download_pack("https://packs.example/v.zip", digest(data), self.dest, max_bytes=1000, opener=FakeOpener(FakeResponse(data)))
        self.assertEqual(got.read_bytes(), data)

    def test_a_download_a_byte_over_the_limit_is_refused_and_leaves_nothing(self):
        data = b"y" * 1001
        opener = FakeOpener(FakeResponse(data))
        with self.assertRaisesRegex(ToolkitError, "more than the .* MiB a pack may be"):
            download_pack("https://packs.example/v.zip", digest(data), self.dest, max_bytes=1000, opener=opener)
        self.assertEqual(self.files(), [])
        self.assertTrue(opener.response.closed)

    def test_a_server_that_never_stops_is_cut_off_at_the_limit(self):
        response = EndlessResponse()
        with self.assertRaisesRegex(ToolkitError, "more than the"):
            download_pack("https://packs.example/v.zip", SHA, self.dest, max_bytes=200_000, opener=FakeOpener(response))
        self.assertLessEqual(response.served, 200_001)
        self.assertEqual(self.files(), [])
        self.assertTrue(response.closed)

    def test_the_default_limit_is_64_mib(self):
        self.assertEqual(toolkit.MAX_PACK_BYTES, 64 * 1024 * 1024)

    def test_a_download_that_fails_is_a_toolkit_error_that_says_why(self):
        for error, expected in (
            (urllib.error.URLError("no route to host"), "no route to host"),
            (urllib.error.HTTPError("https://x/v.zip", 404, "Not Found", {}, None), "HTTP 404 Not Found"),
            (TimeoutError("timed out"), "timed out"),
            (ConnectionResetError(104, "Connection reset by peer"), "Connection reset by peer"),
            (http.client.IncompleteRead(b"abc"), "IncompleteRead"),
        ):
            with self.subTest(expected):
                with self.assertRaisesRegex(ToolkitError, f"could not download https://x/v.zip: .*{expected}"):
                    download_pack("https://x/v.zip", SHA, self.dest, opener=mock.Mock(side_effect=error))
                self.assertEqual(self.files(), [])

    def test_a_connection_that_drops_midway_leaves_nothing(self):
        response = FakeResponse(self.data)
        reads = iter([self.data[:1000]])

        def read(size=-1):
            try:
                return next(reads)
            except StopIteration:
                raise ConnectionResetError(104, "Connection reset by peer") from None

        response.read = read
        with self.assertRaisesRegex(ToolkitError, "Connection reset by peer"):
            download_pack("https://x/v.zip", self.sha, self.dest, opener=FakeOpener(response))
        self.assertEqual(self.files(), [])
        self.assertTrue(response.closed)

    def test_a_file_that_is_not_there(self):
        with self.assertRaisesRegex(ToolkitError, "could not download file://.*absent.zip"):
            download_pack((self.dir / "absent.zip").as_uri(), self.sha, self.dest)

    def test_a_folder_that_cant_be_made(self):
        blocker = self.write(b"a file", "blocker")
        with self.assertRaisesRegex(ToolkitError, "could not download"):
            download_pack("https://x/v.zip", self.sha, blocker / "inside", opener=FakeOpener(FakeResponse(self.data)))

    def test_the_real_opener_has_no_http_ftp_or_data(self):
        for url in ("http://127.0.0.1:9/v.zip", "ftp://127.0.0.1:9/v.zip", "data:text/plain,hi"):
            with self.subTest(url):
                with mock.patch("socket.create_connection", side_effect=AssertionError("connected")):
                    with self.assertRaises(urllib.error.URLError):
                        toolkit._open_url(url, 1)

    def test_a_redirect_may_only_go_to_https(self):
        handler = toolkit._HttpsOnlyRedirect()
        request = urllib.request.Request("https://packs.example/v.zip")
        for target in ("http://evil.example/v.zip", "ftp://evil.example/v.zip", "file:///etc/passwd"):
            with self.subTest(target):
                with self.assertRaisesRegex(urllib.error.HTTPError, "which is not https"):
                    handler.redirect_request(request, None, 302, "Found", {}, target)
        again = handler.redirect_request(request, None, 302, "Found", {}, "https://mirror.example/v.zip")
        self.assertEqual(again.full_url, "https://mirror.example/v.zip")


class ExtractPackTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.dest = self.dir / "out"

    def zip(self, *extra: tuple[str | zipfile.ZipInfo, bytes], skip: tuple[str, ...] = ()) -> Path:
        entries = [entry for entry in GOOD_ENTRIES if entry[0] not in skip]
        return make_zip(self.dir / "pack.zip", [*entries, *extra])

    def assert_refused(self, archive: Path, expected: str, **kwargs) -> None:
        with self.assertRaisesRegex(ToolkitError, expected):
            extract_pack(archive, self.dest, **kwargs)
        self.assertFalse(self.dest.exists(), "nothing may be written for a zip that is refused")

    def test_a_pack_is_unpacked_to_the_folder_and_that_is_returned(self):
        self.assertEqual(extract_pack(self.zip(), self.dest), self.dest)
        for name, data in GOOD_ENTRIES:
            with self.subTest(name):
                self.assertEqual((self.dest / name).read_bytes(), data)
        self.assertEqual(sorted(path.name for path in self.dest.iterdir()), ["firmware", "manifest.json", "manifest.sig", "modules", "tools"])

    def test_strings_are_paths_too(self):
        self.assertEqual(extract_pack(str(self.zip()), str(self.dest)), self.dest)

    def test_folder_entries_inside_the_layout_are_fine(self):
        archive = self.zip(("modules/", b""), ("firmware/rtl_nic/", b""))
        extract_pack(archive, self.dest)
        self.assertTrue((self.dest / "modules").is_dir())

    def test_an_existing_empty_folder_is_used(self):
        self.dest.mkdir()
        extract_pack(self.zip(), self.dest)
        self.assertTrue((self.dest / "manifest.json").is_file())

    def test_a_folder_that_isnt_empty_is_refused_and_left_alone(self):
        self.dest.mkdir()
        (self.dest / "keep").write_bytes(b"mine")
        with self.assertRaisesRegex(ToolkitError, "is not empty"):
            extract_pack(self.zip(), self.dest)
        self.assertEqual([path.name for path in self.dest.iterdir()], ["keep"])

    def test_absolute_paths_are_refused(self):
        for name in ("/etc/passwd", "/modules/evil.ko", "\\evil", "C:/evil", "c:evil"):
            with self.subTest(name):
                self.assert_refused(self.zip((name, b"x")), "absolute path")

    def test_traversal_is_refused(self):
        for name in ("../evil", "modules/../../evil", "modules/..\\..\\evil", "tools/../../evil", "a/../manifest.json", ".."):
            with self.subTest(name):
                self.assert_refused(self.zip((name, b"x")), r"climbs out of the pack with \.\.")
        self.assertFalse((self.dir / "evil").exists())

    def test_symlinks_are_refused(self):
        for name in ("modules/snd.ko", "tools/tinycap2", "manifest.json", "firmware/link"):
            with self.subTest(name):
                archive = self.zip((zip_info(name, stat.S_IFLNK | 0o777), "/etc/passwd".encode()))
                self.assert_refused(archive, "is a symlink")

    def test_a_symlink_in_place_of_a_folder_is_refused(self):
        archive = self.zip((zip_info("tools2", stat.S_IFLNK | 0o777), b"/"), (zip_info("modules", stat.S_IFLNK | 0o777), b"/"))
        self.assert_refused(archive, "is a symlink")

    def test_other_special_files_are_refused(self):
        for kind in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
            with self.subTest(kind):
                self.assert_refused(self.zip((zip_info("tools/special", kind | 0o666), b"")), "neither a file nor a folder")

    def test_an_encrypted_entry_is_refused(self):
        info = zip_info("modules/secret.ko")
        info.flag_bits |= 0x1
        with self.assertRaisesRegex(ToolkitError, "is encrypted"):
            toolkit._check_zip_member(info)

    def test_anything_outside_the_layout_is_refused(self):
        for name in (
            "README",
            "evil.sh",
            "bin/sh",
            "pack/manifest.json",
            "manifest.json.bak",
            "manifest.json/x",
            "modules",
            "tools",
            "Modules/x.ko",
            "firmware",
            "etc/",
        ):
            with self.subTest(name):
                self.assert_refused(self.zip((name, b"x")), "outside a pack's layout")

    def test_names_that_are_not_plain_paths_are_refused(self):
        for name in ("./manifest.json", "modules/a b.ko", "modules/x;reboot", "modules//x.ko", "modules/.hidden", "modules/x\nreboot", "modules/-rf"):
            with self.subTest(name):
                self.assert_refused(self.zip((name, b"x")), "not a plain path inside the pack")

    def test_a_pack_needs_a_manifest(self):
        self.assert_refused(self.zip(skip=("manifest.json",)), "no manifest.json")

    def test_a_pack_needs_a_signature(self):
        self.assert_refused(self.zip(skip=("manifest.sig",)), "no manifest.sig, and an unsigned pack is refused")

    def test_a_folder_called_manifest_json_is_not_the_manifest(self):
        archive = make_zip(self.dir / "pack.zip", [("manifest.json/", b""), ("manifest.sig", b"x")])
        self.assert_refused(archive, "outside a pack's layout")

    def test_a_name_twice_is_refused(self):
        self.assert_refused(self.zip(("modules/soundcore.ko", b"another")), "'modules/soundcore.ko' twice")

    def test_too_many_bytes_are_refused_before_any_is_written(self):
        self.assert_refused(self.zip(("modules/big.ko", b"0" * 5000)), "unpacks to more than", max_bytes=2000)

    def test_a_highly_compressed_file_is_judged_by_what_it_unpacks_to(self):
        path = self.dir / "bomb.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("manifest.json", b"{}")
            zipped.writestr("manifest.sig", b"x")
            zipped.writestr("modules/zeros.ko", bytes(3_000_000))
        self.assertLess(path.stat().st_size, 10_000)
        self.assert_refused(path, "unpacks to more than", max_bytes=1_000_000)

    def test_too_many_files_are_refused(self):
        with mock.patch.object(toolkit, "MAX_PACK_FILES", 3):
            self.assert_refused(self.zip(), "more than the 3 a pack may have")

    def test_modes_in_the_zip_decide_nothing(self):
        archive = self.zip((zip_info("tools/setuid", stat.S_IFREG | 0o4777), b"#!/bin/sh\n"))
        extract_pack(archive, self.dest)
        self.assertEqual(stat.S_IMODE((self.dest / "tools" / "setuid").stat().st_mode) & 0o7000, 0)

    def test_something_that_is_not_a_zip_is_refused(self):
        bad = self.write(b"this is not a zip", "bad.zip")
        with self.assertRaisesRegex(ToolkitError, "could not unpack the pack .*bad.zip"):
            extract_pack(bad, self.dest)

    def test_a_zip_that_is_not_there_is_refused(self):
        with self.assertRaisesRegex(ToolkitError, "could not unpack the pack .*absent.zip"):
            extract_pack(self.dir / "absent.zip", self.dest)

    def test_a_file_that_doesnt_match_its_checksum_is_refused(self):
        archive = self.zip()
        raw = bytearray(archive.read_bytes())
        raw[raw.index(SOUNDCORE)] ^= 0x01
        archive.write_bytes(bytes(raw))
        with self.assertRaisesRegex(ToolkitError, "could not unpack the pack .*Bad CRC-32"):
            extract_pack(archive, self.dest)


class InstallPackUrlTests(SignedPackTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir()
        patcher = mock.patch.object(tempfile, "tempdir", str(self.tmp))  # where the downloaded pack is kept
        patcher.start()
        self.addCleanup(patcher.stop)
        self.zip_pack()

    def zip_pack(self, extra: tuple[tuple[str | zipfile.ZipInfo, bytes], ...] = (), skip: tuple[str, ...] = ()) -> None:
        entries = [
            (path.relative_to(self.pack).as_posix(), path.read_bytes())
            for path in sorted(self.pack.rglob("*"))
            if path.is_file()
        ]
        entries = [entry for entry in entries if entry[0] not in skip]
        self.archive = make_zip(self.dir / "pack.zip", [*entries, *extra])
        self.sha = sha256_file(self.archive)
        self.url = self.archive.as_uri()

    def install_url(self, remote: RecordingRemote, url: str | None = None, sha256: str | None = None, **kwargs) -> PackState:
        return install_pack_url(
            remote,
            url or self.url,
            sha256 or self.sha,
            self.signers,
            verifier=kwargs.pop("verifier", self.verifier),
            **kwargs,
        )

    def assert_url_refused(self, expected: str, url: str | None = None, sha256: str | None = None, **kwargs) -> None:
        remote = self.remote()
        with self.assertRaisesRegex(ToolkitError, expected):
            self.install_url(remote, url, sha256, **kwargs)
        self.assertEqual(remote.steps, [])
        self.assertEqual(remote.written, {})
        self.assertNotIn("camera", self.log)
        self.assertEqual(list(self.tmp.iterdir()), [], "what was fetched must be deleted")

    def test_a_pack_from_a_file_url_installs_and_reports_installed_not_loaded(self):
        remote = self.remote()
        state = self.install_url(remote)
        self.assertEqual(state, PackState(installed=True, loaded=False, path=VENDOR_PATH))
        self.assertEqual(remote.commands, [READ_KERNEL, vendor_prepare(), vendor_verify()])
        self.assertEqual(
            {path: data for path, data in remote.written.items() if path != STATE_PATH},
            {
                f"{VENDOR_PATH}/manifest.json": self.manifest_bytes,
                f"{VENDOR_PATH}/modules/soundcore.ko": SOUNDCORE,
                f"{VENDOR_PATH}/modules/snd-pcm.ko": SND_PCM,
                f"{VENDOR_PATH}/tools/tinycap": TINYCAP,
                f"{VENDOR_PATH}/firmware/rtl_nic/rtl8153a-2.fw": FIRMWARE,
            },
        )
        self.assertEqual(remote.steps[-1], STATE_PATH)
        self.assertFalse([path for path in remote.written if path.endswith(".sig")])

    def test_the_signature_is_checked_on_the_unpacked_pack_before_the_camera_is_asked_anything(self):
        self.install_url(self.remote())
        self.assertEqual(self.log[0], "verify")
        self.assertEqual(self.log.count("verify"), 1)
        self.assertEqual(set(self.log[1:]), {"camera"})
        argv, data = self.verifier.calls[0]
        self.assertEqual(data, self.manifest_bytes)
        signature = argv[argv.index("-s") + 1]
        self.assertTrue(signature.startswith(str(self.tmp.resolve())), signature)
        self.assertTrue(signature.endswith("/pack/manifest.sig"))

    def test_what_was_fetched_is_deleted_afterwards(self):
        self.install_url(self.remote())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_an_https_url_goes_through_the_opener(self):
        opener = FakeOpener(FakeResponse(self.archive.read_bytes()))
        state = self.install_url(self.remote(), "https://packs.example/vendor.zip", opener=opener)
        self.assertTrue(state.installed)
        self.assertEqual(opener.calls, [("https://packs.example/vendor.zip", 30)])

    def test_a_hash_that_doesnt_match_is_refused_before_anything_is_unpacked_or_verified(self):
        self.assert_url_refused(f"has sha256 {self.sha}, not the {OTHER_SHA}", sha256=OTHER_SHA)
        self.assertEqual(self.verifier.calls, [])

    def test_http_is_refused_before_anything_is_fetched(self):
        self.assert_url_refused("https:// or file://", url="http://packs.example/vendor.zip", opener=refusing_opener)
        self.assertEqual(self.verifier.calls, [])

    def test_a_bad_hash_is_refused_before_anything_is_fetched(self):
        self.assert_url_refused("64 hex digits", sha256="abc", opener=refusing_opener)

    def test_a_zip_that_climbs_out_is_refused_before_the_signature_is_checked(self):
        self.zip_pack(extra=(("../evil", b"x"),))
        self.assert_url_refused(r"climbs out of the pack with \.\.")
        self.assertEqual(self.verifier.calls, [])
        self.assertFalse((self.dir / "evil").exists())

    def test_a_zip_with_a_symlink_is_refused(self):
        self.zip_pack(extra=((zip_info("modules/link.ko", stat.S_IFLNK | 0o777), b"/etc/passwd"),))
        self.assert_url_refused("is a symlink")
        self.assertEqual(self.verifier.calls, [])

    def test_a_zip_with_an_absolute_path_is_refused(self):
        self.zip_pack(extra=(("/tmp/evil", b"x"),))
        self.assert_url_refused("absolute path")
        self.assertEqual(self.verifier.calls, [])

    def test_a_zip_with_something_outside_the_layout_is_refused(self):
        self.zip_pack(extra=(("install.sh", b"#!/bin/sh\nreboot\n"),))
        self.assert_url_refused("outside a pack's layout")

    def test_a_zip_without_a_signature_is_refused_without_asking_ssh_keygen(self):
        self.zip_pack(skip=("manifest.sig",))
        self.assert_url_refused("no manifest.sig, and an unsigned pack is refused")
        self.assertEqual(self.verifier.calls, [])

    def test_a_zip_without_a_manifest_is_refused(self):
        self.zip_pack(skip=("manifest.json",))
        self.assert_url_refused("no manifest.json")

    def test_a_signature_that_isnt_accepted_is_refused_before_the_camera(self):
        self.verifier.signed = b"something else was signed"
        self.assert_url_refused("signature .* was not accepted")
        self.assertEqual(len(self.verifier.calls), 1)

    def test_a_module_that_doesnt_match_the_signed_manifest_is_refused(self):
        self.put("modules/snd-pcm.ko", b"a different module")
        self.zip_pack()
        self.assert_url_refused("snd-pcm.ko doesn't match the sha256")

    def test_a_pack_for_another_kernel_is_refused_before_anything_is_copied(self):
        remote = self.remote(answer("kernel=5.15.0\n"))
        with self.assertRaisesRegex(ToolkitError, f"is for kernel {KERNEL}, and the camera reports 5.15.0"):
            self.install_url(remote)
        self.assertEqual(remote.commands, [READ_KERNEL])
        self.assertEqual(remote.written, {})
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_a_hash_that_fails_on_the_camera_names_this_command_to_retry(self):
        sums = self.sums().replace(digest(SND_PCM), OTHER_SHA)
        remote = self.remote(answer(f"kernel={KERNEL}\n"), answer(), answer(sums))
        with self.assertRaisesRegex(ToolkitError, r"modules/snd-pcm.ko in .*failed the sha256 check.*install-pack-url"):
            self.install_url(remote)
        self.assertNotIn(STATE_PATH, remote.written)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_a_lost_link_raises_and_deletes_what_was_fetched(self):
        with self.assertRaisesRegex(NetworkError, "gone"):
            self.install_url(self.remote(NetworkError("gone")))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_it_loads_nothing(self):
        remote = self.remote()
        self.install_url(remote)
        for command in remote.commands:
            for word in ("insmod", "rmmod", "modprobe", "firmware_class", "/dev/quak-toolkit"):
                self.assertNotIn(word, command)

    def test_nothing_real_runs_with_a_fake_verifier(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started a process")) as popen:
            self.install_url(self.remote())
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

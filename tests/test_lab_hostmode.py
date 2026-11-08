"""USB host-mode switching against a fake camera: no hardware, no ssh, no shell, no real sysfs.

The fake records each command and answers from a queue, so these tests pin what Quak asks the camera to
do and how it reads the answers. Each switch is one remote script, so "the command sequence" is the
order of the lines inside it, and those are compared as literals rather than against the module's own
constants: a wrong path has to fail here. Nothing runs the scripts, so whether BusyBox does what the
text says is still for a real camera to show.
"""

import re
import unittest
from unittest import mock

from quak.core.errors import HostModeError, NetworkError
from quak.lab import hostmode
from quak.lab.hostmode import ENABLE_SCRIPT, STATE_SCRIPT, disable_script, requires_network_switch
from quak.lab.models import HostModeState
from quak.ssh import transport
from quak.ssh.models import Endpoint, ExecResult
from quak.ssh.transport import UsbNet

UDC = "19880000.dwc3"
MARKER_SWITCHING = '{"version":1,"phase":"switching"}'
MARKER_HOST = '{"version":1,"phase":"host"}'

HOST_FIELDS = {"mode": "1", "udc": "", "hubs": ("usb1", "usb2"), "gpsv": "T", "gpusb": "T"}
HALF_HOST_FIELDS = {**HOST_FIELDS, "hubs": ("usb1",)}

ENABLE_STEPS = ["pause-gpsv", "pause-gpUsb", "detach", "unbind", "hostmode=1", "bind", "xhci", "marker"]
RESTORE_STEPS = ["unbind", "hostmode=2", "bind", "udc-ready", "attach", "resume-gpUsb", "resume-gpsv"]

MODULES = ["usbcore", "xhci-hcd", "snd_usb_audio"]  # in load order
BAD_MODULES = ["x; reboot", "$(reboot)", "`reboot`", "a b", "../x", "x\n", "", "dd if=/dev/zero of=/dev/sda"]

ENABLE_BODY = [
    f"mkdir -p /tmp/fuse_d/quak && put $MARKER '{MARKER_SWITCHING}' || {{ echo failed=marker; exit 3; }}",
    'step pause-gpsv signal STOP gpsv "$RUNNING" || back_out',
    'step pause-gpUsb signal STOP gpusb "$RUNNING" || back_out',
    '[ -z "$(cat $UDC 2>/dev/null)" ] || step detach put $UDC "" || back_out',
    "[ ! -e $DRV/$GLUE ] || step unbind put $DRV/unbind $GLUE || back_out",
    "step hostmode=1 put $HM 1 || back_out",
    "step bind put $DRV/bind $GLUE || back_out",
    "step xhci wait_path 15 /sys/bus/usb/devices/usb1 /sys/bus/usb/devices/usb2 || back_out",
    f"step marker put $MARKER '{MARKER_HOST}' || back_out",
]

RESTORE_BODY = [
    "rc=0",
    'if [ "$(cat $HM 2>/dev/null)" != 2 ] || [ ! -e $DRV/$GLUE ]; then',
    "[ ! -e $DRV/$GLUE ] || step unbind put $DRV/unbind $GLUE || rc=1",
    "step hostmode=2 put $HM 2 || rc=1",
    "step bind put $DRV/bind $GLUE || rc=1",
    "step udc-ready wait_path 10 /sys/class/udc/$CTL || rc=1",
    "sleep 2",
    "fi",
    '[ "$(cat $UDC 2>/dev/null)" = $CTL ] || step attach put $UDC $CTL || rc=1',
    '[ -z "$(gpusb "$STOPPED")" ] || step resume-gpUsb signal CONT gpusb "$STOPPED" || rc=1',
    '[ -z "$(gpsv "$STOPPED")" ] || step resume-gpsv signal CONT gpsv "$STOPPED" || rc=1',
    "[ $rc != 0 ] || rm -f $MARKER",
    "return $rc",
]

UNMOUNT_BODY = [
    """for v in $(awk 'index($2,"/tmp/quak/mnt/")==1 {print $2}' /proc/mounts); do""",
    '  step "umount:$v" umount "$v" || exit 3',
    "done",
]

# Substrings on purpose, so gpUpdate or reflash can't slip past a word boundary. Only dd is short enough to need one.
FORBIDDEN = re.compile(r"mkfs|mke2fs|format|flash|update|\bdd\b|fdisk|parted|wipefs", re.IGNORECASE)


def state_text(mode="2", *, glue=True, udc=UDC, hubs=(), gpsv="S", gpusb="S", marker=None) -> str:
    """What STATE_SCRIPT prints, line for line. ``gpsv`` and ``gpusb`` are ps STAT values, None if not running."""
    lines = [f"hostmode={mode}"]
    if glue:
        lines.append("glue=1")
    lines.append(f"udc={udc}")
    lines += [f"hub={hub}" for hub in hubs]
    if gpsv is not None:
        lines.append(f"proc=gpsv {gpsv}")
    if gpusb is not None:
        lines.append(f"proc=gpUsb {gpusb}")
    if marker is not None:
        lines.append(f"marker={marker}")
    return "\n".join(lines) + "\n"


def answer(text: str = "", exit_code: int = 0, stderr: bytes = b"") -> ExecResult:
    return ExecResult(exit_code=exit_code, stdout=text.encode(), stderr=stderr)


def device_state(marker=None) -> ExecResult:
    return answer(state_text(marker=marker))


def host_state(marker=MARKER_HOST) -> ExecResult:
    return answer(state_text(**HOST_FIELDS, marker=marker))


def half_host_state(marker=MARKER_SWITCHING) -> ExecResult:
    return answer(state_text(**HALF_HOST_FIELDS, marker=marker))


def unsupported_state() -> ExecResult:
    return answer(state_text(mode=""))


def switch_output(*done: str, failed=(), exit_code: int = 0, stderr: bytes = b"") -> ExecResult:
    lines = [f"done={step}" for step in done] + [f"failed={step}" for step in failed]
    return answer("".join(f"{line}\n" for line in lines), exit_code, stderr)


def body(script: str) -> list[str]:
    """The lines a switch script runs itself, after the variables and functions they all share."""
    return script.partition("back_out() { restore && exit 1; exit 2; }\n")[2].splitlines()


def restore_function(script: str) -> list[str]:
    inside = script.partition("restore() {\n")[2].partition("\n}\n")[0]
    return [line.strip() for line in inside.splitlines()]


def marker_writes(script: str) -> list[str]:
    return re.findall(r"put \$MARKER '([^']*)'", script)


class FakeRemote:
    """A ``Remote`` that records each command and answers from a queue. An exception in the queue is raised."""

    def __init__(self, *answers: ExecResult | Exception) -> None:
        self.answers = list(answers)
        self.commands: list[str] = []
        self.timeouts: list[float] = []

    def exec(self, command: str, input: bytes | None = None, timeout: float = 120.0) -> ExecResult:
        self.commands.append(command)
        self.timeouts.append(timeout)
        if not self.answers:
            raise AssertionError(f"a command nobody expected: {command[:80]!r}")
        reply = self.answers.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def read_file(self, path: str) -> bytes:
        raise AssertionError(f"host mode should do everything in one command, but read {path}")

    def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"host mode should do everything in one command, but wrote {path}")


class StateTests(unittest.TestCase):
    def read(self, **fields) -> HostModeState:
        return hostmode.state(FakeRemote(answer(state_text(**fields))))

    def test_whole_host_mode(self):
        self.assertEqual(
            self.read(**HOST_FIELDS, marker=MARKER_HOST),
            HostModeState(
                enabled=True,
                supported=True,
                hostmode="1",
                devices=["usb1", "usb2"],
                partial=False,
                udc="",
                paused=["gpsv", "gpUsb"],
                marker="host",
            ),
        )

    def test_whole_device_mode(self):
        self.assertEqual(
            self.read(),
            HostModeState(
                enabled=False,
                supported=True,
                hostmode="2",
                devices=[],
                partial=False,
                udc=UDC,
                paused=[],
                marker="",
            ),
        )

    def test_forced_peripheral_counts_as_device_mode(self):
        got = self.read(mode="0")
        self.assertFalse(got.enabled)
        self.assertFalse(got.partial)

    def test_anything_between_the_two_is_partial(self):
        cases = {
            "switch on, one root hub": {"mode": "1", "udc": "", "hubs": ("usb1",)},
            "switch on, the other root hub": {"mode": "1", "udc": "", "hubs": ("usb2",)},
            "switch on, no root hubs": {"mode": "1", "udc": ""},
            "glue unbound": {"glue": False},
            "gadget detached": {"udc": ""},
            "gpsv stopped": {"gpsv": "T"},
            "gpUsb stopped": {"gpusb": "T"},
        }
        for name, fields in cases.items():
            with self.subTest(name):
                got = self.read(**fields)
                self.assertTrue(got.supported)
                self.assertFalse(got.enabled)
                self.assertTrue(got.partial)

    def test_paused_lists_only_stopped_processes(self):
        cases = [
            ({"gpsv": "T", "gpusb": "S"}, ["gpsv"]),
            ({"gpsv": "S", "gpusb": "T"}, ["gpUsb"]),
            ({"gpsv": "T", "gpusb": "T"}, ["gpsv", "gpUsb"]),
            ({"gpsv": "T<", "gpusb": "S"}, ["gpsv"]),
            ({"gpsv": "R", "gpusb": "D"}, []),
            ({"gpsv": None, "gpusb": None}, []),
        ]
        for fields, paused in cases:
            with self.subTest(fields):
                self.assertEqual(self.read(**fields).paused, paused)

    def test_marker_phase(self):
        cases = [
            (MARKER_SWITCHING, "switching"),
            (MARKER_HOST, "host"),
            ('{"version":1,"phase":"other"}', "unknown"),
            ('{"version":1,"phase":["host"]}', "unknown"),
            ('{"version":1}', "unknown"),
            ('["host"]', "unknown"),
            ("null", "unknown"),
            ('{"phase":"ho', "unknown"),
            ("", "unknown"),
            (None, ""),
        ]
        for raw, phase in cases:
            with self.subTest(raw):
                self.assertEqual(self.read(marker=raw).marker, phase)

    def test_no_switch_on_this_camera(self):
        got = self.read(mode="")
        self.assertFalse(got.supported)
        self.assertFalse(got.enabled)
        self.assertFalse(got.partial)
        self.assertIn("/sys/module/dwc3_milbeaut/parameters/hostmode", got.error)

    def test_failed_read_goes_in_error(self):
        got = hostmode.state(FakeRemote(answer(exit_code=1, stderr=b"sh: broken\n")))
        self.assertFalse(got.supported)
        self.assertIn("sh: broken", got.error)
        got = hostmode.state(FakeRemote(answer(exit_code=7)))
        self.assertIn("exit code 7", got.error)

    def test_lost_link_raises(self):
        with self.assertRaises(NetworkError):
            hostmode.state(FakeRemote(NetworkError("gone")))

    def test_sends_the_one_snapshot(self):
        remote = FakeRemote(device_state())
        hostmode.state(remote)
        self.assertEqual(remote.commands, [STATE_SCRIPT])

    def test_snapshot_reads_the_right_places(self):
        for piece in (
            "cat /sys/module/dwc3_milbeaut/parameters/hostmode",
            "/sys/bus/platform/drivers/dwc3-milbeaut/1ba60000.usb31",
            "cat /sys/kernel/config/usb_gadget/gopro_gadget/UDC",
            "for hub in usb1 usb2;",
            "/sys/bus/usb/devices/$hub",
            "-v sv=/usr/local/gopro/bin/gpsv -v usb=/usr/local/gopro/bin/gpUsb",
            "cat /tmp/fuse_d/quak/hostmode.json",
        ):
            with self.subTest(piece):
                self.assertIn(piece, STATE_SCRIPT)

    def test_snapshot_changes_nothing(self):
        self.assertNotIn(">", STATE_SCRIPT.replace("2>/dev/null", ""))
        self.assertNotRegex(STATE_SCRIPT, r"\b(kill|rm|rmmod|insmod|mount|umount|mkdir|sleep|put)\b")


class ScriptPreludeTests(unittest.TestCase):
    """What every switch script relies on, whichever way it goes."""

    SCRIPTS = {"enable": ENABLE_SCRIPT, "disable": disable_script(), "with modules": disable_script(MODULES)}

    def test_survives_losing_the_link(self):
        for name, script in self.SCRIPTS.items():
            with self.subTest(name):
                self.assertTrue(script.startswith("trap '' HUP PIPE\n"))

    def test_points_at_the_camera_paths(self):
        variables = {
            "HM": "/sys/module/dwc3_milbeaut/parameters/hostmode",
            "DRV": "/sys/bus/platform/drivers/dwc3-milbeaut",
            "GLUE": "1ba60000.usb31",
            "UDC": "/sys/kernel/config/usb_gadget/gopro_gadget/UDC",
            "CTL": UDC,
            "MARKER": "/tmp/fuse_d/quak/hostmode.json",
            "SV": "/usr/local/gopro/bin/gpsv",
            "USB": "/usr/local/gopro/bin/gpUsb",
            "RUNNING": "'^[^T]'",
            "STOPPED": "'^T'",
        }
        for name, script in self.SCRIPTS.items():
            for variable, value in variables.items():
                with self.subTest(script=name, variable=variable):
                    self.assertIn(f"\n{variable}={value}\n", script)

    def test_signals_reach_only_the_usb_daemon_and_its_own_supervisor(self):
        gpsv = """gpsv() { ps | awk -v sv=$SV -v usb=$USB -v s="$1" '$5==sv && $6==usb && $4~s {print $1}'; }"""
        gpusb = """gpusb() { ps | awk -v usb=$USB -v s="$1" '$5==usb && NF==5 && $4~s {print $1}'; }"""
        for name, script in self.SCRIPTS.items():
            with self.subTest(name):
                self.assertIn(gpsv, script)
                self.assertIn(gpusb, script)

    def test_restore_runs_the_exact_sequence(self):
        for name, script in self.SCRIPTS.items():
            with self.subTest(name):
                self.assertEqual(restore_function(script), RESTORE_BODY)


class EnableTests(unittest.TestCase):
    def test_script_runs_the_exact_sequence(self):
        self.assertEqual(body(ENABLE_SCRIPT), ENABLE_BODY)

    def test_reads_switches_and_reads_back(self):
        remote = FakeRemote(device_state(), switch_output(*ENABLE_STEPS), host_state())
        result = hostmode.enable(remote)
        self.assertEqual(remote.commands, [STATE_SCRIPT, ENABLE_SCRIPT, STATE_SCRIPT])
        self.assertTrue(result.state.enabled)
        self.assertEqual(result.state.marker, "host")
        self.assertEqual(result.steps, ENABLE_STEPS)
        self.assertEqual(result.message, "Host mode is on")

    def test_switch_gets_longer_than_the_scripts_own_waits(self):
        remote = FakeRemote(device_state(), switch_output(*ENABLE_STEPS), host_state())
        hostmode.enable(remote)
        self.assertEqual(remote.timeouts[1], hostmode.SWITCH_TIMEOUT)
        self.assertGreater(hostmode.SWITCH_TIMEOUT, 15 + 10 + 2)  # xHCI, then backing out: UDC and its settle

    def test_output_other_than_steps_is_ignored(self):
        noise = answer("Welcome to the camera\ndone=unbind\nsh: warning=loud\nfailed\ndone=bind\n")
        result = hostmode.enable(FakeRemote(device_state(), noise, host_state()))
        self.assertEqual(result.steps, ["unbind", "bind"])

    def test_already_on_sends_no_switch(self):
        remote = FakeRemote(host_state())
        result = hostmode.enable(remote)
        self.assertEqual(remote.commands, [STATE_SCRIPT])
        self.assertTrue(result.state.enabled)
        self.assertEqual(result.steps, [])
        self.assertIn("already on", result.message)

    def test_refused_without_the_switch(self):
        remote = FakeRemote(unsupported_state())
        with self.assertRaisesRegex(HostModeError, "no USB host-mode switch"):
            hostmode.enable(remote)
        self.assertEqual(remote.commands, [STATE_SCRIPT])

    def test_failure_says_where_it_stopped_and_how_the_camera_was_left(self):
        cases = [
            (1, "The camera was put back as it was"),
            (2, "only partly restored"),
            (3, "Nothing was switched"),
            (9, "quak lab host recover"),
        ]
        for code, outcome in cases:
            with self.subTest(code):
                remote = FakeRemote(
                    device_state(),
                    switch_output("pause-gpsv", failed=["unbind"], exit_code=code, stderr=b"sh: write error\n"),
                )
                with self.assertRaises(HostModeError) as caught:
                    hostmode.enable(remote)
                message = str(caught.exception)
                self.assertIn(outcome, message)
                self.assertIn("unbind", message)
                self.assertIn("sh: write error", message)
                self.assertEqual(remote.commands, [STATE_SCRIPT, ENABLE_SCRIPT])

    def test_failure_without_a_named_step_gives_the_exit_code(self):
        remote = FakeRemote(device_state(), switch_output(exit_code=9))
        with self.assertRaisesRegex(HostModeError, "exit code 9"):
            hostmode.enable(remote)

    def test_link_lost_during_the_switch_points_at_recover(self):
        remote = FakeRemote(device_state(), NetworkError("Connection reset"))
        with self.assertRaises(HostModeError) as caught:
            hostmode.enable(remote)
        self.assertIn("Connection reset", str(caught.exception))
        self.assertIn("quak lab host recover", str(caught.exception))
        self.assertEqual(remote.commands, [STATE_SCRIPT, ENABLE_SCRIPT])

    def test_link_lost_after_the_switch_keeps_what_was_done(self):
        result = hostmode.enable(FakeRemote(device_state(), switch_output(*ENABLE_STEPS), NetworkError("gone")))
        self.assertEqual(result.steps, ENABLE_STEPS)
        self.assertIn("could not be read back", result.state.error)
        self.assertIn("gone", result.state.error)

    def test_switch_that_did_not_take_is_an_error(self):
        cases = [(device_state(), "hostmode=2"), (half_host_state(MARKER_HOST), "usb1")]
        for after, detail in cases:
            with self.subTest(detail):
                remote = FakeRemote(device_state(), switch_output(*ENABLE_STEPS), after)
                with self.assertRaises(HostModeError) as caught:
                    hostmode.enable(remote)
                self.assertIn(detail, str(caught.exception))
                self.assertIn("quak lab host recover", str(caught.exception))


class DisableTests(unittest.TestCase):
    def test_script_unmounts_then_unloads_in_reverse_then_restores(self):
        self.assertEqual(
            body(disable_script(MODULES)),
            [
                *UNMOUNT_BODY,
                "if [ -d /sys/module/snd_usb_audio ]; then step rmmod:snd_usb_audio rmmod snd_usb_audio; fi",
                "if [ -d /sys/module/xhci_hcd ]; then step rmmod:xhci_hcd rmmod xhci_hcd; fi",
                "if [ -d /sys/module/usbcore ]; then step rmmod:usbcore rmmod usbcore; fi",
                "restore || exit 2",
            ],
        )

    def test_script_without_modules_unloads_nothing(self):
        self.assertEqual(body(disable_script()), [*UNMOUNT_BODY, "restore || exit 2"])

    def test_modules_can_come_from_any_iterable(self):
        self.assertEqual(disable_script(iter(MODULES)), disable_script(MODULES))

    def test_reads_switches_and_reads_back(self):
        remote = FakeRemote(host_state(), switch_output(*RESTORE_STEPS), device_state())
        result = hostmode.disable(remote, modules=MODULES)
        self.assertEqual(remote.commands, [STATE_SCRIPT, disable_script(MODULES), STATE_SCRIPT])
        self.assertFalse(result.state.enabled)
        self.assertFalse(result.state.partial)
        self.assertEqual(result.state.marker, "")
        self.assertEqual(result.steps, RESTORE_STEPS)
        self.assertEqual(result.message, f"Restored: {', '.join(RESTORE_STEPS)}")

    def test_already_in_device_mode_says_so(self):
        result = hostmode.disable(FakeRemote(device_state(MARKER_HOST), switch_output(), device_state()))
        self.assertEqual(result.steps, [])
        self.assertIn("Nothing needed restoring", result.message)

    def test_module_that_stays_loaded_does_not_stop_the_switch(self):
        remote = FakeRemote(host_state(), switch_output(*RESTORE_STEPS, failed=["rmmod:usbcore"]), device_state())
        result = hostmode.disable(remote, modules=MODULES)
        self.assertEqual(result.steps, RESTORE_STEPS)
        self.assertIn("Failed, which doesn't stop the switch: rmmod:usbcore", result.message)

    def test_busy_volume_stops_everything(self):
        volume = "umount:/tmp/quak/mnt/sda1"
        remote = FakeRemote(host_state(), switch_output(failed=[volume], exit_code=3, stderr=b"umount: busy\n"))
        with self.assertRaises(HostModeError) as caught:
            hostmode.disable(remote)
        self.assertIn(volume, str(caught.exception))
        self.assertIn("umount: busy", str(caught.exception))
        self.assertIn("Nothing was switched", str(caught.exception))
        self.assertEqual(remote.commands, [STATE_SCRIPT, disable_script()])

    def test_partial_restore_points_at_recover_and_reboot(self):
        remote = FakeRemote(host_state(), switch_output("unbind", failed=["bind"], exit_code=2))
        with self.assertRaises(HostModeError) as caught:
            hostmode.disable(remote)
        self.assertIn("bind", str(caught.exception))
        self.assertIn("only partly restored", str(caught.exception))
        self.assertIn("quak lab host recover", str(caught.exception))
        self.assertIn("reboot", str(caught.exception))

    def test_restore_that_did_not_take_is_an_error(self):
        cases = {
            "still host": answer(state_text(**HOST_FIELDS)),
            "gadget detached": answer(state_text(udc="")),
            "still paused": answer(state_text(gpsv="T")),
        }
        for name, after in cases.items():
            with self.subTest(name):
                remote = FakeRemote(host_state(), switch_output(*RESTORE_STEPS), after)
                with self.assertRaisesRegex(HostModeError, "quak lab host recover"):
                    hostmode.disable(remote)

    def test_link_lost_during_the_switch_points_at_recover(self):
        remote = FakeRemote(host_state(), NetworkError("Connection reset"))
        with self.assertRaisesRegex(HostModeError, "quak lab host recover"):
            hostmode.disable(remote)
        self.assertEqual(remote.commands, [STATE_SCRIPT, disable_script()])

    def test_link_lost_after_the_switch_keeps_what_was_done(self):
        result = hostmode.disable(FakeRemote(host_state(), switch_output(*RESTORE_STEPS), NetworkError("gone")))
        self.assertEqual(result.steps, RESTORE_STEPS)
        self.assertIn("could not be read back", result.state.error)

    def test_refused_without_the_switch(self):
        remote = FakeRemote(unsupported_state())
        with self.assertRaisesRegex(HostModeError, "no USB host-mode switch"):
            hostmode.disable(remote)
        self.assertEqual(remote.commands, [STATE_SCRIPT])

    def test_a_bad_module_name_never_reaches_the_camera(self):
        for name in BAD_MODULES:
            with self.subTest(name):
                remote = FakeRemote()
                with self.assertRaisesRegex(HostModeError, "not a kernel module name"):
                    hostmode.disable(remote, modules=["usbcore", name])
                self.assertEqual(remote.commands, [])


class RecoverTests(unittest.TestCase):
    def test_leaves_alone_what_it_should(self):
        cases = {
            "no marker, device mode": (state_text(), "Nothing to recover"),
            "no marker, whole host mode": (state_text(**HOST_FIELDS), "Nothing to recover"),
            "no marker, between modes": (state_text(udc=""), "left alone"),
            "marker, whole host mode": (state_text(**HOST_FIELDS, marker=MARKER_HOST), "nothing to recover"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name):
                remote = FakeRemote(answer(text))
                result = hostmode.recover(remote, modules=MODULES)
                self.assertEqual(remote.commands, [STATE_SCRIPT])
                self.assertEqual(result.steps, [])
                self.assertIn(message, result.message)

    def test_runs_the_full_restore_when_a_marker_was_left(self):
        cases = {
            "switching, looks like device mode": state_text(marker=MARKER_SWITCHING),
            "switching, gadget detached": state_text(udc="", marker=MARKER_SWITCHING),
            "switching, whole host mode": state_text(**HOST_FIELDS, marker=MARKER_SWITCHING),
            "host, a root hub gone": state_text(**HALF_HOST_FIELDS, marker=MARKER_HOST),
            "host, off after a reboot": state_text(marker=MARKER_HOST),
            "unreadable, whole host mode": state_text(**HOST_FIELDS, marker="garbage"),
        }
        for name, text in cases.items():
            with self.subTest(name):
                remote = FakeRemote(answer(text), switch_output(*RESTORE_STEPS), device_state())
                result = hostmode.recover(remote, modules=MODULES)
                self.assertEqual(remote.commands, [STATE_SCRIPT, disable_script(MODULES), STATE_SCRIPT])
                self.assertEqual(result.steps, RESTORE_STEPS)
                self.assertEqual(result.message, f"Restored: {', '.join(RESTORE_STEPS)}")
                self.assertEqual(result.state.marker, "")

    def test_old_marker_on_a_rebooted_camera_only_clears_the_marker(self):
        remote = FakeRemote(device_state(MARKER_HOST), switch_output(), device_state())
        result = hostmode.recover(remote)
        self.assertEqual(result.steps, [])
        self.assertIn("Nothing needed restoring", result.message)

    def test_restore_that_did_not_take_is_an_error(self):
        remote = FakeRemote(half_host_state(), switch_output(*RESTORE_STEPS), half_host_state())
        with self.assertRaisesRegex(HostModeError, "quak lab host recover"):
            hostmode.recover(remote)

    def test_failed_restore_is_an_error(self):
        remote = FakeRemote(half_host_state(), switch_output(failed=["attach"], exit_code=2))
        with self.assertRaisesRegex(HostModeError, "attach"):
            hostmode.recover(remote)

    def test_link_lost_during_the_restore_points_at_recover(self):
        remote = FakeRemote(half_host_state(), NetworkError("Connection reset"))
        with self.assertRaisesRegex(HostModeError, "quak lab host recover"):
            hostmode.recover(remote)

    def test_refused_without_the_switch(self):
        remote = FakeRemote(unsupported_state())
        with self.assertRaisesRegex(HostModeError, "no USB host-mode switch"):
            hostmode.recover(remote)
        self.assertEqual(remote.commands, [STATE_SCRIPT])

    def test_a_bad_module_name_never_reaches_the_camera(self):
        for name in BAD_MODULES:
            with self.subTest(name):
                remote = FakeRemote()
                with self.assertRaisesRegex(HostModeError, "not a kernel module name"):
                    hostmode.recover(remote, modules=[name])
                self.assertEqual(remote.commands, [])


class MarkerTests(unittest.TestCase):
    def test_enable_writes_switching_before_anything_changes_and_host_last(self):
        self.assertEqual(marker_writes(ENABLE_SCRIPT), [MARKER_SWITCHING, MARKER_HOST])
        lines = body(ENABLE_SCRIPT)
        self.assertIn(MARKER_SWITCHING, lines[0])
        self.assertIn(MARKER_HOST, lines[-1])

    def test_what_enable_writes_reads_back_as_its_phase(self):
        for written, phase in zip(marker_writes(ENABLE_SCRIPT), ("switching", "host"), strict=True):
            with self.subTest(phase):
                self.assertEqual(hostmode.state(FakeRemote(device_state(written))).marker, phase)

    def test_no_marker_stops_the_switch_before_it_starts(self):
        remote = FakeRemote(device_state(), switch_output(failed=["marker"], exit_code=3))
        with self.assertRaisesRegex(HostModeError, r"\(marker\). Nothing was switched"):
            hostmode.enable(remote)

    def test_restore_only_removes_the_marker_once_everything_is_back(self):
        self.assertEqual(restore_function(disable_script())[-2:], ["[ $rc != 0 ] || rm -f $MARKER", "return $rc"])

    def test_disable_writes_no_marker(self):
        self.assertEqual(marker_writes(disable_script(MODULES)), [])

    def test_backing_out_of_enable_reports_how_the_restore_went(self):
        self.assertIn("back_out() { restore && exit 1; exit 2; }\n", ENABLE_SCRIPT)

    def test_enable_refuses_an_unfinished_switch(self):
        cases = {
            "switching, looks like device mode": state_text(marker=MARKER_SWITCHING),
            "switching, between modes": state_text(udc="", marker=MARKER_SWITCHING),
            "switching, whole host mode": state_text(**HOST_FIELDS, marker=MARKER_SWITCHING),
            "host, a root hub gone": state_text(**HALF_HOST_FIELDS, marker=MARKER_HOST),
            "unreadable, between modes": state_text(udc="", marker="garbage"),
        }
        for name, text in cases.items():
            with self.subTest(name):
                remote = FakeRemote(answer(text))
                with self.assertRaisesRegex(HostModeError, "quak lab host recover"):
                    hostmode.enable(remote)
                self.assertEqual(remote.commands, [STATE_SCRIPT])

    def test_old_marker_on_a_whole_device_mode_camera_does_not_block_enable(self):
        for marker in (MARKER_HOST, "garbage"):
            with self.subTest(marker):
                remote = FakeRemote(device_state(marker), switch_output(*ENABLE_STEPS), host_state())
                result = hostmode.enable(remote)
                self.assertEqual(remote.commands, [STATE_SCRIPT, ENABLE_SCRIPT, STATE_SCRIPT])
                self.assertTrue(result.state.enabled)


class RequiresNetworkSwitchTests(unittest.TestCase):
    CAMERA_NET = UsbNet(interface="usb0", ipv4="172.25.118.55", vid="2672", pid="0052")
    OTHER_CAMERA_NET = UsbNet(interface="usb1", ipv4="172.26.200.55", vid="2672", pid="0052")

    def test_usb_route_is_true_without_looking_at_interfaces(self):
        def must_not_look():
            raise AssertionError("a usb route needs no interface lookup")

        endpoint = Endpoint(host="172.25.118.51", transport="usb")
        self.assertTrue(requires_network_switch(endpoint, find_nets=must_not_look))

    def test_hand_typed_address_on_a_camera_interface_is_true(self):
        endpoint = Endpoint(host="172.25.118.51", transport="host")
        nets = [self.OTHER_CAMERA_NET, self.CAMERA_NET]
        self.assertTrue(requires_network_switch(endpoint, find_nets=lambda: nets))

    def test_other_routes_are_false(self):
        cases = [
            (Endpoint(host="192.168.1.40", transport="host"), [self.CAMERA_NET]),
            (Endpoint(host="10.5.5.9", transport="wifi"), [self.CAMERA_NET]),
            (Endpoint(host="172.25.118.51", transport="host"), [self.OTHER_CAMERA_NET]),
            (Endpoint(host="172.25.118.51", transport="host"), []),
        ]
        for endpoint, nets in cases:
            with self.subTest(endpoint=endpoint.host, nets=len(nets)):
                self.assertFalse(requires_network_switch(endpoint, find_nets=lambda: nets))

    def test_looks_up_this_computers_interfaces_by_default(self):
        with mock.patch.object(transport, "find_usb_nets", return_value=[self.CAMERA_NET]) as find:
            self.assertTrue(requires_network_switch(Endpoint(host="172.25.118.51")))
        find.assert_called_once_with()


class NothingDestructiveTests(unittest.TestCase):
    def test_the_check_would_catch_them(self):
        for command in (
            "mkfs.vfat /dev/sda1",
            "mke2fs /dev/sda1",
            "format /dev/sda",
            "flash_erase /dev/mtd0",
            "dd if=/dev/zero of=/dev/sda",
            "x | dd of=/dev/sda",
            "update_firmware",
            "gpUpdate --force",
            "fdisk /dev/sda",
            "sfdisk /dev/sda",
            "parted /dev/sda",
            "wipefs -a /dev/sda",
        ):
            with self.subTest(command):
                self.assertRegex(command, FORBIDDEN)

    def test_no_script_contains_one(self):
        scripts = {
            "state": STATE_SCRIPT,
            "enable": ENABLE_SCRIPT,
            "disable": disable_script(),
            "with modules": disable_script(MODULES),
        }
        for name, script in scripts.items():
            with self.subTest(name):
                self.assertNotRegex(script, FORBIDDEN)

    def test_nothing_sent_in_a_full_cycle_contains_one(self):
        remote = FakeRemote(
            device_state(),
            switch_output(*ENABLE_STEPS),
            host_state(),
            host_state(),
            switch_output(*RESTORE_STEPS),
            device_state(),
            half_host_state(),
            switch_output(*RESTORE_STEPS),
            device_state(),
        )
        hostmode.enable(remote)
        hostmode.disable(remote, modules=MODULES)
        hostmode.recover(remote, modules=MODULES)
        self.assertEqual(len(remote.commands), 9)
        for command in remote.commands:
            self.assertNotRegex(command, FORBIDDEN)

    def test_the_only_thing_ever_deleted_is_the_marker(self):
        for script in (ENABLE_SCRIPT, disable_script(MODULES)):
            self.assertEqual(re.findall(r"\brm\b.*", script), ["rm -f $MARKER"])
        self.assertEqual(re.findall(r"\brm\b.*", STATE_SCRIPT), [])


if __name__ == "__main__":
    unittest.main()

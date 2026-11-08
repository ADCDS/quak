"""Switch the camera's USB-C port to USB host mode and back, with GoPro's own ``dwc3_milbeaut`` switch.

All of it lives in the camera's RAM: a reboot puts the port back in device mode by itself. Nothing here
formats, flashes or partitions anything.

Each switch is one remote command, not a conversation. The link can run through the very port being
switched (SSH over Ethernet on a hub is a real setup), so ``disable`` may cut its own connection
partway and the camera has to finish without it. That is why the scripts ignore HUP and PIPE, and why
``step`` doesn't care whether its output still reaches anyone.

The marker goes on the card before anything changes, so a switch that dies partway can be seen and
recovered. It also outlives a reboot, which the switch doesn't, so the restore looks at what is there
and only undoes that: on a camera already in device mode it clears the old marker and leaves a working
USB link alone.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Iterable

from quak.core.errors import HostModeError, NetworkError
from quak.lab.models import HostModeResult, HostModeState
from quak.lab.remote import Remote
from quak.ssh import transport
from quak.ssh.models import Endpoint

HOSTMODE_PARAM = "/sys/module/dwc3_milbeaut/parameters/hostmode"
GLUE = "1ba60000.usb31"
GLUE_DRIVER = "/sys/bus/platform/drivers/dwc3-milbeaut"
GADGET_UDC = "/sys/kernel/config/usb_gadget/gopro_gadget/UDC"
UDC_NAME = "19880000.dwc3"
UDC_CLASS = "/sys/class/udc"
USB_DEVICES = "/sys/bus/usb/devices"
ROOT_HUBS = ("usb1", "usb2")
MARKER = "/tmp/fuse_d/quak/hostmode.json"
LAB_MOUNTS = "/tmp/quak/mnt/"
GPSV = "/usr/local/gopro/bin/gpsv"
GPUSB = "/usr/local/gopro/bin/gpUsb"

HOST = "1"
DEVICE = "2"

SWITCH_TIMEOUT = 90.0  # longer than the scripts' own waits, and it only ends the wait here: the camera carries on
_STATE_TIMEOUT = 30.0
_XHCI_WAIT = 15  # seconds; both root hubs took well under one in the live tests
_UDC_WAIT = 10
_UDC_SETTLE = 2  # the gadget has only ever been reattached this long after the bind

_MODULE_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _marker_text(phase: str) -> str:
    return json.dumps({"version": 1, "phase": phase}, separators=(",", ":"))


_ROOT_HUB_NAMES = " ".join(ROOT_HUBS)
_ROOT_HUB_PATHS = " ".join(f"{USB_DEVICES}/{hub}" for hub in ROOT_HUBS)

# gpsv supervises several daemons (gpControlD, gpHttpServer, ...), one process each. Only the gpsv
# watching gpUsb is paused, and BusyBox's ps prints: PID USER VSZ STAT COMMAND [ARGS].
STATE_SCRIPT = f"""\
echo "hostmode=$(cat {HOSTMODE_PARAM} 2>/dev/null)"
[ -e {GLUE_DRIVER}/{GLUE} ] && echo glue=1
echo "udc=$(cat {GADGET_UDC} 2>/dev/null)"
for hub in {_ROOT_HUB_NAMES}; do [ -e {USB_DEVICES}/$hub ] && echo "hub=$hub"; done
ps | awk -v sv={GPSV} -v usb={GPUSB} \
'$5==sv && $6==usb {{print "proc=gpsv", $4}} $5==usb && NF==5 {{print "proc=gpUsb", $4}}'
[ -f {MARKER} ] && echo "marker=$(cat {MARKER} 2>/dev/null)"
true
"""

_VARIABLES = f"""\
trap '' HUP PIPE
HM={HOSTMODE_PARAM}
DRV={GLUE_DRIVER}
GLUE={GLUE}
UDC={GADGET_UDC}
CTL={UDC_NAME}
MARKER={MARKER}
SV={GPSV}
USB={GPUSB}
RUNNING='^[^T]'
STOPPED='^T'
"""

# step's success can't depend on its echo: with the link gone that echo fails, and the action still happened.
_FUNCTIONS = r"""
put() { echo "$2" > "$1"; }
step() { name=$1; shift; if "$@"; then echo "done=$name"; return 0; fi; echo "failed=$name"; return 1; }
gpsv() { ps | awk -v sv=$SV -v usb=$USB -v s="$1" '$5==sv && $6==usb && $4~s {print $1}'; }
gpusb() { ps | awk -v usb=$USB -v s="$1" '$5==usb && NF==5 && $4~s {print $1}'; }
signal() { for p in $($2 "$3"); do kill -"$1" "$p" || return 1; done; }
wait_path() {
  left=$1; shift
  for p in "$@"; do
    while [ ! -e "$p" ]; do
      [ "$left" -gt 0 ] || return 1
      sleep 1; left=$((left-1))
    done
  done
}
"""

# Keeps going past a failed step: the daemons have to come back whatever else went wrong. The marker
# only goes once everything is back, so that recover can try again.
_RESTORE = f"""\
restore() {{
  rc=0
  if [ "$(cat $HM 2>/dev/null)" != {DEVICE} ] || [ ! -e $DRV/$GLUE ]; then
    [ ! -e $DRV/$GLUE ] || step unbind put $DRV/unbind $GLUE || rc=1
    step hostmode={DEVICE} put $HM {DEVICE} || rc=1
    step bind put $DRV/bind $GLUE || rc=1
    step udc-ready wait_path {_UDC_WAIT} {UDC_CLASS}/$CTL || rc=1
    sleep {_UDC_SETTLE}
  fi
  [ "$(cat $UDC 2>/dev/null)" = $CTL ] || step attach put $UDC $CTL || rc=1
  [ -z "$(gpusb "$STOPPED")" ] || step resume-gpUsb signal CONT gpusb "$STOPPED" || rc=1
  [ -z "$(gpsv "$STOPPED")" ] || step resume-gpsv signal CONT gpsv "$STOPPED" || rc=1
  [ $rc != 0 ] || rm -f $MARKER
  return $rc
}}
back_out() {{ restore && exit 1; exit 2; }}
"""

_PRELUDE = _VARIABLES + _FUNCTIONS + _RESTORE

# The supervisor stops first and resumes last, so it never sees gpUsb in a state it would act on.
ENABLE_SCRIPT = (
    _PRELUDE
    + f"""\
mkdir -p {posixpath.dirname(MARKER)} && put $MARKER '{_marker_text("switching")}' || {{ echo failed=marker; exit 3; }}
step pause-gpsv signal STOP gpsv "$RUNNING" || back_out
step pause-gpUsb signal STOP gpusb "$RUNNING" || back_out
[ -z "$(cat $UDC 2>/dev/null)" ] || step detach put $UDC "" || back_out
[ ! -e $DRV/$GLUE ] || step unbind put $DRV/unbind $GLUE || back_out
step hostmode={HOST} put $HM {HOST} || back_out
step bind put $DRV/bind $GLUE || back_out
step xhci wait_path {_XHCI_WAIT} {_ROOT_HUB_PATHS} || back_out
step marker put $MARKER '{_marker_text("host")}' || back_out
"""
)

# A volume still in use stops everything: pulling the port out from under a filesystem could lose data.
_UNMOUNT = f"""\
for v in $(awk 'index($2,"{LAB_MOUNTS}")==1 {{print $2}}' /proc/mounts); do
  step "umount:$v" umount "$v" || exit 3
done
"""

_OUTCOMES = {
    1: "The camera was put back as it was",
    2: (
        "The camera is only partly restored. Run `quak lab host recover`, or reboot the camera: "
        "a switch doesn't survive a reboot"
    ),
    3: "Nothing was switched",
}
_OUTCOME_UNKNOWN = "Run `quak lab host recover` to see where the camera ended up"


def _module_names(modules: Iterable[str]) -> list[str]:
    """The kernel's own spelling of each name. Anything else never reaches a shell."""
    names = []
    for module in modules:
        if not _MODULE_NAME.fullmatch(module):
            raise HostModeError(f"{module!r} is not a kernel module name")
        names.append(module.replace("-", "_"))
    return names


def disable_script(modules: Iterable[str] = ()) -> str:
    """Unmount Lab volumes, unload Lab modules (given in load order, unloaded in reverse), restore device mode.

    A module that stays loaded doesn't stop the switch: it does no harm there, and may simply be in use.
    """
    unload = "".join(
        f"if [ -d /sys/module/{name} ]; then step rmmod:{name} rmmod {name}; fi\n"
        for name in reversed(_module_names(modules))
    )
    return f"{_PRELUDE}{_UNMOUNT}{unload}restore || exit 2\n"


def requires_network_switch(
    endpoint: Endpoint, *, find_nets: Callable[[], list[transport.UsbNet]] | None = None
) -> bool:
    """True if this link runs over the camera's own USB-NCM, which host mode takes away.

    For the caller to check before ``enable``. A ``host`` route can be USB-NCM too, when its address was
    typed by hand, so that case looks for this computer's camera interfaces.
    """
    if endpoint.transport == "usb":
        return True
    nets = (find_nets or transport.find_usb_nets)()
    return any(transport.camera_ip_from_host(net.ipv4) == endpoint.host for net in nets)


def _marker_phase(raw: str) -> str:
    try:
        phase = json.loads(raw).get("phase")
    except (ValueError, AttributeError):
        return "unknown"
    return phase if phase in ("switching", "host") else "unknown"


def _parse_state(text: str) -> HostModeState:
    fields: dict[str, str] = {}
    hubs: list[str] = []
    paused: list[str] = []
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "hub":
            hubs.append(value)
        elif key == "proc":
            name, _, stat = value.partition(" ")
            if stat.startswith("T"):
                paused.append(name)
        else:
            fields[key] = value.strip()
    hostmode = fields.get("hostmode", "")
    if not hostmode:
        return HostModeState(error=f"this camera has no USB host-mode switch at {HOSTMODE_PARAM}")
    udc = fields.get("udc", "")
    enabled = hostmode == HOST and all(hub in hubs for hub in ROOT_HUBS)
    whole_device = hostmode != HOST and "glue" in fields and bool(udc) and not paused
    return HostModeState(
        enabled=enabled,
        supported=True,
        hostmode=hostmode,
        devices=hubs,
        partial=not enabled and not whole_device,
        udc=udc,
        paused=paused,
        marker=_marker_phase(fields["marker"]) if "marker" in fields else "",
    )


def state(remote: Remote) -> HostModeState:
    """Read the port's state in one snapshot. Changes nothing. A lost link raises; a failed read is in ``error``."""
    result = remote.exec(STATE_SCRIPT, timeout=_STATE_TIMEOUT)
    if result.exit_code != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit code {result.exit_code}"
        return HostModeState(error=f"could not read the camera's USB state: {detail}")
    return _parse_state(result.text)


def _switchable(remote: Remote) -> HostModeState:
    current = state(remote)
    if not current.supported:
        raise HostModeError(current.error)
    return current


def _state_after(remote: Remote) -> HostModeState:
    """The state once a switch has finished. It did finish, so a link lost now doesn't undo that."""
    try:
        return state(remote)
    except NetworkError as exc:
        return HostModeState(error=f"the switch finished, but its result could not be read back: {exc}")


def _run_switch(remote: Remote, script: str, doing: str) -> tuple[list[str], list[str]]:
    """Run one switch script. The steps that were done and those that failed, or an error saying where it stopped."""
    try:
        result = remote.exec(script, timeout=SWITCH_TIMEOUT)
    except NetworkError as exc:
        raise HostModeError(
            f"the connection to the camera dropped while trying to {doing} ({exc}). That is expected if it ran through "
            "the camera's USB port. The camera finishes the switch on its own: reconnect and run "
            "`quak lab host recover` to see where it ended up"
        ) from None
    done: list[str] = []
    failed: list[str] = []
    for line in result.text.splitlines():
        key, _, step = line.partition("=")
        if key == "done":
            done.append(step)
        elif key == "failed":
            failed.append(step)
    if result.exit_code == 0:
        return done, failed
    detail = ", ".join(failed) or f"exit code {result.exit_code}"
    stderr = result.stderr.decode("utf-8", "replace").strip()
    if stderr:
        detail += f": {stderr}"
    raise HostModeError(f"could not {doing} ({detail}). {_OUTCOMES.get(result.exit_code, _OUTCOME_UNKNOWN)}")


def enable(remote: Remote) -> HostModeResult:
    """Switch the port to host mode. A failure puts the camera back the way it was.

    The caller checks ``requires_network_switch`` first: over the camera's own USB-NCM the switch would
    still finish, with nothing left to reach the camera through.
    """
    current = _switchable(remote)
    if current.marker == "switching" or (current.marker and current.partial):
        raise HostModeError(
            "an earlier host-mode switch did not finish, or is still running. "
            "Run `quak lab host recover` first. Nothing was changed"
        )
    if current.enabled:
        return HostModeResult(current, message="Host mode was already on")
    done, _ = _run_switch(remote, ENABLE_SCRIPT, "switch to host mode")
    after = _state_after(remote)
    if not after.error and not after.enabled:
        raise HostModeError(
            f"the switch ran, but the camera reports hostmode={after.hostmode} with root hubs "
            f"{', '.join(after.devices) or 'missing'}. Run `quak lab host recover`"
        )
    return HostModeResult(after, done, "Host mode is on")


def _restore(remote: Remote, script: str) -> HostModeResult:
    done, failed = _run_switch(remote, script, "switch back to device mode")
    after = _state_after(remote)
    if not after.error and (after.hostmode == HOST or after.partial):
        raise HostModeError(
            f"the restore ran, but the camera reports hostmode={after.hostmode}, gadget on "
            f"{after.udc or 'nothing'}, paused {', '.join(after.paused) or 'nothing'}. "
            "Run `quak lab host recover`, or reboot the camera: a switch doesn't survive a reboot"
        )
    if done:
        message = f"Restored: {', '.join(done)}"
    else:
        message = "Nothing needed restoring: the camera was already in device mode"
    if failed:
        message += f". Failed, which doesn't stop the switch: {', '.join(failed)}"
    return HostModeResult(after, done, message)


def disable(remote: Remote, *, modules: Iterable[str] = ()) -> HostModeResult:
    """Put the port back in device mode, undoing only what is there. ``modules``: Lab modules, in load order."""
    script = disable_script(modules)
    _switchable(remote)
    return _restore(remote, script)


def recover(remote: Remote, *, modules: Iterable[str] = ()) -> HostModeResult:
    """Finish what an interrupted switch left. Whole host mode and anything without Quak's marker are left alone."""
    script = disable_script(modules)
    current = _switchable(remote)
    if not current.marker:
        if current.partial:
            message = "The port is between modes, but Quak didn't leave it that way, so it was left alone"
        else:
            message = "Nothing to recover"
        return HostModeResult(current, message=message)
    if current.enabled and current.marker == "host":
        return HostModeResult(current, message="Host mode is on and whole: nothing to recover")
    return _restore(remote, script)

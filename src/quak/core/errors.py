"""Exceptions shared by the backend, CLI and GUI. Each carries the CLI exit code it maps to."""

from quak.i18n import N_, term, tr


class QuakError(Exception):
    exit_code = 1


class NotBonded(QuakError):
    """This device isn't bonded with the camera and the camera isn't in pairing mode."""

    exit_code = 2


class Timeout(QuakError):
    exit_code = 3


class Disconnected(QuakError):
    exit_code = 3


class Rejected(QuakError):
    """The camera answered with a non-success status."""

    exit_code = 4

    def __init__(self, what: str, status: int | str):
        super().__init__(tr("{what}: camera answered {status}", what=what, status=term(status)))
        self.status = status


class NotFound(QuakError):
    exit_code = 5


class NetworkError(QuakError):
    """Wi-Fi, HTTP or nmcli failure."""

    exit_code = 6


class AdapterInUse(NetworkError):
    """Joining the camera's Wi-Fi would take this computer's network connection away."""

    def __init__(self, device: str) -> None:
        super().__init__(
            tr(
                "Wi-Fi adapter {device} is this computer's network connection: joining the camera's Wi-Fi would "
                "cut it off until Quak is done. Allow it with --wifi-adapter {device}, or plug in a second "
                "adapter",
                device=device,
            )
        )
        self.device = device


class Unsupported(QuakError):
    """The camera says it doesn't support this (or not in its current mode)."""

    exit_code = 7


BLUETOOTH_LOST = N_("Bluetooth stopped responding while connecting. Check that Bluetooth is on, then try again.")

NOT_BONDED_HINT = N_(
    "This device isn't bonded with the camera and the camera isn't on its pairing screen, so it "
    "ignores the connection. On the camera open Preferences > Connections > Connect Device > "
    "GoPro Quik App, then run `quak pair`."
)

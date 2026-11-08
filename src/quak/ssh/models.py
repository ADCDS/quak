"""Data carried between the SSH backend, the CLI and the app.

Nothing here holds private key material or passphrases: profiles point at key ids, and the
key store keeps private files on disk with 0600 permissions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Transport = Literal["auto", "wifi", "usb", "host"]
Auth = Literal["key", "password", "none"]
HostKeyState = Literal["unknown", "trusted", "changed"]


@dataclass(frozen=True)
class SshProfile:
    """How to reach one camera over SSH. ``host`` is only used by ``transport=host``."""

    address: str
    label: str = ""
    transport: Transport = "auto"
    host: str | None = None
    ssh_port: int = 22
    http_port: int = 80
    user: str = "root"
    auth: Auth = "key"
    key_id: str | None = None
    auto_rearm: bool = False
    created: str = ""
    last_arm: str | None = None


@dataclass(frozen=True)
class SshKey:
    """Metadata for a key in the Quak key store. The private key lives in ``private_file``."""

    id: str
    label: str
    type: str
    private_file: str
    public_key: str
    fingerprint: str
    comment: str = ""
    encrypted: bool = False
    created: str = ""


@dataclass(frozen=True)
class HostKey:
    host: str
    port: int
    key_type: str
    fingerprint: str
    line: str | None = None  # an OpenSSH known_hosts line, when one was scanned


@dataclass(frozen=True)
class Endpoint:
    host: str
    ssh_port: int = 22
    http_port: int = 80
    transport: str = "host"
    interface: str | None = None


@dataclass
class SshStatus:
    profile: SshProfile | None = None
    endpoint: Endpoint | None = None
    reachable: bool = False
    ssh_up: bool = False
    banner: str = ""
    host_key: HostKey | None = None
    host_key_state: HostKeyState = "unknown"
    last_error: str = ""


@dataclass
class ArmResult:
    http_status: int | None = None
    accepted: bool = False
    ssh_up: bool = False
    banner: str = ""
    elapsed: float = 0.0
    message: str = ""


@dataclass
class TestResult:
    ok: bool = False
    banner: str = ""
    uid: str = ""
    uname: str = ""
    host_key: HostKey | None = None
    error: str = ""


@dataclass
class ExecResult:
    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


@dataclass
class KeyMaterial:
    """A generated or imported key, returned before it is written to the store."""

    private_pem: str
    public_key: str
    fingerprint: str
    key_type: str
    comment: str = ""
    encrypted: bool = False


@dataclass
class KeyInstallResult:
    installed: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    verified: bool = False
    message: str = ""

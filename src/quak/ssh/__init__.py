"""SSH access to a rooted GoPro: arm the installed gopro-root payload over SC, verify the
camera host key, and manage keys and connections.

Quak never installs or serves the exploit. The only unauthenticated camera action here is
the SC request that runs the payload already on the SD card; everything else happens over
an authenticated root SSH session.
"""

from quak.ssh.models import (
    ArmResult,
    Endpoint,
    ExecResult,
    HostKey,
    SshKey,
    SshProfile,
    SshStatus,
    TestResult,
)

__all__ = [
    "ArmResult",
    "Endpoint",
    "ExecResult",
    "HostKey",
    "SshKey",
    "SshProfile",
    "SshStatus",
    "TestResult",
]

"""What the Lab can do on a camera it has a link to, one module per area: ``storage`` and ``system``.

Plain functions over a ``Remote``, so tests hand them a fake. The two helpers here are what both share.
"""

from __future__ import annotations

from quak.core.errors import LabError
from quak.lab.remote import Remote
from quak.ssh.models import ExecResult


def printable(text: str, keep: str = "") -> str:
    """``text`` fit for a terminal. Labels, file names and even kernel logs can hold what a plugged-in device chose."""
    return "".join(ch for ch in text if ch.isprintable() or ch in keep)


def run(remote: Remote, command: str, doing: str, timeout: float) -> ExecResult:
    """Run one command. A non-zero exit raises ``LabError`` saying what failed; a lost link stays ``NetworkError``."""
    result = remote.exec(command, timeout=timeout)
    if result.exit_code != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        detail = printable(stderr, keep="\n") or f"exit code {result.exit_code}"
        raise LabError(f"could not {doing}: {detail}")
    return result

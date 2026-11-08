"""Pin the camera's SSH host key.

The GUI trusts a fingerprint (dartssh2 exposes only the SHA256 fingerprint); the command
line also keeps an OpenSSH ``known_hosts`` file so the system ``ssh`` can enforce the same
decision. Both are keyed by ``host:port`` in Quak's SSH store.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
from pathlib import Path

from quak.ssh import store
from quak.ssh.models import HostKey

_PREFERENCE = ("ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")


def fingerprint(blob: bytes) -> str:
    """OpenSSH-style ``SHA256:...`` fingerprint from a raw public-key blob."""
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def fingerprint_from_public_line(line: str) -> str:
    parts = line.strip().split()
    if len(parts) < 2:
        raise ValueError("not a public key line")
    return fingerprint(base64.b64decode(parts[1], validate=True))


def parse_public_line(line: str) -> tuple[str, bytes, str]:
    parts = line.strip().split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("not a public key line")
    key_type, encoded = parts[0], parts[1]
    comment = parts[2] if len(parts) > 2 else ""
    return key_type, base64.b64decode(encoded, validate=True), comment


def _host_field(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def known_hosts_path() -> Path:
    return store.ssh_dir() / "known_hosts"


def _read_known_hosts() -> str:
    try:
        return known_hosts_path().read_text()
    except OSError:
        return ""


def _write_known_hosts(text: str) -> None:
    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)


def _line_hosts(line: str) -> list[str]:
    field = line.split(maxsplit=1)[0]
    return field.split(",")


def lookup_known_hosts(host: str, port: int) -> HostKey | None:
    field = _host_field(host, port)
    for line in _read_known_hosts().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        if field not in _line_hosts(line):
            continue
        try:
            blob = base64.b64decode(parts[2], validate=True)
        except ValueError:
            continue
        return HostKey(host=host, port=port, key_type=parts[1], fingerprint=fingerprint(blob), line=line)
    return None


def add_known_host(host: str, port: int, key_type: str, blob: bytes) -> HostKey:
    """Add or replace one host key in the OpenSSH known_hosts file."""
    field = _host_field(host, port)
    line = f"{field} {key_type} {base64.b64encode(blob).decode('ascii')}"
    remove_known_host(host, port)
    text = _read_known_hosts()
    if text and not text.endswith("\n"):
        text += "\n"
    _write_known_hosts(text + line + "\n")
    return HostKey(host=host, port=port, key_type=key_type, fingerprint=fingerprint(blob), line=line)


def add_host_key(host_key: HostKey) -> HostKey:
    """Add a scanned ``HostKey`` (its OpenSSH ``known_hosts`` line) to the file.

    ``scan`` returns a ``HostKey`` whose ``line`` is a raw ``known_hosts`` line
    (``host key_type base64``). This pins that exact key under the model's host/port,
    re-deriving the bracketed ``[host]:port`` field when the port is non-default.
    """
    if not host_key.line:
        raise ValueError("host key has no scanned known_hosts line to add")
    parts = host_key.line.split()
    if len(parts) < 3:
        raise ValueError("not a known_hosts line")
    blob = base64.b64decode(parts[2], validate=True)
    return add_known_host(host_key.host, host_key.port, host_key.key_type, blob)


def remove_known_host(host: str, port: int) -> bool:
    field = _host_field(host, port)
    kept: list[str] = []
    removed = False
    for line in _read_known_hosts().splitlines():
        if not line.strip():
            continue
        if field in _line_hosts(line):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False
    _write_known_hosts("\n".join(kept) + ("\n" if kept else ""))
    return True


def _fingerprint_key(host: str, port: int) -> str:
    return f"{host}:{port}"


def stored_fingerprint(host: str, port: int) -> HostKey | None:
    data = store.read()["host_fingerprints"].get(_fingerprint_key(host, port))
    if not isinstance(data, dict):
        return None
    return HostKey(
        host=host,
        port=port,
        key_type=str(data.get("type", "")),
        fingerprint=str(data.get("fingerprint", "")),
    )


def remember_fingerprint(host: str, port: int, key_type: str, fp: str) -> HostKey:
    data = store.read()
    data["host_fingerprints"][_fingerprint_key(host, port)] = {"type": key_type, "fingerprint": fp}
    store.write(data)
    return HostKey(host=host, port=port, key_type=key_type, fingerprint=fp)


def forget_fingerprint(host: str, port: int) -> bool:
    data = store.read()
    removed = data["host_fingerprints"].pop(_fingerprint_key(host, port), None) is not None
    if removed:
        store.write(data)
    remove_known_host(host, port)
    return removed


def known(host: str, port: int) -> HostKey | None:
    return stored_fingerprint(host, port) or lookup_known_hosts(host, port)


def scan(host: str, port: int = 22, timeout: float = 8.0) -> HostKey:
    """Scan the camera's host key with OpenSSH's ssh-keyscan."""
    tool = shutil.which("ssh-keyscan")
    if not tool:
        raise FileNotFoundError("ssh-keyscan is not installed; use the app to trust the host key")
    try:
        proc = subprocess.run(
            [tool, "-T", str(max(2, int(timeout))), "-p", str(port), host],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"ssh-keyscan {host}:{port} timed out") from None
    candidates = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            blob = base64.b64decode(parts[2], validate=True)
        except ValueError:
            continue
        candidates.append((parts[1], blob, line))
    if not candidates:
        raise ConnectionError(f"no host key from {host}:{port}")
    candidates.sort(key=lambda c: _PREFERENCE.index(c[0]) if c[0] in _PREFERENCE else len(_PREFERENCE))
    key_type, blob, line = candidates[0]
    return HostKey(host=host, port=port, key_type=key_type, fingerprint=fingerprint(blob), line=line)


def state(host: str, port: int, seen: HostKey | None = None) -> str:
    """unknown / trusted / changed for a scanned host key."""
    remembered = stored_fingerprint(host, port)
    if remembered is None:
        known_key = lookup_known_hosts(host, port)
        if known_key is None:
            return "unknown"
        remembered = known_key
    if seen is None:
        return "trusted"
    return "trusted" if seen.fingerprint == remembered.fingerprint else "changed"

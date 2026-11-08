"""Quak's SSH key store and the authorized_keys edits it makes over an authenticated session.

Private keys live in ``~/.config/quak/ssh/keys/<id>.pem`` with 0600 permissions; the JSON
store only holds metadata. Nothing here logs key material or passphrases.

The camera-side helpers are pure text functions. The manager decides when to read/write the
card's ``authorized_keys`` and the runtime copy at ``/dev/dropbear/authorized_keys``.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import shutil
import socket
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from quak.core.errors import SshKeyError
from quak.ssh import store
from quak.ssh.hostkeys import fingerprint, parse_public_line
from quak.ssh.models import SshKey

KEY_TYPES = ("ssh-ed25519",)

_KEY_TYPE_PREFIXES = ("ssh-", "ecdsa-", "sk-")
_OPENSSH_MAGIC = b"openssh-key-v1\0"
# One field of an authorized_keys line: whitespace only separates fields outside double quotes.
_FIELD = re.compile(r'(?:[^\s"]|"(?:\\.|[^"\\])*"?)+')


def key_dir() -> Path:
    return store.ssh_dir() / "keys"


def ensure_key_dir() -> Path:
    path = key_dir()
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _key_id() -> str:
    return "k_" + uuid.uuid4().hex[:8]


def _default_comment() -> str:
    host = socket.gethostname().strip() or "quak"
    return f"quak@{host}"


def _clean_comment(comment: str) -> str:
    return " ".join(comment.split())


def public_line(key_type: str, blob: bytes, comment: str = "") -> str:
    comment = _clean_comment(comment)
    encoded = base64.b64encode(blob).decode("ascii")
    return f"{key_type} {encoded}" + (f" {comment}" if comment else "")


def _public_parts(line: str) -> tuple[str, bytes, str]:
    try:
        key_type, blob, comment = parse_public_line(line)
    except ValueError:
        raise SshKeyError("that is not a valid OpenSSH public key line") from None
    if not key_type.startswith(_KEY_TYPE_PREFIXES):
        raise SshKeyError("that is not a valid OpenSSH public key line")
    return key_type, blob, comment


def public_blob_parts(blob: bytes) -> tuple[str, bytes]:
    """The key type and blob from an SSH wire-format public key, as OpenSSH private files carry it."""
    if len(blob) < 4:
        raise SshKeyError("that does not contain an OpenSSH public key")
    size = int.from_bytes(blob[:4], "big")
    if size < 1 or size > 64 or len(blob) < 4 + size:
        raise SshKeyError("that does not contain an OpenSSH public key")
    try:
        key_type = blob[4 : 4 + size].decode("ascii")
    except UnicodeDecodeError:
        raise SshKeyError("that does not contain an OpenSSH public key") from None
    if not key_type.startswith(_KEY_TYPE_PREFIXES):
        raise SshKeyError("that does not contain an OpenSSH public key")
    return key_type, blob


def _write_private_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def key_path(key: SshKey) -> Path:
    path = (store.ssh_dir() / key.private_file).resolve()
    if key_dir().resolve() not in path.parents:
        raise SshKeyError(f"key {key.id} points outside the Quak key directory; delete it and add it again")
    return path


def read_private(key: SshKey) -> str:
    return key_path(key).read_text(encoding="utf-8")


def list_keys() -> list[SshKey]:
    out = []
    for key_id, data in store.read()["keys"].items():
        if not isinstance(data, dict):
            continue
        try:
            out.append(
                SshKey(
                    id=key_id,
                    label=str(data.get("label", "")),
                    type=str(data.get("type", "")),
                    private_file=str(data.get("private_file", "")),
                    public_key=str(data.get("public_key", "")),
                    fingerprint=str(data.get("fingerprint", "")),
                    comment=str(data.get("comment", "")),
                    encrypted=bool(data.get("encrypted", False)),
                    created=str(data.get("created", "")),
                )
            )
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda k: (k.created, k.id))


def get_key(key_id: str) -> SshKey | None:
    return next((key for key in list_keys() if key.id == key_id), None)


def default_key() -> SshKey | None:
    keys = list_keys()
    return keys[0] if keys else None


def _put_key(key: SshKey, pem: str) -> SshKey:
    ensure_key_dir()
    path = key_dir() / Path(key.private_file).name
    _write_private_file(path, pem)
    stored = SshKey(
        id=key.id,
        label=key.label,
        type=key.type,
        private_file=str(path.relative_to(store.ssh_dir())),
        public_key=key.public_key,
        fingerprint=key.fingerprint,
        comment=key.comment,
        encrypted=key.encrypted,
        created=key.created or _now(),
    )
    data = store.read()
    data["keys"][stored.id] = {
        "label": stored.label,
        "type": stored.type,
        "private_file": stored.private_file,
        "public_key": stored.public_key,
        "fingerprint": stored.fingerprint,
        "comment": stored.comment,
        "encrypted": stored.encrypted,
        "created": stored.created,
    }
    store.write(data)
    return stored


def delete_key(key_id: str) -> bool:
    key = get_key(key_id)
    if key is None:
        return False
    try:
        key_path(key).unlink(missing_ok=True)
    except SshKeyError:
        pass  # the metadata points outside the key directory: drop the entry and touch nothing else
    except OSError as exc:
        raise SshKeyError(f"could not delete the private key file of {key.id}: {exc.strerror}") from None
    data = store.read()
    removed = data["keys"].pop(key_id, None) is not None
    if removed:
        store.write(data)
    return removed


def _ssh_keygen() -> str:
    tool = shutil.which("ssh-keygen")
    if not tool:
        raise SshKeyError("ssh-keygen was not found; install the OpenSSH client and try again")
    return tool


def _run_keygen(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ssh-keygen away from any terminal, so a locked key fails instead of prompting."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "SSH_ASKPASS_REQUIRE": "never"},
        )
    except subprocess.TimeoutExpired:
        # from None: the exception's text quotes the command line, passphrase included.
        raise SshKeyError("ssh-keygen did not finish in time; try again") from None


def generate(
    label: str,
    *,
    comment: str = "",
    passphrase: str | None = None,
    key_type: str = "ssh-ed25519",
) -> SshKey:
    """Generate an Ed25519 key with the system ssh-keygen and store it."""
    if key_type not in KEY_TYPES:
        raise SshKeyError("Quak generates ssh-ed25519 keys")
    tool = _ssh_keygen()
    comment = _clean_comment(comment) or _default_comment()
    key_id = _key_id()
    with tempfile.TemporaryDirectory(prefix="quak-key.") as tmp:
        private = Path(tmp) / "key"
        result = _run_keygen([tool, "-q", "-t", "ed25519", "-N", passphrase or "", "-C", comment, "-f", str(private)])
        if result.returncode:
            raise SshKeyError(result.stderr.strip() or "ssh-keygen could not create the key")
        pem = private.read_text(encoding="utf-8")
        pub = private.with_suffix(".pub").read_text(encoding="utf-8").strip()
    key_type_name, blob, _ = _public_parts(pub)
    key = SshKey(
        id=key_id,
        label=label or key_id,
        type=key_type_name,
        private_file=f"keys/{key_id}.pem",
        public_key=public_line(key_type_name, blob, comment),
        fingerprint=fingerprint(blob),
        comment=comment,
        encrypted=bool(passphrase),
        created=_now(),
    )
    return _put_key(key, pem)


def add_generated(
    private_pem: str,
    public_key: str,
    label: str = "",
    *,
    comment: str = "",
    encrypted: bool = False,
) -> SshKey:
    """Store a key made elsewhere (the phone's Dart SSH client), where ssh-keygen isn't available."""
    if "PRIVATE KEY" not in private_pem:
        raise SshKeyError("that is not an OpenSSH/PEM private key")
    key_type, blob, parsed_comment = _public_parts(public_key)
    comment = _clean_comment(comment or parsed_comment)
    key_id = _key_id()
    key = SshKey(
        id=key_id,
        label=label or key_id,
        type=key_type,
        private_file=f"keys/{key_id}.pem",
        public_key=public_line(key_type, blob, comment),
        fingerprint=fingerprint(blob),
        comment=comment,
        encrypted=encrypted,
        created=_now(),
    )
    return _put_key(key, private_pem)


def _read_key_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise SshKeyError(f"could not read {path}; check the path and that it is a text key file") from None


def _private_header(pem: str) -> tuple[bool, bytes | None]:
    """``(encrypted, public blob)`` of a private key, readable without its passphrase.

    The OpenSSH format keeps both outside the encrypted part; older PEM files only say whether
    they are encrypted.
    """
    try:
        raw = base64.b64decode("".join(pem.strip().splitlines()[1:-1]))
    except ValueError:
        raw = b""
    if not raw.startswith(_OPENSSH_MAGIC):
        return "ENCRYPTED" in pem, None
    fields = []
    at = len(_OPENSSH_MAGIC)
    for gap in (0, 0, 0, 4):  # cipher, KDF name, KDF options, then the key count before the public key
        at += gap
        size = int.from_bytes(raw[at : at + 4], "big")
        fields.append(raw[at + 4 : at + 4 + size])
        at += 4 + size
    return fields[0] != b"none", fields[3] or None


def import_key(path: Path, label: str, passphrase: str | None = None) -> SshKey:
    """Copy an existing OpenSSH private key into the store and derive its public key.

    A ``.pub`` file beside the key supplies the comment, and stands in when the key can't be
    opened here. It is checked against the private key whenever that is possible, because a stale
    one would have Quak install a key on the camera that it can't log in with.
    """
    path = Path(path).expanduser()
    pem = _read_key_file(path)
    if "PRIVATE KEY" not in pem:
        raise SshKeyError(f"{path} is not an OpenSSH/PEM private key")
    encrypted, embedded = _private_header(pem)
    pub_path = path.with_name(path.name + ".pub")
    pub = _read_key_file(pub_path) if pub_path.exists() else ""
    derived = ""
    # On phones there is no ssh-keygen: the public blob is embedded in the OpenSSH private file and is enough.
    if shutil.which("ssh-keygen") and (not pub or passphrase or not encrypted):
        derived = _public_from_private(pem, passphrase)
    if pub or derived:
        key_type, blob, comment = _public_parts(pub or derived)
    elif embedded is not None:
        key_type, blob = public_blob_parts(embedded)
        comment = ""
    else:
        raise SshKeyError(f"could not read the public half of {path}; put a .pub file beside it")
    expected = _public_parts(derived)[1] if derived else embedded
    if expected is not None and expected != blob:
        raise SshKeyError(f"{pub_path} does not match the private key {path}; replace or remove it")
    comment = _clean_comment(comment)
    key_id = _key_id()
    key = SshKey(
        id=key_id,
        label=label or path.stem,
        type=key_type,
        private_file=f"keys/{key_id}.pem",
        public_key=public_line(key_type, blob, comment),
        fingerprint=fingerprint(blob),
        comment=comment,
        encrypted=encrypted,
        created=_now(),
    )
    return _put_key(key, pem)


def _public_from_private(pem: str, passphrase: str | None) -> str:
    tool = _ssh_keygen()
    with tempfile.TemporaryDirectory(prefix="quak-key.") as tmp:
        private = Path(tmp) / "key"
        _write_private_file(private, pem)
        args = [tool, "-y", "-f", str(private)]
        if passphrase:
            args += ["-P", passphrase]
        result = _run_keygen(args)
        if result.returncode:
            raise SshKeyError(
                "could not open the private key; check the file and the passphrase, "
                "or put the .pub file beside an encrypted key"
            )
        return result.stdout.strip()


# ---------------------------------------------------------------- authorized_keys text


def parse_authorized_keys(text: str) -> list[str]:
    """The non-blank lines, comments included. Only a newline ends a line, as it does for sshd."""
    return [line.strip() for line in text.split("\n") if line.strip()]


def _line_blob(line: str) -> str | None:
    """The base64 key on an authorized_keys line; None for a comment or a line with no valid key."""
    line = line.strip()
    if line.startswith("#"):
        return None
    fields = _FIELD.findall(line)
    # Any options come first, so the key type is the first field that looks like one.
    index = next((i for i, field in enumerate(fields) if field.startswith(_KEY_TYPE_PREFIXES)), None)
    if index is None or index + 1 == len(fields):
        return None
    try:
        base64.b64decode(fields[index + 1], validate=True)
    except ValueError:
        return None
    return fields[index + 1]


def authorized_blobs(text: str) -> set[str]:
    return {blob for line in parse_authorized_keys(text) if (blob := _line_blob(line))}


def _encoded_blob(public_key: str) -> str:
    _, blob, _ = _public_parts(public_key)
    return base64.b64encode(blob).decode("ascii")


def has_public_key(text: str, public_key: str) -> bool:
    return _encoded_blob(public_key) in authorized_blobs(text)


def add_public_key(text: str, public_key: str) -> str:
    """Append the key unless it is already active. Existing lines are kept exactly as they are."""
    if has_public_key(text, public_key):
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    key_type, blob, comment = _public_parts(public_key)
    return text + public_line(key_type, blob, comment) + "\n"


def remove_public_key(text: str, public_key: str) -> str:
    """Drop every active line holding this key. Other lines, comments included, are kept as they are."""
    wanted = _encoded_blob(public_key)
    kept = "\n".join(line for line in text.split("\n") if _line_blob(line) != wanted)
    return kept + "\n" if kept and not kept.endswith("\n") else kept


def key_count(text: str) -> int:
    """How many distinct keys could log in; comments and malformed lines don't count."""
    return len(authorized_blobs(text))

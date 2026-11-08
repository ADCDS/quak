"""The Lab's toolkit of kernel modules and tools: what a pack declares, what the card says is installed, and loading it.

``install_core`` only writes to the card. A module reaches the kernel through an explicit ``load_core`` and nothing
else; ``unload_core`` and ``remove_core`` undo it.

A pack is a folder with a ``manifest.json``, ``modules/<name>.ko``, ``tools/<name>`` and maybe a ``firmware/``.
Module names in one come in two spellings: the file is ``snd-pcm.ko`` and other modules' ``depends=`` say
``snd-pcm``, while the kernel calls it ``snd_pcm``. A name keeps its spelling, because it names a file, and two
names are one module if the kernel would call them the same.

``install_pack`` is the same for a pack that doesn't ship with Quak: a folder with a ``manifest.sig`` beside its
``manifest.json``, made with ``ssh-keygen -Y sign``. It is installed only if ``ssh-keygen -Y verify`` accepts that
signature against a signers file the user names, and nothing in it is trusted before that. Like ``install_core``
it only writes to the card and never loads a module.

``install_pack_url`` gets such a pack as a zip first, from an ``https://`` or ``file://`` URL and never plain
``http://``. The download is capped in size and has to match a SHA-256 the user gave, ``extract_pack`` only unpacks
what a pack's layout allows, and then it is ``install_pack``, signature and all. The hash is no substitute for the
signature: it says this is the file the user meant, and the signature says whose pack it is.

``state.json`` on the card records what is installed::

    {"version": 1, "pack": "core", "files": ["manifest.json", "modules/snd.ko", "tools/tinycap"]}

The file names are relative to the pack's folder, and that folder follows from the pack's id, so a state
file can't point anywhere else. Any computer can edit the card, so all of it is checked before any of it
reaches a shell.
"""

from __future__ import annotations

import contextlib
import dataclasses
import graphlib
import hashlib
import http.client
import json
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import IO

from quak.core.errors import NetworkError, ToolkitError
from quak.lab.models import PackManifest, PackModule, PackState, PackTool
from quak.lab.remote import Remote
from quak.ssh.models import ExecResult

# Data, beside the module of the same name. It must never get an __init__.py: a package would shadow this file.
CORE_DIR = Path(__file__).parent / "toolkit" / "core"
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.sig"  # ``ssh-keygen -Y sign`` over the manifest's bytes, beside the manifest
PACK_IDENTITY = "quak-pack"  # the principal in the signers file that a pack's signature has to belong to
PACK_NAMESPACE = "quak-pack"  # what the signature was made for, so one made for something else can't be reused here

TOOLKIT_DIR = "/tmp/fuse_d/quak/toolkit"
PACKS_DIR = f"{TOOLKIT_DIR}/packs"
PACK_DIR = f"{PACKS_DIR}/{{id}}"  # where one pack lives on the card: ``PACK_DIR.format(id=manifest.id)``
STATE_FILE = f"{TOOLKIT_DIR}/state.json"
STATE_VERSION = 1
STAGE_DIR = "/dev/quak-toolkit"  # tmpfs: what insmod reads, in the camera's RAM rather than on the card
FIRMWARE_PATH_PARAM = "/sys/module/firmware_class/parameters/path"

_TIMEOUT = 30.0
_VERIFY_TIMEOUT = 20.0
_NO_STATE = 3

# A pack that is fetched rather than copied: only these URLs, and these limits on what comes back.
PACK_URL_SCHEMES = ("https", "file")  # not http: nothing on the way may read or change a pack that is being fetched
PACK_TOP_DIRS = ("modules", "tools", "firmware")  # beside manifest.json and manifest.sig, all a pack's zip may hold
MAX_PACK_BYTES = 64 * 1024 * 1024  # the download
MAX_UNPACKED_BYTES = 256 * 1024 * 1024  # what the zip says it holds, all files together
MAX_PACK_FILES = 1024
_CHUNK = 64 * 1024

# Through exec because read_file raises the same NetworkError for a missing file as for a lost link.
READ_STATE_SCRIPT = f"[ -e {STATE_FILE} ] || exit {_NO_STATE}\ncat {STATE_FILE}\n"
READ_KERNEL_SCRIPT = 'echo "kernel=$(uname -r)"\n'

_MODULE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
_PATH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._,+-]*")  # the first character rules out ".", ".." and option-likes
_SHA256 = re.compile(r"[0-9a-f]{64}")  # what sha256sum prints, since that is what these get compared with


def _kernel_name(module: str) -> str:
    return module.replace("-", "_")


def _is_pack_path(path: str) -> bool:
    return all(_PATH_PART.fullmatch(part) for part in path.split("/"))


def _decode_json(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:  # bad bytes are a ValueError too; RecursionError is absurd nesting
        raise ToolkitError(f"it is not valid JSON: {exc}") from None


def _text(data: dict, key: str, where: str = "") -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ToolkitError(f"{where}{key} must be a string")
    return value


def _texts(data: dict, key: str, where: str = "") -> list[str]:
    value = data.get(key, [])
    # Checked before anything loops over it: "deps": "snd" would otherwise read as the modules s, n and d.
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ToolkitError(f"{where}{key} must be a list of strings")
    return value


def _hash_map(data: dict, key: str) -> dict[str, str]:
    value = data.get(key, {})
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ToolkitError(f"{key} must be an object of path to sha256")
    return value


def _parse_module(entry: object, index: int) -> PackModule:
    where = f"modules[{index}]"
    if not isinstance(entry, dict):
        raise ToolkitError(f"{where} must be an object")
    where += "."
    return PackModule(
        name=_text(entry, "name", where),
        deps=_texts(entry, "deps", where),
        sha256=_text(entry, "sha256", where),
    )


def _parse_tools(data: dict) -> list[str | PackTool]:
    value = data.get("tools", [])
    if not isinstance(value, list) or not all(isinstance(item, (str, dict)) for item in value):
        raise ToolkitError("tools must be a list of strings or objects")
    tools: list[str | PackTool] = []
    for index, entry in enumerate(value):
        if isinstance(entry, dict):
            where = f"tools[{index}]."
            entry = PackTool(name=_text(entry, "name", where), sha256=_text(entry, "sha256", where))
        tools.append(entry)
    return tools


def _parse_manifest(data: object) -> PackManifest:
    if not isinstance(data, dict):
        raise ToolkitError("it is not a JSON object")
    modules = data.get("modules")
    if not isinstance(modules, list):
        raise ToolkitError("modules must be a list")
    config_sha256 = data.get("config_sha256")
    if config_sha256 is not None and not isinstance(config_sha256, str):
        raise ToolkitError("config_sha256 must be a string or null")
    return PackManifest(
        id=_text(data, "id"),
        version=_text(data, "version"),
        kernel=_text(data, "kernel"),
        vermagic=_text(data, "vermagic"),
        config_sha256=config_sha256,
        modules=[_parse_module(entry, index) for index, entry in enumerate(modules)],
        tools=_parse_tools(data),
        firmware=_texts(data, "firmware"),
        firmware_sha256=_hash_map(data, "firmware_sha256"),
        license=_text(data, "license"),
        source=_text(data, "source"),
    )


def _validate_modules(modules: list[PackModule]) -> None:
    graph: dict[str, set[str]] = {}
    for module in modules:
        for name in (module.name, *module.deps):
            if not _MODULE_NAME.fullmatch(name):
                raise ToolkitError(f"{name!r} is not a kernel module name")
        if not _SHA256.fullmatch(module.sha256):
            raise ToolkitError(f"module {module.name} needs a sha256 of 64 lowercase hex digits")
        name = _kernel_name(module.name)
        if name in graph:
            raise ToolkitError(f"module {name} is listed twice")
        graph[name] = {_kernel_name(dep) for dep in module.deps}
    # A dependency has to be in the pack: loading goes through the pack's own modules in order, so nothing else
    # would ever provide it.
    for name, deps in graph.items():
        unknown = deps - graph.keys()
        if unknown:
            raise ToolkitError(f"module {name} depends on {', '.join(sorted(unknown))}, which the pack doesn't have")
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as exc:
        raise ToolkitError(f"the module dependencies go in a circle: {' -> '.join(exc.args[1])}") from None


def validate_manifest(manifest: PackManifest) -> None:
    """Raise ``ToolkitError`` unless the manifest's names, paths, hashes and dependencies are all usable.

    ``kernel`` and ``vermagic`` are required: a pack that doesn't say what it was built for could never pass
    ``check_compatible``.
    """
    if not _PATH_PART.fullmatch(manifest.id):
        raise ToolkitError(f"{manifest.id!r} can't be a pack id")
    for key in ("version", "kernel", "vermagic"):
        if not getattr(manifest, key).strip():
            raise ToolkitError(f"{key} is missing")
    if manifest.vermagic.split()[0] != manifest.kernel.strip():
        raise ToolkitError(f"vermagic {manifest.vermagic!r} is not for kernel {manifest.kernel!r}")
    if manifest.config_sha256 is not None and not _SHA256.fullmatch(manifest.config_sha256):
        raise ToolkitError("config_sha256 must be null or 64 lowercase hex digits")
    _validate_modules(manifest.modules)
    for key in ("tools", "firmware"):
        for entry in getattr(manifest, key):
            path = entry.name if isinstance(entry, PackTool) else entry
            if not _is_pack_path(path):
                raise ToolkitError(f"{key} entry {path!r} is not a path inside the pack")
            if isinstance(entry, PackTool) and not _SHA256.fullmatch(entry.sha256):
                raise ToolkitError(f"tool {path} needs a sha256 of 64 lowercase hex digits")
    for name, digest in manifest.firmware_sha256.items():
        if name not in manifest.firmware:
            raise ToolkitError(f"firmware hash for {name!r}, which the pack doesn't list")
        if not _SHA256.fullmatch(digest):
            raise ToolkitError(f"firmware {name} needs a sha256 of 64 lowercase hex digits")


def _manifest_from_bytes(raw: bytes, path: str | Path) -> PackManifest:
    """Parse and validate manifest bytes already read from ``path``, which only names them in the error."""
    try:
        manifest = _parse_manifest(_decode_json(raw))
        validate_manifest(manifest)
    except ToolkitError as exc:
        raise ToolkitError(f"bad pack manifest {path}: {exc}") from None
    return manifest


def load_manifest(path: str | Path) -> PackManifest:
    """Read and validate a pack's manifest, ignoring unknown fields. Anything wrong raises ``ToolkitError``."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ToolkitError(f"could not read the pack manifest {path}: {exc.strerror or exc}") from None
    return _manifest_from_bytes(raw, path)


def manifest_to_dict(manifest: PackManifest) -> dict:
    """The manifest as ``manifest.json`` holds it, without an empty map for packs that ship no firmware."""
    data = dataclasses.asdict(manifest)
    if not data["firmware_sha256"]:
        data.pop("firmware_sha256")
    return data


def core_manifest() -> PackManifest:
    """The manifest of the pack that ships with Quak."""
    return load_manifest(CORE_DIR / MANIFEST_NAME)


def sha256_file(path: str | Path) -> str:
    """A local file's SHA-256 in lowercase hex. An unreadable file raises ``ToolkitError``."""
    try:
        with open(path, "rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()
    except OSError as exc:
        raise ToolkitError(f"could not read {path}: {exc.strerror or exc}") from None


def _words(text: str) -> str:
    """What a value says, whatever a shell's newline or a vermagic's trailing space added to it."""
    return " ".join(text.split())


def check_compatible(manifest: PackManifest, kernel: str, vermagic: str) -> None:
    """Raise ``ToolkitError`` unless the pack was built for exactly the running kernel.

    ``kernel`` is the camera's ``uname -r``. Not knowing a value counts as a mismatch, so two empty strings
    don't pass as equal.
    """
    for what, built_for, running in (("kernel", manifest.kernel, kernel), ("vermagic", manifest.vermagic, vermagic)):
        built_for, running = _words(built_for), _words(running)
        if not running or built_for != running:
            raise ToolkitError(
                f"pack {manifest.id} is for {what} {built_for or '(unknown)'}, and the camera reports "
                f"{running or '(unknown)'}. A module built for another kernel can crash the camera, so the pack "
                "was refused"
            )


def _detail(result: ExecResult) -> str:
    return result.stderr.decode("utf-8", "replace").strip() or f"exit code {result.exit_code}"


def _parse_state(raw: bytes) -> tuple[str, list[str]]:
    """The pack's id and its files, each safe to use in a path. Raises ``ToolkitError`` for anything else."""
    data = _decode_json(raw)
    if not isinstance(data, dict):
        raise ToolkitError("it is not a JSON object")
    if data.get("version") != STATE_VERSION:
        raise ToolkitError(f"its version is not {STATE_VERSION}")
    pack = data.get("pack")
    if not isinstance(pack, str) or not _PATH_PART.fullmatch(pack):
        raise ToolkitError("it doesn't name a usable pack")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise ToolkitError("it doesn't list the pack's files")
    if not all(isinstance(file, str) and _is_pack_path(file) for file in files):
        raise ToolkitError("it lists a file outside the pack")
    return pack, files


def _missing_files_script(root: str, files: list[str]) -> str:
    names = " ".join(shlex.quote(file) for file in files)
    return f'root={shlex.quote(root)}\nfor f in {names}; do [ -e "$root/$f" ] || echo "missing=$f"; done\n'


def toolkit_state(remote: Remote) -> PackState:
    """What the card says is installed, checked against the files there. Changes nothing.

    No state file just means not installed. A lost link raises ``NetworkError``; a failed read, a corrupt
    state file or a missing file is in ``error``. ``loaded`` stays False: this doesn't ask the kernel.
    """
    result = remote.exec(READ_STATE_SCRIPT, timeout=_TIMEOUT)
    if result.exit_code == _NO_STATE:
        return PackState()
    if result.exit_code != 0:
        return PackState(error=f"could not read {STATE_FILE}: {_detail(result)}")
    try:
        pack, files = _parse_state(result.stdout)
    except ToolkitError as exc:
        return PackState(error=f"{STATE_FILE} is corrupt: {exc}. Run `quak lab toolkit install` to install it again")
    path = f"{PACKS_DIR}/{pack}"
    result = remote.exec(_missing_files_script(path, files), timeout=_TIMEOUT)
    if result.exit_code != 0:
        return PackState(path=path, error=f"could not check the files in {path}: {_detail(result)}")
    missing = [line.removeprefix("missing=") for line in result.text.splitlines() if line.startswith("missing=")]
    if missing:
        return PackState(
            path=path,
            error=f"{path} is missing {', '.join(missing)}. Run `quak lab toolkit install` to install it again",
        )
    return PackState(installed=True, path=path)


def _read_pack_file(root: Path, name: str) -> bytes:
    path = root / name
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ToolkitError(f"could not read {path}: {exc.strerror or exc}") from None


def _read_core_file(name: str) -> bytes:
    return _read_pack_file(CORE_DIR, name)


def _declared_hashes(manifest: PackManifest) -> dict[str, str]:
    """The SHA-256 the manifest gives each module and tool, by the file's path in the pack."""
    expected = {f"modules/{module.name}.ko": module.sha256 for module in manifest.modules}
    for tool in manifest.tools:
        if not isinstance(tool, PackTool):
            raise ToolkitError(f"tool {tool} has no sha256 to verify it with, so the pack was refused")
        expected[f"tools/{tool.name}"] = tool.sha256
    for name, digest in manifest.firmware_sha256.items():
        expected[f"firmware/{name}"] = digest
    return expected


def _hashed_files(root: Path, raw_manifest: bytes, expected: dict[str, str]) -> dict[str, tuple[bytes, str]]:
    """Each file the pack installs: its path in the pack, its bytes and the SHA-256 they must have on the card.

    The manifest can't hold its own hash, so the card's copy is held to the one of the bytes given here. Each
    file is read once, and the bytes that were hashed are the bytes that get uploaded.
    """
    files = {MANIFEST_NAME: (raw_manifest, hashlib.sha256(raw_manifest).hexdigest())}
    for name, sha256 in expected.items():
        data = _read_pack_file(root, name)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ToolkitError(f"{root / name} doesn't match the sha256 in its manifest, so the pack was refused")
        files[name] = (data, sha256)
    return files


def _core_files(manifest: PackManifest) -> dict[str, tuple[bytes, str]]:
    expected = _declared_hashes(manifest)
    return _hashed_files(CORE_DIR, _read_core_file(MANIFEST_NAME), expected)


def _firmware_files(root: Path, manifest: PackManifest) -> dict[str, tuple[bytes, str]]:
    """The pack's firmware, each held to the hash the signed manifest gives it."""
    files = {}
    for entry in manifest.firmware:
        name = f"firmware/{entry}"
        data = _read_pack_file(root, name)
        expected = manifest.firmware_sha256.get(entry)
        if expected is None:
            raise ToolkitError(f"firmware {entry} has no sha256 in the manifest, so the pack was refused")
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ToolkitError(f"firmware {entry} does not match its manifest sha256")
        files[name] = (data, expected)
    return files


def _prepare_script(root: str, names: list[str]) -> str:
    # The old state goes first: toolkit_state only checks that files exist, so an install that stops halfway
    # must not leave the card still saying the pack is installed.
    dirs = sorted({posixpath.dirname(f"{root}/{name}") for name in names})
    return f"rm -f {STATE_FILE} && mkdir -p {' '.join(shlex.quote(path) for path in dirs)}\n"


def _verify_script(root: str, names: list[str], executable: list[str] | None = None) -> str:
    chmod = f"chmod 755 {' '.join(shlex.quote(name) for name in executable)} && " if executable else ""
    return f"cd {shlex.quote(root)} && {chmod}sha256sum {' '.join(shlex.quote(name) for name in names)}\n"


def _reported_hashes(output: str) -> dict[str, str]:
    """``<hash>  <name>`` lines by name. Other lines, such as a login notice, name no file anyone looks up."""
    reported = {}
    for line in output.splitlines():
        digest, _, name = line.partition("  ")
        reported[name] = digest
    return reported


def _upload_pack(remote: Remote, pack_id: str, files: dict[str, tuple[bytes, str]], retry: str) -> PackState:
    """Copy the files to the pack's folder on the card, make its tools runnable, verify each one, and record it.

    The old state file goes first and the new one is written last, so the card only says the pack is
    installed once all of it checked out. ``retry`` is the command that tries again.
    """
    names = list(files)
    root = PACK_DIR.format(id=pack_id)

    result = remote.exec(_prepare_script(root, names), timeout=_TIMEOUT)
    if result.exit_code != 0:
        raise ToolkitError(f"could not prepare {root}: {_detail(result)}")
    for name, (data, _) in files.items():
        remote.write_file(f"{root}/{name}", data)

    # write_file leaves everything 0600, which no tool can run from.
    tools = [name for name in names if name.startswith("tools/")]
    result = remote.exec(_verify_script(root, names, executable=tools), timeout=_TIMEOUT)
    if result.exit_code != 0:
        raise ToolkitError(f"could not verify the files in {root}: {_detail(result)}")
    reported = _reported_hashes(result.text)
    bad = [name for name, (_, sha256) in files.items() if reported.get(name) != sha256]
    if bad:
        raise ToolkitError(
            f"{', '.join(bad)} in {root} failed the sha256 check, so the pack was not installed. "
            f"Run `{retry}` to try again"
        )

    state = {"version": STATE_VERSION, "pack": pack_id, "files": names}
    remote.write_file(STATE_FILE, json.dumps(state).encode())
    return PackState(installed=True, path=root)


def install_core(remote: Remote) -> PackState:
    """Copy the bundled pack to the card, make its tools runnable, verify every file, and record it. Loads nothing.

    The old state file goes first and the new one is written last, so the card only says the pack is
    installed once all of it checked out. A bad bundle raises ``ToolkitError`` before the camera is touched.
    A failure on the camera raises ``ToolkitError`` too, and a lost link ``NetworkError``. Whatever was
    uploaded by then stays for the next install to overwrite.
    """
    manifest = core_manifest()
    files = _core_files(manifest)
    return _upload_pack(remote, manifest.id, files, "quak lab toolkit install")


# What runs ssh-keygen: its arguments (the program first) and the manifest for its stdin, and what came out.
Verifier = Callable[[list[str], bytes], ExecResult]


def _run_ssh_keygen(argv: list[str], data: bytes) -> ExecResult:
    """Run ssh-keygen as an argument list, away from any terminal, and give up after ``_VERIFY_TIMEOUT``."""
    tool = shutil.which(argv[0])
    if not tool:
        raise ToolkitError("ssh-keygen was not found, so the pack's signature can't be checked. Install OpenSSH")
    try:
        done = subprocess.run(
            [tool, *argv[1:]],
            input=data,
            capture_output=True,
            timeout=_VERIFY_TIMEOUT,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        raise ToolkitError("ssh-keygen did not finish checking the pack's signature in time") from None
    except OSError as exc:
        raise ToolkitError(f"could not run ssh-keygen: {exc.strerror or exc}") from None
    return ExecResult(exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr)


def verify_pack_signature(
    pack_dir: str | Path,
    allowed_signers: str | Path,
    *,
    identity: str = PACK_IDENTITY,
    namespace: str = PACK_NAMESPACE,
    verifier: Verifier | None = None,
) -> bytes:
    """Check ``manifest.sig`` against ``manifest.json`` with ``ssh-keygen -Y verify``, and return the manifest's bytes.

    Both files are required, so a pack without a signature is refused. The signers file is the user's list of
    keys they trust; ``identity`` must be one of its principals and ``namespace`` what the signature was made
    for. The bytes that were checked are the ones returned, to be parsed in place of reading the file again:
    a file that changes in between can't swap a different manifest in. A signature that isn't accepted, or an
    ssh-keygen that can't run, raises ``ToolkitError``. ``verifier`` replaces the process, for tests.
    """
    if not identity or not namespace:
        raise ToolkitError("the signer identity and the signature namespace can't be empty")
    pack_dir = Path(pack_dir)
    manifest_path, signature_path = pack_dir / MANIFEST_NAME, pack_dir / SIGNATURE_NAME
    if not signature_path.is_file():
        raise ToolkitError(f"{pack_dir} has no {SIGNATURE_NAME}, and an unsigned pack is refused")
    signers = Path(allowed_signers)
    if not signers.is_file():
        raise ToolkitError(f"the allowed signers file {signers} was not found, so the pack was refused")
    raw = _read_pack_file(pack_dir, MANIFEST_NAME)
    argv = [
        "ssh-keygen",
        "-Y",
        "verify",
        "-f",
        str(signers.resolve()),
        "-I",
        identity,
        "-n",
        namespace,
        "-s",
        str(signature_path.resolve()),
    ]
    result = (verifier or _run_ssh_keygen)(argv, raw)
    if result.exit_code != 0:
        why = result.stderr.decode("utf-8", "replace").strip() or result.text.strip() or f"exit code {result.exit_code}"
        raise ToolkitError(
            f"the signature {signature_path} was not accepted for {manifest_path} ({why}), so the pack was refused"
        )
    return raw


def install_pack(
    remote: Remote,
    pack_dir: str | Path,
    allowed_signers: str | Path,
    *,
    verifier: Verifier | None = None,
) -> PackState:
    """Install a signed pack from a folder on this computer to the card. Loads nothing, not even afterwards.

    In this order: the signature must be accepted (nothing else in the folder is looked at before that), then
    the manifest is read from the very bytes that were verified, then every file in the folder must match the
    sha256 the manifest gives it, then the pack must be built for the camera's kernel. Only then is the camera
    written to, and every file is checked there again before ``state.json`` says the pack is installed. All of
    the checks before that raise ``ToolkitError`` with the camera untouched.

    Firmware has no hash in a manifest, so the signature doesn't vouch for it. It is only checked to arrive
    intact. Like ``install_core``, a failure on the camera leaves what was uploaded for the next try.
    """
    return _install_signed_pack(remote, pack_dir, allowed_signers, verifier, "quak lab toolkit install-pack")


def _install_signed_pack(
    remote: Remote, pack_dir: str | Path, allowed_signers: str | Path, verifier: Verifier | None, retry: str
) -> PackState:
    """What ``install_pack`` does. ``retry`` is the command that tries again, for the messages that say so."""
    pack_dir = Path(pack_dir)
    raw = verify_pack_signature(pack_dir, allowed_signers, verifier=verifier)
    manifest = _manifest_from_bytes(raw, pack_dir / MANIFEST_NAME)
    files = _hashed_files(pack_dir, raw, _declared_hashes(manifest))
    files.update(_firmware_files(pack_dir, manifest))
    _check_running_kernel(remote, manifest)
    return _upload_pack(remote, manifest.id, files, retry)


# What fetches a URL: given the URL and a timeout it returns something to read bytes from, and has a ``close()``.
Opener = Callable[[str, float], IO[bytes]]


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https:// URL, so a server can't bounce a download down to http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(req.full_url, code, f"redirected to {newurl}, which is not https", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str, timeout: float) -> IO[bytes]:
    """Open an https:// or file:// URL and nothing else: no http, no ftp, no data, and https certificates are checked."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        _HttpsOnlyRedirect(),
        urllib.request.HTTPSHandler(),
        urllib.request.FileHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener.open(url, timeout=timeout)


def _why(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def check_pack_source(url: str, sha256: str) -> str:
    """Raise ``ToolkitError`` unless ``url`` is an https:// or file:// URL and ``sha256`` is a SHA-256. Returns the digest.

    Plain http:// is refused, and so is every other scheme. A file:// URL has to be on this computer. The digest
    comes back in lowercase, which is how hashes are compared here. Nothing is fetched.
    """
    # Spaces and control characters are refused rather than trimmed: what is checked here is what gets fetched.
    if not url or any(char.isspace() or not char.isprintable() for char in url):
        raise ToolkitError(f"{url!r} is not a usable URL")
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        raise ToolkitError(f"{url!r} is not a usable URL: {exc}") from None
    if parts.scheme not in PACK_URL_SCHEMES:
        raise ToolkitError(
            f"a pack is only fetched from an https:// or file:// URL, and {url} is neither, so it was refused"
        )
    if parts.scheme == "https" and not host:
        raise ToolkitError(f"{url} names no host")
    if parts.scheme == "file" and parts.netloc not in ("", "localhost"):
        raise ToolkitError(f"{url} is not on this computer, so it was refused")
    digest = sha256.strip().lower()
    if not _SHA256.fullmatch(digest):
        raise ToolkitError("the pack's sha256 must be 64 hex digits")
    return digest


def _copy_capped(source: IO[bytes], target: IO[bytes], max_bytes: int) -> tuple[str, int] | None:
    """Copy at most ``max_bytes`` and return the SHA-256 of it and its size, or None if there was more."""
    hasher = hashlib.sha256()
    total = 0
    # One byte past the limit is asked for, so a source that is exactly at it is told apart from one that goes on.
    while chunk := source.read(min(_CHUNK, max_bytes + 1 - total)):
        total += len(chunk)
        if total > max_bytes:
            return None
        hasher.update(chunk)
        target.write(chunk)
    return hasher.hexdigest(), total


def download_pack(
    url: str,
    sha256: str,
    dest_dir: str | Path,
    *,
    timeout: float = 30,
    max_bytes: int = MAX_PACK_BYTES,
    opener: Opener | None = None,
) -> Path:
    """Fetch a pack's zip from an https:// or file:// URL into ``dest_dir`` and return the file, once it has the ``sha256``.

    The bytes go to a temporary file in ``dest_dir`` that never holds more than ``max_bytes``: a download that goes
    past it is stopped and refused. Only a file with exactly the asked-for hash is moved into place, as
    ``pack-<hash>.zip``; anything else is deleted, so a refused download leaves nothing behind. The name never
    comes from the URL. ``timeout`` is for each wait on the network, as in ``urllib``, not for the whole download.
    A redirect may only go to another https:// URL. Any failure raises ``ToolkitError``. ``opener`` replaces the
    fetching, for tests: ``opener(url, timeout)`` returns a readable binary file.
    """
    digest = check_pack_source(url, sha256)
    dest_dir = Path(dest_dir)
    final = dest_dir / f"pack-{digest[:16]}.zip"
    temp: Path | None = None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with (
            contextlib.closing((opener or _open_url)(url, timeout)) as response,
            tempfile.NamedTemporaryFile(dir=dest_dir, prefix=".download-", delete=False) as part,
        ):
            temp = Path(part.name)
            copied = _copy_capped(response, part, max_bytes)
        if copied is None:
            raise ToolkitError(
                f"{url} is more than the {max_bytes / 2**20:g} MiB a pack may be, so the download was stopped "
                "and the pack refused"
            )
        if copied[0] != digest:
            raise ToolkitError(
                f"{url} has sha256 {copied[0]}, not the {digest} that was asked for, so the pack was refused"
            )
        temp.replace(final)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise ToolkitError(f"could not download {url}: {_why(exc)}") from None
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)  # already gone after a successful move
    return final


def _check_zip_member(info: zipfile.ZipInfo) -> tuple[str, bool]:
    """A member's name in the pack, and whether it is a folder. Raises ``ToolkitError`` unless it is one a pack may hold."""
    raw = info.filename
    where = f"the pack's zip has {raw!r}, which"
    if raw.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", raw):
        raise ToolkitError(f"{where} is an absolute path, so the pack was refused")
    if ".." in re.split(r"[\\/]", raw):
        raise ToolkitError(f"{where} climbs out of the pack with .., so the pack was refused")
    # The Unix type, if the zip's maker recorded one: many record only permissions, which say nothing about a type.
    kind = stat.S_IFMT(info.external_attr >> 16)
    if kind == stat.S_IFLNK:
        raise ToolkitError(f"{where} is a symlink, so the pack was refused")
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ToolkitError(f"{where} is neither a file nor a folder, so the pack was refused")
    if info.flag_bits & 0x1:
        raise ToolkitError(f"{where} is encrypted, so the pack was refused")
    is_dir = raw.endswith("/")
    name = raw[:-1] if is_dir else raw
    parts = name.split("/")
    if not _is_pack_path(name):
        raise ToolkitError(f"{where} is not a plain path inside the pack, so the pack was refused")
    if is_dir:
        inside = parts[0] in PACK_TOP_DIRS
    else:
        inside = name in (MANIFEST_NAME, SIGNATURE_NAME) or (len(parts) > 1 and parts[0] in PACK_TOP_DIRS)
    if not inside:
        raise ToolkitError(
            f"{where} is outside a pack's layout ({MANIFEST_NAME}, {SIGNATURE_NAME}, "
            f"{', '.join(f'{top}/' for top in PACK_TOP_DIRS)}), so the pack was refused"
        )
    return name, is_dir


def _check_zip(infos: list[zipfile.ZipInfo], max_bytes: int) -> None:
    """Refuse a zip that holds anything else than a pack, or too much of it, before any of it is written."""
    if len(infos) > MAX_PACK_FILES:
        raise ToolkitError(f"the pack's zip has {len(infos)} entries, more than the {MAX_PACK_FILES} a pack may have")
    if sum(info.file_size for info in infos) > max_bytes:
        raise ToolkitError(f"the pack's zip unpacks to more than {max_bytes / 2**20:g} MiB, so the pack was refused")
    seen: set[str] = set()
    files: set[str] = set()
    for info in infos:
        name, is_dir = _check_zip_member(info)
        if name in seen:
            raise ToolkitError(f"the pack's zip has {name!r} twice, so the pack was refused")
        seen.add(name)
        if not is_dir:
            files.add(name)
    if MANIFEST_NAME not in files:
        raise ToolkitError(f"the pack's zip has no {MANIFEST_NAME}")
    if SIGNATURE_NAME not in files:
        raise ToolkitError(f"the pack's zip has no {SIGNATURE_NAME}, and an unsigned pack is refused")


def extract_pack(archive: str | Path, dest: str | Path, *, max_bytes: int = MAX_UNPACKED_BYTES) -> Path:
    """Unpack a pack's zip into the empty folder ``dest`` and return it: the pack's folder, to give ``install_pack``.

    Every entry is checked before the first is written, and any of these refuses the whole zip: an absolute path,
    ``..``, a symlink or any other special file, an encrypted entry, a name that isn't a plain path, anything outside
    the layout (``manifest.json``, ``manifest.sig``, ``modules/``, ``tools/``, ``firmware/``), a repeated name, more
    entries or bytes than a pack may have, and a ``manifest.json`` or ``manifest.sig`` that isn't there. Files are
    written by name from the zip's contents alone, with default permissions: nothing in the zip decides a mode or a
    place. A bad zip, or a file that doesn't match its checksum, raises ``ToolkitError`` partway through, and what
    was written stays for the caller to delete. Nothing in the zip is trusted by being here: ``install_pack`` is
    what checks the signature.
    """
    archive, dest = Path(archive), Path(dest)
    try:
        with zipfile.ZipFile(archive) as zipped:
            infos = zipped.infolist()
            _check_zip(infos, max_bytes)
            dest.mkdir(parents=True, exist_ok=True)
            if any(dest.iterdir()):
                raise ToolkitError(f"{dest} is not empty, and a pack is only unpacked into a new folder")
            for info in infos:
                target = dest / info.filename
                if info.filename.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # "x" never opens an existing file, or follows a link. A zipfile read stops at the size it declares.
                with zipped.open(info) as source, open(target, "xb") as out:
                    shutil.copyfileobj(source, out, _CHUNK)
    except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError, RuntimeError, zlib.error) as exc:
        raise ToolkitError(f"could not unpack the pack {archive}: {_why(exc)}") from None
    return dest


def install_pack_url(
    remote: Remote,
    url: str,
    sha256: str,
    allowed_signers: str | Path,
    *,
    verifier: Verifier | None = None,
    opener: Opener | None = None,
) -> PackState:
    """Fetch a signed pack's zip from an https:// or file:// URL, and install it as ``install_pack`` does. Loads nothing.

    In this order: the URL and hash are checked for form, the zip is downloaded and held to ``sha256``, it is
    unpacked into a new folder, and then it goes through ``install_pack``, whose signature, hash and kernel
    checks all come before the camera is written to. The hash only says the download is the file the user
    meant; whether to trust what is in it is for the signature. Everything fetched lives in a temporary folder
    that is deleted afterwards, whether the install worked or not. A failure raises ``ToolkitError``, with the
    camera untouched up to the point ``install_pack`` says.
    """
    check_pack_source(url, sha256)
    with tempfile.TemporaryDirectory(prefix="quak-pack-") as work:
        archive = download_pack(url, sha256, Path(work) / "download", opener=opener)
        pack_dir = extract_pack(archive, Path(work) / "pack")
        return _install_signed_pack(remote, pack_dir, allowed_signers, verifier, "quak lab toolkit install-pack-url")


def load_order(manifest: PackManifest) -> list[PackModule]:
    """Each module after its dependencies, by always taking the first in the manifest that is ready.

    A manifest already in a working order keeps it. A validated one has no circles and no unknown dependencies,
    so one is always ready.
    """
    pending = list(manifest.modules)
    ready: set[str] = set()
    order: list[PackModule] = []
    while pending:
        module = next(entry for entry in pending if all(_kernel_name(dep) in ready for dep in entry.deps))
        pending.remove(module)
        ready.add(_kernel_name(module.name))
        order.append(module)
    return order


def _check_running_kernel(remote: Remote, manifest: PackManifest) -> None:
    """Refuse a pack built for another kernel, before anything is copied.

    A running kernel doesn't publish its vermagic, so ``uname -r`` is all that can be asked, and the manifest's
    own vermagic stands in for the rest. ``validate_manifest`` ties its release to ``kernel``, and insmod makes
    the kernel compare the rest of it. Not getting an answer is a refusal, like any unknown value.
    """
    result = remote.exec(READ_KERNEL_SCRIPT, timeout=_TIMEOUT)
    lines = result.text.splitlines()
    kernel = next((line.removeprefix("kernel=") for line in lines if line.startswith("kernel=")), "")
    check_compatible(manifest, kernel, manifest.vermagic)


def _stage_script(root: str, files: list[str]) -> str:
    names = " ".join(shlex.quote(file) for file in files)
    copy = f"mkdir -p -m 0700 {STAGE_DIR} && cd {shlex.quote(f'{root}/modules')} && cp {names} {STAGE_DIR}/"
    return f"{copy} && {_verify_script(STAGE_DIR, files)}"


def _stage_modules(remote: Remote, root: str, modules: list[PackModule]) -> None:
    """Copy the modules to tmpfs and hold the copies to the bundled manifest.

    ``toolkit_state`` only says the files exist, and any computer can have changed them since the install.
    """
    expected = {f"{module.name}.ko": module.sha256 for module in modules}
    result = remote.exec(_stage_script(root, list(expected)), timeout=_TIMEOUT)
    if result.exit_code != 0:
        raise ToolkitError(f"could not copy the modules from {root} to {STAGE_DIR}: {_detail(result)}")
    reported = _reported_hashes(result.text)
    bad = [name for name, sha256 in expected.items() if reported.get(name) != sha256]
    if bad:
        raise ToolkitError(
            f"{', '.join(bad)} from {root} failed the sha256 check, so nothing was loaded. "
            "Run `quak lab toolkit install` to install it again"
        )


def _firmware_script(root: str) -> str:
    # printf because this kernel keeps a trailing newline as part of the path, which echo would add.
    firmware = shlex.quote(f"{root}/firmware")
    return f"[ ! -d {firmware} ] || printf %s {firmware} > {FIRMWARE_PATH_PARAM}\n"


def _insmod_script(name: str) -> str:
    """Load one module and check that the kernel shows it. Already being there counts, so a cut-off load can resume."""
    sysfs = f"/sys/module/{_kernel_name(name)}"
    return (
        f"[ ! -d {sysfs} ] || exit 0\n"
        f"insmod {STAGE_DIR}/{name}.ko || exit 1\n"
        f"[ -d {sysfs} ] || {{ echo 'insmod ran, but {sysfs} is not there' >&2; exit 1; }}\n"
    )


def _unload_script(modules: list[PackModule]) -> str:
    """Unload in reverse, skipping what isn't loaded, then drop the tmpfs copies. Each failure prints ``failed=``.

    One command that ignores HUP and PIPE, so the camera finishes without the link: SSH may be running through
    the Ethernet adapter that ``rmmod r8152`` takes away.
    """
    names = [_kernel_name(module.name) for module in reversed(modules)]
    unload = "".join(f"[ ! -d /sys/module/{name} ] || rmmod {name} || echo failed={name}\n" for name in names)
    return f"trap '' HUP PIPE\n{unload}rm -rf {STAGE_DIR} 2>/dev/null || true\n"


def _unload_modules(remote: Remote, modules: list[PackModule]) -> None:
    """Run ``_unload_script``. Raises ``ToolkitError`` naming any module that stayed."""
    result = remote.exec(_unload_script(modules), timeout=_TIMEOUT)
    if result.exit_code != 0:
        raise ToolkitError(f"could not unload the modules: {_detail(result)}")
    stuck = [line.removeprefix("failed=") for line in result.text.splitlines() if line.startswith("failed=")]
    if stuck:
        raise ToolkitError(f"could not unload {', '.join(stuck)}: {_detail(result)}")


def _undo_load(remote: Remote, modules: list[PackModule], failure: Exception) -> ToolkitError | NetworkError:
    """Take a failed load's modules back out. Returns the error to raise, which says if any may still be loaded."""
    try:
        _unload_modules(remote, modules)
    except (ToolkitError, NetworkError) as exc:
        error = NetworkError if isinstance(exc, NetworkError) else ToolkitError
        return error(
            f"{failure}. Undoing the load failed too, so modules may still be loaded ({exc}). Run "
            "`quak lab toolkit unload` to try again, or reboot the camera: a module doesn't survive a reboot"
        )
    return ToolkitError(f"{failure}. Nothing was left loaded")


def load_core(remote: Remote) -> PackState:
    """Load the installed pack's modules into the running kernel: all of them, or none. Only ever on request.

    The pack must be installed and built for the running kernel, and the copies in tmpfs must match the bundled
    manifest, all before the kernel is touched. A module that won't load takes back out every module this load
    reached, including any an earlier, cut-off load left, and raises ``ToolkitError``. A lost link raises
    ``NetworkError`` if the modules could not be taken back out. The firmware path is left set either way.
    """
    manifest = core_manifest()
    state = toolkit_state(remote)
    if not state.installed:
        raise ToolkitError(state.error or "the toolkit is not installed. Run `quak lab toolkit install` first")
    _check_running_kernel(remote, manifest)
    order = load_order(manifest)
    _stage_modules(remote, state.path, order)
    result = remote.exec(_firmware_script(state.path), timeout=_TIMEOUT)
    if result.exit_code != 0:
        raise ToolkitError(f"could not set the firmware path, so nothing was loaded: {_detail(result)}")

    reached: list[PackModule] = []
    try:
        for module in order:
            reached.append(module)
            result = remote.exec(_insmod_script(module.name), timeout=_TIMEOUT)
            if result.exit_code != 0:
                raise ToolkitError(f"could not load {module.name}: {_detail(result)}")
    except (ToolkitError, NetworkError) as failure:
        raise _undo_load(remote, reached, failure) from None
    return dataclasses.replace(state, loaded=True)


def unload_core(remote: Remote) -> PackState:
    """Take the pack's modules out of the kernel, last loaded first. One that isn't loaded is simply skipped.

    Goes by the bundled manifest, so a card that was edited or swapped can't keep a module in. Tries every module,
    then raises ``ToolkitError`` naming any that stayed. A lost link raises ``NetworkError``, though the camera
    still finishes. Returns what the card says afterwards.
    """
    _unload_modules(remote, load_order(core_manifest()))
    return toolkit_state(remote)


def remove_core(remote: Remote) -> PackState:
    """Unload the pack, then delete it and its state from the card. A module that stays loaded stops the delete.

    The state file goes first, for the reason ``_prepare_script`` gives. The folder follows from the bundled
    manifest's validated id, so nothing on the card can point this ``rm -rf`` anywhere else.
    """
    manifest = core_manifest()
    _unload_modules(remote, load_order(manifest))
    root = f"{PACKS_DIR}/{manifest.id}"
    result = remote.exec(f"rm -f {STATE_FILE} && rm -rf {shlex.quote(root)}\n", timeout=_TIMEOUT)
    if result.exit_code != 0:
        raise ToolkitError(f"could not delete {root}: {_detail(result)}")
    return PackState()

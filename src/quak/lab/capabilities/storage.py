"""Volumes on the camera: find them, mount them under ``/tmp/quak/mnt``, list and copy files, unmount them.

Nothing here can wipe a volume, repartition a disk or delete a volume's contents. No function does, and
the routes that could get there are refused before a command is sent: ``copy`` only touches the SD card
and mounted volumes, so it can't be aimed at a device node, and it never replaces what is already there.
``mount`` and ``unmount`` only work under ``/tmp/quak/mnt``, so the SD card stays mounted.

A plugged-in disk holds whatever someone put on it. It mounts read-only unless the caller asks otherwise,
and never with setuid, device nodes or execution. Its label and file names are only ever data: the scripts
frame them so they can't forge a record, and non-printable characters are dropped from what is returned.
A name shown without them won't match the file on disk, which is the safe way round.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import NamedTuple

from quak.core.errors import LabError
from quak.lab.capabilities import printable, run
from quak.lab.hostmode import LAB_MOUNTS
from quak.lab.models import Volume
from quak.lab.remote import Remote

SD_CARD = "/tmp/fuse_d"
COPY_TIMEOUT = 3600.0  # a whole 4 GB chapter is read three times: once by cp and once by each checksum
_TIMEOUT = 30.0
_LIST_DIR_TIMEOUT = 120.0  # one ls per file, and a folder of videos can hold thousands
_MOUNT_TIMEOUT = 120.0  # an unmount first has to flush whatever a copy left in the page cache
_SECTOR = 512  # what /sys/class/block/*/size counts in, whatever the disk's own sector size
_MOUNT_FLAGS = "nosuid,nodev,noexec"

_BLOCK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_BLKID_FIELD = re.compile(r'(?:^|\s)(LABEL|TYPE)="((?:[^"\\]|\\.)*)"')
_SHA256 = re.compile(r"[0-9a-f]{64}")

# blkid runs once per device and only its first line is kept, so a label can't speak for another device.
LIST_SCRIPT = """\
for d in /sys/class/block/*; do
  [ -e "$d" ] || continue
  name=${d##*/}
  case $name in loop*|ram*|zram*|mtdblock*) continue ;; esac
  printf 'entry=%s\\n' "$name"
  printf 'size=%s\\n' "$(cat "$d/size" 2>/dev/null)"
  printf 'path=%s\\n' "$(cd "$d" 2>/dev/null && pwd -P)"
  printf 'blkid=%s\\n' "$(blkid "/dev/$name" 2>/dev/null | head -n 1)"
done
sed 's/^/mount=/' /proc/mounts 2>/dev/null
true
"""

# NUL between fields, because it is the one byte a file name can't hold. ls gives the size because it is
# everywhere and, unlike wc -c, never reads the file; the size comes before the name, so the name can't move it.
_LIST_DIR_SCRIPT = r"""
for f in * .[!.]* ..?*; do
  [ -e "$f" ] || [ -L "$f" ] || continue
  t=other
  s=0
  if [ -L "$f" ]; then
    t=link
  elif [ -d "$f" ]; then
    t=dir
  elif [ -f "$f" ]; then
    t=file
    s=$(ls -ldn "./$f" 2>/dev/null | { read -r x x x x n x; echo "$n"; })
  fi
  printf '%s\0%s\0%s\0' "$t" "$s" "$f"
done
"""

# Takes src, dst and vol from above.
#
# vol: without this check a copy to a volume that isn't mounted lands in the camera's RAM and looks fine.
# No mkdir -p for the same reason. A file only gets its real name once verified, so a bad copy can't pass
# for a good one. The part file is deleted first, not written through: on a stick it could be a symlink.
_COPY_SCRIPT = r"""
fail() { printf '%s\n' "$1" >&2; exit 1; }
[ -e "$src" ] || fail "$src does not exist"
[ -z "$vol" ] || awk -v mp="$vol" '$2==mp {found=1} END {exit !found}' /proc/mounts || fail "$vol is not mounted"
target=$dst
if [ -d "$dst" ]; then
  target=$dst/${src##*/}
  echo into=1
fi
if [ -e "$target" ] || [ -L "$target" ]; then fail "$target already exists"; fi
[ -d "${target%/*}" ] || fail "${target%/*} is not a folder"
if [ -d "$src" ]; then
  cp -R "$src" "$target" || fail "$target may be left with part of the copy"
  echo kind=dir
  exit 0
fi
[ -f "$src" ] || fail "$src is neither a file nor a folder"
part=$target.quak-part
rm -f "$part"
cp "$src" "$part" || { rm -f "$part"; exit 1; }
a=$(sha256sum < "$src")
b=$(sha256sum < "$part")
printf 'source=%s\ncopy=%s\n' "${a%% *}" "${b%% *}"
if [ -z "$a" ] || [ "$a" != "$b" ]; then
  rm -f "$part"
  fail "checksums differ (source ${a%% *}, copy ${b%% *}), so the copy was removed"
fi
mv "$part" "$target" && sync
"""


class DirEntry(NamedTuple):
    name: str
    type: str  # file, dir, link or other
    size: int  # in bytes, and 0 for anything but a file


@dataclass
class CopyResult:
    target: str  # where it ended up: inside dst if dst was a folder
    sha256: str | None  # what both ends hashed to. None for a folder, which isn't verified


def _parse_blkid(line: str) -> tuple[str, str]:
    """The first LABEL and the last TYPE. BusyBox prints a label raw, so it can add fields but not move the real ones."""
    fields = [(match[1], printable(re.sub(r"\\(.)", r"\1", match[2]))) for match in _BLKID_FIELD.finditer(line)]
    label = next((value for key, value in fields if key == "LABEL"), "")
    fs = next((value for key, value in reversed(fields) if key == "TYPE"), "")
    return label, fs


def _parse_volumes(text: str) -> list[Volume]:
    entries: dict[str, dict[str, str]] = {}
    mounts: dict[str, tuple[str, str, bool]] = {}
    current: dict[str, str] | None = None
    for line in text.split("\n"):  # \n alone: str.splitlines would also break on what a label can hold
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key == "entry":
            current = entries.setdefault(value, {}) if _BLOCK_NAME.fullmatch(value) else None
        elif key == "mount":
            fields = value.split()
            if len(fields) >= 4 and fields[0] not in mounts:
                mounts[fields[0]] = (fields[1], fields[2], "ro" in fields[3].split(","))
        elif current is not None:
            current[key] = value
    # In sysfs a partition's folder sits inside its disk's. A disk with partitions has no volume of its own.
    disks = {posixpath.basename(posixpath.dirname(fields.get("path", ""))) for fields in entries.values()}
    volumes = []
    for name, fields in entries.items():
        sectors = fields.get("size", "")
        size = int(sectors) * _SECTOR if sectors.isdecimal() else 0
        if name in disks or not size:  # no size: a card reader with nothing in it
            continue
        device = f"/dev/{name}"
        label, fs = _parse_blkid(fields.get("blkid", ""))
        mountpoint, mounted_fs, read_only = mounts.get(device, ("", "", True))
        volumes.append(
            Volume(
                device=device,
                label=label,
                fs=fs or mounted_fs,
                size=size,
                mountpoint=mountpoint,
                mounted=bool(mountpoint),
                read_only=read_only,
            )
        )
    return volumes


def list_volumes(remote: Remote) -> list[Volume]:
    """Every partition, and every disk without partitions. Changes nothing. ``fs`` is empty if nothing recognised it."""
    return _parse_volumes(run(remote, LIST_SCRIPT, "list the camera's volumes", _TIMEOUT).text)


def _device_name(device: str) -> str:
    name = device.removeprefix("/dev/")
    if not _BLOCK_NAME.fullmatch(name):
        raise LabError(f"{device!r} is not a block device")
    return name


def _safe_name(name: str) -> str:
    """One path component of plain characters. Host-off reads these back from /proc/mounts, split on whitespace."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".-")
    if not safe:
        raise LabError(f"{name!r} can't name a mountpoint")
    return safe


def _mountpoint(mountpoint: str) -> str:
    """A folder directly under ``/tmp/quak/mnt``, given by its name or its whole path. Nothing else is allowed."""
    path = posixpath.normpath(mountpoint)
    parent, name = posixpath.split(path)
    if parent and f"{parent}/" != LAB_MOUNTS:
        raise LabError(f"refusing {mountpoint!r}: Lab volumes only go directly under {LAB_MOUNTS}")
    return LAB_MOUNTS + _safe_name(name)


def mount(remote: Remote, device: str, mountpoint: str | None = None, read_only: bool = True) -> Volume:
    """Mount a volume, read-only unless ``read_only`` is exactly ``False``. Label and size are ``list_volumes``' job.

    Refuses a device that is already mounted, and a mountpoint in use. A failed mount leaves no folder behind.
    """
    name = _device_name(device)
    node = f"/dev/{name}"
    target = _mountpoint(name if mountpoint is None else mountpoint)
    mode = "rw" if read_only is False else "ro"
    script = f"""\
dev={shlex.quote(node)}
mp={shlex.quote(target)}
if awk -v dev="$dev" -v mp="$mp" '$1==dev || $2==mp {{found=1}} END {{exit !found}}' /proc/mounts; then
  echo "$dev or $mp is already mounted" >&2
  exit 1
fi
mkdir -p "$mp" || exit 1
if ! mount -o {mode},{_MOUNT_FLAGS} "$dev" "$mp"; then
  rmdir "$mp" 2>/dev/null
  exit 1
fi
awk -v mp="$mp" '$2==mp {{print "mounted=" $3, $4}}' /proc/mounts
"""
    result = run(remote, script, f"mount {node} on {target}", _MOUNT_TIMEOUT)
    mounted = re.search(r"^mounted=(\S+) (\S+)$", result.text, re.MULTILINE)
    # The kernel has the last word: it can answer a read-write request with a read-only mount.
    return Volume(
        device=node,
        fs=mounted[1] if mounted else "",
        mountpoint=target,
        mounted=True,
        read_only="ro" in mounted[2].split(",") if mounted else mode == "ro",
    )


def unmount(remote: Remote, mountpoint: str) -> None:
    """Unmount a Lab volume and remove its folder. One still in use stays mounted, and raises."""
    target = _mountpoint(mountpoint)
    quoted = shlex.quote(target)
    command = f"umount {quoted} && {{ rmdir {quoted} 2>/dev/null || true; }}\n"
    run(remote, command, f"unmount {target}", _MOUNT_TIMEOUT)


eject = unmount


def _absolute(path: str) -> str:
    """Normalised, so that what gets checked is what gets used. Absolute, so that it can't read as an option."""
    if not path.startswith("/") or "\0" in path:
        raise LabError(f"{path!r} is not an absolute path")
    return posixpath.normpath(path)


def list_dir(remote: Remote, path: str) -> list[DirEntry]:
    """What is in a folder, hidden files included. Changes nothing. A symlink is reported as such, not followed."""
    target = _absolute(path)
    command = f"cd {shlex.quote(target)} || exit 1\n{_LIST_DIR_SCRIPT}"
    fields = run(remote, command, f"list {target}", _LIST_DIR_TIMEOUT).stdout.split(b"\0")[:-1]
    return [
        DirEntry(printable(name.decode("utf-8", "replace")), kind.decode("ascii", "replace"), int(size or 0))
        for kind, size, name in zip(fields[0::3], fields[1::3], fields[2::3])
        if not size or size.isdigit()
    ]


def _storage_path(path: str) -> str:
    normal = _absolute(path)
    if normal == SD_CARD or normal.startswith((f"{SD_CARD}/", LAB_MOUNTS)):
        return normal
    raise LabError(f"refusing {path!r}: a copy only goes between the SD card ({SD_CARD}) and volumes ({LAB_MOUNTS})")


def copy(remote: Remote, src: str, dst: str, *, timeout: float = COPY_TIMEOUT) -> CopyResult:
    """Copy a file or folder between the SD card and mounted volumes. Never replaces anything.

    A file is copied under a temporary name, and takes its real name only if its sha256 matches the source's.
    A folder isn't verified, and a failure partway leaves what was copied so far. ``dst`` may be an existing
    folder to copy into. Its parent has to exist. A timeout raises ``NetworkError``, and may leave a part file
    that the same copy clears when run again.
    """
    source = _storage_path(src)
    destination = _storage_path(dst)
    if destination == source or destination.startswith(f"{source}/"):
        raise LabError(f"refusing to copy {source} onto or into itself")
    volume = ""
    if destination.startswith(LAB_MOUNTS):
        volume = LAB_MOUNTS + destination.removeprefix(LAB_MOUNTS).split("/")[0]
    variables = f"src={shlex.quote(source)}\ndst={shlex.quote(destination)}\nvol={shlex.quote(volume)}\n"
    doing = f"copy {source} to {destination}"
    result = run(remote, variables + _COPY_SCRIPT, doing, timeout)
    fields = dict(line.partition("=")[::2] for line in result.text.split("\n"))
    target = posixpath.join(destination, posixpath.basename(source)) if "into" in fields else destination
    if fields.get("kind") == "dir":
        return CopyResult(target, None)
    sha256 = fields.get("source", "")
    if not _SHA256.fullmatch(sha256) or sha256 != fields.get("copy"):
        raise LabError(f"could not {doing}: its checksum could not be verified. Don't rely on {target}")
    return CopyResult(target, sha256)

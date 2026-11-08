"""The small JSON store behind SSH profiles, key metadata and pinned host fingerprints.

It lives beside Quak's other state: ``~/.config/quak/ssh.json`` on Linux, the app's private
storage on Android. Private keys themselves are separate files under ``ssh/keys`` (0600).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quak import config

VERSION = 1


def ssh_file() -> Path:
    return config.config_dir() / "ssh.json"


def ssh_dir() -> Path:
    return config.config_dir() / "ssh"


def read() -> dict[str, Any]:
    """Read the store, tolerating a missing, truncated or corrupt file.

    A bad file (or one written by a newer/older Quak with the wrong shape) must never crash a
    read: anything that isn't the expected mapping is replaced with an empty one.
    """
    data = config._read(ssh_file())
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", VERSION)
    for section in ("profiles", "keys", "host_fingerprints"):
        if not isinstance(data.get(section), dict):
            data[section] = {}
    return data


def write(data: dict[str, Any]) -> None:
    data["version"] = VERSION
    config._write_private(ssh_file(), data)

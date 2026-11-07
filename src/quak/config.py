"""Where Quak keeps its small amount of state (paired cameras, lab hotspot, simulator state)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def config_dir() -> Path:
    if os.environ.get("QUAK_CONFIG_DIR"):
        return Path(os.environ["QUAK_CONFIG_DIR"])
    if os.environ.get("FLET_APP_STORAGE_DATA"):  # packaged app (Android, iOS): per-app storage
        return Path(os.environ["FLET_APP_STORAGE_DATA"]) / "config"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "quak"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "quak"


def state_dir() -> Path:
    if os.environ.get("QUAK_STATE_DIR"):
        return Path(os.environ["QUAK_STATE_DIR"])
    if os.environ.get("FLET_APP_STORAGE_DATA"):
        return Path(os.environ["FLET_APP_STORAGE_DATA"]) / "state"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "quak"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "quak"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def developer_mode() -> bool:
    """Shows tools that change a real camera a lot, such as the self-test. For people working on Quak."""
    return os.environ.get("QUAK_DEV") == "1"


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:  # private from the first byte
        json.dump(data, f, indent=1)
    tmp.replace(path)


def cameras_file() -> Path:
    return config_dir() / "cameras.json"


def saved_cameras() -> dict[str, dict]:
    return _read(cameras_file()).get("cameras", {})


def remember_camera(address: str, **info) -> None:
    data = _read(cameras_file())
    data.setdefault("cameras", {})[address.upper()] = info
    data["default"] = address.upper()
    _write_private(cameras_file(), data)


def forget_camera(address: str) -> None:
    data = _read(cameras_file())
    data.get("cameras", {}).pop(address.upper(), None)
    if data.get("default") == address.upper():
        data["default"] = next(iter(data.get("cameras", {})), None)
    _write_private(cameras_file(), data)


def default_address() -> str | None:
    return os.environ.get("QUAK_CAMERA_ADDRESS") or _read(cameras_file()).get("default")


def prefs_file() -> Path:
    return config_dir() / "prefs.json"


def get_pref(key: str, default=None):
    return _read(prefs_file()).get(key, default)


def set_pref(key: str, value) -> None:
    data = _read(prefs_file())
    data[key] = value
    _write_private(prefs_file(), data)


def sim_state_file() -> Path:
    return Path(os.environ.get("QUAK_SIM_STATE", state_dir() / "sim-camera.json"))


def hotspot_file() -> Path:
    return config_dir() / "hotspot.json"

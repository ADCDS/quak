"""Read and write one SSH profile per camera, keyed by the camera's BLE address."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, replace

from quak import config
from quak.ssh import store
from quak.ssh.models import SshProfile

TRANSPORTS = ("auto", "wifi", "usb", "host")
AUTHS = ("key", "password", "none")


def _port(value, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def from_dict(address: str, data: dict) -> SshProfile:
    transport = str(data.get("transport", "auto"))
    auth = str(data.get("auth", "key"))
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r}")
    if auth not in AUTHS:
        raise ValueError(f"unknown auth mode {auth!r}")
    host = data.get("host")
    host = str(host).strip() if host else None
    if transport == "host" and not host:
        raise ValueError("transport=host needs a host")
    return SshProfile(
        address=address.upper(),
        label=str(data.get("label", "")),
        transport=transport,  # type: ignore[arg-type]
        host=host,
        ssh_port=_port(data.get("ssh_port", 22), "ssh_port"),
        http_port=_port(data.get("http_port", 80), "http_port"),
        user=str(data.get("user", "root")),
        auth=auth,  # type: ignore[arg-type]
        key_id=data.get("key_id") or None,
        auto_rearm=bool(data.get("auto_rearm", False)),
        created=str(data.get("created", "")),
        last_arm=data.get("last_arm"),
    )


def to_dict(profile: SshProfile) -> dict:
    return asdict(profile)


def load(address: str) -> SshProfile | None:
    data = store.read()["profiles"].get(address.upper())
    return from_dict(address, data) if isinstance(data, dict) else None


def list_profiles() -> list[SshProfile]:
    profiles = []
    for address, data in store.read()["profiles"].items():
        if isinstance(data, dict):
            try:
                profiles.append(from_dict(address, data))
            except ValueError:
                continue
    return sorted(profiles, key=lambda p: p.address)


def save(profile: SshProfile) -> None:
    if profile.transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {profile.transport!r}")
    if profile.auth not in AUTHS:
        raise ValueError(f"unknown auth mode {profile.auth!r}")
    if profile.transport == "host" and not profile.host:
        raise ValueError("transport=host needs a host")
    _port(profile.ssh_port, "ssh_port")
    _port(profile.http_port, "http_port")
    data = store.read()
    data["profiles"][profile.address.upper()] = to_dict(profile)
    store.write(data)


def delete(address: str) -> bool:
    data = store.read()
    removed = data["profiles"].pop(address.upper(), None) is not None
    if removed:
        store.write(data)
    return removed


def for_camera(address: str) -> SshProfile:
    """The saved profile, or a sensible new one for this camera."""
    existing = load(address)
    if existing:
        return existing
    saved = config.saved_cameras().get(address.upper(), {})
    label = saved.get("model") or saved.get("name") or ""
    return SshProfile(
        address=address.upper(),
        label=label,
        created=dt.datetime.now().isoformat(timespec="seconds"),
    )


def default_address() -> str | None:
    return config.default_address()


def touch_arm(address: str, when: dt.datetime | None = None) -> None:
    profile = load(address)
    if not profile:
        return
    save(replace(profile, last_arm=(when or dt.datetime.now()).isoformat(timespec="seconds")))

"""Arm the gopro-root payload that is already on the camera's SD card.

This is the only camera action that is not over SSH: the unauthenticated SC request. It does
not install, stage or serve anything; if the payload is missing or its ``stage.sh`` is stale,
the camera simply never opens port 22 and Quak says so.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import time

from quak.ssh.models import ArmResult

# Exact request gopro-root documents. ``p`` is the raw byte 0x02, not the ASCII digit 2.
SC_PATH = "/gp/gpCamera?cmd=SC&p=%02"


def _sc_once(host: str, port: int, timeout: float) -> tuple[int | None, bytes, str]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", SC_PATH)
        response = conn.getresponse()
        body = response.read(64)
        return response.status, body, ""
    except (OSError, http.client.HTTPException) as exc:
        return None, b"", str(exc) or type(exc).__name__
    finally:
        conn.close()


def ssh_banner(host: str, port: int = 22, timeout: float = 3.0) -> tuple[bool, str]:
    """True and the banner when something at ``host:port`` speaks SSH."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = sock.recv(255)
    except OSError:
        return False, ""
    text = data.decode("utf-8", errors="replace").strip()
    return text.startswith("SSH-"), text


def _wait_for_ssh(host: str, port: int, wait: float) -> tuple[bool, str]:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        up, banner = ssh_banner(host, port, timeout=2.0)
        if up:
            return True, banner
        time.sleep(1.0)
    return False, ""


def _arm_blocking(
    host: str,
    http_port: int,
    ssh_port: int,
    timeout: float,
    retries: int,
    retry_delay: float,
    wait: float,
) -> ArmResult:
    started = time.monotonic()
    last_status: int | None = None
    last_error = ""
    for attempt in range(retries):
        status, body, error = _sc_once(host, http_port, timeout)
        last_status = status
        if status == 200:
            up, banner = _wait_for_ssh(host, ssh_port, wait)
            elapsed = time.monotonic() - started
            note = ""
            if body not in (b"\x00", b""):
                note = f"unexpected SC body {body!r}; "
            if up:
                message = f"{note}SC accepted and SSH is up after {elapsed:.1f}s"
            else:
                message = (
                    f"{note}SC accepted, but nothing answered on {host}:{ssh_port} after {wait:.0f}s. "
                    "The card's gopro-root payload (or its stage.sh) may need updating; Quak does not install it."
                )
            return ArmResult(
                http_status=status,
                accepted=True,
                ssh_up=up,
                banner=banner,
                elapsed=elapsed,
                message=message,
            )
        if status == 500 and attempt + 1 < retries:
            time.sleep(retry_delay)
            continue
        last_error = error or (f"HTTP {status}" if status is not None else "no response")
        break
    elapsed = time.monotonic() - started
    return ArmResult(
        http_status=last_status,
        accepted=False,
        ssh_up=ssh_banner(host, ssh_port)[0],
        elapsed=elapsed,
        message=f"SC was not accepted: {last_error}. Check that the camera is reachable and the gopro-root payload is installed.",
    )


async def arm(
    host: str,
    http_port: int = 80,
    ssh_port: int = 22,
    *,
    timeout: float = 12.0,
    retries: int = 3,
    retry_delay: float = 10.0,
    wait: float = 45.0,
) -> ArmResult:
    """Send SC, retry when gpNet is busy, then wait for Dropbear."""
    return await asyncio.to_thread(
        _arm_blocking,
        host,
        http_port,
        ssh_port,
        timeout,
        max(1, retries),
        retry_delay,
        wait,
    )

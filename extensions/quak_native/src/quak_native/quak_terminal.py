"""Python side of the QuakTerminal control (Dart side: src/flutter/quak_native/lib/src/quak_terminal_control.dart)."""

from dataclasses import dataclass, field
from typing import Optional

import flet as ft


@dataclass
class QuakTerminalErrorEvent(ft.Event["QuakTerminal"]):
    message: str


@ft.control("QuakTerminal")
class QuakTerminal(ft.LayoutControl):
    """An interactive SSH shell on the phone.

    `connect()` refuses to start without `expected_fingerprint`, and refuses any server whose
    host key doesn't match it.

    Every `connect()` ends in `on_connected` or `on_error`, unless a later `connect()` or
    `disconnect()` abandons it. Every `on_connected` is followed by one `on_disconnected`, except
    when the control leaves the page: that hangs up without an event.
    """

    host: str = ""
    port: int = 22
    username: str = ""
    private_key_pem: Optional[str] = field(default=None, repr=False)
    passphrase: Optional[str] = field(default=None, repr=False)
    password: Optional[str] = field(default=None, repr=False)
    expected_fingerprint: str = ""  # SHA256:<base64>, unpadded, as quak.ssh.hostkeys has it
    font_size: float = 14
    auto_connect: bool = False

    on_connected: Optional[ft.ControlEventHandler["QuakTerminal"]] = None
    on_disconnected: Optional[ft.ControlEventHandler["QuakTerminal"]] = None
    on_error: Optional[ft.EventHandler[QuakTerminalErrorEvent]] = None

    async def connect(self) -> None:
        """Starts connecting, hanging up any current connection, and returns: the events say how it went."""
        self.update()  # Dart reads the fields when this call arrives, so they have to get there first
        await self._invoke_method("connect", {}, timeout=15)

    async def disconnect(self) -> None:
        await self._invoke_method("disconnect", {}, timeout=15)

    async def send_text(self, text: str) -> None:
        """Sends text to the shell as if typed. Raises RuntimeError when not connected."""
        try:
            await self._invoke_method("send_text", {"text": text}, timeout=15)
        except TimeoutError:
            # from None: Flet's message quotes the arguments, and the text may be a password.
            raise TimeoutError("sending text to the terminal timed out") from None

    async def clear(self) -> None:
        """Clears the screen and scrollback on this side. The remote shell isn't told."""
        await self._invoke_method("clear", {}, timeout=15)

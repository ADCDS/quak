"""The phone build ships without bleak and dbus-fast: everything the app imports there must load without them."""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

EXT = str(Path(__file__).resolve().parents[1] / "extensions" / "quak_native" / "src")

SCRIPT = textwrap.dedent("""
    import sys, types
    sys.modules["bleak"] = None          # not in the APK
    sys.modules["dbus_fast"] = None
    try:
        import flet  # noqa: F401
    except ImportError:
        flet = None
    if flet is None:  # the extension needs flet; check the rest with a stand-in
        fake = types.ModuleType("quak_native")
        fake.QuakBle = object
        sys.modules["quak_native"] = fake
    else:  # the real extension package, as the APK bundles it
        sys.path.insert(0, EXT)
    import quak.transport.android, quak.service, quak.flows, quak.net.wifi, quak.net.http, quak.net.android_wifi
    if flet is None:
        print("ok (no flet)")
        raise SystemExit
    import quak.gui.app
    assert quak.gui.app._mobile_ble_available()
    # Flet builds events from the Dart payload's keys (flet/controls/base_control.py _trigger_event)
    from flet.utils.from_dict import from_dict
    from quak_native import QuakBleNotifyEvent, QuakBleDisconnectEvent, QuakWifi  # noqa: F401
    e = from_dict(QuakBleNotifyEvent, {"control": None, "name": "notify", "uuid": "b5f90073", "value": "0102"})
    assert (e.uuid, e.value) == ("b5f90073", "0102"), e
    d = from_dict(QuakBleDisconnectEvent, {"control": None, "name": "disconnected", "address": "AA:BB:CC:DD:EE:FF"})
    assert d.address == "AA:BB:CC:DD:EE:FF"
    print("ok")
""")


class PhoneImportTest(unittest.TestCase):
    def test_phone_modules_import_without_bleak(self):
        p = subprocess.run(
            [sys.executable, "-c", f"EXT = {EXT!r}\n" + SCRIPT], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertTrue(p.stdout.strip().startswith("ok"), p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()

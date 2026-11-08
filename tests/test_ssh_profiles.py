"""SSH profile store: save/load/list/delete, validation, and private (0600) storage.

No hardware and no real ssh: everything runs against an isolated ``QUAK_CONFIG_DIR``.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quak.ssh import profiles, store
from quak.ssh.models import SshProfile

ADDR = "AA:BB:CC:DD:EE:FF"


class SshProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.config_dir = Path(self.tmp.name)

    # save / load / list / delete ------------------------------------------------

    def test_save_then_load_round_trips(self):
        profile = SshProfile(
            address=ADDR,
            label="Hero",
            transport="wifi",
            auth="key",
            key_id="k1",
            ssh_port=8022,
            http_port=8080,
        )
        profiles.save(profile)
        loaded = profiles.load(ADDR)
        self.assertEqual(loaded, profile)

    def test_address_is_normalised_to_upper_case(self):
        profiles.save(SshProfile(address=ADDR.lower()))
        self.assertIsNotNone(profiles.load(ADDR.lower()))
        loaded = profiles.load(ADDR)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.address, ADDR)

    def test_save_overwrites_existing_profile_for_the_same_camera(self):
        profiles.save(SshProfile(address=ADDR, label="first"))
        profiles.save(SshProfile(address=ADDR, label="second"))
        self.assertEqual(profiles.load(ADDR).label, "second")
        self.assertEqual(len(profiles.list_profiles()), 1)

    def test_load_missing_profile_returns_none(self):
        self.assertIsNone(profiles.load("11:22:33:44:55:66"))

    def test_list_profiles_is_sorted_by_address(self):
        profiles.save(SshProfile(address="11:22:33:44:55:66"))
        profiles.save(SshProfile(address="00:00:00:00:00:00"))
        profiles.save(SshProfile(address=ADDR))
        listed = [p.address for p in profiles.list_profiles()]
        self.assertEqual(listed, sorted(listed))
        self.assertEqual(len(listed), 3)

    def test_list_profiles_skips_invalid_entries(self):
        profiles.save(SshProfile(address=ADDR))
        data = store.read()
        data["profiles"]["BAD"] = {"transport": "nonsense"}
        store.write(data)
        listed = [p.address for p in profiles.list_profiles()]
        self.assertEqual(listed, [ADDR])

    def test_delete_removes_profile_and_reports_whether_it_existed(self):
        profiles.save(SshProfile(address=ADDR))
        self.assertTrue(profiles.delete(ADDR.lower()))
        self.assertIsNone(profiles.load(ADDR))
        self.assertFalse(profiles.delete(ADDR))

    # validation -----------------------------------------------------------------

    def test_from_dict_rejects_unknown_transport(self):
        with self.assertRaises(ValueError):
            profiles.from_dict(ADDR, {"transport": "carrier-pigeon"})

    def test_from_dict_rejects_unknown_auth(self):
        with self.assertRaises(ValueError):
            profiles.from_dict(ADDR, {"auth": "fingerprint"})

    def test_save_rejects_unknown_transport(self):
        bad = SshProfile(address=ADDR)
        object.__setattr__(bad, "transport", "carrier-pigeon")
        with self.assertRaises(ValueError):
            profiles.save(bad)

    def test_save_rejects_unknown_auth(self):
        bad = SshProfile(address=ADDR)
        object.__setattr__(bad, "auth", "fingerprint")
        with self.assertRaises(ValueError):
            profiles.save(bad)

    def test_every_declared_transport_and_auth_is_accepted(self):
        for transport in profiles.TRANSPORTS:
            data = {"transport": transport}
            if transport == "host":
                data["host"] = "10.0.0.5"
            self.assertEqual(profiles.from_dict(ADDR, data).transport, transport)
        for auth in profiles.AUTHS:
            self.assertEqual(profiles.from_dict(ADDR, {"auth": auth}).auth, auth)

    def test_ports_must_be_in_range(self):
        for field in ("ssh_port", "http_port"):
            for value in (0, 70000, "nope"):
                with self.assertRaises(ValueError):
                    profiles.from_dict(ADDR, {field: value})

    # host required for transport=host -------------------------------------------

    def test_from_dict_requires_host_for_transport_host(self):
        with self.assertRaises(ValueError):
            profiles.from_dict(ADDR, {"transport": "host"})
        with self.assertRaises(ValueError):
            profiles.from_dict(ADDR, {"transport": "host", "host": "   "})

    def test_from_dict_accepts_transport_host_with_a_host(self):
        profile = profiles.from_dict(ADDR, {"transport": "host", "host": " 10.0.0.5 "})
        self.assertEqual(profile.transport, "host")
        self.assertEqual(profile.host, "10.0.0.5")

    def test_save_requires_host_for_transport_host(self):
        with self.assertRaises(ValueError):
            profiles.save(SshProfile(address=ADDR, transport="host"))
        profiles.save(SshProfile(address=ADDR, transport="host", host="10.0.0.5"))
        self.assertEqual(profiles.load(ADDR).host, "10.0.0.5")

    def test_other_transports_do_not_require_a_host(self):
        for transport in ("auto", "wifi", "usb"):
            profiles.save(SshProfile(address=ADDR, transport=transport))
            self.assertEqual(profiles.load(ADDR).transport, transport)

    # for_camera -----------------------------------------------------------------

    def test_for_camera_returns_saved_profile_when_present(self):
        saved = SshProfile(address=ADDR, label="mine", transport="usb")
        profiles.save(saved)
        self.assertEqual(profiles.for_camera(ADDR.lower()), saved)

    def test_for_camera_builds_a_default_labelled_from_saved_cameras(self):
        from quak import config

        config.remember_camera(ADDR, model="HERO11 Black")
        fresh = profiles.for_camera(ADDR)
        self.assertEqual(fresh.address, ADDR)
        self.assertEqual(fresh.label, "HERO11 Black")
        self.assertEqual(fresh.transport, "auto")
        self.assertTrue(fresh.created)  # stamped, so a later save records it

    # private storage ------------------------------------------------------------

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_store_file_is_private_0600(self):
        profiles.save(SshProfile(address=ADDR))
        mode = stat.S_IMODE(os.stat(store.ssh_file()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_store_is_versioned(self):
        profiles.save(SshProfile(address=ADDR))
        on_disk = json.loads(store.ssh_file().read_text())
        self.assertEqual(on_disk["version"], store.VERSION)

    # tolerant reads -------------------------------------------------------------

    def test_corrupt_store_does_not_crash_load_or_list(self):
        store.ssh_file().write_text("}{ not json")
        self.assertIsNone(profiles.load(ADDR))
        self.assertEqual(profiles.list_profiles(), [])

    def test_wrongly_typed_sections_are_treated_as_empty(self):
        store.ssh_file().parent.mkdir(parents=True, exist_ok=True)
        store.ssh_file().write_text(json.dumps({"version": 1, "profiles": ["x"], "keys": None}))
        self.assertIsNone(profiles.load(ADDR))
        self.assertEqual(profiles.list_profiles(), [])
        # and a subsequent save heals the store
        profiles.save(SshProfile(address=ADDR))
        self.assertIsNotNone(profiles.load(ADDR))


if __name__ == "__main__":
    unittest.main()

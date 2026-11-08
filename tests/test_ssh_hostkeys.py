"""Host-key handling: SHA256 fingerprints, the OpenSSH ``known_hosts`` file, the pinned
fingerprint store, and the unknown/trusted/changed state machine.

No hardware and no real ``ssh-keyscan``: the scan subprocess is mocked, and everything that
touches disk runs against an isolated ``QUAK_CONFIG_DIR``.
"""

import base64
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from quak.ssh import hostkeys, store
from quak.ssh.models import HostKey

# Independently computed: "SHA256:" + base64(sha256(BLOB)) with padding stripped.
BLOB = b"quak-test-hostkey-blob"
BLOB_FP = "SHA256:UIYvT5iMuy+uioM9ftOrKn+dUhg1f5wTd1mwDvPHt4o"


def b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def keyscan_line(host: str, key_type: str, blob: bytes) -> str:
    return f"{host} {key_type} {b64(blob)}"


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_matches_known_vector(self):
        self.assertEqual(hostkeys.fingerprint(BLOB), BLOB_FP)

    def test_fingerprint_has_no_base64_padding(self):
        self.assertFalse(hostkeys.fingerprint(BLOB).endswith("="))

    def test_fingerprint_from_public_line(self):
        line = f"ssh-ed25519 {b64(BLOB)} someone@host"
        self.assertEqual(hostkeys.fingerprint_from_public_line(line), BLOB_FP)

    def test_fingerprint_from_public_line_rejects_junk(self):
        with self.assertRaises(ValueError):
            hostkeys.fingerprint_from_public_line("not-a-key")

    def test_parse_public_line_splits_type_blob_comment(self):
        key_type, blob, comment = hostkeys.parse_public_line(
            f"ssh-ed25519 {b64(BLOB)} my comment here"
        )
        self.assertEqual(key_type, "ssh-ed25519")
        self.assertEqual(blob, BLOB)
        self.assertEqual(comment, "my comment here")


class _IsolatedStore(unittest.TestCase):
    """Base class giving each test a private config/ssh directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.config_dir = Path(self.tmp.name)


class KnownHostsTests(_IsolatedStore):
    def test_round_trip_default_port_uses_bare_host(self):
        added = hostkeys.add_known_host("10.5.5.9", 22, "ssh-ed25519", BLOB)
        self.assertEqual(added.fingerprint, BLOB_FP)

        text = hostkeys.known_hosts_path().read_text()
        self.assertIn(f"10.5.5.9 ssh-ed25519 {b64(BLOB)}", text)
        self.assertNotIn("[10.5.5.9]", text)

        found = hostkeys.lookup_known_hosts("10.5.5.9", 22)
        self.assertIsNotNone(found)
        self.assertEqual(found.key_type, "ssh-ed25519")
        self.assertEqual(found.fingerprint, BLOB_FP)
        self.assertEqual(found.line.split()[0], "10.5.5.9")

    def test_round_trip_non_default_port_uses_bracket_form(self):
        hostkeys.add_known_host("10.5.5.9", 8022, "ssh-ed25519", BLOB)

        text = hostkeys.known_hosts_path().read_text()
        self.assertIn(f"[10.5.5.9]:8022 ssh-ed25519 {b64(BLOB)}", text)

        self.assertIsNone(hostkeys.lookup_known_hosts("10.5.5.9", 22))
        found = hostkeys.lookup_known_hosts("10.5.5.9", 8022)
        self.assertIsNotNone(found)
        self.assertEqual(found.port, 8022)
        self.assertEqual(found.fingerprint, BLOB_FP)

    def test_lookup_missing_host_returns_none(self):
        self.assertIsNone(hostkeys.lookup_known_hosts("127.0.0.1", 22))

    def test_add_replaces_existing_entry_without_duplicating(self):
        hostkeys.add_known_host("10.5.5.9", 22, "ssh-ed25519", b"first-key-blob")
        hostkeys.add_known_host("10.5.5.9", 22, "ssh-ed25519", BLOB)

        lines = [
            ln
            for ln in hostkeys.known_hosts_path().read_text().splitlines()
            if ln.strip() and "10.5.5.9" in ln
        ]
        self.assertEqual(len(lines), 1)
        self.assertEqual(hostkeys.lookup_known_hosts("10.5.5.9", 22).fingerprint, BLOB_FP)

    def test_add_keeps_unrelated_hosts(self):
        hostkeys.add_known_host("10.5.5.9", 22, "ssh-ed25519", BLOB)
        hostkeys.add_known_host("192.168.9.51", 22, "ssh-ed25519", b"other-blob")
        self.assertIsNotNone(hostkeys.lookup_known_hosts("10.5.5.9", 22))
        self.assertIsNotNone(hostkeys.lookup_known_hosts("192.168.9.51", 22))

    def test_remove_known_host(self):
        hostkeys.add_known_host("10.5.5.9", 22, "ssh-ed25519", BLOB)
        self.assertTrue(hostkeys.remove_known_host("10.5.5.9", 22))
        self.assertIsNone(hostkeys.lookup_known_hosts("10.5.5.9", 22))
        self.assertFalse(hostkeys.remove_known_host("10.5.5.9", 22))


class FingerprintStoreTests(_IsolatedStore):
    def test_remember_then_read_back(self):
        hostkeys.remember_fingerprint("10.5.5.9", 22, "ssh-ed25519", BLOB_FP)
        got = hostkeys.stored_fingerprint("10.5.5.9", 22)
        self.assertIsNotNone(got)
        self.assertEqual(got.fingerprint, BLOB_FP)
        self.assertEqual(got.key_type, "ssh-ed25519")

        raw = store.read()["host_fingerprints"]
        self.assertIn("10.5.5.9:22", raw)

    def test_stored_fingerprint_missing_returns_none(self):
        self.assertIsNone(hostkeys.stored_fingerprint("10.5.5.9", 22))

    def test_forget_removes_pin_and_known_hosts_line(self):
        hostkeys.remember_fingerprint("10.5.5.9", 22, "ssh-ed25519", BLOB_FP)
        hostkeys.add_known_host("10.5.5.9", 22, "ssh-ed25519", BLOB)

        self.assertTrue(hostkeys.forget_fingerprint("10.5.5.9", 22))
        self.assertIsNone(hostkeys.stored_fingerprint("10.5.5.9", 22))
        self.assertIsNone(hostkeys.lookup_known_hosts("10.5.5.9", 22))
        self.assertFalse(hostkeys.forget_fingerprint("10.5.5.9", 22))

    def test_known_prefers_pin_then_known_hosts(self):
        self.assertIsNone(hostkeys.known("10.5.5.9", 22))
        hostkeys.add_known_host("10.5.5.9", 22, "ssh-ed25519", BLOB)
        self.assertEqual(hostkeys.known("10.5.5.9", 22).fingerprint, BLOB_FP)
        hostkeys.remember_fingerprint("10.5.5.9", 22, "ssh-ed25519", "SHA256:pinned")
        self.assertEqual(hostkeys.known("10.5.5.9", 22).fingerprint, "SHA256:pinned")


class StateTests(_IsolatedStore):
    def _seen(self, fp: str) -> HostKey:
        return HostKey(host="10.5.5.9", port=22, key_type="ssh-ed25519", fingerprint=fp)

    def test_unknown_when_nothing_pinned(self):
        self.assertEqual(hostkeys.state("10.5.5.9", 22, self._seen(BLOB_FP)), "unknown")

    def test_trusted_when_seen_matches_pin(self):
        hostkeys.remember_fingerprint("10.5.5.9", 22, "ssh-ed25519", BLOB_FP)
        self.assertEqual(hostkeys.state("10.5.5.9", 22, self._seen(BLOB_FP)), "trusted")

    def test_trusted_when_pinned_and_not_rescanned(self):
        hostkeys.remember_fingerprint("10.5.5.9", 22, "ssh-ed25519", BLOB_FP)
        self.assertEqual(hostkeys.state("10.5.5.9", 22, None), "trusted")

    def test_changed_when_seen_differs_from_pin(self):
        hostkeys.remember_fingerprint("10.5.5.9", 22, "ssh-ed25519", BLOB_FP)
        self.assertEqual(
            hostkeys.state("10.5.5.9", 22, self._seen("SHA256:somethingelse")),
            "changed",
        )

    def test_trusted_via_known_hosts_without_pin(self):
        hostkeys.add_known_host("10.5.5.9", 22, "ssh-ed25519", BLOB)
        self.assertEqual(hostkeys.state("10.5.5.9", 22, self._seen(BLOB_FP)), "trusted")
        self.assertEqual(
            hostkeys.state("10.5.5.9", 22, self._seen("SHA256:somethingelse")),
            "changed",
        )


class ScanTests(_IsolatedStore):
    def _run_scan(self, stdout: str):
        completed = types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        with mock.patch.object(hostkeys.shutil, "which", return_value="/usr/bin/ssh-keyscan"), \
                mock.patch.object(hostkeys.subprocess, "run", return_value=completed) as run:
            result = hostkeys.scan("10.5.5.9", 22, timeout=2.0)
        return result, run

    def test_scan_parses_a_single_key(self):
        stdout = "# comment from keyscan\n" + keyscan_line("10.5.5.9", "ssh-ed25519", BLOB) + "\n"
        result, run = self._run_scan(stdout)
        self.assertEqual(result.key_type, "ssh-ed25519")
        self.assertEqual(result.fingerprint, BLOB_FP)
        self.assertEqual(result.host, "10.5.5.9")
        self.assertEqual(result.port, 22)
        # ssh-keyscan was invoked, not the real network scan.
        self.assertTrue(run.called)

    def test_scan_prefers_ed25519_over_rsa(self):
        stdout = (
            keyscan_line("10.5.5.9", "ssh-rsa", b"rsa-blob") + "\n"
            + keyscan_line("10.5.5.9", "ssh-ed25519", BLOB) + "\n"
        )
        result, _ = self._run_scan(stdout)
        self.assertEqual(result.key_type, "ssh-ed25519")
        self.assertEqual(result.fingerprint, BLOB_FP)

    def test_scanned_line_can_be_added_to_known_hosts(self):
        stdout = keyscan_line("10.5.5.9", "ssh-ed25519", BLOB) + "\n"
        result, _ = self._run_scan(stdout)

        # Nothing pinned yet: the scanned key is unknown.
        self.assertEqual(hostkeys.state("10.5.5.9", 22, result), "unknown")

        added = hostkeys.add_host_key(result)
        self.assertEqual(added.fingerprint, BLOB_FP)
        self.assertEqual(hostkeys.lookup_known_hosts("10.5.5.9", 22).fingerprint, BLOB_FP)

        # Now in known_hosts, the same scan is trusted; a different key would be changed.
        self.assertEqual(hostkeys.state("10.5.5.9", 22, result), "trusted")
        other = HostKey(host="10.5.5.9", port=22, key_type="ssh-ed25519", fingerprint="SHA256:other")
        self.assertEqual(hostkeys.state("10.5.5.9", 22, other), "changed")

    def test_scan_raises_when_keyscan_missing(self):
        with mock.patch.object(hostkeys.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                hostkeys.scan("10.5.5.9", 22)

    def test_scan_raises_when_no_key_returned(self):
        completed = types.SimpleNamespace(stdout="# nothing here\n", stderr="", returncode=1)
        with mock.patch.object(hostkeys.shutil, "which", return_value="/usr/bin/ssh-keyscan"), \
                mock.patch.object(hostkeys.subprocess, "run", return_value=completed):
            with self.assertRaises(ConnectionError):
                hostkeys.scan("10.5.5.9", 22)


if __name__ == "__main__":
    unittest.main()

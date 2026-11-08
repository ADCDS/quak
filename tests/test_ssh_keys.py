"""The SSH key store and the authorized_keys text helpers.

No hardware and no camera. Everything on disk runs against an isolated ``QUAK_CONFIG_DIR``. Tests
that need real keys use the system ``ssh-keygen`` and skip when it isn't installed; the rest fake
the private key file, so the store is still covered on a machine without OpenSSH.
"""

import base64
import dataclasses
import json
import os
import shutil
import stat
import struct
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from quak.core.errors import SshKeyError
from quak.ssh import hostkeys, keys, store

# Independently computed: `ssh-keygen -lf` on the public key made from blob(0).
VECTOR_FINGERPRINT = "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA"

PASSPHRASE = "correct horse battery staple"


def _field(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def blob(seed: int) -> bytes:
    """A well-formed ssh-ed25519 public key blob; distinct seeds give distinct keys."""
    return _field(b"ssh-ed25519") + _field(bytes((seed + i) % 256 for i in range(32)))


def encoded(seed: int) -> str:
    return base64.b64encode(blob(seed)).decode("ascii")


def pub(seed: int, comment: str = "") -> str:
    return f"ssh-ed25519 {encoded(seed)} {comment}".rstrip()


def fake_private_pem(public_blob: bytes, cipher: bytes = b"none") -> str:
    """An OpenSSH private key container whose private section is filler, not a usable key."""
    raw = (
        b"openssh-key-v1\0"
        + _field(cipher)
        + _field(b"none" if cipher == b"none" else b"bcrypt")
        + _field(b"")
        + struct.pack(">I", 1)
        + _field(public_blob)
        + _field(b"filler-private-section-" * 8)
    )
    body = base64.b64encode(raw).decode("ascii")
    wrapped = "\n".join(body[i : i + 70] for i in range(0, len(body), 70))
    return f"-----BEGIN OPENSSH PRIVATE KEY-----\n{wrapped}\n-----END OPENSSH PRIVATE KEY-----\n"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def run_keygen(*args: str) -> subprocess.CompletedProcess:
    """Call the real ssh-keygen with no terminal, so a locked key fails instead of prompting."""
    return subprocess.run(
        ["ssh-keygen", *args],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "SSH_ASKPASS_REQUIRE": "never"},
    )


# A file with everything worth preserving: comments, a blank line, options with quoted spaces,
# odd indentation and spacing, a commented-out key, and a second key type.
SAMPLE = (
    "# managed by hand\n"
    "\n"
    f'command="/bin/echo hello world",no-pty ssh-ed25519 {encoded(1)} restricted key\n'
    f"  ssh-ed25519 {encoded(2)} laptop  with  spaces\n"
    f"# ssh-ed25519 {encoded(3)} retired key\n"
    f"ecdsa-sha2-nistp256 {encoded(4)}\n"
)

# The quoted option holds a key type and a blob, and the real key comes after it.
DECOY = f'command="logger ssh-rsa {encoded(8)} seen",no-pty ssh-ed25519 {encoded(9)} real\n'


class ParseAuthorizedKeysTests(unittest.TestCase):
    def test_keeps_options_and_comments_verbatim(self):
        lines = keys.parse_authorized_keys(SAMPLE)
        self.assertEqual(
            lines,
            [
                "# managed by hand",
                f'command="/bin/echo hello world",no-pty ssh-ed25519 {encoded(1)} restricted key',
                f"ssh-ed25519 {encoded(2)} laptop  with  spaces",
                f"# ssh-ed25519 {encoded(3)} retired key",
                f"ecdsa-sha2-nistp256 {encoded(4)}",
            ],
        )

    def test_empty_and_blank_text_have_no_lines(self):
        self.assertEqual(keys.parse_authorized_keys(""), [])
        self.assertEqual(keys.parse_authorized_keys("\n  \n\t\n"), [])

    def test_handles_crlf(self):
        self.assertEqual(keys.parse_authorized_keys(f"{pub(1)}\r\n{pub(2)}\r\n"), [pub(1), pub(2)])

    def test_only_a_newline_ends_a_line(self):
        # sshd reads this as one key whose comment has a separator in it, so Quak must as well.
        text = f"{pub(1)} note ssh-ed25519 {encoded(2)}\n"
        self.assertEqual(len(keys.parse_authorized_keys(text)), 1)
        self.assertEqual(keys.authorized_blobs(text), {encoded(1)})


class AuthorizedBlobsTests(unittest.TestCase):
    def test_finds_active_keys_and_skips_comments(self):
        self.assertEqual(keys.authorized_blobs(SAMPLE), {encoded(1), encoded(2), encoded(4)})

    def test_finds_every_supported_key_type_family(self):
        for key_type in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "sk-ssh-ed25519@openssh.com"):
            with self.subTest(key_type=key_type):
                self.assertEqual(keys.authorized_blobs(f"{key_type} {encoded(1)}\n"), {encoded(1)})

    def test_key_type_inside_a_quoted_option_is_not_the_key(self):
        self.assertEqual(keys.authorized_blobs(DECOY), {encoded(9)})

    def test_escaped_quote_does_not_end_the_option(self):
        text = f'command="echo \\"ssh-rsa {encoded(8)}\\" done" ssh-ed25519 {encoded(9)}\n'
        self.assertEqual(keys.authorized_blobs(text), {encoded(9)})

    def test_key_type_in_the_comment_is_not_another_key(self):
        text = f"ssh-ed25519 {encoded(1)} was ssh-rsa {encoded(2)}\n"
        self.assertEqual(keys.authorized_blobs(text), {encoded(1)})

    def test_quote_in_the_comment_is_harmless(self):
        self.assertEqual(keys.authorized_blobs(f"""ssh-ed25519 {encoded(1)} bob's "laptop\n"""), {encoded(1)})

    def test_malformed_lines_are_not_keys(self):
        for line in ("ssh-ed25519", "ssh-ed25519 not*base64 comment", "garbage on this line", 'no-pty,command="open'):
            with self.subTest(line=line):
                self.assertEqual(keys.authorized_blobs(line + "\n"), set())


class KeyCountTests(unittest.TestCase):
    def test_nothing_to_count(self):
        self.assertEqual(keys.key_count(""), 0)
        self.assertEqual(keys.key_count("# only a comment\n\n"), 0)
        self.assertEqual(keys.key_count(f"# {pub(1)}\n"), 0)

    def test_counts_active_keys(self):
        self.assertEqual(keys.key_count(SAMPLE), 3)

    def test_the_same_key_twice_counts_once(self):
        self.assertEqual(keys.key_count(f"{pub(1, 'a')}\nno-pty {pub(1, 'b')}\n"), 1)

    def test_malformed_line_does_not_count(self):
        # The last-key guard relies on this: a broken line can't log anyone in.
        self.assertEqual(keys.key_count(f"{pub(1)}\nssh-ed25519 not*base64\n"), 1)


class HasPublicKeyTests(unittest.TestCase):
    def test_matches_by_blob_whatever_the_comment_or_options(self):
        self.assertTrue(keys.has_public_key(SAMPLE, pub(1, "a different comment")))
        self.assertTrue(keys.has_public_key(SAMPLE, pub(2)))

    def test_absent_and_commented_out_keys_do_not_match(self):
        self.assertFalse(keys.has_public_key(SAMPLE, pub(3)))
        self.assertFalse(keys.has_public_key(SAMPLE, pub(5)))

    def test_decoy_in_an_option_does_not_match(self):
        self.assertFalse(keys.has_public_key(DECOY, pub(8)))
        self.assertTrue(keys.has_public_key(DECOY, pub(9)))

    def test_rejects_what_is_not_a_public_key(self):
        for bad in ("", "not-a-key", "ssh-ed25519 not*base64", f"unknown-type {encoded(1)}"):
            with self.subTest(bad=bad), self.assertRaises(SshKeyError):
                keys.has_public_key(SAMPLE, bad)


class AddPublicKeyTests(unittest.TestCase):
    def test_add_to_empty_text(self):
        self.assertEqual(keys.add_public_key("", pub(5, "new")), pub(5, "new") + "\n")

    def test_preserves_every_existing_line_exactly(self):
        self.assertEqual(keys.add_public_key(SAMPLE, pub(5, "new")), SAMPLE + pub(5, "new") + "\n")

    def test_missing_final_newline_does_not_join_lines(self):
        self.assertEqual(keys.add_public_key(pub(1), pub(5)), f"{pub(1)}\n{pub(5)}\n")

    def test_adding_a_present_key_changes_nothing(self):
        self.assertEqual(keys.add_public_key(SAMPLE, pub(2, "other comment")), SAMPLE)

    def test_does_not_add_an_unrestricted_copy_of_a_restricted_key(self):
        self.assertEqual(keys.add_public_key(SAMPLE, pub(1)), SAMPLE)

    def test_commented_out_key_is_added_as_a_new_active_line(self):
        added = keys.add_public_key(SAMPLE, pub(3, "back again"))
        self.assertEqual(added, SAMPLE + pub(3, "back again") + "\n")
        self.assertEqual(keys.key_count(added), 4)

    def test_newline_in_the_key_cannot_smuggle_in_another_line(self):
        added = keys.add_public_key("", f"{pub(1, 'note')}\nssh-ed25519 {encoded(2)} sneaky")
        self.assertEqual(len(added.splitlines()), 1)
        self.assertEqual(keys.authorized_blobs(added), {encoded(1)})

    def test_rejects_junk_key(self):
        with self.assertRaises(SshKeyError):
            keys.add_public_key(SAMPLE, "not-a-key")


class RemovePublicKeyTests(unittest.TestCase):
    def test_removes_by_blob_and_preserves_the_rest_exactly(self):
        removed = keys.remove_public_key(SAMPLE, pub(2, "comment does not matter"))
        self.assertEqual(removed, SAMPLE.replace(f"  ssh-ed25519 {encoded(2)} laptop  with  spaces\n", ""))

    def test_removes_a_line_that_has_options(self):
        removed = keys.remove_public_key(SAMPLE, pub(1))
        self.assertFalse(keys.has_public_key(removed, pub(1)))
        self.assertNotIn("/bin/echo", removed)
        self.assertEqual(keys.key_count(removed), 2)

    def test_removes_the_real_key_behind_a_decoy_option(self):
        self.assertEqual(keys.remove_public_key(DECOY, pub(9)), "")

    def test_decoy_inside_an_option_does_not_remove_the_line(self):
        self.assertEqual(keys.remove_public_key(DECOY, pub(8)), DECOY)

    def test_removes_every_copy_of_the_key(self):
        text = f"{pub(1, 'a')}\n{pub(2)}\nno-pty {pub(1, 'b')}\n"
        self.assertEqual(keys.remove_public_key(text, pub(1)), f"{pub(2)}\n")

    def test_leaves_a_commented_out_copy_alone(self):
        self.assertEqual(keys.remove_public_key(SAMPLE, pub(3)), SAMPLE)

    def test_removing_an_absent_key_changes_nothing(self):
        self.assertEqual(keys.remove_public_key(SAMPLE, pub(5)), SAMPLE)

    def test_removing_the_only_key_leaves_empty_text(self):
        self.assertEqual(keys.remove_public_key(pub(1) + "\n", pub(1)), "")

    def test_removing_the_last_key_keeps_comments(self):
        removed = keys.remove_public_key(f"# keep me\n{pub(1)}\n", pub(1))
        self.assertEqual(removed, "# keep me\n")
        self.assertEqual(keys.key_count(removed), 0)

    def test_result_ends_with_a_newline(self):
        self.assertEqual(keys.remove_public_key(f"{pub(1)}\n{pub(2)}", pub(2)), f"{pub(1)}\n")

    def test_crlf_on_other_lines_is_kept(self):
        self.assertEqual(keys.remove_public_key(f"{pub(1)}\r\n{pub(2)}\r\n", pub(1)), f"{pub(2)}\r\n")

    def test_add_then_remove_restores_the_original(self):
        self.assertEqual(keys.remove_public_key(keys.add_public_key(SAMPLE, pub(5)), pub(5)), SAMPLE)

    def test_rejects_junk_key(self):
        with self.assertRaises(SshKeyError):
            keys.remove_public_key(SAMPLE, "not-a-key")


class PublicLineTests(unittest.TestCase):
    def test_round_trips_through_the_parser(self):
        line = keys.public_line("ssh-ed25519", blob(1), "me@laptop")
        self.assertEqual(line, pub(1, "me@laptop"))
        self.assertEqual(hostkeys.parse_public_line(line), ("ssh-ed25519", blob(1), "me@laptop"))

    def test_no_comment_leaves_no_trailing_space(self):
        self.assertEqual(keys.public_line("ssh-ed25519", blob(1)), pub(1))
        self.assertEqual(keys.public_line("ssh-ed25519", blob(1), " \r\n "), pub(1))

    def test_comment_cannot_break_the_line(self):
        line = keys.public_line("ssh-ed25519", blob(1), "one\ntwo\r\n  three four")
        self.assertEqual(line, pub(1, "one two three four"))

    def test_fingerprint_matches_the_ssh_keygen_vector(self):
        self.assertEqual(hostkeys.fingerprint(blob(0)), VECTOR_FINGERPRINT)

    def test_fingerprint_is_the_same_from_blob_and_line(self):
        line = keys.public_line("ssh-ed25519", blob(0), "any comment")
        self.assertEqual(hostkeys.fingerprint_from_public_line(line), VECTOR_FINGERPRINT)

    def test_comment_does_not_change_the_fingerprint(self):
        self.assertEqual(
            hostkeys.fingerprint_from_public_line(pub(1, "one")),
            hostkeys.fingerprint_from_public_line(pub(1, "two")),
        )


class _IsolatedStore(unittest.TestCase):
    """A private config directory, plus a separate one for key files to import from."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_dir = Path(tmp.name) / "config"
        self.source_dir = Path(tmp.name) / "source"
        self.source_dir.mkdir()
        patch = mock.patch.dict(os.environ, {"QUAK_CONFIG_DIR": str(self.config_dir)})
        patch.start()
        self.addCleanup(patch.stop)

    def permissive_umask(self):
        self.addCleanup(os.umask, os.umask(0))

    def assert_nothing_stored(self):
        self.assertEqual(keys.list_keys(), [])
        self.assertEqual(list(keys.key_dir().glob("*")), [])

    def assert_no_private_material(self, pem: str, text: str):
        """``text`` holds no PEM armour or body. A full body line is longer than a public key."""
        self.assertNotIn("PRIVATE KEY", text)
        body = [line for line in pem.splitlines() if not line.startswith("-----")]
        self.assertTrue(body)
        for line in body:
            self.assertNotIn(line, text)


class _WithoutTool(_IsolatedStore):
    """Hides ssh-keygen, so the fake private keys are stored without being parsed by it."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(keys.shutil, "which", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def key_files(self, seed: int = 1, name: str = "id_test", comment: str = "me@laptop", cipher: bytes = b"none"):
        private = self.source_dir / name
        private.write_text(fake_private_pem(blob(seed), cipher))
        (self.source_dir / f"{name}.pub").write_text(pub(seed, comment) + "\n")
        return private

    def imported(self, seed: int = 1, name: str = "id_test"):
        return keys.import_key(self.key_files(seed, name), f"label {seed}")


class KeyStoreFileTests(_WithoutTool):
    def test_ensure_key_dir_creates_it_private(self):
        self.permissive_umask()
        path = keys.ensure_key_dir()
        self.assertEqual(path, self.config_dir / "ssh" / "keys")
        self.assertEqual(mode(path), 0o700)

    def test_ensure_key_dir_tightens_an_existing_dir(self):
        keys.key_dir().mkdir(parents=True)
        keys.key_dir().chmod(0o755)
        keys.ensure_key_dir()
        self.assertEqual(mode(keys.key_dir()), 0o700)

    def test_files_are_private_under_a_permissive_umask(self):
        self.permissive_umask()
        source = self.key_files()
        source.chmod(0o644)
        key = keys.import_key(source, "camera")
        self.assertEqual(mode(keys.key_dir()), 0o700)
        self.assertEqual(mode(keys.key_path(key)), 0o600)
        self.assertEqual(mode(store.ssh_file()), 0o600)

    def test_stored_under_the_key_dir_with_nothing_left_beside_it(self):
        key = self.imported()
        self.assertEqual(key.private_file, f"keys/{key.id}.pem")
        self.assertEqual(keys.key_path(key), (keys.key_dir() / f"{key.id}.pem").resolve())
        self.assertEqual([p.name for p in keys.key_dir().iterdir()], [f"{key.id}.pem"])

    def test_read_private_returns_the_stored_key(self):
        source = self.key_files()
        key = keys.import_key(source, "camera")
        self.assertEqual(keys.read_private(key), source.read_text())

    def test_key_path_refuses_to_leave_the_key_dir(self):
        key = self.imported()
        for bad in ("../../outside.pem", "/etc/passwd", "known_hosts", "keys", ""):
            with self.subTest(bad=bad), self.assertRaises(SshKeyError):
                keys.key_path(dataclasses.replace(key, private_file=bad))


class MetadataTests(_WithoutTool):
    def test_store_holds_metadata_only(self):
        source = self.key_files(comment="me@laptop")
        key = keys.import_key(source, "camera")

        raw = store.ssh_file().read_text()
        self.assert_no_private_material(source.read_text(), raw)
        self.assertEqual(
            json.loads(raw)["keys"][key.id],
            {
                "label": "camera",
                "type": "ssh-ed25519",
                "private_file": f"keys/{key.id}.pem",
                "public_key": pub(1, "me@laptop"),
                "fingerprint": hostkeys.fingerprint(blob(1)),
                "comment": "me@laptop",
                "encrypted": False,
                "created": key.created,
            },
        )

    def test_key_object_carries_no_private_material(self):
        source = self.key_files()
        key = keys.import_key(source, "camera")
        self.assert_no_private_material(source.read_text(), repr(key))

    def test_public_key_and_fingerprint_agree(self):
        key = self.imported()
        self.assertEqual(hostkeys.fingerprint_from_public_line(key.public_key), key.fingerprint)
        self.assertEqual(hostkeys.parse_public_line(key.public_key)[1], blob(1))


class ListGetTests(_WithoutTool):
    def test_empty_store(self):
        self.assertEqual(keys.list_keys(), [])
        self.assertIsNone(keys.default_key())
        self.assertIsNone(keys.get_key("k_missing"))

    def test_get_and_list_read_back_what_import_returned(self):
        first = self.imported(1, "one")
        second = self.imported(2, "two")
        self.assertEqual(keys.get_key(first.id), first)
        self.assertEqual(keys.get_key(second.id), second)
        self.assertCountEqual(keys.list_keys(), [first, second])
        self.assertIn(keys.default_key(), (first, second))

    def test_list_skips_entries_of_the_wrong_shape(self):
        key = self.imported()
        data = store.read()
        data["keys"]["k_broken"] = "not a mapping"
        store.write(data)
        self.assertEqual(keys.list_keys(), [key])


class DeleteTests(_WithoutTool):
    def test_removes_the_file_and_the_metadata(self):
        key = self.imported()
        path = keys.key_path(key)
        self.assertTrue(keys.delete_key(key.id))
        self.assertFalse(path.exists())
        self.assertIsNone(keys.get_key(key.id))
        self.assertNotIn(key.id, store.read()["keys"])

    def test_unknown_id_returns_false(self):
        self.assertFalse(keys.delete_key("k_missing"))

    def test_second_delete_returns_false(self):
        key = self.imported()
        self.assertTrue(keys.delete_key(key.id))
        self.assertFalse(keys.delete_key(key.id))

    def test_leaves_other_keys_alone(self):
        doomed = self.imported(1, "one")
        kept = self.imported(2, "two")
        self.assertTrue(keys.delete_key(doomed.id))
        self.assertEqual(keys.list_keys(), [kept])
        self.assertTrue(keys.key_path(kept).exists())

    def test_already_missing_file_still_drops_the_metadata(self):
        key = self.imported()
        keys.key_path(key).unlink()
        self.assertTrue(keys.delete_key(key.id))
        self.assertIsNone(keys.get_key(key.id))

    def test_file_that_cannot_be_deleted_keeps_the_metadata(self):
        # Reporting success here would leave a private key on disk that Quak no longer knows about.
        key = self.imported()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(SshKeyError):
                keys.delete_key(key.id)
        self.assertEqual(keys.get_key(key.id), key)
        self.assertTrue(keys.key_path(key).exists())

    def test_tampered_path_drops_the_entry_without_touching_the_target(self):
        outside = self.source_dir / "victim"
        known_hosts = self.config_dir / "ssh" / "known_hosts"
        for victim, private_file in ((outside, str(outside)), (known_hosts, "known_hosts")):
            with self.subTest(private_file=private_file):
                key = self.imported()
                victim.write_text("keep me")
                data = store.read()
                data["keys"][key.id]["private_file"] = private_file
                store.write(data)
                self.assertTrue(keys.delete_key(key.id))
                self.assertEqual(victim.read_text(), "keep me")
                self.assertIsNone(keys.get_key(key.id))


class ImportWithoutToolTests(_WithoutTool):
    def test_uses_the_public_file_beside_the_key(self):
        key = keys.import_key(self.key_files(comment="me@laptop"), "camera")
        self.assertEqual(key.label, "camera")
        self.assertEqual(key.type, "ssh-ed25519")
        self.assertEqual(key.public_key, pub(1, "me@laptop"))
        self.assertEqual(key.comment, "me@laptop")
        self.assertEqual(key.fingerprint, hostkeys.fingerprint(blob(1)))
        self.assertFalse(key.encrypted)

    def test_public_file_is_found_for_a_name_with_a_suffix(self):
        key = keys.import_key(self.key_files(name="camera.key"), "camera")
        self.assertEqual(key.public_key, pub(1, "me@laptop"))

    def test_label_defaults_to_the_file_name(self):
        self.assertEqual(keys.import_key(self.key_files(name="id_camera"), "").label, "id_camera")

    def test_source_files_are_left_alone(self):
        source = self.key_files()
        before = source.read_text()
        keys.import_key(source, "camera")
        self.assertEqual(source.read_text(), before)

    def test_public_key_that_does_not_match_is_refused(self):
        source = self.key_files(seed=1)
        (self.source_dir / "id_test.pub").write_text(pub(2) + "\n")
        with self.assertRaises(SshKeyError):
            keys.import_key(source, "camera")
        self.assert_nothing_stored()

    def test_second_line_in_the_public_file_stays_out_of_the_key_line(self):
        source = self.key_files()
        (self.source_dir / "id_test.pub").write_text(f"{pub(1, 'note')}\nssh-ed25519 {encoded(2)} sneaky\n")
        key = keys.import_key(source, "camera")
        self.assertNotIn("\n", key.public_key)
        self.assertEqual(keys.authorized_blobs(keys.add_public_key("", key.public_key)), {encoded(1)})

    def test_junk_public_file_is_refused(self):
        source = self.key_files()
        (self.source_dir / "id_test.pub").write_text("not a public key\n")
        with self.assertRaises(SshKeyError):
            keys.import_key(source, "camera")
        self.assert_nothing_stored()

    def test_encrypted_key_is_recorded_as_encrypted_without_a_passphrase(self):
        key = keys.import_key(self.key_files(cipher=b"aes256-ctr"), "camera")
        self.assertTrue(key.encrypted)

    def test_legacy_encrypted_pem_is_recorded_as_encrypted(self):
        source = self.source_dir / "legacy"
        source.write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00\n\n"
            "ZmlsbGVy\n-----END RSA PRIVATE KEY-----\n"
        )
        (self.source_dir / "legacy.pub").write_text(pub(1) + "\n")
        self.assertTrue(keys.import_key(source, "camera").encrypted)

    def test_missing_file_is_a_key_error(self):
        with self.assertRaises(SshKeyError):
            keys.import_key(self.source_dir / "nope", "camera")

    def test_binary_file_is_a_key_error(self):
        source = self.source_dir / "binary"
        source.write_bytes(b"\xff\xfe\x00binary")
        with self.assertRaises(SshKeyError):
            keys.import_key(source, "camera")

    def test_not_a_private_key_is_refused(self):
        source = self.source_dir / "notes.txt"
        source.write_text("just some notes\n")
        with self.assertRaises(SshKeyError):
            keys.import_key(source, "camera")
        self.assert_nothing_stored()

    def test_no_public_file_uses_the_blob_embedded_in_the_private_key(self):
        source = self.key_files()
        (self.source_dir / "id_test.pub").unlink()
        key = keys.import_key(source, "camera")
        self.assertEqual(key.type, "ssh-ed25519")
        self.assertEqual(key.fingerprint, hostkeys.fingerprint(blob(1)))
        self.assertFalse(key.encrypted)

    def test_generate_without_the_tool_names_the_tool(self):
        with self.assertRaisesRegex(SshKeyError, "ssh-keygen"):
            keys.generate("camera")
        self.assert_nothing_stored()


class KeygenFailureTests(_IsolatedStore):
    """What the system tool does is faked here; only the error handling is being checked."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(keys.shutil, "which", return_value="/usr/bin/ssh-keygen")
        patch.start()
        self.addCleanup(patch.stop)

    def test_only_ed25519_is_generated(self):
        with self.assertRaises(SshKeyError):
            keys.generate("camera", key_type="ssh-rsa")

    def test_failed_generate_is_a_key_error(self):
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="no entropy\n")
        with mock.patch.object(keys.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(SshKeyError, "no entropy"):
                keys.generate("camera")
        self.assert_nothing_stored()

    def test_timeout_never_reveals_the_passphrase(self):
        # str(TimeoutExpired) quotes the whole command line, and the passphrase is on it.
        def timed_out(args, **_):
            raise subprocess.TimeoutExpired(args, 20)

        with mock.patch.object(keys.subprocess, "run", side_effect=timed_out):
            with self.assertRaises(SshKeyError) as caught:
                keys.generate("camera", passphrase=PASSPHRASE)
        self.assertNotIn(PASSPHRASE, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assert_nothing_stored()


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is not installed")
class _RealKeys(_IsolatedStore):
    def make_key(self, name: str = "id_test", passphrase: str = "", comment: str = "me@laptop") -> Path:
        path = self.source_dir / name
        result = run_keygen("-q", "-t", "ed25519", "-N", passphrase, "-C", comment, "-f", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        return path

    def public_of(self, private: Path, passphrase: str = "") -> str:
        """The public key ssh-keygen itself reads from a private key file."""
        result = run_keygen("-y", "-P", passphrase, "-f", str(private))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def assert_consistent(self, key):
        """Metadata, stored private key and the system tool's fingerprint all describe one key."""
        passphrase = PASSPHRASE if key.encrypted else ""
        derived = self.public_of(keys.key_path(key), passphrase)
        self.assertEqual(derived.split()[:2], key.public_key.split()[:2])

        listed = self.source_dir / "listed.pub"
        listed.write_text(key.public_key + "\n")
        self.assertEqual(run_keygen("-lf", str(listed)).stdout.split()[1], key.fingerprint)

        key_type, key_blob, comment = hostkeys.parse_public_line(key.public_key)
        self.assertEqual(key.type, key_type)
        self.assertEqual(key.comment, comment)
        self.assertEqual(key.fingerprint, hostkeys.fingerprint(key_blob))
        self.assertEqual(key.public_key, keys.public_line(key_type, key_blob, comment))


class GenerateTests(_RealKeys):
    def test_generates_a_consistent_ed25519_key(self):
        key = keys.generate("camera", comment="me@laptop")
        self.assertTrue(key.id.startswith("k_"))
        self.assertEqual(key.label, "camera")
        self.assertEqual(key.type, "ssh-ed25519")
        self.assertEqual(key.comment, "me@laptop")
        self.assertFalse(key.encrypted)
        self.assertTrue(key.created)
        self.assert_consistent(key)

    def test_is_stored_and_read_back(self):
        key = keys.generate("camera")
        self.assertEqual(keys.get_key(key.id), key)
        self.assertEqual(keys.list_keys(), [key])
        self.assertEqual(keys.default_key(), key)

    def test_files_are_private_under_a_permissive_umask(self):
        self.permissive_umask()
        key = keys.generate("camera")
        self.assertEqual(mode(keys.key_dir()), 0o700)
        self.assertEqual(mode(keys.key_path(key)), 0o600)
        self.assertEqual(mode(store.ssh_file()), 0o600)
        self.assertEqual([p.name for p in keys.key_dir().iterdir()], [f"{key.id}.pem"])

    def test_store_has_no_private_material(self):
        key = keys.generate("camera")
        pem = keys.read_private(key)
        self.assertIn("PRIVATE KEY", pem)
        self.assert_no_private_material(pem, store.ssh_file().read_text())
        self.assert_no_private_material(pem, repr(key))

    def test_default_comment_names_quak(self):
        self.assertTrue(keys.generate("camera").comment.startswith("quak@"))
        self.assertTrue(keys.generate("camera", comment=" \n ").comment.startswith("quak@"))

    def test_label_defaults_to_the_id(self):
        key = keys.generate("")
        self.assertEqual(key.label, key.id)

    def test_newline_in_the_comment_stays_on_one_line(self):
        key = keys.generate("camera", comment="one\ntwo")
        self.assertEqual(key.comment, "one two")
        self.assertEqual(len(key.public_key.splitlines()), 1)
        self.assert_consistent(key)

    def test_passphrase_encrypts_the_key_and_stays_out_of_the_store(self):
        key = keys.generate("camera", passphrase=PASSPHRASE)
        self.assertTrue(key.encrypted)
        self.assert_consistent(key)
        self.assertNotIn(PASSPHRASE, store.ssh_file().read_text())
        self.assertNotIn(PASSPHRASE, repr(key))
        self.assertNotEqual(run_keygen("-y", "-P", "wrong passphrase", "-f", str(keys.key_path(key))).returncode, 0)

    def test_generated_keys_differ(self):
        self.assertNotEqual(keys.generate("one").fingerprint, keys.generate("two").fingerprint)

    def test_works_with_authorized_keys(self):
        key = keys.generate("camera")
        text = keys.add_public_key(SAMPLE, key.public_key)
        self.assertTrue(keys.has_public_key(text, key.public_key))
        self.assertEqual(keys.key_count(text), keys.key_count(SAMPLE) + 1)
        self.assertEqual(keys.remove_public_key(text, key.public_key), SAMPLE)

    def test_delete_removes_a_generated_key(self):
        key = keys.generate("camera")
        path = keys.key_path(key)
        self.assertTrue(keys.delete_key(key.id))
        self.assertFalse(path.exists())
        self.assertEqual(keys.list_keys(), [])


class ImportTests(_RealKeys):
    def drop_public(self, private: Path):
        Path(f"{private}.pub").unlink()

    def test_derives_the_public_key_when_there_is_no_public_file(self):
        source = self.make_key(comment="from the key")
        expected = Path(f"{source}.pub").read_text().strip()
        self.drop_public(source)
        key = keys.import_key(source, "camera")
        self.assertEqual(key.public_key, expected)
        self.assertEqual(key.comment, "from the key")
        self.assertFalse(key.encrypted)
        self.assert_consistent(key)

    def test_uses_the_comment_from_a_matching_public_file(self):
        source = self.make_key(comment="original")
        public = Path(f"{source}.pub")
        public.write_text(" ".join(public.read_text().split()[:2]) + " edited by hand\n")
        key = keys.import_key(source, "camera")
        self.assertEqual(key.comment, "edited by hand")
        self.assert_consistent(key)

    def test_stored_copy_is_identical_and_private(self):
        self.permissive_umask()
        source = self.make_key()
        source.chmod(0o644)
        key = keys.import_key(source, "camera")
        self.assertEqual(keys.read_private(key), source.read_text())
        self.assertEqual(mode(keys.key_path(key)), 0o600)
        self.assertEqual(mode(keys.key_dir()), 0o700)
        self.assertEqual(mode(store.ssh_file()), 0o600)

    def test_store_has_no_private_material(self):
        source = self.make_key()
        key = keys.import_key(source, "camera")
        self.assert_no_private_material(source.read_text(), store.ssh_file().read_text())
        self.assert_no_private_material(source.read_text(), repr(key))

    def test_public_file_for_another_key_is_refused(self):
        source = self.make_key("one")
        other = self.make_key("two")
        Path(f"{source}.pub").write_text(Path(f"{other}.pub").read_text())
        with self.assertRaises(SshKeyError):
            keys.import_key(source, "camera")
        self.assert_nothing_stored()

    def test_corrupt_private_key_is_refused(self):
        source = self.source_dir / "broken"
        source.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nZmlsbGVy\n-----END OPENSSH PRIVATE KEY-----\n")
        with self.assertRaises(SshKeyError):
            keys.import_key(source, "camera")
        self.assert_nothing_stored()

    def test_encrypted_key_with_its_passphrase(self):
        source = self.make_key(passphrase=PASSPHRASE)
        self.drop_public(source)
        key = keys.import_key(source, "camera", passphrase=PASSPHRASE)
        self.assertTrue(key.encrypted)
        self.assert_consistent(key)
        self.assertNotIn(PASSPHRASE, store.ssh_file().read_text())

    def test_encrypted_key_needs_a_passphrase_or_a_public_file(self):
        source = self.make_key(passphrase=PASSPHRASE)
        self.drop_public(source)
        with self.assertRaises(SshKeyError):
            keys.import_key(source, "camera")
        self.assert_nothing_stored()

    def test_encrypted_key_with_a_public_file_needs_no_passphrase(self):
        source = self.make_key(passphrase=PASSPHRASE)
        key = keys.import_key(source, "camera")
        self.assertTrue(key.encrypted)
        self.assert_consistent(key)

    def test_encrypted_key_with_a_public_file_for_another_key_is_refused(self):
        # With no passphrase the key can't be opened, but its public half is readable anyway.
        source = self.make_key("one", passphrase=PASSPHRASE)
        other = self.make_key("two")
        Path(f"{source}.pub").write_text(Path(f"{other}.pub").read_text())
        with self.assertRaises(SshKeyError):
            keys.import_key(source, "camera")
        self.assert_nothing_stored()

    def test_wrong_passphrase_is_refused_without_repeating_it(self):
        source = self.make_key(passphrase=PASSPHRASE)
        for has_public_file in (True, False):
            if not has_public_file:
                self.drop_public(source)
            with self.subTest(has_public_file=has_public_file):
                with self.assertRaises(SshKeyError) as caught:
                    keys.import_key(source, "camera", passphrase="the wrong passphrase")
                self.assertNotIn("the wrong passphrase", str(caught.exception))
                self.assert_nothing_stored()

    def test_passphrase_given_for_an_unencrypted_key_does_not_mark_it_encrypted(self):
        self.assertFalse(keys.import_key(self.make_key(), "camera", passphrase=PASSPHRASE).encrypted)

    def test_delete_removes_an_imported_key_and_keeps_the_source(self):
        source = self.make_key()
        key = keys.import_key(source, "camera")
        path = keys.key_path(key)
        self.assertTrue(keys.delete_key(key.id))
        self.assertFalse(path.exists())
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()

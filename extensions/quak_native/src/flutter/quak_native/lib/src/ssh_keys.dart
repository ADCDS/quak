import 'dart:convert';
import 'dart:isolate';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart' show Ed25519, Sha256;
import 'package:dartssh2/dartssh2.dart' show OpenSSHEd25519KeyPair;

/// A newly generated key, before anything has stored it.
class GeneratedSshKey {
  const GeneratedSshKey({required this.privatePem, required this.publicKey, required this.fingerprint});

  /// OpenSSH private key; encrypted when a passphrase was given.
  final String privatePem;

  /// `ssh-ed25519 <base64 blob> <comment>`, without the comment when it's empty.
  final String publicKey;

  /// `SHA256:<base64>` without padding, as OpenSSH prints it.
  final String fingerprint;
}

/// Generates an Ed25519 key. A null or empty [passphrase] leaves the private key unencrypted.
Future<GeneratedSshKey> generateEd25519({String comment = '', String? passphrase}) async {
  // Same cleanup as quak.ssh.keys: a line break here would add a line to authorized_keys.
  final cleanComment = comment.trim().split(RegExp(r'\s+')).join(' ');

  final keyPair = await Ed25519().newKeyPair();
  final seed = await keyPair.extractPrivateKeyBytes();
  final public = (await keyPair.extractPublicKey()).bytes;
  final pair = OpenSSHEd25519KeyPair(
    Uint8List.fromList(public),
    Uint8List.fromList([...seed, ...public]),
    cleanComment,
  );

  // With a passphrase this runs bcrypt_pbkdf, which would stall the UI thread.
  final privatePem = await Isolate.run(() => pair.toPem(passphrase: passphrase));
  final blob = pair.toPublicKey().encode();
  final line = 'ssh-ed25519 ${base64.encode(blob)}';
  return GeneratedSshKey(
    privatePem: privatePem,
    publicKey: cleanComment.isEmpty ? line : '$line $cleanComment',
    fingerprint: await fingerprintOfPublicBlob(blob),
  );
}

/// OpenSSH-style `SHA256:<base64>` fingerprint, without padding, of a raw public key blob.
Future<String> fingerprintOfPublicBlob(List<int> blob) async {
  final digest = await Sha256().hash(blob);
  return 'SHA256:${base64.encode(digest.bytes).replaceAll('=', '')}';
}

/// Fingerprint of a `<type> <base64 blob> [comment]` public key line.
Future<String> fingerprintOfPublicLine(String line) async {
  final parts = line.trim().split(RegExp(r'\s+'));
  if (parts.length < 2) throw const FormatException('not a public key line');
  return fingerprintOfPublicBlob(base64.decode(parts[1]));
}

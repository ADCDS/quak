import 'dart:async';
import 'dart:convert';
import 'dart:isolate';

import 'package:dartssh2/dartssh2.dart'
    show
        SSHAuthAbortError,
        SSHClient,
        SSHHostkeyError,
        SSHKeyDecodeError,
        SSHKeyDecryptError,
        SSHKeyPair,
        SSHMessageError,
        SSHSocket;
import 'package:flet/flet.dart';

import 'ssh_keys.dart';

/// SSH for Quak on the phone: host key scans, key generation, and one-off logins that test the login or run a
/// command. A login only goes ahead with a server that offers the expected host key.
class QuakSshService extends FletService {
  QuakSshService({required super.control});

  static const _connectTimeout = Duration(seconds: 10);
  static const _handshakeTimeout = Duration(seconds: 20);
  static const _authTimeout = Duration(seconds: 30);
  static const _testTimeout = Duration(seconds: 20); // the command, once logged in: as long as the system ssh gets
  static const _execTimeout = Duration(seconds: 120);
  static const _testCommand = "id; uname -srm";

  @override
  void init() {
    super.init();
    control.addInvokeMethodListener(_invoke);
  }

  Future<dynamic> _invoke(String name, dynamic args) async {
    switch (name) {
      case "scan_host_key":
        return scanHostKey(args["host"] as String, (args["port"] as num).toInt());
      case "generate_key":
        return generateKey(args["comment"] as String? ?? "", args["passphrase"] as String?);
      case "fingerprint":
        return fingerprint(args["public_key_line"] as String);
      case "test":
        return test(SshLogin.fromArgs(args));
      case "exec":
        return exec(SshLogin.fromArgs(args), args["command"] as String? ?? "");
      default:
        throw Exception("Unknown QuakSsh method: $name");
    }
  }

  /// Reads the server's host key and hangs up before authentication: {key_type, fingerprint}.
  ///
  /// dartssh2 checks the key's signature before it calls onVerifyHostKey, so a captured key is
  /// one the server has proved it holds. Refusing the key closes the connection.
  Future<Map<String, String>> scanHostKey(String host, int port) async {
    final socket = await SSHSocket.connect(host, port, timeout: _connectTimeout);
    String? keyType;
    String? keyFingerprint;
    final client = SSHClient(
      socket,
      username: "", // never sent: refusing the key below ends the session before authentication
      handshakeTimeout: _handshakeTimeout,
      onVerifyHostKey: (type, encoded) {
        keyType = type;
        keyFingerprint = utf8.decode(encoded); // already "SHA256:<base64>", unpadded
        return false;
      },
    );
    try {
      await client.done; // completes once the transport has closed the socket
    } on SSHHostkeyError {
      // Expected after the refusal above. With nothing captured, the signature check failed.
      if (keyFingerprint == null) rethrow;
    }
    if (keyFingerprint == null) {
      throw Exception("$host:$port closed the connection before offering a host key");
    }
    return {"key_type": keyType!, "fingerprint": keyFingerprint!};
  }

  /// A new Ed25519 key: {private_pem, public_key, fingerprint}.
  Future<Map<String, String>> generateKey(String comment, String? passphrase) async {
    final key = await generateEd25519(comment: comment, passphrase: passphrase);
    return {"private_pem": key.privatePem, "public_key": key.publicKey, "fingerprint": key.fingerprint};
  }

  Future<String> fingerprint(String publicKeyLine) => fingerprintOfPublicLine(publicKeyLine);

  /// Logs in and runs `id; uname -srm`: {ok, uid, uname, error}, as the system ssh's test reads it.
  ///
  /// Every failure comes back in `error`, a host key that doesn't match included, never as a throw.
  Future<Map<String, dynamic>> test(SshLogin login) async {
    try {
      final result = await _loggedIn(login, (client) => client.runWithResult(_testCommand).timeout(_testTimeout));
      final stdout = utf8.decode(result.stdout, allowMalformed: true).trim();
      final code = result.exitCode;
      if (code != 0) {
        final stderr = utf8.decode(result.stderr, allowMalformed: true).trim();
        final exited = code == null ? "The test command ended without an exit code" : "The test command exited $code";
        return _testResult(error: stderr.isNotEmpty ? stderr : (stdout.isNotEmpty ? stdout : exited));
      }
      final lines = LineSplitter.split(stdout).where((line) => line.trim().isNotEmpty).toList();
      return _testResult(
        ok: true,
        uid: lines.isNotEmpty ? lines[0] : "",
        uname: lines.length > 1 ? lines[1] : "",
      );
    } on _SshFailure catch (failure) {
      return _testResult(error: failure.message);
    }
  }

  /// Logs in and runs [command]: {exit_code, stdout, stderr}.
  ///
  /// The output is decoded as UTF-8, with any invalid byte replaced. exit_code is null when the server reported
  /// none, as when a signal ended the command. A failure to log in or to finish within the time limit throws, so
  /// it can't be taken for the command's own exit.
  Future<Map<String, dynamic>> exec(SshLogin login, String command) async {
    if (command.trim().isEmpty) throw const _SshFailure("No remote command given");
    final result = await _loggedIn(login, (client) => client.runWithResult(command).timeout(_execTimeout));
    return {
      "exit_code": result.exitCode,
      "stdout": utf8.decode(result.stdout, allowMalformed: true),
      "stderr": utf8.decode(result.stderr, allowMalformed: true),
    };
  }

  /// Logs in as [login] says and hands the client to [use]. Whatever happens, the connection is closed afterwards.
  ///
  /// A server that doesn't offer the expected host key is refused before authentication, so no secret reaches it.
  /// Anything that goes wrong, in here or in [use], throws a [_SshFailure] with a message fit to show.
  Future<T> _loggedIn<T>(SshLogin login, Future<T> Function(SSHClient client) use) async {
    if (login.host.isEmpty || login.username.isEmpty) {
      throw const _SshFailure("A host and a username are needed to connect");
    }
    if (login.expectedFingerprint.isEmpty) {
      // Without one any server would pass, so don't even open the socket.
      throw const _SshFailure("No expected host key fingerprint, so the server can't be verified");
    }

    SSHSocket? socket;
    SSHClient? client;
    String? refusedFingerprint;
    try {
      final identities =
          login.privateKeyPem.isEmpty ? null : await _decodePrivateKey(login.privateKeyPem, login.passphrase);
      socket = await SSHSocket.connect(login.host, login.port, timeout: _connectTimeout);
      client = SSHClient(
        socket,
        username: login.username,
        identities: identities,
        onPasswordRequest: login.password.isEmpty ? null : () => login.password,
        handshakeTimeout: _handshakeTimeout,
        authTimeout: _authTimeout,
        onVerifyHostKey: (type, fingerprint) {
          final offered = utf8.decode(fingerprint); // already "SHA256:<base64>", unpadded
          if (offered == login.expectedFingerprint) return true;
          refusedFingerprint = offered;
          return false;
        },
      );
      await client.authenticated;
      return await use(client);
    } catch (error) {
      final refused = refusedFingerprint;
      throw _SshFailure(refused != null
          ? "Host key mismatch: expected ${login.expectedFingerprint}, the server offered $refused"
          : _describe(error));
    } finally {
      // Not awaited: closing an io socket waits for the server's side too, and destroy() ends it here and now.
      // catchError: a socket that already failed can fail again as it closes, and it's going anyway.
      if (client != null) unawaited(client.close().catchError((Object _) {}));
      socket?.destroy();
    }
  }

  @override
  void dispose() {
    control.removeInvokeMethodListener(_invoke);
    super.dispose();
  }
}

/// Where to log in and with what. It holds the secrets, so it goes to dartssh2 and nowhere else.
class SshLogin {
  SshLogin.fromArgs(dynamic args)
      : host = args["host"] as String? ?? "",
        port = (args["port"] as num? ?? 22).toInt(),
        username = args["username"] as String? ?? "",
        password = args["password"] as String? ?? "",
        privateKeyPem = args["private_key_pem"] as String? ?? "",
        passphrase = args["passphrase"] as String? ?? "",
        expectedFingerprint = args["expected_fingerprint"] as String? ?? "";

  final String host;
  final int port;
  final String username;
  final String password; // empty: don't offer one
  final String privateKeyPem; // empty: no key
  final String passphrase;
  final String expectedFingerprint; // SHA256:<base64>, unpadded
}

/// A failed login or command, with a message that says what happened and quotes nothing secret.
class _SshFailure implements Exception {
  const _SshFailure(this.message);

  final String message;

  @override
  String toString() => message; // what Flet hands the Python side
}

Map<String, dynamic> _testResult({bool ok = false, String uid = "", String uname = "", String error = ""}) =>
    {"ok": ok, "uid": uid, "uname": uname, "error": error};

/// Reads a private key on another isolate: with a passphrase this runs bcrypt_pbkdf, which would
/// stall the UI thread. The same as the terminal's.
Future<List<SSHKeyPair>> _decodePrivateKey(String pem, String passphrase) async {
  try {
    return await Isolate.run(() {
      // dartssh2 refuses a passphrase for an unencrypted key.
      final encrypted = SSHKeyPair.isEncryptedPem(pem);
      return SSHKeyPair.fromPem(pem, encrypted && passphrase.isNotEmpty ? passphrase : null);
    });
  } on SSHKeyDecryptError {
    rethrow; // "Invalid passphrase" and the like, fixed strings
  } catch (_) {
    // Not forwarded: a FormatException can quote the PEM it stopped at.
    throw SSHKeyDecodeError("The private key is damaged or in an unsupported format");
  }
}

String _describe(Object error) {
  if (error is SSHAuthAbortError && error.reason != null) return _describe(error.reason!);
  if (error is SSHMessageError) return error.message;
  if (error is TimeoutException) return "Timed out waiting for the server";
  return error.toString();
}

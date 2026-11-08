import 'dart:async';
import 'dart:convert';
import 'dart:isolate';
import 'dart:typed_data';

import 'package:dartssh2/dartssh2.dart'
    show
        SSHAuthAbortError,
        SSHClient,
        SSHKeyDecodeError,
        SSHKeyDecryptError,
        SSHKeyPair,
        SSHMessageError,
        SSHPtyConfig,
        SSHSession,
        SSHSocket;
import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart' show TargetPlatform, defaultTargetPlatform;
import 'package:flutter/widgets.dart';
import 'package:xterm/xterm.dart' show Terminal, TerminalStyle, TerminalView;

/// An interactive SSH shell on the phone.
///
/// Every `connect` ends in a `connected` or an `error` event, unless a later `connect` or
/// `disconnect` abandons it. Every `connected` is followed by one `disconnected`, except when the
/// control leaves the page: that hangs up without an event.
class QuakTerminalWidget extends StatefulWidget {
  const QuakTerminalWidget({super.key, required this.control});

  final Control control;

  @override
  State<QuakTerminalWidget> createState() => _QuakTerminalWidgetState();
}

class _QuakTerminalWidgetState extends State<QuakTerminalWidget> {
  static const _connectTimeout = Duration(seconds: 10);
  static const _handshakeTimeout = Duration(seconds: 20);
  static const _authTimeout = Duration(seconds: 30);
  static const _defaultFontSize = 14.0;

  final _terminal = Terminal();

  SSHClient? _client;
  SSHSession? _session;
  List<StreamSubscription<String>> _output = const [];

  /// Moves on whenever a connection is given up, so its late callbacks can tell they're stale.
  int _attempt = 0;

  @override
  void initState() {
    super.initState();
    widget.control.addInvokeMethodListener(_invokeMethod);
    if (widget.control.getBool("auto_connect", false)!) {
      // After the first layout, so the shell starts at the size the view really has.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) unawaited(_connect());
      });
    }
  }

  @override
  void dispose() {
    widget.control.removeInvokeMethodListener(_invokeMethod);
    _hangUp();
    super.dispose();
  }

  Future<dynamic> _invokeMethod(String name, dynamic args) async {
    switch (name) {
      case "connect":
        // The outcome arrives as an event: a login can outlast any sensible method call timeout.
        unawaited(_connect());
      case "disconnect":
        _disconnect();
      case "send_text":
        final session = _session;
        if (session == null) throw Exception("QuakTerminal is not connected");
        session.write(utf8.encode(args["text"] as String));
      case "clear":
        // Erase the screen and the scrollback, then move the cursor home.
        _terminal.write("\x1b[2J\x1b[3J\x1b[H");
      case "resize":
        _terminal.resize((args["width"] as num).toInt(), (args["height"] as num).toInt());
        _terminal.notifyListeners(); // Terminal.resize doesn't repaint by itself
      default:
        throw Exception("Unknown QuakTerminal method: $name");
    }
  }

  Future<void> _connect() async {
    _disconnect();
    final attempt = _attempt;

    final control = widget.control;
    final host = control.getString("host", "")!;
    final port = control.getInt("port", 22)!;
    final username = control.getString("username", "")!;
    final expectedFingerprint = control.getString("expected_fingerprint", "")!;
    final privateKeyPem = control.getString("private_key_pem", "")!;
    final passphrase = control.getString("passphrase", "")!;
    final password = control.getString("password", "")!;

    if (host.isEmpty || username.isEmpty) {
      _reportError("A host and a username are needed to connect");
      return;
    }
    if (expectedFingerprint.isEmpty) {
      // Without one any server would pass, so don't even open the socket.
      _reportError("No expected host key fingerprint, so the server can't be verified");
      return;
    }

    SSHClient? client;
    String? refusedFingerprint;
    try {
      final identities = privateKeyPem.isEmpty ? null : await _decodePrivateKey(privateKeyPem, passphrase);
      if (attempt != _attempt) return;

      final socket = await SSHSocket.connect(host, port, timeout: _connectTimeout);
      if (attempt != _attempt) {
        socket.destroy();
        return;
      }

      client = SSHClient(
        socket,
        username: username,
        identities: identities,
        onPasswordRequest: password.isEmpty ? null : () => password,
        handshakeTimeout: _handshakeTimeout,
        authTimeout: _authTimeout,
        onVerifyHostKey: (type, fingerprint) {
          final offered = utf8.decode(fingerprint); // already "SHA256:<base64>", unpadded
          if (offered == expectedFingerprint) return true;
          refusedFingerprint = offered;
          return false;
        },
      );
      _client = client;
      await client.authenticated;

      final columns = _terminal.viewWidth;
      final rows = _terminal.viewHeight;
      final session = await client.shell(pty: SSHPtyConfig(width: columns, height: rows));
      if (attempt != _attempt) return; // _hangUp has already closed the client

      _session = session;
      _output = [_decode(session.stdout).listen(_terminal.write), _decode(session.stderr).listen(_terminal.write)];
      _terminal.onOutput = (data) => session.write(utf8.encode(data));
      _terminal.onResize = (width, height, pixelWidth, pixelHeight) =>
          session.resizeTerminal(width, height, pixelWidth, pixelHeight);
      if (_terminal.viewWidth != columns || _terminal.viewHeight != rows) {
        session.resizeTerminal(_terminal.viewWidth, _terminal.viewHeight); // resized while the shell was starting
      }

      // The shell exiting ends the connection, so client.done is the one place that sees the end.
      final connected = client;
      unawaited(session.done.then((_) => _close(connected)));
      unawaited(connected.done.then(
        (_) => _onClosed(attempt),
        onError: (Object error) => _onClosed(attempt, error),
      ));
      control.triggerEvent("connected");
    } catch (error) {
      if (client != null) _close(client);
      if (attempt != _attempt) return;
      _client = null;
      final refused = refusedFingerprint;
      _reportError(refused != null
          ? "Host key mismatch: expected $expectedFingerprint, the server offered $refused"
          : _describe(error));
    }
  }

  void _disconnect() {
    final wasConnected = _session != null;
    _hangUp();
    if (wasConnected) widget.control.triggerEvent("disconnected");
  }

  /// Gives up on the current connection or attempt, without reporting anything.
  void _hangUp() {
    final client = _client;
    _forgetConnection();
    for (final subscription in _output) {
      unawaited(subscription.cancel());
    }
    _output = const [];
    if (client != null) _close(client);
  }

  /// The connection ended on its own. Stdout stays subscribed for the output still on its way.
  void _onClosed(int attempt, [Object? error]) {
    if (attempt != _attempt) return;
    _forgetConnection();
    if (error != null) _reportError(_describe(error));
    widget.control.triggerEvent("disconnected");
  }

  void _forgetConnection() {
    _attempt++;
    _terminal.onOutput = null;
    _terminal.onResize = null;
    _session = null;
    _client = null;
  }

  void _reportError(String message) => widget.control.triggerEvent("error", {"message": message});

  @override
  Widget build(BuildContext context) {
    return LayoutControl(
      control: widget.control,
      child: TerminalView(
        _terminal,
        textStyle: TerminalStyle(fontSize: widget.control.getDouble("font_size", _defaultFontSize)!),
        // On-screen keyboards don't send a key event for backspace.
        deleteDetection:
            defaultTargetPlatform == TargetPlatform.android || defaultTargetPlatform == TargetPlatform.iOS,
      ),
    );
  }
}

/// Reads a private key on another isolate: with a passphrase this runs bcrypt_pbkdf, which would
/// stall the UI thread.
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

void _close(SSHClient client) {
  // catchError: a socket that already failed can fail again as it closes, and it's going anyway.
  unawaited(client.close().catchError((Object _) {}));
}

/// allowMalformed: one stray byte from the server shouldn't end the session. The decoder is chunked,
/// so a character split across two packets still arrives whole.
Stream<String> _decode(Stream<Uint8List> bytes) =>
    bytes.cast<List<int>>().transform(const Utf8Decoder(allowMalformed: true));

String _describe(Object error) {
  if (error is SSHAuthAbortError && error.reason != null) return _describe(error.reason!);
  if (error is SSHMessageError) return error.message;
  return error.toString();
}

import 'dart:async';
import 'dart:typed_data';

import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart';
import 'package:universal_ble/universal_ble.dart';

String _hex(Uint8List b) => b.map((x) => x.toRadixString(16).padLeft(2, '0')).join();

Uint8List _bytes(String h) {
  final out = Uint8List(h.length ~/ 2);
  for (var i = 0; i < out.length; i++) {
    out[i] = int.parse(h.substring(i * 2, i * 2 + 2), radix: 16);
  }
  return out;
}

/// Bluetooth LE central for Quak: scan, connect, bond, GATT read/write/notify.
class QuakBleService extends FletService {
  QuakBleService({required super.control});

  final Map<String, Map<String, dynamic>> _found = {};
  final Map<String, String> _serviceOf = {}; // characteristic uuid -> service uuid
  String? _device;

  @override
  void init() {
    super.init();
    control.addInvokeMethodListener(_invoke);
    UniversalBle.onScanResult = (d) {
      final gopro = d.manufacturerDataList.where((m) => m.companyId == 0x02F2).toList();
      _found[d.deviceId] = {
        "address": d.deviceId,
        "name": d.name ?? "",
        "rssi": d.rssi ?? 0,
        "gopro": gopro.isEmpty ? "" : _hex(gopro.first.payload),
      };
    };
    UniversalBle.onValueChange = (v) {
      control.triggerEvent("notify", {"uuid": v.characteristicId.toLowerCase(), "value": _hex(v.value)});
    };
    UniversalBle.onConnectionChange = (c) {
      if (!c.isConnected && c.deviceId == _device) {
        control.triggerEvent("disconnected", {"address": c.deviceId});
      }
    };
  }

  String _svc(String uuid) {
    final s = _serviceOf[uuid.toLowerCase()];
    if (s == null) throw Exception("characteristic $uuid not found on the device");
    return s;
  }

  Future<dynamic> _invoke(String name, dynamic args) async {
    switch (name) {
      case "permissions":
        await UniversalBle.requestPermissions(withAndroidFineLocation: true);
        return (await UniversalBle.getBluetoothAvailabilityState()).name;
      case "scan":
        _found.clear();
        final secs = (args["seconds"] as num?)?.toDouble() ?? 6;
        await UniversalBle.startScan();
        await Future.delayed(Duration(milliseconds: (secs * 1000).round()));
        await UniversalBle.stopScan();
        return _found.values.toList();
      case "connect":
        final id = args["address"] as String;
        _device = id;
        await UniversalBle.connect(id, timeout: Duration(seconds: (args["timeout"] as num?)?.toInt() ?? 20));
        final services = await UniversalBle.discoverServices(id);
        _serviceOf.clear();
        for (final s in services) {
          for (final c in s.characteristics) {
            _serviceOf[c.uuid.toLowerCase()] = s.uuid;
          }
        }
        var mtu = 23;
        try {
          mtu = await UniversalBle.requestMtu(id, 247);
        } catch (e) {
          debugPrint("QuakBle: MTU request failed: $e");
        }
        return {"mtu": mtu, "characteristics": _serviceOf.keys.toList()};
      case "is_paired":
        return await UniversalBle.isPaired(args["address"] as String);
      case "pair":
        // Android asks the user to confirm ("Pair with GoPro ####?"); the library's 10 s default is too short
        await UniversalBle.pair(args["address"] as String, timeout: const Duration(seconds: 60));
        return true;
      case "disconnect":
        if (_device != null) await UniversalBle.disconnect(_device!);
        return true;
      case "write":
        final uuid = (args["uuid"] as String).toLowerCase();
        await UniversalBle.write(_device!, _svc(uuid), uuid, _bytes(args["data"] as String));
        return true;
      case "read":
        final uuid = (args["uuid"] as String).toLowerCase();
        return _hex(await UniversalBle.read(_device!, _svc(uuid), uuid));
      case "subscribe":
        final uuid = (args["uuid"] as String).toLowerCase();
        await UniversalBle.subscribeNotifications(_device!, _svc(uuid), uuid);
        return true;
      default:
        throw Exception("Unknown QuakBle method: $name");
    }
  }

  @override
  void dispose() {
    control.removeInvokeMethodListener(_invoke);
    UniversalBle.onScanResult = null;
    UniversalBle.onValueChange = null;
    UniversalBle.onConnectionChange = null;
    super.dispose();
  }
}

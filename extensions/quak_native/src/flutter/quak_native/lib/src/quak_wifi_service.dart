import 'package:flet/flet.dart';
import 'package:wifi_iot/wifi_iot.dart';

/// Joins the camera's access point for this app only (Android 10+: WifiNetworkSpecifier, which
/// shows the system's "Connect to device" prompt) and routes the app's traffic over it, so the
/// Python side can reach the camera's HTTP API at 10.5.5.9.
class QuakWifiService extends FletService {
  QuakWifiService({required super.control});

  bool _joined = false;

  @override
  void init() {
    super.init();
    control.addInvokeMethodListener(_invoke);
  }

  Future<dynamic> _invoke(String name, dynamic args) async {
    switch (name) {
      case "join":
        final joined = await WiFiForIoTPlugin.connect(args["ssid"] as String,
            password: args["password"] as String?,
            security: NetworkSecurity.WPA,
            joinOnce: true,
            withInternet: false, // false = WifiNetworkSpecifier, scoped to this app
            timeoutInSeconds: (args["timeout"] as num?)?.toInt() ?? 60);
        if (!joined) return {"joined": false, "bound": false};
        _joined = true;
        final bound = await WiFiForIoTPlugin.forceWifiUsage(true); // bindProcessToNetwork
        return {"joined": true, "bound": bound};
      case "release":
        await WiFiForIoTPlugin.forceWifiUsage(false);
        if (_joined) {
          await WiFiForIoTPlugin.disconnect(); // drops the request; Android goes back to the previous network
          _joined = false;
        }
        return true;
      case "status":
        return {
          "connected": await WiFiForIoTPlugin.isConnected(),
          "ssid": await WiFiForIoTPlugin.getSSID(),
          "joined": _joined,
        };
      default:
        throw Exception("Unknown QuakWifi method: $name");
    }
  }

  @override
  void dispose() {
    control.removeInvokeMethodListener(_invoke);
    super.dispose();
  }
}

import 'package:flet/flet.dart';

import 'quak_ble_service.dart';
import 'quak_wifi_service.dart';

class Extension extends FletExtension {
  @override
  FletService? createService(Control control) {
    switch (control.type) {
      case "QuakBle":
        return QuakBleService(control: control);
      case "QuakWifi":
        return QuakWifiService(control: control);
      default:
        return null;
    }
  }
}

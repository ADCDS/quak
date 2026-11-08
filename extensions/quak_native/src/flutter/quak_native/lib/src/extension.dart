import 'package:flet/flet.dart';
import 'package:flutter/widgets.dart' show Key, Widget;

import 'quak_ble_service.dart';
import 'quak_ssh_service.dart';
import 'quak_terminal_control.dart';
import 'quak_wifi_service.dart';

class Extension extends FletExtension {
  @override
  FletService? createService(Control control) {
    switch (control.type) {
      case "QuakBle":
        return QuakBleService(control: control);
      case "QuakWifi":
        return QuakWifiService(control: control);
      case "QuakSsh":
        return QuakSshService(control: control);
      default:
        return null;
    }
  }

  @override
  Widget? createWidget(Key? key, Control control) {
    switch (control.type) {
      case "QuakTerminal":
        return QuakTerminalWidget(key: key, control: control);
      default:
        return null;
    }
  }
}

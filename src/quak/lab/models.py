"""What the Camera Lab reports: plain data carried between the Lab, the CLI and, later, the app."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HostModeState:
    """The USB-C port as the camera reports it. ``enabled`` means whole: the switch is on and the root hubs are up."""

    enabled: bool = False
    supported: bool = False  # this camera has the dwc3_milbeaut switch
    hostmode: str = ""  # the raw parameter: 1 host, 0 peripheral, 2 as the device tree says (the camera's default)
    devices: list[str] = field(default_factory=list)  # the root hubs found: usb1, usb2
    error: str = ""
    partial: bool = False  # neither whole host mode nor whole device mode: a switch was left unfinished
    udc: str = ""  # the controller the USB gadget is attached to, empty when detached
    paused: list[str] = field(default_factory=list)  # which of gpsv and gpUsb are stopped
    marker: str = ""  # the phase in Quak's marker: switching, host, or unknown if unreadable. Empty without a marker


@dataclass
class HostModeResult:
    state: HostModeState
    steps: list[str] = field(default_factory=list)  # what actually changed on the camera, in order
    message: str = ""


@dataclass
class UsbInterface:
    id: str
    iclass: str = ""
    driver: str = ""


@dataclass
class UsbDevice:
    id: str
    vid: str = ""
    pid: str = ""
    product: str = ""
    manufacturer: str = ""
    bclass: str = ""
    speed: str = ""
    driver: str = ""
    interfaces: list[UsbInterface] = field(default_factory=list)


@dataclass
class Volume:
    device: str
    label: str = ""
    fs: str = ""
    size: int = 0
    mountpoint: str = ""
    mounted: bool = False
    read_only: bool = True


@dataclass
class AudioDevice:
    """One way a sound card can capture. A card that offers several has an entry for each."""

    card: int
    device: int = 0
    name: str = ""
    capture_format: str = ""  # S16_LE, as ALSA spells it. Empty when this card can't capture
    rates: list[int] = field(default_factory=list)  # in Hz
    channels: int = 0


@dataclass
class NetworkInterface:
    name: str
    mac: str = ""  # empty for a link type that has none, like a tunnel
    up: bool = False  # switched on, whether or not a cable or an access point is there
    kind: str = "other"  # ethernet, wifi, usb or other
    addresses: list[str] = field(default_factory=list)  # IPv4, as ip prints them: 192.168.1.5/24
    driver: str = ""  # empty when there is no device behind it, like lo


@dataclass
class Framebuffer:
    name: str  # what its driver calls it, which can be nothing
    virtual_size: str = ""  # 1920x1080. Empty when the camera can't say
    bits_per_pixel: int = 0  # 0 when the camera can't say
    state: str = ""  # running or suspended, anything else as the kernel has it. Empty when the camera can't say


@dataclass
class SerialPort:
    device: str  # /dev/ttyUSB0
    driver: str = ""  # empty when sysfs names none
    usb_ids: str = ""  # vendor:product, as lsusb prints them: 0403:6001. Empty when the port isn't on USB


@dataclass
class PackModule:
    name: str
    deps: list[str] = field(default_factory=list)
    sha256: str = ""


@dataclass
class PackTool:
    name: str
    sha256: str = ""


@dataclass
class PackManifest:
    id: str
    version: str = ""
    kernel: str = ""
    vermagic: str = ""
    config_sha256: str | None = None
    modules: list[PackModule] = field(default_factory=list)
    tools: list[str | PackTool] = field(default_factory=list)  # a bare name has no hash, so it can't be installed
    firmware: list[str] = field(default_factory=list)
    firmware_sha256: dict[str, str] = field(default_factory=dict)  # relative to firmware/, required for every firmware file
    license: str = ""
    source: str = ""


@dataclass
class PackState:
    installed: bool = False
    loaded: bool = False
    path: str = ""
    error: str = ""


@dataclass
class LabStatus:
    host_mode: HostModeState | None = None
    devices: list[UsbDevice] = field(default_factory=list)
    toolkit: PackState | None = None
    battery: str = ""
    error: str = ""

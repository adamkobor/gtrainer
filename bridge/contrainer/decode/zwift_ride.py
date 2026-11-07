"""Zwift Ride (firmware <= 1.2.x) BLE protocol.

Reverse-engineered by the community (MAKINOLO, Zwift Play/Ride projects). On firmware
1.2 there is no encryption: write b"RideOn" to SYNC_RX and button frames arrive as
notifications. Firmware 1.3+ ("Ride V2") hides the service until Zwift unlocks it.

Frame = 1 opcode byte + protobuf payload.
  0x23 RideKeyPadStatus: field 1 uint32 ButtonMap (bit CLEARED = pressed),
                         field 3 repeated {1: location enum, 2: sint32 analog value}
                         (all four locations are always sent; 0 = released;
                         the paddles swing both ways: -100..100)
  0x15 empty/keepalive (about once per second), 0x2A device status (ignored)
  0x19 BatteryNotification: field 2 uint32 percent
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .protobuf import WIRE_LEN, WIRE_VARINT, DecodeError, iter_fields, zigzag_decode

SERVICE_UUID = "00000001-19ca-4651-86e5-fa29dcdd09d1"
ADVERTISED_SERVICE_UUID = "0000fc82-0000-1000-8000-00805f9b34fb"
ASYNC_UUID = "00000002-19ca-4651-86e5-fa29dcdd09d1"  # notify: button frames
SYNC_RX_UUID = "00000003-19ca-4651-86e5-fa29dcdd09d1"  # write without response
SYNC_TX_UUID = "00000004-19ca-4651-86e5-fa29dcdd09d1"  # indicate: command responses

FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"

RIDE_ON = b"RideOn"

OP_EMPTY = 0x15
OP_BATTERY = 0x19
OP_CONTROLLER = 0x23
OP_RIDE_ON = 0x52  # == ord("R")

# Ride button bit masks (a cleared bit means the button is pressed).
BUTTON_MASKS: dict[str, int] = {
    "LEFT": 0x0001,
    "UP": 0x0002,
    "RIGHT": 0x0004,
    "DOWN": 0x0008,
    "A": 0x0010,
    "B": 0x0020,
    "Y": 0x0040,
    "Z": 0x0080,
    "SHIFT_UP_L": 0x0100,
    "SHIFT_DN_L": 0x0200,
    "POWERUP_L": 0x0400,
    "ONOFF_L": 0x0800,
    "SHIFT_UP_R": 0x1000,
    "SHIFT_DN_R": 0x2000,
    "POWERUP_R": 0x4000,
    "ONOFF_R": 0x8000,
}
RIDE_BUTTONS: tuple[str, ...] = tuple(BUTTON_MASKS)
ALL_RELEASED = 0xFFFFFFFF

PADDLE_LEFT = 0
PADDLE_RIGHT = 1
PADDLE_FULL_SCALE = 100  # observed range -100..100; the sign is the swing direction

MAX_SUPPORTED_FIRMWARE = (1, 2)


@dataclass(frozen=True)
class KeypadStatus:
    pressed: frozenset[str]
    paddles: dict[int, int] = field(default_factory=dict)  # location -> raw analog value
    button_map: int = ALL_RELEASED

    def paddle(self, location: int) -> float:
        """Paddle deflection normalized to -1..1 (sign = swing direction)."""
        raw = self.paddles.get(location, 0) / PADDLE_FULL_SCALE
        return max(-1.0, min(raw, 1.0))


@dataclass(frozen=True)
class Battery:
    percent: int


@dataclass(frozen=True)
class RideOnAck:
    pass


@dataclass(frozen=True)
class Empty:
    pass


@dataclass(frozen=True)
class Unknown:
    opcode: int
    payload: bytes


Message = KeypadStatus | Battery | RideOnAck | Empty | Unknown


def decode_keypad(payload: bytes, *, cleared_means_pressed: bool = True) -> KeypadStatus:
    button_map: int | None = None
    paddles: dict[int, int] = {}
    for num, wire, value in iter_fields(payload):
        if num == 1 and wire == WIRE_VARINT:
            button_map = int(value) & 0xFFFFFFFF
        elif num == 3 and wire == WIRE_LEN:
            location, analog = 0, 0
            for num2, wire2, value2 in iter_fields(value):  # type: ignore[arg-type]
                if num2 == 1 and wire2 == WIRE_VARINT:
                    location = int(value2)
                elif num2 == 2 and wire2 == WIRE_VARINT:
                    analog = zigzag_decode(int(value2))
            paddles[location] = analog
    if button_map is None:
        # proto3 omits zero values; a literal "every button pressed" frame is not
        # physically plausible, so treat a missing map as "nothing pressed".
        button_map = ALL_RELEASED if cleared_means_pressed else 0
    if cleared_means_pressed:
        pressed = frozenset(n for n, m in BUTTON_MASKS.items() if not button_map & m)
    else:
        pressed = frozenset(n for n, m in BUTTON_MASKS.items() if button_map & m)
    return KeypadStatus(pressed=pressed, paddles=paddles, button_map=button_map)


def decode_battery(payload: bytes) -> Battery:
    percent = 0
    for num, wire, value in iter_fields(payload):
        if num == 2 and wire == WIRE_VARINT:
            percent = int(value)
    return Battery(percent=percent)


def parse_message(data: bytes, *, cleared_means_pressed: bool = True) -> Message | None:
    if not data:
        return None
    if data.startswith(RIDE_ON):
        return RideOnAck()
    opcode, payload = data[0], bytes(data[1:])
    if opcode == OP_EMPTY and not payload:
        return Empty()
    try:
        if opcode == OP_CONTROLLER:
            return decode_keypad(payload, cleared_means_pressed=cleared_means_pressed)
        if opcode == OP_BATTERY:
            return decode_battery(payload)
    except DecodeError:
        return Unknown(opcode, payload)
    return Unknown(opcode, payload)


def parse_firmware(revision: str) -> tuple[int, ...] | None:
    parts: list[int] = []
    for chunk in revision.strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or None


def firmware_supported(revision: str) -> bool | None:
    """True/False if the firmware is known to work/be locked; None if unparseable."""
    version = parse_firmware(revision)
    if version is None:
        return None
    return version[:2] <= MAX_SUPPORTED_FIRMWARE

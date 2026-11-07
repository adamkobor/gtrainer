import struct

import pytest

from gtrainer.decode import zwift_ride as zr
from gtrainer.decode.fitness import (
    CadenceCalculator,
    parse_cycling_power,
    parse_indoor_bike_data,
)
from gtrainer.decode.protobuf import encode_varint, zigzag_decode, zigzag_encode


def keypad_frame(button_map: int | None = zr.ALL_RELEASED, paddles: dict[int, int] | None = None) -> bytes:
    body = b""
    if button_map is not None:
        body += b"\x08" + encode_varint(button_map)
    for loc, val in (paddles or {}).items():
        inner = b"\x08" + encode_varint(loc) + b"\x10" + encode_varint(zigzag_encode(val))
        body += b"\x1a" + encode_varint(len(inner)) + inner
    return bytes([zr.OP_CONTROLLER]) + body


def test_released_frame_matches_known_bytes():
    # BikeControl's documented "released" frame: 08 FF FF FF FF 0F
    frame = bytes([0x23, 0x08, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F])
    assert frame == keypad_frame()
    msg = zr.parse_message(frame)
    assert isinstance(msg, zr.KeypadStatus)
    assert msg.pressed == frozenset()


@pytest.mark.parametrize("name", zr.RIDE_BUTTONS)
def test_each_button_cleared_bit_is_pressed(name):
    msg = zr.parse_message(keypad_frame(zr.ALL_RELEASED & ~zr.BUTTON_MASKS[name]))
    assert msg.pressed == frozenset({name})


def test_multiple_buttons_and_inverted_polarity_option():
    m = zr.ALL_RELEASED & ~(zr.BUTTON_MASKS["A"] | zr.BUTTON_MASKS["SHIFT_UP_R"])
    assert zr.parse_message(keypad_frame(m)).pressed == {"A", "SHIFT_UP_R"}
    set_bits = zr.BUTTON_MASKS["B"]
    assert zr.parse_message(keypad_frame(set_bits), cleared_means_pressed=False).pressed == {"B"}


def test_missing_button_map_means_released():
    assert zr.parse_message(keypad_frame(None)).pressed == frozenset()


def test_paddles_signed_and_clamped():
    msg = zr.parse_message(keypad_frame(paddles={zr.PADDLE_LEFT: 50, zr.PADDLE_RIGHT: -100}))
    assert msg.paddle(zr.PADDLE_LEFT) == pytest.approx(0.5)
    assert msg.paddle(zr.PADDLE_RIGHT) == pytest.approx(-1.0)
    assert zr.parse_message(keypad_frame(paddles={0: 140})).paddle(0) == 1.0
    assert msg.pressed == frozenset()


# Real frames captured from a Zwift Ride (hw B.0, fw 1.2.0), 2026-11-07.
REAL_IDLE = "2308ffffffff0f1a04080010001a04080110001a04080210001a0408031000"
REAL_LEFT_BUTTON = "2308feffffff0f1a04080010001a04080110001a04080210001a0408031000"
REAL_SHIFT_UP_R = "2308ffdfffff0f1a04080010001a04080110001a04080210001a0408031000"
REAL_RIGHT_PADDLE_POS = "2308ffffffff0f1a04080010001a05080110c8011a04080210001a0408031000"
REAL_LEFT_PADDLE_NEG = "2308ffffffff0f1a05080010c7011a04080110001a04080210001a0408031000"
REAL_BOTH_PADDLES = "2308ffffffff0f1a05080010c8011a05080110c7011a04080210001a0408031000"
REAL_LEFT_PARTIAL = "2308ffffffff0f1a04080010291a05080110c7011a04080210001a0408031000"


@pytest.mark.parametrize(
    ("hexframe", "pressed", "left", "right"),
    [
        (REAL_IDLE, set(), 0.0, 0.0),
        (REAL_LEFT_BUTTON, {"LEFT"}, 0.0, 0.0),
        (REAL_SHIFT_UP_R, {"SHIFT_UP_R"}, 0.0, 0.0),
        (REAL_RIGHT_PADDLE_POS, set(), 0.0, 1.0),
        (REAL_LEFT_PADDLE_NEG, set(), -1.0, 0.0),
        (REAL_BOTH_PADDLES, set(), 1.0, -1.0),
        (REAL_LEFT_PARTIAL, set(), -0.21, -1.0),
    ],
)
def test_real_ride_frames(hexframe, pressed, left, right):
    msg = zr.parse_message(bytes.fromhex(hexframe))
    assert isinstance(msg, zr.KeypadStatus)
    assert msg.pressed == pressed
    assert set(msg.paddles) == {0, 1, 2, 3}  # every location is always sent
    assert msg.paddle(zr.PADDLE_LEFT) == pytest.approx(left)
    assert msg.paddle(zr.PADDLE_RIGHT) == pytest.approx(right)


def test_real_handshake_and_housekeeping_frames():
    assert zr.parse_message(bytes.fromhex("526964654f6e")) == zr.RideOnAck()
    assert zr.parse_message(bytes.fromhex("15")) == zr.Empty()
    assert zr.parse_message(bytes.fromhex("19105a")) == zr.Battery(90)
    status = zr.parse_message(bytes.fromhex("2a0803120f120d0818100018482205" "0f003fbf1e"))
    assert isinstance(status, zr.Unknown) and status.opcode == 0x2A


def test_battery_and_rideon_and_garbage():
    assert zr.parse_message(bytes([0x19, 0x10, 87])) == zr.Battery(87)
    assert zr.parse_message(b"RideOn\x01\x03") == zr.RideOnAck()
    garbage = zr.parse_message(bytes([0x23, 0x08, 0xFF]))  # truncated varint
    assert isinstance(garbage, zr.Unknown)
    assert zr.parse_message(b"") is None


@pytest.mark.parametrize(
    ("rev", "ok"), [("1.2.0", True), ("1.1.9", True), ("1.3.0", False), ("2.0", False), ("garbage", None)]
)
def test_firmware_gate(rev, ok):
    assert zr.firmware_supported(rev) is ok


def test_zigzag_roundtrip():
    for n in (0, 1, -1, 63, -64, 100, -100, 2**31 - 1, -(2**31)):
        assert zigzag_decode(zigzag_encode(n)) == n


def test_ftms_indoor_bike_data_basic():
    # flags: speed present (bit0 = 0), cadence (bit2), power (bit6)
    data = struct.pack("<HHHh", 0x0044, 3050, 180, 250)
    s = parse_indoor_bike_data(data)
    assert (s.speed_kph, s.cadence_rpm, s.power_w) == (30.5, 90.0, 250.0)


def test_ftms_skips_optional_fields():
    # avg speed (bit1), cadence (bit2), avg cadence (bit3), distance (bit4), resistance (bit5), power (bit6)
    flags = 0x0002 | 0x0004 | 0x0008 | 0x0010 | 0x0020 | 0x0040
    data = struct.pack("<HH", flags, 2000)  # inst speed
    data += struct.pack("<H", 1900)  # avg speed
    data += struct.pack("<H", 170)  # cadence 85
    data += struct.pack("<H", 160)  # avg cadence
    data += (12345).to_bytes(3, "little")  # distance
    data += struct.pack("<h", 10)  # resistance
    data += struct.pack("<h", 321)  # power
    s = parse_indoor_bike_data(data)
    assert (s.speed_kph, s.cadence_rpm, s.power_w) == (20.0, 85.0, 321.0)


def test_ftms_more_data_bit_means_no_speed():
    s = parse_indoor_bike_data(struct.pack("<Hh", 0x0041, 150))
    assert s.speed_kph is None and s.power_w == 150.0


def test_truncated_ftms_raises():
    with pytest.raises(ValueError):
        parse_indoor_bike_data(struct.pack("<H", 0x0044) + b"\x01")


def test_cycling_power_with_crank_and_cadence_rollover():
    data = struct.pack("<HhIHHH", 0x0030, 280, 1000, 2048, 65535, 65000)
    s = parse_cycling_power(data)
    assert s.power_w == 280 and s.crank_revs == 65535 and s.crank_event_time == 65000
    calc = CadenceCalculator()
    calc.update(65535, 65000, now=0.0)
    # +1 rev (wraps to 0) in 683/1024 s (wraps too) -> ~90 rpm
    cad = calc.update(0, (65000 + 683) & 0xFFFF, now=0.7)
    assert cad == pytest.approx(90, abs=0.2)
    # no new revs for > stale window -> 0
    assert calc.update(0, (65000 + 683) & 0xFFFF, now=4.0) == 0.0


# Real frames from a Wahoo KICKR CORE (hw 3, fw 3.5.34), 2026-11-07, ~1 Hz each.
@pytest.mark.parametrize(
    ("hexframe", "speed", "cadence", "power"),
    [
        ("4400000000000000", 0.0, 0.0, 0.0),
        ("4400780600005e00", 16.56, 0.0, 94.0),
        ("440065080000d900", 21.49, 0.0, 217.0),  # power is there, cadence not yet
        ("44006e098000e900", 24.14, 64.0, 233.0),
    ],
)
def test_real_kickr_indoor_bike_data(hexframe, speed, cadence, power):
    s = parse_indoor_bike_data(bytes.fromhex(hexframe))
    assert (s.speed_kph, s.cadence_rpm, s.power_w) == (speed, cadence, power)


def test_real_kickr_cycling_power():
    # flags 0x34: accumulated torque + wheel revs + crank revs
    a = parse_cycling_power(bytes.fromhex("3400d10082201b00000076f502007107"))
    b = parse_cycling_power(bytes.fromhex("3400bb0018241e000000ccfc0300130b"))
    assert (a.power_w, a.crank_revs, a.crank_event_time) == (209.0, 2, 1905)
    assert (b.power_w, b.crank_revs, b.crank_event_time) == (187.0, 3, 2835)
    calc = CadenceCalculator()
    calc.update(a.crank_revs, a.crank_event_time, now=0.0)
    assert calc.update(b.crank_revs, b.crank_event_time, now=1.0) == pytest.approx(66.1, abs=0.1)

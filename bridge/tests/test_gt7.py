import struct

import pytest

from gtrainer.decode import fitness as f
from gtrainer.gt7 import telemetry as tm
from gtrainer.gt7.gears import gear_fraction, grade_for_power, power_at
from gtrainer.gt7.resistance import compute_target
from gtrainer.signals import SignalHub


def make_packet(*, gear=3, suggested=15, ratios=(3.2, 2.1, 1.6, 1.3, 1.1, 0.9), flags=tm.FLAG_ON_TRACK,
                packet_id=42, speed_ms=30.0, rpm=6500.0, throttle=200) -> bytes:
    p = bytearray(tm.MIN_PACKET)
    struct.pack_into("<I", p, 0, tm.MAGIC)
    struct.pack_into("<f", p, tm.OFF_RPM, rpm)
    struct.pack_into("<f", p, tm.OFF_SPEED, speed_ms)
    struct.pack_into("<i", p, tm.OFF_PACKET_ID, packet_id)
    struct.pack_into("<h", p, tm.OFF_FLAGS, flags)
    p[tm.OFF_GEARS] = (suggested << 4) | gear
    p[tm.OFF_THROTTLE] = throttle
    padded = list(ratios) + [0.0] * (8 - len(ratios))
    struct.pack_into("<8f", p, tm.OFF_GEAR_RATIOS, *padded)
    return bytes(p)


def test_decrypt_and_parse_roundtrip():
    data = tm.encrypt(make_packet(), seed=0x12345678)
    assert data[:4] != struct.pack("<I", tm.MAGIC)  # actually encrypted
    plain = tm.decrypt(data)
    assert plain is not None
    s = tm.parse(plain)
    assert (s.gear, s.suggested_gear, s.num_gears, s.packet_id) == (3, None, 6, 42)
    assert s.on_track and s.driving and not s.paused
    assert s.speed_kph == pytest.approx(108.0)
    assert s.rpm == pytest.approx(6500.0)
    assert s.throttle == pytest.approx(200 / 255)


def test_flags_and_suggested_gear():
    s = tm.parse(make_packet(flags=tm.FLAG_ON_TRACK | tm.FLAG_PAUSED, suggested=4, gear=0))
    assert s.paused and not s.driving and s.suggested_gear == 4 and s.gear == 0
    assert not tm.parse(make_packet(flags=0)).driving
    assert tm.parse(make_packet(ratios=(3.0, 2.0, 1.5, 1.2, 1.0, 0.85, 0.75, 0.65))).num_gears == 8


def test_decrypt_rejects_garbage_and_wrong_key():
    assert tm.decrypt(b"short") is None
    assert tm.decrypt(bytes(tm.MIN_PACKET)) is None
    data = bytearray(tm.encrypt(make_packet(), seed=1))
    data[0] ^= 0xFF
    assert tm.decrypt(bytes(data)) is None


def test_telemetry_ignores_stale_packets_and_other_hosts():
    states = []
    t = tm.Gt7Telemetry(states.append, host="10.0.0.5")
    t._on_packet(tm.encrypt(make_packet(packet_id=10), 7), ("10.0.0.5", 33739))
    t._on_packet(tm.encrypt(make_packet(packet_id=9), 7), ("10.0.0.5", 33739))  # older
    t._on_packet(tm.encrypt(make_packet(packet_id=11), 7), ("10.0.0.9", 33739))  # other host
    t._on_packet(tm.encrypt(make_packet(packet_id=11), 7), ("10.0.0.5", 33739))
    assert [s.packet_id for s in states] == [10, 11]
    assert t.live and t.to_json()["state"]["gear"] == 3


@pytest.mark.parametrize(
    ("gear", "expected"), [(0, 0.5), (1, 0.5), (2, 0.6), (3, 0.7), (4, 0.8), (5, 0.9), (6, 1.0), (9, 1.0)]
)
def test_gear_fraction_six_speed(gear, expected):
    assert gear_fraction(gear, 6, 0.5) == pytest.approx(expected)


def test_gear_fraction_edge_cases():
    assert gear_fraction(1, 1, 0.5) == 1.0  # single-gear car (EV)
    assert gear_fraction(1, 2, 0.4) == pytest.approx(0.4)
    assert gear_fraction(2, 2, 0.4) == 1.0


def test_grade_for_power_inverts_power_model():
    for target in (100, 150, 200, 300):
        grade = grade_for_power(target, speed_kph=33.6, weight_kg=76)
        assert power_at(grade, 33.6, 76) == pytest.approx(target, rel=1e-6)
    assert grade_for_power(100, 33.6, 76) == pytest.approx(1.04, abs=0.01)
    assert grade_for_power(200, 33.6, 76) == pytest.approx(2.47, abs=0.01)
    assert grade_for_power(5000, 10, 60) == 20.0  # clamped


def test_hub_power_gear_follows_telemetry_and_falls_back():
    now = [0.0]
    hub = SignalHub(clock=lambda: now[0], ftp_w=200, first_gear_ftp=0.5)
    hub.update_bike(f.BikeSample(power_w=100))
    snap = hub.snapshot()
    assert not snap.gt7_live and snap.power_gear == pytest.approx(0.5)  # = power / FTP
    hub.update_gt7(tm.parse(make_packet(gear=1)))
    snap = hub.snapshot()
    assert snap.gt7_live and snap.gear == 1 and snap.gear_target_w == pytest.approx(100)
    assert snap.power_gear == pytest.approx(1.0)  # 100 W = full throttle in 1st
    hub.update_gt7(tm.parse(make_packet(gear=6)))
    assert hub.snapshot().power_gear == pytest.approx(0.5)  # top gear needs FTP
    now[0] = 2.0  # telemetry stale (bike data still fresh)
    snap = hub.snapshot()
    assert not snap.gt7_live and snap.power_gear == pytest.approx(0.5)


def test_hub_learns_speed_per_rpm():
    hub = SignalHub()
    for _ in range(100):
        hub.update_bike(f.BikeSample(power_w=200, cadence_rpm=80, speed_kph=32))
    assert hub.speed_per_rpm == pytest.approx(0.4, abs=0.001)
    hub.update_bike(f.BikeSample(power_w=0, cadence_rpm=0, speed_kph=20))  # coasting: ignored
    assert hub.speed_per_rpm == pytest.approx(0.4, abs=0.001)


def test_resistance_target_per_state():
    now = [0.0]
    hub = SignalHub(clock=lambda: now[0])
    kw = dict(ftp_w=200, first_gear_ftp=0.5, target_cadence_rpm=90, weight_kg=76)
    t = compute_target(hub, **kw)
    assert (t.reason, t.gear, t.target_w) == ("no telemetry", 1, 100)
    hub.update_gt7(tm.parse(make_packet(gear=4)))
    t4 = compute_target(hub, **kw)
    assert (t4.reason, t4.gear, t4.target_w) == ("driving", 4, pytest.approx(160))
    hub.update_gt7(tm.parse(make_packet(gear=6)))
    t6 = compute_target(hub, **kw)
    assert t6.target_w == pytest.approx(200) and t6.grade_pct > t4.grade_pct > t.grade_pct
    hub.update_gt7(tm.parse(make_packet(gear=6, flags=tm.FLAG_ON_TRACK | tm.FLAG_PAUSED)))
    tp = compute_target(hub, **kw)
    assert (tp.reason, tp.gear, tp.grade_pct) == ("not driving", 1, t.grade_pct)
    # target power at target cadence: speed_per_rpm * 90
    assert power_at(t4.grade_pct, hub.speed_per_rpm * 90, 76) == pytest.approx(160, rel=1e-3)


def test_ftms_control_point_messages():
    assert f.build_request_control() == b"\x00"
    assert f.build_reset() == b"\x01"
    assert f.build_simulation(1.44, crr=0.004, cw=0.0) == bytes([0x11, 0, 0, 0x90, 0x00, 40, 0])
    assert f.build_simulation(-2.5) == bytes([0x11, 0, 0]) + struct.pack("<h", -250) + bytes([40, 0])
    assert f.parse_control_response(bytes([0x80, 0x11, 0x01])) == (0x11, f.RESULT_SUCCESS)
    assert f.parse_control_response(bytes([0x80, 0x00, 0x05])) == (0x00, f.RESULT_CONTROL_NOT_PERMITTED)
    assert f.parse_control_response(b"\x01\x02") is None
    assert f.parse_weight(b"`;") == pytest.approx(76.0)  # what the user's KICKR reported
    assert f.parse_weight(b"\x00\x00") is None

"""Standard Bluetooth fitness profiles: FTMS Indoor Bike Data and Cycling Power."""

from __future__ import annotations

import struct
from dataclasses import dataclass

FTMS_SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA_UUID = "00002ad2-0000-1000-8000-00805f9b34fb"
CPS_SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_MEASUREMENT_UUID = "00002a63-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class BikeSample:
    power_w: float | None = None
    cadence_rpm: float | None = None
    speed_kph: float | None = None


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = bytes(data)
        self.pos = 0

    def take(self, fmt: str) -> int:
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise ValueError("truncated fitness frame")
        (value,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return value

    def skip(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise ValueError("truncated fitness frame")
        self.pos += n

    def uint24(self) -> int:
        lo = self.take("<H")
        hi = self.take("<B")
        return lo | (hi << 16)


def parse_indoor_bike_data(data: bytes) -> BikeSample:
    """FTMS 0x2AD2. Note bit 0 is inverted: 0 means Instantaneous Speed IS present."""
    r = _Reader(data)
    flags = r.take("<H")
    speed = cadence = power = None
    if not flags & 0x0001:
        speed = r.take("<H") / 100.0  # km/h, 0.01 resolution
    if flags & 0x0002:
        r.skip(2)  # average speed
    if flags & 0x0004:
        cadence = r.take("<H") / 2.0  # rpm, 0.5 resolution
    if flags & 0x0008:
        r.skip(2)  # average cadence
    if flags & 0x0010:
        r.skip(3)  # total distance
    if flags & 0x0020:
        r.skip(2)  # resistance level
    if flags & 0x0040:
        power = float(r.take("<h"))
    # Remaining fields (avg power, energy, HR, MET, times) are not needed.
    return BikeSample(power_w=power, cadence_rpm=cadence, speed_kph=speed)


@dataclass(frozen=True)
class CyclingPowerSample:
    power_w: float
    crank_revs: int | None = None
    crank_event_time: int | None = None  # 1/1024 s, rolls over at 65536


def parse_cycling_power(data: bytes) -> CyclingPowerSample:
    r = _Reader(data)
    flags = r.take("<H")
    power = float(r.take("<h"))
    if flags & 0x0001:
        r.skip(1)  # pedal power balance
    if flags & 0x0004:
        r.skip(2)  # accumulated torque
    if flags & 0x0010:
        r.skip(6)  # wheel revs (u32) + last wheel event time (u16)
    crank_revs = crank_time = None
    if flags & 0x0020:
        crank_revs = r.take("<H")
        crank_time = r.take("<H")
    return CyclingPowerSample(power_w=power, crank_revs=crank_revs, crank_event_time=crank_time)


class CadenceCalculator:
    """Derives cadence from cumulative crank revolutions / event time (CPS or CSC)."""

    def __init__(self, stale_after_s: float = 3.0) -> None:
        self._last: tuple[int, int] | None = None
        self._last_change_at: float | None = None
        self._cadence = 0.0
        self.stale_after_s = stale_after_s

    def update(self, revs: int, event_time: int, now: float) -> float:
        if self._last is not None:
            d_revs = (revs - self._last[0]) & 0xFFFF
            d_time = (event_time - self._last[1]) & 0xFFFF
            if d_revs and d_time:
                self._cadence = d_revs * 60.0 * 1024.0 / d_time
                self._last_change_at = now
            elif self._last_change_at is not None and now - self._last_change_at > self.stale_after_s:
                self._cadence = 0.0
        else:
            self._last_change_at = now
        self._last = (revs, event_time)
        return self._cadence


# --- FTMS control point (trainer control; only used when GT7 resistance is enabled) ----------
FTMS_CONTROL_POINT_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"
USER_WEIGHT_UUID = "00002a98-0000-1000-8000-00805f9b34fb"  # uint16, 0.005 kg

OP_REQUEST_CONTROL = 0x00
OP_RESET = 0x01
OP_START_RESUME = 0x07
OP_SET_SIMULATION = 0x11
OP_RESPONSE = 0x80

RESULT_SUCCESS = 0x01
RESULT_NOT_SUPPORTED = 0x02
RESULT_INVALID_PARAMETER = 0x03
RESULT_FAILED = 0x04
RESULT_CONTROL_NOT_PERMITTED = 0x05


def build_request_control() -> bytes:
    return bytes([OP_REQUEST_CONTROL])


def build_reset() -> bytes:
    return bytes([OP_RESET])


def build_start() -> bytes:
    return bytes([OP_START_RESUME])


def build_simulation(grade_pct: float, crr: float = 0.004, cw: float = 0.0, wind_ms: float = 0.0) -> bytes:
    """Set Indoor Bike Simulation Parameters: wind sint16 (0.001 m/s), grade sint16
    (0.01 %), crr uint8 (0.0001), cw uint8 (0.01 kg/m)."""
    return struct.pack(
        "<BhhBB",
        OP_SET_SIMULATION,
        round(wind_ms * 1000),
        round(grade_pct * 100),
        max(0, min(255, round(crr * 10000))),
        max(0, min(255, round(cw * 100))),
    )


def parse_control_response(data: bytes) -> tuple[int, int] | None:
    """(request opcode, result code) from a control point indication, or None."""
    if len(data) >= 3 and data[0] == OP_RESPONSE:
        return data[1], data[2]
    return None


def parse_weight(data: bytes) -> float | None:
    if len(data) < 2:
        return None
    kg = struct.unpack_from("<H", data)[0] * 0.005
    return kg if 20 <= kg <= 250 else None

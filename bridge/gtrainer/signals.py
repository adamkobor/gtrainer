"""Normalized input signals shared between the BLE readers, the mapping engine and the UI."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .decode.fitness import BikeSample
from .gt7.gears import gear_fraction
from .gt7.telemetry import Gt7State
from .decode.zwift_ride import RIDE_BUTTONS, KeypadStatus, PADDLE_LEFT, PADDLE_RIGHT

# Analog sources usable in profiles. power_w/cadence_rpm/speed_kph are >= 0;
# power_ftp = power_w / rider FTP (1.0 = riding at FTP);
# power_gear = power_w / the current GT7 gear's target (1.0 = full throttle; falls
#   back to power_ftp without live telemetry);
# paddle_l/paddle_r are -1..1 (the Ride paddles swing both ways). Ride buttons are also usable as 0/1 sources
# with the "btn:" prefix, e.g. "btn:LEFT".
ANALOG_SOURCES: tuple[str, ...] = (
    "power_w", "power_ftp", "power_gear", "cadence_rpm", "speed_kph", "paddle_l", "paddle_r",
)
BUTTON_SOURCE_PREFIX = "btn:"


def is_valid_source(name: str) -> bool:
    if name.startswith(BUTTON_SOURCE_PREFIX):
        return name[len(BUTTON_SOURCE_PREFIX) :] in RIDE_BUTTONS
    return name in ANALOG_SOURCES


def all_sources() -> list[str]:
    return list(ANALOG_SOURCES) + [BUTTON_SOURCE_PREFIX + b for b in RIDE_BUTTONS]


@dataclass(frozen=True)
class Snapshot:
    power_w: float = 0.0
    power_ftp: float = 0.0
    power_gear: float = 0.0
    cadence_rpm: float = 0.0
    speed_kph: float = 0.0
    paddle_l: float = 0.0
    paddle_r: float = 0.0
    buttons: frozenset[str] = frozenset()
    ride_battery: int | None = None
    kickr_connected: bool = False
    ride_connected: bool = False
    # GT7 telemetry (gt7_live False = no fresh packets)
    gt7_live: bool = False
    gt7_driving: bool = False
    gear: int = 0
    num_gears: int = 0
    gear_target_w: float = 0.0

    def value(self, source: str) -> float:
        if source.startswith(BUTTON_SOURCE_PREFIX):
            return 1.0 if source[len(BUTTON_SOURCE_PREFIX) :] in self.buttons else 0.0
        return float(getattr(self, source))

    def to_json(self) -> dict:
        return {
            "power_w": round(self.power_w, 1),
            "power_ftp": round(self.power_ftp, 3),
            "power_gear": round(self.power_gear, 3),
            "cadence_rpm": round(self.cadence_rpm, 1),
            "speed_kph": round(self.speed_kph, 2),
            "paddle_l": round(self.paddle_l, 3),
            "paddle_r": round(self.paddle_r, 3),
            "buttons": sorted(self.buttons),
            "ride_battery": self.ride_battery,
            "kickr_connected": self.kickr_connected,
            "ride_connected": self.ride_connected,
            "gt7_live": self.gt7_live,
            "gt7_driving": self.gt7_driving,
            "gear": self.gear,
            "num_gears": self.num_gears,
            "gear_target_w": round(self.gear_target_w),
        }


@dataclass
class _BikeState:
    power_w: float = 0.0
    cadence_rpm: float = 0.0
    speed_kph: float = 0.0
    updated_at: float | None = None


@dataclass
class SignalHub:
    """Latest values from every source. Bike data that stops arriving goes to zero
    after `bike_stale_s`, so a dropped trainer connection can't hold the throttle."""

    bike_stale_s: float = 3.0
    ftp_w: float = 200.0
    first_gear_ftp: float = 0.5
    gt7_stale_s: float = 1.0
    # learned from the trainer: virtual speed (km/h) per rpm, for the grade calibration
    speed_per_rpm: float = 0.373
    _gt7: Gt7State | None = None
    _gt7_at: float | None = None
    clock: Callable[[], float] = time.monotonic
    _bike: _BikeState = field(default_factory=_BikeState)
    _keypad: KeypadStatus | None = None
    _battery: int | None = None
    _connected: dict[str, bool] = field(default_factory=lambda: {"kickr": False, "ride": False})
    _input_event: asyncio.Event | None = None

    # --- writers -----------------------------------------------------------------
    def update_bike(self, sample: BikeSample) -> None:
        if sample.power_w is not None:
            self._bike.power_w = max(sample.power_w, 0.0)
        if sample.cadence_rpm is not None:
            self._bike.cadence_rpm = max(sample.cadence_rpm, 0.0)
        if sample.speed_kph is not None:
            self._bike.speed_kph = max(sample.speed_kph, 0.0)
        self._bike.updated_at = self.clock()
        b = self._bike
        if b.cadence_rpm >= 50 and b.speed_kph >= 10:
            self.speed_per_rpm += 0.1 * (b.speed_kph / b.cadence_rpm - self.speed_per_rpm)

    def update_gt7(self, state: Gt7State) -> None:
        self._gt7, self._gt7_at = state, self.clock()

    def gt7_state(self) -> Gt7State | None:
        """The latest telemetry, or None if it's stale."""
        if self._gt7 is None or self._gt7_at is None or self.clock() - self._gt7_at > self.gt7_stale_s:
            return None
        return self._gt7

    def update_ride(self, status: KeypadStatus) -> None:
        self._keypad = status
        self._notify_input()

    def set_battery(self, percent: int) -> None:
        self._battery = percent

    def set_connected(self, device: str, connected: bool) -> None:
        self._connected[device] = connected
        if device == "ride" and not connected:
            self._keypad = None
            self._notify_input()

    # --- readers -----------------------------------------------------------------
    def snapshot(self) -> Snapshot:
        now = self.clock()
        bike = self._bike
        bike_fresh = bike.updated_at is not None and now - bike.updated_at <= self.bike_stale_s
        keypad = self._keypad
        power = bike.power_w if bike_fresh else 0.0
        power_ftp = power / self.ftp_w if self.ftp_w > 0 else 0.0
        gt7 = self.gt7_state()
        if gt7 is not None:
            share = gear_fraction(gt7.gear, gt7.num_gears, self.first_gear_ftp)
            target = self.ftp_w * share
            power_gear = power / target if target > 0 else 0.0
        else:
            target, power_gear = self.ftp_w, power_ftp
        return Snapshot(
            power_w=power,
            power_ftp=power_ftp,
            power_gear=power_gear,
            gt7_live=gt7 is not None,
            gt7_driving=bool(gt7 and gt7.driving),
            gear=gt7.gear if gt7 else 0,
            num_gears=gt7.num_gears if gt7 else 0,
            gear_target_w=target,
            cadence_rpm=bike.cadence_rpm if bike_fresh else 0.0,
            speed_kph=bike.speed_kph if bike_fresh else 0.0,
            paddle_l=keypad.paddle(PADDLE_LEFT) if keypad else 0.0,
            paddle_r=keypad.paddle(PADDLE_RIGHT) if keypad else 0.0,
            buttons=keypad.pressed if keypad else frozenset(),
            ride_battery=self._battery,
            kickr_connected=self._connected.get("kickr", False),
            ride_connected=self._connected.get("ride", False),
        )

    async def wait_input(self, timeout: float) -> bool:
        """Wait until a button/paddle change arrives or `timeout` passes."""
        event = self._event()
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            event.clear()

    def _event(self) -> asyncio.Event:
        if self._input_event is None:
            self._input_event = asyncio.Event()
        return self._input_event

    def _notify_input(self) -> None:
        if self._input_event is not None:
            self._input_event.set()

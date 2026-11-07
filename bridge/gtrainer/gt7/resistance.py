"""GT7 gear -> KICKR simulated grade.

Each gear has a target power (first_gear_ftp x FTP in 1st, rising evenly to FTP in
the top gear). The trainer runs in FTMS simulation mode with no air drag, so power
grows linearly with cadence, and the grade is chosen so the target power comes at
the rider's target cadence (90 rpm by default). Then: the gear sets how heavy it
feels, and spinning at the target cadence gives full throttle in any gear.

Not driving (menus, paused, loading, replays, no telemetry) -> 1st-gear grade.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..signals import SignalHub
from .gears import DEFAULT_CRR, gear_fraction, grade_for_power

log = logging.getLogger(__name__)

DEFAULT_WEIGHT_KG = 75.0
RESEND_THRESHOLD_PCT = 0.05
TICK_S = 0.25


@dataclass(frozen=True)
class ResistanceTarget:
    gear: int  # gear used for the target (1 when not driving)
    reason: str  # "driving" | "not driving" | "no telemetry"
    target_w: float
    grade_pct: float


def compute_target(
    hub: SignalHub,
    *,
    ftp_w: float,
    first_gear_ftp: float,
    target_cadence_rpm: float,
    weight_kg: float,
    crr: float = DEFAULT_CRR,
) -> ResistanceTarget:
    gt7 = hub.gt7_state()
    if gt7 is None:
        gear, num, reason = 1, 2, "no telemetry"
    elif not gt7.driving:
        gear, num, reason = 1, max(gt7.num_gears, 2), "not driving"
    else:
        gear, num, reason = max(gt7.gear, 1), gt7.num_gears, "driving"
    target_w = ftp_w * gear_fraction(gear, num, first_gear_ftp)
    ref_speed = hub.speed_per_rpm * target_cadence_rpm
    return ResistanceTarget(gear, reason, target_w, grade_for_power(target_w, ref_speed, weight_kg, crr))


class ResistanceController:
    def __init__(self, hub: SignalHub, kickr, rider_getter, crr: float = DEFAULT_CRR) -> None:
        self.hub = hub
        self.kickr = kickr
        self.rider_getter = rider_getter
        self.crr = crr
        self.target: ResistanceTarget | None = None
        self.sent_grade: float | None = None
        self._sent_epoch = -1

    def current_target(self) -> ResistanceTarget:
        rider = self.rider_getter()
        return compute_target(
            self.hub,
            ftp_w=rider.ftp_w,
            first_gear_ftp=rider.first_gear_ftp,
            target_cadence_rpm=rider.target_cadence_rpm,
            weight_kg=self.kickr.weight_kg or DEFAULT_WEIGHT_KG,
            crr=self.crr,
        )

    def to_json(self) -> dict:
        t = self.target
        return {
            "control": self.kickr.to_json().get("control"),
            "weight_kg": self.kickr.weight_kg or DEFAULT_WEIGHT_KG,
            "speed_per_rpm": round(self.hub.speed_per_rpm, 3),
            "target": None
            if t is None
            else {"gear": t.gear, "reason": t.reason, "target_w": round(t.target_w), "grade_pct": round(t.grade_pct, 2)},
            "sent_grade_pct": None if self.sent_grade is None else round(self.sent_grade, 2),
        }

    async def run(self) -> None:
        try:
            while True:
                self.target = self.current_target()
                if self.kickr.control_ready:
                    fresh_control = self.kickr.control_epoch != self._sent_epoch
                    changed = self.sent_grade is None or abs(self.target.grade_pct - self.sent_grade) >= RESEND_THRESHOLD_PCT
                    if fresh_control or changed:
                        if await self.kickr.set_simulation(self.target.grade_pct, self.crr):
                            self.sent_grade = self.target.grade_pct
                            self._sent_epoch = self.kickr.control_epoch
                            log.info(
                                "resistance: gear %d (%s) -> %.0f W at target cadence, grade %.2f%%",
                                self.target.gear, self.target.reason, self.target.target_w, self.target.grade_pct,
                            )
                await asyncio.sleep(TICK_S)
        finally:
            await self.kickr.release()

"""Gear -> target power, and target power -> simulated grade for the trainer."""

from __future__ import annotations

G = 9.81
DEFAULT_CRR = 0.004
GRADE_MIN_PCT = -5.0
GRADE_MAX_PCT = 20.0


def gear_fraction(gear: int, num_gears: int, first: float) -> float:
    """Share of FTP that means full throttle: `first` in 1st gear, rising evenly to
    1.0 in the top gear. Reverse/neutral (0) counts as 1st gear."""
    if num_gears <= 1:
        return 1.0
    g = min(max(gear, 1), num_gears)
    return first + (1.0 - first) * (g - 1) / (num_gears - 1)


def grade_for_power(target_w: float, speed_kph: float, weight_kg: float, crr: float = DEFAULT_CRR) -> float:
    """Grade (%) at which `target_w` is needed at `speed_kph`, with no air drag:
    P = v * m * g * (grade + crr). Power then rises linearly with cadence."""
    v = max(speed_kph, 1.0) / 3.6
    grade = target_w / (v * weight_kg * G) - crr
    return max(GRADE_MIN_PCT, min(grade * 100.0, GRADE_MAX_PCT))


def power_at(grade_pct: float, speed_kph: float, weight_kg: float, crr: float = DEFAULT_CRR) -> float:
    v = speed_kph / 3.6
    return v * weight_kg * G * (grade_pct / 100.0 + crr)

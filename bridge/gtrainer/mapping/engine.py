"""Turns signal snapshots into DualSense controller states according to a profile."""

from __future__ import annotations

import math

from ..output.chiaki import BUTTON_BITS, TRIGGERS, ControllerState
from ..signals import Snapshot
from .schema import AxisBinding, ButtonBinding, Profile

TAP_PULSE_S = 0.08


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def shape_axis(binding: AxisBinding, raw: float) -> float:
    """Map an (already smoothed) raw value to the output range, then apply
    deadzone + curve to its magnitude. Pure; exposed for tests and the UI."""
    lo, hi = binding.in_
    olo, ohi = binding.out if binding.out is not None else (0.0, 1.0)
    t = _clamp((raw - lo) / (hi - lo), 0.0, 1.0)
    n = olo + t * (ohi - olo)
    peak = max(abs(olo), abs(ohi)) or 1.0
    u = abs(n) / peak
    dz = binding.deadzone
    u = 0.0 if u <= dz else (u - dz) / (1.0 - dz)
    if binding.curve == "expo":
        u = u**binding.gamma
    return math.copysign(u * peak, n)


class Engine:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.reset()

    def reset(self) -> None:
        self._prev: frozenset[str] = frozenset()
        self._press_start: dict[str, float] = {}
        self._toggles: dict[str, bool] = {}
        self._pulse_until: dict[str, float] = {}
        self._ema: dict[str, float] = {}
        self._layer_toggled: dict[str, bool] = {}
        self._last_t: float | None = None
        self.active_layers: list[str] = []

    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.reset()

    def step(self, snap: Snapshot, now: float) -> ControllerState:
        pressed = snap.buttons
        newly = pressed - self._prev
        released = self._prev - pressed
        for b in newly:
            self._press_start[b] = now
        released_after: dict[str, float] = {}
        for b in released:
            start = self._press_start.pop(b, now)
            released_after[b] = now - start
        dt = 0.0 if self._last_t is None else max(now - self._last_t, 0.0)

        # Layers
        active: list[str] = []
        for name, layer in self.profile.layers.items():
            if layer.toggle is not None and layer.toggle in newly:
                self._layer_toggled[name] = not self._layer_toggled.get(name, False)
            if (layer.hold is not None and layer.hold in pressed) or self._layer_toggled.get(name, False):
                active.append(name)
        self.active_layers = active

        buttons: dict[str, tuple[str, ButtonBinding | None]] = {
            out: ("base", b) for out, b in self.profile.buttons.items()
        }
        axes: dict[str, tuple[str, AxisBinding | None]] = {out: ("base", a) for out, a in self.profile.axes.items()}
        for name in active:
            layer = self.profile.layers[name]
            buttons.update({out: (name, b) for out, b in layer.buttons.items()})
            axes.update({out: (name, a) for out, a in layer.axes.items()})

        # Buttons
        bits = 0
        for out, (scope, binding) in buttons.items():
            if binding is None:
                continue
            key = f"{scope}:{out}"
            if self._button(binding, key, snap, newly, released_after, now):
                bits |= BUTTON_BITS[out]

        # Axes
        values: dict[str, int] = {}
        for out, (scope, binding) in axes.items():
            if binding is None:
                continue
            key = f"{scope}:{out}"
            if any(c.holds(snap.value(c.source)) for c in binding.zero_when):
                self._ema[key] = 0.0
                values[out] = 0
                continue
            term_values = [t.value(snap.value(t.source)) for t in binding.terms()]
            raw = max(term_values, key=abs) if binding.mix == "max" else sum(term_values)
            release_ms = binding.smooth_ms if binding.release_ms is None else binding.release_ms
            raw = self._smooth(key, raw, binding.smooth_ms, release_ms, dt)
            v = shape_axis(binding, raw)
            if out in TRIGGERS:
                values[out] = round(_clamp(v, 0.0, 1.0) * 255)
            else:
                values[out] = round(_clamp(v, -1.0, 1.0) * 32767)

        self._prev = pressed
        self._last_t = now
        return ControllerState(buttons=bits, **values)

    def _button(
        self,
        b: ButtonBinding,
        key: str,
        snap: Snapshot,
        newly: frozenset[str],
        released_after: dict[str, float],
        now: float,
    ) -> bool:
        pressed = snap.buttons
        kind = b.kind
        if kind == "button":
            return b.button in pressed
        if kind == "any":
            return any(x in pressed for x in b.any or ())
        if kind == "combo":
            return all(x in pressed for x in b.combo or ())
        if kind == "hold":
            start = self._press_start.get(b.hold or "")
            return b.hold in pressed and start is not None and now - start >= b.ms / 1000.0
        if kind == "tap":
            held = released_after.get(b.tap or "")
            if held is not None and held < b.ms / 1000.0:
                self._pulse_until[key] = now + TAP_PULSE_S
            return now < self._pulse_until.get(key, 0.0)
        if kind == "toggle":
            if b.toggle in newly:
                self._toggles[key] = not self._toggles.get(key, False)
            return self._toggles.get(key, False)
        if kind == "signal":
            v = snap.value(b.signal or "")
            return (b.above is None or v > b.above) and (b.below is None or v < b.below)
        return False

    def _smooth(self, key: str, raw: float, smooth_ms: float, release_ms: float, dt: float) -> float:
        prev = self._ema.get(key)
        if prev is None:
            self._ema[key] = raw
            return raw
        # falling back toward zero uses the release time constant
        tau_ms = release_ms if abs(raw) < abs(prev) else smooth_ms
        if tau_ms <= 0:
            value = raw
        else:
            alpha = 1.0 - math.exp(-dt / (tau_ms / 1000.0)) if dt > 0 else 0.0
            value = prev + alpha * (raw - prev)
        self._ema[key] = value
        return value

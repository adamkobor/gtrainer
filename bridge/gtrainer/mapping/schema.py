"""Profile model: how bike/Ride signals map onto DualSense buttons and axes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..decode.zwift_ride import RIDE_BUTTONS
from ..output.chiaki import PS_AXES, PS_BUTTONS
from ..signals import ANALOG_SOURCES, BUTTON_SOURCE_PREFIX, is_valid_source


def _check_ride_button(name: str) -> str:
    if name not in RIDE_BUTTONS:
        raise ValueError(f"unknown Ride button {name!r} (valid: {', '.join(RIDE_BUTTONS)})")
    return name


def _check_source(name: str) -> str:
    if not is_valid_source(name):
        raise ValueError(
            f"unknown source {name!r} (valid: {', '.join(ANALOG_SOURCES)} or {BUTTON_SOURCE_PREFIX}<RideButton>)"
        )
    return name


class Term(BaseModel):
    """One input of a combined axis: clip(source, min, max) * scale.
    E.g. {source: paddle_r, min: 0} uses only the paddle's positive swing."""

    model_config = ConfigDict(extra="forbid")
    source: str
    scale: float = 1.0
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _range(self) -> Term:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("'min' must not be greater than 'max'")
        return self

    def value(self, raw: float) -> float:
        if self.min is not None and raw < self.min:
            raw = self.min
        if self.max is not None and raw > self.max:
            raw = self.max
        return raw * self.scale

    @field_validator("source")
    @classmethod
    def _v_source(cls, v: str) -> str:
        return _check_source(v)


class Condition(BaseModel):
    """True while `source` is above and/or below a threshold."""

    model_config = ConfigDict(extra="forbid")
    source: str
    above: float | None = None
    below: float | None = None

    @field_validator("source")
    @classmethod
    def _v_source(cls, v: str) -> str:
        return _check_source(v)

    @model_validator(mode="after")
    def _threshold(self) -> Condition:
        if self.above is None and self.below is None:
            raise ValueError("a condition needs 'above' and/or 'below'")
        return self

    def holds(self, value: float) -> bool:
        return (self.above is None or value > self.above) and (self.below is None or value < self.below)


class AxisBinding(BaseModel):
    """value = combine terms (sum, or `mix: max` = the term with the largest magnitude)
    -> EMA smoothing -> map `in` range onto `out` range (clamped) -> deadzone and
    curve applied to the magnitude, sign preserved."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str | None = None
    combine: list[Term] | None = None
    mix: Literal["sum", "max"] = "sum"
    in_: tuple[float, float] = Field(default=(0.0, 1.0), alias="in")
    out: tuple[float, float] | None = None  # default (0, 1)
    curve: Literal["linear", "expo"] = "linear"
    gamma: float = Field(default=2.0, gt=0)
    deadzone: float = Field(default=0.0, ge=0, lt=1)
    smooth_ms: float = Field(default=0.0, ge=0)
    # smoothing while the value falls back toward zero; None = same as smooth_ms
    release_ms: float | None = Field(default=None, ge=0)
    # output is forced to zero (and smoothing reset) while any condition holds
    zero_when: list[Condition] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"source": data}
        return data

    @field_validator("source")
    @classmethod
    def _v_source(cls, v: str | None) -> str | None:
        return None if v is None else _check_source(v)

    @model_validator(mode="after")
    def _exactly_one(self) -> AxisBinding:
        if (self.source is None) == (self.combine is None):
            raise ValueError("set exactly one of 'source' or 'combine'")
        if self.combine is not None and not self.combine:
            raise ValueError("'combine' must not be empty")
        if self.in_[0] == self.in_[1]:
            raise ValueError("'in' range must not be empty")
        return self

    def terms(self) -> list[Term]:
        return self.combine if self.combine is not None else [Term(source=self.source or "")]

    def ride_buttons_used(self) -> set[str]:
        sources = [t.source for t in self.terms()] + [c.source for c in self.zero_when]
        return {s[len(BUTTON_SOURCE_PREFIX) :] for s in sources if s.startswith(BUTTON_SOURCE_PREFIX)}


_TRIGGER_KINDS = ("button", "any", "combo", "hold", "tap", "toggle", "signal")


class ButtonBinding(BaseModel):
    """Exactly one trigger kind:
    button: X            pressed while X is held (shorthand: just "X")
    any: [X, Y]          pressed while any is held (shorthand: a list)
    combo: [X, Y]        pressed while all are held
    hold: X, ms          pressed once X has been held for `ms`
    tap: X, ms           short pulse when X is released within `ms`
    toggle: X            each press of X flips the output on/off
    signal: s, above/below  pressed while an analog signal is past a threshold
    """

    model_config = ConfigDict(extra="forbid")

    button: str | None = None
    any: list[str] | None = None
    combo: list[str] | None = None
    hold: str | None = None
    tap: str | None = None
    toggle: str | None = None
    signal: str | None = None
    ms: int = Field(default=500, ge=0)
    above: float | None = None
    below: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"button": data}
        if isinstance(data, list):
            return {"any": data}
        return data

    @model_validator(mode="after")
    def _validate(self) -> ButtonBinding:
        kinds = [k for k in _TRIGGER_KINDS if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError(f"set exactly one of {', '.join(_TRIGGER_KINDS)} (got {kinds or 'none'})")
        for name in self.ride_buttons_used():
            _check_ride_button(name)
        if self.signal is not None:
            _check_source(self.signal)
            if self.above is None and self.below is None:
                raise ValueError("'signal' needs 'above' and/or 'below'")
        if self.combo is not None and len(self.combo) < 2:
            raise ValueError("'combo' needs at least two buttons")
        if self.any is not None and not self.any:
            raise ValueError("'any' must not be empty")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in _TRIGGER_KINDS if getattr(self, k) is not None)

    def ride_buttons_used(self) -> set[str]:
        used: set[str] = set()
        for k in ("button", "hold", "tap", "toggle"):
            v = getattr(self, k)
            if v is not None:
                used.add(v)
        for k in ("any", "combo"):
            v = getattr(self, k)
            if v:
                used.update(v)
        if self.signal and self.signal.startswith(BUTTON_SOURCE_PREFIX):
            used.add(self.signal[len(BUTTON_SOURCE_PREFIX) :])
        return used


def _check_keys(mapping: dict[str, Any], valid: tuple[str, ...], what: str) -> None:
    for key in mapping:
        if key not in valid:
            raise ValueError(f"unknown {what} {key!r} (valid: {', '.join(valid)})")


class Layer(BaseModel):
    """While active, its bindings replace the base ones for the same outputs.
    A binding set to null disables that output while the layer is active."""

    model_config = ConfigDict(extra="forbid")

    hold: str | None = None
    toggle: str | None = None
    buttons: dict[str, ButtonBinding | None] = Field(default_factory=dict)
    axes: dict[str, AxisBinding | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> Layer:
        if (self.hold is None) == (self.toggle is None):
            raise ValueError("a layer needs exactly one of 'hold' or 'toggle'")
        _check_ride_button(self.activator)
        _check_keys(self.buttons, PS_BUTTONS, "PS button")
        _check_keys(self.axes, PS_AXES, "PS axis")
        return self

    @property
    def activator(self) -> str:
        return self.hold or self.toggle or ""


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    axes: dict[str, AxisBinding] = Field(default_factory=dict)
    buttons: dict[str, ButtonBinding] = Field(default_factory=dict)
    layers: dict[str, Layer] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> Profile:
        _check_keys(self.buttons, PS_BUTTONS, "PS button")
        _check_keys(self.axes, PS_AXES, "PS axis")
        activators = {layer.activator: name for name, layer in self.layers.items()}
        used: dict[str, str] = {}
        for out, b in self.buttons.items():
            for rb in b.ride_buttons_used():
                used.setdefault(rb, f"buttons.{out}")
        for out, a in self.axes.items():
            for rb in a.ride_buttons_used():
                used.setdefault(rb, f"axes.{out}")
        for lname, layer in self.layers.items():
            for out, b in layer.buttons.items():
                for rb in b.ride_buttons_used() if b else ():
                    used.setdefault(rb, f"layers.{lname}.buttons.{out}")
            for out, a in layer.axes.items():
                for rb in a.ride_buttons_used() if a else ():
                    used.setdefault(rb, f"layers.{lname}.axes.{out}")
        for rb, lname in activators.items():
            if rb in used:
                raise ValueError(f"{rb} activates layer {lname!r} and cannot also be used in {used[rb]}")
        return self

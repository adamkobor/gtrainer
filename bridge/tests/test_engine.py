import pytest

from contrainer.mapping.engine import TAP_PULSE_S, Engine, shape_axis
from contrainer.mapping.schema import AxisBinding, Profile
from contrainer.output.chiaki import BUTTON_BITS
from contrainer.signals import Snapshot


def profile(**doc) -> Profile:
    return Profile.model_validate({"name": "t", **doc})


def snap(buttons=(), **kw) -> Snapshot:
    return Snapshot(buttons=frozenset(buttons), **kw)


def pressed(state) -> set[str]:
    return set(state.pressed())


def test_shape_axis_linear_range_and_clamp():
    b = AxisBinding.model_validate({"source": "power_w", "in": [100, 300]})
    assert shape_axis(b, 50) == 0
    assert shape_axis(b, 200) == pytest.approx(0.5)
    assert shape_axis(b, 999) == 1


def test_shape_axis_signed_deadzone_and_expo():
    b = AxisBinding.model_validate(
        {"source": "paddle_l", "in": [-1, 1], "out": [-1, 1], "deadzone": 0.1, "curve": "expo", "gamma": 2}
    )
    assert shape_axis(b, 0.05) == 0
    assert shape_axis(b, -0.05) == 0
    assert shape_axis(b, 1) == pytest.approx(1)
    assert shape_axis(b, -1) == pytest.approx(-1)
    assert shape_axis(b, 0.55) == pytest.approx(0.25)  # (0.55-0.1)/0.9 = 0.5, squared
    assert shape_axis(b, -0.55) == pytest.approx(-0.25)


def test_inverted_out_range_for_forward_stick():
    b = AxisBinding.model_validate({"source": "cadence_rpm", "in": [20, 100], "out": [0, -1]})
    assert shape_axis(b, 20) == 0
    assert shape_axis(b, 60) == pytest.approx(-0.5)
    assert shape_axis(b, 100) == -1


def test_power_to_r2_and_steering_combine():
    e = Engine(
        profile(
            axes={
                "r2": {"source": "power_w", "in": [0, 400]},
                "left_x": {
                    "combine": [{"source": "paddle_l", "scale": -1}, {"source": "paddle_r"}],
                    "in": [-1, 1],
                    "out": [-1, 1],
                },
            }
        )
    )
    s = e.step(snap(power_w=200, paddle_l=1.0), 0.0)
    assert s.r2 == 128
    assert s.left_x == -32767
    s = e.step(snap(power_w=800, paddle_r=0.5), 0.01)
    assert s.r2 == 255
    assert s.left_x == round(0.5 * 32767)


def test_button_source_axis_partial_brake():
    e = Engine(profile(axes={"l2": {"combine": [{"source": "btn:SHIFT_UP_L", "scale": 0.4}, {"source": "btn:SHIFT_DN_L"}]}}))
    assert e.step(snap(["SHIFT_UP_L"]), 0).l2 == round(0.4 * 255)
    assert e.step(snap(["SHIFT_UP_L", "SHIFT_DN_L"]), 0.01).l2 == 255
    assert e.step(snap(), 0.02).l2 == 0


def test_direct_any_combo():
    e = Engine(profile(buttons={"cross": "A", "circle": ["B", "Z"], "ps": {"combo": ["ONOFF_L", "ONOFF_R"]}}))
    assert pressed(e.step(snap(["A"]), 0)) == {"cross"}
    assert pressed(e.step(snap(["Z"]), 0.01)) == {"circle"}
    assert pressed(e.step(snap(["ONOFF_L"]), 0.02)) == set()
    assert pressed(e.step(snap(["ONOFF_L", "ONOFF_R"]), 0.03)) == {"ps"}


def test_tap_and_hold_on_same_button():
    e = Engine(profile(buttons={"options": {"tap": "ONOFF_R", "ms": 400}, "touchpad": {"hold": "ONOFF_R", "ms": 800}}))
    # short press -> options pulse after release, no touchpad
    e.step(snap(["ONOFF_R"]), 0.0)
    assert pressed(e.step(snap(["ONOFF_R"]), 0.2)) == set()
    assert pressed(e.step(snap(), 0.3)) == {"options"}
    assert pressed(e.step(snap(), 0.3 + TAP_PULSE_S / 2)) == {"options"}
    assert pressed(e.step(snap(), 0.3 + TAP_PULSE_S + 0.01)) == set()
    # long press -> touchpad while held, no options on release
    e.step(snap(["ONOFF_R"]), 1.0)
    assert pressed(e.step(snap(["ONOFF_R"]), 1.5)) == set()
    assert pressed(e.step(snap(["ONOFF_R"]), 1.85)) == {"touchpad"}
    assert pressed(e.step(snap(), 2.0)) == set()
    assert pressed(e.step(snap(), 2.02)) == set()


def test_toggle_output():
    e = Engine(profile(buttons={"l3": {"toggle": "Y"}}))
    assert pressed(e.step(snap(["Y"]), 0)) == {"l3"}
    assert pressed(e.step(snap(), 0.1)) == {"l3"}
    assert pressed(e.step(snap(["Y"]), 0.2)) == set()
    assert pressed(e.step(snap(), 0.3)) == set()


def test_signal_threshold():
    e = Engine(profile(buttons={"l3": {"signal": "cadence_rpm", "above": 100}}))
    assert pressed(e.step(snap(cadence_rpm=99), 0)) == set()
    assert pressed(e.step(snap(cadence_rpm=101), 0.1)) == {"l3"}


def test_layers_toggle_and_hold_override_and_disable():
    e = Engine(
        profile(
            buttons={"cross": "A"},
            axes={"right_x": {"combine": [{"source": "btn:LEFT", "scale": -1}, {"source": "btn:RIGHT"}], "in": [-1, 1], "out": [-1, 1]}},
            layers={
                "nav": {"toggle": "POWERUP_L", "buttons": {"dpad_left": "LEFT"}, "axes": {"right_x": None}},
                "alt": {"hold": "POWERUP_R", "buttons": {"cross": None, "square": "A"}},
            },
        )
    )
    s = e.step(snap(["LEFT"]), 0)
    assert s.right_x == -32767 and pressed(s) == set()
    e.step(snap(["POWERUP_L"]), 0.1)  # toggle nav on
    assert e.active_layers == ["nav"]
    s = e.step(snap(["LEFT"]), 0.2)
    assert s.right_x == 0 and pressed(s) == {"dpad_left"}
    # hold layer replaces cross with square while held
    s = e.step(snap(["POWERUP_R", "A"]), 0.3)
    assert pressed(s) == {"square"} and e.active_layers == ["nav", "alt"]
    s = e.step(snap(["A"]), 0.4)
    assert pressed(s) == {"cross"}
    e.step(snap(["POWERUP_L"]), 0.5)  # toggle nav off
    assert e.active_layers == []


def test_smoothing_converges():
    e = Engine(profile(axes={"r2": {"source": "power_w", "in": [0, 100], "smooth_ms": 100}}))
    assert e.step(snap(power_w=0), 0.0).r2 == 0
    first = e.step(snap(power_w=100), 0.05).r2
    assert 0 < first < 255
    t, last = 0.05, first
    for _ in range(100):
        t += 0.01
        last = e.step(snap(power_w=100), t).r2
    assert last == 255


def test_profile_switch_resets_state():
    e = Engine(profile(buttons={"l3": {"toggle": "Y"}}))
    e.step(snap(["Y"]), 0)
    e.set_profile(profile(buttons={"l3": {"toggle": "Y"}}))
    assert pressed(e.step(snap(), 0.1)) == set()


def test_all_button_bits_distinct():
    assert len(set(BUTTON_BITS.values())) == 16


def test_term_clipping():
    from contrainer.mapping.schema import Term

    assert Term(source="paddle_r", min=0).value(-0.7) == 0
    assert Term(source="paddle_r", min=0).value(0.7) == 0.7
    assert Term(source="paddle_l", max=0, scale=-1).value(-0.4) == 0.4
    assert Term(source="paddle_l", max=0, scale=-1).value(0.4) == 0
    with pytest.raises(ValueError):
        Term(source="paddle_l", min=1, max=0)


def test_mix_max_takes_largest_magnitude():
    e = Engine(profile(axes={"l2": {"mix": "max", "combine": [{"source": "paddle_l", "scale": -1}, {"source": "paddle_r", "scale": -1}]}}))
    assert e.step(snap(paddle_l=-0.6, paddle_r=-0.6), 0).l2 == round(0.6 * 255)
    e2 = Engine(profile(axes={"l2": {"combine": [{"source": "paddle_l", "scale": -1}, {"source": "paddle_r", "scale": -1}]}}))
    assert e2.step(snap(paddle_l=-0.6, paddle_r=-0.6), 0).l2 == 255  # default sum, clamped

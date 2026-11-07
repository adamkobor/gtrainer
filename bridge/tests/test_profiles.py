import shutil
from pathlib import Path

import pytest

from gtrainer.mapping.profiles import ProfileError, ProfileStore, merge_profiles

SHIPPED = Path(__file__).resolve().parent.parent / "profiles"


@pytest.fixture
def store(tmp_path) -> ProfileStore:
    for p in SHIPPED.glob("*.yaml"):
        shutil.copy(p, tmp_path / p.name)
    return ProfileStore(tmp_path)


@pytest.mark.parametrize("name", sorted(p.stem for p in SHIPPED.glob("*.yaml")))
def test_shipped_profiles_validate(name):
    ProfileStore(SHIPPED).load(name)


def test_gt7_inherits_menu_and_overrides(store):
    gt7 = store.load("gt7")
    assert gt7.name == "GT7"
    assert gt7.buttons["dpad_up"].button == "UP"  # inherited
    assert gt7.buttons["cross"].any == ["A", "SHIFT_UP_L", "SHIFT_UP_R"]  # overridden
    assert gt7.buttons["l1"].button == "B"  # overridden: look back
    assert "l3" not in gt7.buttons
    assert gt7.axes["r2"].source == "power_gear"  # overridden
    assert [t.source for t in gt7.axes["l2"].combine] == ["paddle_l", "paddle_r"]


def test_walking_layer(store):
    w = store.load("walking")
    assert w.layers["nav"].toggle == "POWERUP_L"
    assert w.layers["nav"].axes["right_y"] is None
    assert [t.source for t in w.layers["nav"].axes["right_x"].terms()] == ["paddle_r"]
    assert "dpad_up" not in w.buttons


def test_merge_semantics():
    base = {"name": "B", "buttons": {"cross": "A", "l1": "Z"}, "axes": {"l2": {"source": "btn:A"}}}
    child = {"buttons": {"l1": None, "r1": "B"}, "axes": {"l2": {"combine": [{"source": "btn:B"}]}}, "description": "d"}
    assert merge_profiles(base, child) == {
        "name": "B",
        "buttons": {"cross": "A", "r1": "B"},
        "axes": {"l2": {"combine": [{"source": "btn:B"}]}},  # replaced whole, not merged
        "description": "d",
    }


def test_invalid_save_is_rejected_and_file_untouched(store):
    before = store.read_text("gt7")
    with pytest.raises(ProfileError, match="unknown Ride button"):
        store.save_text("gt7", "name: X\nbuttons:\n  cross: NOPE\n")
    with pytest.raises(ProfileError, match="unknown PS button"):
        store.save_text("gt7", "name: X\nbuttons:\n  jump: A\n")
    with pytest.raises(ProfileError, match="unknown source"):
        store.save_text("gt7", "name: X\naxes:\n  r2: watts\n")
    with pytest.raises(ProfileError, match="invalid YAML"):
        store.save_text("gt7", "name: [unclosed\n")
    assert store.read_text("gt7") == before


def test_layer_activator_conflict(store):
    text = "name: X\nbuttons:\n  cross: POWERUP_L\nlayers:\n  nav:\n    toggle: POWERUP_L\n"
    with pytest.raises(ProfileError, match="activates layer"):
        store.save_text("x", text)


def test_extends_cycle_and_missing_parent(store):
    (store.dir / "a.yaml").write_text("name: A\nextends: b\n")
    (store.dir / "b.yaml").write_text("name: B\nextends: a\n")
    with pytest.raises(ProfileError, match="cycle"):
        store.load("a")
    with pytest.raises(ProfileError, match="not found"):
        store.save_text("c", "name: C\nextends: missing\n")


def test_bad_names_rejected(store):
    for bad in ("../etc", "Upper", "", "a/b"):
        with pytest.raises(ProfileError):
            store.path(bad)


def test_save_doc_roundtrip(store):
    doc = store.read_doc("menu")
    doc["buttons"]["dpad_up"] = "B"
    store.save_doc("menu", doc)
    assert store.load("menu").buttons["dpad_up"].button == "B"
    assert store.load("gt7").buttons["dpad_up"].button == "B"  # child sees parent change


def test_gt7_throttle_is_percent_of_gear_target(store):
    from gtrainer.mapping.engine import Engine
    from gtrainer.signals import Snapshot

    e = Engine(store.load("gt7"))
    # step far apart in time so the 400 ms smoothing has settled
    assert e.step(Snapshot(power_gear=0.5), 0.0).r2 == 128
    assert e.step(Snapshot(power_gear=1.0), 10.0).r2 == 255
    assert e.step(Snapshot(power_gear=1.3), 20.0).r2 == 255
    assert e.step(Snapshot(power_gear=0.0), 30.0).r2 == 0


def test_gt7_steering_shifting_and_brake(store):
    from gtrainer.mapping.engine import Engine
    from gtrainer.signals import Snapshot

    e = Engine(store.load("gt7"))
    t = [0.0]

    def step(**kw):
        t[0] += 10.0  # far apart: smoothing settled
        if "buttons" in kw:
            kw["buttons"] = frozenset(kw["buttons"])
        return e.step(Snapshot(**kw), t[0])

    # measured signs: right paddle outer = +, inner = -; left paddle outer = -, inner = +
    assert step(paddle_r=1.0).left_x == 32767  # right outer -> steer right
    assert step(paddle_l=-1.0).left_x == -32767  # left outer -> steer left
    s = step(paddle_r=-1.0)  # right inner -> brake
    assert s.left_x == 0 and s.l2 == 255
    s = step(paddle_l=1.0)  # left inner -> brake
    assert s.left_x == 0 and s.l2 == 255
    assert step(paddle_l=0.5).l2 == 128
    assert step(paddle_l=0.5, paddle_r=-0.3).l2 == 128  # max, not sum
    s = step(paddle_r=1.0, paddle_l=0.5)  # steer right while braking with the left
    assert s.left_x == 32767 and s.l2 == 128
    # buttons (GT7 default layout): shift direction depends on the button, not the side
    for up in ("SHIFT_UP_L", "SHIFT_UP_R"):
        assert set(step(buttons={up}).pressed()) == {"cross"}
    for down in ("SHIFT_DN_L", "SHIFT_DN_R"):
        s = step(buttons={down})
        assert set(s.pressed()) == {"square"} and s.l2 == 0  # no brake on shifters
    s = step(buttons={"SHIFT_DN_L"}, paddle_r=1.0)  # downshift with the left hand while steering right
    assert set(s.pressed()) == {"square"} and s.left_x == 32767
    assert set(step(buttons={"B"}).pressed()) == {"l1"}  # look back
    assert set(step(buttons={"Y"}).pressed()) == {"r1"}  # change view
    assert set(step(buttons={"A"}).pressed()) == {"cross"}
    assert set(step(buttons={"POWERUP_L"}).pressed()) == {"r3"}  # nitrous / overtake
    assert set(step(buttons={"Z"}).pressed()) == {"circle"}
    assert set(step(buttons={"POWERUP_R"}).pressed()) == {"options"}
    # tap ONOFF_R -> triangle (short pulse after release), hold -> touchpad
    step(buttons={"ONOFF_R"})
    t[0] += 0.2 - 10.0  # released 200 ms later (step() adds 10 s)
    assert set(step().pressed()) == {"triangle"}

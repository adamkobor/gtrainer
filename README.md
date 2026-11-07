# contrainer

Use a **Wahoo KICKR CORE 2** and a **Zwift Ride** handlebar as a **PS5 controller**.

```
KICKR ──BLE──┐
             ├─► bridge (Python, this Mac) ──UDP 127.0.0.1:47800──► chiaki-ng (patched) ──Remote Play──► PS5 ──► TV
Zwift Ride ──┘     mapping profiles + web UI
```

PS5 games only accept authenticated controllers, so the input goes in through
Remote Play (the open-source [chiaki-ng](https://github.com/streetpea/chiaki-ng) client)
with a small patch that adds a local UDP input source. The PS5 keeps showing the
game on the TV; the chiaki-ng window can stay minimized.

> **Zwift Ride firmware:** only ≤ 1.2.x works without Zwift's daily "unlock".
> Don't accept Ride firmware updates in the Zwift Companion app.

## Setup

1. **chiaki-ng with the patch** (one-time, about 20–30 minutes):
   ```sh
   chiaki/build.sh --deps     # installs Homebrew deps (Qt 6, ffmpeg, …), clones, patches, builds
   open chiaki/src/build/gui/chiaki.app
   ```
   In chiaki-ng, register your PS5 once (PSN account ID plus the PIN from
   PS5 → Settings → System → Remote Play → Pair Device). Enable Remote Play on
   the PS5, and use Ethernet for the PS5 if you can.
2. **Bridge:**
   ```sh
   cd bridge
   uv sync
   uv run contrainer --sim --open     # try it without hardware first
   ```
   macOS will ask for Bluetooth permission for your terminal on first real run.

## Daily use

1. Wake the trainer and the Ride. Close Zwift and any other app that might be connected to them.
2. Start chiaki-ng and connect to the PS5.
3. `cd bridge && uv run contrainer --profile gt7 --open` (or `walking`, `menu`).
4. Watch the web UI at http://127.0.0.1:8765: live meters, PS5 output preview,
   profile switching and editing. Run it under `caffeinate -i` to keep the Mac awake.

If the bridge stops, chiaki-ng releases all inputs within 250 ms.

## First run on real hardware: probe

The Ride protocol is community reverse-engineered. Check it on your unit first:

```sh
cd bridge
uv run python tools/probe.py scan
uv run python tools/probe.py ride     # press every button and both paddles
uv run python tools/probe.py kickr    # pedal
```

Raw frames are saved to `bridge/captures/`. If every button shows as pressed and
releasing makes them "un-press", run the bridge with `--ride-set-bit-pressed`.

To test chiaki-ng without any bike hardware: `uv run python tools/send_test_state.py`.

## Rider settings

Set your **FTP** in the web UI header (or `--ftp 260`). It's stored in `bridge/rider.yaml` and
shared by all profiles through the `power_ftp` signal.

## Profiles (`bridge/profiles/*.yaml`)

Profiles reload live when saved, whether from the web UI or a text editor. An
invalid profile is rejected and the last valid one stays active.

```yaml
name: GT7
extends: menu                  # inherit; entries here replace the parent's, null removes one
axes:                          # l2 r2 left_x left_y right_x right_y
  r2: {source: power_ftp, in: [0, 1], smooth_ms: 400}   # 100% FTP = full throttle
  left_x:
    combine: [{source: paddle_l, scale: -1}, {source: paddle_r}]
    in: [-1, 1]
    out: [-1, 1]               # sticks: -1..1 (y: negative = forward); triggers: 0..1
    deadzone: 0.08
  l2:
    mix: max                   # combine as the largest input (default: sum)
    combine: [{source: paddle_l, max: 0, scale: -1}, {source: paddle_r, max: 0, scale: -1}]   # min/max clip a term
buttons:                       # cross circle square triangle dpad_* l1 r1 l3 r3 options create touchpad ps
  cross: A                     # while held
  circle: [B, Z]               # any of
  ps: {combo: [ONOFF_L, ONOFF_R]}
  options: {tap: ONOFF_R, ms: 400}
  touchpad: {hold: ONOFF_R, ms: 800}
  l3: {toggle: Y}
  r3: {signal: cadence_rpm, above: 100}
layers:                        # override bindings while active
  nav: {toggle: POWERUP_L, buttons: {dpad_up: UP}, axes: {right_y: null}}
```

- **Sources:** `power_w`, `power_ftp` (power ÷ your FTP; 1.0 = at FTP), `cadence_rpm`, `speed_kph`, `paddle_l`, `paddle_r` (−1..1; each paddle swings both ways), and `btn:<RideButton>` (0/1).
- **Ride buttons:** `LEFT UP RIGHT DOWN A B Y Z SHIFT_UP_L SHIFT_DN_L SHIFT_UP_R SHIFT_DN_R POWERUP_L POWERUP_R ONOFF_L ONOFF_R`.

## Development

```sh
cd bridge && uv run pytest
```

The chiaki-ng change lives in `chiaki/external-input.patch`, pinned to an upstream commit in `chiaki/build.sh`.

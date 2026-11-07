# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

gtrainer turns a smart trainer (Wahoo KICKR, Bluetooth FTMS) and a Zwift Ride handlebar into a PS5 controller. A Python bridge reads both devices over BLE, maps them to a DualSense state through YAML profiles, and sends that state over UDP to a **patched chiaki-ng** (PlayStation Remote Play client), which forwards it to the PS5. Optional: GT7 telemetry drives gear-relative throttle and trainer resistance. A home-made USB/BT gamepad can't work because PS5 games require authenticated controllers, so Remote Play is the only input path.

## Commands

All Python work happens in `bridge/` with uv (Python 3.13 pinned in `bridge/.python-version`).

```sh
cd bridge
uv sync                                   # install deps (incl. dev group: pytest, httpx)
uv run pytest                             # all tests
uv run pytest tests/test_engine.py -k tap # single file / single test
uv run gtrainer --sim --open              # run without hardware (synthetic trainer, clickable Ride in the web UI)
uv run gtrainer --profile gt7 --gt7 -v    # real devices + GT7 telemetry/resistance
uv run python tools/probe.py scan|ride|kickr   # read-only BLE probes (raw frames go to bridge/captures/, gitignored)
uv run python tools/gt7_probe.py [--ps5 IP]    # read-only GT7 telemetry check
uv run python tools/send_test_state.py         # drive the PS5 D-pad via chiaki without bike hardware
```

chiaki-ng: `chiaki/build.sh [--deps]` clones streetpea/chiaki-ng into `chiaki/src/` (gitignored), checks out `PINNED_COMMIT`, applies `chiaki/external-input.patch`, builds with cmake/ninja and code-signs the app. There is no linter configured.

## Architecture

Data flow (one asyncio process, tasks created in `gtrainer/__main__.py`):

```
ble/kickr.py ─┐                      ┌─ gt7/telemetry.py (UDP 33739/33740, Salsa20)
ble/ride.py  ─┴─► signals.SignalHub ◄┘
                      │ snapshot() every 10 ms (or immediately on a Ride input event)
                      ▼
              mapping/engine.Engine.step(snapshot, now) ──► output/chiaki.ChiakiSender ──► UDP 127.0.0.1:47800
gt7/resistance.ResistanceController (4 Hz) ── hub.gt7_state() ──► KickrLink.set_simulation (FTMS control point)
web/app.py (FastAPI + /ws push every 50 ms) ── reads/writes Runtime
```

- **`runtime.Runtime`** is the hub object the web app and CLI share: active profile + `Engine`, rider settings, links, telemetry, resistance, sim state. `run_engine` is the 100 Hz loop; it sends `IDLE` on exit.
- **`signals.SignalHub` / `Snapshot`**: the only interface between inputs and the mapping engine. Every analog source a profile may reference is a `Snapshot` field listed in `ANALOG_SOURCES`; Ride buttons are `btn:<NAME>` sources. Adding a new signal means: a `Snapshot` field, an `ANALOG_SOURCES` entry (schema validation and the web UI pick it up from there), and computing it in `snapshot()`. Bike data goes to 0 after 3 s without updates; GT7 state is stale after 1 s.
- **Derived throttle signals**: `power_ftp = power / FTP`; `power_gear = power / (FTP × gear_fraction(gear))`, falling back to `power_ftp` without live telemetry. `gt7/gears.py` holds the gear→share and power→grade math; `resistance.compute_target` picks the grade (1st-gear grade when not driving / no telemetry). `speed_per_rpm` is learned from the trainer by EMA in `update_bike`.
- **Mapping** (`mapping/`):
  - `schema.py`: pydantic models. `AxisBinding` pipeline is terms (`source` or `combine`, each clipped by `min`/`max` and scaled) → `mix` (sum/max) → EMA (`smooth_ms` rising, `release_ms` falling) → `in`→`out` range → deadzone/curve on magnitude. `zero_when` forces 0 and resets the EMA. `ButtonBinding` has exactly one kind (button/any/combo/hold/tap/toggle/signal); string and list shorthands are expanded in `_shorthand`. A layer's activator button may not be used by any other binding.
  - `profiles.py`: `ProfileStore` with `extends:` inheritance. `merge_profiles` merges `buttons`/`axes`/`layers` **per entry, replacing whole bindings** (a child axis never deep-merges with the parent's); `null` removes an entry. Saves validate the resolved document first, then write atomically.
  - `engine.py`: stateful per profile (press timestamps, toggles, EMA, layer toggles); state is keyed `"<scope>:<output>"` so layer bindings have their own state. `set_profile` resets everything.
- **Hot reload**: `Runtime.watch_profiles` reloads the active profile on any change in the profiles dir; an invalid file keeps the last valid profile and surfaces `rt.error` in the UI. Schema changes need a bridge restart (otherwise reloads fail validation).
- **BLE** (`ble/`): `DeviceLink.run` is the scan → connect → wait-for-disconnect → retry loop; scans are serialized by a module-level lock (CoreBluetooth dislikes parallel scans). Devices are matched by name substring. The KICKR also exposes the Zwift `fc82` service, so `RideLink` must never match names containing "kickr". KICKR control (FTMS control point) is opt-in (`control_enabled`, i.e. `--gt7` without `--no-resistance`); `_command` correlates indications by request opcode under a lock; `control_epoch` bumps on every granted control so the resistance loop resends.
- **Decoders** (`decode/`) are pure and tested against real captured frames: a minimal protobuf reader for the Ride, FTMS/CPS parsers and FTMS control-point builders.
- **chiaki patch**: adds `ExternalInput` (QUdpSocket on 127.0.0.1, env `CHIAKI_EXTERNAL_INPUT_PORT`, default 47800, 0 disables) and ORs its state into `StreamSession::SendFeedbackState`/`DpadSendFeedbackState` via `chiaki_controller_state_or` (buttons OR'd, larger trigger, larger-magnitude stick). Drops non-increasing sequence numbers; idles after 250 ms without packets.

## Protocol facts that are easy to get wrong

- UDP packet to chiaki (23 bytes, LE): `<4sBIIBBhhhh` = `"CTRN" | u8 version=1 | u32 seq | u32 buttons | u8 l2 | u8 r2 | i16 lx ly rx ry`. Button bits mirror chiaki's `controller.h` (`BUTTON_BITS` in `output/chiaki.py`); stick y negative = up/forward. Python and C++ sides must change together.
- Zwift Ride (fw ≤ 1.2 only; 1.3+ is locked to Zwift): handshake writes `b"RideOn"`; opcode `0x23` button map uses **cleared bit = pressed**; paddles are sint32 −100..100. Measured signs: right paddle outer +/inner −, left paddle outer −/inner +.
- KICKR CORE reports power/cadence/speed at ~1 Hz and cadence lags ~3 s; this is the floor for throttle latency.
- GT7 telemetry: heartbeat `b"A"` to PS5:33739, packets arrive on local UDP 33740; field offsets live in `gt7/telemetry.py`.

## Conventions and gotchas

- macOS Local Network privacy is per app: chiaki must be code-signed (build.sh does it) and the terminal/IDE running the bridge must be allowed, or PS5 discovery/telemetry silently fails.
- `bridge/rider.yaml` (FTP etc., written by the web UI) and `bridge/captures/` (contain device serials) are gitignored; keep it that way.
- Profiles saved through the web UI's form are re-dumped by `yaml.safe_dump`, which drops comments.
- Moving the repo breaks `chiaki/src/build` (delete it and rerun build.sh) and `bridge/.venv` (`uv sync`).
- If the chiaki patch changes, regenerate it with `git add -N` for new files before `git diff`, and check it with `git apply --check` on a clean checkout of `PINNED_COMMIT`.
- License: AGPL-3.0 (the chiaki patch is a derivative of chiaki-ng). BikeControl was used as protocol documentation only; don't copy code from it (non-commercial license).

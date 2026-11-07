"""Wires signals -> mapping engine -> chiaki output, plus profile hot reload."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from watchfiles import awatch

from .decode.fitness import BikeSample
from .decode.zwift_ride import RIDE_BUTTONS, KeypadStatus
from .gt7.telemetry import Gt7State
from .mapping.engine import Engine
from .mapping.profiles import ProfileError, ProfileStore
from .mapping.schema import Profile
from .output.chiaki import IDLE, ChiakiSender, ControllerState
from .settings import RiderSettings, save_rider
from .signals import SignalHub, Snapshot

log = logging.getLogger(__name__)

TICK_S = 0.01  # 100 Hz
TASK_ERROR_SHOW_S = 60.0


@dataclass
class Runtime:
    store: ProfileStore
    hub: SignalHub
    sender: ChiakiSender
    active_name: str
    links: list = field(default_factory=list)
    rider: RiderSettings = field(default_factory=RiderSettings)
    rider_path: Path | None = None
    telemetry: object | None = None  # gt7.telemetry.Gt7Telemetry
    resistance: object | None = None  # gt7.resistance.ResistanceController
    sim: bool = False
    error: str = ""
    last_state: ControllerState = IDLE
    last_snapshot: Snapshot = field(default_factory=Snapshot)

    def __post_init__(self) -> None:
        self.hub.ftp_w = self.rider.ftp_w
        self.hub.first_gear_ftp = self.rider.first_gear_ftp
        self.profile: Profile = self.store.load(self.active_name)
        self.engine = Engine(self.profile)

    # --- rider ------------------------------------------------------------------
    def set_rider(self, rider: RiderSettings, *, persist: bool = True) -> None:
        self.rider = rider
        self.hub.ftp_w = rider.ftp_w
        self.hub.first_gear_ftp = rider.first_gear_ftp
        if persist and self.rider_path is not None:
            save_rider(self.rider_path, rider)
        log.info(
            "rider: FTP %d W, 1st gear %d%% FTP, target cadence %d rpm",
            rider.ftp_w, round(rider.first_gear_ftp * 100), rider.target_cadence_rpm,
        )

    # --- profiles ----------------------------------------------------------------
    def activate(self, name: str) -> Profile:
        profile = self.store.load(name)  # raises ProfileError
        changed = name != self.active_name or profile != self.profile
        self.active_name, self.profile, self.error = name, profile, ""
        if changed:
            self.engine.set_profile(profile)
            log.info("profile: %s", name)
        return profile

    def reload_active(self) -> None:
        try:
            profile = self.store.load(self.active_name)
        except ProfileError as e:
            self.error = f"{self.active_name}: {e}\n(keeping the last valid version)"
            log.warning("profile reload failed: %s", e)
            return
        self.error = ""
        if profile == self.profile:
            # Unrelated file or a no-op save: keep toggles, layers and smoothing state.
            return
        self.profile = profile
        self.engine.set_profile(profile)
        log.info("profile reloaded: %s", self.active_name)

    async def watch_profiles(self) -> None:
        async for _changes in awatch(self.store.dir):
            # Any change can matter (a profile's parent may have changed).
            self.reload_active()

    # --- background tasks -----------------------------------------------------------
    task_errors: dict = field(default_factory=dict)  # task name -> (monotonic time, message)

    async def supervise(self, name: str, factory, restart_after_s: float = 2.0) -> None:
        """Run `factory()` and restart it if it crashes, logging the error and showing
        it in the UI for TASK_ERROR_SHOW_S; a crashed task must never fail silently."""
        while True:
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("task %s crashed; restarting in %.0f s", name, restart_after_s)
                self.task_errors[name] = (time.monotonic(), f"{name}: {type(e).__name__}: {e} (restarted)")
                await asyncio.sleep(restart_after_s)

    def recent_task_errors(self) -> list[str]:
        now = time.monotonic()
        return [msg for at, msg in self.task_errors.values() if now - at <= TASK_ERROR_SHOW_S]

    # --- engine loop ---------------------------------------------------------------
    async def run_engine(self) -> None:
        try:
            while True:
                await self.hub.wait_input(TICK_S)
                snap = self.hub.snapshot()
                state = self.engine.step(snap, time.monotonic())
                self.sender.send(state)
                self.last_snapshot, self.last_state = snap, state
        finally:
            for _ in range(3):  # make sure chiaki sees an idle controller right away
                self.sender.send(IDLE)

    # --- simulation ----------------------------------------------------------------
    sim_buttons: frozenset[str] = frozenset()
    sim_paddles: tuple[float, float] = (0.0, 0.0)
    sim_gear: int | None = None  # simulated GT7 gear (6-speed car), None = no telemetry

    def set_sim_gear(self, gear: int | None) -> None:
        self.sim_gear = None if gear is None else max(0, min(gear, 6))
        self._push_sim_gear()

    def _push_sim_gear(self) -> None:
        if self.sim_gear is None or self.telemetry is not None:
            return
        self.hub.update_gt7(
            Gt7State(
                packet_id=0, gear=self.sim_gear, suggested_gear=None, num_gears=6, on_track=True,
                paused=False, loading=False, speed_kph=0.0, rpm=0.0, throttle=0.0,
            )
        )

    def set_sim_input(self, buttons: list[str], paddle_l: float = 0.0, paddle_r: float = 0.0) -> None:
        self.sim_buttons = frozenset(b for b in buttons if b in RIDE_BUTTONS)
        self.sim_paddles = (max(-1.0, min(paddle_l, 1.0)), max(-1.0, min(paddle_r, 1.0)))
        self.hub.update_ride(
            KeypadStatus(
                pressed=self.sim_buttons,
                paddles={0: round(self.sim_paddles[0] * 100), 1: round(self.sim_paddles[1] * 100)},
            )
        )

    async def run_sim(self) -> None:
        """Synthetic trainer: slow power/cadence waves. Ride input comes from the web UI."""
        self.hub.set_connected("kickr", True)
        self.hub.set_connected("ride", True)
        self.set_sim_input([])
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            self.hub.update_bike(
                BikeSample(
                    power_w=max(0.0, 200 + 180 * math.sin(t / 3)),
                    cadence_rpm=max(0.0, 70 + 35 * math.sin(t / 5)),
                    speed_kph=30 + 8 * math.sin(t / 3),
                )
            )
            self._push_sim_gear()  # keep simulated telemetry fresh
            await asyncio.sleep(0.25)

    def status_json(self) -> dict:
        return {
            "signals": self.last_snapshot.to_json(),
            "output": self.last_state.to_json(),
            "profile": self.active_name,
            "rider": self.rider.model_dump(),
            "layers": list(self.engine.active_layers),
            "error": "\n".join(m for m in [self.error, *self.recent_task_errors()] if m),
            "sim": self.sim,
            "devices": [link.to_json() for link in self.links],
            "chiaki": {"addr": f"{self.sender.addr[0]}:{self.sender.addr[1]}", "errors": self.sender.errors},
            "gt7": self.telemetry.to_json() if self.telemetry is not None else None,
            "resistance": self.resistance.to_json() if self.resistance is not None else None,
            "sim_gear": self.sim_gear,
        }

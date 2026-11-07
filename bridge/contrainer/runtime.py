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
from .mapping.engine import Engine
from .mapping.profiles import ProfileError, ProfileStore
from .mapping.schema import Profile
from .output.chiaki import IDLE, ChiakiSender, ControllerState
from .settings import RiderSettings, save_rider
from .signals import SignalHub, Snapshot

log = logging.getLogger(__name__)

TICK_S = 0.01  # 100 Hz


@dataclass
class Runtime:
    store: ProfileStore
    hub: SignalHub
    sender: ChiakiSender
    active_name: str
    links: list = field(default_factory=list)
    rider: RiderSettings = field(default_factory=RiderSettings)
    rider_path: Path | None = None
    sim: bool = False
    error: str = ""
    last_state: ControllerState = IDLE
    last_snapshot: Snapshot = field(default_factory=Snapshot)

    def __post_init__(self) -> None:
        self.hub.ftp_w = self.rider.ftp_w
        self.profile: Profile = self.store.load(self.active_name)
        self.engine = Engine(self.profile)

    # --- rider ------------------------------------------------------------------
    def set_rider(self, rider: RiderSettings, *, persist: bool = True) -> None:
        self.rider = rider
        self.hub.ftp_w = rider.ftp_w
        if persist and self.rider_path is not None:
            save_rider(self.rider_path, rider)
        log.info("FTP: %d W", rider.ftp_w)

    # --- profiles ----------------------------------------------------------------
    def activate(self, name: str) -> Profile:
        profile = self.store.load(name)  # raises ProfileError
        self.active_name, self.profile, self.error = name, profile, ""
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
        self.profile, self.error = profile, ""
        self.engine.set_profile(profile)
        log.info("profile reloaded: %s", self.active_name)

    async def watch_profiles(self) -> None:
        async for _changes in awatch(self.store.dir):
            # Any change can matter (a profile's parent may have changed).
            self.reload_active()

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
            await asyncio.sleep(0.25)

    def status_json(self) -> dict:
        return {
            "signals": self.last_snapshot.to_json(),
            "output": self.last_state.to_json(),
            "profile": self.active_name,
            "rider": self.rider.model_dump(),
            "layers": list(self.engine.active_layers),
            "error": self.error,
            "sim": self.sim,
            "devices": [link.to_json() for link in self.links],
            "chiaki": {"addr": f"{self.sender.addr[0]}:{self.sender.addr[1]}", "errors": self.sender.errors},
        }

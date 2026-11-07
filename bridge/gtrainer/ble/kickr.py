"""Wahoo KICKR (any FTMS / Cycling Power trainer): power, cadence, speed, and
optional resistance control.

A KICKR CORE (fw 3.5.x) notifies both FTMS and CPS at only ~1 Hz, and cadence
needs ~3 s of pedalling to appear (it's derived from whole crank revolutions);
power reacts within ~1 s both ways, so it's the most responsive signal.

Reading only subscribes to notifications. Control (FTMS simulation mode, used by
the GT7 gear -> resistance feature) is opt-in: `control_enabled=True`.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient
from bleak.backends.scanner import AdvertisementData

from ..decode import fitness as f
from ..decode.fitness import (
    CPS_SERVICE_UUID,
    CYCLING_POWER_MEASUREMENT_UUID,
    FTMS_SERVICE_UUID,
    INDOOR_BIKE_DATA_UUID,
    BikeSample,
    CadenceCalculator,
    parse_cycling_power,
    parse_indoor_bike_data,
)
from ..signals import SignalHub
from .link import DeviceLink

log = logging.getLogger(__name__)

RESPONSE_TIMEOUT_S = 3.0


class KickrLink(DeviceLink):
    kind = "kickr"

    def __init__(
        self,
        hub: SignalHub,
        *,
        name_filter: str | None = "KICKR",
        address: str | None = None,
        control_enabled: bool = False,
    ) -> None:
        super().__init__(hub, name_filter=name_filter, address=address)
        self._cadence = CadenceCalculator()
        self.control_enabled = control_enabled
        self.control_status = "off" if not control_enabled else "not connected"
        self.control_epoch = 0  # bumps on every (re)gained control; resend state then
        self.weight_kg: float | None = None
        self._client: BleakClient | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._cp_lock = asyncio.Lock()

    def advertises_service(self, adv: AdvertisementData) -> bool:
        uuids = {u.lower() for u in adv.service_uuids}
        return FTMS_SERVICE_UUID in uuids or CPS_SERVICE_UUID in uuids

    @property
    def control_ready(self) -> bool:
        return self.control_status == "ok" and self._client is not None and self._client.is_connected

    def to_json(self) -> dict:
        connected = self._client is not None and self._client.is_connected
        control = self.control_status if (connected or not self.control_enabled) else "not connected"
        return {**super().to_json(), "control": control, "weight_kg": self.weight_kg}

    # --- connection -----------------------------------------------------------------
    async def on_connected(self, client: BleakClient) -> None:
        services = client.services
        if services.get_characteristic(INDOOR_BIKE_DATA_UUID) is not None:
            await client.start_notify(INDOOR_BIKE_DATA_UUID, self._on_indoor_bike_data)
            self.detail = "FTMS indoor bike data"
        elif services.get_characteristic(CYCLING_POWER_MEASUREMENT_UUID) is not None:
            await client.start_notify(CYCLING_POWER_MEASUREMENT_UUID, self._on_cycling_power)
            self.detail = "Cycling Power (no speed)"
        else:
            raise RuntimeError("trainer exposes neither FTMS indoor bike data nor cycling power")

        if services.get_characteristic(f.USER_WEIGHT_UUID) is not None:
            try:
                self.weight_kg = f.parse_weight(bytes(await client.read_gatt_char(f.USER_WEIGHT_UUID)))
            except Exception as e:
                log.debug("could not read trainer weight: %s", e)

        self._client = client
        if self.control_enabled:
            if services.get_characteristic(f.FTMS_CONTROL_POINT_UUID) is None:
                self.control_status = "unsupported (no FTMS control point)"
            else:
                await client.start_notify(f.FTMS_CONTROL_POINT_UUID, self._on_control_point)
                await self.request_control()

    async def run(self) -> None:
        try:
            await super().run()
        finally:
            self._client = None

    # --- control ----------------------------------------------------------------------
    async def request_control(self) -> bool:
        result = await self._command(f.build_request_control())
        if result != f.RESULT_SUCCESS:
            self.control_status = f"denied (code {result}); close other trainer apps"
            return False
        await self._command(f.build_start())  # optional on most trainers; ignore result
        self.control_status = "ok"
        self.control_epoch += 1
        log.info("KICKR: control granted")
        return True

    async def set_simulation(self, grade_pct: float, crr: float) -> bool:
        if self._client is None or not self._client.is_connected:
            return False
        result = await self._command(f.build_simulation(grade_pct, crr=crr, cw=0.0))
        if result == f.RESULT_CONTROL_NOT_PERMITTED:
            self.control_status = "lost; re-requesting"
            if await self.request_control():
                result = await self._command(f.build_simulation(grade_pct, crr=crr, cw=0.0))
        if result != f.RESULT_SUCCESS:
            self.control_status = f"simulation rejected (code {result})"
            return False
        return True

    async def release(self) -> None:
        """Hand the trainer back (FTMS reset -> its default mode)."""
        if self._client is not None and self._client.is_connected and self.control_status == "ok":
            try:
                await asyncio.wait_for(self._command(f.build_reset()), 2.0)
                log.info("KICKR: control released")
            except Exception as e:
                log.debug("release failed: %s", e)

    async def _command(self, payload: bytes) -> int | None:
        client = self._client
        if client is None:
            return None
        async with self._cp_lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[payload[0]] = fut
            try:
                await client.write_gatt_char(f.FTMS_CONTROL_POINT_UUID, payload, response=True)
                return await asyncio.wait_for(fut, RESPONSE_TIMEOUT_S)
            except TimeoutError:
                return None
            finally:
                self._pending.pop(payload[0], None)

    def _on_control_point(self, _char, data: bytearray) -> None:
        parsed = f.parse_control_response(bytes(data))
        if parsed is None:
            return
        op, result = parsed
        fut = self._pending.get(op)
        if fut is not None and not fut.done():
            fut.set_result(result)

    # --- data ---------------------------------------------------------------------------
    def _on_indoor_bike_data(self, _char, data: bytearray) -> None:
        try:
            self.hub.update_bike(parse_indoor_bike_data(bytes(data)))
        except ValueError:
            pass

    def _on_cycling_power(self, _char, data: bytearray) -> None:
        try:
            sample = parse_cycling_power(bytes(data))
        except ValueError:
            return
        cadence = None
        if sample.crank_revs is not None and sample.crank_event_time is not None:
            cadence = self._cadence.update(sample.crank_revs, sample.crank_event_time, time.monotonic())
        self.hub.update_bike(BikeSample(power_w=sample.power_w, cadence_rpm=cadence))

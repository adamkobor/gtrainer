"""Wahoo KICKR (any FTMS / Cycling Power trainer): read-only power, cadence, speed.

A KICKR CORE (fw 3.5.x) notifies both FTMS and CPS at only ~1 Hz, and cadence
needs ~3 s of pedalling to appear (it's derived from whole crank revolutions);
power reacts within ~1 s both ways, so it's the most responsive signal.

Only notifications are subscribed; the FTMS control point is never written, so the
trainer's resistance mode is left untouched. (A future GT7-telemetry-driven
resistance feature would add a separate control-point writer here.)
"""

from __future__ import annotations

import time

from bleak import BleakClient
from bleak.backends.scanner import AdvertisementData

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


class KickrLink(DeviceLink):
    kind = "kickr"

    def __init__(self, hub: SignalHub, *, name_filter: str | None = "KICKR", address: str | None = None) -> None:
        super().__init__(hub, name_filter=name_filter, address=address)
        self._cadence = CadenceCalculator()

    def advertises_service(self, adv: AdvertisementData) -> bool:
        uuids = {u.lower() for u in adv.service_uuids}
        return FTMS_SERVICE_UUID in uuids or CPS_SERVICE_UUID in uuids

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

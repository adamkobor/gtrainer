"""Zwift Ride handlebar controller reader (firmware <= 1.2.x)."""

from __future__ import annotations

import logging

from bleak import BleakClient
from bleak.backends.scanner import AdvertisementData

from ..decode import zwift_ride as zr
from ..signals import SignalHub
from .link import DeviceLink

log = logging.getLogger(__name__)


class RideLink(DeviceLink):
    kind = "ride"

    def __init__(
        self,
        hub: SignalHub,
        *,
        name_filter: str | None = "Zwift Ride",
        address: str | None = None,
        cleared_means_pressed: bool = True,
    ) -> None:
        super().__init__(hub, name_filter=name_filter, address=address)
        self.cleared_means_pressed = cleared_means_pressed
        self.firmware = ""

    def advertises_service(self, adv: AdvertisementData) -> bool:
        # Only used when no name filter is set. Note: Wahoo KICKR CORE trainers also
        # expose the Zwift fc82 service (for Ride pairing), so never match those.
        uuids = {u.lower() for u in adv.service_uuids}
        if "kickr" in (adv.local_name or "").lower():
            return False
        return zr.ADVERTISED_SERVICE_UUID in uuids or zr.SERVICE_UUID in uuids

    async def on_connected(self, client: BleakClient) -> None:
        services = client.services
        self.warning = ""
        if services.get_characteristic(zr.FIRMWARE_REVISION_UUID) is not None:
            try:
                raw = await client.read_gatt_char(zr.FIRMWARE_REVISION_UUID)
                self.firmware = raw.decode(errors="replace").strip("\x00 ")
                if zr.firmware_supported(self.firmware) is False:
                    self.warning = (
                        f"firmware {self.firmware} is newer than 1.2.x and may be locked to Zwift"
                    )
                    log.warning("Zwift Ride %s", self.warning)
            except Exception as e:
                log.debug("could not read Ride firmware: %s", e)

        missing = [
            u for u in (zr.ASYNC_UUID, zr.SYNC_RX_UUID, zr.SYNC_TX_UUID) if services.get_characteristic(u) is None
        ]
        if missing:
            raise RuntimeError(f"Zwift service characteristics missing: {missing} (locked firmware?)")

        await client.start_notify(zr.ASYNC_UUID, self._on_data)
        await client.start_notify(zr.SYNC_TX_UUID, self._on_data)
        await client.write_gatt_char(zr.SYNC_RX_UUID, zr.RIDE_ON, response=False)
        self.detail = f"firmware {self.firmware or '?'}"

    def _on_data(self, _char, data: bytearray) -> None:
        msg = zr.parse_message(bytes(data), cleared_means_pressed=self.cleared_means_pressed)
        if isinstance(msg, zr.KeypadStatus):
            self.hub.update_ride(msg)
        elif isinstance(msg, zr.Battery):
            self.hub.set_battery(msg.percent)

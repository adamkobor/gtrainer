"""Scan / connect / auto-reconnect loop shared by the device readers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..signals import SignalHub

log = logging.getLogger(__name__)

# CoreBluetooth copes badly with overlapping scans; serialize them.
_SCAN_LOCK = asyncio.Lock()


def adv_name(device: BLEDevice, adv: AdvertisementData) -> str:
    return adv.local_name or device.name or ""


class DeviceLink(ABC):
    kind: str  # "kickr" | "ride"

    def __init__(self, hub: SignalHub, *, name_filter: str | None, address: str | None) -> None:
        self.hub = hub
        self.name_filter = name_filter
        self.address = address
        self.status = "idle"
        self.detail = ""
        self.device_name = ""
        self.warning = ""

    @abstractmethod
    def advertises_service(self, adv: AdvertisementData) -> bool: ...

    @abstractmethod
    async def on_connected(self, client: BleakClient) -> None: ...

    def matches(self, device: BLEDevice, adv: AdvertisementData) -> bool:
        if self.address:
            return device.address.lower() == self.address.lower()
        name = adv_name(device, adv).lower()
        if self.name_filter and self.name_filter.lower() in name:
            return True
        return self.advertises_service(adv) and not self.name_filter

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "name": self.device_name,
            "warning": self.warning,
        }

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                self.status, self.detail = "scanning", ""
                async with _SCAN_LOCK:
                    device = await BleakScanner.find_device_by_filter(self.matches, timeout=10.0)
                if device is None:
                    self.detail = "not found (is it awake and not connected to another app?)"
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.5, 10.0)
                    continue
                self.device_name = device.name or device.address
                self.status = "connecting"
                disconnected = asyncio.Event()
                async with BleakClient(device, disconnected_callback=lambda _c: disconnected.set()) as client:
                    await self.on_connected(client)
                    self.status = "connected"
                    self.hub.set_connected(self.kind, True)
                    backoff = 1.0
                    log.info("%s connected: %s", self.kind, self.device_name)
                    await disconnected.wait()
                log.warning("%s disconnected", self.kind)
                self.status = "disconnected"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # BLE stacks throw a zoo of exceptions; keep retrying
                log.warning("%s error: %s", self.kind, e)
                self.status, self.detail = "error", str(e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)
            finally:
                self.hub.set_connected(self.kind, False)
            await asyncio.sleep(0.5)

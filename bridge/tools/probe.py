"""Phase-0 BLE probe: verify the KICKR + Zwift Ride assumptions on real hardware.

    uv run python tools/probe.py scan                 # list nearby BLE devices
    uv run python tools/probe.py ride  [--seconds 60] # dump Ride GATT, handshake, log button frames
    uv run python tools/probe.py kickr [--seconds 20] # dump trainer GATT, log FTMS/CPS samples

Raw frames are also appended to captures/<device>-<timestamp>.log (hex), for tests.
Press each Ride button and both paddles one at a time while `ride` runs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contrainer.decode import fitness  # noqa: E402
from contrainer.decode import zwift_ride as zr  # noqa: E402

CAPTURES = Path(__file__).resolve().parent.parent / "captures"


def capture_file(kind: str):
    CAPTURES.mkdir(exist_ok=True)
    path = CAPTURES / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    print(f"capturing raw frames to {path}")
    return path.open("a")


async def scan(seconds: float) -> None:
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = sorted(found.values(), key=lambda da: -(da[1].rssi or -999))
    for device, adv in rows:
        name = adv.local_name or device.name or "?"
        uuids = ", ".join(u[4:8] if u.endswith("-0000-1000-8000-00805f9b34fb") else u for u in adv.service_uuids)
        print(f"{adv.rssi:>4} dBm  {device.address}  {name:<28} {uuids}")


async def dump_gatt(client: BleakClient) -> None:
    for service in client.services:
        print(f"service {service.uuid}  {service.description}")
        for ch in service.characteristics:
            line = f"    char {ch.uuid}  [{', '.join(ch.properties)}]  {ch.description}"
            if "read" in ch.properties:
                try:
                    value = await client.read_gatt_char(ch)
                    text = value.decode(errors="replace") if all(32 <= b < 127 for b in value) else value.hex(" ")
                    line += f"  = {text!r}"
                except Exception as e:
                    line += f"  (read failed: {e})"
            print(line)


async def find(match, what: str):
    print(f"scanning for {what}…")
    device = await BleakScanner.find_device_by_filter(match, timeout=15)
    if device is None:
        raise SystemExit(f"{what} not found. Wake it up and make sure Zwift/other apps are not connected.")
    print(f"found {device.name} ({device.address})")
    return device


async def probe_ride(seconds: float, address: str | None) -> None:
    def match(d, adv):
        if address:
            return d.address.lower() == address.lower()
        return "zwift ride" in (adv.local_name or d.name or "").lower()  # KICKRs also expose fc82

    device = await find(match, "Zwift Ride")
    log = capture_file("ride")
    prev: frozenset[str] = frozenset()

    def on_data(char, data: bytearray):
        nonlocal prev
        raw = bytes(data)
        log.write(f"{time.time():.3f} {char.uuid} {raw.hex()}\n")
        log.flush()
        msg = zr.parse_message(raw)
        if isinstance(msg, zr.KeypadStatus):
            paddles = {k: v for k, v in msg.paddles.items() if v}
            if msg.pressed != prev or paddles:
                print(f"keypad map=0x{msg.button_map:08x} pressed={sorted(msg.pressed)} paddles={paddles}")
            prev = msg.pressed
        elif not isinstance(msg, zr.Empty):
            print(f"{char.uuid[4:8]} {type(msg).__name__}: {msg}  raw={raw.hex(' ')}")

    async with BleakClient(device) as client:
        await dump_gatt(client)
        fw = ""
        if client.services.get_characteristic(zr.FIRMWARE_REVISION_UUID):
            fw = (await client.read_gatt_char(zr.FIRMWARE_REVISION_UUID)).decode(errors="replace")
        print(f"\nfirmware: {fw!r}  supported={zr.firmware_supported(fw) if fw else 'unknown'}")
        await client.start_notify(zr.ASYNC_UUID, on_data)
        await client.start_notify(zr.SYNC_TX_UUID, on_data)
        await client.write_gatt_char(zr.SYNC_RX_UUID, zr.RIDE_ON, response=False)
        print(f"handshake sent; press every button and paddle (logging {seconds:.0f}s)…\n")
        await asyncio.sleep(seconds)


async def probe_kickr(seconds: float, address: str | None, name: str) -> None:
    def match(d, adv):
        if address:
            return d.address.lower() == address.lower()
        return name.lower() in (adv.local_name or d.name or "").lower()

    device = await find(match, f"trainer matching {name!r}")
    log = capture_file("kickr")

    def on_ibd(char, data: bytearray):
        raw = bytes(data)
        log.write(f"{time.time():.3f} {char.uuid} {raw.hex()}\n")
        flags = int.from_bytes(raw[:2], "little")
        print(f"FTMS flags=0x{flags:04x} {fitness.parse_indoor_bike_data(raw)}")

    def on_cps(char, data: bytearray):
        raw = bytes(data)
        log.write(f"{time.time():.3f} {char.uuid} {raw.hex()}\n")
        print(f"CPS {fitness.parse_cycling_power(raw)}")

    async with BleakClient(device) as client:
        await dump_gatt(client)
        if client.services.get_characteristic(fitness.INDOOR_BIKE_DATA_UUID):
            await client.start_notify(fitness.INDOOR_BIKE_DATA_UUID, on_ibd)
        if client.services.get_characteristic(fitness.CYCLING_POWER_MEASUREMENT_UUID):
            await client.start_notify(fitness.CYCLING_POWER_MEASUREMENT_UUID, on_cps)
        print(f"\npedal a bit (logging {seconds:.0f}s)…\n")
        await asyncio.sleep(seconds)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--seconds", type=float, default=8)
    r = sub.add_parser("ride")
    r.add_argument("--seconds", type=float, default=60)
    r.add_argument("--address")
    k = sub.add_parser("kickr")
    k.add_argument("--seconds", type=float, default=20)
    k.add_argument("--address")
    k.add_argument("--name", default="KICKR")
    args = p.parse_args()
    if args.cmd == "scan":
        asyncio.run(scan(args.seconds))
    elif args.cmd == "ride":
        asyncio.run(probe_ride(args.seconds, args.address))
    else:
        asyncio.run(probe_kickr(args.seconds, args.address, args.name))


if __name__ == "__main__":
    main()

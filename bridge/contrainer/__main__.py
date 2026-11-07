"""contrainer: KICKR + Zwift Ride -> PS5 controller bridge.

    uv run contrainer                    # real devices, profile gt7
    uv run contrainer --sim              # no hardware: synthetic trainer, click Ride buttons in the UI
    uv run contrainer --profile walking
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import webbrowser
from pathlib import Path

import uvicorn

from .ble.kickr import KickrLink
from .ble.ride import RideLink
from .mapping.profiles import ProfileError, ProfileStore
from .output.chiaki import DEFAULT_PORT, ChiakiSender
from .runtime import Runtime
from .settings import RiderSettings, SettingsError, load_rider
from .signals import SignalHub
from .web.app import create_app

DEFAULT_PROFILES = Path(__file__).resolve().parent.parent / "profiles"
DEFAULT_RIDER = Path(__file__).resolve().parent.parent / "rider.yaml"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="contrainer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default="gt7", help="profile to start with (default: gt7)")
    p.add_argument("--profiles-dir", type=Path, default=DEFAULT_PROFILES)
    p.add_argument("--rider-file", type=Path, default=DEFAULT_RIDER, help="rider settings (FTP), editable in the web UI")
    p.add_argument("--ftp", type=int, help="set FTP in watts (saved to the rider file)")
    p.add_argument("--sim", action="store_true", help="simulate the trainer; Ride input comes from the web UI")
    p.add_argument("--kickr-name", default="KICKR", help="substring of the trainer's BLE name (default: KICKR)")
    p.add_argument("--kickr-address", help="exact trainer BLE address/UUID (overrides --kickr-name)")
    p.add_argument("--ride-name", default="Zwift Ride", help="substring of the Ride's BLE name (default: Zwift Ride)")
    p.add_argument("--ride-address", help="exact Ride BLE address/UUID")
    p.add_argument("--no-kickr", action="store_true", help="don't connect to the trainer")
    p.add_argument("--no-ride", action="store_true", help="don't connect to the Ride")
    p.add_argument(
        "--ride-set-bit-pressed",
        action="store_true",
        help="treat a SET button bit as pressed (only if the probe shows inverted buttons)",
    )
    p.add_argument("--chiaki-port", type=int, default=DEFAULT_PORT)
    p.add_argument("--web-port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open the web UI in the browser")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    store = ProfileStore(args.profiles_dir)
    hub = SignalHub()
    sender = ChiakiSender(port=args.chiaki_port)
    rider = load_rider(args.rider_file)
    rt = Runtime(
        store=store, hub=hub, sender=sender, active_name=args.profile, sim=args.sim,
        rider=rider, rider_path=args.rider_file,
    )
    if args.ftp is not None:
        rt.set_rider(RiderSettings(ftp_w=args.ftp))

    tasks: list[asyncio.Task] = [
        asyncio.create_task(rt.run_engine(), name="engine"),
        asyncio.create_task(rt.watch_profiles(), name="profiles"),
    ]
    if args.sim:
        tasks.append(asyncio.create_task(rt.run_sim(), name="sim"))
    else:
        if not args.no_kickr:
            rt.links.append(KickrLink(hub, name_filter=args.kickr_name, address=args.kickr_address))
        if not args.no_ride:
            rt.links.append(
                RideLink(
                    hub,
                    name_filter=args.ride_name,
                    address=args.ride_address,
                    cleared_means_pressed=not args.ride_set_bit_pressed,
                )
            )
        tasks += [asyncio.create_task(link.run(), name=link.kind) for link in rt.links]

    url = f"http://127.0.0.1:{args.web_port}"
    server = uvicorn.Server(
        uvicorn.Config(create_app(rt), host="127.0.0.1", port=args.web_port, log_level="warning")
    )
    logging.getLogger(__name__).info("web UI: %s  ·  chiaki-ng: 127.0.0.1:%d", url, args.chiaki_port)
    if args.open:
        webbrowser.open(url)
    try:
        await server.serve()  # returns on Ctrl+C
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        sender.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(run(args))
    except ProfileError as e:
        raise SystemExit(f"profile error: {e}") from e
    except (SettingsError, ValueError) as e:
        raise SystemExit(f"settings error: {e}") from e
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

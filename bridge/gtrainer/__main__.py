"""gtrainer: KICKR + Zwift Ride -> PS5 controller bridge.

    uv run gtrainer                    # real devices, profile gt7
    uv run gtrainer --sim              # no hardware: synthetic trainer, click Ride buttons in the UI
    uv run gtrainer --profile walking
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
from .gt7.resistance import ResistanceController
from .gt7.telemetry import Gt7Telemetry
from .mapping.profiles import ProfileError, ProfileStore
from .output.chiaki import DEFAULT_PORT, ChiakiSender
from .runtime import Runtime
from .settings import RiderSettings, SettingsError, load_rider
from .signals import SignalHub
from .web.app import create_app

DEFAULT_PROFILES = Path(__file__).resolve().parent.parent / "profiles"
DEFAULT_RIDER = Path(__file__).resolve().parent.parent / "rider.yaml"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="gtrainer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    p.add_argument(
        "--gt7",
        action="store_true",
        help="read GT7 telemetry: gear-based throttle (power_gear) and trainer resistance per gear",
    )
    p.add_argument("--ps5", help="PS5 IP for GT7 telemetry (default: discover on the LAN); implies --gt7")
    p.add_argument("--no-resistance", action="store_true", help="with --gt7: don't control the trainer")
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
        rt.set_rider(rt.rider.model_copy(update={"ftp_w": RiderSettings(ftp_w=args.ftp).ftp_w}))
    use_gt7 = args.gt7 or bool(args.ps5)

    tasks: list[asyncio.Task] = []

    def start(name: str, factory) -> None:
        # supervised: a crash is logged, shown in the web UI and the task restarted
        tasks.append(asyncio.create_task(rt.supervise(name, factory), name=name))

    start("engine", rt.run_engine)
    start("profiles", rt.watch_profiles)
    if use_gt7:
        rt.telemetry = Gt7Telemetry(hub.update_gt7, host=args.ps5)
        start("gt7", rt.telemetry.run)
    if args.sim:
        start("sim", rt.run_sim)
    else:
        if not args.no_kickr:
            kickr = KickrLink(
                hub,
                name_filter=args.kickr_name,
                address=args.kickr_address,
                control_enabled=use_gt7 and not args.no_resistance,
            )
            rt.links.append(kickr)
            if kickr.control_enabled:
                rt.resistance = ResistanceController(hub, kickr, lambda: rt.rider)
                start("resistance", rt.resistance.run)
        if not args.no_ride:
            rt.links.append(
                RideLink(
                    hub,
                    name_filter=args.ride_name,
                    address=args.ride_address,
                    cleared_means_pressed=not args.ride_set_bit_pressed,
                )
            )
        for link in rt.links:
            start(link.kind, link.run)

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
        # Release the trainer first (needs the BLE link still up), then stop the rest.
        first = [t for t in tasks if t.get_name() == "resistance"]
        rest = [t for t in tasks if t not in first]
        for group in (first, rest):
            for t in group:
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*group, return_exceptions=True)
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

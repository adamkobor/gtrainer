"""Read-only GT7 telemetry check: prints gear, flags and speed (GT7 must be running).

    uv run python tools/gt7_probe.py                 # discover the PS5 on the LAN
    uv run python tools/gt7_probe.py --ps5 192.168.1.50 --seconds 20
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtrainer.gt7.telemetry import Gt7Telemetry, discover_ps5  # noqa: E402


async def main(host: str | None, seconds: float) -> None:
    if host is None:
        print("discovering PS5…")
        host = await discover_ps5()
        if host is None:
            raise SystemExit("no PS5 found (is it on and on the same network?)")
    print(f"PS5 at {host}; sending heartbeats, listening on UDP 33740 for {seconds:.0f}s")
    last_print = 0.0

    def on_state(s):
        nonlocal last_print
        now = time.monotonic()
        if now - last_print >= 0.5:
            last_print = now
            gear = s.gear or "R/N"
            print(
                f"gear {gear}/{s.num_gears}  suggested {s.suggested_gear or '-'}  "
                f"{'on track' if s.on_track else 'off track'}{' PAUSED' if s.paused else ''}"
                f"{' LOADING' if s.loading else ''}  {s.speed_kph:5.1f} km/h  {s.rpm:5.0f} rpm  "
                f"throttle {s.throttle:4.0%}"
            )

    t = Gt7Telemetry(on_state, host=host)
    task = asyncio.create_task(t.run())
    await asyncio.sleep(seconds)
    task.cancel()
    print(f"{t.packets} packets ({t.bad_packets} undecodable)")
    if t.packets == 0:
        print("no telemetry: GT7 only sends while the game is running (menus count, the PS5 home screen doesn't)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ps5")
    p.add_argument("--seconds", type=float, default=10)
    a = p.parse_args()
    asyncio.run(main(a.ps5, a.seconds))

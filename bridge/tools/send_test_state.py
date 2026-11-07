"""Send a scripted input sweep to the patched chiaki-ng, without any bike hardware.

    uv run python tools/send_test_state.py            # R2 ramp, stick circle, button taps
    uv run python tools/send_test_state.py --cut      # stop mid-ramp: chiaki must go idle within 250 ms

Watch the PS5 (e.g. Settings > Accessibility > Controllers > "Test" screens, or GT7).
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtrainer.output.chiaki import BUTTON_BITS, DEFAULT_PORT, IDLE, ChiakiSender, ControllerState  # noqa: E402

HZ = 100


def run_for(sender: ChiakiSender, seconds: float, fn) -> None:
    t0 = time.monotonic()
    while (t := time.monotonic() - t0) < seconds:
        sender.send(fn(t / seconds))
        time.sleep(1 / HZ)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--cut", action="store_true", help="exit abruptly in the middle of the R2 ramp")
    args = p.parse_args()
    sender = ChiakiSender(port=args.port)

    print("R2 ramp 0 → 100% over 3 s")
    if args.cut:
        run_for(sender, 1.5, lambda f: ControllerState(r2=round(f * 0.5 * 255)))
        print("cut! (no idle packet sent; chiaki should release R2 by itself)")
        return
    run_for(sender, 3, lambda f: ControllerState(r2=round(f * 255)))

    print("left stick circle, 4 s")
    run_for(
        sender,
        4,
        lambda f: ControllerState(
            left_x=round(math.cos(f * 2 * math.pi) * 32767), left_y=round(math.sin(f * 2 * math.pi) * 32767)
        ),
    )

    for name in ("dpad_down", "dpad_up", "dpad_right", "dpad_left"):
        print(f"tap {name}")
        run_for(sender, 0.15, lambda f, n=name: ControllerState(buttons=BUTTON_BITS[n]))
        run_for(sender, 0.4, lambda f: IDLE)

    for _ in range(3):
        sender.send(IDLE)
    print(f"done ({sender.sent} packets, {sender.errors} send errors)")


if __name__ == "__main__":
    main()

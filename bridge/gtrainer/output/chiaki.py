"""Controller state model + UDP sender for the patched chiaki-ng (chiaki/external-input.patch)."""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

# Bit values from chiaki-ng lib/include/chiaki/controller.h (ChiakiControllerButton).
BUTTON_BITS: dict[str, int] = {
    "cross": 1 << 0,
    "circle": 1 << 1,
    "square": 1 << 2,
    "triangle": 1 << 3,
    "dpad_left": 1 << 4,
    "dpad_right": 1 << 5,
    "dpad_up": 1 << 6,
    "dpad_down": 1 << 7,
    "l1": 1 << 8,
    "r1": 1 << 9,
    "l3": 1 << 10,
    "r3": 1 << 11,
    "options": 1 << 12,
    "create": 1 << 13,  # "share" in chiaki
    "touchpad": 1 << 14,
    "ps": 1 << 15,
}
PS_BUTTONS: tuple[str, ...] = tuple(BUTTON_BITS)
TRIGGERS: tuple[str, ...] = ("l2", "r2")
STICKS: tuple[str, ...] = ("left_x", "left_y", "right_x", "right_y")  # y: negative = up/forward
PS_AXES: tuple[str, ...] = TRIGGERS + STICKS

MAGIC = b"CTRN"
PROTOCOL_VERSION = 1
PACKET = struct.Struct("<4sBIIBBhhhh")
assert PACKET.size == 23
DEFAULT_PORT = 47800


@dataclass(frozen=True)
class ControllerState:
    buttons: int = 0
    l2: int = 0  # 0..255
    r2: int = 0
    left_x: int = 0  # -32767..32767
    left_y: int = 0
    right_x: int = 0
    right_y: int = 0

    def pressed(self) -> list[str]:
        return [name for name, bit in BUTTON_BITS.items() if self.buttons & bit]

    def to_json(self) -> dict:
        return {
            "buttons": self.pressed(),
            "l2": self.l2,
            "r2": self.r2,
            "left_x": self.left_x,
            "left_y": self.left_y,
            "right_x": self.right_x,
            "right_y": self.right_y,
        }


IDLE = ControllerState()


def pack(state: ControllerState, seq: int) -> bytes:
    return PACKET.pack(
        MAGIC,
        PROTOCOL_VERSION,
        seq & 0xFFFFFFFF,
        state.buttons & 0xFFFF,
        state.l2,
        state.r2,
        state.left_x,
        state.left_y,
        state.right_x,
        state.right_y,
    )


def unpack(data: bytes) -> tuple[int, ControllerState]:
    magic, version, seq, buttons, l2, r2, lx, ly, rx, ry = PACKET.unpack(data)
    if magic != MAGIC or version != PROTOCOL_VERSION:
        raise ValueError("not a gtrainer packet")
    return seq, ControllerState(buttons, l2, r2, lx, ly, rx, ry)


class ChiakiSender:
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
        self.addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._seq = 0
        self.sent = 0
        self.errors = 0

    def send(self, state: ControllerState) -> None:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        try:
            self._sock.sendto(pack(state, self._seq), self.addr)
            self.sent += 1
        except OSError:
            # Nothing listening yet / transient; chiaki will time out to idle on its own.
            self.errors += 1

    def close(self) -> None:
        self._sock.close()

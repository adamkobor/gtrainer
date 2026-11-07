"""Gran Turismo 7 "Simulator Interface" telemetry over UDP.

Protocol (community reverse-engineered; see Nenkai/PDTools, Bornhall/gt7telemetry):
- Send a heartbeat b"A" to <PS5>:33739 at least every ~100 packets (we send 1/s);
  GT7 answers with ~60 packets/s to our UDP port 33740.
- Packets are Salsa20-encrypted: key = first 32 bytes of
  b"Simulator Interface Packet GT7 ver 0.0"; the 8-byte nonce is built from the
  4-byte seed at offset 0x40: (seed ^ 0xDEADBEAF, seed), little-endian.
- Decrypted packets start with magic 0x47375330 ("0S7G").

Only GT7 running on the console sends telemetry; nothing arrives in the PS5 menus.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass

from Crypto.Cipher import Salsa20

log = logging.getLogger(__name__)

SEND_PORT = 33739
RECV_PORT = 33740
DISCOVERY_PORT = 9302
KEY = b"Simulator Interface Packet GT7 ver 0.0"[:32]
MAGIC = 0x47375330
NONCE_XOR = 0xDEADBEAF  # sic, not DEADBEEF
MIN_PACKET = 0x128  # heartbeat "A" packets are 296 bytes

# offsets in the decrypted packet
OFF_RPM = 0x3C  # float
OFF_SPEED = 0x4C  # float, m/s
OFF_PACKET_ID = 0x70  # int32
OFF_FLAGS = 0x8E  # int16
OFF_GEARS = 0x90  # u8: low nibble current gear (0 = reverse/neutral), high nibble suggested (15 = none)
OFF_THROTTLE = 0x91  # u8 0..255
OFF_GEAR_RATIOS = 0x104  # 8 floats, 0 for gears the car doesn't have

FLAG_ON_TRACK = 1 << 0
FLAG_PAUSED = 1 << 1
FLAG_LOADING = 1 << 2


@dataclass(frozen=True)
class Gt7State:
    packet_id: int
    gear: int  # 0 = reverse/neutral
    suggested_gear: int | None
    num_gears: int
    on_track: bool
    paused: bool
    loading: bool
    speed_kph: float
    rpm: float
    throttle: float  # 0..1 (what GT7 sees)

    @property
    def driving(self) -> bool:
        return self.on_track and not self.paused and not self.loading

    def to_json(self) -> dict:
        return {
            "gear": self.gear,
            "suggested_gear": self.suggested_gear,
            "num_gears": self.num_gears,
            "on_track": self.on_track,
            "paused": self.paused,
            "loading": self.loading,
            "driving": self.driving,
            "speed_kph": round(self.speed_kph, 1),
            "rpm": round(self.rpm),
            "throttle": round(self.throttle, 2),
        }


def _nonce(seed: int) -> bytes:
    return struct.pack("<II", seed ^ NONCE_XOR, seed)


def decrypt(data: bytes) -> bytes | None:
    if len(data) < MIN_PACKET:
        return None
    (seed,) = struct.unpack_from("<I", data, 0x40)
    plain = Salsa20.new(key=KEY, nonce=_nonce(seed)).decrypt(bytes(data))
    if struct.unpack_from("<I", plain, 0)[0] != MAGIC:
        return None
    return plain


def encrypt(plain: bytes, seed: int) -> bytes:
    """Inverse of decrypt (used by tests / replay): the seed is stored in clear at 0x40."""
    data = bytearray(Salsa20.new(key=KEY, nonce=_nonce(seed)).encrypt(bytes(plain)))
    struct.pack_into("<I", data, 0x40, seed)
    return bytes(data)


def parse(plain: bytes) -> Gt7State:
    gears = plain[OFF_GEARS]
    suggested = gears >> 4
    ratios = struct.unpack_from("<8f", plain, OFF_GEAR_RATIOS)
    flags = struct.unpack_from("<h", plain, OFF_FLAGS)[0]
    return Gt7State(
        packet_id=struct.unpack_from("<i", plain, OFF_PACKET_ID)[0],
        gear=gears & 0x0F,
        suggested_gear=None if suggested == 15 else suggested,
        num_gears=sum(1 for r in ratios if r > 0.0) or 1,
        on_track=bool(flags & FLAG_ON_TRACK),
        paused=bool(flags & FLAG_PAUSED),
        loading=bool(flags & FLAG_LOADING),
        speed_kph=struct.unpack_from("<f", plain, OFF_SPEED)[0] * 3.6,
        rpm=struct.unpack_from("<f", plain, OFF_RPM)[0],
        throttle=plain[OFF_THROTTLE] / 255.0,
    )


DISCOVERY_MSG = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"

LAN_BLOCKED_HINT = (
    "can't reach the local network ({err}). On macOS, allow Local Network access for the app "
    "you run gtrainer from (Terminal, iTerm, IntelliJ, ...): System Settings > Privacy & Security > "
    "Local Network, then restart that app"
)


async def discover_ps5(timeout: float = 3.0) -> str | None:
    """Find a PS5 on the LAN with the Remote Play discovery broadcast."""

    def _search() -> str | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("0.0.0.0", 0))
            s.settimeout(0.5)
            s.sendto(DISCOVERY_MSG, ("255.255.255.255", DISCOVERY_PORT))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    data, addr = s.recvfrom(2048)
                except TimeoutError:
                    continue
                if b"host-type:PS5" in data:
                    return addr[0]
            return None
        finally:
            s.close()

    return await asyncio.to_thread(_search)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: Gt7Telemetry) -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner._on_packet(data, addr)

    def error_received(self, exc: Exception) -> None:
        self.owner._on_send_error(exc)


class Gt7Telemetry:
    """Keeps the heartbeat going and feeds parsed states to `on_state`."""

    STALE_S = 1.0

    def __init__(self, on_state, host: str | None = None) -> None:
        self.on_state = on_state
        self.host = host
        self.status = "idle"
        self.packets = 0
        self.bad_packets = 0
        self.last_state: Gt7State | None = None
        self.last_at: float | None = None
        self._last_id = -1
        self._send_error: str | None = None

    def _on_send_error(self, exc: Exception) -> None:
        self._send_error = LAN_BLOCKED_HINT.format(err=getattr(exc, "strerror", None) or exc)

    @property
    def live(self) -> bool:
        return self.last_at is not None and time.monotonic() - self.last_at <= self.STALE_S

    def to_json(self) -> dict:
        return {
            "host": self.host,
            "status": self.status if not self.live else "live",
            "packets": self.packets,
            "state": self.last_state.to_json() if self.last_state and self.live else None,
        }

    def _on_packet(self, data: bytes, addr) -> None:
        if self.host and addr[0] != self.host:
            return
        plain = decrypt(data)
        if plain is None:
            self.bad_packets += 1
            return
        state = parse(plain)
        if state.packet_id <= self._last_id and self._last_id - state.packet_id < 1000:
            return  # stale/reordered
        self._last_id = state.packet_id
        self.packets += 1
        self.last_state, self.last_at = state, time.monotonic()
        self.on_state(state)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while self.host is None:
            self.status = "searching for PS5"
            try:
                self.host = await discover_ps5()
            except OSError as e:  # macOS Local Network privacy shows up as EHOSTUNREACH etc.
                self.status = LAN_BLOCKED_HINT.format(err=e.strerror or e)
                log.warning("GT7 telemetry: %s", self.status)
            else:
                if self.host is None:
                    self.status = "no PS5 found on the network yet (is it on? or pass --ps5 <ip>)"
            if self.host is None:
                await asyncio.sleep(5)
        log.info("GT7 telemetry: PS5 at %s", self.host)
        while True:
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self), local_addr=("0.0.0.0", RECV_PORT)
                )
                break
            except OSError as e:
                self.status = (
                    f"can't listen on UDP {RECV_PORT} ({e.strerror or e}); is another gtrainer "
                    "or gt7_probe running? retrying"
                )
                log.warning("GT7 telemetry: %s", self.status)
                await asyncio.sleep(5)
        self.status = f"waiting for GT7 at {self.host}"
        try:
            while True:
                self._send_error = None
                try:
                    transport.sendto(b"A", (self.host, SEND_PORT))
                except OSError as e:
                    self._on_send_error(e)
                await asyncio.sleep(1.0)
                if not self.live:
                    self.status = self._send_error or f"waiting for GT7 at {self.host} (is the game running?)"
                    self._last_id = -1  # GT7 restarts its packet counter
                    if self._send_error:
                        log.warning("GT7 telemetry: %s", self._send_error)
        finally:
            transport.close()

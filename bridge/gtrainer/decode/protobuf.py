"""Minimal protobuf wire-format reader (enough for the Zwift Ride messages)."""

from __future__ import annotations

from collections.abc import Iterator


class DecodeError(ValueError):
    pass


WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise DecodeError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift >= 64:
            raise DecodeError("varint too long")


def _take(buf: bytes, pos: int, n: int) -> tuple[bytes, int]:
    if pos + n > len(buf):
        raise DecodeError("truncated field")
    return bytes(buf[pos : pos + n]), pos + n


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Yield (field_number, wire_type, value); value is int for varints, bytes otherwise."""
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
        elif wire == WIRE_FIXED64:
            value, pos = _take(buf, pos, 8)
        elif wire == WIRE_LEN:
            length, pos = read_varint(buf, pos)
            value, pos = _take(buf, pos, length)
        elif wire == WIRE_FIXED32:
            value, pos = _take(buf, pos, 4)
        else:
            raise DecodeError(f"unsupported wire type {wire}")
        yield field, wire, value


def zigzag_decode(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def encode_varint(n: int) -> bytes:
    """Used by tests to build frames."""
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag_encode(n: int) -> int:
    return (n << 1) ^ (n >> 63)

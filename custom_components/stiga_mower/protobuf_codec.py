"""Minimal Protobuf wire-format codec for STIGA MQTT frames.

STIGA does not publish a `.proto` schema for its MQTT messages, so this codec
operates on raw field numbers (the same approach matthewgream takes in
`StigaAPIUtilitiesProtobuf.js`). Decoded values are returned as
`dict[int, Any]`; encoded payloads accept the same shape.

Wire-type handling matches matthewgream's reference implementation so that
the same hex fixtures roundtrip identically:

  * VARINT (0)  -> Python int
  * FIXED64 (1) -> 8-byte little-endian bytes (use `read_double_le` to
                   reinterpret as IEEE 754 double when needed; this is what
                   STIGA does for GPS offsets and orientation)
  * LEN (2)     -> printable utf-8 -> str; otherwise try recursive decode;
                   if neither parses cleanly, raw bytes
  * FIXED32 (5) -> Python float (IEEE 754); set `fixed32_as_int=True` to read
                   as signed int instead

Repeated fields with the same number collapse into a list. Encoding the
inverse: pass a list and each element is emitted as its own field record.
"""

from __future__ import annotations

import struct
from typing import Any

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


class ProtobufError(ValueError):
    """Raised for malformed wire-format input."""


# ---------------------------------------------------------------- Varint helpers


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(buf):
            raise ProtobufError(f"truncated varint at offset {start}")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtobufError(f"varint too long at offset {start}")


def _write_varint(out: bytearray, value: int) -> None:
    if value < 0:
        # Two's complement to 64-bit unsigned, matching protobuf semantics
        # for negative VARINT (uses 10 bytes).
        value &= (1 << 64) - 1
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)


def _is_printable_utf8(buf: bytes) -> bool:
    if not buf:
        return False
    try:
        text = buf.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(0x20 <= ord(c) <= 0x7E or c in "\t\n\r" for c in text)


# ---------------------------------------------------------------- Decode


def decode(buf: bytes, *, fixed32_as_int: bool = False) -> dict[int, Any]:
    """Parse a protobuf payload into `{field_number: value}`.

    Repeated fields are returned as a list. Nested messages are decoded
    recursively when the LEN payload happens to be valid wire format.
    """
    out: dict[int, Any] = {}
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field = tag >> 3
        wire = tag & 0x07
        if field == 0:
            raise ProtobufError(f"invalid field number 0 at offset {pos - 1}")

        if wire == WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
            # Fold the high-bit-set range back to signed two's complement so
            # int32/int64 fields decode to their natural negative value
            # (e.g. RSSI = -65 round-trips through 0xFFFFFFFFFFFFFFBF).
            # Real STIGA telemetry never uses uint64 values > 2^63, so this
            # is unambiguous; uint-style fields (battery %, RSRQ, …) all
            # stay below the threshold and pass through unchanged.
            if value >= (1 << 63):
                value -= 1 << 64
        elif wire == WIRE_FIXED64:
            if pos + 8 > len(buf):
                raise ProtobufError("truncated fixed64")
            value = buf[pos : pos + 8]
            pos += 8
        elif wire == WIRE_LEN:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise ProtobufError("truncated length-delimited")
            payload = buf[pos : pos + length]
            pos += length
            value = _decode_len(payload, fixed32_as_int=fixed32_as_int)
        elif wire == WIRE_FIXED32:
            if pos + 4 > len(buf):
                raise ProtobufError("truncated fixed32")
            chunk = buf[pos : pos + 4]
            pos += 4
            value = (
                struct.unpack("<i", chunk)[0] if fixed32_as_int else struct.unpack("<f", chunk)[0]
            )
        else:
            raise ProtobufError(f"unsupported wire type {wire} at field {field}")

        if field in out:
            existing = out[field]
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[field] = [existing, value]
        else:
            out[field] = value

    return out


def _decode_len(payload: bytes, *, fixed32_as_int: bool) -> Any:
    if _is_printable_utf8(payload):
        return payload.decode("utf-8")
    try:
        nested = decode(payload, fixed32_as_int=fixed32_as_int)
        if nested:
            return nested
    except ProtobufError:
        pass
    return payload


# ---------------------------------------------------------------- Encode


def encode(fields: dict[int, Any]) -> bytes:
    """Serialise a `{field_number: value}` mapping to protobuf wire format.

    Type handling mirrors matthewgream's encoder:

      * bool         -> VARINT (0/1)
      * int          -> VARINT
      * float        -> FIXED32 (single-precision float)
      * bytes        -> LEN (raw)
      * str          -> LEN (utf-8)
      * dict         -> LEN (recursive encode)
      * list/tuple   -> emitted as repeated field
    """
    out = bytearray()
    for field, value in fields.items():
        if not isinstance(field, int) or field <= 0:
            raise ProtobufError(f"invalid field number {field!r}")
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                _encode_one(out, field, item)
        else:
            _encode_one(out, field, value)
    return bytes(out)


def _encode_one(out: bytearray, field: int, value: Any) -> None:
    if isinstance(value, bool):
        _write_varint(out, (field << 3) | WIRE_VARINT)
        _write_varint(out, 1 if value else 0)
    elif isinstance(value, int):
        _write_varint(out, (field << 3) | WIRE_VARINT)
        _write_varint(out, value)
    elif isinstance(value, float):
        _write_varint(out, (field << 3) | WIRE_FIXED32)
        out.extend(struct.pack("<f", value))
    elif isinstance(value, (bytes, bytearray, memoryview)):
        _write_varint(out, (field << 3) | WIRE_LEN)
        payload = bytes(value)
        _write_varint(out, len(payload))
        out.extend(payload)
    elif isinstance(value, str):
        _write_varint(out, (field << 3) | WIRE_LEN)
        payload = value.encode("utf-8")
        _write_varint(out, len(payload))
        out.extend(payload)
    elif isinstance(value, dict):
        _write_varint(out, (field << 3) | WIRE_LEN)
        payload = encode(value)
        _write_varint(out, len(payload))
        out.extend(payload)
    else:
        raise ProtobufError(f"unsupported value type {type(value).__name__}")


# ---------------------------------------------------------------- Helpers


def read_double_le(value: bytes | None) -> float | None:
    """Reinterpret an 8-byte FIXED64 payload as IEEE 754 double (little-endian).

    Used for GPS lat/lon offsets and orientation in `LOG/ROBOT_POSITION` and
    the location subfields of `LOG/STATUS`. Returns ``None`` for missing or
    malformed input rather than raising — these fields can legitimately be
    absent on inactive mowers.
    """
    if not isinstance(value, (bytes, bytearray)) or len(value) != 8:
        return None
    return struct.unpack("<d", bytes(value))[0]


def hex_to_dict(hex_str: str, *, fixed32_as_int: bool = False) -> dict[int, Any]:
    """Convenience: parse a hex string (any whitespace ignored)."""
    cleaned = "".join(hex_str.split())
    return decode(bytes.fromhex(cleaned), fixed32_as_int=fixed32_as_int)


def dict_to_hex(fields: dict[int, Any]) -> str:
    """Convenience: encode and return uppercase hex."""
    return encode(fields).hex()

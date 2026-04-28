"""Tests for the minimal protobuf wire-format codec.

Vectors are computed by hand from the wire-format spec so we can roundtrip
against matthewgream/stiga-api without pulling in protobufjs as a test
dependency. Where matthewgream's codec produces a specific byte string for
a known frame (e.g. the STATUS_REQUEST command), we lock that exact bytes
here so future refactors don't silently change the on-wire shape.
"""

from __future__ import annotations

import struct

import pytest

from custom_components.stiga_mower import protobuf_codec as pb

# ---------------------------------------------------------------- Roundtrip


@pytest.mark.parametrize(
    ("fields", "expected_hex"),
    [
        # Single varint field: tag = (1<<3)|0 = 0x08, value 28 = 0x1C
        ({1: 28}, "081c"),
        # START command frame: {1: 1, 3: 1} (matthewgream's encodeRobotCommand
        # emits field 1 + field 3 echo when params=undefined)
        ({1: 1, 3: 1}, "08011801"),
        # STATUS_REQUEST with battery param: nested message in field 2
        ({1: 28, 2: {1: 1}, 3: 28}, "081c1202080118 1c".replace(" ", "")),
        # Nested message
        ({1: {1: 1, 2: 5}}, "0a0408011005"),
        # String field — printable utf-8 round-trips through LEN
        ({1: "hello"}, "0a0568656c6c6f"),
        # Empty mapping -> empty bytes
        ({}, ""),
    ],
)
def test_encode_known_vectors(fields: dict, expected_hex: str) -> None:
    assert pb.encode(fields).hex() == expected_hex
    # And decode roundtrips
    assert pb.decode(bytes.fromhex(expected_hex)) == fields


def test_repeated_field_collapses_to_list() -> None:
    encoded = pb.encode({1: [1, 2, 3]})
    # Three separate field-1 records
    assert encoded.hex() == "080108020803"
    assert pb.decode(encoded) == {1: [1, 2, 3]}


def test_bool_encodes_as_varint_0_or_1() -> None:
    assert pb.encode({1: True}).hex() == "0801"
    assert pb.encode({1: False}).hex() == "0800"
    # Decoding gives ints back (no schema means we can't tell bool from int)
    assert pb.decode(bytes.fromhex("0801")) == {1: 1}


def test_bytes_payload_preserved_when_non_utf8() -> None:
    raw = b"\xff\x00\xfe\x01"
    encoded = pb.encode({1: raw})
    # Tag = 0x0a, length = 4, then raw bytes
    assert encoded == bytes.fromhex("0a04") + raw
    decoded = pb.decode(encoded)
    assert decoded == {1: raw}


def test_float_uses_fixed32() -> None:
    encoded = pb.encode({1: 1.5})
    # Tag = (1<<3)|5 = 0x0d, payload = float32(1.5) = 0x3fc00000 LE
    assert encoded.hex() == "0d" + struct.pack("<f", 1.5).hex()
    decoded = pb.decode(encoded)
    assert decoded[1] == pytest.approx(1.5)


# ---------------------------------------------------------------- FIXED64


def test_fixed64_returned_as_raw_bytes() -> None:
    """Status frames keep FIXED64 raw so callers can decide between hex/double."""
    payload = struct.pack("<d", 50.123456789)
    # Tag = (1<<3)|1 = 0x09
    encoded = b"\x09" + payload
    decoded = pb.decode(encoded)
    assert decoded[1] == payload
    assert pb.read_double_le(decoded[1]) == pytest.approx(50.123456789)


def test_read_double_le_handles_missing_or_bad_input() -> None:
    assert pb.read_double_le(None) is None
    assert pb.read_double_le(b"") is None
    assert pb.read_double_le(b"\x00" * 7) is None  # wrong length
    assert pb.read_double_le(b"\x00" * 8) == 0.0


# ---------------------------------------------------------------- Errors


def test_truncated_varint_raises() -> None:
    with pytest.raises(pb.ProtobufError):
        pb.decode(b"\x80")  # continuation bit set, no follow-up byte


def test_truncated_length_delimited_raises() -> None:
    # Tag for LEN field 1, length 5, but only 2 payload bytes follow
    with pytest.raises(pb.ProtobufError):
        pb.decode(b"\x0a\x05ab")


def test_truncated_fixed64_raises() -> None:
    with pytest.raises(pb.ProtobufError):
        pb.decode(b"\x09\x00\x00")


def test_truncated_fixed32_raises() -> None:
    with pytest.raises(pb.ProtobufError):
        pb.decode(b"\x0d\x00\x00")


def test_field_zero_rejected() -> None:
    # Tag 0 (field 0, wire 0) is invalid in protobuf
    with pytest.raises(pb.ProtobufError):
        pb.decode(b"\x00\x00")


def test_encode_rejects_unknown_value_type() -> None:
    with pytest.raises(pb.ProtobufError):
        pb.encode({1: object()})


def test_encode_skips_none_values() -> None:
    """Mirrors matthewgream's encoder: undefined/None means 'omit field'."""
    assert pb.encode({1: 1, 2: None, 3: 1}).hex() == "08011801"


def test_negative_varint_round_trips_through_sign_folding() -> None:
    """int32/int64 fields encode as 10-byte VARINTs; decode folds back."""
    encoded = pb.encode({1: -65, 2: -1, 3: -2147483648})
    decoded = pb.decode(encoded)
    assert decoded == {1: -65, 2: -1, 3: -2147483648}
    # 0 stays 0; max positive int63 stays positive
    assert pb.decode(pb.encode({1: (1 << 62)})) == {1: (1 << 62)}


# ---------------------------------------------------------------- Helpers


def test_hex_to_dict_strips_whitespace() -> None:
    assert pb.hex_to_dict("08 1c") == {1: 28}
    assert pb.hex_to_dict("\n08\n1C\n") == {1: 28}


def test_dict_to_hex_round_trips_through_hex_to_dict() -> None:
    src = {1: 28, 2: {1: 1}, 3: 28}
    assert pb.hex_to_dict(pb.dict_to_hex(src)) == src

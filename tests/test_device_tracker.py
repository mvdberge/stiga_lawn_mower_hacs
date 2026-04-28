"""Tests for the device_tracker GPS position entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.stiga_mower.coordinator import StigaDataUpdateCoordinator
from custom_components.stiga_mower.device_tracker import (
    StigaPositionTracker,
    _offset_to_wgs84,
)

# ------------------------------------------------------------------ unit: _offset_to_wgs84


def test_zero_offsets_returns_base_position() -> None:
    lat, lon = _offset_to_wgs84(53.0, 10.0, 0.0, 0.0)
    assert lat == pytest.approx(53.0, abs=1e-7)
    assert lon == pytest.approx(10.0, abs=1e-7)


def test_positive_lat_offset_moves_north() -> None:
    base_lat, base_lon = 53.0, 10.0
    lat, lon = _offset_to_wgs84(base_lat, base_lon, lat_offset_cm=11111.0, lon_offset_cm=0.0)
    # 11 111 cm = 111.11 m ≈ 0.001° north
    assert lat > base_lat
    assert lon == pytest.approx(base_lon, abs=1e-6)


def test_positive_lon_offset_moves_east() -> None:
    base_lat, base_lon = 53.0, 10.0
    lat, lon = _offset_to_wgs84(base_lat, base_lon, lat_offset_cm=0.0, lon_offset_cm=11111.0)
    assert lon > base_lon
    assert lat == pytest.approx(base_lat, abs=1e-6)


def test_negative_offsets_move_southwest() -> None:
    base_lat, base_lon = 53.0, 10.0
    lat, lon = _offset_to_wgs84(base_lat, base_lon, lat_offset_cm=-5000.0, lon_offset_cm=-5000.0)
    assert lat < base_lat
    assert lon < base_lon


def test_lon_scale_depends_on_latitude() -> None:
    # At the equator cos(0°)=1; at 60° cos(60°)=0.5, so same cm shift = double deg shift.
    _, lon_eq = _offset_to_wgs84(0.0, 0.0, 0.0, 10_000.0)
    _, lon_60 = _offset_to_wgs84(60.0, 0.0, 0.0, 10_000.0)
    assert lon_60 == pytest.approx(lon_eq * 2, rel=1e-3)


# ------------------------------------------------------------------ integration helpers

import pytest  # noqa: E402  (needed after the functions that use it above)


def _make_tracker(hass, *, base_lat=None, base_lon=None, position_frame=None):
    api = MagicMock()
    api.get_token = AsyncMock(return_value="token")
    entry = MagicMock(data={"email": "e", "password": "p"})
    c = StigaDataUpdateCoordinator(hass, entry, api)

    last_position: dict | None = None
    if base_lat is not None and base_lon is not None:
        last_position = {"lat": base_lat, "lon": base_lon}

    c._devices = [
        {
            "attributes": {
                "uuid": "u1",
                "name": "Bot",
                "mac_address": "MAC1",
                "last_position": last_position,
            },
        }
    ]
    c.async_set_updated_data(c._build_data(rest_statuses={"u1": {"has_data": True}}))

    if position_frame is not None:
        c._live_position["MAC1"] = position_frame
        c.async_set_updated_data(c._build_data())

    device = c.data["devices"][0]
    tracker = StigaPositionTracker(c, device)
    return tracker


# ------------------------------------------------------------------ availability


def test_tracker_unavailable_when_no_position_frame(hass) -> None:
    t = _make_tracker(hass, base_lat=53.0, base_lon=10.0)
    assert t.available is False


def test_tracker_unavailable_when_no_base_position(hass) -> None:
    t = _make_tracker(
        hass,
        position_frame={"lat_offset_cm": 0.0, "lon_offset_cm": 0.0},
    )
    assert t.latitude is None
    assert t.longitude is None


# ------------------------------------------------------------------ position values


def test_tracker_lat_lon_with_zero_offset(hass) -> None:
    t = _make_tracker(
        hass,
        base_lat=53.0,
        base_lon=10.0,
        position_frame={"lat_offset_cm": 0.0, "lon_offset_cm": 0.0},
    )
    assert t.available is True
    assert t.latitude == pytest.approx(53.0, abs=1e-5)
    assert t.longitude == pytest.approx(10.0, abs=1e-5)


def test_tracker_lat_moves_with_offset(hass) -> None:
    t = _make_tracker(
        hass,
        base_lat=53.0,
        base_lon=10.0,
        position_frame={"lat_offset_cm": 111_111.0, "lon_offset_cm": 0.0},
    )
    assert t.latitude is not None
    assert t.latitude > 53.0


def test_tracker_lon_moves_with_offset(hass) -> None:
    t = _make_tracker(
        hass,
        base_lat=53.0,
        base_lon=10.0,
        position_frame={"lat_offset_cm": 0.0, "lon_offset_cm": 111_111.0},
    )
    assert t.longitude is not None
    assert t.longitude > 10.0


def test_tracker_returns_none_when_frame_lacks_offsets(hass) -> None:
    t = _make_tracker(
        hass,
        base_lat=53.0,
        base_lon=10.0,
        position_frame={"some_other_field": 1},
    )
    assert t.latitude is None
    assert t.longitude is None

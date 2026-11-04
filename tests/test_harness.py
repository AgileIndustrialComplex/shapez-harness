"""Tests for the harness's pure logic — no browser launched.

Everything that can be checked without shapez.io running is checked here: the
pixel<->tile calibration maths and the building-code table. The live loop is
exercised by `harness.py --expert place_extractors`, not by pytest, because CI
must not depend on a game server or a GPU.
"""
import pytest

from harness import BUILDING_CODES, ShapezHarness


@pytest.fixture
def h():
    """Harness instance with calibration injected, deliberately not booted."""
    obj = ShapezHarness.__new__(ShapezHarness)   # skip __init__ -> no browser
    obj.calibrated = True
    # Fit measured on this box: 41.18 px/tile across, 43.75 px/tile down, no skew.
    obj._fit_x = (1 / 41.18, 0.0, -23.714286)
    obj._fit_y = (0.0, 1 / 43.75, -6.857143)
    return obj


@pytest.mark.parametrize("tile", [(-2, -2), (-9, 5), (3, 8), (0, 0), (-14, 0)])
def test_tile_pixel_roundtrip(h, tile):
    px, py = h.pixel_from_tile(*tile)
    assert h.tile_from_pixel(px, py) == tile


def test_calibration_matches_known_points(h):
    """Two probe points measured live; the fit must reproduce them."""
    assert h.tile_from_pixel(400, 300) == (-14, 0)
    assert h.tile_from_pixel(1100, 650) == (3, 8)


def test_degenerate_calibration_is_rejected(h):
    h._fit_x = (0.0, 0.0, 0.0)
    h._fit_y = (0.0, 0.0, 0.0)
    with pytest.raises(RuntimeError):
        h.pixel_from_tile(1, 1)


def test_place_tile_requires_calibration():
    obj = ShapezHarness.__new__(ShapezHarness)
    obj.calibrated = False
    with pytest.raises(RuntimeError, match="calibrate"):
        obj.place_tile(0, 0)


def test_building_code_table():
    """Codes are MetaBuilding internalIds; these are the ones the demo uses."""
    assert BUILDING_CODES[1] == "belt"
    assert BUILDING_CODES[7] == "miner"
    assert BUILDING_CODES[26] == "hub"
    assert BUILDING_CODES[9] == "cutter"
    # No duplicate ids — a collision would silently mislabel entities.
    assert len(set(BUILDING_CODES)) == len(BUILDING_CODES)

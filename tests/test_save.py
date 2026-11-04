"""Tests for the savegame codec — pure Python, no browser, no game installed.

The charmap is the highest-risk part of this repo: a 146-character string whose
*order* defines every key and string in the file. These tests pin it in both
directions so a typo cannot pass.

The end-to-end test runs against `tests/fixtures/level1.bin`, a real blob
captured from the browser build (level 1: hub + 1 extractor + 9 belt tiles).
"""
import json
from pathlib import Path

import lzstring
import pytest

import shapez_save as ss

FIXTURE = Path(__file__).parent / "fixtures" / "level1.bin"


def test_charmap_is_stable():
    """Exactly 146 unique chars; order is load-bearing, so pin the landmarks."""
    assert len(ss.CHARMAP) == 146
    assert len(set(ss.CHARMAP)) == 146
    assert ss.CHARMAP[0] == "!"
    assert ss.CHARMAP[1] == "#"
    assert ss.CHARMAP[2] == "%"
    assert ss.CHARMAP[-1] == "Z"


@pytest.mark.parametrize("value", [0, 1, 2, 3, 25, 60, 61, 62, 99, 145, 146, 1000, 10000])
def test_int_roundtrip(value):
    assert ss.decompress_int(ss.compress_int(value)) == value


@pytest.mark.parametrize(
    "value,encoded",
    # compressInt() adds 1 before encoding, so value 0 is '#' (index 1), not '!'.
    [(0, "#"), (1, "%"), (2, "&"), (145, "!#"), (146, "##"), (292, "#%")],
)
def test_int_encoding_matches_spec(value, encoded):
    """compressInt() is (i+1) in base 146 — hand-computable, so assert exactly."""
    assert ss.compress_int(value) == encoded
    assert ss.decompress_int(encoded) == value


def test_unknown_char_raises():
    with pytest.raises(ValueError):
        ss.decompress_int(" ")


def test_nested_roundtrip():
    """decompressObject() must substitute both keys and interned string values."""
    payload = {
        "keys": ["level", "entities", "uid", "name"],
        "values": ["regular"],
        "data": {
            ss.compress_int(0): 7,                          # level: 7
            ss.compress_int(1): [                           # entities: [...]
                {
                    ss.compress_int(2): 5,                  # uid: 5
                    ss.compress_int(3): ss.compress_int(0),  # name: "regular"
                }
            ],
        },
    }
    assert ss.decompress(payload) == {"level": 7, "entities": [{"uid": 5, "name": "regular"}]}


def test_decompress_is_idempotent_on_plain_dicts():
    plain = {"level": 1, "entities": []}
    assert ss.decompress(plain) is plain


def test_bad_lz_payload_raises():
    with pytest.raises(ValueError):
        ss.decode_blob("\x01not-valid-lz")


def test_crc_header_is_stripped():
    """`crc32---<hex>` precedes the LZ payload and is not part of it."""
    body = json.dumps({"keys": ["level"], "values": [], "data": {ss.compress_int(0): 4}})
    blob = ("\x01crc32---------------------------deadbeef"
            + lzstring.LZString().compressToEncodedURIComponent(body))
    assert ss.decode_blob(blob) == {"level": 4}


def test_real_fixture_end_to_end():
    """The whole pipeline against a real capture — names, not single chars."""
    game = ss.decode_blob(FIXTURE.read_bytes())
    assert set(game) >= {"version", "dump"}
    dump = game["dump"]
    assert set(dump) >= {"camera", "entities", "hubGoals", "beltPaths"}

    # Field names survived decompression.
    first = dump["entities"][0]
    assert first["components"]["StaticMapEntity"]["origin"] == {"x": -2, "y": -2}

    s = ss.summarize(game)
    assert s["entities"] == 11
    assert s["level"] == 1
    # hub + extractor + 9 belts
    assert s["by_code"] == {26: 1, 7: 1, 1: 9}
    assert s["camera"]["zoomLevel"] == pytest.approx(1.3368)


def test_summarize_reads_dump_not_index():
    """A save *index* has no `dump`; a map save nests everything under it.

    Getting this wrong reports 0 entities for a real game — exactly the false
    observation a harness must never produce.
    """
    game = {
        "version": 1010,
        "dump": {
            "camera": {"zoomLevel": 1.3},
            "hubGoals": {"level": 3, "storedShapes": {}},
            "beltPaths": [[1, 2]],
            "entities": [
                {"uid": 1, "components": {"StaticMapEntity": {"origin": {"x": 0, "y": 0}, "code": 26}}},
                {"uid": 2, "components": {"StaticMapEntity": {"origin": {"x": 5, "y": -2}, "code": 7}}},
                {"uid": 3, "components": {"StaticMapEntity": {"origin": {"x": 6, "y": -2}, "code": 1}}},
            ],
        },
    }
    s = ss.summarize(game)
    assert s["entities"] == 3
    assert s["level"] == 3
    assert s["by_code"] == {26: 1, 7: 1, 1: 1}
    assert s["origins"] == [(0, 0), (5, -2), (6, -2)]
    assert s["belt_paths"] == 1
    assert s["camera"] == {"zoomLevel": 1.3}


def test_summarize_tolerates_missing_components():
    game = {"dump": {"entities": [{"uid": 1}, {"uid": 2, "components": {}}]}}
    s = ss.summarize(game)
    assert s["entities"] == 2
    assert s["by_code"] == {}
    assert s["origins"] == [None, None]

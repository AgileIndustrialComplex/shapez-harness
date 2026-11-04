"""shapez.io savegame decoding — pure Python.

The on-disk blob (IndexedDB `app_storage` key `savegame-*.bin`) is:

    \\x01  LZ-string(URI-encoded)  crc32-------<hex>  <json>

`<json>` is `{keys, values, data}` and is NOT a plain object: every key and
string value is an integer compressed into a character via the game's own
charmap. This module ports `src/js/savegame/savegame_compressor.js` exactly, so
`decompress()` returns the same structure the game serialised — real field names,
real strings, no single-char placeholders.
"""
from __future__ import annotations

import json
import re

try:
    import lzstring
except ImportError as exc:  # pragma: no cover
    raise ImportError("pip install lzstring") from exc

# Verbatim from src/js/savegame/savegame_compressor.js. Order is load-bearing:
# compressInt() indexes this string, so any reordering corrupts every key.
CHARMAP = (
    "!#%&'()*+,-./:;<=>?@[]^_`{|}~¥¦§¨©ª«¬\u00ad®¯°±²³´µ¶·¸¹º»¼½¾¿"
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖ×ØÙÚÛÜÝÞß"
    "àáâãäåæçèéêëìíîïðñòóôõö÷øùúûüýþÿ"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)

_CHAR_TO_INDEX = {ch: i for i, ch in enumerate(CHARMAP)}

_MAGIC = "\x01"
# Header is `crc32` + dashes + an 8-hex-digit digest, then the LZ payload:
#   crc32---------------------------52ba3941{...lz...}
# The digest must be consumed too — stripping only `crc32-*` leaves the hex
# glued to the payload and every decode fails.
_CRC = re.compile(r"^crc32-*[0-9a-f]{8}")


def decompress_int(s: str) -> int:
    """Port of decompressInt(): little-endian base-len(charmap) digits, minus 1."""
    if not s:
        raise ValueError("empty compressed int")
    result = 0
    for ch in reversed(s):
        try:
            digit = _CHAR_TO_INDEX[ch]
        except KeyError:
            raise ValueError(f"character {ch!r} is not in the shapez charmap") from None
        result = result * len(CHARMAP) + digit
    return result - 1


def compress_int(i: int) -> str:
    """Port of compressInt(), for round-trip tests and writing saves back."""
    i += 1
    out = []
    while True:
        out.append(CHARMAP[i % len(CHARMAP)])
        i //= len(CHARMAP)
        if i <= 0:
            break
    return "".join(out)


def _decode(node, keys, values):
    if isinstance(node, list):
        return [_decode(v, keys, values) for v in node]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            out[keys[decompress_int(k)]] = _decode(v, keys, values)
        return out
    if isinstance(node, str):
        return values[decompress_int(node)]
    return node


def decompress(payload: dict):
    """Port of decompressObject(), tolerant of already-plain input."""
    if isinstance(payload, dict) and {"keys", "values", "data"} <= payload.keys():
        return _decode(payload["data"], payload["keys"], payload["values"])
    return payload


def decode_blob(raw: bytes | str) -> dict:
    """Full pipeline: raw IndexedDB contents -> fully expanded game dict."""
    s = raw.decode("latin1") if isinstance(raw, (bytes, bytearray)) else raw
    if s.startswith(_MAGIC):
        s = s[1:]
    # The crc32 header is not part of the LZ payload; LZ-URI encoding never
    # begins with "crc32", so trimming it is unambiguous.
    s = _CRC.sub("", s)
    text = lzstring.LZString().decompressFromEncodedURIComponent(s)
    if not text:
        raise ValueError("LZ-string payload did not decompress")
    return decompress(json.loads(text[text.index("{"):]))


def summarize(game: dict) -> dict:
    """Compact, policy-friendly view of a decoded savegame.

    The interesting state lives under `dump` (the save *index* — the
    `savegames.bin` list — has no `dump` key and summarises to empty).
    """
    d = game.get("dump") if isinstance(game.get("dump"), dict) else game
    entities = d.get("entities") or []
    by_code: dict[int, int] = {}
    origins = []
    for e in entities:
        sme = (e.get("components") or {}).get("StaticMapEntity") or {}
        code = sme.get("code")
        if code is not None:
            by_code[code] = by_code.get(code, 0) + 1
        o = sme.get("origin")
        origins.append((o.get("x"), o.get("y")) if o else None)
    goals = d.get("hubGoals") or {}
    return {
        "level": goals.get("level"),
        "entities": len(entities),
        "by_code": by_code,
        "origins": origins,
        "storedShapes": goals.get("storedShapes"),
        "upgrades": goals.get("upgradeLevels"),
        "camera": d.get("camera"),
        "belt_paths": len(d.get("beltPaths") or []),
    }

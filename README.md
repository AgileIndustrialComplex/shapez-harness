# shapez-harness

Give a model eyes and hands in [shapez.io](https://shapez.io): read the world from the
savegame, place buildings with real input, score progress from the game's own state.

Verified end-to-end on a GPU-less Linux box against the live browser demo: the scripted
expert places 1 extractor + 10 belt tiles, and the savegame confirms
`{hub: 1, miner: 1, belt: 10}`.

```
$ python harness.py --expert place_extractors
{
 "entities_before": 1,
 "entities_after": 12,
 "buildings_before": {"hub": 1},
 "buildings_after": {"hub": 1, "miner": 1, "belt": 10},
 "placed": 11
}
```

## Why it's built this way

**Observation is the savegame, not pixels.** The whole world is LZ-string-compressed JSON in
IndexedDB. Decoded, it's a real structure: `dump.entities[]` with `StaticMapEntity.code`,
`origin`, `rotation`; `dump.hubGoals.level`; `dump.beltPaths`. Scoring needs no vision model, and
a policy can act on exact tile coordinates. Screenshots are still available for a vision-based
policy — they're just not the only channel.

**Actuation must be trusted input.** shapez binds raw `window` `mousedown`/`keydown` and never
uses DOM click handlers for building placement. `element.click()` silently does nothing: you get
a ghost preview that never commits. Only `page.mouse` / `page.keyboard` work.

**Calibration replaces pixel guessing.** `calibrate()` places four belt probes at known screen
points, reads their tile origins back out of the savegame, and least-squares-fits an affine
pixel↔tile map (41.2 px/tile across, 43.8 px/tile down on this box, zero round-trip error). A
policy can then say "place at tile (-8, 7)" instead of hardcoding pixels that break on any camera
move.

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
```

## Use

```bash
.venv/bin/python harness.py --expert place_extractors           # headless
.venv/bin/python harness.py --expert place_extractors --headful # watch it play
.venv/bin/python harness.py --expert calibrate
.venv/bin/python harness.py --expert observe
```

```python
from harness import ShapezHarness

with ShapezHarness() as h:
    h.calibrate()                    # once per session, per camera position
    h.select_building(3)             # toolbar slot; 3 = extractor on level 1
    h.place_tile(-8, 7)              # world coordinates
    h.cancel_placement()             # right-click, never Escape
    print(h.save())                  # {entities, buildings, by_code, origins, level, ...}
```

## Writing a policy

One loop iteration is one decision:

```python
with ShapezHarness() as h:
    h.calibrate()
    while True:
        obs = h.observe()                # screenshot + decoded state + tutorial text
        action = policy(obs)             # LLM, RL net, or a scripted planner
        apply(h, action)
        reward = score(h.save())         # level, delivered shapes, building counts
```

`obs["tutorial"]` is worth pointing out: it's the game's own instruction text and it advances as
you succeed, which makes it a usable natural-language task spec and reward signal for an LLM
agent without hand-writing a curriculum.

## The savegame format

```
\x01 | LZ-string(URI-encoded) | crc32---<8 hex> | {keys, values, data}
```

`data` is not a plain object. Every key and string value is an integer encoded through a
146-character charmap (`src/js/savegame/savegame_compressor.js`), so `data` is full of
single-char keys like `#`, `%`, `&`. `shapez_save.py` ports `decompressInt()`/`compressInt()`
exactly and expands the result, so you get `dump.entities[0].components.StaticMapEntity.code`
rather than `data['%']['}'][0]['¥']['¦']['ª']`.

Two details that cost real time:

* the charmap **order** is load-bearing — `compressInt()` adds 1 then does base-146 digits, so
  value `0` is `#` (index 1), not `!`;
* the CRC header includes an 8-hex digest that must be stripped along with `crc32---`, or the
  payload has eight junk chars glued to the front and every decode fails.

Building codes are `MetaBuilding.getAllVariantCombinations().internalId`:
`1`=belt `7`=miner `9`=cutter `11`=rotater `26`=hub `27`=wire (`BUILDING_CODES` has the table).

## Gotchas found the hard way

* **`Escape` is a trap.** In placement mode it cancels the build; *outside* placement it opens
  the pause menu, whose scrim makes every later save click time out. Cancel with right-click.
* **`--use-gl=swiftshader`** is required for headless Chromium on a host with no GPU.
* The savegame key doesn't exist until the first save of a fresh map, and the write is async —
  `save()` retries rather than reporting a stale blob as "0 entities".
* Belt drags need real intermediate `mouse.move` steps; one `move` + `up` produces nothing.
* `/` zoom and `WASD` pan change the pixel↔tile mapping — recalibrate after moving the camera.
* Building tooltips render **on the canvas**, so slot meaning can't be read from the DOM.
  Derive it from `state["buildings"]` after placing one of each.
* Level 1 unlocks two buildings: toolbar slot 0 (belt) and slot 3 (extractor). Slot indices are
  toolbar positions, not hotkey numbers, and they shift as levels unlock.

## Steam / standalone build (a better target)

The web demo is a stripped production build: `window.shapez` and `GLOBAL_APP` are only assigned
when `G_IS_DEV || G_IS_STANDALONE` (`src/js/mods/modloader.js:exposeExports`), so on the browser
you get pixels + savegame and nothing else. The Electron build exposes the whole game:

```bash
./shapez --remote-debugging-port=9222 --dev
```

```python
browser = p.chromium.connect_over_cdp("http://localhost:9222")
page = browser.contexts[0].pages[0]
page.evaluate("typeof window.shapez")     # "object"
```

`window.shapez` is a live-getter map of every module export, so a policy can call
`map.placeStaticEntity()` directly, read `stateMgr`, or register a `GameSystem` — no pixels
involved. `--dev` also opens the mods folder, so `mods/*.js` can install hooks that log every
action to disk for offline training. That's the better foundation for serious work; this repo
targets the browser build because it needs no Steam account.

## Tests

```bash
.venv/bin/python -m pytest tests -q     # 37 passed
```

CI covers the savegame codec (charmap round-trip in both directions, a real captured fixture,
nested decompression, CRC stripping) and the calibration maths. **Nothing in CI touches a live
game** — driving shapez.io needs a browser session and is run manually.

## Status

Working: observation, calibration, placement of extractors and belts, scoring.

Not done: completing tutorial step 2. Belts reach the hub column but the game hasn't registered
the connection and `0/10` never moves; the drag produces belt entities with corner rotations
rather than a routed path into a hub face. That's a path-planning gap in the expert, not a
harness limitation.

## Licence

MIT. shapez.io itself is MIT-licensed by tobspr Games; this repo ships no game assets. The
test fixture under `tests/fixtures/` is a savegame produced by playing the game, not extracted
content.

"""shapez.io agent harness: observe, act, score.

Two channels, chosen deliberately (see README):

  * observation -- the savegame, fully decoded to real field names by
    `shapez_save`. No vision model needed to know the world.
  * actuation   -- trusted mouse/keyboard. shapez binds raw `window` events for
    building placement, so `element.click()` is silently ignored.

Requires: playwright (+ `playwright install chromium`), lzstring.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

import shapez_save

URL = "https://shapez.io/"
HERE = os.path.dirname(os.path.abspath(__file__))

# StaticMapEntity.code -> MetaBuilding.getAllVariantCombinations().internalId.
# Source: src/js/game/buildings/*.js. First variant of each; further variants
# (belt 2-3, painter 17-19, ...) are reported numerically.
BUILDING_CODES = {
    1: "belt", 4: "balancer", 7: "miner", 9: "cutter", 11: "rotater",
    14: "stacker", 15: "mixer", 16: "painter", 20: "trash", 21: "storage",
    22: "underground_belt", 26: "hub", 27: "wire", 31: "constant_signal",
    33: "lever", 37: "filter", 39: "wire_tunnel", 40: "display",
    43: "analyzer", 46: "comparator", 49: "reader", 61: "item_producer",
    64: "block",
}

# Newest `savegame-*.bin` blob (the map); `savegames.bin` is only the index.
READ_SAVE = """async () => {
  const open = n => new Promise((res, rej) => {
    const r = indexedDB.open(n); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  const db = await open('app_storage');
  const os = db.transaction('files', 'readonly').objectStore('files');
  const keys = await new Promise(res => {
    const q = os.getAllKeys(); q.onsuccess = () => res(q.result);
  });
  let best = null, bestLen = -1;
  for (const k of keys) {
    if (!k.startsWith('savegame-')) continue;
    const v = await new Promise(res => {
      const q = os.get(k); q.onsuccess = () => res(q.result);
    });
    if (v && v.contents && v.contents.length > bestLen) { bestLen = v.contents.length; best = v.contents; }
  }
  return best;
}"""

SLOTS = """() => [...document.querySelectorAll('#ingame_HUD_BuildingsToolbar .building')]
    .map(e => { const r = e.getBoundingClientRect();
                return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                        unlocked: e.className.includes('unlocked'),
                        selected: e.className.includes('selected')}; })"""

MENU_OPEN = """() => {
  const m = document.querySelector('#ingame_HUD_GameMenu');
  return !!(m && m.offsetParent !== null && m.getBoundingClientRect().height > 50);
}"""


class ShapezHarness:
    """One chromium page driving shapez.io."""

    def __init__(self, headless: bool = True, width: int = 1512, height: int = 770):
        self.width, self.height = width, height
        self.step = 0
        self.calibrated = False
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=headless,
            # Software GL: required on hosts with no GPU (this box has none).
            args=["--use-gl=swiftshader", "--disable-dev-shm-usage"],
        )
        self.page = self.browser.new_page(viewport={"width": width, "height": height})

    # ------------------------------------------------------------ lifecycle
    def boot(self) -> "ShapezHarness":
        self.page.goto(URL, wait_until="networkidle")
        self.page.wait_for_timeout(4000)
        for label in ("PLAY", "CONTINUE"):
            btn = self.page.query_selector(f"button.playButton:has-text('{label}')")
            if btn:
                btn.click()
                break
        else:
            self.page.click("text=NEW GAME")
        self.page.wait_for_selector("#ingame_Canvas", timeout=60000)
        self.page.wait_for_timeout(5000)
        return self

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def __enter__(self) -> "ShapezHarness":
        return self.boot()

    def __exit__(self, *exc) -> None:
        self.close()

    # --------------------------------------------------------- observation
    def observe(self, screenshot: bool = True) -> dict:
        """One policy step: screenshot path + DOM hints + decoded world state."""
        self.step += 1
        out: dict = {
            "step": self.step,
            "tutorial": self._txt("#ingame_HUD_InteractiveTutorial"),
            "hud": self._txt("#state_InGameState", 300),
            "slots": self.page.evaluate(SLOTS),
            "state": self.read_state(),
        }
        if screenshot:
            path = os.path.join(HERE, f"obs_{self.step:03d}.png")
            self.page.screenshot(path=path)
            out["screenshot"] = path
        return out

    def _txt(self, sel: str, n: int = 200) -> str:
        el = self.page.query_selector(sel)
        return re.sub(r"\s+", " ", el.inner_text())[:n] if el else ""

    def read_state(self) -> dict:
        """Decode the current savegame. Never masks a failure as an empty world."""
        raw = self.page.evaluate(READ_SAVE) or ""
        if len(raw) < 50:
            return {"error": "no savegame blob yet", "entities": None}
        try:
            game = shapez_save.decode_blob(raw.encode("latin1"))
        except Exception as exc:
            return {"error": f"decode failed: {exc}", "entities": None}
        s = shapez_save.summarize(game)
        s["buildings"] = {BUILDING_CODES.get(c, f"code_{c}"): n for c, n in s["by_code"].items()}
        return s

    def save(self, settle: int = 2500, attempts: int = 3) -> dict:
        """Click save (forces serialisation) then read. Retries because the write
        is async and the key is absent until the first save of a map."""
        st: dict = {}
        for _ in range(attempts):
            self.close_menu()
            try:
                self.page.click(".button.save", timeout=4000)
                self.page.wait_for_timeout(settle)
            except Exception:
                pass
            st = self.read_state()
            if st.get("entities") is not None:
                return st
            self.page.wait_for_timeout(1000)
        return st

    # --------------------------------------------------------------- menus
    def menu_open(self) -> bool:
        return bool(self.page.evaluate(MENU_OPEN))

    def close_menu(self) -> None:
        """Escape OPENS the pause menu when not placing, and its scrim makes every
        later save click time out -- so only send Escape when it is really open."""
        if self.menu_open():
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(600)

    def cancel_placement(self) -> None:
        """Right-click cancels a build. Never Escape -- see close_menu()."""
        self.page.mouse.click(self.width // 2, 300, button="right")
        self.page.wait_for_timeout(400)

    # ---------------------------------------------------------- calibration
    def calibrate(self, probes=((400, 300), (1100, 300), (400, 650), (1100, 650))) -> dict:
        """Fit pixel<->tile by placing belt probes and reading their origins back.

        Lets a policy reason in the savegame's own tile coordinates instead of
        guessing pixels. Mutates the map (leaves a few belts behind).
        """
        base = self.read_state()
        known = {tuple(o) for o in base.get("origins") or [] if o}

        self.select_building(0)  # belt is placeable on empty ground
        self.page.wait_for_timeout(500)
        samples: list[tuple[int, int, tuple[int, int]]] = []
        for px, py in probes:
            self.page.mouse.move(px, py)
            self.page.wait_for_timeout(220)
            self.page.mouse.click(px, py)
            self.page.wait_for_timeout(600)
            st = self.save(settle=1500, attempts=2)
            for o in st.get("origins") or []:
                if o and tuple(o) not in known:
                    known.add(tuple(o))
                    samples.append((px, py, tuple(o)))
        self.cancel_placement()

        if len(samples) < 3:
            raise RuntimeError(f"calibration failed: only {len(samples)} probes landed")

        import statistics

        def fit(idx: int) -> tuple[float, float, float]:
            xs = [s[0] for s in samples]
            ys = [s[1] for s in samples]
            ts = [s[2][idx] for s in samples]
            mx, my, mt = statistics.mean(xs), statistics.mean(ys), statistics.mean(ts)
            sxx = sum((x - mx) ** 2 for x in xs) or 1e-9
            syy = sum((y - my) ** 2 for y in ys) or 1e-9
            sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            sxt = sum((x - mx) * (t - mt) for x, t in zip(xs, ts))
            syt = sum((y - my) * (t - mt) for y, t in zip(ys, ts))
            det = sxx * syy - sxy * sxy
            if abs(det) < 1e-9:
                return (sxt / sxx, 0.0, mt - (sxt / sxx) * mx)
            a = (sxt * syy - syt * sxy) / det
            b = (syt * sxx - sxt * sxy) / det
            return (a, b, mt - a * mx - b * my)

        self._fit_x, self._fit_y = fit(0), fit(1)
        self.calibrated = True
        return {"samples": samples, "fit_x": self._fit_x, "fit_y": self._fit_y}

    def tile_from_pixel(self, px: float, py: float) -> tuple[int, int]:
        ax, bx, cx = self._fit_x
        ay, by, cy = self._fit_y
        return round(ax * px + bx * py + cx), round(ay * px + by * py + cy)

    def pixel_from_tile(self, tx: float, ty: float) -> tuple[int, int]:
        ax, bx, cx = self._fit_x
        ay, by, cy = self._fit_y
        det = ax * by - bx * ay
        if abs(det) < 1e-12:
            raise RuntimeError("degenerate calibration; rerun calibrate()")
        u, v = tx - cx, ty - cy
        return round((by * u - bx * v) / det), round((-ay * u + ax * v) / det)

    # ------------------------------------------------------------- actions
    def select_building(self, slot: int = 0) -> dict:
        s = self.page.evaluate(SLOTS)[slot]
        self.page.mouse.click(s["x"], s["y"])
        self.page.wait_for_timeout(600)
        return s

    def place(self, px: int, py: int) -> None:
        self.page.mouse.move(px, py)
        self.page.wait_for_timeout(250)
        self.page.mouse.click(px, py)
        self.page.wait_for_timeout(700)

    def place_tile(self, tx: int, ty: int) -> tuple[int, int]:
        """Place at a tile coordinate. Requires calibrate()."""
        if not self.calibrated:
            raise RuntimeError("call calibrate() first")
        px, py = self.pixel_from_tile(tx, ty)
        self.place(px, py)
        return px, py

    def drag(self, x0: int, y0: int, x1: int, y1: int, steps: int = 15) -> None:
        """Belt runs need a real drag with intermediate moves; a single
        move+up produces nothing at all."""
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        for i in range(1, steps + 1):
            self.page.mouse.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
            self.page.wait_for_timeout(80)
        self.page.mouse.up()
        self.page.wait_for_timeout(1200)

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(300)


# ------------------------------------------------------------------ experts
# Level 1 unlocks two buildings. These are TOOLBAR positions, not hotkey numbers,
# and they shift as levels unlock -- re-derive from state["buildings"] later on.
EXTRACTOR_SLOT = 3
BELT_SLOT = 0


def expert_place_extractors(h: ShapezHarness) -> dict:
    """Place an extractor on the circle field, then belt toward the hub."""
    before = h.save()
    h.select_building(EXTRACTOR_SLOT)
    for xy in [(500, 400), (700, 400), (500, 560)]:
        h.place(*xy)
    h.cancel_placement()
    h.page.wait_for_timeout(600)
    mid = h._txt("#ingame_HUD_InteractiveTutorial")

    h.select_building(BELT_SLOT)
    h.page.wait_for_timeout(500)
    h.drag(600, 540, 1010, 540)
    h.cancel_placement()
    h.page.mouse.move(1300, 120)
    h.page.wait_for_timeout(1500)

    after = h.save()
    return {
        "entities_before": before.get("entities"),
        "entities_after": after.get("entities"),
        "buildings_before": before.get("buildings"),
        "buildings_after": after.get("buildings"),
        "placed": (after.get("entities") or 0) - (before.get("entities") or 0),
        "tutorial_after_extractors": mid,
        "tutorial_after_belt": h._txt("#ingame_HUD_InteractiveTutorial"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="shapez.io agent harness")
    ap.add_argument("--expert", default="place_extractors",
                    choices=["place_extractors", "calibrate", "observe"])
    ap.add_argument("--headful", action="store_true")
    a = ap.parse_args(argv)

    with ShapezHarness(headless=not a.headful) as h:
        if a.expert == "calibrate":
            print(json.dumps(h.calibrate(), indent=1))
        elif a.expert == "observe":
            print(json.dumps(h.observe(), indent=1)[:1500])
        else:
            print(json.dumps(expert_place_extractors(h), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

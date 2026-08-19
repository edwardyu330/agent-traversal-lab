"""Phase 2 — generate `agent_raw_cdp` labeled sessions: raw Playwright/CDP driving
the /arcade gauntlet, no LLM reasoning involved. This is the "fast, mechanical"
baseline agent profile to contrast against Browser Use (LLM-driven) and real
human sessions — on the SAME metric set humans produce, not the old storefront
flow, which would leave human data on the new arcade metrics and agent data
only on the old ones with nothing comparable.

/arcade auto-starts (skips the landing page's Start button) when it sees
?label=agent_raw_cdp — see arcade.js's resolveLabel(). No reveal-form submission
needed: arcade.html auto-submits /api/reveal for any generator-labeled session
once it finishes (see renderReveal()'s GENERATOR_CLAIMED_TYPE branch) — this
script doesn't have to do anything itself for that to happen.

--speed varies typing delay and adds a small paced pause before each click,
independent of each other: "fast" (delay=0, no pause) is the original,
maximally-mechanical baseline; "slow" paces itself like it's being deliberate
without ever being less than perfectly uniform (arcade_metrics.cadence_cv
should still read near-zero regardless of speed — this exists to check that
the detector is actually keying on UNIFORMITY, not raw speed, per its own
docstring). "random" (the --count>1 default) picks one of the three per
session so a single batch run naturally covers the range instead of needing
three separate invocations.
"""

import argparse
import math
import random
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

from agent_runner.common import SERVER_URL

C4_POLL_WINDOW_S = 11
C4_POLL_INTERVAL_MS = 80
A4_LEVEL_COUNT = 4

SPEED_PROFILES = {
    "fast": {"type_delay": 0, "pause_ms": 0},
    "normal": {"type_delay": 15, "pause_ms": 0},
    "slow": {"type_delay": 70, "pause_ms": 280},
}


def check_server(base_url: str) -> None:
    try:
        urllib.request.urlopen(base_url, timeout=3)
    except urllib.error.URLError:
        sys.exit(
            f"Can't reach {base_url}. Start the test site first:\n"
            f"  python -m test_site.server"
        )


def run_one_session(playwright, base_url: str, headless: bool, speed: str = "normal") -> None:
    profile = SPEED_PROFILES[speed]
    pause = profile["pause_ms"]
    type_delay = profile["type_delay"]

    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page(viewport={"width": 1000, "height": 800})
    try:
        page.goto(f"{base_url}/arcade?label=agent_raw_cdp")

        # C1 — flash reaction. Any click ends the stage (false start if too
        # early); no point racing the randomized decoy timing.
        page.wait_for_selector(".arcade-canvas", timeout=8000)
        if pause:
            page.wait_for_timeout(pause)
        page.click(".arcade-canvas")

        # A1 — perception probe. Floating shapes never satisfy Playwright's
        # actionability "stable" check, so force=True (see CLAUDE.md's note on
        # this being a real signal, not just a testing footgun). Click whichever
        # shape the *visible* instruction names — the plain, non-discriminating
        # channel a mechanical script would naturally default to reading.
        page.wait_for_selector(".arcade-shape-field .arcade-shape", timeout=8000)
        visible_text = page.text_content(".arcade-stage-a1 .arcade-instruction")
        kind = visible_text.strip().split()[-1].lower()
        if pause:
            page.wait_for_timeout(pause)
        page.click(f'.arcade-shape[data-kind="{kind}"]', force=True)

        # C4 — go/no-go whack-a-mole. Moles appear/vanish in well under a
        # second; poll and click whatever's up. Not paced even in "slow" — the
        # round budget is already tight against MOLE_VISIBLE_MS, and pacing
        # this stage would just mean missing more rounds, not looking more human.
        page.wait_for_selector(".arcade-mole-grid", timeout=8000)
        deadline = time.time() + C4_POLL_WINDOW_S
        while time.time() < deadline:
            mole = page.query_selector(".arcade-mole")
            if mole:
                try:
                    mole.click(force=True, timeout=500)
                except Exception:
                    pass
            page.wait_for_timeout(C4_POLL_INTERVAL_MS)
            if page.query_selector(".arcade-box-row"):
                break

        # A2 — visual vs. DOM order. Read the instructed ordinal ("3rd") and
        # click that DOM SOURCE position — the mechanical, position-blind
        # choice: reads the number, applies it to source order, never looks
        # at rendered layout. Was hardcoded to always click dom-index 1
        # regardless of the instructed ordinal, which only actually landed on
        # the DOM-order trap position when instructedN happened to be 1 (1/6
        # of the time) — the rest of the time it was a genuinely off-target
        # click, invisible under the old "any click ends the stage" rule but
        # a real bug once wrong clicks reset the round instead.
        page.wait_for_selector(".arcade-order-box", timeout=8000)
        if pause:
            page.wait_for_timeout(pause)
        instruction_text = page.text_content(".arcade-stage-a2 .arcade-instruction")
        ordinal_match = re.search(r"(\d+)(st|nd|rd|th)", instruction_text)
        dom_index = ordinal_match.group(1) if ordinal_match else "1"
        page.click(f'.arcade-order-box[data-dom-index="{dom_index}"]')

        # B1 — layout shift. Move toward the target's initial position (this is
        # what actually triggers the shift via the ambient pointermove
        # listener), then click wherever it ends up.
        page.wait_for_selector(".arcade-shift-target", timeout=8000)
        box = page.eval_on_selector(
            ".arcade-shift-target",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; }",
        )
        page.mouse.move(50, 50)
        page.mouse.move(box["x"], box["y"], steps=25)
        page.wait_for_timeout(150)
        box2 = page.eval_on_selector(
            ".arcade-shift-target",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left + r.width/2, y: r.top + r.height/2}; }",
        )
        page.mouse.click(box2["x"], box2["y"])

        # B2 — draw the shape. Trace a mathematically perfect circle via
        # discrete mouse.move steps while the button is held — mechanical on
        # purpose, same posture as every other stage this script drives; a
        # too-perfect trace is itself informative (see draw_shape_mean_deviation_px).
        page.wait_for_selector(".arcade-draw-canvas", timeout=8000)
        box = page.eval_on_selector(
            ".arcade-draw-canvas",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        ccx, ccy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        draw_radius = 110  # matches RADIUS in b2_draw_shape.js
        page.mouse.move(ccx + draw_radius, ccy)
        page.mouse.down()
        DRAW_STEPS = 36
        for i in range(1, DRAW_STEPS + 1):
            angle = 2 * math.pi * i / DRAW_STEPS
            page.mouse.move(ccx + draw_radius * math.cos(angle), ccy + draw_radius * math.sin(angle))
        page.mouse.up()

        # A5 — type the phrase (visible, no memory) + scattered words.
        page.wait_for_selector(".arcade-phrase-display", timeout=8000)
        phrase = page.text_content(".arcade-phrase-display").strip()
        page.type(".arcade-type-input", phrase, delay=type_delay)
        page.press(".arcade-type-input", "Enter")

        page.wait_for_selector(".arcade-scatter-field", timeout=5000)
        words = page.eval_on_selector_all(".arcade-scatter-word", "els => els.map(e => e.textContent)")
        for w in words:
            page.click(".arcade-type-input")
            page.keyboard.type(w, delay=type_delay)
            page.keyboard.press("Enter")
            page.wait_for_timeout(60)

        # A4 — complexity ramp. Click the "target" cell at each of the 4 levels.
        page.wait_for_selector(".arcade-ramp-grid", timeout=8000)
        for _ in range(A4_LEVEL_COUNT):
            page.wait_for_selector(".arcade-ramp-cell.target", timeout=7000)
            if pause:
                page.wait_for_timeout(pause)
            page.click(".arcade-ramp-cell.target")

        # No manual reveal-form submission needed — arcade.html auto-submits
        # for generator-labeled sessions. Just let the tail telemetry flush
        # before closing (also gives that auto-reveal fetch time to land).
        page.wait_for_timeout(2200)
    finally:
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20, help="number of sessions to generate")
    parser.add_argument("--headed", action="store_true", help="run with a visible browser window")
    parser.add_argument("--speed", choices=["fast", "normal", "slow", "random"], default="random",
                         help="typing/pacing profile — 'random' (default) picks one per session")
    parser.add_argument("--base-url", default=SERVER_URL)
    args = parser.parse_args()

    check_server(args.base_url)

    with sync_playwright() as p:
        for i in range(args.count):
            speed = random.choice(["fast", "normal", "slow"]) if args.speed == "random" else args.speed
            try:
                run_one_session(p, args.base_url, headless=not args.headed, speed=speed)
                print(f"[{i + 1}/{args.count}] session complete (speed={speed})")
            except Exception as e:
                print(f"[{i + 1}/{args.count}] session failed (speed={speed}): {e}")


if __name__ == "__main__":
    main()

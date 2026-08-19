"""Phase 2 — generate `agent_stealth_cdp` labeled sessions: raw Playwright/CDP
driving the /arcade gauntlet, but deliberately evasive — the "smart adversary"
profile, in contrast to run_playwright_raw.py's naive mechanical baseline.

Targets both detection tracks at once:
  - raw_automation_score (signals/webdriver_artifacts.py): defeated by patching
    navigator.webdriver via an init script, and by running headed by default
    (avoids the SwiftShader/"HeadlessChrome" tells headless Chromium leaves).
  - arcade score (signals/arcade_metrics.py): defeated by curved (quadratic
    bezier) mouse paths with per-step jitter instead of Playwright's default
    straight-line interpolation, click points jittered within the target's
    bounding box instead of dead-center, and per-keystroke typing delay drawn
    from a random range instead of a constant.

This is a genuine test of whether the detection thesis holds up against an
adversary actively trying to look human, not just a naive scraper. Whatever it
still gets caught on is real signal; whatever it evades is a real gap — either
way, more useful than another naive-bot data point.

--evasion controls how much of the above actually gets applied. "full" (both
tracks defeated) has an important property: when it works, it leaves ZERO
rules fired anywhere, which means update_weights_from_reveal() has nothing to
nudge on self-report — you can run a thousand of these and the live weight
system won't move, by construction (it can strengthen an existing signal, it
can't invent one). "no_patch" and "no_curve" each defeat only one track,
which DOES give the weight system something to work with when self-report
reveals the mismatch. "random" (the --count>1 default) mixes all three per
batch so a single run covers the full spectrum instead of needing three
separate invocations.
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

from agent_runner.adversary_state import choose_evasion, load_state, record_outcome, save_state
from agent_runner.common import SERVER_URL
from scoring.rule_based_scorer import score_session
from test_site.storage import get_conn

C4_POLL_WINDOW_S = 11
C4_POLL_INTERVAL_MS = 80
A4_LEVEL_COUNT = 4

WEBDRIVER_PATCH = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"


def check_server(base_url: str) -> None:
    try:
        urllib.request.urlopen(base_url, timeout=3)
    except urllib.error.URLError:
        sys.exit(
            f"Can't reach {base_url}. Start the test site first:\n"
            f"  python -m test_site.server"
        )


def _bezier_move(page, cur, target, steps):
    cur_x, cur_y = cur
    tgt_x, tgt_y = target
    dx, dy = tgt_x - cur_x, tgt_y - cur_y
    dist = math.hypot(dx, dy) or 1
    # control point offset perpendicular to the straight line, random side/magnitude
    offset = random.uniform(0.1, 0.3) * dist * random.choice((-1, 1))
    ctrl_x = (cur_x + tgt_x) / 2 - dy / dist * offset
    ctrl_y = (cur_y + tgt_y) / 2 + dx / dist * offset
    for i in range(1, steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * cur_x + 2 * (1 - t) * t * ctrl_x + t ** 2 * tgt_x
        y = (1 - t) ** 2 * cur_y + 2 * (1 - t) * t * ctrl_y + t ** 2 * tgt_y
        x += random.uniform(-1.5, 1.5)
        y += random.uniform(-1.5, 1.5)
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(4, 14))


def humanlike_click(page, cur, x, y, fast=False, use_curve=True):
    """Move along a jittered curve to (x, y) and click. Returns the new cursor
    position. `fast` shortens the path for time-constrained targets (C4's
    moles) while keeping some curvature/jitter rather than none. `use_curve`
    False collapses this to a plain straight click — the --evasion=no_curve
    profile, still webdriver-patched and headed, but leaving the arcade-side
    geometry checks (dead_center_clicks, no_path_corrections) able to fire."""
    if not use_curve:
        page.mouse.click(x, y)
        return (x, y)
    dist = math.hypot(x - cur[0], y - cur[1])
    steps = max(4, min(12, int(dist / 25))) if fast else max(8, min(35, int(dist / 12)))
    _bezier_move(page, cur, (x, y), steps)
    page.wait_for_timeout(random.randint(10, 30) if fast else random.randint(30, 90))
    page.mouse.down()
    page.wait_for_timeout(random.randint(15, 40))
    page.mouse.up()
    return (x, y)


def approach_move(page, cur, target, steps, use_curve):
    """Move the cursor toward target without clicking — used where the approach
    itself matters (B1's shift trigger needs the cursor to actually pass near
    the pre-shift position, not just teleport there). Straight multi-step
    move when use_curve is False, so --evasion=no_curve still triggers B1's
    shift correctly, just without the bezier/jitter."""
    if use_curve:
        _bezier_move(page, cur, target, steps)
    else:
        page.mouse.move(target[0], target[1], steps=max(2, steps))


def humanlike_draw_circle(page, cx, cy, radius, use_curve):
    """Trace a circle with per-step radius/angle jitter and occasional small
    backward micro-corrections — a real hand-drawn circle is never
    mathematically perfect, unlike run_playwright_raw.py's deliberately exact
    version. use_curve=False draws a mechanically perfect circle instead (no
    jitter), matching the no_curve evasion profile's "defeats one track,
    leaves the other alone" design elsewhere in this script."""
    steps = 48
    start_x, start_y = cx + radius, cy
    page.mouse.move(start_x, start_y)
    page.mouse.down()
    angle = 0.0
    while angle < 2 * math.pi + 0.15:
        if use_curve:
            r = radius + random.uniform(-8, 8)
            step = (2 * math.pi / steps) * random.uniform(0.6, 1.4)
        else:
            r = radius
            step = 2 * math.pi / steps
        angle += step
        page.mouse.move(cx + r * math.cos(angle), cy + r * math.sin(angle))
        if use_curve:
            page.wait_for_timeout(random.randint(3, 12))
    page.mouse.up()


def jittered_point(box, spread=0.3):
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    jx = cx + random.uniform(-spread, spread) * box["width"] / 2
    jy = cy + random.uniform(-spread, spread) * box["height"] / 2
    return jx, jy


def humanlike_type(page, selector, text):
    page.click(selector)
    for ch in text:
        page.keyboard.type(ch, delay=0)
        page.wait_for_timeout(random.randint(60, 180))


def run_one_session(playwright, base_url: str, headless: bool, evasion: str = "full",
                     label: str = "agent_stealth_cdp", type_fn=None) -> str | None:
    """`label`/`type_fn` let a caller reuse this whole gauntlet walk under a
    different generator label and a different typing strategy without
    duplicating it — see run_playwright_stealth_typing.py, which swaps in
    backspace-simulating typing for the adversarial durability test while
    keeping every other evasion behavior (curved mouse approach before every
    click, webdriver patch) identical."""
    apply_patch = evasion != "no_patch"
    use_curve = evasion != "no_curve"
    type_fn = type_fn or humanlike_type

    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page(viewport={"width": 1000, "height": 800})
    if apply_patch:
        page.add_init_script(WEBDRIVER_PATCH)
    cur = (random.uniform(100, 300), random.uniform(100, 300))
    try:
        page.goto(f"{base_url}/arcade?label={label}")

        # C1 — flash reaction. Click somewhere on the canvas, curved approach.
        page.wait_for_selector(".arcade-canvas", timeout=8000)
        box = page.eval_on_selector(
            ".arcade-canvas",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        cur = humanlike_click(page, cur, *jittered_point(box), use_curve=use_curve)

        # A1 — perception probe. Read the visible instruction (same channel a
        # mechanical script defaults to). Shapes float continuously via rAF, so
        # a single bounding-box read taken before a slow curved approach goes
        # stale by the time the click lands — coarse-approach toward the
        # stale box first, then re-read a fresh box for a short final
        # correction, same ballistic-then-corrective pattern a real human's
        # eye-hand tracking uses (and the same fix B1 already needed for its
        # one-time shift).
        page.wait_for_selector(".arcade-shape-field .arcade-shape", timeout=8000)
        visible_text = page.text_content(".arcade-stage-a1 .arcade-instruction")
        kind = visible_text.strip().split()[-1].lower()
        shape_selector = f'.arcade-shape[data-kind="{kind}"]'
        box_js = "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }"
        box = page.eval_on_selector(shape_selector, box_js)
        approach = jittered_point(box, spread=0.1)
        approach_move(page, cur, approach, steps=max(6, min(20, int(math.hypot(approach[0] - cur[0], approach[1] - cur[1]) / 15))), use_curve=use_curve)
        cur = approach
        box = page.eval_on_selector(shape_selector, box_js)
        cur = humanlike_click(page, cur, *jittered_point(box), fast=True, use_curve=use_curve)

        # C4 — go/no-go whack-a-mole. Tight time budget, so use the fast/
        # abbreviated curve rather than skipping curvature entirely.
        page.wait_for_selector(".arcade-mole-grid", timeout=8000)
        deadline = time.time() + C4_POLL_WINDOW_S
        while time.time() < deadline:
            mole = page.query_selector(".arcade-mole")
            if mole:
                try:
                    box = mole.bounding_box()
                    if box:
                        cur = humanlike_click(page, cur, *jittered_point(box), fast=True, use_curve=use_curve)
                except Exception:
                    pass
            page.wait_for_timeout(C4_POLL_INTERVAL_MS)
            if page.query_selector(".arcade-box-row"):
                break

        # A2 — visual vs. DOM order. Same mechanical DOM-order choice as the
        # naive baseline (read the instructed ordinal, apply it to DOM source
        # position, ignore rendered layout — see run_playwright_raw.py's
        # comment for why this needs to read the actual ordinal now instead
        # of always clicking dom-index 1).
        page.wait_for_selector(".arcade-order-box", timeout=8000)
        instruction_text = page.text_content(".arcade-stage-a2 .arcade-instruction")
        ordinal_match = re.search(r"(\d+)(st|nd|rd|th)", instruction_text)
        dom_index = ordinal_match.group(1) if ordinal_match else "1"
        box = page.eval_on_selector(
            f'.arcade-order-box[data-dom-index="{dom_index}"]',
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        cur = humanlike_click(page, cur, *jittered_point(box), use_curve=use_curve)

        # B1 — layout shift. Curved move toward the initial position (triggers
        # the shift via the ambient pointermove listener), then curved move to
        # wherever it ends up.
        page.wait_for_selector(".arcade-shift-target", timeout=8000)
        box = page.eval_on_selector(
            ".arcade-shift-target",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        pre_target = jittered_point(box)
        approach_move(page, cur, pre_target, steps=max(8, min(35, int(math.hypot(pre_target[0] - cur[0], pre_target[1] - cur[1]) / 12))), use_curve=use_curve)
        cur = pre_target
        page.wait_for_timeout(random.randint(120, 220))
        box2 = page.eval_on_selector(
            ".arcade-shift-target",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        cur = humanlike_click(page, cur, *jittered_point(box2), use_curve=use_curve)

        # B2 — draw the shape. Jittered trace when use_curve, mechanically
        # exact otherwise (see humanlike_draw_circle's docstring).
        page.wait_for_selector(".arcade-draw-canvas", timeout=8000)
        box = page.eval_on_selector(
            ".arcade-draw-canvas",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        draw_cx, draw_cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        humanlike_draw_circle(page, draw_cx, draw_cy, 110, use_curve)
        cur = (draw_cx + 110, draw_cy)

        # A5 — type the phrase (visible, no memory) + scattered words, with
        # per-keystroke randomized cadence instead of a constant delay.
        page.wait_for_selector(".arcade-phrase-display", timeout=8000)
        phrase = page.text_content(".arcade-phrase-display").strip()
        type_fn(page, ".arcade-type-input", phrase)
        page.press(".arcade-type-input", "Enter")

        page.wait_for_selector(".arcade-scatter-field", timeout=5000)
        words = page.eval_on_selector_all(".arcade-scatter-word", "els => els.map(e => e.textContent)")
        for w in words:
            type_fn(page, ".arcade-type-input", w)
            page.keyboard.press("Enter")
            page.wait_for_timeout(random.randint(80, 200))

        # A4 — complexity ramp. Curved move to each level's target cell.
        page.wait_for_selector(".arcade-ramp-grid", timeout=8000)
        for _ in range(A4_LEVEL_COUNT):
            page.wait_for_selector(".arcade-ramp-cell.target", timeout=7000)
            box = page.eval_on_selector(
                ".arcade-ramp-cell.target",
                "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
            )
            cur = humanlike_click(page, cur, *jittered_point(box), use_curve=use_curve)

        # arcade.html auto-submits /api/reveal for generator-labeled sessions
        # once it finishes — nothing to do here but wait for that (and the
        # rest of the telemetry queue) to flush before closing.
        page.wait_for_timeout(2200)
        return page.evaluate("() => window.ARCADE && window.ARCADE.sessionId")
    finally:
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=5, help="number of sessions to generate")
    parser.add_argument("--headless", action="store_true", help="run headless instead of the default headed window")
    parser.add_argument("--evasion", choices=["full", "no_patch", "no_curve", "random"], default="random",
                         help="which evasion track to apply — 'random' (default) picks one per session")
    parser.add_argument("--base-url", default=SERVER_URL)
    args = parser.parse_args()

    check_server(args.base_url)

    state = load_state() if args.evasion == "random" else None

    with sync_playwright() as p:
        for i in range(args.count):
            evasion = choose_evasion(state) if state is not None else args.evasion
            try:
                session_id = run_one_session(p, args.base_url, headless=args.headless, evasion=evasion)
                status = f"[{i + 1}/{args.count}] session complete (evasion={evasion})"

                # The adversary "knowing why it's being detected": read our own
                # just-completed session's score breakdown straight from the DB
                # (direct access, not the public /api/verdict — that endpoint
                # deliberately withholds this from real players on purpose) and
                # use it to bias the NEXT random evasion pick away from whatever
                # just got caught. This is the other half of the co-evolution
                # loop from update_weights_from_reveal(): the detector adapts to
                # self-reports, this adapts to the detector.
                if state is not None and session_id:
                    with get_conn() as conn:
                        result = score_session(session_id, conn)
                    caught = result["overall_detection_score"] >= 50
                    dominant = result["breakdown"] if result["score"] >= result["raw_automation_score"] else result["raw_automation_breakdown"]
                    fired = ", ".join(r["rule"] for r in dominant) or "nothing"
                    status += f" — caught={caught} (fired: {fired})"
                    record_outcome(state, evasion, caught)
                    save_state(state)

                print(status)
            except Exception as e:
                print(f"[{i + 1}/{args.count}] session failed (evasion={evasion}): {e}")


if __name__ == "__main__":
    main()

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
"""

import argparse
import math
import random
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

from agent_runner.common import SERVER_URL

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


def humanlike_click(page, cur, x, y, fast=False):
    """Move along a jittered curve to (x, y) and click. Returns the new cursor
    position. `fast` shortens the path for time-constrained targets (C4's
    moles) while keeping some curvature/jitter rather than none."""
    dist = math.hypot(x - cur[0], y - cur[1])
    steps = max(4, min(12, int(dist / 25))) if fast else max(8, min(35, int(dist / 12)))
    _bezier_move(page, cur, (x, y), steps)
    page.wait_for_timeout(random.randint(10, 30) if fast else random.randint(30, 90))
    page.mouse.down()
    page.wait_for_timeout(random.randint(15, 40))
    page.mouse.up()
    return (x, y)


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


def run_one_session(playwright, base_url: str, headless: bool) -> None:
    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page(viewport={"width": 1000, "height": 800})
    page.add_init_script(WEBDRIVER_PATCH)
    cur = (random.uniform(100, 300), random.uniform(100, 300))
    try:
        page.goto(f"{base_url}/arcade?label=agent_stealth_cdp")

        # C1 — flash reaction. Click somewhere on the canvas, curved approach.
        page.wait_for_selector(".arcade-canvas", timeout=8000)
        box = page.eval_on_selector(
            ".arcade-canvas",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        cur = humanlike_click(page, cur, *jittered_point(box))

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
        _bezier_move(page, cur, approach, steps=max(6, min(20, int(math.hypot(approach[0] - cur[0], approach[1] - cur[1]) / 15))))
        cur = approach
        box = page.eval_on_selector(shape_selector, box_js)
        cur = humanlike_click(page, cur, *jittered_point(box), fast=True)

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
                        cur = humanlike_click(page, cur, *jittered_point(box), fast=True)
                except Exception:
                    pass
            page.wait_for_timeout(C4_POLL_INTERVAL_MS)
            if page.query_selector(".arcade-box-row"):
                break

        # A2 — visual vs. DOM order. Same mechanical DOM-first choice as the
        # naive baseline (this stage isn't about mouse geometry) but curved.
        page.wait_for_selector(".arcade-order-box", timeout=8000)
        box = page.eval_on_selector(
            '.arcade-order-box[data-dom-index="1"]',
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        cur = humanlike_click(page, cur, *jittered_point(box))

        # B1 — layout shift. Curved move toward the initial position (triggers
        # the shift via the ambient pointermove listener), then curved move to
        # wherever it ends up.
        page.wait_for_selector(".arcade-shift-target", timeout=8000)
        box = page.eval_on_selector(
            ".arcade-shift-target",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        pre_target = jittered_point(box)
        _bezier_move(page, cur, pre_target, steps=max(8, min(35, int(math.hypot(pre_target[0] - cur[0], pre_target[1] - cur[1]) / 12))))
        cur = pre_target
        page.wait_for_timeout(random.randint(120, 220))
        box2 = page.eval_on_selector(
            ".arcade-shift-target",
            "el => { const r = el.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; }",
        )
        cur = humanlike_click(page, cur, *jittered_point(box2))

        # A5 — type the phrase (visible, no memory) + scattered words, with
        # per-keystroke randomized cadence instead of a constant delay.
        page.wait_for_selector(".arcade-phrase-display", timeout=8000)
        phrase = page.text_content(".arcade-phrase-display").strip()
        humanlike_type(page, ".arcade-type-input", phrase)
        page.press(".arcade-type-input", "Enter")

        page.wait_for_selector(".arcade-scatter-field", timeout=5000)
        words = page.eval_on_selector_all(".arcade-scatter-word", "els => els.map(e => e.textContent)")
        for w in words:
            humanlike_type(page, ".arcade-type-input", w)
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
            cur = humanlike_click(page, cur, *jittered_point(box))

        # No reveal-form submission — label/trust are already correct from
        # session-start. Just let the tail telemetry flush before closing.
        page.wait_for_timeout(2200)
    finally:
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=5, help="number of sessions to generate")
    parser.add_argument("--headless", action="store_true", help="run headless instead of the default headed window")
    parser.add_argument("--base-url", default=SERVER_URL)
    args = parser.parse_args()

    check_server(args.base_url)

    with sync_playwright() as p:
        for i in range(args.count):
            try:
                run_one_session(p, args.base_url, headless=args.headless)
                print(f"[{i + 1}/{args.count}] session complete")
            except Exception as e:
                print(f"[{i + 1}/{args.count}] session failed: {e}")


if __name__ == "__main__":
    main()

"""Phase 2 — generate `agent_raw_cdp` labeled sessions: raw Playwright/CDP driving
the /arcade gauntlet, no LLM reasoning involved. This is the "fast, mechanical"
baseline agent profile to contrast against Browser Use (LLM-driven) and real
human sessions — on the SAME metric set humans produce, not the old storefront
flow, which would leave human data on the new arcade metrics and agent data
only on the old ones with nothing comparable.

/arcade auto-starts (skips the landing page's Start button) when it sees
?label=agent_raw_cdp — see arcade.js's resolveLabel(). No reveal-form submission
needed: label/trust are already correct from session-start.
"""

import argparse
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

from agent_runner.common import SERVER_URL

C4_POLL_WINDOW_S = 11
C4_POLL_INTERVAL_MS = 80
A4_LEVEL_COUNT = 4


def check_server(base_url: str) -> None:
    try:
        urllib.request.urlopen(base_url, timeout=3)
    except urllib.error.URLError:
        sys.exit(
            f"Can't reach {base_url}. Start the test site first:\n"
            f"  python -m test_site.server"
        )


def run_one_session(playwright, base_url: str, headless: bool) -> None:
    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page(viewport={"width": 1000, "height": 800})
    try:
        page.goto(f"{base_url}/arcade?label=agent_raw_cdp")

        # C1 — flash reaction. Any click ends the stage (false start if too
        # early); no point racing the randomized decoy timing.
        page.wait_for_selector(".arcade-canvas", timeout=8000)
        page.click(".arcade-canvas")

        # A1 — perception probe. Floating shapes never satisfy Playwright's
        # actionability "stable" check, so force=True (see CLAUDE.md's note on
        # this being a real signal, not just a testing footgun). Click whichever
        # shape the *visible* instruction names — the plain, non-discriminating
        # channel a mechanical script would naturally default to reading.
        page.wait_for_selector(".arcade-shape-field .arcade-shape", timeout=8000)
        visible_text = page.text_content(".arcade-stage-a1 .arcade-instruction")
        kind = visible_text.strip().split()[-1].lower()
        page.click(f'.arcade-shape[data-kind="{kind}"]', force=True)

        # C4 — go/no-go whack-a-mole. Moles appear/vanish in well under a
        # second; poll and click whatever's up.
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

        # A2 — visual vs. DOM order. Click the first element in DOM source
        # order — the mechanical, position-blind choice.
        page.wait_for_selector(".arcade-order-box", timeout=8000)
        page.click('.arcade-order-box[data-dom-index="1"]')

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

        # A5 — type the phrase (visible, no memory) + scattered words.
        page.wait_for_selector(".arcade-phrase-display", timeout=8000)
        phrase = page.text_content(".arcade-phrase-display").strip()
        page.type(".arcade-type-input", phrase, delay=15)
        page.press(".arcade-type-input", "Enter")

        page.wait_for_selector(".arcade-scatter-field", timeout=5000)
        words = page.eval_on_selector_all(".arcade-scatter-word", "els => els.map(e => e.textContent)")
        for w in words:
            page.click(".arcade-type-input")
            page.keyboard.type(w, delay=15)
            page.keyboard.press("Enter")
            page.wait_for_timeout(60)

        # A4 — complexity ramp. Click the "target" cell at each of the 4 levels.
        page.wait_for_selector(".arcade-ramp-grid", timeout=8000)
        for _ in range(A4_LEVEL_COUNT):
            page.wait_for_selector(".arcade-ramp-cell.target", timeout=7000)
            page.click(".arcade-ramp-cell.target")

        # No reveal-form submission — label/trust are already correct from
        # session-start (see module docstring). Just let the tail telemetry
        # flush before closing.
        page.wait_for_timeout(2200)
    finally:
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20, help="number of sessions to generate")
    parser.add_argument("--headed", action="store_true", help="run with a visible browser window")
    parser.add_argument("--base-url", default=SERVER_URL)
    args = parser.parse_args()

    check_server(args.base_url)

    with sync_playwright() as p:
        for i in range(args.count):
            try:
                run_one_session(p, args.base_url, headless=not args.headed)
                print(f"[{i + 1}/{args.count}] session complete")
            except Exception as e:
                print(f"[{i + 1}/{args.count}] session failed: {e}")


if __name__ == "__main__":
    main()

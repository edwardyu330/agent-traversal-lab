"""The adversarial durability test from the separation-analysis follow-up
(Part 2, not more volume): does anything in the ranked spec survive an agent
that specifically defeats the two features the memo flagged as strongest —
ipi_cv (keystroke rhythm) and the nullity features (movement/click presence)?

run_playwright_stealth.py's evasion=full profile already produces real
pointer movement before every click (bezier-curved approach, see
humanlike_click) — that part needs no changes. What it does NOT do is
backspace: humanlike_type() there is character-by-character with randomized
delay, but never mistypes or corrects, so it can defeat ipi_cv's raw timing
CV without ever touching backspace_rate. This script swaps in
humanlike_type_with_backspaces(), everything else identical (always
evasion="full" — a single deliberate test, not another random co-evolution
batch), under its own label (agent_stealth_typing_cdp) so it never gets
silently folded into the existing agent_stealth_cdp dataset.

Run: python -m agent_runner.run_playwright_stealth_typing --count 12
Then: python -m analysis.adversarial_rescore
"""

import argparse
import random
import sys

from playwright.sync_api import sync_playwright

from agent_runner.common import SERVER_URL
from agent_runner.run_playwright_stealth import check_server, run_one_session

LABEL = "agent_stealth_typing_cdp"


def humanlike_type_with_backspaces(page, selector, text, backspace_prob=0.08):
    """Same per-character human-ish delay as run_playwright_stealth.py's
    humanlike_type, plus an occasional simulated typo: type a wrong
    character, pause, backspace it, then continue with the real one — the
    exact pattern backspace_rate/ipi_cv are built to expect from a human."""
    page.click(selector)
    for ch in text:
        if random.random() < backspace_prob:
            wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
            page.keyboard.type(wrong, delay=0)
            page.wait_for_timeout(random.randint(70, 200))
            page.keyboard.press("Backspace")
            page.wait_for_timeout(random.randint(60, 150))
        page.keyboard.type(ch, delay=0)
        page.wait_for_timeout(random.randint(60, 180))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=12, help="number of sessions to generate")
    parser.add_argument("--headless", action="store_true", help="run headless instead of the default headed window")
    parser.add_argument("--base-url", default=SERVER_URL)
    args = parser.parse_args()

    check_server(args.base_url)

    with sync_playwright() as p:
        for i in range(args.count):
            try:
                session_id = run_one_session(
                    p, args.base_url, headless=args.headless, evasion="full",
                    label=LABEL, type_fn=humanlike_type_with_backspaces,
                )
                print(f"[{i + 1}/{args.count}] session complete: {session_id}")
            except Exception as e:
                print(f"[{i + 1}/{args.count}] session failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

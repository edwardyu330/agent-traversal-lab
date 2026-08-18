"""Phase 2 — capture one `human` labeled session.

Opens a real, visible browser window against test_site with the task instructions
printed to the terminal. Do the task like a normal person — no need to rush or be
careful about it. Telemetry is captured automatically by collector.js as you go.
Close the browser window (or press Enter here) when you're done.

Run once per session you want to capture — aim for ~20-30 across a few different
people/days for a baseline that isn't just one person's muscle memory.
"""

import argparse

from playwright.sync_api import sync_playwright

from agent_runner.common import SERVER_URL, TASK_DESCRIPTION
from agent_runner.run_playwright_raw import check_server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=SERVER_URL)
    args = parser.parse_args()

    check_server(args.base_url)

    print("=" * 60)
    print("HUMAN BASELINE SESSION")
    print("=" * 60)
    print(f"Task: {TASK_DESCRIPTION}")
    print("A browser window will open now. Do the task, then close the window")
    print("(or come back here and press Enter) when you're finished.")
    print("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(f"{args.base_url}/?label=human")
        try:
            input("Press Enter here once you've submitted the order... ")
        except (EOFError, KeyboardInterrupt):
            pass
        browser.close()

    print("Session captured.")


if __name__ == "__main__":
    main()

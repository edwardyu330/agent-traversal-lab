"""Phase 2 — generate `agent_llm_cdp` labeled sessions: Browser Use (LLM-driven,
still CDP underneath) playing the /arcade gauntlet. Contrast this against
run_playwright_raw.py's mechanical, scripted actions — the interesting question
is whether the *reasoning pauses* between LLM tool calls, and the imprecision of
vision/DOM-grounded clicking, show up as a distinct timing/geometry signature
from both raw CDP and real humans, on the SAME metric set humans produce (not
the old storefront flow).

Some stages are effectively impossible for an LLM agent on a
perceive-screenshot-reason-act loop that takes seconds per step: C1's decoy
flashes (~260-450ms each) and C4's moles (~850ms visible) will almost certainly
time out every round. That's expected and is itself a data point, not a bug —
the game's own per-round timeouts handle a missed window gracefully (they just
record it as incorrect and move on), so this never needs special-casing here.
What DOES need handling here: agent.run() itself failing (API error, hitting
max_steps, getting stuck) must not crash the whole --count loop or discard
whatever telemetry the session already sent before it happened — see
run_one_session()'s try/except.

Requires ANTHROPIC_API_KEY in the environment. Each session costs real API calls
(a full 7-stage run costs noticeably more than the old single-checkout task —
start with a small --count while checking things work) and this max_steps is
generous by comparison to the old default (25).
"""

import argparse
import asyncio
import os
import sys

from browser_use import Agent, BrowserProfile
from browser_use.llm.anthropic.chat import ChatAnthropic
from dotenv import load_dotenv

from agent_runner.common import SERVER_URL
from agent_runner.run_playwright_raw import check_server

load_dotenv()  # picks up ANTHROPIC_API_KEY from a project-root .env, if present

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_STEPS = 80

TASK_DESCRIPTION = (
    "You are on a page that already navigated to /arcade with your session pre-labeled "
    "and pre-authenticated — you do NOT need to click any 'Start' button or fill in a "
    "reveal/self-report form at the end; the run finishes on its own. "
    "Play through the mini-game gauntlet: it's a sequence of short challenges (reaction "
    "time, perception, memory, precision, typing) that advance automatically once you "
    "complete or time out on each one. Just do your best on whatever's currently on "
    "screen. Some challenges involve targets that appear and disappear in under a "
    "second — if you miss the window, the game moves on by itself; don't retry a "
    "challenge that already advanced, and don't get stuck re-reading instructions for "
    "a screen that's already gone. Keep going until the page shows a final score and a "
    "'We think you were: ...' verdict — that's the end of the run."
)


async def run_one_session(base_url: str, model: str, headless: bool) -> None:
    llm = ChatAnthropic(model=model)
    agent = Agent(
        task=TASK_DESCRIPTION,
        llm=llm,
        browser_profile=BrowserProfile(headless=headless),
        initial_actions=[{"navigate": {"url": f"{base_url}/arcade?label=agent_llm_cdp"}}],
    )
    await agent.run(max_steps=MAX_STEPS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=5, help="number of sessions to generate")
    parser.add_argument("--headed", action="store_true", help="run with a visible browser window")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=SERVER_URL)
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY is not set. Export it first:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-..."
        )
    check_server(args.base_url)

    for i in range(args.count):
        try:
            asyncio.run(run_one_session(args.base_url, args.model, headless=not args.headed))
            print(f"[{i + 1}/{args.count}] session complete")
        except Exception as e:
            # Whatever telemetry the browser already sent (session-start, any
            # stage_results reached before this failure) is already in
            # traversal.db regardless — a partial agent_llm_cdp session is
            # real data, not something to discard. See module docstring.
            print(f"[{i + 1}/{args.count}] session failed partway through: {e}")


if __name__ == "__main__":
    main()

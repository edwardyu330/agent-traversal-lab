# agent-traversal-lab

Local research harness for observing how AI agents vs. humans traverse a website, and
producing a first-pass, rule-based session score from the technical signals that separate
them. See [CLAUDE.md](CLAUDE.md) for scope, guardrails, and conventions.

**Part 1 of two research phases.** This is the behavioral-signal phase — what a session
*does* (mouse/keyboard/timing/perception probes on the `/arcade` gauntlet) separates human
from agent. Phase 2, [network-identity-lab](https://github.com/edwardyu330/network-identity-lab),
picks up where this one's own findings pointed: a threshold calibrated on naive agent
traffic caught 0% of a moderately careful adversary (see
`analysis/adversarial_rescore.py`), and every signal that survived adversarial pressure
was cross-session, not within-session. That's the network/TLS/identity-clustering layer
this repo's behavioral signals can't reach on their own — it only sees traffic that
executes JavaScript, and network-identity-lab is what covers what's left.

## Status

- [x] Phase 1 — instrumented test site + client-side collector + SQLite storage
- [~] Phase 2 — `run_playwright_raw.py` done and tested; `run_human_baseline.py` written,
  needs someone to actually run it ~20-30x; `run_browser_use.py` written, needs
  `ANTHROPIC_API_KEY` and a live test run
- [x] Phase 3 — signal extraction (`signals/`) — implemented and validated against
  `agent_raw_cdp` data
- [~] Phase 4 — scorer (`scoring/rule_based_scorer.py`) and report
  (`analysis/compare_agent_vs_human.py`) implemented and validated end-to-end, but
  "first results" (measurable separation across labels) needs `human` and
  `agent_llm_cdp` sessions in the dataset — currently only `agent_raw_cdp` exists
- Phase 5 (ML classifier, OS-level agent detection, real IP intel, cross-session
  correlation) is intentionally out of scope — see CLAUDE.md.
- [x] `/play` challenge (storefront-based human/adversarial data-collection flow):
  landing page, reveal screen with coarse verdict, honor-system self-report all built and
  working end-to-end — see CLAUDE.md's "`/play` challenge flow" section.
- [~] `/arcade` gauntlet — **now the primary data-collection surface**, superseding
  `/play` for the same reason `/play` superseded the CLI loop: richer, denser telemetry
  and a metric set actually built to catch a real-browser agent, not just raw CDP. Stage
  harness + ambient telemetry (unthrottled pointer stream, click-offset, frame timing) +
  7 of the ~11 planned stages (C1 flash reaction, A1 perception probe with floating +
  rotating shapes, A2 visual-vs-DOM order, A4 complexity ramp, A5 type-the-phrase, B1
  layout-shift intercept, C4 go/no-go whack-a-mole) + reveal + name/email collection +
  three label-specific leaderboards (Human/Bot/Agent, `GET /api/leaderboard`), all
  validated end-to-end including `arcade_metrics.py`'s full signal set (14 of 18 metrics
  now real, only the three A6-dependent ones still `None`) and the restructured scorer
  (`raw_automation_score` split out of the main composite — see CLAUDE.md's "`/arcade`
  gauntlet" section for why). Not yet built: A3/A6/B2, rate limiting, and the tunnel for
  sharing outside localhost.
- [x] `/metrics` — live dashboard (dataset counts, score-by-label, full numeric/boolean/
  categorical signal breakdowns, session table), not linked from any public page, reuses
  `analysis/compare_agent_vs_human.py`'s aggregation instead of duplicating it.

### To get real first results

```bash
python -m test_site.server &                                  # in one terminal
python -m agent_runner.run_playwright_raw --count 20           # agent_raw_cdp
export ANTHROPIC_API_KEY=sk-ant-...                             # or drop it in .env
python -m agent_runner.run_browser_use --count 20               # agent_llm_cdp
# for human data, open http://127.0.0.1:8000/arcade and play it yourself a few times
# (older paths still work: /play, or python -m agent_runner.run_human_baseline)
python -m analysis.compare_agent_vs_human                       # writes analysis/report.md
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run the test site

```bash
python -m test_site.server
# open http://127.0.0.1:8000/
```

Every session's telemetry (mouse, clicks, keystrokes, WebDriver/WebGL artifacts, scroll,
navigation) is captured by `test_site/static/collector.js` and stored in
`data/traversal.db`.

To label a session at generation time, hit the first page with `?label=`, e.g.
`http://127.0.0.1:8000/?label=human`. Valid labels: `human`, `agent_raw_cdp`,
`agent_llm_cdp`. The label persists across the task flow via `sessionStorage`.

The task used across all three traffic sources: **find and click checkout, then submit
the order.**

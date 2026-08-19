# agent-traversal-lab

Research harness that observes how AI agents (and humans, as baseline) traverse a website,
extracts detection-relevant technical signals, and produces a first-pass session score.
This is the evidence base for a future product's Identification/Fingerprinting/Adaptive
layers — not the product itself.

## Guardrail

All agent traffic in this repo runs against the local `test_site` only. Never point any
script here at a third-party production site without explicit authorization.

## Scope

Rule-based/heuristic detection only. No ML classification in this build (dataset isn't
big enough yet to be worth training on). Explicitly deferred, tracked as known gaps:
OS-level/computer-use agent detection (drives input via OS accessibility layer, not CDP —
produces none of the artifacts this harness looks for), cross-session/cross-customer
correlation, real IP/proxy-network intelligence (stubbed with a TODO), hardware attestation.

## Layout

- `test_site/` — FastAPI app serving `/arcade` (the mini-game gauntlet — the *only*
  player-facing surface; `/` just redirects there) and `/metrics` — a live dashboard, not
  linked from any public page, reusing `analysis.compare_agent_vs_human.build_dataframe()`
  rather than duplicating its aggregation logic. `arcade.js` is the only client-side
  telemetry path (unthrottled — see "The `/arcade` gauntlet"). Sessions and raw events
  land in `data/traversal.db` (SQLite, gitignored). The original build also had a
  storefront (product listing/login/checkout) and a `/play` challenge flow reusing it,
  plus a throttled `collector.js` telemetry path and a `run_human_baseline.py` driver —
  all deleted once `/arcade` fully superseded them as the data-collection surface;
  historical sessions captured through that flow are still in `data/traversal.db` and
  still show up in `signals/`/`scoring/`/`analysis/` (`classify_surface()` in
  `analysis/audit_dataset.py` still calls that shape "storefront" for exactly this reason)
  — only the code that served it is gone, not the data.
- `agent_runner/` — scripts that drive traffic against `test_site` and label sessions at
  generation time: raw Playwright/CDP (`run_playwright_raw.py`), Browser Use (LLM-driven,
  still CDP underneath — `run_browser_use.py`), and an evasive raw-CDP profile
  (`run_playwright_stealth.py` — patched `navigator.webdriver`, headed, curved mouse
  paths, testing whether detection holds up against an adversary actively trying to look
  human). Human baseline data now comes from real people playing `/arcade` directly (no
  driver script needed) plus honest self-report on reveal.
- `signals/` — signal-extraction modules reading from `data/traversal.db`: WebDriver/CDP
  artifacts, inter-action timing (`event_gaps()` is a raw diagnostic, not used by the
  scorer — see `analysis/inspect_timing.py`), mouse geometry (no longer the main
  composite's backbone), network fingerprint (noted limitations), and `arcade_metrics.py`
  — the metric set `/arcade`'s games actually drive the score with now. A few fields
  (`path_optimality`, `backtrack_count`, `dead_end_rate`) are still `None` pending A6, the
  one stage not built yet — check there before assuming a metric is wired up just because
  it's defined.
- `scoring/` — `rule_based_scorer.py` combines signals into `score` (arcade + mouse-
  curvature composite) and a separately-tracked `raw_automation_score` (webdriver flag /
  headless UA / suspicious WebGL — 100% accurate against raw CDP, reads 0 against a real
  browser under agent control, so it's deliberately *not* summed into `score` — see the
  module docstring for the Browser Use session that motivated this split).
  `overall_detection_score` (max of the two tracks) is what `coarse_verdict()` — the only
  thing `/arcade` is allowed to show — actually scores against; never
  score/band/breakdown/raw_automation_breakdown, so the reveal screen can't double as an
  evasion-tuning guide for the audience it's built to catch. Point values for every rule
  live in `scoring/weights_store.py`'s `data/scorer_weights.json`, not as literals in
  `rule_based_scorer.py` — loaded fresh on every score, and nudged automatically by
  `update_weights_from_reveal()` on every `/api/reveal` where the verdict shown didn't
  match the player's own claim (see that function's docstring for the honor-system
  exposure that comes with that being fully automatic, by explicit product decision).
- `analysis/` — `compare_agent_vs_human.py` (report), `inspect_timing.py` (gap histogram
  diagnostic, used to check timing-window assumptions against real data before changing
  them), and `audit_dataset.py` (read-only dataset/telemetry-quality audit, scoped to the
  current `build_version` by default).

Honor-system self-report, still true after the storefront removal: `/api/reveal`
(`claimed_type`: human/bot_script/agent, from `CLAIMED_TYPES`; optional free-text `tool`)
never rejects, gates, or cross-checks a claim against telemetry — it's taken at face value
and lands as `trust='verified'` immediately, same as a generator-created session. There is
no "unverified" consistency-check machinery to build; `trust` exists as a column mainly so
a future adversarial-verification mode could be turned on without another migration, not
because this build computes it. `/api/reveal` also *creates* the session if `session_id`
doesn't already exist — how a pure-HTTP client with no JS execution self-reports: it never
ran `arcade.js`, so it never got a session_id from us, but it can make one up and POST
straight to `/api/reveal`. That session then has zero telemetry, which is expected, not a
bug: the value is capturing that this category of visitor showed up at all.

## The `/arcade` gauntlet

Now the primary data-collection surface — built because `/play`'s storefront flow only
produces ~5-10 actions in one slow linear path, and the signals tuned around raw-CDP
artifacts don't measure what actually distinguishes an agent driving a *real* browser
(raw Playwright scored 100/100 under the old scorer; the one live Browser Use session,
real non-headless Chrome, scored 5/100 — see `rule_based_scorer.py`'s docstring). The
games exist to force that distinction; `signals/arcade_metrics.py` is the metric set that
reads them. Brand across every page is "The Turing Arcade" (renamed from the original
storefront-era "Riverstone Goods").

`arcade.html` opens on `#arcade-landing` (title, short pitch, privacy note, "Start the
Gauntlet" button) with `#arcade-root` hidden — `ARCADE.runArcade()` (which is what fires
`POST /api/session/start` and starts ambient capture) is only called from the Start
button's click handler, not on page load. Verified via server log: no `/api/session/start`
call happens before the click. Same consent-before-tracking principle `/play` already
used (keeping `collector.js` off its landing page), now applied to `/arcade` too.

**Why a separate `arcade.js` instead of extending `collector.js`**: `collector.js`
throttles `mousemove` to one sample per 50ms (`MOUSEMOVE_MIN_INTERVAL_MS`) — fine for the
storefront, but it would destroy exactly the resolution these games exist to capture.
`arcade.js` is self-contained (own session bookkeeping, own `track`/`flush`, same
`/api/session/start` and `/api/telemetry` backend contract) and captures pointer data via
unthrottled `pointerrawupdate` (falling back to `pointermove` + `getCoalescedEvents()`
where `pointerrawupdate` isn't supported), plus click-offset-from-target-center, frame
timing per stage, and `event.isTrusted`/`pointerType`/`pressure`/tilt/`movementX`/`Y` on
every sample — ambient, captured identically regardless of which stage is running.

**Stage harness**: `window.ARCADE_STAGES` is populated by loading each stage's script
(`test_site/static/arcade_stages/*.js`) before `arcade.js`; each stage is `{id, tier,
title, mount(container, ctx), cleanup(container)}`, where `ctx` gives the stage
`track()`, `prefersReducedMotion`, and an `onDone(stageResult)` callback. The harness
(`runStage`/`runArcade` in `arcade.js`) chains stages with a countdown between them,
tracks per-stage frame stats, and logs a `stage_result` event with whatever `correct`/
`extra` the stage reported. Run order (`arcade.html`'s script tags, which is also
execution/registration order) alternates fun and research per the original design intent:
`c1_flash_reaction` → `a1_perception_probe` → `c4_whack_a_mole` → `a2_visual_vs_dom_order`
→ `b1_layout_shift` → `b2_draw_shape` → `a5_type_phrase` → `a4_complexity_ramp`.

- `c1_flash_reaction` — reaction-time wash game.
- `a1_perception_probe` — same instruction delivered three ways (canvas-rendered image,
  visually-hidden+`aria-hidden` DOM text, normal visible text), classifying
  `perception_mode` by which one the player acted on. Shapes float via rAF with a
  bouncing velocity and a slow independent rotation, skipped under
  `prefers-reduced-motion`.
- `a2_visual_vs_dom_order` — CSS `order` scrambles visual left-to-right position away
  from DOM source order; the layout generator retries until the two genuinely diverge
  for the instructed position, so the test never silently degenerates to
  non-discriminating.
- `a4_complexity_ramp` — the same simple "find the odd one out" click repeated at 4
  escalating field sizes (4/9/16/25 items). `arcade_metrics._latency_complexity_slope`
  fits a linear regression of latency vs. field size across the (non-timed-out) levels —
  the *shape* of that slope is the signal (flat/negative for a script indifferent to
  visual clutter, positive for a human whose search time scales with it), not the raw
  latency.
- `a5_type_phrase` — two parts, both pure typing-speed (the text stays visible the whole
  time — no memorization, unlike the original flash-then-hide design). Part 1: type a
  visible phrase as fast as possible. Part 2: type all N words from a field of visually
  scattered (not floating) words, in any order. Both parts tag their `key_detail` events
  with the same `stage_id` so `ipi_cv`/`backspace_rate` aggregate across the whole stage
  with no change needed in `arcade_metrics.py`. Only keystroke *timing* and whether a key
  was backspace is logged, never the character, same posture as `collector.js`'s keydown
  handler.
- `b1_layout_shift` — a target jumps position once the cursor gets within
  `TRIGGER_RADIUS_PX`, mimicking a real lazy-load/reflow. `arcade_metrics._stale_frame_offset_ms`
  converts the click's miss-distance from the target's actual (post-shift) position into
  milliseconds using *this player's own* locally-measured cursor speed from the ambient
  `pointer_sample` stream in the run-up to the click — not a guessed constant.
- `c4_whack_a_mole` — go/no-go: click green, avoid red, 10 rounds under time pressure.
  Deliberately designed to induce a few human mistakes — a session with zero errors here
  is itself informative. Emits a `stage_result` *per round* (10 data points), not one
  aggregate at the end, which is what actually makes `error_rate_floor` meaningful now
  instead of resting on 2-3 near-perfect stages.

- `b2_draw_shape` — trace a circle by pressing, dragging all the way around its
  outline, and releasing. The signal isn't the final accuracy — it's that a real
  drag naturally produces dozens of intermediate pointer samples along a
  continuously curving path, which is specifically expensive for a script to
  fake cheaply (`draw_shape_point_count` catches a script that "draws" via a
  couple of teleporting `mouse.move()` calls; see `sparse_draw_path` in
  `rule_based_scorer.py`, the one rule this stage actually feeds — everything
  else it captures, same as A4/A5/B1's newer fields, is visible on `/metrics`
  but not yet wired into scoring).

A3, A6 from the original design are still not built — `arcade_metrics.py` stubs
`path_optimality`/`backtrack_count`/`dead_end_rate` (need A6) as explicit `None`.

None of the four newest metrics (`latency_complexity_slope`, `stale_frame_offset_ms`,
`ipi_cv`, `backspace_rate`, plus the now-populated `error_rate_floor`) feed any
`rule_based_scorer.py` rule yet — captured and visible on `/metrics`, deliberately not
wired into scoring, same "don't recalibrate without real data" discipline as everything
else in this file.

**Polish**: `.fade-in` (`arcade.html`) is applied to each stage's freshly-created wrap
element and to the reveal screen — a plain CSS keyframes animation, no JS retrigger logic
needed since each is a new DOM node. The countdown's `.pop` class *is* reused across ticks
on the same element, so it needs the remove/reflow/re-add dance (`popTick()` in
`arcade.js`) to replay — a bare `classList.add` on an already-present class is a no-op.
A1's floating shapes also rotate slowly (`s.rot`/`s.rotSpeed`, independent of bounce
velocity) — since that sets an inline `transform`, it has to restate the
`translate(-50%,-50%)` centering the CSS class normally provides, or the shape jumps.

**Known correctness bug class, already hit once**: a stage's `mount()` typically drives
its own `requestAnimationFrame`/`setTimeout` chains (see `c1_flash.js`'s wash animation,
`a1_perception.js`'s floating shapes). If the player finishes the stage (clicks) before
that chain naturally completes, the chain must be cancelled — otherwise it keeps running
(and drawing to a canvas/DOM the harness may have already torn down for the next stage)
after the stage has already reported in. Both existing animated stages guard every
scheduled continuation with a `done` check for this reason; any new stage with an
animation loop needs the same guard, not just an `onDone` deduplication check.

**Known Playwright-testing gotcha, not a game bug**: floating targets (A1) never satisfy
Playwright's default `.click()` actionability check, which waits for an element to be
"stable" (stationary for 2 consecutive frames) before clicking — a continuously-animated
target never stabilizes, so a naive test hangs until its click timeout. Use
`page.click(selector, force=True)` (or a coordinate-based click) against A1's shapes.
This is arguably a feature, not just a testing footgun: it means high-level
element-based-click automation frameworks (Selenium/Playwright's own `.click()`) can't
trivially drive this stage the same way a human or a raw-coordinate CDP call can.

**Reveal + leaderboard**: `GET /api/verdict/{id}`
(coarse verdict only) then `POST /api/reveal` (`claimed_type`/`tool`, plus optional
`player_score`, `name`, `email`). `name` is free text rendered back to every future
visitor via the leaderboard — `arcade.html` escapes it through `textContent`/`innerHTML`
round-tripping before ever interpolating it, since it's the one place in this codebase
that renders user-supplied text back as markup (stored-XSS risk otherwise). `email` is
optional, never displayed, and deliberately excluded from both `GET /api/leaderboard` and
the `/api/sessions` debug endpoint — `storage.leaderboard()` only ever selects
name/score/timestamp. `GET /api/leaderboard?label=<human|bot_script|agent>` ranks by
`player_score DESC`, `trust='verified'` only, blank names default to "Anonymous" via SQL
`COALESCE`. `arcade.html`'s reveal screen also shows headline stats (C1 reaction time, A5
WPM) pulled from `onComplete`'s `stageResults`.

`GET /leaderboard` (`leaderboard.html`) is the same three tabs standalone — reachable
without playing, linked from `/arcade`'s header — for people who just want to check
standings. Hits the same `/api/leaderboard` endpoint the reveal screen uses; no separate
backend logic to keep in sync.

Not built from the original `/arcade` spec: A3/A6.

## Google Sheets sync

`test_site/google_sheets.py::append_reveal()` mirrors every revealed name/email (plus
label/tool/player_score/session_id) to a Google Sheet, called from `/api/reveal` right
after the DB write. Deliberately fire-and-forget — every exception is swallowed and
logged, never raised, because this is a bonus lead-capture side effect and must never be
able to break the reveal flow (the actual research data) for a player.

**Requires setup only a human with Google Cloud Console access can do** — I can't create
Google credentials myself:
1. In Google Cloud Console, enable the Google Sheets API on a project (new or existing).
2. Create a service account, generate a JSON key, download it.
3. Share the target spreadsheet with the service account's email address (found in the
   JSON key, looks like `...@...iam.gserviceaccount.com`) as an Editor.
4. Put the JSON key's file path in `.env` as `GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/key.json`
   (`.env` is gitignored — never commit the key itself).
5. Restart `test_site.server`.

Without that env var set, `append_reveal()` logs `GOOGLE_SERVICE_ACCOUNT_FILE not set —
skipping Sheets sync` once per process and no-ops on every reveal after that — verified
this doesn't affect the reveal response (`POST /api/reveal` still returns `200 {"ok":
true}` and the DB write still happens) before wiring it in. `_attempted` caches a failed
connection attempt for the process lifetime too, so a broken/missing credential doesn't
retry (and re-log) on every single reveal.

Target spreadsheet ID (`SPREADSHEET_ID` in that file) is hardcoded to the one sheet this
was built for: `1IF8T_-kPBSjQ3ppH1xjnFBqpL_ZdBQCPVufzjOxTR-k`. First successful connection
writes a header row if the sheet is empty.

## Conventions

- Python 3.11+, FastAPI + uvicorn for the test server, Playwright for automation, SQLite
  for storage (no ORM — plain `sqlite3`).
- Session labels are assigned at traffic-generation time via a `?label=` query param on
  `/arcade` for generator scripts (`agent_raw_cdp`, `agent_llm_cdp`, `agent_stealth_cdp`
  — see `GENERATOR_LABELS` in `arcade.js`). A real player gets no query param and stays
  `pending` until they self-report at reveal — see above.
- Keep signal modules pure functions: `(session_id, conn) -> dict of signal values`. The
  scorer composes them; don't let scoring logic leak into signal extraction.
- When a phase's real implementation is out of scope for now, write a stub with a `# TODO`
  explaining what real work replaces it — don't silently skip it or fake a result.
- `data/traversal.db`'s schema evolves via additive migration in
  `storage._migrate()` (ALTER TABLE ADD COLUMN, idempotent backfills) — not a wipe.
  Existing captured sessions are real research data; don't discard them over a schema
  change if a migration can preserve them instead.

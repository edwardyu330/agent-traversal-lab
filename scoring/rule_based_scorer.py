"""Rule-based session scorer: combines signal modules into a single 0-100
"how automation-like is this session" score, and an allow/step-up/block band.

Rule NAMES and WHICH SIGNALS feed them are still hand-designed and only change
when someone edits this file (see no_pointer_or_click_telemetry's addition
below for an example of that happening — a real batch of agent_llm_cdp/Browser
Use sessions surfacing a structural gap, not a guess). The POINT VALUES are a
different story: they live in scoring/weights_store.py's data/scorer_weights.json,
loaded fresh on every score_session() call, and update_weights_from_reveal()
(below) nudges them automatically on every /api/reveal where the verdict shown
didn't match what the player claimed to be. That's live and fully automatic, by
explicit request — see that function's docstring for what "automatic" actually
means here, including the honor-system self-report exposure it comes with.

=== no_pointer_or_click_telemetry ===

stage_result events prove interaction happened; zero pointer_sample and zero
click_detail events for the whole session prove it didn't happen through a real
dispatched pointer/click, since both streams are ambient captures wired to DOM
event bubbling — a human or any CDP/OS-level-driven click always leaves a trail in
one of them. 6/6 fresh agent_llm_cdp sessions hit this; 0/16 agent_raw_cdp, 0/5
agent_stealth_cdp, and the human sessions with any arcade data did not. Weighted at
55 specifically to clear coarse_verdict's score>=50 "Bot" threshold on its own —
the point raised was "make it confirm this is a bot," and previously nothing did.

=== Why raw-automation and arcade signals are scored separately ===

webdriver_flag / headless_ua / suspicious_webgl_renderer are 100% accurate against
raw CDP/headless automation (see webdriver_artifacts.py) but read 0 against a real,
non-headless browser under agent control — the one live Browser Use session in this
project's dataset scored 5/100 under the old composite despite being an agent,
precisely because these three signals dominated the score and none of them fired.
They're still worth capturing (cheap, unambiguous when they do fire), just not as
the backbone of the main score anymore. Reported as raw_automation_score,
separately, never summed into the main score.

The main score is now carried by signals/arcade_metrics.py — perception-mode and
decision-structure probes, cadence shape, and pointer/click geometry, which don't
depend on any particular automation stack leaving CDP-shaped fingerprints. Sessions
with no /arcade data (storefront-only) simply skip every arcade-derived rule, same
pattern as any other missing-signal case in this file.

=== Removed, not replaced with a new constant ===

reasoning_pause_fraction's hardcoded 1-15s window (timing_analysis.py) is gone from
this scorer. It was never validated against real data — see analysis/inspect_timing.py's
findings on the one real agent_llm_cdp session, where the actual per-step latency
spanned roughly 2.8s-16s and most "interaction" gaps were sub-10ms keystroke noise
drowning out the two real pauses. cadence_cv (below) replaces it structurally, not
as a retuned constant: it measures the *shape* of inter-action timing (how uniform
it is) rather than betting on a specific duration band.

teleport_click_fraction as a binary rule is also gone — mouse_geometry.py's
extraction still runs and mean_path_curvature is still scored, but the crude
"had any teleport clicks at all" threshold is superseded by arcade_metrics.py's
click_offset_scatter/correction_count/overshoot_rate, which measure the same
underlying idea (does this session's pointer behavior look like it teleports
straight to targets) with continuous, richer signal instead of one binary flag.
"""

import sqlite3

from scoring.weights_store import load_weights, nudge, save_weights
from signals import arcade_metrics, human_baseline, mouse_geometry, network_fingerprint, timing_analysis, webdriver_artifacts

BANDS = (
    (70, "block"),
    (40, "step_up"),
    (0, "allow"),
)

# A player's own claimed_type on /api/reveal, collapsed to automation-or-not —
# see update_weights_from_reveal()'s docstring for what this drives and why.
CLAIMED_TYPE_TO_AUTOMATION = {"human": False, "bot_script": True, "agent": True}


def _band(score: float) -> str:
    for threshold, name in BANDS:
        if score >= threshold:
            return name
    return "allow"


def _score_raw_automation(wd: dict, weights: dict) -> tuple[float, list[dict]]:
    breakdown = []
    if wd["webdriver_flag"]:
        breakdown.append({"rule": "webdriver_flag", "points": weights["webdriver_flag"],
                           "reason": "navigator.webdriver reported true"})
    if wd["suspicious_webgl_renderer"]:
        breakdown.append({"rule": "suspicious_webgl_renderer", "points": weights["suspicious_webgl_renderer"],
                           "reason": f"software-rendered WebGL: {wd['webgl_renderer_strings']}"})
    if wd["headless_ua"]:
        breakdown.append({"rule": "headless_ua", "points": weights["headless_ua"],
                           "reason": "user agent contains 'HeadlessChrome'"})
    return min(100, sum(r["points"] for r in breakdown)), breakdown


def _score_arcade(arcade: dict, weights: dict, human_ranges: dict) -> list[dict]:
    breakdown = []

    def add(name: str, reason: str):
        points = weights[name]
        if points:
            breakdown.append({"rule": name, "points": points, "reason": reason})

    if arcade["perception_mode"] == "dom":
        add("dom_only_perception",
            "acted on the instruction only present in hidden DOM (visually + aria hidden) — "
            "a real human or vision-grounded agent can't see it")

    if arcade["dom_only_target_hit"]:
        add("dom_order_over_visual_order",
            "clicked the box matching source-order position, not visual position")

    cv = arcade["cadence_cv"]
    if cv is not None:
        if cv < 0.15:
            add("uniform_cadence", f"inter-action timing coefficient of variation {cv:.2f} (near-constant)")
        elif cv < 0.3:
            add("fairly_uniform_cadence", f"inter-action timing coefficient of variation {cv:.2f}")

    corrections = arcade["correction_count"]
    if corrections is not None and corrections < 0.5:
        add("no_path_corrections", f"mean {corrections:.1f} direction reversals before a click")

    overshoot = arcade["overshoot_rate"]
    if overshoot is not None and overshoot < 0.1:
        add("no_overshoot", f"overshoot on {overshoot:.0%} of clicks")

    scatter = arcade["click_offset_scatter"]
    if scatter is not None and scatter < 2:
        add("dead_center_clicks", f"click-offset-from-center scatter {scatter:.1f}px")

    density = arcade["pointer_sample_density"]
    if density is not None and density < 1:
        add("sparse_pointer_movement", f"{density:.2f} pointer samples/sec")

    error_rate = arcade["error_rate_floor"]
    if error_rate is not None and error_rate == 0:
        add("zero_errors", "no incorrect stage attempts, across stages designed to induce some")

    if arcade["no_pointer_or_click_telemetry"]:
        add("no_pointer_or_click_telemetry",
            "stage results were recorded but zero pointer_sample/click_detail events exist for "
            "the whole session — a real pointer/click always leaves a trail in one of those "
            "streams, so progress with neither means the game was driven by something other "
            "than a dispatched pointer/click event")

    if error_rate is not None and error_rate >= 0.85:
        add("chronic_incorrect_spam",
            f"wrong on {error_rate:.0%} of attempts, including stages that reset and gave repeat "
            "tries on a wrong answer — getting it wrong nearly every single time, even across "
            "retries, isn't a human having an off day, it's clicking without attempting the task")

    if arcade["draw_shape_attempted"] and arcade["draw_shape_point_count"] is not None and arcade["draw_shape_point_count"] < 15:
        add("sparse_draw_path",
            f"traced the circle with only {arcade['draw_shape_point_count']} recorded points — a "
            "real drag naturally produces dozens of intermediate samples, not a handful")

    # Cross-referencing layer, not another hand-picked bot tell: does this
    # session look like what real humans actually produce, independent of
    # whether it also matches a catalogued automation pattern. See
    # signals/human_baseline.py. Two or more signals outside the human
    # 5th-95th percentile band together is the bar, not one alone — a single
    # signal drifting outside typical human range happens by chance even for
    # real humans (that's what "5th-95th", not "0th-100th", means).
    outside = human_baseline.signals_outside_range(arcade, human_ranges)
    if len(outside) >= 2:
        add("outside_human_baseline",
            f"{len(outside)} signals fall outside the range real human sessions actually produce "
            f"(5th-95th percentile): {', '.join(outside)}")

    return breakdown


def score_session(session_id: str, conn: sqlite3.Connection, weights: dict | None = None) -> dict:
    # Loaded fresh every call, not cached at import time — this is exactly what
    # makes the scorer "live": a weight nudged by update_weights_from_reveal()
    # a second ago is already in effect for the very next /api/verdict call.
    # `weights` can be passed explicitly to score against a CANDIDATE set that
    # isn't live yet — see update_weights_from_reveal()'s human-safety check,
    # which needs to ask "what would this nudge do" before committing it.
    weights = weights if weights is not None else load_weights()

    wd = webdriver_artifacts.extract(session_id, conn)
    timing = timing_analysis.extract(session_id, conn)
    mouse = mouse_geometry.extract(session_id, conn)
    network = network_fingerprint.extract(session_id, conn)
    arcade = arcade_metrics.extract(session_id, conn)
    human_ranges = human_baseline.compute_human_ranges(conn)

    raw_automation_score, raw_automation_breakdown = _score_raw_automation(wd, weights)

    breakdown = []

    def add(name: str, reason: str):
        points = weights[name]
        if points:
            breakdown.append({"rule": name, "points": points, "reason": reason})

    curvature = mouse["mean_path_curvature"]
    if curvature is not None:
        if curvature <= 1.05:
            add("straight_line_mouse_path", f"mean path curvature {curvature:.2f} (~straight line)")
        elif curvature <= 1.3:
            add("mostly_straight_mouse_path", f"mean path curvature {curvature:.2f}")

    breakdown.extend(_score_arcade(arcade, weights, human_ranges))

    total = min(100, sum(r["points"] for r in breakdown))

    return {
        "session_id": session_id,
        "score": total,
        "band": _band(total),
        "breakdown": breakdown,
        "raw_automation_score": raw_automation_score,
        "raw_automation_breakdown": raw_automation_breakdown,
        # The two tracks never get summed (that would let raw-automation dominate
        # again, exactly what we're demoting it to avoid) — but neither should a
        # session with strong raw-automation tells and zero arcade data (i.e. every
        # /play-only session, and every /arcade session until it has enough stage
        # data) read as a false "Human" just because the arcade track is empty.
        # Either track independently earning a high score is enough to call it.
        "overall_detection_score": max(total, raw_automation_score),
        "signals": {
            "webdriver_artifacts": wd,
            "timing_analysis": timing,
            "mouse_geometry": mouse,
            "network_fingerprint": network,
            "arcade_metrics": arcade,
        },
    }


def _nudge_creates_human_false_positive(current_weights: dict, candidate_weights: dict, conn: sqlite3.Connection) -> bool:
    """True if scoring every known human-labeled session under candidate_weights
    would newly flip any of them from Human to Bot, compared to current_weights.
    Sessions already misclassified under current_weights are skipped — this
    catches a nudge making things WORSE, not pre-existing problems it didn't
    cause (those need their own fix, not a block on unrelated future nudges)."""
    from signals.common import get_session, list_session_ids

    for sid in list_session_ids(conn):
        if get_session(conn, sid).get("label") != "human":
            continue
        before = score_session(sid, conn, weights=current_weights)["overall_detection_score"]
        if before >= 50:
            continue
        after = score_session(sid, conn, weights=candidate_weights)["overall_detection_score"]
        if after >= 50:
            return True
    return False


def update_weights_from_reveal(session_id: str, claimed_type: str, conn: sqlite3.Connection) -> dict | None:
    """Live weight update, run on every /api/reveal. This is the "algorithm
    constantly changes as people signal which type of user they are" behavior,
    by explicit request — fully automatic, no human review step. Worth being
    clear about what that means: /api/reveal is honor-system (no consistency
    check against telemetry, see server.py), so this is a feedback loop wired
    directly to unverified, player-controlled input. A bot operator who always
    clicks "Human" on reveal can push these weights down over many sessions.
    WEIGHT_BOUNDS in weights_store.py (0-80 per rule) is the safety net for
    that — it limits how much damage a poisoning campaign can do, it doesn't
    prevent it. If that turns out to be a real problem, the fix is upstream of
    this function (e.g. only counting reveals whose claim doesn't contradict
    strong independent signals), not inside it.

    Weights only move on a MISCLASSIFICATION (perceptron-style: correct
    verdicts reinforce nothing, since there's nothing to correct), and only
    the rules in whichever track (arcade vs. raw-automation) actually
    determined overall_detection_score — the other track wasn't "responsible"
    for the wrong verdict shown, so it isn't touched. A session where nothing
    fired at all (e.g. a fully-evasive bot with zero tripped rules) has
    nothing to nudge — this mechanism can strengthen or weaken existing
    signals, it can't invent a new one.

    Returns an outcome dict for EVERY recognized claimed_type — correct
    verdicts included, not just misclassifications — so the caller (server.py's
    /api/reveal) can persist a `detection_accuracy` event either way. This is
    the actual accuracy record: every self-report, human included, not just
    the ones that happened to move a weight. Returns None only when
    claimed_type isn't in CLAIMED_TYPE_TO_AUTOMATION (no ground truth to check
    against at all).
    """
    if claimed_type not in CLAIMED_TYPE_TO_AUTOMATION:
        return None
    true_automation = CLAIMED_TYPE_TO_AUTOMATION[claimed_type]

    result = score_session(session_id, conn)
    verdict = coarse_verdict(result["overall_detection_score"])
    predicted_automation = result["overall_detection_score"] >= 50
    correct = predicted_automation == true_automation

    nudged_rules = None
    if not correct:
        dominant = result["breakdown"] if result["score"] >= result["raw_automation_score"] else result["raw_automation_breakdown"]
        if dominant:
            direction = 1 if true_automation else -1
            current_weights = load_weights()
            candidate = dict(current_weights)
            nudge(candidate, [r["rule"] for r in dominant], direction)
            # Safety valve, only relevant when strengthening (direction=1) —
            # weakening a rule can only ever help humans, never hurt them.
            # Simulates the candidate weights against every known human
            # session BEFORE committing; if any human that currently reads
            # correctly would flip to a false positive under the candidate,
            # skip this nudge rather than apply it. Found necessary the hard
            # way: no_path_corrections drifted 10->42 purely from repeated,
            # individually-correct stealth-bot nudges, and at 42 it started
            # misclassifying real humans with unremarkable, low-correction
            # mouse paths — each nudge was locally justified, the accumulated
            # result wasn't. This doesn't make the system safe against a
            # sustained poisoning campaign (see this function's main
            # docstring), it makes it safe against catching real humans in
            # the crossfire of legitimately-earned bot-catching pressure.
            if direction == 1 and _nudge_creates_human_false_positive(current_weights, candidate, conn):
                nudged_rules = None
            else:
                save_weights(candidate)
                nudged_rules = [r["rule"] for r in dominant]

    return {
        "correct": correct,
        "claimed_type": claimed_type,
        "true_automation": true_automation,
        "predicted_automation": predicted_automation,
        "predicted_verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
        "overall_detection_score": result["overall_detection_score"],
        "nudged_rules": nudged_rules,
    }


def coarse_verdict(score: float) -> dict:
    """Score -> the only two fields the /play and /arcade reveal screens are
    allowed to show. Deliberately excludes band/breakdown/signals/raw_automation_*:
    those double as an evasion guide for an audience specifically trying to beat
    this scorer, which is the one thing the reveal screen must not hand out.
    Confidence is symmetric around the score's midpoint (50 = a coin flip either
    way) — a presentation mapping only, doesn't touch the underlying weights.

    "Bot", not "Agent": the score can't tell a mechanical script from an
    LLM-driven agent apart (that distinction only exists as ground truth on our
    own labeled generator sessions — see LABEL_DISPLAY_NAMES in server.py, where
    "Agent" is reserved for the known agent_llm_cdp case specifically). Calling
    every real-world detection "Agent" when most of what trips this is a plain
    script overclaims what the score actually knows.
    """
    if score >= 50:
        return {"verdict": "Bot", "confidence": round(score)}
    return {"verdict": "Human", "confidence": round(100 - score)}

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
from signals import arcade_metrics, mouse_geometry, network_fingerprint, timing_analysis, webdriver_artifacts

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


def _score_arcade(arcade: dict, weights: dict) -> list[dict]:
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

    return breakdown


def score_session(session_id: str, conn: sqlite3.Connection) -> dict:
    # Loaded fresh every call, not cached at import time — this is exactly what
    # makes the scorer "live": a weight nudged by update_weights_from_reveal()
    # a second ago is already in effect for the very next /api/verdict call.
    weights = load_weights()

    wd = webdriver_artifacts.extract(session_id, conn)
    timing = timing_analysis.extract(session_id, conn)
    mouse = mouse_geometry.extract(session_id, conn)
    network = network_fingerprint.extract(session_id, conn)
    arcade = arcade_metrics.extract(session_id, conn)

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

    breakdown.extend(_score_arcade(arcade, weights))

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

    Only adjusts on a MISCLASSIFICATION (perceptron-style: correct verdicts
    reinforce nothing, since there's nothing to correct), and only the rules
    in whichever track (arcade vs. raw-automation) actually determined
    overall_detection_score — the other track wasn't "responsible" for the
    wrong verdict shown, so it isn't touched. A session where nothing fired at
    all (e.g. a fully-evasive bot with zero tripped rules) has nothing to
    nudge — this mechanism can strengthen or weaken existing signals, it can't
    invent a new one.
    """
    if claimed_type not in CLAIMED_TYPE_TO_AUTOMATION:
        return None
    true_automation = CLAIMED_TYPE_TO_AUTOMATION[claimed_type]

    result = score_session(session_id, conn)
    predicted_automation = result["overall_detection_score"] >= 50
    if predicted_automation == true_automation:
        return None

    dominant = result["breakdown"] if result["score"] >= result["raw_automation_score"] else result["raw_automation_breakdown"]
    if not dominant:
        return None

    direction = 1 if true_automation else -1
    weights = load_weights()
    nudge(weights, [r["rule"] for r in dominant], direction)
    save_weights(weights)
    return weights


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

"""Rule-based session scorer: combines signal modules into a single 0-100
"how automation-like is this session" score, and an allow/step-up/block band.
Weights below are first-pass guesses meant to be checked against a labeled dataset
(see analysis/compare_agent_vs_human.py), not tuned yet — treat them as a starting
point to falsify, not a finished model. This build does not recalibrate them: we
have zero human /arcade sessions yet, and producing that data is the point of
/arcade existing.

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

from signals import arcade_metrics, mouse_geometry, network_fingerprint, timing_analysis, webdriver_artifacts

BANDS = (
    (70, "block"),
    (40, "step_up"),
    (0, "allow"),
)

CURVATURE_STRAIGHT_WEIGHT = 10
CURVATURE_MOSTLY_STRAIGHT_WEIGHT = 5

RAW_AUTOMATION_WEIGHTS = {
    "webdriver_flag": 40,
    "suspicious_webgl_renderer": 25,
    "headless_ua": 15,
}


def _band(score: float) -> str:
    for threshold, name in BANDS:
        if score >= threshold:
            return name
    return "allow"


def _score_raw_automation(wd: dict) -> tuple[float, list[dict]]:
    breakdown = []
    if wd["webdriver_flag"]:
        breakdown.append({"rule": "webdriver_flag", "points": RAW_AUTOMATION_WEIGHTS["webdriver_flag"],
                           "reason": "navigator.webdriver reported true"})
    if wd["suspicious_webgl_renderer"]:
        breakdown.append({"rule": "suspicious_webgl_renderer", "points": RAW_AUTOMATION_WEIGHTS["suspicious_webgl_renderer"],
                           "reason": f"software-rendered WebGL: {wd['webgl_renderer_strings']}"})
    if wd["headless_ua"]:
        breakdown.append({"rule": "headless_ua", "points": RAW_AUTOMATION_WEIGHTS["headless_ua"],
                           "reason": "user agent contains 'HeadlessChrome'"})
    return min(100, sum(r["points"] for r in breakdown)), breakdown


def _score_arcade(arcade: dict) -> list[dict]:
    breakdown = []

    def add(name: str, points: float, reason: str):
        if points:
            breakdown.append({"rule": name, "points": points, "reason": reason})

    if arcade["perception_mode"] == "dom":
        add("dom_only_perception", 35,
            "acted on the instruction only present in hidden DOM (visually + aria hidden) — "
            "a real human or vision-grounded agent can't see it")

    if arcade["dom_only_target_hit"]:
        add("dom_order_over_visual_order", 20,
            "clicked the box matching source-order position, not visual position")

    cv = arcade["cadence_cv"]
    if cv is not None:
        if cv < 0.15:
            add("uniform_cadence", 20, f"inter-action timing coefficient of variation {cv:.2f} (near-constant)")
        elif cv < 0.3:
            add("fairly_uniform_cadence", 8, f"inter-action timing coefficient of variation {cv:.2f}")

    corrections = arcade["correction_count"]
    if corrections is not None and corrections < 0.5:
        add("no_path_corrections", 10, f"mean {corrections:.1f} direction reversals before a click")

    overshoot = arcade["overshoot_rate"]
    if overshoot is not None and overshoot < 0.1:
        add("no_overshoot", 5, f"overshoot on {overshoot:.0%} of clicks")

    scatter = arcade["click_offset_scatter"]
    if scatter is not None and scatter < 2:
        add("dead_center_clicks", 10, f"click-offset-from-center scatter {scatter:.1f}px")

    density = arcade["pointer_sample_density"]
    if density is not None and density < 1:
        add("sparse_pointer_movement", 10, f"{density:.2f} pointer samples/sec")

    error_rate = arcade["error_rate_floor"]
    if error_rate is not None and error_rate == 0:
        add("zero_errors", 8, "no incorrect stage attempts, across stages designed to induce some")

    return breakdown


def score_session(session_id: str, conn: sqlite3.Connection) -> dict:
    wd = webdriver_artifacts.extract(session_id, conn)
    timing = timing_analysis.extract(session_id, conn)
    mouse = mouse_geometry.extract(session_id, conn)
    network = network_fingerprint.extract(session_id, conn)
    arcade = arcade_metrics.extract(session_id, conn)

    raw_automation_score, raw_automation_breakdown = _score_raw_automation(wd)

    breakdown = []

    def add(name: str, points: float, reason: str):
        if points:
            breakdown.append({"rule": name, "points": points, "reason": reason})

    curvature = mouse["mean_path_curvature"]
    if curvature is not None:
        if curvature <= 1.05:
            add("straight_line_mouse_path", CURVATURE_STRAIGHT_WEIGHT, f"mean path curvature {curvature:.2f} (~straight line)")
        elif curvature <= 1.3:
            add("mostly_straight_mouse_path", CURVATURE_MOSTLY_STRAIGHT_WEIGHT, f"mean path curvature {curvature:.2f}")

    breakdown.extend(_score_arcade(arcade))

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


def coarse_verdict(score: float) -> dict:
    """Score -> the only two fields the /play and /arcade reveal screens are
    allowed to show. Deliberately excludes band/breakdown/signals/raw_automation_*:
    those double as an evasion guide for an audience specifically trying to beat
    this scorer, which is the one thing the reveal screen must not hand out.
    Confidence is symmetric around the score's midpoint (50 = a coin flip either
    way) — a presentation mapping only, doesn't touch the underlying weights.
    """
    if score >= 50:
        return {"verdict": "Agent", "confidence": round(score)}
    return {"verdict": "Human", "confidence": round(100 - score)}

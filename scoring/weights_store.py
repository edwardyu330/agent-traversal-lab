"""Persisted, live-adjustable rule weights for rule_based_scorer.py.

Every previous version of this scorer had its point values as Python literals —
fixed until someone edited the file. This module moves them into
data/scorer_weights.json (gitignored, same tier as traversal.db) so
update_weights_from_reveal() in rule_based_scorer.py can adjust them at
runtime, in response to what players say they actually were on /api/reveal.

DEFAULT_WEIGHTS is the starting point (identical to this scorer's original
hand-set values) and the fallback for any rule name the file doesn't have yet
(forward-compatible with new rules added to the scorer later). WEIGHT_BOUNDS
is the safety net for the fact that /api/reveal is honor-system and
unverified — see CLAIMED_TYPE_TO_AUTOMATION and update_weights_from_reveal()'s
docstring in rule_based_scorer.py — clamping every weight to [0, MAX_WEIGHT]
so no amount of adversarial self-report can zero out a signal entirely or
blow one up into single-handedly deciding every verdict.
"""

import json
from pathlib import Path

WEIGHTS_PATH = Path(__file__).resolve().parent.parent / "data" / "scorer_weights.json"

MIN_WEIGHT = 0
MAX_WEIGHT = 80

LEARNING_RATE = 4  # points nudged per misclassified reveal

DEFAULT_WEIGHTS = {
    # raw_automation_score track (webdriver_artifacts.py)
    "webdriver_flag": 40,
    "suspicious_webgl_renderer": 25,
    "headless_ua": 15,
    # score track — mouse_geometry.py
    "straight_line_mouse_path": 10,
    "mostly_straight_mouse_path": 5,
    # score track — arcade_metrics.py
    "dom_only_perception": 35,
    "dom_order_over_visual_order": 20,
    "uniform_cadence": 20,
    "fairly_uniform_cadence": 8,
    "no_path_corrections": 10,
    "no_overshoot": 5,
    "dead_center_clicks": 10,
    "sparse_pointer_movement": 10,
    "zero_errors": 8,
    "no_pointer_or_click_telemetry": 55,
    "sparse_draw_path": 15,
    "chronic_incorrect_spam": 60,
    "outside_human_baseline": 20,
}


def _clamp(value: float) -> float:
    return max(MIN_WEIGHT, min(MAX_WEIGHT, value))


def load_weights() -> dict:
    if not WEIGHTS_PATH.exists():
        save_weights(dict(DEFAULT_WEIGHTS))
        return dict(DEFAULT_WEIGHTS)
    try:
        stored = json.loads(WEIGHTS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_WEIGHTS)
    # Merge so a rule added to the scorer after this file was first written
    # still gets a value, instead of silently scoring 0 forever.
    return {**DEFAULT_WEIGHTS, **stored}


def save_weights(weights: dict) -> None:
    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_PATH.write_text(json.dumps(weights, indent=2, sort_keys=True))


def nudge(weights: dict, rule_names: list[str], direction: int) -> dict:
    """direction=+1 reinforces (a real bot/agent was under-scored), -1 weakens
    (a real human was over-scored). Only touches rules that actually fired."""
    for name in rule_names:
        weights[name] = _clamp(weights.get(name, DEFAULT_WEIGHTS.get(name, 0)) + direction * LEARNING_RATE)
    return weights

"""Persisted state for run_playwright_stealth.py's adaptive evasion choice —
the other half of the co-evolution loop alongside scoring/weights_store.py.
The detector adapts its rule weights from what players self-report
(update_weights_from_reveal); this lets the "smart adversary" bot adapt its
own evasion choice from whether IT just got caught, using the exact same
score_session() breakdown a real adversary probing this system would only get
by trial and error — read here via direct DB access (this is our own test
harness, not the public API coarse_verdict() deliberately restricts) so the
learning loop can close within the same process.

Per-evasion-level running catch rate, weighted-random choice biased away from
whichever level is currently getting caught most — not a hard cutover, so it
keeps sampling caught levels occasionally rather than assuming one bad run
means "always avoid this now." Converges toward "full" over time precisely
because "full" is the one level with no existing signal to be caught on (see
run_playwright_stealth.py's module docstring) — that convergence is itself
the finding: the live weight system can push back on no_patch/no_curve, it
structurally cannot touch full evasion.
"""

import json
import random
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "adversary_state.json"

EVASION_LEVELS = ["full", "no_patch", "no_curve"]
MIN_WEIGHT = 0.15  # floor so a level that's currently 100% caught still gets sampled sometimes


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {level: {"caught": 0, "total": 0} for level in EVASION_LEVELS}
    try:
        stored = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        stored = {}
    return {level: stored.get(level, {"caught": 0, "total": 0}) for level in EVASION_LEVELS}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def choose_evasion(state: dict) -> str:
    weights = []
    for level in EVASION_LEVELS:
        s = state[level]
        catch_rate = (s["caught"] / s["total"]) if s["total"] else 0.0
        weights.append(max(MIN_WEIGHT, 1.0 - catch_rate))
    return random.choices(EVASION_LEVELS, weights=weights, k=1)[0]


def record_outcome(state: dict, evasion: str, caught: bool) -> None:
    if evasion not in state:
        return
    state[evasion]["total"] += 1
    if caught:
        state[evasion]["caught"] += 1

"""Inter-action timing signals. The hypothesis this is testing: raw CDP automation
acts near-instantly with tight, uniform gaps; humans have variable but mostly
sub-second "think time" between actions; an LLM-driven agent should show a distinct
"reasoning-pause" signature — gaps shaped like an inference round trip (roughly
1-15s, one per tool call) rather than either of the other two distributions.
"""

import sqlite3
import statistics

from signals.common import get_events

INTERACTION_TYPES = {"click", "keydown", "add_to_cart", "task_complete"}
REASONING_PAUSE_MS = (1000, 15000)


def extract(session_id: str, conn: sqlite3.Connection) -> dict:
    events = get_events(conn, session_id)
    interactions = [e for e in events if e["type"] in INTERACTION_TYPES]

    if len(interactions) < 2:
        return {
            "interaction_count": len(interactions),
            "session_duration_ms": 0.0,
            "gap_mean_ms": None,
            "gap_median_ms": None,
            "gap_stdev_ms": None,
            "gap_max_ms": None,
            "reasoning_pause_fraction": None,
        }

    timestamps = [e["client_ts"] for e in interactions]
    gaps_ms = [(b - a) for a, b in zip(timestamps, timestamps[1:])]
    lo, hi = REASONING_PAUSE_MS
    reasoning_pauses = [g for g in gaps_ms if lo <= g <= hi]

    return {
        "interaction_count": len(interactions),
        "session_duration_ms": timestamps[-1] - timestamps[0],
        "gap_mean_ms": statistics.mean(gaps_ms),
        "gap_median_ms": statistics.median(gaps_ms),
        "gap_stdev_ms": statistics.stdev(gaps_ms) if len(gaps_ms) > 1 else 0.0,
        "gap_max_ms": max(gaps_ms),
        "reasoning_pause_fraction": len(reasoning_pauses) / len(gaps_ms),
    }


def event_gaps(
    session_id: str,
    conn: sqlite3.Connection,
    types: set[str] | None = None,
    exclude_types: set[str] | None = None,
) -> list[dict]:
    """Ordered events plus the gap (ms) since the previous event of the same filtered
    set. `types=None` returns every event type (minus `exclude_types`, if given).
    Filtering happens before gaps are computed, so an excluded event's timestamp
    never leaks into a neighboring gap. Raw/diagnostic — not used by `extract` or the
    scorer; for inspecting what a gap distribution actually looks like before picking
    a window like REASONING_PAUSE_MS.
    """
    events = get_events(conn, session_id)
    if types is not None:
        events = [e for e in events if e["type"] in types]
    if exclude_types:
        events = [e for e in events if e["type"] not in exclude_types]

    rows = []
    prev_ts = None
    for e in events:
        gap_ms = (e["client_ts"] - prev_ts) if prev_ts is not None else None
        rows.append({"type": e["type"], "page": e["page"], "client_ts": e["client_ts"], "gap_ms": gap_ms})
        prev_ts = e["client_ts"]
    return rows

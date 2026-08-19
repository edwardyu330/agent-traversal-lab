"""Empirical human-behavior ranges, computed from labeled human sessions —
the cross-referencing layer. Every other rule in rule_based_scorer.py asks
"does this match a known bot tell" (positive evidence FOR automation, found
by hand). This asks the inverse question directly: does this value fall
outside what real humans actually produce, regardless of whether it also
matches any specific bot signature. A session can dodge every hand-picked
rule in the file and still be behaviorally nothing like a human — a value so
far from what real people produce that it's suspicious on its own terms, not
because it resembles a catalogued bot pattern. That's what this catches.

Computed fresh from signals/arcade_metrics.py on every human-labeled session
currently in the DB, every call — not cached. The human sample is small
(dozens, not thousands) so this stays cheap for now; revisit with real
caching if/when the human sample grows enough to make that a real cost.

5th-95th percentile band, not min/max — a single outlier human shouldn't set
the entire boundary. MIN_HUMAN_SAMPLES gates this off entirely below a sample
size where a "range" is really just noise — better to say nothing than to
flag against a range computed from 3 people.
"""

import sqlite3

from signals import arcade_metrics
from signals.common import get_session, list_session_ids

# Signals picked because they measure something continuous and physically
# grounded (timing uniformity, click precision, drawing precision) rather
# than a categorical choice — a percentile band on a categorical field
# wouldn't mean anything.
BASELINE_SIGNALS = ["cadence_cv", "click_offset_scatter", "ipi_cv", "draw_shape_mean_deviation_px"]
MIN_HUMAN_SAMPLES = 5


def compute_human_ranges(conn: sqlite3.Connection) -> dict[str, tuple[float, float, int] | None]:
    values: dict[str, list[float]] = {sig: [] for sig in BASELINE_SIGNALS}
    for session_id in list_session_ids(conn):
        if get_session(conn, session_id).get("label") != "human":
            continue
        metrics = arcade_metrics.extract(session_id, conn)
        for sig in BASELINE_SIGNALS:
            v = metrics.get(sig)
            if v is not None:
                values[sig].append(v)

    ranges: dict[str, tuple[float, float, int] | None] = {}
    for sig, vals in values.items():
        if len(vals) < MIN_HUMAN_SAMPLES:
            ranges[sig] = None
            continue
        vals_sorted = sorted(vals)
        lo = vals_sorted[max(0, int(len(vals_sorted) * 0.05))]
        hi = vals_sorted[min(len(vals_sorted) - 1, int(len(vals_sorted) * 0.95))]
        ranges[sig] = (lo, hi, len(vals_sorted))
    return ranges


def signals_outside_range(arcade: dict, ranges: dict) -> list[str]:
    """Which BASELINE_SIGNALS fall outside the human 5th-95th percentile band
    for this session, skipping any signal with no range (too few humans) or
    no value (this session never produced it)."""
    outside = []
    for sig, r in ranges.items():
        if r is None:
            continue
        lo, hi, _n = r
        v = arcade.get(sig)
        if v is None:
            continue
        if v < lo or v > hi:
            outside.append(sig)
    return outside

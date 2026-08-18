"""Diagnostic tool, not part of the pipeline: dump and histogram the raw inter-event
gap distribution for one session, so a timing window (REASONING_PAUSE_MS et al.) can
be picked by looking at real data instead of guessed and then curve-fit to it.

Prints two views: every captured event type, and the INTERACTION_TYPES subset that
timing_analysis.extract() actually scores on — the gap between those two views is
usually the interesting part.
"""

import argparse

from signals.common import get_conn
from signals.timing_analysis import INTERACTION_TYPES, event_gaps

BUCKETS_MS = [10, 100, 1000, 5000, 15000, 30000, float("inf")]
BUCKET_LABELS = ["<10ms", "10-100ms", "100ms-1s", "1-5s", "5-15s", "15-30s", "30s+"]


def bucket_index(gap_ms: float) -> int:
    for i, upper in enumerate(BUCKETS_MS):
        if gap_ms < upper:
            return i
    return len(BUCKETS_MS) - 1


def print_histogram(label: str, gaps_ms: list[float]) -> None:
    print(f"\n{label} (n={len(gaps_ms)} gaps)")
    if not gaps_ms:
        print("  (no gaps)")
        return
    counts = [0] * len(BUCKET_LABELS)
    for g in gaps_ms:
        counts[bucket_index(g)] += 1
    max_count = max(counts)
    for bucket_label, count in zip(BUCKET_LABELS, counts):
        bar = "#" * (40 * count // max_count) if max_count else ""
        print(f"  {bucket_label:>10}  {count:4d}  {bar}")


def print_large_gaps(label: str, rows: list[dict], threshold_ms: float = 1000) -> None:
    print(f"\n{label} — gaps >= {threshold_ms:.0f}ms:")
    found = False
    for r in rows:
        if r["gap_ms"] is not None and r["gap_ms"] >= threshold_ms:
            found = True
            print(f"  +{r['gap_ms']:9.1f}ms  before {r['type']:20s} {r['page']}")
    if not found:
        print("  (none)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument(
        "--include-http-headers",
        action="store_true",
        help="include the one-off http_headers event (excluded by default — it's session "
        "metadata fired once at session start, not a traversal-path event)",
    )
    args = parser.parse_args()

    exclude = set() if args.include_http_headers else {"http_headers"}

    with get_conn() as conn:
        all_rows = event_gaps(args.session_id, conn, exclude_types=exclude)
        interaction_rows = event_gaps(args.session_id, conn, types=INTERACTION_TYPES)

    all_gaps = [r["gap_ms"] for r in all_rows if r["gap_ms"] is not None]
    interaction_gaps = [r["gap_ms"] for r in interaction_rows if r["gap_ms"] is not None]

    print(f"Session {args.session_id}")
    print(f"Total events: {len(all_rows)} (excluding: {exclude or 'none'})")
    print(f"INTERACTION_TYPES events: {len(interaction_rows)} ({sorted(INTERACTION_TYPES)})")

    print_histogram("ALL event types", all_gaps)
    print_histogram("INTERACTION_TYPES only (what extract() scores on)", interaction_gaps)

    print_large_gaps("ALL event types", all_rows)
    print_large_gaps("INTERACTION_TYPES only", interaction_rows)


if __name__ == "__main__":
    main()

"""Read-only audit of traversal.db composition and telemetry quality — run
before seeding more data or touching any scoring weights. Never mutates the
database. See the report this produces for what's populated vs. broken.
"""

import statistics
from collections import Counter

from signals.arcade_metrics import extract as arcade_extract
from signals.common import get_events, get_session, list_session_ids
from test_site.storage import get_conn

ARCADE_STAGE_ORDER = [
    "c1_flash_reaction",
    "a1_perception_probe",
    "c4_whack_a_mole",
    "a2_visual_vs_dom_order",
    "b1_layout_shift",
    "a5_type_phrase",
    "a4_complexity_ramp",
]

NUMERIC_METRICS = [
    "cadence_cv",
    "pointer_sample_density",
    "coalesced_event_ratio",
    "click_offset_scatter",
    "correction_count",
    "overshoot_rate",
    "error_rate_floor",
    "frame_jank_ratio",
    "latency_complexity_slope",
    "stale_frame_offset_ms",
    "ipi_cv",
    "backspace_rate",
    "path_optimality",
    "backtrack_count",
    "dead_end_rate",
]
CATEGORICAL_METRICS = ["perception_mode", "visual_vs_dom_order_choice"]
BOOL_METRICS = ["dom_only_target_hit", "all_clicks_trusted"]

JANK_COMPROMISED_THRESHOLD = 0.10  # >10% dropped frames = timing data suspect


def classify_surface(session: dict) -> str:
    fp = session.get("first_page")
    if fp == "/arcade":
        return "arcade"
    if fp is None:
        return "pure_http_reveal"
    return "storefront"


def stage_progress(events: list[dict]) -> tuple[set, bool]:
    reached = {e["payload"].get("stage_id") for e in events if e["type"] == "stage_result"}
    completed = any(e["type"] == "arcade_complete" for e in events)
    return reached, completed


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> None:
    with get_conn() as conn:
        session_ids = list_session_ids(conn)
        sessions = {sid: get_session(conn, sid) for sid in session_ids}

        section(f"1. DATASET AUDIT — {len(session_ids)} total sessions")

        by_label = Counter(s.get("label") for s in sessions.values())
        by_trust = Counter(s.get("trust") for s in sessions.values())
        by_surface = Counter(classify_surface(s) for s in sessions.values())

        print("\nBy label:")
        for k, v in by_label.most_common():
            print(f"  {k or '(null)'}: {v}")
        print("\nBy trust:")
        for k, v in by_trust.most_common():
            print(f"  {k or '(null)'}: {v}")
        print("\nBy collection surface:")
        for k, v in by_surface.most_common():
            print(f"  {k}: {v}")

        arcade_ids = [sid for sid, s in sessions.items() if classify_surface(s) == "arcade"]
        print(f"\n--- Arcade completion ({len(arcade_ids)} arcade-surface sessions) ---")

        completed = 0
        drop_at = Counter()
        events_by_id = {}
        for sid in arcade_ids:
            events = get_events(conn, sid)
            events_by_id[sid] = events
            reached, is_complete = stage_progress(events)
            if is_complete:
                completed += 1
            else:
                idxs = [ARCADE_STAGE_ORDER.index(r) for r in reached if r in ARCADE_STAGE_ORDER]
                if not idxs:
                    drop_at["never reached any stage_result"] += 1
                else:
                    last = max(idxs)
                    nxt = ARCADE_STAGE_ORDER[last + 1] if last + 1 < len(ARCADE_STAGE_ORDER) else "(reveal step)"
                    drop_at[f"dropped before/at: {nxt}"] += 1

        print(f"Completed all {len(ARCADE_STAGE_ORDER)} stages: {completed}/{len(arcade_ids)}")
        if drop_at:
            print("Drop-off breakdown (stage they never reached a result for):")
            for k, v in drop_at.most_common():
                print(f"  {k}: {v}")

        # ==================================================================
        section("2. TELEMETRY QUALITY — arcade_metrics.py fields")
        # ==================================================================
        arcade_results = {sid: arcade_extract(sid, conn) for sid in arcade_ids}
        n = len(arcade_ids)
        print(f"\nDenominator: {n} arcade-surface sessions\n")

        def report_numeric(name):
            vals = [r[name] for r in arcade_results.values() if r[name] is not None]
            pct = (len(vals) / n * 100) if n else 0
            print(f"\n{name}: {len(vals)}/{n} populated ({pct:.0f}%)")
            if not vals:
                print("  *** NULL EVERYWHERE — not wired up, or no session has produced it yet ***")
                return
            uniq = set(round(v, 6) if isinstance(v, float) else v for v in vals)
            if len(uniq) == 1:
                print(f"  *** CONSTANT across every session: {vals[0]!r} — suspicious, check wiring ***")
            print(f"  min={min(vals):.4g}  max={max(vals):.4g}  mean={statistics.mean(vals):.4g}  "
                  f"median={statistics.median(vals):.4g}")
            print(f"  raw values: {[round(v, 3) if isinstance(v, float) else v for v in vals]}")

        def report_categorical(name):
            vals = [r[name] for r in arcade_results.values() if r[name] is not None]
            pct = (len(vals) / n * 100) if n else 0
            print(f"\n{name}: {len(vals)}/{n} populated ({pct:.0f}%)")
            if not vals:
                print("  *** NULL EVERYWHERE ***")
                return
            print(f"  distribution: {dict(Counter(vals))}")

        def report_bool(name):
            vals = [r[name] for r in arcade_results.values() if r[name] is not None]
            pct = (len(vals) / n * 100) if n else 0
            print(f"\n{name}: {len(vals)}/{n} populated ({pct:.0f}%)")
            if not vals:
                print("  *** NULL EVERYWHERE ***")
                return
            true_rate = sum(1 for v in vals if v) / len(vals)
            print(f"  true rate: {true_rate:.2f}  ({sum(1 for v in vals if v)}/{len(vals)} true)")

        print("--- Categorical ---")
        for m in CATEGORICAL_METRICS:
            report_categorical(m)
        print("\n--- Boolean ---")
        for m in BOOL_METRICS:
            report_bool(m)
        print("\n--- Numeric ---")
        for m in NUMERIC_METRICS:
            report_numeric(m)

        # ------------------------------------------------------------------
        section("2a. SPECIFIC SANITY CHECKS")
        # ------------------------------------------------------------------
        densities = [r["pointer_sample_density"] for r in arcade_results.values() if r["pointer_sample_density"] is not None]
        print("\npointer_sample_density check (tens vs hundreds/sec):")
        if densities:
            print(f"  values: {[round(d, 1) for d in densities]}")
            print(f"  mean: {statistics.mean(densities):.1f} samples/sec")
            if statistics.mean(densities) < 50:
                print("  *** LOW — consistent with throttled mousemove, not raw pointerrawupdate/coalesced capture ***")
            else:
                print("  Consistent with unthrottled high-frequency capture.")
        else:
            print("  no data")

        print("\nFrame jank check:")
        jank_vals = [r["frame_jank_ratio"] for r in arcade_results.values() if r["frame_jank_ratio"] is not None]
        if jank_vals:
            compromised = sum(1 for j in jank_vals if j > JANK_COMPROMISED_THRESHOLD)
            print(f"  {compromised}/{len(jank_vals)} sessions exceed {JANK_COMPROMISED_THRESHOLD:.0%} dropped-frame ratio")
            print(f"  jank ratios: {[round(j, 4) for j in jank_vals]}")
        else:
            print("  no data")
        print("  low_quality flag exists in codebase: NO (grepped — frame_jank_ratio is computed but never used to flag/gate anything)")

        print("\nevent.isTrusted check:")
        trusted_vals = [r["all_clicks_trusted"] for r in arcade_results.values() if r["all_clicks_trusted"] is not None]
        print(f"  all_clicks_trusted populated: {len(trusted_vals)}/{n}")
        print(f"  values: {trusted_vals}")

        print("\nstale_frame_offset_ms sanity (B1):")
        stale_vals = [r["stale_frame_offset_ms"] for r in arcade_results.values() if r["stale_frame_offset_ms"] is not None]
        if stale_vals:
            print(f"  values: {[round(v, 1) for v in stale_vals]}")
            implausible = [v for v in stale_vals if v > 5000 or v < 0]
            if implausible:
                print(f"  *** {len(implausible)} implausible value(s) (>5000ms or negative) — check for near-zero-speed divide ***")
            else:
                print("  all values in plausible range")
        else:
            print("  no B1 data yet")


if __name__ == "__main__":
    main()

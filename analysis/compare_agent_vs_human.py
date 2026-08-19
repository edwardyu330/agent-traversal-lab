"""Phase 4 — score every captured session and report where the signals do (and
don't) separate agent traffic from humans. Run after generating some mix of
agent_raw_cdp / agent_llm_cdp / human sessions via agent_runner/.
"""

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from scoring.rule_based_scorer import score_session
from signals.common import get_session, list_session_ids
from test_site.storage import get_conn

EXPECTED_LABELS = ("human", "agent_raw_cdp", "agent_llm_cdp")

NUMERIC_SIGNALS = [
    ("timing_analysis", "session_duration_ms"),
    ("timing_analysis", "gap_median_ms"),
    ("timing_analysis", "reasoning_pause_fraction"),
    ("mouse_geometry", "mean_path_curvature"),
    ("mouse_geometry", "teleport_click_fraction"),
    ("arcade_metrics", "cadence_cv"),
    ("arcade_metrics", "pointer_sample_density"),
    ("arcade_metrics", "click_offset_scatter"),
    ("arcade_metrics", "correction_count"),
    ("arcade_metrics", "overshoot_rate"),
    ("arcade_metrics", "error_rate_floor"),
    ("arcade_metrics", "frame_jank_ratio"),
    ("arcade_metrics", "latency_complexity_slope"),
    ("arcade_metrics", "stale_frame_offset_ms"),
    ("arcade_metrics", "ipi_cv"),
    ("arcade_metrics", "backspace_rate"),
    ("arcade_metrics", "coalesced_event_ratio"),
    ("arcade_metrics", "coalesced_extra_samples_per_batch"),
    ("arcade_metrics", "stale_element_interaction_rate"),
    ("arcade_metrics", "draw_shape_point_count"),
    ("arcade_metrics", "draw_shape_mean_deviation_px"),
    ("arcade_metrics", "draw_shape_angular_coverage_deg"),
]
BOOL_SIGNALS = [
    ("webdriver_artifacts", "webdriver_flag"),
    ("webdriver_artifacts", "suspicious_webgl_renderer"),
    ("webdriver_artifacts", "headless_ua"),
    ("arcade_metrics", "dom_only_target_hit"),
    ("arcade_metrics", "all_clicks_trusted"),
    ("arcade_metrics", "has_pointerrawupdate"),
    ("arcade_metrics", "no_pointer_or_click_telemetry"),
    ("arcade_metrics", "draw_shape_attempted"),
]
# String-valued signals — kept separate from NUMERIC_SIGNALS since they can't go
# through pd.to_numeric or a mean() aggregation; shown as value-count breakdowns.
CATEGORICAL_SIGNALS = [
    ("arcade_metrics", "perception_mode"),
    ("arcade_metrics", "visual_vs_dom_order_choice"),
]

# Human-readable labels for "group.key" columns — used by /metrics (server.py) so
# the dashboard doesn't just dump raw signal-module field names at a reader.
SIGNAL_LABELS = {
    "timing_analysis.session_duration_ms": "Session duration (ms)",
    "timing_analysis.gap_median_ms": "Median action gap (ms)",
    "timing_analysis.reasoning_pause_fraction": "Reasoning-pause fraction",
    "mouse_geometry.mean_path_curvature": "Mouse path curvature",
    "mouse_geometry.teleport_click_fraction": "Teleport-click fraction",
    "arcade_metrics.cadence_cv": "Action cadence (CV)",
    "arcade_metrics.pointer_sample_density": "Pointer samples / sec",
    "arcade_metrics.click_offset_scatter": "Click offset scatter (px)",
    "arcade_metrics.correction_count": "Path corrections / click",
    "arcade_metrics.overshoot_rate": "Overshoot rate",
    "arcade_metrics.error_rate_floor": "Error rate (whack-a-mole etc.)",
    "arcade_metrics.frame_jank_ratio": "Dropped-frame ratio",
    "arcade_metrics.latency_complexity_slope": "Latency / complexity slope (ms per item)",
    "arcade_metrics.stale_frame_offset_ms": "Stale-frame offset (ms)",
    "arcade_metrics.ipi_cv": "Keystroke interval (CV)",
    "arcade_metrics.backspace_rate": "Backspace rate",
    "arcade_metrics.coalesced_event_ratio": "Coalesced pointer-event ratio",
    "arcade_metrics.coalesced_extra_samples_per_batch": "Extra samples per coalesced batch",
    "arcade_metrics.stale_element_interaction_rate": "Stale-element click rate",
    "webdriver_artifacts.webdriver_flag": "navigator.webdriver flag",
    "webdriver_artifacts.suspicious_webgl_renderer": "Software-rendered WebGL",
    "webdriver_artifacts.headless_ua": "Headless user-agent",
    "arcade_metrics.dom_only_target_hit": "Fell for DOM-order trap",
    "arcade_metrics.all_clicks_trusted": "All clicks isTrusted",
    "arcade_metrics.has_pointerrawupdate": "Supports pointerrawupdate",
    "arcade_metrics.no_pointer_or_click_telemetry": "Zero pointer/click telemetry despite progress",
    "arcade_metrics.draw_shape_point_count": "Draw-circle sample count",
    "arcade_metrics.draw_shape_mean_deviation_px": "Draw-circle mean deviation (px)",
    "arcade_metrics.draw_shape_angular_coverage_deg": "Draw-circle angular coverage (deg)",
    "arcade_metrics.draw_shape_attempted": "Attempted the draw-circle stage",
}


def build_dataframe(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = []
    for session_id in list_session_ids(conn):
        label = get_session(conn, session_id).get("label", "unknown")
        result = score_session(session_id, conn)
        row = {
            "session_id": session_id,
            "label": label,
            "score": result["score"],  # arcade/mouse-curvature composite only
            "raw_automation_score": result["raw_automation_score"],
            "overall_detection_score": result["overall_detection_score"],  # max of both — what /api/verdict uses
            "band": result["band"],
        }
        for group, key in NUMERIC_SIGNALS + BOOL_SIGNALS + CATEGORICAL_SIGNALS:
            row[f"{group}.{key}"] = result["signals"][group][key]
        rows.append(row)
    df = pd.DataFrame(rows)
    for group, key in NUMERIC_SIGNALS:
        df[f"{group}.{key}"] = pd.to_numeric(df[f"{group}.{key}"], errors="coerce")
    return df


def render_report(df: pd.DataFrame) -> str:
    lines = ["# Agent vs. human session comparison", ""]

    counts = df["label"].value_counts() if not df.empty else {}
    lines.append("## Dataset")
    lines.append("")
    lines.append("| label | sessions |")
    lines.append("|---|---|")
    for label in EXPECTED_LABELS:
        lines.append(f"| {label} | {counts.get(label, 0)} |")
    other = set(df["label"]) - set(EXPECTED_LABELS) if not df.empty else set()
    for label in other:
        lines.append(f"| {label} | {counts.get(label, 0)} |")
    lines.append("")

    missing = [l for l in EXPECTED_LABELS if counts.get(l, 0) == 0]
    if missing:
        lines.append("**Missing labels — results below are partial until these exist:**")
        for label in missing:
            how = {
                "human": "`python -m agent_runner.run_human_baseline` (run ~20-30x)",
                "agent_raw_cdp": "`python -m agent_runner.run_playwright_raw --count 20`",
                "agent_llm_cdp": "`python -m agent_runner.run_browser_use --count 20` (needs ANTHROPIC_API_KEY)",
            }[label]
            lines.append(f"- `{label}`: {how}")
        lines.append("")

    if df.empty:
        lines.append("No sessions captured yet.")
        return "\n".join(lines)

    lines.append("## Score by label")
    lines.append("")
    lines.append(
        "`overall_detection_score` is what `/api/verdict` actually uses (max of the "
        "arcade/curvature composite and the separate raw-automation track — see "
        "rule_based_scorer.py). `score` and `raw_automation_score` are the two tracks "
        "that feed it, shown separately so it's visible which one is carrying a given "
        "label; they are never summed."
    )
    lines.append("")
    score_stats = (
        df.groupby("label")[["score", "raw_automation_score", "overall_detection_score"]]
        .agg(["mean", "min", "max"])
        .round(1)
    )
    lines.append(score_stats.to_markdown())
    lines.append("")

    lines.append("## Signal means by label")
    lines.append("")
    lines.append("Where this shows a clean gap between `human` and the `agent_*` rows, that")
    lines.append("signal is pulling weight. Where it doesn't, the rule/weight likely needs")
    lines.append("rework before relying on it.")
    lines.append("")
    signal_cols = [f"{g}.{k}" for g, k in NUMERIC_SIGNALS]
    means = df.groupby("label")[signal_cols].mean(numeric_only=True).round(3)
    lines.append(means.to_markdown())
    lines.append("")

    lines.append("## Boolean artifact rates by label")
    lines.append("")
    bool_cols = [f"{g}.{k}" for g, k in BOOL_SIGNALS]
    rates = df.groupby("label")[bool_cols].mean(numeric_only=True).round(2)
    lines.append(rates.to_markdown())
    lines.append("")

    if len(set(df["label"])) >= 2:
        lines.append("## Score distribution overlap")
        lines.append("")
        lines.append(
            "Per-label score range above shows whether bands (`allow` <40, `step_up` "
            "40-69, `block` >=70) would cleanly separate these labels at this sample "
            "size, or whether ranges overlap enough that the current weights need "
            "adjustment."
        )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="analysis/report.md")
    args = parser.parse_args()

    with get_conn() as conn:
        df = build_dataframe(conn)

    report = render_report(df)
    out_path = Path(args.out)
    out_path.write_text(report)
    print(f"Wrote {out_path} ({len(df)} sessions)")


if __name__ == "__main__":
    main()

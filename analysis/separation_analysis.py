"""One-off separation analysis for the internal memo on which arcade_metrics
signals go into the Shopify plugin. NOT part of the scoring pipeline — this
deliberately ignores the live-adapted weights in scoring/weights_store.py and
scores each of the original 12 arcade_metrics signals on its own, human vs
agent_raw_cdp and human vs agent_llm_cdp, ranked by AUC on the second pair.

Session grouping is by ORIGIN, not current label: many raw_cdp/llm_cdp
sessions have since self-reported through /api/reveal and had their `label`
column overwritten (e.g. agent_raw_cdp -> bot_script), with the origin
preserved in `tool`. A session counts as a group member if EITHER label or
tool matches, so this covers sessions from before that auto-reveal existed
too.

Run: python -m analysis.separation_analysis
"""

import random
import statistics

from signals import arcade_metrics
from signals.common import get_session, list_session_ids
from test_site.storage import get_conn

METRICS = [
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
]

# Nullity-as-feature re-run, per user request: absence of pointer/typing data
# is inherent to a programmatic click/fill, not missing data to drop. These
# are booleans/fractions (0/1-valued), not continuous — auc()/bootstrap_auc_ci()
# both work unchanged on them since Python bool compares as int.
NULLITY_METRICS = [
    "has_pointer_samples",
    "has_typing_data",
    "has_movement_before_click",
    "no_pointer_or_click_telemetry",
]


def session_groups(conn):
    human, raw_cdp, llm_cdp, wild = [], [], [], []
    for sid in list_session_ids(conn):
        s = get_session(conn, sid)
        label, tool = s.get("label"), s.get("tool")
        if label == "human":
            human.append(sid)
        elif label == "wild_scanner_suspected":
            wild.append(sid)
        elif label == "agent_raw_cdp" or tool == "agent_raw_cdp":
            raw_cdp.append(sid)
        elif label == "agent_llm_cdp" or tool == "agent_llm_cdp":
            llm_cdp.append(sid)
    return human, raw_cdp, llm_cdp, wild


def extract_all(conn, session_ids):
    return [arcade_metrics.extract(sid, conn) for sid in session_ids]


def auc(pos_values, neg_values):
    """Mann-Whitney-U-based AUC = P(pos > neg) + 0.5*P(pos == neg), where
    "pos" is the class we're calling automation-like for this comparison."""
    if not pos_values or not neg_values:
        return None
    wins = 0.0
    for p in pos_values:
        for n in neg_values:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos_values) * len(neg_values))


def paired_values(rows_a, rows_b, metric):
    a = [r[metric] for r in rows_a if r.get(metric) is not None]
    b = [r[metric] for r in rows_b if r.get(metric) is not None]
    return a, b


def bootstrap_auc_ci(pos, neg, n_boot=3000, seed=0):
    rng = random.Random(seed)
    if len(pos) < 2 or len(neg) < 2:
        return None
    vals = []
    for _ in range(n_boot):
        p_sample = [rng.choice(pos) for _ in pos]
        n_sample = [rng.choice(neg) for _ in neg]
        a = auc(p_sample, n_sample)
        if a is not None:
            vals.append(a)
    if not vals:
        return None
    vals.sort()
    lo = vals[int(0.025 * len(vals))]
    hi = vals[min(len(vals) - 1, int(0.975 * len(vals)))]
    return lo, hi


def summ(vals):
    if not vals:
        return "n=0"
    return f"n={len(vals)} median={statistics.median(vals):.4g} range=[{min(vals):.4g}, {max(vals):.4g}]"


def build_composite_loocv(rows_by_id, human_ids, other_ids, metric_directions, other_label="other"):
    """metric_directions: {metric: +1 or -1}, +1 meaning "higher = more
    automation-like" for that metric, -1 meaning lower does. Composite is an
    unweighted mean of per-metric z-scores (oriented so higher composite =
    more automation-like), z computed against the HUMAN population's
    mean/stdev — refit on every LOO fold using only the training humans, so
    no session's own value ever leaks into its own normalization.

    Returns per-session (session_id, true_label, loocv_score) tuples, only
    for sessions with at least one of the composite metrics populated.
    `other_label` is just what true_label reads for the non-human group in
    the output — lets this same function score any agent population against
    human, not just agent_llm_cdp (see analysis/adversarial_rescore.py).
    """
    pool = [(sid, "human") for sid in human_ids] + [(sid, other_label) for sid in other_ids]
    metrics = list(metric_directions.keys())
    out = []
    for held_out_sid, true_label in pool:
        train_human_ids = [sid for sid in human_ids if sid != held_out_sid]
        train_stats = {}
        for m in metrics:
            vals = [rows_by_id[sid][m] for sid in train_human_ids if rows_by_id[sid].get(m) is not None]
            if len(vals) >= 3:
                train_stats[m] = (statistics.mean(vals), statistics.pstdev(vals) or 1e-9)
        z_scores = []
        for m in metrics:
            v = rows_by_id[held_out_sid].get(m)
            if v is None or m not in train_stats:
                continue
            mean, std = train_stats[m]
            z = (v - mean) / std
            z_scores.append(z * metric_directions[m])
        if not z_scores:
            continue
        out.append((held_out_sid, true_label, statistics.mean(z_scores), len(z_scores)))
    return out


def roc_points(scored, other_label="other"):
    """scored: list of (label, score) with label in {'human', other_label}.
    Returns sorted unique thresholds with confusion-matrix counts at each."""
    thresholds = sorted({s for _, s in scored})
    points = []
    for t in thresholds:
        tp = sum(1 for lbl, s in scored if lbl == other_label and s >= t)
        fn = sum(1 for lbl, s in scored if lbl == other_label and s < t)
        fp = sum(1 for lbl, s in scored if lbl == "human" and s >= t)
        tn = sum(1 for lbl, s in scored if lbl == "human" and s < t)
        points.append((t, tp, fn, fp, tn))
    return points


def per_metric_separation(human_rows, other_rows, metrics):
    """Human vs one other group, per metric. Used both for the human-vs-
    agent_raw_cdp/agent_llm_cdp report and (see adversarial_rescore.py) for
    human-vs-stealth-typing-adversary."""
    results = {}
    for metric in metrics:
        pos, neg = paired_values(other_rows, human_rows, metric)
        results[metric] = {
            "human_vals": neg,
            "other_vals": pos,
            "auc": auc(pos, neg),
        }
    return results


def run_composite(rows_by_id, human_ids, other_ids, name, directions, other_label="other"):
    print(f"\n=== LOOCV COMPOSITE: {name} ===")
    print(f"metrics + direction (+1=higher is more automation-like): {directions}")
    scored_full = build_composite_loocv(rows_by_id, human_ids, other_ids, directions, other_label)
    scored = [(lbl, s) for _sid, lbl, s, _k in scored_full]
    n_human_scored = sum(1 for lbl, _ in scored if lbl == "human")
    n_other_scored = sum(1 for lbl, _ in scored if lbl == other_label)
    print(f"sessions scored: human={n_human_scored}/{len(human_ids)}  {other_label}={n_other_scored}/{len(other_ids)}")
    human_scores = [s for lbl, s in scored if lbl == "human"]
    other_scores = [s for lbl, s in scored if lbl == other_label]
    loocv_auc = auc(other_scores, human_scores)
    print(f"LOOCV AUC (human vs {other_label}): {loocv_auc}")
    ci = bootstrap_auc_ci(other_scores, human_scores)
    if ci:
        print(f"95% bootstrap CI: [{ci[0]:.2f}, {ci[1]:.2f}]")

    points = roc_points(scored, other_label)
    if not points:
        print("no scoreable sessions — every session had none of the composite's metrics populated")
        return {"scored": scored_full, "auc": loocv_auc, "ci": ci, "threshold": None,
                "tp": 0, "fn": n_other_scored, "fp": 0, "tn": n_human_scored}
    best = max(points, key=lambda p: (p[1] / max(1, p[1] + p[2])) - (p[3] / max(1, p[3] + p[4])))
    t, tp, fn, fp, tn = best
    print(f"Youden-optimal threshold on LOOCV scores: {t:.3f}")
    print(f"  confusion matrix @ threshold: TP({other_label} caught)={tp} FN({other_label} missed)={fn} "
          f"FP(human flagged)={fp} TN(human clear)={tn}")
    print(f"  {other_label} catch rate: {tp / max(1, tp + fn):.0%}   human false-positive rate: {fp / max(1, fp + tn):.0%}")
    return {"scored": scored_full, "auc": loocv_auc, "ci": ci, "threshold": t,
            "tp": tp, "fn": fn, "fp": fp, "tn": tn}


def examine_unscoreable(rows_by_id, other_ids, metrics, conn):
    """Sessions with NONE of `metrics` populated — are they a coverage gap or
    the most obviously non-human sessions in the set? Cross-checks against
    has_pointer_samples/has_typing_data/no_pointer_or_click_telemetry, which
    are populated independently of whether the composite's own metrics fired."""
    unscoreable = [sid for sid in other_ids if all(rows_by_id[sid].get(m) is None for m in metrics)]
    print(f"\n=== UNSCOREABLE SESSIONS (none of {metrics} populated): {len(unscoreable)}/{len(other_ids)} ===")
    for sid in unscoreable:
        row = rows_by_id[sid]
        print(f"  {sid[:8]}: has_pointer_samples={row.get('has_pointer_samples')} "
              f"has_typing_data={row.get('has_typing_data')} "
              f"no_pointer_or_click_telemetry={row.get('no_pointer_or_click_telemetry')} "
              f"ran_arcade={row.get('ran_arcade')}")
    return unscoreable


def main():
    with get_conn() as conn:
        human_ids, raw_ids, llm_ids, wild_ids = session_groups(conn)
        human_rows = extract_all(conn, human_ids)
        raw_rows = extract_all(conn, raw_ids)
        llm_rows = extract_all(conn, llm_ids)
        wild_rows = extract_all(conn, wild_ids)

    print(f"GROUP SIZES: human={len(human_ids)} agent_raw_cdp={len(raw_ids)} "
          f"agent_llm_cdp={len(llm_ids)} wild_scanner={len(wild_ids)}")

    all_metrics = METRICS + NULLITY_METRICS

    results = {}
    for metric in all_metrics:
        raw_pos, raw_neg = paired_values(raw_rows, human_rows, metric)
        llm_pos, llm_neg = paired_values(llm_rows, human_rows, metric)
        auc_raw = auc(raw_pos, raw_neg)
        auc_llm = auc(llm_pos, llm_neg)
        results[metric] = {
            "human_vals": llm_neg,
            "raw_vals": raw_pos,
            "llm_vals": llm_pos,
            "auc_human_vs_raw": auc_raw,
            "auc_human_vs_llm": auc_llm,
        }

    def strength(item):
        a = item[1]["auc_human_vs_llm"]
        return abs(a - 0.5) if a is not None else -1

    ranked = sorted(results.items(), key=strength, reverse=True)

    print("\n=== PER-METRIC SEPARATION, ranked by |AUC-0.5| on human vs agent_llm_cdp ===\n")
    for metric, r in ranked:
        tag = " [NULLITY]" if metric in NULLITY_METRICS else ""
        print(f"--- {metric}{tag} ---")
        print(f"  human:   {summ(r['human_vals'])}")
        print(f"  raw_cdp: {summ(r['raw_vals'])}   AUC(human vs raw_cdp) = {r['auc_human_vs_raw']}")
        print(f"  llm_cdp: {summ(r['llm_vals'])}   AUC(human vs llm_cdp) = {r['auc_human_vs_llm']}")
        ci = bootstrap_auc_ci(r["llm_vals"], r["human_vals"])
        if ci:
            print(f"  95% bootstrap CI (human vs llm_cdp AUC): [{ci[0]:.2f}, {ci[1]:.2f}]")
        print()

    rows_by_id = {}
    for sid, row in zip(human_ids, human_rows):
        rows_by_id[sid] = row
    for sid, row in zip(llm_ids, llm_rows):
        rows_by_id[sid] = row

    examine_unscoreable(rows_by_id, llm_ids, ["pointer_sample_density", "ipi_cv", "backspace_rate", "coalesced_event_ratio"], conn)

    stat_only = run_composite(
        rows_by_id, human_ids, llm_ids,
        "statistical-best (ignores deployability)",
        {"error_rate_floor": 1, "ipi_cv": -1, "pointer_sample_density": -1},
        other_label="llm_cdp",
    )
    # Naive nullity composite — included to show its own failure mode, not
    # because it's the recommended one. has_pointer_samples' 2 human false
    # positives are BOTH sessions with ran_arcade=False (a real visitor who
    # bounced before playing, never any_pointer_sample by construction —
    # nothing to do with automation). has_typing_data barely separates at all
    # (AUC ~0.47, CI crosses 0.5) since Browser Use's fill still emits
    # key_detail events, just with near-zero interval — presence isn't the
    # tell, timing (ipi_cv) is. See the engagement-gated version below.
    naive_nullity = run_composite(
        rows_by_id, human_ids, llm_ids,
        "naive nullity (has_pointer_samples/has_typing_data/has_movement_before_click, ungated)",
        {"has_pointer_samples": -1, "has_typing_data": -1, "has_movement_before_click": -1},
        other_label="llm_cdp",
    )
    # Engagement-gated version: no_pointer_or_click_telemetry (already in
    # arcade_metrics.py, requires proof of progress via a stage_result AND
    # zero pointer/click trail) instead of raw has_pointer_samples. Fixes the
    # naive version's false-positive source by construction — a bounce with
    # zero engagement can't satisfy the "made progress" half of the gate, so
    # it can't be misread as automation-like.
    plugin_spec = run_composite(
        rows_by_id, human_ids, llm_ids,
        "plugin-spec (deployable metrics + engagement-gated nullity)",
        {"pointer_sample_density": -1, "ipi_cv": -1, "backspace_rate": -1, "coalesced_event_ratio": -1,
         "no_pointer_or_click_telemetry": 1},
        other_label="llm_cdp",
    )

    return {
        "human_ids": human_ids, "raw_ids": raw_ids, "llm_ids": llm_ids, "wild_ids": wild_ids,
        "human_rows": human_rows, "raw_rows": raw_rows, "llm_rows": llm_rows, "wild_rows": wild_rows,
        "results": results, "ranked": ranked,
        "stat_only_composite": stat_only, "plugin_spec_composite": plugin_spec,
        "naive_nullity_composite": naive_nullity,
    }


if __name__ == "__main__":
    main()

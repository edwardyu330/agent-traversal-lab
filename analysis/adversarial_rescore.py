"""Adversarial durability test — Part 2 of the separation-analysis follow-up.

The memo's own conclusion was that a stealth-postured agent (real mouse-path
movement before clicks, human-paced character-by-character typing) would
evade the plugin spec. This re-scores that claim directly against real
sessions from agent_runner/run_playwright_stealth_typing.py instead of
speculating: same curved-mouse-approach-before-every-click evasion as the
existing agent_stealth_cdp profile, PLUS backspace-simulating human-ish
typing that agent_stealth_cdp never had.

Expected and fine: ipi_cv should collapse toward 0.5 (its perfect AUC in
agent_llm_cdp was a Browser Use fingerprint, not a fundamental limit — see
separation_analysis.py's docstring for that call). The actual question: does
anything in the plugin spec still separate this adversary from human, and
does the plugin-spec composite still catch it?

Run: python -m analysis.adversarial_rescore
(after agent_runner.run_playwright_stealth_typing has generated sessions)
"""

from signals import arcade_metrics
from analysis.separation_analysis import (
    METRICS, NULLITY_METRICS, auc, bootstrap_auc_ci, extract_all,
    paired_values, run_composite, summ,
)
from signals.common import get_session, list_session_ids
from test_site.storage import get_conn

ADVERSARY_LABEL = "agent_stealth_typing_cdp"


def session_groups(conn):
    human, adversary = [], []
    for sid in list_session_ids(conn):
        s = get_session(conn, sid)
        label, tool = s.get("label"), s.get("tool")
        if label == "human":
            human.append(sid)
        elif label == ADVERSARY_LABEL or tool == ADVERSARY_LABEL:
            adversary.append(sid)
    return human, adversary


def main():
    with get_conn() as conn:
        human_ids, adv_ids = session_groups(conn)
        human_rows = extract_all(conn, human_ids)
        adv_rows = extract_all(conn, adv_ids)

    print(f"GROUP SIZES: human={len(human_ids)} {ADVERSARY_LABEL}={len(adv_ids)}")
    if not adv_ids:
        print("No adversarial sessions found yet — run agent_runner.run_playwright_stealth_typing first.")
        return

    all_metrics = METRICS + NULLITY_METRICS

    print(f"\n=== PER-METRIC SEPARATION, human vs {ADVERSARY_LABEL} ===\n")
    results = {}
    for metric in all_metrics:
        pos, neg = paired_values(adv_rows, human_rows, metric)
        a = auc(pos, neg)
        results[metric] = a
        print(f"--- {metric} ---")
        print(f"  human: {summ(neg)}")
        print(f"  adversary: {summ(pos)}   AUC = {a}")
        ci = bootstrap_auc_ci(pos, neg)
        if ci:
            print(f"  95% bootstrap CI: [{ci[0]:.2f}, {ci[1]:.2f}]")
        print()

    rows_by_id = {}
    for sid, row in zip(human_ids, human_rows):
        rows_by_id[sid] = row
    for sid, row in zip(adv_ids, adv_rows):
        rows_by_id[sid] = row

    print("\n### Does the plugin spec still catch it? ###")
    run_composite(
        rows_by_id, human_ids, adv_ids,
        "plugin-spec (deployable metrics + engagement-gated nullity) vs stealth-typing adversary",
        {"pointer_sample_density": -1, "ipi_cv": -1, "backspace_rate": -1, "coalesced_event_ratio": -1,
         "no_pointer_or_click_telemetry": 1},
        other_label=ADVERSARY_LABEL,
    )

    print("\n### For reference: the statistical-best (non-deployable) composite ###")
    run_composite(
        rows_by_id, human_ids, adv_ids,
        "statistical-best vs stealth-typing adversary",
        {"error_rate_floor": 1, "ipi_cv": -1, "pointer_sample_density": -1},
        other_label=ADVERSARY_LABEL,
    )

    print("\n### Isolated: does anything survive alone? ###")
    for metric in all_metrics:
        pos, neg = paired_values(adv_rows, human_rows, metric)
        if not pos or not neg:
            continue
        run_composite(
            rows_by_id, human_ids, adv_ids, f"{metric} ALONE",
            {metric: 1 if (results[metric] or 0.5) >= 0.5 else -1},
            other_label=ADVERSARY_LABEL,
        )


if __name__ == "__main__":
    main()

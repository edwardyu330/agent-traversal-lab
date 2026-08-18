"""Signals derived from the /arcade stage games — the metric set the storefront/
`/play` flow's raw-automation signals (webdriver flag, WebGL renderer, teleport
clicks) can't produce, because a real-browser agent (Browser Use et al.) doesn't
trip any of them. These are built to catch the perception→decision→action loop
itself, not artifacts of a specific automation stack.

Only a subset is computable with the stages built so far (C1, A1, A2, A4, A5, B1, C4 —
see arcade.js's `window.ARCADE_STAGES` registry). Metrics needing a stage that doesn't
exist yet (A3, A6, B2) are explicit None + TODO, not faked.

KNOWN BLIND SPOT, same posture as webdriver_artifacts.py's headless-UA check:
`click_detail.is_trusted` (from the DOM's `event.isTrusted`) catches JS-injected
clicks (`element.click()`, `dispatchEvent`) but reads `true` for CDP-driven clicks
(`Input.dispatchMouseEvent`) same as a real click — cheap to log, not a real tell
against the adversary this build is actually built for. Logged for completeness,
not weighted as if it caught anything.
"""

import sqlite3
import statistics

from signals.common import get_events

CADENCE_EVENT_TYPES = {"click_detail", "stage_result"}
CORRECTION_WINDOW_MS = 500
JANK_THRESHOLD_MS = 20  # worse than 50fps


def _cv(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = statistics.mean(values)
    if mean == 0:
        return None
    return statistics.stdev(values) / mean


def _stage_result(events: list[dict], stage_id: str) -> dict | None:
    for e in events:
        if e["type"] == "stage_result" and e["payload"].get("stage_id") == stage_id:
            return e["payload"]
    return None


def _perception_mode(events: list[dict]) -> str | None:
    result = _stage_result(events, "a1_perception_probe")
    if result is None:
        return None
    choice = result.get("extra", {}).get("perception_choice")
    if choice == "vision":
        return "vision"
    if choice == "dom":
        return "dom"
    return "ambiguous"  # covers "visible" (non-discriminating) and "none" (no click)


def _visual_vs_dom(events: list[dict]) -> tuple[str | None, bool | None]:
    result = _stage_result(events, "a2_visual_vs_dom_order")
    if result is None:
        return None, None
    choice = result.get("extra", {}).get("choice")
    if choice is None:
        return None, None
    return choice, choice == "dom"


def _cadence_cv(events: list[dict]) -> float | None:
    timestamps = sorted(e["client_ts"] for e in events if e["type"] in CADENCE_EVENT_TYPES)
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    return _cv(gaps)


def _pointer_density_and_coalesced_ratio(events: list[dict], duration_s: float | None) -> tuple[float | None, float | None]:
    samples = [e for e in events if e["type"] == "pointer_sample"]
    if not samples:
        return None, None
    density = (len(samples) / duration_s) if duration_s else None
    coalesced = sum(1 for e in samples if e["payload"].get("coalesced"))
    return density, coalesced / len(samples)


def _click_offset_scatter(events: list[dict]) -> float | None:
    offsets = []
    for e in events:
        if e["type"] != "click_detail":
            continue
        ox, oy = e["payload"].get("offset_x"), e["payload"].get("offset_y")
        if ox is not None and oy is not None:
            offsets.append((ox**2 + oy**2) ** 0.5)
    if len(offsets) < 2:
        return None
    return statistics.stdev(offsets)


def _corrections_and_overshoot(events: list[dict]) -> tuple[float | None, float | None]:
    """For each click, look at pointer_samples in the preceding window on the same
    stage and count direction reversals (corrections) and whether the path passed
    further from the eventual click point than its final approach (overshoot).
    Heuristic, not a precise physiological measure — good enough to separate a
    dead-straight CDP-style approach from a human's wandering-then-correcting one.
    """
    samples = [e for e in events if e["type"] == "pointer_sample"]
    clicks = [e for e in events if e["type"] == "click_detail"]
    if not samples or not clicks:
        return None, None

    correction_counts = []
    overshoot_flags = []
    for click in clicks:
        stage_id = click["payload"].get("stage_id")
        cx, cy = click["payload"].get("x"), click["payload"].get("y")
        if cx is None or cy is None:
            continue
        window = [
            s for s in samples
            if s["payload"].get("stage_id") == stage_id
            and click["client_ts"] - CORRECTION_WINDOW_MS <= s["client_ts"] <= click["client_ts"]
        ]
        if len(window) < 3:
            continue

        points = [(s["payload"]["x"], s["payload"]["y"]) for s in window if "x" in s["payload"] and "y" in s["payload"]]
        if len(points) < 3:
            continue

        dxs = [b[0] - a[0] for a, b in zip(points, points[1:])]
        dys = [b[1] - a[1] for a, b in zip(points, points[1:])]
        use_x = sum(abs(d) for d in dxs) >= sum(abs(d) for d in dys)
        deltas = dxs if use_x else dys
        signs = [1 if d > 0 else (-1 if d < 0 else 0) for d in deltas if d != 0]
        corrections = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
        correction_counts.append(corrections)

        max_dist = max(((px - cx) ** 2 + (py - cy) ** 2) ** 0.5 for px, py in points)
        final_dist = ((points[-1][0] - cx) ** 2 + (points[-1][1] - cy) ** 2) ** 0.5
        overshoot_flags.append(max_dist > final_dist + 5)  # 5px slack

    if not correction_counts:
        return None, None
    return (
        sum(correction_counts) / len(correction_counts),
        sum(overshoot_flags) / len(overshoot_flags),
    )


def _error_rate_floor(events: list[dict]) -> float | None:
    results = [e["payload"] for e in events if e["type"] == "stage_result" and "correct" in e["payload"]]
    if not results:
        return None
    return sum(1 for r in results if not r["correct"]) / len(results)


def _ipi_cv_and_backspace_rate(events: list[dict]) -> tuple[float | None, float | None]:
    """Inter-keypress-interval CV and backspace rate from A5's key_detail stream.
    Character content is never captured (see a5_type_phrase.js) — only timing and
    whether a key was backspace, same posture as the storefront's keydown handler.
    """
    keys = [e for e in events if e["type"] == "key_detail" and e["payload"].get("stage_id") == "a5_type_phrase"]
    if not keys:
        return None, None
    timestamps = sorted(e["client_ts"] for e in keys)
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    backspaces = sum(1 for e in keys if e["payload"].get("is_backspace"))
    return _cv(gaps), backspaces / len(keys)


def _linear_regression_slope(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_mean, y_mean = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom == 0:
        return None
    numer = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    return numer / denom


def _latency_complexity_slope(events: list[dict]) -> float | None:
    """ms of latency per additional item in the field, from A4's escalating
    visual-search task. The shape (not the raw latency) is the signal — see
    a4_complexity_ramp.js's docstring. Timed-out levels are excluded: their
    "latency" is just the fixed timeout ceiling, not a real reaction.
    """
    result = _stage_result(events, "a4_complexity_ramp")
    if result is None:
        return None
    levels = result.get("extra", {}).get("levels", [])
    points = [(lv["distractor_count"], lv["latency_ms"]) for lv in levels if not lv.get("timed_out")]
    return _linear_regression_slope(points)


def _stale_frame_offset_ms(events: list[dict]) -> float | None:
    """How far behind "now" this player's click was, in milliseconds, from B1's
    layout-shift stage. offset_px is the miss distance between where the click
    landed and where the target actually was after it shifted (a clean hit would
    be ~0px); converted to ms using THIS player's own locally-measured cursor
    speed in the run-up to the click (from ambient pointer_sample), not a
    guessed constant — see b1_layout_shift.js's docstring.
    """
    result = _stage_result(events, "b1_layout_shift")
    if result is None:
        return None
    extra = result.get("extra", {})
    if not extra.get("shifted") or extra.get("click_x") is None:
        return None

    offset_px = ((extra["click_x"] - extra["post_shift_x"]) ** 2 + (extra["click_y"] - extra["post_shift_y"]) ** 2) ** 0.5

    click_ts = next((e["client_ts"] for e in events if e["type"] == "stage_result" and e["payload"].get("stage_id") == "b1_layout_shift"), None)
    if click_ts is None:
        return None
    window = [
        e for e in events
        if e["type"] == "pointer_sample" and e["payload"].get("stage_id") == "b1_layout_shift"
        and click_ts - CORRECTION_WINDOW_MS <= e["client_ts"] <= click_ts
    ]
    points = [(e["payload"]["x"], e["payload"]["y"], e["client_ts"]) for e in window if "x" in e["payload"]]
    if len(points) < 2:
        return None
    points.sort(key=lambda p: p[2])
    total_dist = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(points, points[1:]))
    total_time_ms = points[-1][2] - points[0][2]
    if total_time_ms <= 0 or total_dist == 0:
        return None
    speed_px_per_ms = total_dist / total_time_ms
    return offset_px / speed_px_per_ms


def _frame_jank_ratio(events: list[dict]) -> float | None:
    stats = [e["payload"] for e in events if e["type"] == "frame_stats"]
    if not stats:
        return None
    total_frames = sum(s.get("frame_count", 0) for s in stats)
    dropped = sum(s.get("dropped_frames", 0) for s in stats)
    if total_frames == 0:
        return None
    return dropped / total_frames


def extract(session_id: str, conn: sqlite3.Connection) -> dict:
    events = get_events(conn, session_id)

    complete = next((e["payload"] for e in events if e["type"] == "arcade_complete"), None)
    duration_s = (complete["total_duration_ms"] / 1000) if complete else None
    reduced_motion = complete.get("reduced_motion") if complete else None

    perception_mode = _perception_mode(events)
    visual_vs_dom_choice, dom_only_hit = _visual_vs_dom(events)
    pointer_density, coalesced_ratio = _pointer_density_and_coalesced_ratio(events, duration_s)
    correction_count, overshoot_rate = _corrections_and_overshoot(events)
    ipi_cv, backspace_rate = _ipi_cv_and_backspace_rate(events)

    is_trusted_values = [e["payload"].get("is_trusted") for e in events if e["type"] == "click_detail"]
    all_clicks_trusted = all(is_trusted_values) if is_trusted_values else None

    return {
        "ran_arcade": complete is not None,
        "reduced_motion": reduced_motion,
        "frame_jank_ratio": _frame_jank_ratio(events),
        # Perception / decision-structure probes (A1, A2)
        "perception_mode": perception_mode,
        "visual_vs_dom_order_choice": visual_vs_dom_choice,
        "dom_only_target_hit": dom_only_hit,
        # Timing shape, not raw duration — see module docstring for why this
        # replaces timing_analysis.reasoning_pause_fraction's hardcoded window.
        "cadence_cv": _cadence_cv(events),
        # Ambient pointer geometry
        "pointer_sample_density": pointer_density,
        "coalesced_event_ratio": coalesced_ratio,
        "click_offset_scatter": _click_offset_scatter(events),
        "correction_count": correction_count,
        "overshoot_rate": overshoot_rate,
        "error_rate_floor": _error_rate_floor(events),
        # Cheap, known-blind-spot signal — see module docstring. Not weighted as if
        # it catches CDP-driven agents; it only catches JS-injected clicks.
        "all_clicks_trusted": all_clicks_trusted,
        # Not computable without stages that don't exist yet (A4, A5, A6, B1) —
        # explicit None, not faked. Wire these up when those stages land.
        "ipi_cv": ipi_cv,
        "backspace_rate": backspace_rate,
        "latency_complexity_slope": _latency_complexity_slope(events),
        "stale_frame_offset_ms": _stale_frame_offset_ms(events),
        # Not computable without A6, which doesn't exist yet — explicit None, not
        # faked. Wire these up if/when it lands.
        "path_optimality": None,
        "backtrack_count": None,
        "dead_end_rate": None,
    }

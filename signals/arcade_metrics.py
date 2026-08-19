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
    """The LAST matching stage_result, not the first. Several stages
    (c1_flash_reaction, a1_perception_probe, a2_visual_vs_dom_order,
    a4_complexity_ramp) log an intermediate stage_result per wrong attempt
    now, via ctx.track(), before the harness's own onDone-triggered one —
    that harness-generated event is always the actual outcome and always
    comes last chronologically (get_events() returns events ordered by
    client_ts, and mount()'s promise only resolves, triggering the harness's
    log call, after every intermediate attempt already happened). Returning
    the first match would silently grab a wrong attempt instead."""
    result = None
    for e in events:
        if e["type"] == "stage_result" and e["payload"].get("stage_id") == stage_id:
            result = e["payload"]
    return result


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
    # "coalesced" (set client-side) now means getCoalescedEvents() returned
    # >1 entries — a genuine multi-sample merge, not the old always-true ">0"
    # check. See arcade.js's samplePointer()/pointerHandler().
    coalesced = sum(1 for e in samples if e["payload"].get("coalesced"))
    return density, coalesced / len(samples)


def _extra_samples_per_batch(events: list[dict]) -> float | None:
    """Mean real extra samples gained per raw pointer event, from genuine
    coalescing batches only. Dedups by batch_seq first — every sample in a
    batch carries the same batch_size, so summing (batch_size-1) per SAMPLE
    would overcount a single 3-sample batch as 6 extra instead of 2. Distinct
    from coalesced_event_ratio (fraction of samples that came from a >1 batch)
    — this instead answers "when coalescing happens, how much extra
    resolution does it actually add."
    """
    samples = [e for e in events if e["type"] == "pointer_sample"]
    batches: dict = {}
    for e in samples:
        seq = e["payload"].get("batch_seq")
        size = e["payload"].get("batch_size", 1)
        if seq is not None and seq not in batches:
            batches[seq] = size
    if not batches:
        return None
    return sum(max(0, size - 1) for size in batches.values()) / len(batches)


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


def _draw_shape_metrics(events: list[dict]) -> tuple[int | None, float | None, float | None, bool | None]:
    """From B2's trace-the-circle stage: point_count (samples during the drag
    — a real drag naturally produces dozens, a script faking it with a couple
    of teleporting mouse.move() calls produces very few), mean_deviation_px
    (how tightly the traced path hugged the target radius), angular_coverage_deg
    (did they actually sweep most of a loop, not just scribble), and whether
    they attempted it at all (distinguishes "didn't even try" from "tried and
    failed" — a bot that has no drawing logic times out with attempted=False).
    """
    result = _stage_result(events, "b2_draw_shape")
    if result is None:
        return None, None, None, None
    extra = result.get("extra", {})
    return (
        extra.get("point_count"),
        extra.get("mean_deviation_px"),
        extra.get("angular_coverage_deg"),
        extra.get("attempted"),
    )


MIN_SHIFT_DISPLACEMENT_PX = 30  # below this, pre/post are too close to tell which one a click was aimed at


def _stale_frame_offset_ms(events: list[dict]) -> float | None:
    """How far behind "now" this player's click was, in milliseconds, from B1's
    layout-shift stage. Keeps a timestamped position history for the target
    (spawn position + shift position) and finds which historical position the
    click geometrically matches best, then reports the real elapsed time since
    that position was current — read directly off recorded timestamps.

    Deliberately does NOT infer time from distance÷velocity anymore: that
    produced multi-second garbage whenever the divisor (cursor approach speed,
    measured from ambient pointer_sample) was merely small rather than exactly
    zero — e.g. a 20px spatial error over a ~10px/s approach silently inflated
    to ~2000ms, passing the old "not exactly zero" guard while still being
    nonsense. b1_layout_shift.js also used to stamp spawn/shift timestamps with
    bare performance.now(), not comparable to any other event's client_ts
    (which include performance.timeOrigin) — fixed there too; extra.spawn_ts/
    shift_ts/click_ts here are only trustworthy from sessions captured after
    that fix (older extras won't have them and return None, not garbage).

    Rounds where the shift displacement itself is under
    MIN_SHIFT_DISPLACEMENT_PX are rejected outright — if pre/post nearly
    coincide, no geometric match can meaningfully say which one a click was
    "aimed at" regardless of timestamps.
    """
    result = _stage_result(events, "b1_layout_shift")
    if result is None:
        return None
    extra = result.get("extra", {})
    if not extra.get("shifted") or extra.get("click_x") is None or extra.get("click_ts") is None:
        return None

    pre = (extra.get("pre_shift_x"), extra.get("pre_shift_y"), extra.get("spawn_ts"))
    post = (extra.get("post_shift_x"), extra.get("post_shift_y"), extra.get("shift_ts"))
    if pre[2] is None or post[2] is None:
        return None  # pre-fix session (bare performance.now(), not cross-referenceable) or malformed

    shift_displacement = ((post[0] - pre[0]) ** 2 + (post[1] - pre[1]) ** 2) ** 0.5
    if shift_displacement < MIN_SHIFT_DISPLACEMENT_PX:
        return None

    click_x, click_y, click_ts = extra["click_x"], extra["click_y"], extra["click_ts"]
    best_ts, best_dist = None, None
    for x, y, ts in (pre, post):
        d = ((click_x - x) ** 2 + (click_y - y) ** 2) ** 0.5
        if best_dist is None or d < best_dist:
            best_dist, best_ts = d, ts

    return click_ts - best_ts


def _telemetry_free_progress(events: list[dict]) -> bool | None:
    """True when the session produced at least one stage_result (proof some
    interaction genuinely advanced the game) but generated zero pointer_sample
    AND zero click_detail events across the entire run. A human, or any
    automation stack driving input through real OS/CDP-level events
    (Input.dispatchMouseEvent, an actual pointer/click), always leaves a
    pointer or click trail — it's structurally impossible not to, since those
    are ambient captures wired to the DOM's own event bubbling, not something
    a caller opts into per-action. Only an automation path that advances the
    game through something other than a dispatched pointer/click event (found
    via the agent_llm_cdp batch: Browser Use's "could not get element
    geometry from any method, falling back to JavaScript click" path)
    produces stage_results with zero trace in either ambient stream — 6/6
    sessions in that batch, 0/16 raw_cdp, 0/5 stealth_cdp. Returns None (not
    False) when there's no stage_result at all, since "no progress, no
    telemetry" isn't evidence of anything.
    """
    if not any(e["type"] == "stage_result" for e in events):
        return None
    has_pointer = any(e["type"] == "pointer_sample" for e in events)
    has_click = any(e["type"] == "click_detail" for e in events)
    return not has_pointer and not has_click


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

    capabilities = next((e["payload"] for e in events if e["type"] == "arcade_capabilities"), None)
    has_pointerrawupdate = capabilities.get("has_pointerrawupdate") if capabilities else None

    # True means a click's own per-element handler ran with el.isConnected
    # already false — the element it targeted had already been removed from
    # the DOM. A live human/CDP click can never land on something that isn't
    # currently on screen; only a stale cached reference can. See
    # arcade.js/c4_whack_a_mole.js's click handlers and CLAUDE.md's writeup —
    # this was found via the Browser Use session that clicked an already-gone
    # mole 10.8s after its round ended.
    stale_flags = [e["payload"].get("stale_element_interaction") for e in events if e["type"] == "click_detail"]
    stale_flags = [f for f in stale_flags if f is not None]
    stale_element_interaction_rate = (sum(1 for f in stale_flags if f) / len(stale_flags)) if stale_flags else None

    draw_point_count, draw_mean_deviation, draw_angular_coverage, draw_attempted = _draw_shape_metrics(events)

    return {
        "ran_arcade": complete is not None,
        "reduced_motion": reduced_motion,
        "has_pointerrawupdate": has_pointerrawupdate,
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
        "coalesced_extra_samples_per_batch": _extra_samples_per_batch(events),
        "click_offset_scatter": _click_offset_scatter(events),
        "correction_count": correction_count,
        "overshoot_rate": overshoot_rate,
        "error_rate_floor": _error_rate_floor(events),
        # Cheap, known-blind-spot signal — see module docstring. Not weighted as if
        # it catches CDP-driven agents; it only catches JS-injected clicks.
        "all_clicks_trusted": all_clicks_trusted,
        "stale_element_interaction_rate": stale_element_interaction_rate,
        "no_pointer_or_click_telemetry": _telemetry_free_progress(events),
        # B2 — trace-the-circle. draw_shape_attempted distinguishes "never
        # tried" (bot with no drawing logic, times out) from "tried and
        # produced a suspiciously sparse/inaccurate path."
        "draw_shape_point_count": draw_point_count,
        "draw_shape_mean_deviation_px": draw_mean_deviation,
        "draw_shape_angular_coverage_deg": draw_angular_coverage,
        "draw_shape_attempted": draw_attempted,
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

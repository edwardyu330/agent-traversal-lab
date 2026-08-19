import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

load_dotenv()  # picks up GOOGLE_SERVICE_ACCOUNT_FILE etc. from a project-root .env

from scoring.rule_based_scorer import coarse_verdict, score_session, update_weights_from_reveal
from scoring.weights_store import DEFAULT_WEIGHTS, load_weights
from test_site.google_sheets import append_reveal
from test_site.storage import get_conn, init_db, insert_events, leaderboard, reveal_session, upsert_session

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="agent-traversal-lab test_site")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "pages")

# Basic per-IP rate limiting — scoped in the original /arcade spec (item #8),
# never built. In-memory sliding window, no new dependency: this app runs as a
# single process, so a per-process dict is enough for "small private beta"
# scale. 60 req/10s is generous for normal play (a telemetry flush batching
# dozens of pointer samples is still just ONE POST) but blocks the kind of
# rapid burst the wild_scanner_suspected sessions produced (8 sessions in 18s,
# each presumably making several requests). Prefers cf-connecting-ip (the real
# client IP behind the Cloudflare tunnel) over request.client.host, which
# behind a tunnel/proxy is just the tunnel daemon's local connection.
RATE_LIMIT_WINDOW_S = 10
RATE_LIMIT_MAX_REQUESTS = 60
_request_log: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    ip = _client_ip(request)
    now = time.monotonic()
    log = _request_log[ip]
    while log and now - log[0] > RATE_LIMIT_WINDOW_S:
        log.popleft()
    if len(log) >= RATE_LIMIT_MAX_REQUESTS:
        return JSONResponse({"error": "rate limit exceeded, slow down"}, status_code=429)
    log.append(now)
    return await call_next(request)

VALID_LABELS = {"human", "agent_raw_cdp", "agent_llm_cdp", "agent_stealth_cdp", "agent_stealth_typing_cdp", "pending", "unknown"}

# Self-reported categories on the /arcade reveal screen — a separate, smaller
# vocabulary from VALID_LABELS (which also covers our own controlled
# generators). A reveal overwrites a "pending" session's label with one of these.
CLAIMED_TYPES = {"human", "bot_script", "agent"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/")
def index():
    # No storefront anymore — /arcade is the only player-facing surface.
    return RedirectResponse("/arcade")


@app.get("/arcade")
def arcade(request: Request):
    return templates.TemplateResponse(request, "arcade.html", {})


@app.get("/leaderboard")
def leaderboard_page(request: Request):
    return templates.TemplateResponse(request, "leaderboard.html", {})


LABEL_DISPLAY_NAMES = {
    "human": "Human",
    "agent_raw_cdp": "Raw CDP Bot",
    "agent_llm_cdp": "LLM Agent (Browser Use)",
    "agent_stealth_cdp": "Stealth Bot",
    "agent_stealth_typing_cdp": "Stealth Bot (human-typing adversarial test)",
    "wild_scanner_suspected": "Wild Scanner",
    "pending": "Pending (unrevealed)",
    "unknown": "Unknown",
    "bot_script": "Self-reported Bot",
    "agent": "Self-reported Agent",
}

# In the wild, only three things actually exist: Human, Bot (scripted/mechanical
# automation), and Agent (LLM-driven automation). Our OWN generator scripts know
# which is which because we wrote them — that's ground truth, not inference. A
# real, unlabeled visitor's score can only ever say automation-or-not (see
# coarse_verdict's Bot/Human split); Bot vs. Agent for THEM is only knowable if
# they self-report it (the reveal form's claimed_type). So /metrics groups
# everything into these three categories rather than raw labels — showing e.g.
# "agent_raw_cdp" and "agent_stealth_cdp" as separate rows implies a distinction
# real traffic could never give us. wild_scanner_suspected reads as Bot (see
# wild_scanner_writeup.md — repetitive, non-reasoning link-clicking, not
# evidence of LLM involvement). "pending" sessions never revealed, so we have no
# determination at all — they're excluded from these category views, not folded
# into any of the three, and called out separately so totals stay honest.
LABEL_TO_CATEGORY = {
    "human": "Human",
    "agent_raw_cdp": "Bot",
    "agent_stealth_cdp": "Bot",
    "agent_stealth_typing_cdp": "Bot",
    "wild_scanner_suspected": "Bot",
    "agent_llm_cdp": "Agent",
    # /api/reveal overwrites label with the player's own claimed_type on
    # self-report (CLAIMED_TYPES in this file) — "human" is already covered
    # above; these two are that same self-report vocabulary, not a generator
    # label, and belong in the same three buckets for the same reason.
    "bot_script": "Bot",
    "agent": "Agent",
}
CATEGORY_ORDER = ["Human", "Bot", "Agent"]

# Categories where a HIGH detection rate is the desired outcome (they're
# automation). Human is scored the opposite way — a high rate there is a
# false-positive problem, not a win. Drives the color coding on the /metrics
# "Detection Rates" section.
AUTOMATION_CATEGORIES = {"Bot", "Agent"}


@app.get("/metrics")
def metrics_page(request: Request):
    # Local import — analysis/ isn't part of the request-serving path anywhere
    # else, so this stays out of server.py's module-load-time import graph.
    from analysis.compare_agent_vs_human import (
        BOOL_SIGNALS,
        CATEGORICAL_SIGNALS,
        NUMERIC_SIGNALS,
        SIGNAL_LABELS,
        build_dataframe,
    )

    with get_conn() as conn:
        df = build_dataframe(conn)
        session_rows = [dict(r) for r in conn.execute(
            """
            SELECT session_id, label, trust, tool, player_score, player_name,
                   build_version, started_at, revealed_at
            FROM sessions ORDER BY started_at DESC
            """
        )]

    if df.empty:
        return templates.TemplateResponse(request, "metrics.html", {"empty": True})

    df["category"] = df["label"].map(LABEL_TO_CATEGORY)
    uncategorized_count = int(df["category"].isna().sum())
    categorized = df[df["category"].notna()]

    score_by_session = df.set_index("session_id")[["score", "raw_automation_score", "overall_detection_score", "band", "category"]].to_dict("index")
    for row in session_rows:
        row.update(score_by_session.get(row["session_id"], {}))
        if "overall_detection_score" in row:
            row["verdict"] = coarse_verdict(row["overall_detection_score"])["verdict"]
        # pandas leaves an uncategorized row's "category" as float NaN, not
        # None — and bool(float("nan")) is True, so the template's truthy
        # check would render the literal string "nan" instead of falling
        # through to "Unclassified". Normalize here, once.
        if isinstance(row.get("category"), float):
            row["category"] = None

    # Persisted accuracy record — every detection_accuracy event ever written
    # (see /api/reveal), independent of score_session()'s CURRENT weights. This
    # answers "how often were we actually right at the time," not "how would
    # today's weights score historical sessions" (that's what the Detection
    # Rates section above already answers, recomputed live).
    import json

    with get_conn() as acc_conn:
        accuracy_event_rows = acc_conn.execute(
            "SELECT payload_json FROM events WHERE type = 'detection_accuracy' ORDER BY client_ts ASC"
        ).fetchall()
    accuracy_rows = [json.loads(r[0]) for r in accuracy_event_rows]

    # Rolling accuracy trend — chronological order, window of the last 10
    # reveals at each point. This is the direct visual answer to "is the live
    # weight system actually improving as more tests run," which a single
    # snapshot accuracy number can't show on its own.
    TREND_WINDOW = 10

    def rolling_trend(rows):
        points = []
        for i in range(len(rows)):
            window = rows[max(0, i - TREND_WINDOW + 1):i + 1]
            correct_n = sum(1 for r in window if r.get("correct"))
            points.append(round(correct_n / len(window) * 100))
        return points

    accuracy_trend = rolling_trend(accuracy_rows)
    trend_by_category = {}
    for category in CATEGORY_ORDER:
        claimed = {"Human": "human", "Bot": "bot_script", "Agent": "agent"}[category]
        trend_by_category[category] = rolling_trend([r for r in accuracy_rows if r.get("claimed_type") == claimed])

    def svg_polyline(values, width=600, height=140):
        if len(values) < 2:
            return ""
        step = width / (len(values) - 1)
        return " ".join(f"{i * step:.1f},{height - (v / 100 * height):.1f}" for i, v in enumerate(values))

    trend_svg = {c: svg_polyline(pts) for c, pts in trend_by_category.items()}
    overall_trend_svg = svg_polyline(accuracy_trend)
    accuracy_by_category = {}
    for category in CATEGORY_ORDER:
        claimed = {"Human": "human", "Bot": "bot_script", "Agent": "agent"}[category]
        rows = [r for r in accuracy_rows if r.get("claimed_type") == claimed]
        n = len(rows)
        correct_n = sum(1 for r in rows if r.get("correct"))
        accuracy_by_category[category] = {
            "n": n,
            "correct": correct_n,
            "rate_pct": round(correct_n / n * 100) if n else None,
        }
    overall_accuracy = {
        "n": len(accuracy_rows),
        "correct": sum(1 for r in accuracy_rows if r.get("correct")),
    }
    overall_accuracy["rate_pct"] = (
        round(overall_accuracy["correct"] / overall_accuracy["n"] * 100) if overall_accuracy["n"] else None
    )

    category_counts = categorized["category"].value_counts().to_dict()
    score_stats = categorized.groupby("category")[["score", "raw_automation_score", "overall_detection_score"]].mean().round(1).to_dict("index")

    # Detection rate: fraction of a category's sessions where coarse_verdict (the
    # exact function /api/verdict calls) reads "Bot" off overall_detection_score.
    # For Bot/Agent this is the catch rate; for Human it's a false-positive rate
    # — same computation, opposite meaning, which is why AUTOMATION_CATEGORIES
    # drives the color coding in the template rather than a hardcoded
    # "higher is better" assumption baked in here.
    detection_rates = {}
    for category, group in categorized.groupby("category"):
        verdicts = [coarse_verdict(s)["verdict"] for s in group["overall_detection_score"]]
        n = len(verdicts)
        agent_n = sum(1 for v in verdicts if v == "Bot")
        rate = (agent_n / n) if n else None
        is_automation = category in AUTOMATION_CATEGORIES
        if rate is None:
            rating = "neutral"
        elif is_automation:
            rating = "good" if rate >= 0.7 else ("bad" if rate < 0.4 else "warn")
        else:
            rating = "good" if rate <= 0.15 else ("bad" if rate > 0.4 else "warn")
        detection_rates[category] = {
            "n": n,
            "agent_flagged": agent_n,
            "agent_rate": round(rate, 3) if rate is not None else None,
            "agent_rate_pct": round(rate * 100) if rate is not None else None,
            "is_automation": is_automation,
            "rating": rating,
        }
    band_dist = {
        category: group["band"].value_counts().to_dict()
        for category, group in categorized.groupby("category")
    }

    # Score-distribution histogram per category — 10 buckets of 10 points each
    # across overall_detection_score's 0-100 range. This is the actual shape
    # behind detection_rates' single percentage: a category could hit the same
    # catch rate either because every session clusters near 0 or 100 (confident
    # either way) or because everything sits near the 50 threshold (the scorer
    # is guessing) — the bar alone can't tell those apart, the histogram can.
    HIST_BUCKET_SIZE = 10
    HIST_BUCKET_COUNT = 100 // HIST_BUCKET_SIZE
    score_histograms = {}
    for category, group in categorized.groupby("category"):
        counts = [0] * HIST_BUCKET_COUNT
        for s in group["overall_detection_score"]:
            idx = min(HIST_BUCKET_COUNT - 1, int(s // HIST_BUCKET_SIZE))
            counts[idx] += 1
        score_histograms[category] = counts
    hist_max = max((max(counts) for counts in score_histograms.values() if counts), default=1) or 1

    # player_score lives on the sessions table, not in the signals dataframe — a
    # revealed-flow-only value (generator sessions never set it), so a plain
    # Python average over session_rows is simpler than threading it through df.
    player_scores_by_category: dict[str, list[int]] = {}
    for row in session_rows:
        cat = LABEL_TO_CATEGORY.get(row["label"])
        if cat is not None and row["player_score"] is not None:
            player_scores_by_category.setdefault(cat, []).append(row["player_score"])
    player_score_avg = {
        category: round(sum(vals) / len(vals), 1) for category, vals in player_scores_by_category.items()
    }
    all_player_scores = [s for vals in player_scores_by_category.values() for s in vals]

    overview = {
        "avg_overall_detection_score": round(categorized["overall_detection_score"].mean(), 1),
        "avg_player_score": round(sum(all_player_scores) / len(all_player_scores), 1) if all_player_scores else None,
        "revealed_count": sum(1 for r in session_rows if r["revealed_at"]),
        "verified_count": sum(1 for r in session_rows if r["trust"] == "verified"),
        "uncategorized_count": uncategorized_count,
    }

    # A category that never populated a given signal at all averages to
    # pandas' float NaN, not Python None — and Jinja's "is not none" doesn't
    # catch that (NaN is a float, not None), so it would render the literal
    # string "nan" instead of falling through to the template's "—". Scrub
    # every {group}.{key} -> value dict the same way before handing it to the
    # template, rather than special-casing each lookup site.
    def _nan_to_none(nested: dict) -> dict:
        return {outer: {k: (None if v != v else v) for k, v in inner.items()} for outer, inner in nested.items()}

    numeric_cols = [f"{g}.{k}" for g, k in NUMERIC_SIGNALS]
    numeric_means = _nan_to_none(categorized.groupby("category")[numeric_cols].mean(numeric_only=True).round(3).to_dict("index"))

    bool_cols = [f"{g}.{k}" for g, k in BOOL_SIGNALS]
    bool_rates = _nan_to_none(categorized.groupby("category")[bool_cols].mean(numeric_only=True).round(2).to_dict("index"))

    categorical_cols = [f"{g}.{k}" for g, k in CATEGORICAL_SIGNALS]
    categorical_dist = {
        col: categorized.groupby("category")[col].value_counts().unstack(fill_value=0).to_dict("index")
        for col in categorical_cols
    }

    # Live weights (scoring/weights_store.py) — drift from DEFAULT_WEIGHTS is
    # the visible trace of update_weights_from_reveal() actually having fired.
    current_weights = load_weights()
    weight_rows = [
        {"rule": name, "current": current_weights.get(name, default), "default": default,
         "delta": current_weights.get(name, default) - default}
        for name, default in sorted(DEFAULT_WEIGHTS.items())
    ]

    return templates.TemplateResponse(request, "metrics.html", {
        "empty": False,
        "total": len(df),
        "categories": [c for c in CATEGORY_ORDER if c in category_counts],
        "category_counts": category_counts,
        "label_display": LABEL_DISPLAY_NAMES,
        "overview": overview,
        "detection_rates": detection_rates,
        "band_dist": band_dist,
        "score_stats": score_stats,
        "player_score_avg": player_score_avg,
        "numeric_cols": numeric_cols,
        "numeric_means": numeric_means,
        "bool_cols": bool_cols,
        "bool_rates": bool_rates,
        "categorical_cols": categorical_cols,
        "categorical_dist": categorical_dist,
        "signal_labels": SIGNAL_LABELS,
        "weight_rows": weight_rows,
        "accuracy_by_category": accuracy_by_category,
        "overall_accuracy": overall_accuracy,
        "accuracy_trend": accuracy_trend,
        "trend_by_category": trend_by_category,
        "trend_svg": trend_svg,
        "overall_trend_svg": overall_trend_svg,
        "score_histograms": score_histograms,
        "hist_max": hist_max,
        "hist_bucket_size": HIST_BUCKET_SIZE,
        "sessions": session_rows,
    })


class SessionStart(BaseModel):
    session_id: str
    label: str
    user_agent: str
    first_page: str
    build_version: str | None = None


# Headers never logged at full value even though this is a research harness,
# not a production capture path — a cookie/authorization value is a live
# credential, not a fingerprinting signal, and there's no reason to persist
# one just because it happened to be present on the request.
HEADER_DENYLIST = ("cookie", "authorization", "proxy-authorization", "x-api-key")

# Superset of what the header-consistency checker (analysis/header_consistency_
# checker.py) and any future network-layer work need: the old fixed subset
# missed sec-ch-ua-mobile (needed to catch a mobile-claiming UA sending
# sec-ch-ua-mobile=?0) and every IP-carrying header (cf-connecting-ip,
# x-forwarded-for — the exact gap wild_scanner_writeup.md flagged: we had
# proof the scanner went through Cloudflare's edge but never captured the IP
# those headers actually carried). Now captures every header present minus
# HEADER_DENYLIST, not a fixed allowlist, so a header nobody thought to name
# ahead of time still gets captured instead of silently dropped.
def _capture_headers(request: Request) -> dict:
    header_order = list(request.headers.keys())
    header_values = {k: v for k, v in request.headers.items() if k.lower() not in HEADER_DENYLIST}
    return {"header_order": header_order, "client_ip": _client_ip(request), **header_values}


@app.post("/api/session/start")
def session_start(body: SessionStart, request: Request):
    label = body.label if body.label in VALID_LABELS else "unknown"
    received = now_iso()

    # A non-"pending" label here came from one of our own generator scripts setting
    # ?label= directly (run_playwright_raw.py, run_browser_use.py, run_playwright_stealth.py)
    # — we control those, so trust it immediately. "pending" means this is a real
    # player on /arcade; their real label (and any trust) isn't known until reveal time.
    trust = "verified" if label != "pending" else None

    # Practical, server-observable substitute for TLS/JA4 fingerprinting (which needs
    # raw ClientHello bytes we don't have access to here — see signals/network_fingerprint.py).
    captured = _capture_headers(request)

    with get_conn() as conn:
        upsert_session(conn, body.session_id, label, trust, body.user_agent, body.first_page, received,
                        body.build_version, captured["client_ip"])
        insert_events(
            conn,
            [(
                body.session_id,
                "http_headers",
                body.first_page,
                time.time() * 1000,  # epoch ms, matching collector.js's client_ts scale
                _dumps(captured),
                received,
            )],
        )
    return JSONResponse({"ok": True})


class TelemetryEvent(BaseModel):
    type: str
    page: str
    client_ts: float
    payload: dict


class TelemetryBatch(BaseModel):
    session_id: str
    events: list[TelemetryEvent]


@app.post("/api/telemetry")
def telemetry(batch: TelemetryBatch):
    received = now_iso()
    rows = [
        (batch.session_id, e.type, e.page, e.client_ts, _dumps(e.payload), received)
        for e in batch.events
    ]
    if rows:
        with get_conn() as conn:
            insert_events(conn, rows)
    return JSONResponse({"ok": True, "count": len(rows)})


@app.get("/api/sessions")
def list_sessions():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.label, s.trust, s.tool, s.user_agent, s.first_page,
                   s.build_version, s.started_at, s.revealed_at, COUNT(e.id) AS event_count
            FROM sessions s
            LEFT JOIN events e ON e.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.started_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/verdict/{session_id}")
def get_verdict(session_id: str):
    with get_conn() as conn:
        session = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise HTTPException(404, "unknown session_id")
        result = score_session(session_id, conn)
    # Deliberately only the coarse verdict — never score/band/breakdown/signals.
    # See coarse_verdict()'s docstring for why. overall_detection_score (not the
    # bare arcade-only score) so a raw-automation tell alone is still enough to
    # call it — see rule_based_scorer.score_session()'s comment on that field.
    return JSONResponse(coarse_verdict(result["overall_detection_score"]))


class RevealBody(BaseModel):
    session_id: str
    claimed_type: str
    tool: str | None = None
    player_score: int | None = None
    name: str | None = None
    email: str | None = None


@app.post("/api/reveal")
def reveal(body: RevealBody, request: Request):
    if body.claimed_type not in CLAIMED_TYPES:
        raise HTTPException(400, f"claimed_type must be one of {sorted(CLAIMED_TYPES)}")

    now = now_iso()
    with get_conn() as conn:
        # Covers pure-HTTP self-reports too: a script that never executed arcade.js
        # has no prior session row (no JS ran, so no session_id was ever registered
        # server-side) — it can make up any session_id and POST straight here. The
        # upsert is a no-op (ON CONFLICT DO NOTHING) for sessions that already exist
        # from the normal /arcade flow, so it never clobbers real telemetry-linked rows.
        captured = _capture_headers(request)
        upsert_session(conn, body.session_id, "pending", None,
                        request.headers.get("user-agent"), None, now,
                        client_ip=captured["client_ip"])
        # A session with no prior http_headers event is exactly the pure-HTTP,
        # no-JS case CLAUDE.md describes — this is the only place its headers
        # will ever get captured, since it never ran arcade.js to hit
        # /api/session/start. Guarded so a normal /arcade session (which
        # already logged its real page-load headers there) doesn't get a
        # second, less meaningful set from the reveal POST itself overwriting
        # the picture.
        existing = conn.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND type = 'http_headers' LIMIT 1",
            (body.session_id,),
        ).fetchone()
        if not existing:
            insert_events(
                conn,
                [(
                    body.session_id,
                    "http_headers",
                    "/api/reveal",
                    time.time() * 1000,
                    _dumps(captured),
                    now,
                )],
            )
        # Honor-system product: the claim is trusted outright, no consistency check
        # against telemetry — every reveal lands as "verified".
        reveal_session(conn, body.session_id, body.claimed_type, body.tool, "verified", now,
                        body.player_score, body.name, body.email)
        # Live weight update — see update_weights_from_reveal()'s docstring for
        # what "live" means here and the honor-system exposure that comes with
        # wiring it straight to this unverified claim. Also the actual accuracy
        # record: persisted as an event for EVERY reveal (human included, not
        # just bots/agents, and correct verdicts included, not just misses) —
        # a weight nudge alone left no queryable trail of how often we're
        # actually right.
        outcome = update_weights_from_reveal(body.session_id, body.claimed_type, conn)
        if outcome is not None:
            insert_events(
                conn,
                [(
                    body.session_id,
                    "detection_accuracy",
                    "/arcade",
                    time.time() * 1000,
                    _dumps(outcome),
                    now,
                )],
            )

    # Best-effort, never blocks/breaks the reveal — see google_sheets.py's docstring.
    append_reveal(body.session_id, body.claimed_type, body.tool, body.name, body.email, body.player_score)

    return JSONResponse({"ok": True})


@app.get("/api/leaderboard")
def get_leaderboard(label: str, limit: int = 10):
    """Name + score only, `trust='verified'` only. Deliberately a separate query
    from /api/sessions (which is a debug endpoint returning full session rows,
    including tool/user_agent) — email is never selected here, and never should be:
    this endpoint's response is meant to be safe to render directly on a public
    leaderboard.
    """
    if label not in CLAIMED_TYPES:
        raise HTTPException(400, f"label must be one of {sorted(CLAIMED_TYPES)}")
    with get_conn() as conn:
        rows = leaderboard(conn, label, min(limit, 50))
    return JSONResponse(rows)


def _dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"))


def main() -> None:
    import uvicorn

    uvicorn.run("test_site.server:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()

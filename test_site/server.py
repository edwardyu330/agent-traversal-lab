import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

load_dotenv()  # picks up GOOGLE_SERVICE_ACCOUNT_FILE etc. from a project-root .env

from scoring.rule_based_scorer import coarse_verdict, score_session
from test_site.google_sheets import append_reveal
from test_site.storage import get_conn, init_db, insert_events, leaderboard, reveal_session, upsert_session

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="agent-traversal-lab test_site")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "pages")

VALID_LABELS = {"human", "agent_raw_cdp", "agent_llm_cdp", "pending", "unknown"}

# Self-reported categories on the /play reveal screen — a separate, smaller vocabulary
# from VALID_LABELS (which also covers our own controlled generators). A reveal
# overwrites a "pending" session's label with one of these.
CLAIMED_TYPES = {"human", "bot_script", "agent"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/login")
def login(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/checkout")
def checkout(request: Request):
    return templates.TemplateResponse(request, "checkout.html", {})


@app.get("/play")
def play(request: Request):
    return templates.TemplateResponse(request, "play.html", {})


@app.get("/arcade")
def arcade(request: Request):
    return templates.TemplateResponse(request, "arcade.html", {})


@app.get("/leaderboard")
def leaderboard_page(request: Request):
    return templates.TemplateResponse(request, "leaderboard.html", {})


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
                   started_at, revealed_at
            FROM sessions ORDER BY started_at DESC
            """
        )]

    if df.empty:
        return templates.TemplateResponse(request, "metrics.html", {"empty": True})

    score_by_session = df.set_index("session_id")[["score", "raw_automation_score", "overall_detection_score", "band"]].to_dict("index")
    for row in session_rows:
        row.update(score_by_session.get(row["session_id"], {}))

    label_counts = df["label"].value_counts().to_dict()
    score_stats = df.groupby("label")[["score", "raw_automation_score", "overall_detection_score"]].mean().round(1).to_dict("index")

    # player_score lives on the sessions table, not in the signals dataframe — a
    # revealed-flow-only value (generator sessions never set it), so a plain
    # Python average over session_rows is simpler than threading it through df.
    player_scores_by_label: dict[str, list[int]] = {}
    for row in session_rows:
        if row["player_score"] is not None:
            player_scores_by_label.setdefault(row["label"], []).append(row["player_score"])
    player_score_avg = {
        label: round(sum(vals) / len(vals), 1) for label, vals in player_scores_by_label.items()
    }
    all_player_scores = [s for vals in player_scores_by_label.values() for s in vals]

    overview = {
        "avg_overall_detection_score": round(df["overall_detection_score"].mean(), 1),
        "avg_player_score": round(sum(all_player_scores) / len(all_player_scores), 1) if all_player_scores else None,
        "revealed_count": sum(1 for r in session_rows if r["revealed_at"]),
        "verified_count": sum(1 for r in session_rows if r["trust"] == "verified"),
    }

    numeric_cols = [f"{g}.{k}" for g, k in NUMERIC_SIGNALS]
    numeric_means = df.groupby("label")[numeric_cols].mean(numeric_only=True).round(3).to_dict("index")

    bool_cols = [f"{g}.{k}" for g, k in BOOL_SIGNALS]
    bool_rates = df.groupby("label")[bool_cols].mean(numeric_only=True).round(2).to_dict("index")

    categorical_cols = [f"{g}.{k}" for g, k in CATEGORICAL_SIGNALS]
    categorical_dist = {
        col: df.groupby("label")[col].value_counts().unstack(fill_value=0).to_dict("index")
        for col in categorical_cols
    }

    return templates.TemplateResponse(request, "metrics.html", {
        "empty": False,
        "total": len(df),
        "labels": sorted(label_counts.keys()),
        "label_counts": label_counts,
        "overview": overview,
        "score_stats": score_stats,
        "player_score_avg": player_score_avg,
        "numeric_cols": numeric_cols,
        "numeric_means": numeric_means,
        "bool_cols": bool_cols,
        "bool_rates": bool_rates,
        "categorical_cols": categorical_cols,
        "categorical_dist": categorical_dist,
        "signal_labels": SIGNAL_LABELS,
        "sessions": session_rows,
    })


class SessionStart(BaseModel):
    session_id: str
    label: str
    user_agent: str
    first_page: str


HEADER_FINGERPRINT_KEYS = (
    "accept",
    "accept-language",
    "accept-encoding",
    "sec-ch-ua",
    "sec-ch-ua-platform",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
)


@app.post("/api/session/start")
def session_start(body: SessionStart, request: Request):
    label = body.label if body.label in VALID_LABELS else "unknown"
    received = now_iso()

    # A non-"pending" label here came from one of our own generator scripts setting
    # ?label= directly (run_playwright_raw.py, run_browser_use.py, run_human_baseline.py)
    # — we control those, so trust it immediately. "pending" means this is a /play
    # session; its real label (and any trust) isn't known until reveal time.
    trust = "verified" if label != "pending" else None

    # Practical, server-observable substitute for TLS/JA4 fingerprinting (which needs
    # raw ClientHello bytes we don't have access to here — see signals/network_fingerprint.py).
    header_order = list(request.headers.keys())
    header_values = {k: request.headers.get(k) for k in HEADER_FINGERPRINT_KEYS if k in request.headers}

    with get_conn() as conn:
        upsert_session(conn, body.session_id, label, trust, body.user_agent, body.first_page, received)
        insert_events(
            conn,
            [(
                body.session_id,
                "http_headers",
                body.first_page,
                time.time() * 1000,  # epoch ms, matching collector.js's client_ts scale
                _dumps({"header_order": header_order, **header_values}),
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
                   s.started_at, s.revealed_at, COUNT(e.id) AS event_count
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
        # Covers pure-HTTP self-reports too: a script that never executed collector.js
        # has no prior session row (no JS ran, so no session_id was ever registered
        # server-side) — it can make up any session_id and POST straight here. The
        # upsert is a no-op (ON CONFLICT DO NOTHING) for sessions that already exist
        # from the normal /play flow, so it never clobbers real telemetry-linked rows.
        upsert_session(conn, body.session_id, "pending", None,
                        request.headers.get("user-agent"), None, now)
        # Honor-system product: the claim is trusted outright, no consistency check
        # against telemetry — every reveal lands as "verified".
        reveal_session(conn, body.session_id, body.claimed_type, body.tool, "verified", now,
                        body.player_score, body.name, body.email)

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

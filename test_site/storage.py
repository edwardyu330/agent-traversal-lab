import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "traversal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    trust           TEXT,
    tool            TEXT,
    user_agent      TEXT,
    first_page      TEXT,
    started_at      TEXT NOT NULL,
    revealed_at     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL,
    type                TEXT NOT NULL,
    page                TEXT,
    client_ts           REAL NOT NULL,
    payload_json        TEXT NOT NULL,
    server_received_at  TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (session_id)
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id);
"""


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive-only migration for DBs created before the play/reveal feature —
    preserves existing sessions instead of requiring a wipe."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    for col in ("trust", "tool", "revealed_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
    if "player_score" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN player_score INTEGER")
    for col in ("player_name", "player_email"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
    if "build_version" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN build_version TEXT")

    # Idempotent backfill: any session with no trust value that isn't sitting in
    # "pending" (unrevealed) state was created by a controlled generator script
    # before trust-at-creation existed — bring it in line with new sessions.
    conn.execute("UPDATE sessions SET trust = 'verified' WHERE trust IS NULL AND label != 'pending'")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def upsert_session(conn: sqlite3.Connection, session_id: str, label: str, trust: str | None,
                    user_agent: str, first_page: str, started_at: str,
                    build_version: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO sessions (session_id, label, trust, user_agent, first_page, started_at, build_version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (session_id) DO NOTHING
        """,
        (session_id, label, trust, user_agent, first_page, started_at, build_version),
    )


def reveal_session(conn: sqlite3.Connection, session_id: str, label: str, tool: str | None,
                    trust: str, revealed_at: str, player_score: int | None = None,
                    player_name: str | None = None, player_email: str | None = None) -> None:
    conn.execute(
        "UPDATE sessions SET label = ?, tool = ?, trust = ?, revealed_at = ?, "
        "player_score = COALESCE(?, player_score), "
        "player_name = COALESCE(?, player_name), "
        "player_email = COALESCE(?, player_email) "
        "WHERE session_id = ?",
        (label, tool, trust, revealed_at, player_score, player_name, player_email, session_id),
    )


def leaderboard(conn: sqlite3.Connection, label: str, limit: int = 10) -> list[dict]:
    """Name + score only — never email, never signal data. See server.py's
    /api/leaderboard docstring for why this is a separate query, not a reuse of
    /api/sessions."""
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(player_name), ''), 'Anonymous') AS name, player_score, revealed_at
        FROM sessions
        WHERE label = ? AND player_score IS NOT NULL AND trust = 'verified'
        ORDER BY player_score DESC, revealed_at ASC
        LIMIT ?
        """,
        (label, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_events(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO events (session_id, type, page, client_ts, payload_json, server_received_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

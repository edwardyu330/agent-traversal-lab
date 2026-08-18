import json
import sqlite3

from test_site.storage import get_conn  # noqa: F401  (re-exported for signal modules)


def get_session(conn: sqlite3.Connection, session_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else {}


def get_events(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT type, page, client_ts, payload_json FROM events "
        "WHERE session_id = ? ORDER BY client_ts",
        (session_id,),
    ).fetchall()
    events = []
    for r in rows:
        events.append(
            {
                "type": r["type"],
                "page": r["page"],
                "client_ts": r["client_ts"],
                "payload": json.loads(r["payload_json"]),
            }
        )
    return events


def list_session_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT session_id FROM sessions").fetchall()
    return [r["session_id"] for r in rows]

"""Mouse path signals. Playwright's .click() (and CDP input dispatch generally)
moves the cursor straight to the target in one hop, producing at most a single
mousemove per click with a dead-straight, teleport-like path. Real human mouse
movement is made of many small moves forming a curved path with overshoot/
correction. This module measures both the shape of the path and how many
mousemove samples precede each click.
"""

import math
import sqlite3

from signals.common import get_events


def _dist(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _path_curvature(points: list[dict]) -> float | None:
    if len(points) < 2:
        return None
    straight = _dist(points[0], points[-1])
    traveled = sum(_dist(a, b) for a, b in zip(points, points[1:]))
    if straight == 0:
        return None
    return traveled / straight


def extract(session_id: str, conn: sqlite3.Connection) -> dict:
    events = get_events(conn, session_id)

    pages = {}
    for e in events:
        if e["type"] in ("mousemove", "click"):
            pages.setdefault(e["page"], []).append(e)

    curvatures = []
    mousemoves_before_click = []

    for page_events in pages.values():
        pending_moves: list[dict] = []
        for e in page_events:
            if e["type"] == "mousemove":
                pending_moves.append(e["payload"])
            elif e["type"] == "click":
                mousemoves_before_click.append(len(pending_moves))
                curvature = _path_curvature(pending_moves + [e["payload"]])
                if curvature is not None:
                    curvatures.append(curvature)
                pending_moves = []

    total_mousemoves = sum(1 for e in events if e["type"] == "mousemove")
    total_clicks = sum(1 for e in events if e["type"] == "click")
    teleport_clicks = sum(1 for n in mousemoves_before_click if n <= 1)

    return {
        "mousemove_count": total_mousemoves,
        "click_count": total_clicks,
        "mean_mousemoves_before_click": (
            sum(mousemoves_before_click) / len(mousemoves_before_click)
            if mousemoves_before_click
            else None
        ),
        "teleport_click_fraction": (
            teleport_clicks / len(mousemoves_before_click) if mousemoves_before_click else None
        ),
        "mean_path_curvature": sum(curvatures) / len(curvatures) if curvatures else None,
    }

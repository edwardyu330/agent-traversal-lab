"""WebDriver/CDP artifact signals: navigator.webdriver, software-rendered WebGL
(a strong tell for headless/automated Chrome, which falls back to a software
rasterizer instead of the machine's real GPU), and headless UA strings.
"""

import sqlite3

from signals.common import get_events, get_session

SUSPICIOUS_WEBGL_SUBSTRINGS = ("swiftshader", "llvmpipe", "software rasterizer", "google, google")
HEADLESS_UA_SUBSTRINGS = ("headlesschrome",)


def extract(session_id: str, conn: sqlite3.Connection) -> dict:
    events = get_events(conn, session_id)
    load_signals = [e["payload"] for e in events if e["type"] == "page_load_signals"]

    webdriver_flag = any(p.get("webdriver") for p in load_signals)

    renderer_strings = [
        (p.get("webgl") or {}).get("renderer", "") or "" for p in load_signals
    ]
    suspicious_webgl = any(
        any(s in r.lower() for s in SUSPICIOUS_WEBGL_SUBSTRINGS) for r in renderer_strings
    )

    user_agent = (get_session(conn, session_id).get("user_agent") or "").lower()
    headless_ua = any(s in user_agent for s in HEADLESS_UA_SUBSTRINGS)

    return {
        "webdriver_flag": webdriver_flag,
        "suspicious_webgl_renderer": suspicious_webgl,
        "webgl_renderer_strings": sorted(set(r for r in renderer_strings if r)),
        "headless_ua": headless_ua,
        "page_load_count": len(load_signals),
    }

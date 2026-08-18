"""Network-layer signals.

KNOWN LIMITATION: real network fingerprinting (JA4/JA3-style TLS ClientHello
fingerprinting) needs raw bytes off the socket before TLS termination — a plain
ASGI app never sees them, and Playwright's own network stack won't necessarily
match a real browser's TLS handshake byte-for-byte even if we could capture it.
That's a separate, much larger effort (the kind GreyNoise/Spur build full-time) and
is explicitly out of scope for this build. TODO: revisit with a TLS-terminating
proxy in front of test_site if this becomes worth pursuing.

What *is* practical from a plain server: HTTP header presence, order, and casing,
captured server-side in test_site/server.py's /api/session/start handler. Headless/
automation HTTP clients frequently omit or reorder headers a real browser always
sends (e.g. missing Sec-CH-UA, Sec-Fetch-*, or a different Accept-Language shape).
"""

import sqlite3

from signals.common import get_events

EXPECTED_BROWSER_HEADERS = ("sec-ch-ua", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest")


def extract(session_id: str, conn: sqlite3.Connection) -> dict:
    events = get_events(conn, session_id)
    header_events = [e["payload"] for e in events if e["type"] == "http_headers"]

    if not header_events:
        return {
            "header_order": None,
            "missing_expected_browser_headers": None,
            "accept_language": None,
        }

    headers = header_events[0]
    missing = [h for h in EXPECTED_BROWSER_HEADERS if h not in headers]

    return {
        "header_order": headers.get("header_order"),
        "missing_expected_browser_headers": missing,
        "accept_language": headers.get("accept-language"),
        "ja4_available": False,  # see module docstring
    }

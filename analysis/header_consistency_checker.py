"""Standalone header self-consistency checker — Part 4 of the separation-
analysis follow-up. Deliberately its OWN module, not wired into
rule_based_scorer.py: this checks a different thing than arcade_metrics.py
(cross-request identity consistency, not in-session behavior) and needs no
JavaScript at all, so it's the one piece of this project that sees HTTP-only
traffic the entire behavioral stack is blind to. This is the bridge to the
network/proxy layer, not part of the arcade score.

Origin: wild_scanner_writeup.md's finding — 9 real, unsolicited sessions all
sent the identical `sec-ch-ua: "Chromium";v="131"` Client Hint while 3 of them
claimed to be an iPhone. That's not a statistical tendency, it's a categorical
impossibility: Client Hints (sec-ch-ua/-platform/-mobile) are a Chromium-only
feature. No iOS browser can send them at all — Apple's App Store rules force
every iOS browser (Safari, Chrome-on-iOS, Firefox-on-iOS) to be a WebKit
wrapper under the hood, and WebKit doesn't implement UA-CH. Real Safari and
Firefox (any platform) don't send them either. So: UA claims a Client-Hints-
incapable browser/platform, but the request still carries sec-ch-ua -> the
request is lying about at least one of the two, full stop, not "unusual."

This module intentionally reports a boolean (impossible / not) per session
plus the specific reasons, not a continuous score — the ask was to flag
categorical impossibilities, not tendencies dressed up as one more weighted
signal.

Run: python -m analysis.header_consistency_checker
"""

import sqlite3
from collections import defaultdict

from signals.common import get_events, get_session, list_session_ids
from test_site.storage import get_conn

# Client-Hints-incapable browser markers. FxiOS/CriOS/EdgiOS are Firefox/
# Chrome/Edge running as WebKit wrappers on iOS — still Client-Hints-incapable
# despite the Chrome/Firefox-looking UA substring, which is exactly why UA
# alone can't answer this and header cross-referencing has to.
IOS_MARKERS = ("iPhone", "iPad", "iPod", "CriOS", "FxiOS", "EdgiOS")
CHROMIUM_UA_MARKERS = ("Chrome/", "Chromium/", "Edg/", "OPR/")

CH_PLATFORM_FROM_UA = [
    # (check function description handled inline below) — order matters: iOS
    # check must run before the plain "Macintosh" substring check, since an
    # iPhone/iPad UA can still contain neither Chrome/Edg/OPR markers.
]


def _claims_ios(ua: str) -> bool:
    return any(m in ua for m in IOS_MARKERS)


def _claims_android(ua: str) -> bool:
    return "Android" in ua


def _claims_windows(ua: str) -> bool:
    return "Windows" in ua


def _claims_mac(ua: str) -> bool:
    return "Macintosh" in ua and not _claims_ios(ua)


def _claims_linux(ua: str) -> bool:
    return "Linux" in ua and not _claims_android(ua) and not _claims_ios(ua)


def _ua_platform_guess(ua: str) -> str | None:
    if _claims_ios(ua):
        return "iOS"
    if _claims_android(ua):
        return "Android"
    if _claims_windows(ua):
        return "Windows"
    if _claims_mac(ua):
        return "macOS"
    if _claims_linux(ua):
        return "Linux"
    return None


def _claims_client_hints_incapable_browser(ua: str) -> str | None:
    """Returns a reason string if this UA claims to be a browser/platform that
    cannot send Client Hints at all, else None. iOS is checked first and
    covers every iOS browser regardless of which one it claims to be — see
    IOS_MARKERS."""
    if _claims_ios(ua):
        return "UA claims an iOS device — every iOS browser is a WebKit wrapper (App Store rules) and none implement Client Hints"
    if "Firefox" in ua:
        return "UA claims Firefox — Firefox does not implement Client Hints"
    if "Safari" in ua and not any(m in ua for m in CHROMIUM_UA_MARKERS):
        return "UA claims Safari (non-Chromium) — Safari does not implement Client Hints"
    return None


def check_headers(headers: dict, user_agent: str | None) -> dict:
    """Pure function: headers dict (as captured by server.py's
    _capture_headers, or the old HEADER_FINGERPRINT_KEYS subset for
    pre-existing sessions) + the reported User-Agent -> consistency verdict.
    `impossible=True` means at least one categorical contradiction was found,
    not just an unusual combination."""
    ua = user_agent or headers.get("user-agent") or ""
    sec_ch_ua = headers.get("sec-ch-ua")
    sec_ch_ua_platform = headers.get("sec-ch-ua-platform")
    sec_ch_ua_mobile = headers.get("sec-ch-ua-mobile")

    if not ua and sec_ch_ua is None and sec_ch_ua_platform is None:
        return {"has_data": False, "impossible": False, "reasons": []}

    reasons = []

    incapable_reason = _claims_client_hints_incapable_browser(ua) if ua else None
    if incapable_reason and sec_ch_ua is not None:
        reasons.append(f"{incapable_reason}, but this request sent sec-ch-ua={sec_ch_ua!r}")

    ua_platform = _ua_platform_guess(ua) if ua else None
    if ua_platform and sec_ch_ua_platform:
        ch_platform_clean = sec_ch_ua_platform.strip('"')
        if ch_platform_clean.lower() != ua_platform.lower():
            reasons.append(
                f"UA claims platform {ua_platform!r} but sec-ch-ua-platform says {ch_platform_clean!r}"
            )

    if ua_platform in ("iOS", "Android") and sec_ch_ua_mobile == "?0":
        reasons.append(f"UA claims a mobile platform ({ua_platform}) but sec-ch-ua-mobile=?0 (desktop)")
    elif ua_platform not in ("iOS", "Android", None) and sec_ch_ua_mobile == "?1":
        reasons.append(f"UA claims a desktop platform ({ua_platform}) but sec-ch-ua-mobile=?1 (mobile)")

    return {
        "has_data": True,
        "impossible": len(reasons) > 0,
        "reasons": reasons,
        "ua_platform_guess": ua_platform,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_platform": sec_ch_ua_platform,
    }


def _session_headers(session_id: str, conn: sqlite3.Connection) -> dict | None:
    events = get_events(conn, session_id)
    header_events = [e["payload"] for e in events if e["type"] == "http_headers"]
    return header_events[0] if header_events else None


def check_session(session_id: str, conn: sqlite3.Connection) -> dict:
    headers = _session_headers(session_id, conn)
    session = get_session(conn, session_id)
    if headers is None:
        return {"has_data": False, "impossible": False, "reasons": []}
    return check_headers(headers, session.get("user_agent"))


def find_identity_clusters(conn: sqlite3.Connection) -> list[dict]:
    """Generalizes the wild-scanner finding into a reusable check. Clusters on
    (sec-ch-ua, header_order, accept-language) — NOT sec-ch-ua-platform,
    deliberately: the wild scanner varied sec-ch-ua-platform per session to
    "match" whatever device it was pretending to be that session (iOS for the
    iPhone UAs, macOS for the Mac UAs), which is exactly why a naive
    "everything about this fingerprint must match" check missed it in an
    earlier version of this function. header_order (the sequence of ~15-20
    header names, not values) is much higher-entropy than sec-ch-ua's brand
    string alone — two real distinct Chrome installs commonly share the same
    major-version sec-ch-ua by coincidence, but sharing both that AND the
    exact same header ordering AND then claiming to be different DEVICES
    (compared via UA string, not the self-reported CH platform) is what
    actually indicates one script presenting multiple fake identities."""
    clusters: dict[tuple, list[dict]] = defaultdict(list)
    for sid in list_session_ids(conn):
        headers = _session_headers(sid, conn)
        if not headers or not headers.get("sec-ch-ua"):
            continue
        session = get_session(conn, sid)
        header_order = headers.get("header_order")
        fp = (headers.get("sec-ch-ua"), tuple(header_order) if header_order else None, headers.get("accept-language"))
        ua = session.get("user_agent") or headers.get("user-agent") or ""
        clusters[fp].append({"session_id": sid, "label": session.get("label"), "ua_platform_guess": _ua_platform_guess(ua)})

    findings = []
    for fp, members in clusters.items():
        platforms = {m["ua_platform_guess"] for m in members if m["ua_platform_guess"]}
        if len(members) > 1 and len(platforms) > 1:
            findings.append({"fingerprint": fp, "platforms_claimed": sorted(platforms), "members": members})
    return findings


def main():
    with get_conn() as conn:
        session_ids = list_session_ids(conn)
        by_label = defaultdict(list)
        for sid in session_ids:
            label = get_session(conn, sid).get("label")
            result = check_session(sid, conn)
            by_label[label].append(result)

        print("=== PER-LABEL RESULTS (categorical impossibility rate) ===\n")
        for label in sorted(by_label):
            results = by_label[label]
            with_data = [r for r in results if r["has_data"]]
            impossible = [r for r in with_data if r["impossible"]]
            print(f"{label}: n={len(results)}  with_header_data={len(with_data)}  "
                  f"flagged_impossible={len(impossible)}"
                  + (f"  ({len(impossible)/len(with_data):.0%})" if with_data else ""))
            for r in impossible[:5]:
                print(f"    reasons: {r['reasons']}")

        print("\n=== CROSS-SESSION IDENTITY CLUSTERS (same Client Hints, different claimed platform) ===\n")
        clusters = find_identity_clusters(conn)
        if not clusters:
            print("none found")
        for c in clusters:
            print(f"fingerprint sec-ch-ua={c['fingerprint'][0]!r} platform={c['fingerprint'][1]!r} "
                  f"accept-language={c['fingerprint'][2]!r}")
            print(f"  claimed platforms in this cluster: {c['platforms_claimed']}")
            print(f"  {len(c['members'])} sessions: "
                  + ", ".join(f"{m['session_id'][:8]}({m['label']}/{m['ua_platform_guess']})" for m in c["members"]))
            print()


if __name__ == "__main__":
    main()

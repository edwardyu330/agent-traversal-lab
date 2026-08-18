"""Best-effort sync of revealed player name/email to a Google Sheet, so leads
land somewhere outside data/traversal.db too. Requires a Google Cloud service
account JSON key — see CLAUDE.md's "Google Sheets sync" section for the setup
steps only a human with Google Cloud Console access can do (create the service
account, share the spreadsheet with its email). Never raises: a missing or
broken Sheets connection must not break the reveal flow for players — that's
core research data, this is a bonus lead-capture side effect.
"""

import os
from datetime import datetime, timezone

SPREADSHEET_ID = "1IF8T_-kPBSjQ3ppH1xjnFBqpL_ZdBQCPVufzjOxTR-k"

_sheet = None
_attempted = False


def _get_sheet():
    global _sheet, _attempted
    if _sheet is not None:
        return _sheet
    if _attempted:
        return None  # already tried and failed this process; don't retry every reveal
    _attempted = True

    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not creds_path:
        print("[google_sheets] GOOGLE_SERVICE_ACCOUNT_FILE not set — skipping Sheets sync "
              "(reveal still succeeds, just isn't mirrored to the spreadsheet)")
        return None

    try:
        import gspread

        client = gspread.service_account(filename=creds_path)
        _sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        if not _sheet.get_all_values():
            _sheet.append_row(["timestamp_utc", "name", "email", "label", "tool", "player_score", "session_id"])
        print("[google_sheets] connected")
    except Exception as e:
        print(f"[google_sheets] connection failed, sync disabled for this process: {e}")
        return None
    return _sheet


def append_reveal(session_id: str, label: str, tool: str | None, name: str | None,
                   email: str | None, player_score: int | None) -> None:
    """Fire-and-forget: appends one row if either name or email was given.
    Swallows every exception — see module docstring for why."""
    if not name and not email:
        return
    sheet = _get_sheet()
    if sheet is None:
        return
    try:
        sheet.append_row([
            datetime.now(timezone.utc).isoformat(),
            name or "",
            email or "",
            label,
            tool or "",
            player_score if player_score is not None else "",
            session_id,
        ])
    except Exception as e:
        print(f"[google_sheets] append_row failed (reveal still succeeded): {e}")

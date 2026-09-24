"""Google Sheets tracking for the WhatsApp lead agent (thread-safe, whitespace-tolerant)."""
import os
import threading
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

import config  # triggers load_dotenv so GOOGLE_* env vars are available

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADERS = ["Name", "Number", "Status", "Last Contacted",
           "Follow-ups Sent", "Last Reply", "Last Reply At", "Notes"]
TS_FMT = "%Y-%m-%d %H:%M:%S"

_ws = None
_hdr = None
_lock = threading.RLock()


def _now():
    return datetime.now().strftime(TS_FMT)


def _stamp():
    """A short, human-readable timestamp for the Notes column — 'Sep 23, 2:30 PM'.
    The full ISO clock with seconds is what made the old notes read like a machine
    log; the client only needs to know roughly when, not to the second."""
    t = datetime.now()
    return f"{t.strftime('%b %d')}, {t.strftime('%I:%M').lstrip('0')} {t.strftime('%p')}"


_NOTES_KEEP = 3  # newest N entries only — the Status column already carries the state


def _tidy_notes(existing, note):
    """Turn the Notes cell into a short, readable trail instead of an ever-growing
    one-liner. Old rows joined every note with ' | ' on a single line, which is
    what made the sheet a wall of text; we split on both that and newlines so a
    half-migrated cell still tidies up, drop a note that just repeats the most
    recent one (the retry loop can re-write the same line), keep only the last
    few, and put each on its own line so it wraps cleanly in the cell."""
    parts = [p.strip() for chunk in (existing or "").split("\n")
             for p in chunk.split(" | ") if p.strip()]

    def _body(s):  # the note text without its "[stamp] " prefix, for comparison
        return s.split("] ", 1)[-1] if s.startswith("[") else s

    if not parts or _body(parts[-1]) != note.strip():
        parts.append(f"[{_stamp()}] {note.strip()}")
    return "\n".join(parts[-_NOTES_KEEP:])


def _digits(s):
    return "".join(ch for ch in str(s) if ch.isdigit())


def ws():
    global _ws
    with _lock:
        if _ws is not None:
            return _ws
        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE")
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        if not creds_file or not sheet_id:
            raise RuntimeError("GOOGLE_CREDENTIALS_FILE and GOOGLE_SHEET_ID must be set in .env")
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
        worksheet = gspread.authorize(creds).open_by_key(sheet_id).sheet1
        if not worksheet.row_values(1):
            worksheet.update(range_name="A1", values=[HEADERS])
        _ws = worksheet
        return _ws


def _headers(worksheet):
    global _hdr
    if _hdr is None:
        _hdr = [h.strip() for h in worksheet.row_values(1)]
    return _hdr


def _col(worksheet, name):
    for i, h in enumerate(_headers(worksheet), start=1):
        if h == name:
            return i
    raise ValueError(f"Column {name!r} not found in {_headers(worksheet)}")


def _find_row(worksheet, number):
    target = _digits(number)
    values = worksheet.col_values(_col(worksheet, "Number"))
    for i, v in enumerate(values[1:], start=2):
        vd = _digits(v)
        if vd and (vd == target or vd.endswith(target) or target.endswith(vd)):
            return i
    return None


def _rows(worksheet):
    values = worksheet.get_all_values()
    if not values:
        return []
    headers = [h.strip() for h in values[0]]
    out = []
    for i, row in enumerate(values[1:], start=2):
        rec = dict(zip(headers, row))
        rec["_row"] = i
        out.append(rec)
    return out


def _set(worksheet, row, header, value):
    worksheet.update_cell(row, _col(worksheet, header), value)


def get_pending_leads():
    with _lock:
        worksheet = ws()
        out = []
        for rec in _rows(worksheet):
            number = str(rec.get("Number", "")).strip()
            status = str(rec.get("Status", "")).strip().lower()
            if number and status in ("", "pending"):
                out.append({"row": rec["_row"], "name": rec.get("Name", "").strip(), "number": number})
        return out


def get_followup_candidates(after_hours, max_followups):
    with _lock:
        worksheet = ws()
        now = datetime.now()
        due, exhausted = [], []
        for rec in _rows(worksheet):
            number = str(rec.get("Number", "")).strip()
            status = str(rec.get("Status", "")).strip().lower()
            if not number or status != "sent":
                continue
            try:
                fups = int(str(rec.get("Follow-ups Sent", "0")).strip() or "0")
            except ValueError:
                fups = 0
            last = str(rec.get("Last Contacted", "")).strip()
            try:
                last_dt = datetime.strptime(last, TS_FMT) if last else None
            except ValueError:
                last_dt = None
            if last_dt is None or (now - last_dt).total_seconds() / 3600.0 < after_hours:
                continue
            item = {"row": rec["_row"], "name": rec.get("Name", "").strip(),
                    "number": number, "followups": fups}
            (exhausted if fups >= max_followups else due).append(item)
        return due, exhausted


def update_fields(number, **fields):
    with _lock:
        worksheet = ws()
        row = _find_row(worksheet, number)
        if row is None:
            return False
        for header, value in fields.items():
            _set(worksheet, row, header, value)
        return True


def mark_sent(number):
    update_fields(number, **{"Status": "sent", "Last Contacted": _now()})


def get_status(number):
    """Current Status cell for a lead, lowercased and stripped ('' if the
    number isn't in the sheet at all, e.g. an Instagram lead). Used by
    main.py to check "are we actually waiting on a cold-send delivery
    confirmation for this recipient?" before touching Status on a webhook
    status callback — a delivered/failed event fires for EVERY outbound
    message (cold sends, follow-ups, and live replies alike), and without
    this check a delivered reply to an already-escalated lead would silently
    stomp their "escalated" status back to "sent"."""
    with _lock:
        worksheet = ws()
        row = _find_row(worksheet, number)
        if row is None:
            return ""
        return (worksheet.cell(row, _col(worksheet, "Status")).value or "").strip().lower()


def increment_followup(number):
    with _lock:
        worksheet = ws()
        row = _find_row(worksheet, number)
        if row is None:
            return
        cur = worksheet.cell(row, _col(worksheet, "Follow-ups Sent")).value
        try:
            n = int(str(cur).strip() or "0")
        except ValueError:
            n = 0
        _set(worksheet, row, "Follow-ups Sent", n + 1)
        _set(worksheet, row, "Last Contacted", _now())


def mark_status(number, status, note=None):
    with _lock:
        worksheet = ws()
        row = _find_row(worksheet, number)
        if row is None:
            return
        _set(worksheet, row, "Status", status)
        if note:
            existing = worksheet.cell(row, _col(worksheet, "Notes")).value or ""
            _set(worksheet, row, "Notes", _tidy_notes(existing, note))


def record_reply(number, text, name=None):
    with _lock:
        worksheet = ws()
        row = _find_row(worksheet, number)
        if row is None:
            worksheet.append_row([name or "", str(number), "replied", "", "0", text, _now(), ""],
                                 value_input_option="USER_ENTERED")
            return
        _set(worksheet, row, "Last Reply", text)
        _set(worksheet, row, "Last Reply At", _now())
        status_now = (worksheet.cell(row, _col(worksheet, "Status")).value or "").strip().lower()
        if status_now != "escalated":
            _set(worksheet, row, "Status", "replied")
        if name and not (worksheet.cell(row, _col(worksheet, "Name")).value or "").strip():
            _set(worksheet, row, "Name", name)


def get_contacted_leads(limit=None):
    """Every lead cold-messaged at least once (Status not blank/pending),
    newest Last Contacted first. Powers the dashboard's 'Cold outreach sent' card."""
    with _lock:
        worksheet = ws()
        out = []
        for rec in _rows(worksheet):
            number = str(rec.get("Number", "")).strip()
            status = str(rec.get("Status", "")).strip().lower()
            if number and status not in ("", "pending"):
                out.append({
                    "name": rec.get("Name", "").strip(),
                    "number": number,
                    "status": status,
                    "last_contacted": rec.get("Last Contacted", "").strip(),
                })
        out.sort(key=lambda r: r["last_contacted"], reverse=True)
        if limit:
            out = out[:limit]
        return out

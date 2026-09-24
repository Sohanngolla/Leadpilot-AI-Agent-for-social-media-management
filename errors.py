"""
errors.py — the "Doctor Desk": one place that says WHAT went wrong, in two
languages.

WHY THIS FILE EXISTS
Every module in this project already handles its own failures well — it catches,
it logs a specific reason, and for the failures that matter it alerts the owner
on WhatsApp. The one thing missing was memory and a surface:

  * Docker container logs vanish when the container is rebuilt, so an error from
    last week is gone the moment you `docker compose up --build`. There was no
    lasting record to look back on.
  * Two very different people need to know when something breaks. The CLIENT (a
    business owner) needs plain language and business impact — "Instagram replies
    paused, WhatsApp is fine". The DEVELOPER needs the error type, the module and
    the stack trace to actually fix it.

This module solves both WITHOUT touching the error handling that already works.
It attaches a single logging handler to the project's logger tree, so every
`log.warning(...)`, `log.error(...)` and `log.exception(...)` the existing code
already emits is captured into a `errors` table in the same SQLite database
(which lives on the persistent ./data volume, so it survives rebuilds).

This file is only the ENGINE — capture, rollup, classify, query, resolve. The
two audience views live inside the control panel now, as the "Health" tab (plain
language, for the client) and the "Developer" tab (source, trace, resolve) of
/dashboard. dashboard.py reads summary()/recent() and owns the routes;
dashboard_view.py draws the markup in the panel's own brand. Keeping the surface
there means one password, one page, and one visual language rather than a second
tool bolted on at /desk.

DESIGN NOTES
  * Identical errors are ROLLED UP, not duplicated: a fingerprint (source + a
    digit-masked message) means "AI failed for 40 different leads" is one row
    with count=40, not 40 rows that bury everything else.
  * The handler can NEVER break the thing it is watching. Every path is wrapped;
    a logging call that fails to persist is swallowed, because a broken desk must
    not take down a working reply loop.
  * It listens at WARNING and above only. INFO is normal operation, not an issue.
  * Its own logger is deliberately NOT captured — otherwise an error while saving
    an error would recurse.
"""
import hashlib
import logging
import re
import threading
from datetime import datetime

import config
import store

# Our own logger. Named so install() can exclude it from capture — saving an
# error must never be able to generate another error to save.
log = logging.getLogger("whatsapp-relay.errors")

ROOT_LOGGER = "whatsapp-relay"

# --- Schema -----------------------------------------------------------------
# Reuses store's per-thread WAL connection, so this table sits in the same
# agent.db on the persistent volume and inherits the same concurrency handling.
_inited = False
_init_lock = threading.Lock()


def _db():
    conn = store._conn()
    global _inited
    if not _inited:
        with _init_lock:
            if not _inited:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS errors (
                        fingerprint    TEXT PRIMARY KEY,   -- rollup key
                        first_seen     TEXT NOT NULL,
                        last_seen      TEXT NOT NULL,
                        count          INTEGER NOT NULL DEFAULT 1,
                        level          TEXT NOT NULL,       -- WARNING|ERROR|CRITICAL
                        source         TEXT NOT NULL,       -- which module logged it
                        category       TEXT NOT NULL,       -- for grouping + client view
                        severity       TEXT NOT NULL,       -- low|medium|high
                        client_message TEXT NOT NULL,       -- plain language for the owner
                        tech_message   TEXT NOT NULL,       -- the raw log line
                        traceback      TEXT,                -- dev only, may be NULL
                        resolved       INTEGER NOT NULL DEFAULT 0,
                        alerted_at     TEXT                 -- last time a phone was buzzed
                    );
                    CREATE INDEX IF NOT EXISTS idx_errors_last ON errors(resolved, last_seen);
                """)
                # Migrate databases created before alerting existed. On a fresh
                # table the column is already there and this raises "duplicate
                # column name" — harmless, so swallow it.
                try:
                    conn.execute("ALTER TABLE errors ADD COLUMN alerted_at TEXT")
                except Exception:
                    pass
                conn.commit()
                _inited = True
    return conn


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_human():
    """Timestamp for the alert template's {{2}} — the phone-facing 'Time:' line."""
    return datetime.now().strftime("%d %b %Y, %I:%M %p")


# --- Classifier -------------------------------------------------------------
# Turns a raw log record into (category, severity, plain-language message). The
# rules are checked top to bottom, first match wins, so the specific ones sit
# above the catch-all. `src` is the logger name (e.g. "whatsapp-relay.pacer"),
# `msg` the lowercased text. Keep the client_message about IMPACT, never jargon.

def _classify(src, level, msg):
    m = msg.lower()

    if src.endswith("credentials") or ("token" in m and "instagram" in m):
        return ("instagram_token", "high",
                "Instagram replies are at risk — its secure connection needs "
                "renewing soon. WhatsApp is not affected.")

    if "could not reply" in m or "brain failed" in m or "ai error" in m or src.endswith("brain"):
        return ("ai_brain", "high",
                "The AI couldn't answer one or more messages just now, so those "
                "leads may not have received an automatic reply. It usually "
                "recovers on its own within a few minutes.")

    if src.endswith(".ig") or "instagram" in m and "send" in m:
        return ("instagram_send", "medium",
                "A reply to a lead couldn't be delivered on Instagram.")

    if src.endswith(".wa") or "failed to send" in m:
        return ("whatsapp_send", "medium",
                "A reply to a lead couldn't be delivered on WhatsApp.")

    if "sheet" in m:
        return ("google_sheet", "low",
                "Your Google Sheet couldn't be updated. Your leads are safe "
                "inside the app — only the spreadsheet copy is behind.")

    if "gave up" in m or "attempt" in m or "retry" in m:
        return ("cold_delivery", "medium",
                "A first-contact message to a new lead couldn't be delivered "
                "after several attempts.")

    if "scheduler pass" in m:
        return ("scheduler", "low",
                "A background task (outreach or follow-ups) hit a temporary "
                "snag. It will try again automatically on the next cycle.")

    if "webhook payload" in m or "unexpected" in m:
        return ("bad_payload", "low",
                "An incoming message arrived in an unexpected format and was "
                "skipped. No action is usually needed.")

    # Catch-all: severity tracks the log level so a bare ERROR still stands out.
    sev = {"WARNING": "low", "ERROR": "medium", "CRITICAL": "high"}.get(level, "medium")
    return ("other", sev,
            "The system logged a technical issue. It has been recorded for the "
            "developer to review.")


_DIGITS = re.compile(r"\d+")


def _fingerprint(src, level, category, msg):
    """A stable key for 'the same kind of problem'. Numbers (lead ids, phone
    numbers, counts, error codes that vary) are masked so the same failure for
    100 different leads rolls up into one counted row instead of flooding."""
    skeleton = _DIGITS.sub("#", msg.lower())[:120]
    raw = f"{src}|{level}|{category}|{skeleton}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


# --- Recording --------------------------------------------------------------

def record(source, level, message, traceback_text=None):
    """Persist one issue, rolling it up onto any identical earlier one. Defensive
    to the core: this runs from inside a logging handler on live threads, so it
    must never raise. The worst acceptable outcome is that one issue goes
    unrecorded — never that a reply thread dies because the desk had a bad day."""
    try:
        message = (message or "").strip() or "(no message)"
        category, severity, client_message = _classify(source, level, message)
        fp = _fingerprint(source, level, category, message)
        now = _now()
        conn = _db()
        # First occurrence inserts; repeats bump the count and last_seen and,
        # importantly, flip resolved back to 0 — an issue you marked fixed that
        # happens again is news, not history.
        conn.execute(
            "INSERT INTO errors(fingerprint, first_seen, last_seen, count, level, "
            "source, category, severity, client_message, tech_message, traceback, resolved) "
            "VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(fingerprint) DO UPDATE SET "
            "last_seen=excluded.last_seen, count=errors.count+1, resolved=0, "
            "tech_message=excluded.tech_message, "
            "traceback=COALESCE(excluded.traceback, errors.traceback)",
            (fp, now, now, level, source, category, severity,
             client_message, message[:2000],
             (traceback_text or None)),
        )
        conn.commit()
        _maybe_alert(fp, level, source, category, severity, client_message,
                     message[:2000], traceback_text)
        return fp
    except Exception:                       # pragma: no cover - defensive
        # Fall back to stderr via the handler's own machinery; never re-log
        # through the captured tree.
        return None


# --- Alerting ---------------------------------------------------------------
# The Doctor Desk already REMEMBERS every fault. This part decides when a fault
# is worth interrupting a human, and mirrors hot-lead escalation exactly: the
# developer gets the technical brief (module, level, trace) on their WhatsApp,
# the client gets a plain-language business notice on theirs, and a per-
# fingerprint cooldown stops a recurring fault from buzzing on every occurrence.
#
# Three things make this safe to run from inside a logging handler on live reply
# threads:
#   1. The decision is cheap and the SEND is fired on a throwaway thread, so a
#      slow or blocked WhatsApp call never stalls the reply loop that logged.
#   2. whatsapp_client logs its own failures at WARNING, which this very handler
#      captures — a textbook recursion. A thread-local "busy" flag means any log
#      emitted WHILE we are sending an alert records to the desk but never starts
#      a second alert.
#   3. The cooldown is stamped BEFORE the send, so even if the flag were somehow
#      bypassed, the same fingerprint is no longer "due" and cannot loop.
#
# Routing by severity (see _classify): the developer hears about real faults
# (high + medium); the client is only interrupted for high-severity, business-
# impacting issues, so low-severity noise never reaches them. Change the two
# sets below to re-tune who hears what.
_DEV_SEVERITIES = {"high", "medium"}
_CLIENT_SEVERITIES = {"high"}

# Categories the CLIENT is never paged about, whatever their severity. A WhatsApp
# reply that fails to send is already covered by the send-retry mechanism and
# usually clears itself, so it would only be noise on the client's phone. The
# developer still gets it. Add category names here to hide more from the client.
_CLIENT_MUTE_CATEGORIES = {"whatsapp_send", "cold_delivery"}

_alerting = threading.local()


def _alerts_on():
    try:
        return store.get_bool("error_alerts_enabled")
    except Exception:
        return False


def _cooldown_hours():
    try:
        return store.get_float("error_alert_cooldown_hours")
    except Exception:
        return 6.0


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _alert_due(fp, cooldown_hours):
    """Has enough quiet passed to buzz a phone about this fingerprint again?

    Stamps alerted_at the moment it says yes — before any message goes out — so a
    failed send that logs its own error can't loop back through record() and
    re-alert. On any DB trouble it returns False: a broken desk stays silent
    rather than risking a flood.
    """
    try:
        conn = _db()
        row = conn.execute(
            "SELECT alerted_at FROM errors WHERE fingerprint = ?", (fp,)).fetchone()
        due = True
        if row is not None and row["alerted_at"]:
            try:
                last = datetime.strptime(row["alerted_at"], "%Y-%m-%d %H:%M:%S")
                due = (datetime.now() - last).total_seconds() >= cooldown_hours * 3600
            except ValueError:
                due = True                      # unparseable = treat as never alerted
        if due:
            conn.execute("UPDATE errors SET alerted_at = ? WHERE fingerprint = ?",
                         (_now(), fp))
            conn.commit()
        return due
    except Exception:                           # pragma: no cover - defensive
        return False


def _dev_param(level, source, severity, tech_message, traceback_text):
    """The developer alert as ONE line, for the system-alert template's {{1}}.

    Template variables can't hold newlines, so the old multi-line brief is
    flattened into a single sentence: severity · source · level — message, then
    the failing frame if we have one. _clip already collapses whitespace, so the
    result is guaranteed newline/tab-free."""
    parts = [f"{severity.upper()} · {source} · {level} — {_clip(tech_message, 400) or '—'}"]
    if traceback_text:
        tail = [ln for ln in traceback_text.strip().splitlines() if ln.strip()]
        if tail:
            parts.append(f"Where: {_clip(tail[-1], 160)}")
    parts.append("Full detail is in the dashboard's Developer tab.")
    return _clip(" · ".join(parts), 900)


def _client_param(client_message):
    """The client alert as one line, for the system-alert template's {{1}}."""
    return _clip(client_message or
                 "A technical issue was detected. The team has been alerted and "
                 "is looking into it.", 900)


def _send(number, param):
    """One best-effort WhatsApp alert via the approved system-alert template.

    Templates deliver outside the 24h window (plain text does not), so an alert
    reaches the recipient even if they haven't messaged the business number
    lately. `param` is the flattened {{1}} message; {{2}} is the timestamp.

    Imported lazily so the engine stays light for the hermetic tests and so a
    missing transport can never break import."""
    try:
        import whatsapp_client
        whatsapp_client.send_template(
            number, config.SYSTEM_ALERT_TEMPLATE, [param or "—", _now_human()])
    except Exception:                           # pragma: no cover - defensive
        # whatsapp_client already logged the reason (and this handler recorded
        # it). A failed alert must die right here — never re-raise, never re-log.
        pass


def _dispatch(fp, level, source, category, severity, client_message, tech_message, traceback_text):
    """Runs on its own daemon thread. Decides due-ness once, then sends."""
    _alerting.busy = True
    try:
        if not _alert_due(fp, _cooldown_hours()):
            return
        if severity in _DEV_SEVERITIES and config.DEVELOPER_NUMBER:
            _send(config.DEVELOPER_NUMBER,
                  _dev_param(level, source, severity, tech_message, traceback_text))
        # Skip the client copy when the category is one they've asked not to hear
        # about (transient send failures), or when it would land on the same phone
        # as the developer alert — the technical one is the more actionable.
        if (severity in _CLIENT_SEVERITIES and category not in _CLIENT_MUTE_CATEGORIES
                and config.CLIENT_NUMBER
                and config.CLIENT_NUMBER != config.DEVELOPER_NUMBER):
            _send(config.CLIENT_NUMBER, _client_param(client_message))
    finally:
        _alerting.busy = False


def _maybe_alert(fp, level, source, category, severity, client_message, tech_message, traceback_text):
    """Cheap gate before spawning a sender. Called from record(), so it must
    never raise and must never start an alert about an alert."""
    try:
        if getattr(_alerting, "busy", False):   # we are mid-send on this thread
            return
        if not _alerts_on():
            return
        if severity not in _DEV_SEVERITIES and severity not in _CLIENT_SEVERITIES:
            return
        threading.Thread(
            target=_dispatch,
            args=(fp, level, source, category, severity, client_message, tech_message, traceback_text),
            daemon=True,
        ).start()
    except Exception:                           # pragma: no cover - defensive
        pass


class _Handler(logging.Handler):
    """Bridges Python logging into the errors table. Attached once by install().

    We read the record rather than re-deriving anything: `record.name` is the
    module logger, `record.getMessage()` renders the "%s"-style args the code
    already passed, and exc_info (present whenever someone used log.exception or
    log.error(..., exc_info=True)) becomes the developer's stack trace.
    """

    def emit(self, rec):
        try:
            if rec.name == log.name:         # never capture our own logger
                return
            tb = None
            if rec.exc_info:
                tb = logging.Formatter().formatException(rec.exc_info)
            record(rec.name, rec.levelname, rec.getMessage(), tb)
        except Exception:                    # pragma: no cover - defensive
            self.handleError(rec)


_handler = None


def install(level=logging.WARNING):
    """Attach the capture handler to the whole project logger tree. Idempotent —
    calling it twice does not double-record. Call once at startup (main.py)."""
    global _handler
    if _handler is not None:
        return
    _handler = _Handler()
    _handler.setLevel(level)
    logging.getLogger(ROOT_LOGGER).addHandler(_handler)
    log.info("Error desk installed — capturing %s and above.",
             logging.getLevelName(level))


# --- Queries ----------------------------------------------------------------

def recent(limit=100, include_resolved=False):
    """Rows for the developer view, newest activity first."""
    sql = ("SELECT fingerprint, first_seen, last_seen, count, level, source, "
           "category, severity, client_message, tech_message, traceback, resolved "
           "FROM errors ")
    if not include_resolved:
        sql += "WHERE resolved = 0 "
    sql += "ORDER BY last_seen DESC LIMIT ?"
    return [dict(r) for r in _db().execute(sql, (limit,)).fetchall()]


_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def summary():
    """The business view: one plain-language line per KIND of open issue, worst
    first, with how many times and how recently it happened. Empty means healthy.

    Groups by category so the owner sees "the AI had trouble replying (x40, last
    at 15:12)" as a single, human statement rather than a wall of rows.
    """
    rows = _db().execute(
        "SELECT category, severity, client_message, SUM(count) AS n, "
        "MAX(last_seen) AS last FROM errors WHERE resolved = 0 "
        "GROUP BY category").fetchall()
    items = [dict(r) for r in rows]
    items.sort(key=lambda r: (_SEV_RANK.get(r["severity"], 3), r["last"]), reverse=False)
    return items


def resolve(fingerprint=None, all_resolved=False):
    """Mark one issue (or everything) as dealt with, so it drops off both views
    until it happens again."""
    conn = _db()
    if all_resolved:
        conn.execute("UPDATE errors SET resolved = 1 WHERE resolved = 0")
    elif fingerprint:
        conn.execute("UPDATE errors SET resolved = 1 WHERE fingerprint = ?", (fingerprint,))
    conn.commit()


def prune(keep_days=30):
    """Housekeeping: forget resolved issues older than keep_days. Called
    opportunistically; the rollup already keeps the table small, so this is only
    to stop long-resolved history accumulating forever."""
    try:
        cutoff = (datetime.now().timestamp() - keep_days * 86400)
        cutoff_str = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M:%S")
        conn = _db()
        conn.execute("DELETE FROM errors WHERE resolved = 1 AND last_seen < ?", (cutoff_str,))
        conn.commit()
    except Exception:                        # pragma: no cover - defensive
        pass

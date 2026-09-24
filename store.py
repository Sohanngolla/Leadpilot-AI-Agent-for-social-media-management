"""
SQLite-backed state for the lead agent: live-editable settings, per-lead
conversation memory, escalation/mute flags, and daily send counters.

Why a database and not just .env: the CLIENT needs to change how the agent
behaves — its persona prompt, reply speed, follow-up delay, automation on/off —
from the dashboard while the server keeps running 24/7. So the split is:

    config.py  -> secrets an operator sets once (tokens, ids, API keys)
    store.py   -> knobs the client can change any time, read LIVE on every use

"Read live" is the important part: nothing is cached, so a change made in the
dashboard takes effect on the very next message or scheduler pass. No restart,
no redeploy, no SSH.

No new dependencies — sqlite3 is stdlib. Thread-safe by giving each thread its
own connection (the relay runs blocking work in a threadpool) and using WAL so
readers never block the writer.
"""
import os
import sqlite3
import threading
from datetime import datetime, date, timedelta

DB_PATH = os.environ.get("AGENT_DB_PATH", os.path.join(os.path.dirname(__file__), "data", "agent.db"))

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False


# --- Defaults ---------------------------------------------------------------
# Every client-editable knob lives here with a safe default. A key that is not
# in the DB falls back to this table, so a fresh install works out of the box
# and adding a new setting later never needs a migration.

# The business name is read from the environment so this codebase ships only a
# generic placeholder; a real deployment sets BRAND_NAME in .env.
_BRAND = (os.environ.get("BRAND_NAME") or "Dunder Mifflin").strip() or "Dunder Mifflin"

DEFAULT_PERSONA = f"""You are the assistant for {_BRAND}, an interior-design and construction firm in Hyderabad.
You are texting someone who enquired about a project.

ANSWER THE MESSAGE YOU WERE SENT. Match what it actually says, and its size:
- Just a greeting ("hi", "hello", "gm") and nothing else -> greet them back and
  leave it there, plus at most one short opener. Do not pitch, do not list
  services, do not fire questions at someone who has said one word.
- A greeting WITH something attached ("hi, where are we at?", "hello, do you do
  false ceilings?") -> the attached part is the point. Answer that. The greeting
  is worth three words at most.
- Someone we have spoken to before -> pick the thread up where it stopped. No
  greeting again, no introducing yourself again, and never ask for something
  they have already told you.
- A specific question -> answer that question, then at most one question back.

STAY INSIDE THE BUSINESS. You only discuss interiors, construction, their space,
their project, and arranging a call with our team. If a message is about anything
else — general knowledge, politics, news, cricket, films, maths, code, your own
opinions, who runs a country, jokes, personal questions — do not answer it. Not
briefly, not partly, not even when the answer is obvious and harmless. Say in one
line that this number is only for {_BRAND} project enquiries, ask one
question about theirs, and nothing more. Do not explain yourself, do not
apologise twice, do not argue, and never be drawn into it a second time.

Always:
- Never quote prices, rates, or timelines. If asked, say our design lead will
  share details on a quick call.
- Ask ONE question at a time. Never send a list of questions.
- Keep replies short — one or two sentences, like a real person texting.
- Never say you are an AI, a bot, or an assistant. You are part of the team.
- Do not invent facts about past projects, materials, or availability.
- Across the conversation, gently collect: name, project type (home / office /
  commercial), location, approximate size, what they want done, and a good time
  for our team to call. Gently — one at a time, in between answering them.
- If they are clearly ready to move forward, or ask to speak to someone, or ask
  for a quote or a site visit, hand them to a human instead of continuing."""

DEFAULT_ESCALATION_CRITERIA = """Mark the lead as SERIOUS when any of these is true:
- they ask for a price, quote, estimate or budget discussion
- they ask for a call, meeting, site visit or to speak to a person
- they give real project details (location + type + size or timeline)
- they say they are ready to start, or ask how to proceed
- they sound urgent ("asap", "this month", "immediately")

Mark the lead as TIME_WASTER when the message has nothing to do with a project
and is not ordinary politeness: general-knowledge questions, testing what you
are, steering you onto other subjects, abuse, flirting, nonsense, or the same
joke again. A greeting, a thank-you, "one min", a wrong number or a confused
question about us is NOT a time-waster — those are normal. Every reply costs
real money, so this verdict is what stops us paying for a conversation that was
never about a project.

Mark the lead as NOT_INTERESTED when they ask to stop or say they don't want
this.

Otherwise the lead is BROWSING (just asking general questions)."""

DEFAULTS = {
    # --- master switches (the dashboard toggle flips these) ---
    "cold_auto": "false",          # auto cold-outreach to pending leads
    "followup_auto": "true",       # auto follow-up non-responders
    "reply_auto": "true",          # auto-reply to inbound messages

    # --- human pacing: makes replies feel typed, and keeps us under every
    #     rate limit at the same time. See pacer.py ---
    "reply_delay_min_seconds": "25",
    "reply_delay_max_seconds": "75",
    # ...but only for the FIRST reply. The slow window is what stops a stranger's
    # opening message looking machine-answered; applying it to every turn just
    # makes a live conversation unusable, because nobody waits a minute between
    # lines of a chat they are already having. See pacer._think_delay.
    "reply_delay_followup_seconds": "5",
    "reply_workers": "3",          # parallel conversations in flight
    "brain_max_per_minute": "8",   # hard cap on AI calls/min (429 protection)
    "typing_indicator": "true",

    # --- outreach volume + safety ---
    "cold_daily_cap": "50",        # never exceed this many cold sends per day
    "cold_batch_limit": "10",      # per scheduler pass
    "cold_pacing_seconds": "45",   # gap between cold sends
    "followup_after_hours": "24",
    "followup_max": "2",
    "scheduler_interval_seconds": "300",
    # --- outreach quiet hours (client's local time, IST) ---
    # Cold outreach AND its retries only go out inside this window, so a lead is
    # never messaged at, say, 2 AM their time — the fastest way to read as spam
    # and to annoy a real prospect. Hours are 24h IST; the container runs UTC and
    # within_outreach_window() converts. 10 -> 19 means 10:00 AM to 7:00 PM IST.
    # Setting start == end switches the window off (send at any hour).
    "cold_window_start_hour": "10",
    "cold_window_end_hour": "19",
    # --- cold-send delivery retry (three-attempt escalation) ---
    # A cold TEMPLATE send can be rejected by Meta for reasons a same-second
    # retry will never fix — most commonly error 131049, "not delivered to
    # maintain healthy ecosystem engagement", which per Meta's own docs is a
    # PER-RECIPIENT cap on marketing templates from ANY business, not a
    # problem with our account or this template. Meta's own guidance is to
    # wait at least 24h before resending. So a failed cold send is queued
    # here rather than retried on the next scheduler pass:
    #   attempt 1 fails -> wait cold_retry_backoff_hours -> attempt 2
    #     (same cold template, config.CAMPAIGN_TEMPLATE_NAME)
    #   attempt 2 fails -> wait cold_retry_backoff_hours -> attempt 3
    #     (a different, restructured template — see jobs.COLD_RETRY_TEMPLATE_3)
    #   attempt 3 fails -> give up: mute the lead and stamp the sheet
    #     "failed_permanent" so it never resurfaces in a batch again.
    # This master switch lets the client pause the whole retry mechanism from
    # the dashboard without touching cold_auto (which only gates attempt 1).
    "cold_retry_auto": "true",
    "cold_retry_backoff_hours": "24",

    # --- the brain ---
    "brain": "gemini",             # "gemini" | "openclaw"
    # 2.5-flash was retired for new API keys in 2026 — Google returns 404
    # NOT_FOUND with a pointer to this one. Model id only; the call shape
    # (v1beta :generateContent + x-goog-api-key) is unchanged.
    "gemini_model": "gemini-3.6-flash",
    # Gemini's 503 is "this model is currently experiencing high demand" — a
    # per-MODEL capacity problem, not our quota and not our code. More retries on
    # the same model just wait longer for the same overloaded pool, so after it
    # gives up we ask a DIFFERENT model instead. Empty means "work out which one
    # yourself" — brain.py asks the API which models the key can actually use, so
    # a model id retired by Google can never leave us with a dead fallback.
    "gemini_fallback_model": "",
    # A read timeout is not free: the lead is sitting there watching a typing
    # bubble that Meta drops after ~25s. 30s is long enough for a slow-but-real
    # answer and short enough to move on to the fallback while it still matters.
    "gemini_timeout_seconds": "30",
    # Whole-call ceiling across every attempt and every model. Without it, four
    # attempts at 30s plus backoff could leave someone waiting three minutes for
    # a two-line reply, which reads as ignored rather than busy.
    "gemini_deadline_seconds": "120",
    "history_turns": "12",         # conversation memory depth per lead
    "system_prompt": DEFAULT_PERSONA,
    "escalation_criteria": DEFAULT_ESCALATION_CRITERIA,
    # Every reply costs tokens, so a conversation that is not about a project is
    # a straight loss. After this many off-topic messages in a row the agent
    # stops answering that lead and the owner is told once. Consecutive, not
    # total: one silly question in the middle of a real enquiry is not a
    # time-waster. 0 disables the cutoff entirely.
    "off_topic_strikes_max": "3",
    # OFF by design. Escalation alerts the owner but the agent KEEPS TALKING.
    # If it went quiet and the owner didn't happen to pick up his personal
    # phone, the hottest lead in the pipeline would sit there being ignored —
    # which is worse than a bot answering. Turn it on only if the client wants
    # radio silence after a handoff.
    "escalation_mutes_bot": "false",
    # Because the bot keeps replying, review() keeps running on that lead. One
    # alert per message would train the owner to ignore alerts, so re-alert only
    # after this much quiet.
    "escalation_alert_cooldown_hours": "6",
    # Error → WhatsApp alerts. The Doctor Desk records every fault regardless;
    # this master switch only governs whether it also texts a phone. The cooldown
    # is per-fingerprint, so a fault that recurs 40 times (one rolled-up row)
    # alerts once and then stays quiet for this long — the same logic that keeps
    # a hot lead from buzzing the owner on every message.
    "error_alerts_enabled": "true",
    "error_alert_cooldown_hours": "6",
    "handoff_message": "Let me bring in our design lead — they'll message you here shortly.",
}


# --- Connection handling ----------------------------------------------------

def _conn():
    """One connection per thread. WAL means a reader never blocks the writer,
    which matters because the scheduler, the webhook threadpool and the pacer
    workers all touch this file concurrently."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        _local.conn = conn
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn):
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                lead    TEXT NOT NULL,
                role    TEXT NOT NULL,          -- 'user' | 'model'
                text    TEXT NOT NULL,
                at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_turns_lead ON turns(lead, id);

            CREATE TABLE IF NOT EXISTS lead_state (
                lead        TEXT PRIMARY KEY,
                muted       INTEGER NOT NULL DEFAULT 0,   -- human has taken over
                escalated_at TEXT,
                last_intent TEXT,
                alerted_at  TEXT,                         -- last owner ping (cooldown)
                handled_at  TEXT,                         -- owner ticked it off
                off_topic_strikes INTEGER NOT NULL DEFAULT 0   -- consecutive off-topic messages
            );
            CREATE TABLE IF NOT EXISTS sends (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                lead TEXT NOT NULL,
                kind TEXT NOT NULL,             -- 'cold' | 'followup' | 'reply'
                day  TEXT NOT NULL,
                at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sends_day ON sends(day, kind);

            -- One row per lead CURRENTLY inside the cold delivery-retry flow.
            -- A row exists from the moment attempt 1 is submitted until the
            -- lead either delivers successfully (row deleted, see
            -- clear_cold_attempt) or exhausts all 3 attempts (row deleted,
            -- lead muted). last_attempt is the highest attempt number sent so
            -- far; next_retry_at is NULL while an attempt is in flight
            -- (submitted, awaiting Meta's delivery/failure callback) and set
            -- to a future timestamp once that attempt has failed and we're
            -- waiting out the backoff before trying the next one.
            CREATE TABLE IF NOT EXISTS cold_retries (
                lead          TEXT PRIMARY KEY,
                name          TEXT,
                last_attempt  INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                updated_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cold_retries_due ON cold_retries(next_retry_at);
        """)
        # CREATE TABLE IF NOT EXISTS won't add a column to a table that already
        # exists, so a database created before these columns existed needs them
        # bolted on. Names and types are literals here, never user input. SQLite
        # allows ADD COLUMN NOT NULL only with a default, which is why the strike
        # counter carries one — existing leads start from zero.
        have = {row["name"] for row in conn.execute("PRAGMA table_info(lead_state)")}
        for column, coltype in (("alerted_at", "TEXT"), ("handled_at", "TEXT"),
                                ("off_topic_strikes", "INTEGER NOT NULL DEFAULT 0")):
            if column not in have:
                conn.execute(f"ALTER TABLE lead_state ADD COLUMN {column} {coltype}")
        conn.commit()
        _initialised = True


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --- Settings ---------------------------------------------------------------

def get(key, default=None):
    """Read a setting LIVE. Falls back to DEFAULTS, then to `default`."""
    row = _conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return DEFAULTS.get(key, default)


def set(key, value):
    """Write a setting. Takes effect on the next read — no restart needed."""
    conn = _conn()
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), _now()),
    )
    conn.commit()
    return get(key)


def get_bool(key):
    return str(get(key, "false")).strip().lower() in ("1", "true", "yes", "on")


def get_int(key):
    try:
        return int(float(str(get(key, "0")).strip()))
    except (TypeError, ValueError):
        return int(float(DEFAULTS.get(key, 0)))


def get_float(key):
    try:
        return float(str(get(key, "0")).strip())
    except (TypeError, ValueError):
        return float(DEFAULTS.get(key, 0))


def all_settings():
    """Every knob with its current value + whether it's been customised.
    This is what the dashboard renders."""
    rows = {r["key"]: r["value"] for r in _conn().execute("SELECT key, value FROM settings")}
    return {
        key: {"value": rows.get(key, default), "default": default,
              "customised": key in rows and rows[key] != default}
        for key, default in DEFAULTS.items()
    }


# --- Conversation memory ----------------------------------------------------
# OpenClaw kept conversation state in its own session files. Doing it here means
# the container is stateless apart from one mounted volume, and the dashboard
# can show the client the actual transcript of any lead.

def append_turn(lead, role, text):
    conn = _conn()
    conn.execute("INSERT INTO turns(lead, role, text, at) VALUES(?, ?, ?, ?)",
                 (str(lead), role, text, _now()))
    conn.commit()


def history(lead, limit=None):
    """Recent turns oldest-first, capped to `history_turns` so the prompt stays
    small (and cheap) no matter how long the conversation runs."""
    limit = limit or get_int("history_turns")
    rows = _conn().execute(
        "SELECT role, text FROM turns WHERE lead = ? ORDER BY id DESC LIMIT ?",
        (str(lead), limit),
    ).fetchall()
    return [{"role": r["role"], "text": r["text"]} for r in reversed(rows)]


def transcript(lead):
    rows = _conn().execute(
        "SELECT role, text, at FROM turns WHERE lead = ? ORDER BY id", (str(lead),)
    ).fetchall()
    return [dict(r) for r in rows]


# --- Per-lead state (escalation / human takeover) ---------------------------

def _state_row(lead):
    return _conn().execute("SELECT * FROM lead_state WHERE lead = ?", (str(lead),)).fetchone()


def is_muted(lead):
    """True once a human has taken this conversation over, so the bot shuts up."""
    row = _state_row(lead)
    return bool(row and row["muted"])


def is_escalated(lead):
    """True if this lead has been flagged hot at some point before now.

    Separate from is_muted because escalation no longer implies silence — this
    is what lets a repeat alert say "STILL HOT" instead of announcing the same
    lead as if it were new.
    """
    row = _state_row(lead)
    return bool(row and row["escalated_at"])


def set_muted(lead, muted=True, intent=None):
    conn = _conn()
    conn.execute(
        "INSERT INTO lead_state(lead, muted, escalated_at, last_intent) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(lead) DO UPDATE SET muted=excluded.muted, "
        "escalated_at=COALESCE(excluded.escalated_at, lead_state.escalated_at), "
        "last_intent=COALESCE(excluded.last_intent, lead_state.last_intent)",
        (str(lead), 1 if muted else 0, _now() if muted else None, intent),
    )
    conn.commit()


def note_intent(lead, intent):
    """Record the model's latest verdict WITHOUT touching the mute flag.

    Separate from set_muted on purpose: a 'browsing' verdict must never quietly
    un-mute a conversation a human has already taken over.
    """
    conn = _conn()
    conn.execute(
        "INSERT INTO lead_state(lead, muted, last_intent) VALUES(?, 0, ?) "
        "ON CONFLICT(lead) DO UPDATE SET last_intent=excluded.last_intent",
        (str(lead), intent),
    )
    conn.commit()


def off_topic_strikes(lead):
    row = _state_row(lead)
    return int((row["off_topic_strikes"] if row is not None else 0) or 0)


def note_off_topic(lead):
    """Count one more off-topic message from this lead and return the new total.

    CONSECUTIVE by design — clear_off_topic() wipes it the moment they say
    anything about a real project. Someone who cracks one joke and then asks
    about their kitchen is a customer, not a time-waster, and cutting them off
    would cost the client an actual lead.
    """
    conn = _conn()
    conn.execute(
        "INSERT INTO lead_state(lead, muted, last_intent, off_topic_strikes) "
        "VALUES(?, 0, 'time_waster', 1) "
        "ON CONFLICT(lead) DO UPDATE SET last_intent='time_waster', "
        "off_topic_strikes=COALESCE(lead_state.off_topic_strikes, 0) + 1",
        (str(lead),),
    )
    conn.commit()
    return off_topic_strikes(lead)


def clear_off_topic(lead):
    """Reset the strike count, and drop a stale 'time_waster' verdict with it.

    The two always travel together: the verdict is what badges the lead as
    stopped in the dashboard, so leaving it behind after the owner hands the
    conversation back would show "Stopped" on a lead the agent is actively
    talking to. Cheap enough to call after every on-topic reply — the WHERE
    clause means the common case writes nothing at all.
    """
    conn = _conn()
    conn.execute(
        "UPDATE lead_state SET off_topic_strikes = 0, last_intent = "
        "CASE WHEN last_intent = 'time_waster' THEN 'browsing' ELSE last_intent END "
        "WHERE lead = ? AND (COALESCE(off_topic_strikes, 0) != 0 "
        "OR last_intent = 'time_waster')",
        (str(lead),),
    )
    conn.commit()


# --- Cold-send delivery retry queue -----------------------------------------
# See the cold_retries table + the DEFAULTS block above for the design. This
# is the single source of truth for "which attempt number is a given lead on,
# and when (if ever) is their next attempt due" — main.py's status webhook and
# jobs.py's send functions both read/write through here so the two can never
# disagree about where a lead stands.

def note_cold_attempt(lead, name, attempt):
    """Record that `attempt` (1, 2 or 3) has just been SUBMITTED to Meta for
    this lead. Clears next_retry_at — we're now waiting on Meta's own
    delivered/failed callback for this attempt, not on a backoff timer."""
    conn = _conn()
    conn.execute(
        "INSERT INTO cold_retries(lead, name, last_attempt, next_retry_at, updated_at) "
        "VALUES(?, ?, ?, NULL, ?) "
        "ON CONFLICT(lead) DO UPDATE SET name=excluded.name, "
        "last_attempt=excluded.last_attempt, next_retry_at=NULL, updated_at=excluded.updated_at",
        (str(lead), name, attempt, _now()),
    )
    conn.commit()


def get_cold_attempt(lead):
    """(last_attempt, name) for a lead currently in the retry flow, or (0, None)
    if they aren't tracked at all (never cold-messaged, or already resolved)."""
    row = _conn().execute(
        "SELECT last_attempt, name FROM cold_retries WHERE lead = ?", (str(lead),)
    ).fetchone()
    if row is None:
        return 0, None
    return row["last_attempt"], row["name"]


def schedule_cold_retry(lead):
    """The attempt just recorded via note_cold_attempt has FAILED. Leave
    last_attempt as-is (it's still the count of attempts made) and set
    next_retry_at `cold_retry_backoff_hours` from now — that's what
    due_cold_retries() watches for."""
    conn = _conn()
    hours = get_float("cold_retry_backoff_hours")
    when = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE cold_retries SET next_retry_at = ?, updated_at = ? WHERE lead = ?",
        (when, _now(), str(lead)),
    )
    conn.commit()


def due_cold_retries():
    """Leads whose backoff has elapsed and are ready for their next attempt.
    Each row's last_attempt tells the caller which attempt just failed, so
    the next one to send is last_attempt + 1 (2 or 3)."""
    now = _now()
    rows = _conn().execute(
        "SELECT lead, name, last_attempt FROM cold_retries "
        "WHERE next_retry_at IS NOT NULL AND next_retry_at <= ? "
        "ORDER BY next_retry_at", (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_cold_attempt(lead):
    """Remove a lead from the retry flow entirely — called on a genuine
    delivery confirmation (resolved, no further tracking needed)."""
    conn = _conn()
    conn.execute("DELETE FROM cold_retries WHERE lead = ?", (str(lead),))
    conn.commit()


def note_escalation(lead, cooldown_hours=None):
    """Flag a lead as hot, and answer one question: is the owner due an alert?

    Returns True the first time a lead turns serious, and after that only once
    `cooldown_hours` of quiet have passed. This exists because the agent no
    longer goes silent on escalation — review() runs after every reply, so
    without a cooldown a single enthusiastic conversation would buzz the owner's
    phone on every message and he would very quickly stop reading them.

    Deliberately never touches `muted`. Whether the bot goes quiet is the
    caller's decision, and a lead being hot is not a reason to stop talking.
    """
    if cooldown_hours is None:
        cooldown_hours = get_float("escalation_alert_cooldown_hours")
    row = _state_row(lead)
    due = True
    if row is not None and row["alerted_at"]:
        try:
            last = datetime.strptime(row["alerted_at"], "%Y-%m-%d %H:%M:%S")
            due = (datetime.now() - last).total_seconds() >= cooldown_hours * 3600
        except ValueError:
            due = True                      # unparseable = treat as never alerted

    conn = _conn()
    now = _now()
    if due:
        # A lead the owner already ticked off going hot again IS news, so
        # handled_at is cleared and it reappears in "Needs you".
        conn.execute(
            "INSERT INTO lead_state(lead, muted, escalated_at, last_intent, alerted_at) "
            "VALUES(?, 0, ?, 'serious', ?) "
            "ON CONFLICT(lead) DO UPDATE SET "
            "escalated_at=COALESCE(lead_state.escalated_at, excluded.escalated_at), "
            "last_intent='serious', alerted_at=excluded.alerted_at, handled_at=NULL",
            (str(lead), now, now))
    else:
        conn.execute(
            "INSERT INTO lead_state(lead, muted, escalated_at, last_intent) "
            "VALUES(?, 0, ?, 'serious') "
            "ON CONFLICT(lead) DO UPDATE SET "
            "escalated_at=COALESCE(lead_state.escalated_at, excluded.escalated_at), "
            "last_intent='serious'",
            (str(lead), now))
    conn.commit()
    return due


def mark_handled(lead):
    """Take a lead out of the dashboard's "Needs you" list.

    The escalation history stays — escalated_at is never wiped — this only
    records that the owner has seen it and doesn't need reminding.
    """
    conn = _conn()
    conn.execute("UPDATE lead_state SET handled_at = ? WHERE lead = ?", (_now(), str(lead)))
    conn.commit()


def escalated_leads(limit=50):
    """Feeds the dashboard's escalations inbox.

    Keyed on escalated_at, NOT on muted. The agent stays live through an
    escalation now, so being muted is no longer what makes a lead hot — and if
    this still filtered on muted the inbox would be permanently empty.
    """
    # 'undeliverable', 'not_interested' and 'opted_out' are excluded: set_muted
    # stamps escalated_at whenever it mutes, so a number the delivery-retry flow
    # gave up on, or a lead who asked to STOP, would otherwise land in "Needs you"
    # with a red alert. Neither is something the owner must answer — the owner
    # still gets the one opt-out alert (see escalation.review), but the lead then
    # belongs in Stopped, silently, not in the action inbox.
    rows = _conn().execute(
        "SELECT lead, escalated_at, last_intent, muted FROM lead_state "
        "WHERE escalated_at IS NOT NULL AND handled_at IS NULL "
        "AND (last_intent IS NULL OR last_intent NOT IN ('undeliverable', 'not_interested', 'opted_out')) "
        "ORDER BY escalated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_leads(limit=200):
    """Every conversation with its most recent message — the dashboard's inbox.

    Deliberately one query rather than one per lead: the inner MAX(id) picks each
    lead's newest turn straight off the (lead, id) index, so a client with
    hundreds of conversations still costs a single round-trip. Newest activity
    first, because that is the order the client wants to work in.
    """
    rows = _conn().execute("""
        SELECT t.lead                                            AS lead,
               t.text                                            AS last_text,
               t.role                                            AS last_role,
               t.at                                              AS last_at,
               (SELECT COUNT(*) FROM turns WHERE lead = t.lead)   AS turns,
               COALESCE(s.muted, 0)                              AS muted,
               s.last_intent                                     AS last_intent,
               s.escalated_at                                    AS escalated_at
          FROM turns t
          LEFT JOIN lead_state s ON s.lead = t.lead
         WHERE t.id = (SELECT MAX(id) FROM turns WHERE lead = t.lead)
         ORDER BY t.id DESC
         LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# --- Daily send counters ----------------------------------------------------
# The hard safety net behind cold outreach. WhatsApp bans numbers that blast, so
# we count every send and refuse to go past the client's daily cap even if the
# sheet has hundreds of pending leads.

def record_send(lead, kind):
    conn = _conn()
    conn.execute("INSERT INTO sends(lead, kind, day, at) VALUES(?, ?, ?, ?)",
                 (str(lead), kind, date.today().isoformat(), _now()))
    conn.commit()


def sent_today(kind=None):
    sql = "SELECT COUNT(*) AS n FROM sends WHERE day = ?"
    args = [date.today().isoformat()]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    return _conn().execute(sql, args).fetchone()["n"]


def cold_budget_left():
    """How many more cold messages we're allowed to send today."""
    return max(0, get_int("cold_daily_cap") - sent_today("cold"))


def within_outreach_window(now_utc=None):
    """True if the current moment is inside the client's cold-outreach hours (IST).

    jobs.run_cold_batch and jobs.run_cold_retries gate on this so a lead is never
    cold-messaged in the middle of the night their time. The container runs on
    UTC, so we add the fixed +5:30 IST offset here rather than trusting the box's
    timezone. A window whose start == end is treated as "always on" (off).
    Follow-ups and live replies are NOT gated — those answer people who already
    engaged, at whatever hour they wrote.
    """
    start = get_int("cold_window_start_hour")
    end = get_int("cold_window_end_hour")
    if start == end:
        return True
    ist = (now_utc or datetime.utcnow()) + timedelta(hours=5, minutes=30)
    hour = ist.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window that wraps past midnight


def stats():
    """Small summary for the dashboard header."""
    conn = _conn()
    return {
        "cold_sent_today": sent_today("cold"),
        "followups_sent_today": sent_today("followup"),
        "replies_sent_today": sent_today("reply"),
        "cold_daily_cap": get_int("cold_daily_cap"),
        "cold_budget_left": cold_budget_left(),
        "conversations": conn.execute("SELECT COUNT(DISTINCT lead) AS n FROM turns").fetchone()["n"],
        "escalated_open": conn.execute(
            "SELECT COUNT(*) AS n FROM lead_state "
            "WHERE escalated_at IS NOT NULL AND handled_at IS NULL "
            "AND (last_intent IS NULL OR last_intent NOT IN ('undeliverable', 'not_interested', 'opted_out'))").fetchone()["n"],
    }

"""
escalation.py — hand a serious lead to a real human WITHOUT dropping the thread.

The old version matched keywords in `config.ESCALATION_KEYWORDS`. That fired on
"can you call me a taxi" and missed "we're finalising vendors this week", because
a keyword can't read intent. Now the verdict comes from the same model call that
wrote the reply (brain.py returns {reply, intent}), so it has the whole
conversation in view and costs no extra API call.

Four verdicts, four deterministic actions — the model decides *what this is*,
Python decides *what happens next*:

    serious        -> alert the owner, mark the sheet, and KEEP REPLYING
    not_interested -> stop messaging this person entirely (policy, not politeness)
    time_waster    -> count it; at the limit, stop replying and tell the owner once
    browsing       -> nothing but a note; the bot keeps chatting

"Keep replying" is the important one. Escalation used to mute the agent, on the
theory that a human was about to take over. In practice the owner is on his
personal phone and might be on site, driving, or asleep — and a hot lead left on
read for two hours is a lost lead. So the alert goes out, the agent stays in the
conversation, and the owner steps in whenever he actually can. The client can
still choose silence with the `escalation_mutes_bot` switch.

The cost of staying live is that this function runs again on every subsequent
reply for the same lead, so the owner alert is rate-limited by
`escalation_alert_cooldown_hours` — see store.note_escalation.

Everything here is best-effort and independently wrapped: a failed Sheets write
must never stop the owner's alert.

WhatsApp only. Instagram is a reply-only channel by the client's decision — the
agent answers DMs and comments there and nothing else escalates, so review()
returns early for those leads (see the top of review()). Lead-facing details
still go through channel.py rather than assuming a phone number, because the
switch is one line if that ever changes.
"""
import logging
from datetime import datetime

import channel
import config
import sheets
import store
import whatsapp_client

log = logging.getLogger("whatsapp-relay.escalation")

# Cues that mean the reply already told the lead a human is coming. If it did,
# we don't pile a second "someone will contact you" message on top — double
# texting is the most bot-like thing we could do at the most important moment.
_HANDOFF_CUES = (
    "call", "team", "design lead", "get back", "reach out", "connect",
    "someone will", "colleague", "in touch", "contact you",
)


def _mentions_handoff(reply_text):
    low = (reply_text or "").lower()
    return any(cue in low for cue in _HANDOFF_CUES)


def _clip(text, limit):
    text = " ".join((text or "").split())          # collapse newlines in a turn
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _lead_params(lead, name, incoming_text, turns=6, muted=False, repeat=False):
    """Build the five variables for the approved leads-alert (LEADS_ALERT_TEMPLATE)
    template. The template's fixed text supplies the section headers and their
    order (headline, contact, what-they-said, conversation, what-to-do), so the
    owner still reads the same shape every time — the layout now lives in Meta,
    not in a hand-built string.

    Template variables may not contain newlines, tabs, or >4 consecutive spaces,
    so every value goes through _clip (which collapses whitespace) and the
    multi-turn conversation is flattened onto one line with " / " between turns.
    No value may be empty, hence the "—" fallbacks.

        {{1}} headline   e.g. "HOT LEAD · Priya" / "STILL HOT · Priya"
        {{2}} contact    channel.label — a searchable number or an IG handle
        {{3}} they said  the message that tipped it over
        {{4}} conversation, flattened
        {{5}} what to do + the one tappable link into the right chat
    """
    headline = f"{'STILL HOT' if repeat else 'HOT LEAD'} · {name or 'Unknown'}"

    history = store.history(lead, limit=turns)
    turns_out = []
    for turn in history:
        who = "Us" if turn["role"] == "model" else "Them"
        turns_out.append(f"{who}: {_clip(turn['text'], 140)}")
    conversation = " / ".join(turns_out) if turns_out else "—"

    if muted:
        todo = "The agent has gone quiet on this lead — it is yours now."
    elif repeat:
        todo = ("Told you about this one earlier and nobody has taken it over "
                "yet; the agent is still holding the conversation.")
    else:
        todo = ("The agent is still replying and keeping them warm, so there is "
                "no rush — but sooner is better.")
    # The open link carries no spaces or newlines, so it is already parameter-safe
    # and must NOT be clipped — truncating it would break the tap target.
    todo = f"{_clip(todo, 200)} {channel.open_link(lead)}"

    return [
        _clip(headline, 60),
        _clip(channel.label(lead, name), 80) or "—",
        _clip(incoming_text, 300) or "—",
        _clip(conversation, 700),
        todo,
    ]


def _send_lead_alert(lead, name, incoming_text, muted=False, repeat=False):
    """Send the hot-lead handoff as the approved template so it reaches the owner
    even outside WhatsApp's 24h window — the owner will not message the business
    number every day, and a free-form alert to a closed window is silently
    dropped. Raises on failure so the _safe() wrapper logs it like before."""
    params = _lead_params(lead, name, incoming_text, muted=muted, repeat=repeat)
    whatsapp_client.send_template(config.OWNER_NUMBER, config.LEADS_ALERT_TEMPLATE, params)


def _now_human():
    """Timestamp for {{2}} of the system-alert template. Matches the format used
    by errors.py and watchdog.sh so every operational alert reads the same."""
    return datetime.now().strftime("%d %b %Y, %I:%M %p")


def _send_owner_notice(text):
    """Send an operational owner alert (opt-out / time-waster) as the approved
    system-alert (SYSTEM_ALERT_TEMPLATE) template instead of free-form text.

    WHY THIS EXISTS: these two notices used to go out via jobs.notify_owner,
    which is free-form and therefore only lands inside WhatsApp's 24h window.
    The owner does not message the business number every day, so that window is
    almost always shut and the alert was silently dropped. The client asked to
    be told about opt-outs and time-wasters as well as hot leads, so all three
    now ride an approved template that delivers regardless of the window.

    The template has two body variables: {{1}} the one-line message, {{2}} the
    timestamp. Template variables may not hold newlines/tabs/>4 spaces, so the
    caller's text is collapsed to a single line first; the tappable link inside
    it carries no spaces, so it survives. Raises on failure so _safe() logs it."""
    whatsapp_client.send_template(
        config.OWNER_NUMBER, config.SYSTEM_ALERT_TEMPLATE,
        [_clip(text, 900) or "—", _now_human()])


def _safe(what, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        detail = getattr(getattr(e, "response", None), "text", str(e))
        log.error("%s failed: %s", what, detail)
        return None


def _mark_sheet(lead, status, note):
    """Update the campaign sheet — but only for leads that live in it.

    Instagram ids must never be written there: sheets._find_row matches by
    digits with a suffix comparison, so a 16-digit Instagram id can collide with
    a real phone number, and an id it doesn't match gets APPENDED as a brand-new
    row. Either outcome corrupts the client's tracker.
    """
    if not channel.tracked_in_sheet(lead):
        log.debug("%s is not a sheet lead — skipping the '%s' sheet write",
                  lead, status)
        return None
    return _safe("Sheet mark_status", sheets.mark_status, lead, status, note=note)


def _timewaster_summary(lead, name, incoming_text, strikes):
    """Build the one-line time-waster notice for the system-alert template.

    A different kind of alert from a hot lead: operational, not commercial.
    Nobody needs to ring this person back — it says what happened, what it cost,
    and how to undo it if the agent got it wrong (because it sometimes will, and
    the client needs to know the door isn't locked).

    Returns a SINGLE line: the template's fixed text supplies "Update about your
    service" and the timestamp, so this is just the body. It is sent
    through _send_owner_notice, which collapses any stray whitespace and appends
    the timestamp — the wa.me/IG link stays intact because it has no spaces.
    """
    return (
        f"TIME-WASTER — agent stopped. {name or 'Unknown'} · "
        f"{channel.label(lead, name)}. {strikes} messages in a row with nothing "
        "to do with a project, and every reply costs tokens, so the agent has "
        f"stopped answering this one. They just said: {_clip(incoming_text, 300) or '—'}. "
        "If this is actually a real lead, hand it back to the agent from the "
        f"dashboard and it will pick up where it left off: {channel.open_link(lead)}"
    )


def _optout_summary(lead, name, incoming_text):
    """Build the one-line opt-out notice for the system-alert template.

    Operational, not a lead handoff: the client asked to be TOLD when someone
    stops, but nobody should chase this person — messaging them again is exactly
    what the opt-out forbids. Same single-line shape as the time-waster note; the
    template and _send_owner_notice supply the wrapper and timestamp.
    """
    return (
        f"OPTED OUT — agent stopped. {name or 'Unknown'} · "
        f"{channel.label(lead, name)}. They asked to stop / said they're not "
        "interested, so the agent has stopped messaging them — contacting them "
        "again would breach WhatsApp's rules, so this one is closed automatically. "
        f"They just said: {_clip(incoming_text, 300) or '—'}. If the agent misread "
        "a message and this isn't really an opt-out, hand it back from the "
        f"dashboard: {channel.open_link(lead)}"
    )


def _handle_time_waster(lead, name, incoming_text):
    """Count the strike and, at the limit, stop replying and tell the owner once.

    Runs for EVERY channel, unlike escalation. That is deliberate and it is not a
    contradiction of the reply-only rule for Instagram: this alert is not a lead
    handoff, it is the client's money. An Instagram DM burns exactly the same
    Gemini tokens as a WhatsApp message, so leaving that channel uncapped would
    leave the only real cost of the system uncapped.

    The mute is what actually saves the money — main._dispatch_inbound logs a
    muted lead's messages without paying for a reply. It is a mute and not a
    block on purpose: the owner can hand the lead straight back from the panel if
    the model called it wrong.
    """
    strikes = store.note_off_topic(lead)
    limit = store.get_int("off_topic_strikes_max")
    if limit <= 0:
        log.info("%s off-topic (%d) — cutoff disabled, still replying", lead, strikes)
        return "off_topic"
    if strikes < limit:
        log.info("%s off-topic (%d/%d) — redirected, still replying",
                 lead, strikes, limit)
        return "off_topic"

    store.set_muted(lead, True, intent="time_waster")
    _safe("Time-waster alert", _send_owner_notice,
          _timewaster_summary(lead, name, incoming_text, strikes))
    _mark_sheet(lead, "time_waster",
                f"Cut off after {strikes} off-topic messages")
    log.warning("CUT OFF %s after %d off-topic messages — no further replies", lead, strikes)
    return "cut_off"


def review(lead, name, incoming_text, intent, reply_text=""):
    """Called after every reply is sent. Returns the action taken, for logging
    and for the hermetic test to assert on."""
    lead = str(lead)

    # Before the WhatsApp-only gate below, because token burn is not a
    # channel-specific problem — see _handle_time_waster.
    if intent == "time_waster":
        return _handle_time_waster(lead, name, incoming_text)
    # Any on-topic message forgives the earlier ones. Consecutive is the whole
    # point: a lead who asks something silly and then talks about their flat is a
    # customer, and cutting them off two messages later would cost a real job.
    store.clear_off_topic(lead)

    # WhatsApp only, by the client's decision. Instagram is a REPLY channel: the
    # agent answers DMs and comments there and that is all. No owner alerts, no
    # sheet row, no handoff notice — those all assume a phone number the owner
    # can ring and a row in the phone-number tracker. The verdict is still
    # recorded so the dashboard can badge the conversation.
    if not channel.is_whatsapp(lead):
        store.note_intent(lead, intent or "browsing")
        log.info("%s intent=%s (Instagram — reply-only channel, no escalation)",
                 lead, intent)
        return "none"

    if intent == "serious":
        # Mute only if the client explicitly asked for silence. Default is to
        # stay in the conversation — see the module docstring.
        muted = store.get_bool("escalation_mutes_bot")
        if muted:
            store.set_muted(lead, True, intent="serious")

        # One source of truth for "have we already told him about this lead?".
        # Ask BEFORE the alert, so a Sheets or WhatsApp failure can't put us in a
        # loop where we retry the alert on every single message.
        already_hot = store.is_escalated(lead)
        alert_due = store.note_escalation(lead)

        if alert_due:
            _safe("Owner alert", _send_lead_alert,
                  lead, name, incoming_text, muted=muted, repeat=already_hot)
            _mark_sheet(lead, "escalated",
                        f'Serious lead: "{(incoming_text or "")[:120]}"')
        else:
            log.info("Still-hot lead %s — owner already alerted, staying quiet "
                     "for now (escalation_alert_cooldown_hours)", lead)

        if muted and not _mentions_handoff(reply_text):
            notice = (store.get("handoff_message") or "").strip()
            if notice:
                _safe("Handoff notice", channel.send_text, lead, notice)
        log.info("ESCALATED %s (bot muted=%s, owner alerted=%s)", lead, muted, alert_due)
        return "escalated" if alert_due else "escalated_again"

    if intent == "not_interested":
        # Opt-out. WhatsApp treats continued messaging after a stop signal as a
        # policy breach, and it is the fastest way to get a number blocked — so
        # this mute is unconditional and not client-configurable. The client
        # asked to be told when someone opts out, so the owner gets one alert
        # here; the lead then sits under Stopped (never in "Needs you" — see
        # store.escalated_leads, which excludes this intent). Because the lead is
        # now muted, main._dispatch_inbound stops paying for replies, so review()
        # won't run again for them and this alert fires exactly once.
        store.set_muted(lead, True, intent="not_interested")
        _mark_sheet(lead, "not_interested", "Lead asked to stop / not interested")
        _safe("Opt-out alert", _send_owner_notice,
              _optout_summary(lead, name, incoming_text))
        log.info("OPTED OUT %s — owner alerted, no further messages", lead)
        return "opted_out"

    store.note_intent(lead, intent or "browsing")
    return "none"

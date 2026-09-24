"""Shared outreach logic for both campaign.py (manual) and the scheduler in main.py.

Volume settings are read LIVE from store.py on every pass, so the client can
change the daily cap or the pacing from the dashboard mid-campaign. config.py is
only consulted for things an operator sets once (template names, country code).

Two hard safety rails, both enforced here rather than trusted to the caller:
  * `cold_daily_cap` — every send is counted, and we stop dead when the day's
    budget is spent, no matter how many pending leads the sheet holds. Blasting
    is what gets WhatsApp numbers banned.
  * muted leads are skipped — someone who opted out, or who a human has taken
    over, must never receive automated outreach again.
"""
import logging
import os
import random
import re
import threading
import time

import config
import sheets
import store
import whatsapp_client

log = logging.getLogger("whatsapp-relay.jobs")

# Attempt 3's template. Attempt 2 reuses config.FOLLOWUP_TEMPLATE_NAME (the
# same follow-up template name is already the existing config var, so no new
# .env entry is needed for it). Attempt 3 is new; kept as an env-overridable
# constant here rather than added to config.py, since config.py wasn't touched
# by this change — move it there whenever config.py next gets edited, for
# consistency with the others. The default is a placeholder; a real deployment
# sets the approved template name in .env.
COLD_RETRY_TEMPLATE_3 = os.environ.get("COLD_RETRY_TEMPLATE_3", "dundermifflin_followup3")

# Serialises cold-batch and follow-up runs against themselves. Both can be
# triggered from three places — the scheduler loop, POST /run-campaign, and
# POST /run-followups — with nothing upstream stopping two of them from
# overlapping. Without a lock, two concurrent passes can both read the same
# lead as "pending" before either writes "sent", double-messaging that lead.
# Two separate locks (not one global one) so a slow cold batch can't block a
# follow-up pass that has nothing to do with it.
_cold_lock = threading.Lock()
_followup_lock = threading.Lock()
_retry_lock = threading.Lock()


def normalize_number(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10:
        d = config.DEFAULT_COUNTRY_CODE + d
    return d


def pick_greeting():
    """Kept for a template whose {{1}} is the whole opening phrase. The live intro
    template writes its own greeting, so _params_for no longer calls this."""
    return random.choice(config.GREETINGS)


# Meta rejects a body parameter that is empty, and one containing a newline, a tab,
# or four-plus consecutive spaces. A name coming out of a spreadsheet cell can
# carry all four, and a trailing space is invisible in the sheet while still
# failing the send — so the value is squeezed onto one line on the way out.
NAME_FALLBACK = "there"
NAME_MAX_CHARS = 60


def clean_name(name):
    one_line = " ".join((name or "").split())
    if not one_line:
        return NAME_FALLBACK
    if len(one_line) > NAME_MAX_CHARS:
        one_line = one_line[:NAME_MAX_CHARS].rstrip()
    return one_line


def _params_for(template_name, name):
    """{{1}} is the lead's name off the sheet and nothing else. The template body
    carries the greeting around it, so prepending one here would render
    "Hey Hey Sohan". hello_world takes no parameters at all."""
    if template_name == "hello_world":
        return None
    return [clean_name(name)]


def send_cold_message(lead, dry_run=False):
    to = normalize_number(lead["number"])
    name = lead.get("name") or "there"
    if dry_run:
        log.info("  [dry-run] would cold-message %s -> %s", name, to)
        return "dry-run"
    resp = whatsapp_client.send_template(to, config.CAMPAIGN_TEMPLATE_NAME,
                                         _params_for(config.CAMPAIGN_TEMPLATE_NAME, name))
    msg_id = resp.get("messages", [{}])[0].get("id", "?")
    # NOT sheets.mark_sent() here on purpose: this only confirms Meta ACCEPTED
    # the request, not that it delivered. Real confirmation comes later, async,
    # via the status webhook (see main.py's _handle_send_status) — that's the
    # only place "sent" gets written now. "awaiting_delivery" is what makes the
    # sheet honest in the meantime, and it's also how the status webhook knows
    # this delivered/failed event belongs to a cold attempt it should act on,
    # rather than to an unrelated reply or follow-up.
    sheets.mark_status(lead["number"], "awaiting_delivery", note=f"Attempt 1 submitted ({config.CAMPAIGN_TEMPLATE_NAME})")
    store.note_cold_attempt(lead["number"], name, 1)
    store.record_send(to, "cold")
    log.info("  sent cold -> %s (%s) id=%s", name, to, msg_id)
    return msg_id


def _get_pending_leads_safe():
    """sheets.get_pending_leads() with no error handling of its own — a
    gspread hiccup (Google API rate limit, expired creds, network blip) would
    otherwise propagate straight out of run_cold_batch and, depending on the
    caller, either kill the whole scheduler pass or 500 the /run-campaign
    endpoint. Treat "can't read the sheet right now" the same way we already
    treat "nothing to send": log it and send nothing this pass. The next
    scheduled pass (or the client hitting the button again) tries again."""
    try:
        return sheets.get_pending_leads()
    except Exception as e:
        log.error("Could not read pending leads from Sheets — sending nothing "
                  "this pass: %s", e)
        return None


def _outside_window_reason():
    """None if outreach may go out now, else a short human reason for the log.

    Cold sends, retries and follow-ups are all business-initiated template
    messages the scheduler fires unprompted, so all three honour the client's
    quiet-hours window (store.within_outreach_window) — a 2 AM template is the
    fastest way to read as spam and annoy a real prospect. Live replies are NOT
    gated anywhere: if a lead writes at 11 PM the agent still answers."""
    if store.within_outreach_window():
        return None
    return (f"outside outreach hours "
            f"({store.get_int('cold_window_start_hour'):02d}:00–"
            f"{store.get_int('cold_window_end_hour'):02d}:00 IST)")


def run_cold_batch(dry_run=False, limit=None, respect_window=True):
    if not _cold_lock.acquire(blocking=False):
        log.warning("Cold batch already running — skipping this trigger "
                    "(scheduler and a manual run overlapped)")
        return 0
    try:
        if respect_window and not dry_run:
            reason = _outside_window_reason()
            if reason:
                log.info("Cold batch: %s — sending nothing this pass", reason)
                return 0
        leads = _get_pending_leads_safe()
        if leads is None:
            return 0
        if not leads:
            log.info("Cold batch: no pending leads.")
            return 0

        # The batch size is whichever is smallest: what the caller asked for, the
        # per-pass limit, and what's left of today's cap. The cap is the one that
        # protects the number, so it wins even when the sheet has hundreds waiting.
        allowed = limit or store.get_int("cold_batch_limit")
        if not dry_run:
            budget = store.cold_budget_left()
            if budget <= 0:
                log.info("Cold batch: daily cap spent (%d/%d today) — sending nothing",
                         store.sent_today("cold"), store.get_int("cold_daily_cap"))
                return 0
            allowed = min(allowed, budget)
        leads = leads[:allowed]

        pacing = store.get_int("cold_pacing_seconds")
        log.info("Cold batch: %d to send (cap left today=%s, template=%s, %ss apart)",
                 len(leads), store.cold_budget_left(), config.CAMPAIGN_TEMPLATE_NAME, pacing)
        sent = 0
        for lead in leads:
            if store.is_muted(normalize_number(lead["number"])):
                log.info("  skipping %s — opted out or handled by a human", lead.get("number"))
                continue
            try:
                send_cold_message(lead, dry_run=dry_run)
                sent += 1
            except Exception as e:
                detail = getattr(getattr(e, "response", None), "text", str(e))
                log.error("  cold FAILED %s -> %s: %s", lead.get("name"), lead.get("number"), detail)
            if not dry_run:
                time.sleep(pacing)
        return sent
    finally:
        _cold_lock.release()


def run_cold_retries(dry_run=False, respect_window=True):
    """Attempts 2 and 3 for leads whose backoff has elapsed since their last
    failure. Runs on the same scheduler cadence as everything else (see
    main.py) but is NOT gated by that cadence in any meaningful sense — a
    lead only ever gets picked up here once real wall-clock time
    (cold_retry_backoff_hours) has passed since its last failure, regardless
    of how often this function itself gets called. Calling it more often just
    means the 24h boundary is checked more precisely; it never sends sooner.
    """
    if not store.get_bool("cold_retry_auto"):
        return 0
    if not _retry_lock.acquire(blocking=False):
        log.warning("Cold retry pass already running — skipping this trigger")
        return 0
    try:
        if respect_window and not dry_run:
            reason = _outside_window_reason()
            if reason:
                log.info("Cold retries: %s — leaving queued for next in-hours pass", reason)
                return 0
        try:
            due = store.due_cold_retries()
        except Exception as e:
            log.error("Could not read due cold retries from the DB: %s", e)
            return 0
        if not due:
            return 0

        if not dry_run:
            budget = store.cold_budget_left()
            if budget <= 0:
                log.info("Cold retries: daily cap spent — leaving %d lead(s) queued for next pass",
                         len(due))
                return 0
            due = due[:budget]

        pacing = store.get_int("cold_pacing_seconds")
        log.info("Cold retries: %d due", len(due))
        sent = 0
        for item in due:
            number = item["lead"]
            name = item.get("name") or "there"
            to = normalize_number(number)
            if store.is_muted(to):
                log.info("  skipping retry to %s — opted out or handled by a human", to)
                store.clear_cold_attempt(number)
                continue

            next_attempt = (item.get("last_attempt") or 1) + 1
            if next_attempt == 2:
                template = config.FOLLOWUP_TEMPLATE_NAME
            elif next_attempt == 3:
                template = COLD_RETRY_TEMPLATE_3
            else:
                # Shouldn't happen — due_cold_retries only returns rows with
                # last_attempt 1 or 2 (3 means main.py already gave up and
                # deleted the row). Defensive: give up cleanly rather than
                # sending a 4th attempt Meta was never asked to approve.
                log.error("  %s has last_attempt=%s in cold_retries — giving up "
                         "instead of guessing a template", number, item.get("last_attempt"))
                store.clear_cold_attempt(number)
                try:
                    store.set_muted(to, True, intent="undeliverable")
                    sheets.mark_status(number, "failed_permanent",
                                       note="Retry queue had an unexpected attempt count")
                except Exception as e:
                    log.error("  could not finalise %s: %s", number, e)
                continue

            if dry_run:
                log.info("  [dry-run] would send attempt %d (%s) -> %s (%s)",
                         next_attempt, template, name, to)
                continue
            try:
                whatsapp_client.send_template(to, template, _params_for(template, name))
                sheets.mark_status(number, "awaiting_delivery",
                                   note=f"Attempt {next_attempt} submitted ({template})")
                store.note_cold_attempt(number, name, next_attempt)
                store.record_send(to, "cold")
                log.info("  retry attempt %d -> %s (%s)", next_attempt, name, to)
                sent += 1
            except Exception as e:
                detail = getattr(getattr(e, "response", None), "text", str(e))
                log.error("  retry attempt %d FAILED to submit for %s: %s", next_attempt, number, detail)
                # Submission itself failed (network/auth, not a WhatsApp
                # delivery rejection) — leave the row queued as-is so the next
                # pass tries again, rather than silently dropping the lead.
            time.sleep(pacing)
        return sent
    finally:
        _retry_lock.release()


def run_followups(dry_run=False, respect_window=True):
    if not _followup_lock.acquire(blocking=False):
        log.warning("Follow-up batch already running — skipping this trigger "
                    "(scheduler and a manual run overlapped)")
        return 0
    try:
        if respect_window and not dry_run:
            reason = _outside_window_reason()
            if reason:
                log.info("Follow-ups: %s — sending nothing this pass", reason)
                return 0
        try:
            due, exhausted = sheets.get_followup_candidates(
                store.get_float("followup_after_hours"), store.get_int("followup_max"))
        except Exception as e:
            log.error("Could not read follow-up candidates from Sheets — "
                      "sending nothing this pass: %s", e)
            return 0

        for item in exhausted:
            if dry_run:
                log.info("  [dry-run] would mark no_response: %s", item["number"])
            else:
                try:
                    sheets.mark_status(item["number"], "no_response",
                                       note="No reply after max follow-ups")
                    log.info("  marked no_response: %s", item["number"])
                except Exception as e:
                    log.error("  could not mark %s no_response: %s", item["number"], e)
        if not due:
            return 0
        log.info("Follow-ups: %d due", len(due))
        if config.FOLLOWUP_TEMPLATE_NAME == config.CAMPAIGN_TEMPLATE_NAME:
            log.warning("  FOLLOWUP_TEMPLATE_NAME is unset, so each follow-up re-sends the "
                        "full campaign template (%s) to a lead who already received it — "
                        "set a short follow-up template in .env",
                        config.CAMPAIGN_TEMPLATE_NAME)
        pacing = store.get_int("cold_pacing_seconds")
        sent = 0
        for item in due:
            to = normalize_number(item["number"])
            name = item.get("name") or "there"
            if store.is_muted(to):
                log.info("  skipping follow-up to %s — opted out or handled by a human", to)
                continue
            if dry_run:
                log.info("  [dry-run] would follow up %s -> %s", name, to)
                continue
            try:
                whatsapp_client.send_template(to, config.FOLLOWUP_TEMPLATE_NAME,
                                              _params_for(config.FOLLOWUP_TEMPLATE_NAME, name))
                sheets.increment_followup(item["number"])
                store.record_send(to, "followup")
                log.info("  follow-up -> %s (%s)", name, to)
                sent += 1
            except Exception as e:
                detail = getattr(getattr(e, "response", None), "text", str(e))
                log.error("  follow-up FAILED %s: %s", item["number"], detail)
            time.sleep(pacing)
        return sent
    finally:
        _followup_lock.release()


def detect_escalation(text):
    """Legacy keyword check. The relay no longer uses it — escalation.review acts
    on the model's `intent` instead. Kept as the fallback for the OpenClaw brain
    (see brain._keyword_intent) and for manual scripts."""
    low = (text or "").lower()
    for kw in config.ESCALATION_KEYWORDS:
        if kw in low:
            return True, kw
    return False, None


def notify_owner(message):
    if not config.OWNER_NUMBER:
        return
    try:
        whatsapp_client.send_text(config.OWNER_NUMBER, message)
        log.info("Owner notified.")
    except Exception as e:
        log.warning("Could not notify owner (likely no open 24h window): %s", e)

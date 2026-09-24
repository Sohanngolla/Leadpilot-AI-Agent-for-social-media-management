"""
Relay: Meta webhook -> paced AI reply -> WhatsApp or Instagram, plus sheet
tracking, AI-driven escalation to a human, and an in-process scheduler for cold
outreach + follow-ups.

    uvicorn main:app --host 0.0.0.0 --port 8001 --reload

Two inbound channels, ONE process:

    POST /webhook            WhatsApp Cloud API
    POST /webhook/instagram  Instagram DMs and comments

They deliberately share this process rather than running as two services. The
pacer's worker pool and — more importantly — its `brain_max_per_minute` rate gate
are per-process, so splitting them would hand each channel a full Gemini quota
and put us straight back into the 429s. Sharing also means one persona, one
transcript store, one escalation inbox and one dashboard for both.

The webhook does almost nothing on purpose: it validates, de-duplicates, hands the
message to pacer.py and ACKs Meta in milliseconds. Everything slow (the AI call,
the human-like delay, sending, sheet writes) happens on background threads, so
Meta never times out and never re-delivers.

Runtime behaviour is read LIVE from store.py, not from env: the client can pause
replies, slow them down, or rewrite the persona from the dashboard while this
process keeps running. For a fixed domain in production, drop --reload.
"""
import asyncio
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import FileResponse

import channel
import config
import credentials
import dashboard
import errors
import jobs
import pacer
import sheets
import store

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("whatsapp-relay")


# --- Duplicate protection --------------------------------------------------
# Meta re-delivers webhooks: it retries when our ACK is slow or fails, and
# occasionally sends the same message more than once. Without a guard, every
# re-delivery would fire another OpenClaw/Gemini call AND send a duplicate
# reply — wasted AI tokens and API calls. We remember message IDs we've already
# claimed and skip repeats. In-memory + bounded (an LRU set): a process restart
# clears it, which is fine because Meta's retries land within minutes, and the
# fast ACK below means retries are rare in the first place.
_PROCESSED_IDS: "OrderedDict[str, None]" = OrderedDict()
_PROCESSED_MAX = 1000


def _claim_message(msg_id) -> bool:
    """Record msg_id as handled. Returns True if it was ALREADY seen (caller
    should skip). Called synchronously before any await/background work so a
    concurrent retry is blocked while the first one is still being processed."""
    if not msg_id:
        return False
    if msg_id in _PROCESSED_IDS:
        return True
    _PROCESSED_IDS[msg_id] = None
    if len(_PROCESSED_IDS) > _PROCESSED_MAX:
        _PROCESSED_IDS.popitem(last=False)
    return False


async def _scheduler_loop():
    """Cold outreach + follow-ups on a timer, plus credential upkeep.

    Every setting is re-read each pass from store.py, so flipping automation off
    in the dashboard takes effect within one interval — no restart. The interval
    itself is floored at 30s so a bad value can't turn this into a hot loop.

    The Instagram token refresh runs on every pass regardless of the automation
    switches: it is maintenance, not outreach, and a client who has turned cold
    messaging off still wants their DMs answered next month. It is also the only
    thing here that keeps working while nobody is watching, which is the whole
    point — the token would otherwise die 60 days after the last person touched
    it. Note that SCHEDULER_ENABLED=false disables this too.
    """
    await asyncio.sleep(config.SCHEDULER_STARTUP_DELAY_SECONDS)
    log.info("Scheduler on (cold_auto=%s, followup_auto=%s, every %ss)",
             store.get_bool("cold_auto"), store.get_bool("followup_auto"),
             store.get_int("scheduler_interval_seconds"))
    while True:
        try:
            await asyncio.to_thread(credentials.maintain)
            if store.get_bool("cold_auto"):
                await asyncio.to_thread(jobs.run_cold_batch, False, store.get_int("cold_batch_limit"))
            if store.get_bool("followup_auto"):
                await asyncio.to_thread(jobs.run_followups, False)
            # Independent of cold_auto/followup_auto: this only ever sends
            # when a lead's own 24h+ backoff timer has actually elapsed (see
            # jobs.run_cold_retries), so running it every pass just means the
            # deadline is checked promptly — it never sends sooner than the
            # backoff allows, however often the scheduler itself ticks.
            await asyncio.to_thread(jobs.run_cold_retries, False)
        except Exception as e:
            log.error("Scheduler pass failed: %s", e)
        await asyncio.sleep(max(30, store.get_int("scheduler_interval_seconds")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start capturing warnings/errors into the persistent Doctor Desk before any
    # work begins, so nothing that breaks during startup is missed.
    errors.install()
    task = asyncio.create_task(_scheduler_loop()) if config.SCHEDULER_ENABLED else None
    if task is None:
        log.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan)

# The client's control panel (/dashboard). It lives behind its own shared password
# and closes itself if DASHBOARD_PASSWORD is unset — see dashboard.py.
app.include_router(dashboard.router)

# The Doctor Desk now lives INSIDE /dashboard as two extra views — a plain-language
# "Health" tab for the client and a technical "Developer" tab — rather than a
# separate /desk URL. errors.py is the capture engine behind both (install() above);
# dashboard.py owns the routes and dashboard_view.py the markup. See dashboard.py.


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == config.VERIFY_TOKEN:
        log.info("Webhook verified successfully")
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    log.warning("Webhook verification failed (token mismatch)")
    return Response(status_code=403)


def _record_inbound(sender, name, text):
    """Sheet bookkeeping only — runs off the request path because gspread is slow.

    Escalation is NOT decided here any more. It used to be a keyword match on the
    raw text, which fired on "call me a taxi" and missed "we're finalising vendors
    this week". The verdict now comes from the same model call that writes the
    reply (see escalation.review, driven by brain.py's `intent`), so there is
    exactly one decision-maker and no double owner alerts.

    Instagram leads are skipped: that sheet is the phone-number campaign tracker
    and record_reply APPENDS an unrecognised id as a new row — see
    channel.tracked_in_sheet.
    """
    if not channel.tracked_in_sheet(sender):
        return
    try:
        sheets.record_reply(sender, text, name=name)
    except Exception as e:
        log.error("Sheet record_reply failed: %s", e)


def _dispatch_inbound(lead, text, name=None, message_id=None):
    """Mute check, then queue the paced reply. Shared by all three inbound kinds
    (WhatsApp, Instagram DM, Instagram comment) so the takeover rule can't drift
    apart between channels."""
    if store.is_muted(lead):
        # A human owns this conversation now. Stay silent, but still write the
        # message into the transcript so the owner (and the dashboard) sees the
        # full thread rather than a gap.
        store.append_turn(lead, "user", text)
        log.info("%s is muted (human handling) — logged, not answering", lead)
        return False
    pacer.submit(lead, text, name=name, message_id=message_id)
    return True


def _handle_send_status(status, recipient, errors):
    """A delivery-status callback for a message WE sent. This fires for EVERY
    outbound message — cold sends, retry attempts, follow-ups, and live
    replies alike — with no way to tell from the payload alone which kind
    this was. That's why every branch below checks sheets.get_status(recipient)
    first: we only act on this callback if the lead's sheet currently says
    "awaiting_delivery", meaning WE are the ones actively tracking an
    outstanding cold-flow attempt for them right now. A delivered/failed
    event for an ordinary reply to an escalated or replied lead is real and
    fine, it's just not ours to react to — falling through here leaves their
    sheet status exactly as the conversation earned it.

    "failed" used to unconditionally reset the sheet row to "pending", which
    just re-queued the same dead number every scheduler pass forever. Now it
    drives the three-attempt retry flow in store.py's cold_retries table (see
    jobs.run_cold_retries): a failure queues the NEXT attempt
    `cold_retry_backoff_hours` from now (Meta's own guidance for error 131049,
    the per-recipient marketing-template cap, is to wait 24h+ before
    resending — an immediate retry just fails again). After the 3rd attempt
    fails, the lead is muted and the sheet stamped "failed_permanent" so it
    never resurfaces.
    """
    if not recipient:
        return

    try:
        current = sheets.get_status(recipient)
    except Exception as e:
        log.error("Could not read sheet status for %s — skipping status handling: %s", recipient, e)
        return
    if current != "awaiting_delivery":
        return  # not ours right now — a reply/follow-up delivery, leave it alone

    if status == "failed":
        reason = (errors or [{}])[0].get("title") or (errors or [{}])[0].get("message") or "unknown error"
        last_attempt, _name = store.get_cold_attempt(recipient)
        last_attempt = last_attempt or 1  # defensive: treat an untracked failure as attempt 1

        if last_attempt >= 3:
            try:
                store.set_muted(recipient, True, intent="undeliverable")
            except Exception as e:
                log.error("Could not mute %s after repeated failures: %s", recipient, e)
            try:
                sheets.mark_status(recipient, "failed_permanent",
                                   note=f"Gave up after {last_attempt} failed attempts: {reason}")
            except Exception as e:
                log.error("Could not mark %s failed_permanent: %s", recipient, e)
            store.clear_cold_attempt(recipient)
            log.warning("GAVE UP on %s after %d failed attempts (%s) — muted, no further attempts",
                       recipient, last_attempt, reason)
        else:
            backoff_hours = store.get_float("cold_retry_backoff_hours")
            try:
                store.schedule_cold_retry(recipient)
            except Exception as e:
                log.error("Could not schedule retry for %s: %s", recipient, e)
                return
            try:
                sheets.mark_status(recipient, f"retry_{last_attempt}_queued",
                                   note=f"Attempt {last_attempt} failed ({reason}) — "
                                        f"next attempt in {backoff_hours:.0f}h")
            except Exception as e:
                log.error("Could not update sheet status for %s: %s", recipient, e)
            log.warning("Attempt %d failed for %s (%s) — retry queued in %.0fh",
                       last_attempt, recipient, reason, backoff_hours)

    elif status in ("delivered", "read"):
        # Genuine confirmation. mark_sent() is the ONLY place "sent" gets
        # written now, and only ever after real delivery — not at submission
        # time — which is what makes the sheet (and the dashboard reading it)
        # trustworthy: a lead mid-retry or permanently failed can never show
        # as "sent" by accident.
        try:
            sheets.mark_sent(recipient)
        except Exception as e:
            log.error("Could not mark %s sent: %s", recipient, e)
        store.clear_cold_attempt(recipient)


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    log.debug("Raw webhook: %s", body)
    try:
        value = body["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            # Status callback (sent/delivered/read) about a reply we already
            # sent — informational only, nothing to answer. Log one concise
            # line instead of dumping the whole payload.
            statuses = value.get("statuses")
            if statuses:
                s = statuses[0]
                log.info("Status: %s (id=%s) errors=%s", s.get("status"), s.get("id"), s.get("errors"))
                _handle_send_status(s.get("status"), s.get("recipient_id"), s.get("errors"))
            return Response(status_code=200)
        message = messages[0]
        msg_id = message.get("id")
        sender = message["from"]
        msg_type = message.get("type")
        contacts = value.get("contacts") or []
        name = (contacts[0].get("profile") or {}).get("name") if contacts else None
        if msg_type == "text":
            text = message["text"]["body"]
        elif msg_type == "button":
            # A quick-reply tap on one of our own templates. Meta delivers it as
            # its own message type with the label under a different key, so the
            # old blanket `type != "text"` guard dropped it — and the lead who
            # pressed the button we put in front of them got total silence on the
            # very first message the client ever sends. The intro template
            # carries a HOME button, so this was not hypothetical.
            #
            # The label is what the lead meant to say, so it goes to the brain as
            # if they had typed it. `payload` is the fallback because a button can
            # legally carry a payload that differs from its label.
            btn = message.get("button") or {}
            text = btn.get("text") or btn.get("payload")
            if not text:
                log.info("Button tap carried neither text nor payload — skipping")
                return Response(status_code=200)
        else:
            log.info("Skipping non-text message type: %s", msg_type)
            return Response(status_code=200)
    except (KeyError, IndexError) as e:
        log.warning("Unexpected webhook payload shape: %s (%s)", e, body)
        return Response(status_code=200)

    if _claim_message(msg_id):
        log.info("Duplicate delivery of message %s — skipping (no AI call, no reply)", msg_id)
        return Response(status_code=200)

    # `msg_type` rather than a fixed word, so a button tap is distinguishable
    # from a typed message when reading the log back.
    log.info("Inbound %s from %s (%s): %r", msg_type, sender, name, text)

    _dispatch_inbound(sender, text, name=name, message_id=msg_id)

    # ACK Meta immediately. The reply is already being paced on a worker thread;
    # only the (slow, network-bound) sheet write is deferred to a background task.
    background_tasks.add_task(_record_inbound, sender, name, text)
    return Response(status_code=200)


# --- Instagram --------------------------------------------------------------
# A separate path, not a separate service: see the module docstring. Meta sends
# Instagram events in a different shape from WhatsApp's, and one payload can
# legitimately carry several — a burst of DMs, or a DM and a comment together —
# so this loops over everything instead of reading entry[0] like the WhatsApp
# handler does.
#
# Both loops below are lifted from the standalone ig-relay, where they were
# proven against real Meta traffic. What's new is where they hand off: to
# pacer.submit with a channel-prefixed lead key, instead of to OpenClaw.


@app.get("/webhook/instagram")
async def verify_instagram_webhook(request: Request):
    """Meta's subscription handshake. Its own token, because Instagram may be
    configured in a different Meta app than WhatsApp — config.IG_VERIFY_TOKEN
    falls back to the WhatsApp one when a single app serves both."""
    params = request.query_params
    if (params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == config.IG_VERIFY_TOKEN):
        log.info("Instagram webhook verified successfully")
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    log.warning("Instagram webhook verification failed (token mismatch)")
    return Response(status_code=403)


@app.post("/webhook/instagram")
async def receive_instagram_webhook(request: Request):
    body = await request.json()
    log.debug("Raw Instagram webhook: %s", body)

    for entry in body.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message") or {}
            sender_id = str((event.get("sender") or {}).get("id") or "")

            # Our own outgoing message, echoed back to us. Answering it would
            # start the agent talking to itself.
            if message.get("is_echo") or sender_id == str(config.IG_BUSINESS_ACCOUNT_ID):
                continue
            # Read receipts, reactions, deliveries and attachment-only messages
            # all arrive here with no text. Nothing to reply to.
            text = message.get("text")
            if not sender_id or not text:
                continue

            mid = message.get("mid")
            if _claim_message(mid):
                log.info("Duplicate IG DM %s — skipping (no AI call, no reply)", mid)
                continue

            lead = channel.dm_key(sender_id)
            # Instagram's payload carries no profile name, unlike WhatsApp's, so
            # this is a Graph lookup (cached, and never fatal) — it's what lets
            # the brain greet them by name and the owner alert say "@handle".
            name = await asyncio.to_thread(channel.inbound_name, lead)
            log.info("Inbound IG DM from %s (%s): %r", sender_id, name, text)
            _dispatch_inbound(lead, text, name=name, message_id=mid)

        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            value = change.get("value") or {}
            from_id = str((value.get("from") or {}).get("id") or "")
            text = value.get("text")
            if from_id == str(config.IG_BUSINESS_ACCOUNT_ID) or not text:
                continue

            comment_id = value.get("id")
            if _claim_message(comment_id):
                log.info("Duplicate IG comment %s — skipping", comment_id)
                continue

            # Keyed on the COMMENT, not the commenter: the reply has to go back to
            # /replies on this comment, and a public thread is not the same
            # conversation as that person's DMs.
            lead = channel.comment_key(comment_id)
            name = (value.get("from") or {}).get("username")
            log.info("Inbound IG comment from %s (%s): %r", from_id, name, text)
            _dispatch_inbound(lead, text, name=f"@{name}" if name else None)

    # Meta wants a fast 200 and nothing else; the replies are already in flight.
    return Response(status_code=200)


@app.post("/run-campaign")
async def run_campaign(request: Request, limit: int = None):
    """Manual trigger. Authenticated: this sends real WhatsApp messages to real
    people, so it must not be reachable by anyone who happens to find the tunnel
    URL. Same dashboard session cookie as the panel.

    respect_window=False: a manual "send now" is an explicit human decision, so
    it bypasses the 10am–7pm quiet-hours gate that the automatic scheduler
    honours. The client clicked the button; don't silently do nothing."""
    if not dashboard.authed(request):
        return Response(status_code=401)
    return {"cold_sent": await asyncio.to_thread(jobs.run_cold_batch, False, limit, False)}


@app.post("/run-followups")
async def run_followups_endpoint(request: Request):
    if not dashboard.authed(request):
        return Response(status_code=401)
    # respect_window=False — explicit manual trigger, see /run-campaign.
    return {"followups_sent": await asyncio.to_thread(jobs.run_followups, False, False)}


@app.post("/run-cold-retries")
async def run_cold_retries_endpoint(request: Request):
    """Manual trigger for the delivery-retry flow — mostly for testing that a
    lead queued after a failure actually goes out once its backoff has
    elapsed, without waiting for the scheduler's own interval."""
    if not dashboard.authed(request):
        return Response(status_code=401)
    # respect_window=False — explicit manual trigger, see /run-campaign.
    return {"retries_sent": await asyncio.to_thread(jobs.run_cold_retries, False, False)}


@app.get("/health")
async def health():
    """Liveness for Docker's HEALTHCHECK, plus enough state to debug from a phone."""
    return {"status": "ok", "queue": pacer.status(), "today": store.stats()}


@app.get("/embedded-signup.html")
async def embedded_signup_page():
    return FileResponse("static/embedded_signup.html")

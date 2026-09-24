"""
test_agent.py — hermetic end-to-end test of the whole reply loop.

    python3 test_agent.py

Touches NO network: Meta, Google Sheets and Gemini are all replaced with fakes,
and every sleep is recorded instead of slept. So it runs in under a second and
can be run before any deploy, on any machine, with no credentials.

What it actually proves (each of these was a real risk in the design):
  * an inbound message produces exactly one AI call and one reply
  * Meta's duplicate deliveries don't cost a second AI call or a second reply
  * several messages from one person while we're "thinking" become ONE reply
  * a serious lead alerts the owner, marks the sheet, and silences the bot
  * a lead who opts out is never messaged again, by any code path
  * an AI outage sends the customer nothing and the owner one alert
  * dashboard settings take effect live, with no restart
  * the daily cold cap holds even when the sheet is full of pending leads
  * Instagram DMs and comments go through the same brain, pacing and escalation
    as WhatsApp — and never touch the phone-number Google Sheet
"""
import asyncio
import json as _json
import os
import shutil
import sys
import threading
import time
import types
from urllib.parse import urlencode

TMP = "/tmp/agent-hermetic-test"
shutil.rmtree(TMP, ignore_errors=True)
os.environ.update(
    WHATSAPP_TOKEN="test-token", PHONE_NUMBER_ID="123456", VERIFY_TOKEN="verify-me",
    GEMINI_API_KEY="test-key", OWNER_NUMBER="910000000000",
    AGENT_DB_PATH=os.path.join(TMP, "agent.db"),
    # Instagram is the second inbound channel. A separate verify token on
    # purpose: it can live in a different Meta app than WhatsApp.
    IG_ACCESS_TOKEN="ig-test-token", IG_BUSINESS_ACCOUNT_ID="17800000000000000",
    IG_VERIFY_TOKEN="ig-verify-me",
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{'' if condition else '  <- ' + str(detail)}")


# --- Stub out the three things we refuse to talk to for real ----------------
# fastapi/gspread aren't even installed in CI-like environments, so these go
# into sys.modules BEFORE the app is imported.

fake_fastapi = types.ModuleType("fastapi")


class _Response:
    def __init__(self, content=None, status_code=200, media_type=None, headers=None):
        self.content, self.status_code, self.media_type = content, status_code, media_type
        self.headers = dict(headers or {})


class _BackgroundTasks(list):
    def add_task(self, fn, *args, **kwargs):
        self.append((fn, args, kwargs))

    def run_all(self):
        for fn, args, kwargs in self:
            fn(*args, **kwargs)


class _FastAPI:
    def __init__(self, *a, **k):
        pass

    def _route(self, *a, **k):
        return lambda fn: fn

    get = post = _route

    def include_router(self, router):
        self.router = router


class _APIRouter(_FastAPI):
    pass


fake_fastapi.FastAPI = _FastAPI
fake_fastapi.APIRouter = _APIRouter
fake_fastapi.Response = _Response
fake_fastapi.BackgroundTasks = _BackgroundTasks
fake_fastapi.Request = object
sys.modules["fastapi"] = fake_fastapi

# Fake Sheets: records what would have been written. sheets.py itself needs a
# real Google service account, so it is the one module we substitute wholesale.
fake_sheets = types.ModuleType("sheets")
SHEET_CALLS = []
PENDING_LEADS = []


def _sheet(name):
    def fn(*args, **kwargs):
        SHEET_CALLS.append((name, args, kwargs))
    return fn


fake_sheets.record_reply = _sheet("record_reply")
fake_sheets.mark_status = _sheet("mark_status")
fake_sheets.mark_sent = _sheet("mark_sent")
fake_sheets.increment_followup = _sheet("increment_followup")
fake_sheets.get_pending_leads = lambda: list(PENDING_LEADS)
fake_sheets.get_followup_candidates = lambda hours, mx: ([], [])
sys.modules["sheets"] = fake_sheets

import brain          # noqa: E402
import channel        # noqa: E402
import config         # noqa: E402
import dashboard      # noqa: E402
import escalation     # noqa: E402
import instagram_client  # noqa: E402
import jobs           # noqa: E402
import main           # noqa: E402
import pacer          # noqa: E402
import store          # noqa: E402
import whatsapp_client  # noqa: E402


# --- Fake Meta --------------------------------------------------------------
SENT = []       # every outbound message: (to, body)
SIGNALS = []    # read receipts / typing indicators
# Templates get a second, fuller record. SENT keeps only the name, which is enough
# for "did this lead get messaged" but blind to the two things that actually break
# a live send: the language code and what landed in {{1}}.
TEMPLATES = []  # (to, name, language_code, [body params])


class _HTTP:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {"messages": [{"id": "wamid.fake"}]}
        self.text = _json.dumps(self._payload)
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _meta_post(url, headers=None, json=None, timeout=None):
    body = json or {}
    if body.get("status") == "read":
        SIGNALS.append(("typing" if "typing_indicator" in body else "read", body["message_id"]))
    elif body.get("type") == "text":
        SENT.append((body["to"], body["text"]["body"]))
    else:
        tpl = body["template"]
        TEMPLATES.append((body["to"], tpl["name"], tpl["language"]["code"],
                          [p["text"] for comp in tpl.get("components") or []
                           for p in comp.get("parameters") or []]))
        SENT.append((body["to"], f'[template:{tpl["name"]}]'))
    return _HTTP()


whatsapp_client.requests = types.SimpleNamespace(post=_meta_post, RequestException=Exception)


# --- Fake Instagram ---------------------------------------------------------
# A different host, a different token and different endpoints from WhatsApp, so
# it gets its own fake. Recording the URL matters: a DM must go to
# /{IG_ID}/messages and a comment reply to /{comment_id}/replies, and sending a
# comment reply down the DM endpoint would be invisible to the person who asked.
IG_SENT = []        # (kind, target_id, body)
IG_TOKENS_USED = []  # the access_token each outbound IG call actually carried
IG_PROFILE = {"name": "Sohan", "username": "sohan.arch"}
IG_PROFILE_LOOKUPS = []


def _ig_post(url, headers=None, json=None, timeout=None):
    body = json or {}
    IG_TOKENS_USED.append(body.get("access_token"))
    if url.endswith("/messages"):
        IG_SENT.append(("dm", str(body["recipient"]["id"]), body["message"]["text"]))
    elif url.endswith("/replies"):
        IG_SENT.append(("comment", url.rsplit("/", 2)[-2], body["message"]))
    else:                                        # pragma: no cover - would be a bug
        IG_SENT.append(("unknown", url, body))
    return _HTTP(200, {"message_id": "ig.fake"})


def _ig_get(url, params=None, timeout=None):
    IG_PROFILE_LOOKUPS.append(url.rsplit("/", 1)[-1])
    if IG_PROFILE is None:
        return _HTTP(400, {"error": {"message": "no permission"}})
    return _HTTP(200, dict(IG_PROFILE))


instagram_client.requests = types.SimpleNamespace(
    post=_ig_post, get=_ig_get, RequestException=Exception)


# --- Fake Gemini ------------------------------------------------------------
GEMINI_CALLS = []
GEMINI_MODELS = []      # which model id each call actually went to
GEMINI_TIMEOUTS = []    # what (connect, read) we asked requests for
MODEL_LOOKUPS = []      # every ListModels call — a fallback must not cost one per message
NEXT = {"intent": "browsing", "reply": "We work on homes and offices — what's yours?",
        "status": 200, "mode": None}

# Per-model status, so a test can make ONE model unavailable the way Google does
# ("this model is currently experiencing high demand") while the rest answer.
MODEL_STATUS = {}

# What ListModels returns. Shaped like the real thing, including the entries that
# must never be picked as a stand-in for a chat model.
MODEL_LIST = [
    {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.6-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.6-pro", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-4.0-flash-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-005", "supportedGenerationMethods": ["embedContent"]},
    {"name": "models/imagen-4.0-generate", "supportedGenerationMethods": ["generateContent"]},
]


def _model_of(url):
    return url.rsplit("/models/", 1)[-1].split(":")[0]


def _gemini_post(url, headers=None, json=None, timeout=None):
    GEMINI_CALLS.append(json)
    GEMINI_MODELS.append(_model_of(url))
    GEMINI_TIMEOUTS.append(timeout)
    status = MODEL_STATUS.get(_model_of(url), NEXT["status"])
    if status != 200:
        resp = _HTTP(status, {"error": "forced by test"})
        if NEXT.get("retry_after"):
            # Google often says how long to wait. Honouring it is what lets a
            # single 503 blow the whole deadline, which is worth proving.
            resp.headers["Retry-After"] = str(NEXT["retry_after"])
        return resp
    if NEXT["mode"] == "thought_out":
        # Gemini 3.x reasons before answering and those hidden tokens are
        # charged against maxOutputTokens. When the ceiling is too low the API
        # returns 200 with a candidate that has NO text parts at all.
        return _HTTP(200, {"candidates": [{"content": {"role": "model"},
                                           "finishReason": "MAX_TOKENS"}],
                           "usageMetadata": {"thoughtsTokenCount": 2048}})
    return _HTTP(200, {"candidates": [{"content": {"role": "model", "parts": [
        {"text": _json.dumps({"intent": NEXT["intent"], "reply": NEXT["reply"]})}]},
        "finishReason": "STOP"}]})


def _gemini_get(url, headers=None, timeout=None):
    """ListModels. The fallback model id is discovered, never hard-coded, so this
    is the call that keeps a retired model from becoming a dead fallback."""
    MODEL_LOOKUPS.append(url)
    if MODEL_LIST is None:                     # the lookup itself is down
        return _HTTP(503, {"error": "forced by test"})
    return _HTTP(200, {"models": MODEL_LIST})


brain.requests = types.SimpleNamespace(
    post=_gemini_post, get=_gemini_get, RequestException=Exception)


# --- Time control -----------------------------------------------------------
# The pacer's whole job is waiting. We record the waits instead of serving them,
# so the suite runs instantly and can ASSERT on the delays.
WAITS = []


def _fake_sleep(seconds):
    """Records the wait, and drops a marker into SIGNALS too. That shared
    timeline is the only way to prove the blue ticks land AFTER the pause rather
    than the instant the webhook arrived."""
    WAITS.append(seconds)
    SIGNALS.append(("wait", round(seconds, 1)))


pacer._sleep = _fake_sleep
# brain.py retries with real backoff and enforces a real deadline; skip the
# waiting, keep the retrying, and let the deadline clock run for real so a test
# can prove it fires by setting the budget to nothing.
brain.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=time.monotonic)


class _Req:
    """Stand-in for a Starlette Request carrying a Meta webhook payload."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def inbound(sender, text, msg_id, name="Test Lead"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": name}, "wa_id": sender}],
        "messages": [{"id": msg_id, "from": sender, "type": "text",
                      "text": {"body": text}}],
    }}]}]}


def button_tap(sender, label, msg_id, name="Test Lead", payload=None, bare=False):
    """A quick-reply tap on one of our templates. Meta sends this as type
    `button` with the label under `button.text` — not as text — which is why it
    needs its own builder and its own checks."""
    button = {} if bare else {"text": label, "payload": payload or label}
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": name}, "wa_id": sender}],
        "messages": [{"id": msg_id, "from": sender, "type": "button",
                      "context": {"id": "wamid.OUR_TEMPLATE"}, "button": button}],
    }}]}]}


def deliver(payload):
    """Push a webhook through the real handler and drain the paced queue."""
    tasks = fake_fastapi.BackgroundTasks()
    resp = asyncio.run(main.receive_webhook(_Req(payload), tasks))
    pacer._leads.join()
    tasks.run_all()            # the sheet write FastAPI would have deferred
    return resp


def ig_dm(sender_id, text, mid, echo=False, attachment=False):
    """Instagram's messaging webhook shape — nothing like WhatsApp's."""
    message = {"mid": mid}
    if attachment:
        message["attachments"] = [{"type": "image"}]
    else:
        message["text"] = text
    if echo:
        message["is_echo"] = True
    return {"object": "instagram", "entry": [{
        "id": config.IG_BUSINESS_ACCOUNT_ID,
        "messaging": [{"sender": {"id": sender_id},
                       "recipient": {"id": config.IG_BUSINESS_ACCOUNT_ID},
                       "message": message}],
    }]}


def ig_comment(comment_id, from_id, text, username="curious.one"):
    return {"object": "instagram", "entry": [{
        "id": config.IG_BUSINESS_ACCOUNT_ID,
        "changes": [{"field": "comments", "value": {
            "id": comment_id, "text": text,
            "from": {"id": from_id, "username": username},
            "media": {"id": "17900000000000000"},
        }}],
    }]}


def deliver_ig(payload):
    """Same idea as deliver(), against the Instagram endpoint. No BackgroundTasks:
    Instagram has no sheet write to defer."""
    resp = asyncio.run(main.receive_instagram_webhook(_Req(payload)))
    pacer._leads.join()
    return resp


def reset(**settings):
    SENT.clear(); SIGNALS.clear(); GEMINI_CALLS.clear(); SHEET_CALLS.clear(); WAITS.clear()
    TEMPLATES.clear()
    IG_SENT.clear(); IG_PROFILE_LOOKUPS.clear(); IG_TOKENS_USED.clear()
    GEMINI_MODELS.clear(); GEMINI_TIMEOUTS.clear(); MODEL_LOOKUPS.clear()
    for key, value in settings.items():
        store.set(key, value)


def to_owner():
    return [body for to, body in SENT if to == os.environ["OWNER_NUMBER"]]


def to_lead(number):
    return [body for to, body in SENT if to == number]


def lead_alerts():
    """The hot-lead handoffs, as their template body params. The escalation alert
    now goes out as the approved leads-alert template (LEADS_ALERT_TEMPLATE) — it has to
    reach the owner outside WhatsApp's 24h window), so its content lives in
    TEMPLATES, not in the flat SENT body."""
    return [t[3] for t in TEMPLATES if t[1] == config.LEADS_ALERT_TEMPLATE]


def owner_notices():
    """Operational owner alerts (opt-out, time-waster), as their template body
    params. These now ride the approved system-alert template (SYSTEM_ALERT_TEMPLATE) for
    the same reason as the lead alerts — they must deliver outside the 24h
    window — so each entry is [message, timestamp] and the readable text is
    param[0]."""
    return [t[3] for t in TEMPLATES if t[1] == config.SYSTEM_ALERT_TEMPLATE]


# ===========================================================================
print("\n1. Inbound message -> one AI call, one paced, human-looking reply")
reset(reply_auto="true", typing_indicator="true",
      reply_delay_min_seconds="25", reply_delay_max_seconds="75")
NEXT.update(intent="browsing", reply="We do homes and offices — which is yours?")
resp = deliver(inbound("919111111111", "hi, do you do interiors?", "wamid.1"))
check("webhook ACKs 200 straight away", resp.status_code == 200, resp.status_code)
check("exactly one AI call", len(GEMINI_CALLS) == 1, len(GEMINI_CALLS))
check("exactly one reply sent", len(SENT) == 1, SENT)
check("reply is the AI's text", SENT and SENT[0][1] == NEXT["reply"], SENT)
# Blue ticks the moment a message lands, then a minute of silence, is a bot tell:
# no human reads instantly and answers a minute later. Nothing may be shown to
# the lead until the wait is over.
kinds = [k for k, _ in SIGNALS]
check("the message is left unread while the agent 'hasn't picked up the phone'",
      kinds and kinds[0] == "wait" and "read" not in kinds, SIGNALS)
check("then ticks and typing land together, in one call, after the wait",
      kinds.count("typing") == 1 and kinds[1] == "typing", SIGNALS)
check("waited 25-75s like a person", WAITS and 25 <= WAITS[0] <= 75, WAITS)
check("reply counted against today", store.sent_today("reply") == 1, store.sent_today("reply"))
check("logged to the sheet", any(c[0] == "record_reply" for c in SHEET_CALLS), SHEET_CALLS)

# ===========================================================================
print("\n2. Meta re-delivers the same message (it does this constantly)")
reset()
deliver(inbound("919111111111", "hi, do you do interiors?", "wamid.1"))
check("no second AI call — no wasted tokens", len(GEMINI_CALLS) == 0, GEMINI_CALLS)
check("no duplicate reply to the lead", SENT == [], SENT)

# ===========================================================================
print("\n3. Three quick messages from one person become ONE reply")
reset()
NEXT.update(intent="browsing", reply="Sure — whereabouts is the site?")
hold = threading.Event()


def _gated(seconds):
    WAITS.append(seconds)
    if len(WAITS) == 1:          # park inside the first think-delay
        hold.wait(5)


pacer._sleep = _gated
bg = fake_fastapi.BackgroundTasks()
for i, line in enumerate(["hi", "actually it's a 3BHK", "in Gachibowli"]):
    asyncio.run(main.receive_webhook(_Req(inbound("919222222222", line, f"wamid.1{i}")), bg))
hold.set()
pacer._leads.join()
pacer._sleep = lambda s: WAITS.append(s)
prompt = GEMINI_CALLS[0]["contents"][-1]["parts"][0]["text"] if GEMINI_CALLS else ""
check("still one AI call for the three messages", len(GEMINI_CALLS) == 1, len(GEMINI_CALLS))
check("still one reply", len(SENT) == 1, SENT)
check("all three messages reached the prompt",
      "3BHK" in prompt and "Gachibowli" in prompt, prompt)

# ===========================================================================
print("\n4. A serious lead is handed to the owner")
reset(escalation_mutes_bot="true")
NEXT.update(intent="serious", reply="Let me get our design lead to call you today.")
deliver(inbound("919333333333", "what will a 3BHK cost? can someone call me?",
                "wamid.20", name="Priya"))
alerts = to_owner()
check("owner alerted on WhatsApp", len(alerts) == 1, SENT)
params = lead_alerts()
check("alert carries the name and their own words",
      params and "Priya" in params[-1][0] and "3BHK" in params[-1][2], params)
check("sheet marked escalated",
      any(c[0] == "mark_status" and "escalated" in c[1] for c in SHEET_CALLS), SHEET_CALLS)
check("bot muted so it stops talking over the owner", store.is_muted("919333333333"))
check("lead got one message, not two (reply already offered a call)",
      len(to_lead("919333333333")) == 1, SENT)

# ===========================================================================
print("\n5. The escalated lead messages again while the owner handles it")
reset()
deliver(inbound("919333333333", "ok waiting for the call", "wamid.21"))
check("no AI call", len(GEMINI_CALLS) == 0, GEMINI_CALLS)
check("bot stays silent", SENT == [], SENT)
check("but the message is still in the transcript the owner reads",
      any(t["text"] == "ok waiting for the call" for t in store.transcript("919333333333")))

# ===========================================================================
print("\n6. Someone opts out")
reset()
NEXT.update(intent="not_interested", reply="No problem at all — all the best!")
deliver(inbound("919444444444", "stop messaging me", "wamid.30"))
check("muted permanently", store.is_muted("919444444444"))
check("sheet marked not_interested",
      any(c[0] == "mark_status" and "not_interested" in c[1] for c in SHEET_CALLS), SHEET_CALLS)
# The client asked to be told about opt-outs as well as hot leads, so the owner
# gets exactly one alert here — and it goes out as the system-alert template so
# it lands even when the owner's 24h window is shut.
optout = owner_notices()
check("owner IS pinged once for an opt-out", len(optout) == 1, SENT)
check("the opt-out alert says what happened, in one template line",
      optout and "OPTED OUT" in optout[-1][0] and len(optout[-1]) == 2, optout)

# ===========================================================================
print("\n7. Cold outreach obeys the opt-out and the daily cap")
reset(cold_pacing_seconds="0", cold_batch_limit="10", cold_daily_cap="99")
PENDING_LEADS[:] = [{"name": "OptedOut", "number": "919444444444"},
                    {"name": "Fresh", "number": "919555550001"}]
sent = jobs.run_cold_batch(dry_run=False)
check("opted-out lead is never cold-messaged again", to_lead("919444444444") == [], SENT)
check("the other lead is", sent == 1 and len(to_lead("919555550001")) == 1, (sent, SENT))

reset(cold_pacing_seconds="0", cold_batch_limit="10")
store.set("cold_daily_cap", str(store.sent_today("cold") + 2))
PENDING_LEADS[:] = [{"name": f"L{i}", "number": f"91955555{i:04d}"} for i in range(5)]
check("stops dead at the daily cap even with 5 pending",
      jobs.run_cold_batch(dry_run=False) == 2, len(SENT))
check("next scheduler pass sends nothing", jobs.run_cold_batch(dry_run=False) == 0)

# The approved template writes its own "Hey ... 👋", so {{1}} has to be the bare
# name off the sheet. The code used to prepend a random greeting to it, which
# would have rendered "Hey Hey Latha !" on every first impression the client's
# leads ever get — invisible in the hermetic suite until the payload was checked.
reset(cold_pacing_seconds="0", cold_batch_limit="10", cold_daily_cap="999")
PENDING_LEADS[:] = [{"name": "  Latha \n  Reddy ", "number": "919555561111"},
                    {"name": "", "number": "919555561112"}]
jobs.run_cold_batch(dry_run=False)
check("{{1}} carries the lead's name and nothing else",
      TEMPLATES[0][3] == ["Latha Reddy"], TEMPLATES)
check("a sheet cell's stray newline and double spaces are squeezed out, "
      "since Meta rejects a parameter containing either",
      "\n" not in TEMPLATES[0][3][0] and "  " not in TEMPLATES[0][3][0], TEMPLATES[0][3])
check("a blank name still sends rather than failing on an empty parameter",
      TEMPLATES[1][3] == ["there"], TEMPLATES[1])
check("the send carries the configured language code",
      TEMPLATES[0][2] == config.TEMPLATE_LANGUAGE_CODE, TEMPLATES[0][2])
check("and with nothing in .env that is `en` — `en_US` would 132001 on this template",
      os.environ.get("TEMPLATE_LANGUAGE_CODE") is not None
      or config.TEMPLATE_LANGUAGE_CODE == "en", config.TEMPLATE_LANGUAGE_CODE)
check("hello_world is still sent with no parameters at all",
      jobs._params_for("hello_world", "Latha") is None)
check("a name long enough to wreck the message is clipped",
      len(jobs.clean_name("x" * 200)) == 60, len(jobs.clean_name("x" * 200)))

# ===========================================================================
print("\n8. Gemini is down")
reset()
NEXT["status"] = 500
pacer._last_brain_alert = None
deliver(inbound("919666666666", "hello?", "wamid.40"))
check("customer is sent NOTHING rather than a robot apology",
      to_lead("919666666666") == [], SENT)
check("owner is told a human is needed", len(to_owner()) == 1, to_owner())
deliver(inbound("919666666666", "anyone there?", "wamid.41"))
check("owner is not spammed once per message during an outage",
      len(to_owner()) == 1, to_owner())
NEXT["status"] = 200

# --- The 200 that carries no answer ----------------------------------------
# The nastiest shape of brain failure on Gemini 3.x: HTTP 200, valid JSON, and
# no reply inside it, because the model spent every allowed token on hidden
# reasoning. Silence to the customer is right; silence to us is not.
reset()
NEXT["mode"] = "thought_out"
pacer._last_brain_alert = None
deliver(inbound("919666666667", "send me a quote", "wamid.42"))
check("a 200 with no text is treated as an outage, not as a reply",
      to_lead("919666666667") == [], SENT)
check("the owner is told, so it can't fail silently",
      len(to_owner()) == 1, to_owner())
NEXT["mode"] = None

try:
    brain._parse({"candidates": [{"content": {"role": "model"},
                                  "finishReason": "MAX_TOKENS"}],
                  "usageMetadata": {"thoughtsTokenCount": 2048}})
    why = "no error raised"
except brain.BrainError as e:
    why = str(e)
check("and the error names the token budget, not just 'empty'",
      "MAX_TOKENS" in why and "thinking" in why.lower(), why)

check("the live request leaves room for thinking plus a reply",
      GEMINI_CALLS[-1]["generationConfig"]["maxOutputTokens"] >= 1024
      and GEMINI_CALLS[-1]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low",
      GEMINI_CALLS[-1]["generationConfig"])

# ===========================================================================
print("\n9. The client changes settings from the dashboard, mid-run")
reset(reply_auto="false")
deliver(inbound("919777777777", "hi", "wamid.50"))
check("the off switch actually silences the bot",
      len(GEMINI_CALLS) == 0 and SENT == [], (GEMINI_CALLS, SENT))
reset(reply_auto="true", reply_delay_min_seconds="5", reply_delay_max_seconds="6")
NEXT.update(intent="browsing", reply="Hi! What are you planning?")
deliver(inbound("919777777777", "hi again", "wamid.51"))
check("new delay window applies with no restart", WAITS and 5 <= WAITS[0] <= 6, WAITS)
reset(reply_delay_min_seconds="25", reply_delay_max_seconds="75")

# ===========================================================================
print("\n10. The AI rate cap that stops the 429s")
reset(brain_max_per_minute="2")
gate = pacer._RateGate()
gate.acquire()
gate.acquire()


class _Throttled(Exception):
    pass


def _record_then_stop(seconds):
    WAITS.append(seconds)
    raise _Throttled


WAITS.clear()
pacer._sleep = _record_then_stop
try:
    gate.acquire()
    check("third AI call in a minute is throttled", False, "it went straight through")
except _Throttled:
    check("third AI call in a minute is held ~60s", 55 <= WAITS[0] <= 61, WAITS)
pacer._sleep = lambda s: WAITS.append(s)

# ===========================================================================
# The control panel. It can start outbound campaigns and rewrite the agent's
# prompt, so these tests are about who is allowed to do that — not about markup.
print("\n11. The control panel is shut to everyone but the client")


class _Web:
    """Stand-in for a Starlette Request as the dashboard routes actually use it:
    cookies, query params, an IP, and a urlencoded form body."""

    def __init__(self, cookies=None, form=None, params=None, ip="203.0.113.9"):
        self.cookies = dict(cookies or {})
        self.query_params = dict(params or {})
        self.headers = {}
        self.client = types.SimpleNamespace(host=ip)
        self._body = urlencode(form or {}).encode()

    async def body(self):
        return self._body


def _get(**kw):
    return asyncio.run(dashboard.page(_Web(**kw)))


def _post(route, **kw):
    return asyncio.run(route(_Web(**kw)))


def _body_of(resp):
    return str(getattr(resp, "content", "") or "")


# An unset password must mean "closed", never "open".
config.DASHBOARD_PASSWORD = ""
shut = _get()
check("no password configured -> panel refuses to open, 503",
      shut.status_code == 503, shut.status_code)
check("and no session can be valid while it is unset",
      dashboard.authed(_Web(cookies={dashboard.COOKIE: "anything"})) is False)

config.DASHBOARD_PASSWORD = "correct-horse-battery"

anon = _get()
check("a stranger gets the sign-in page, not the panel",
      anon.status_code == 200 and 'type="password"' in _body_of(anon)
      and 'name="system_prompt"' not in _body_of(anon), anon.status_code)

wrong = _post(dashboard.login, form={"password": "guess"}, ip="198.51.100.1")
check("wrong password is rejected with 401", wrong.status_code == 401, wrong.status_code)
check("and the page doesn't leak the real one",
      "correct-horse-battery" not in _body_of(wrong))

right = _post(dashboard.login, form={"password": "correct-horse-battery"},
              ip="198.51.100.1")
cookie = right.headers.get("set-cookie", "")
check("right password redirects and sets a session cookie",
      right.status_code == 303 and cookie.startswith(dashboard.COOKIE + "="), right.status_code)
check("the cookie is HttpOnly and SameSite",
      "HttpOnly" in cookie and "SameSite=Lax" in cookie, cookie)

# A single shared password behind a public tunnel is guessable at leisure
# without this.
for i in range(dashboard._MAX_FAILS):
    last = _post(dashboard.login, form={"password": f"try{i}"}, ip="198.51.100.66")
check("guessing is allowed up to the limit", last.status_code == 401, last.status_code)
over = _post(dashboard.login, form={"password": "try-again"}, ip="198.51.100.66")
check("then that IP is locked out with 429", over.status_code == 429, over.status_code)
check("lockout is not bypassed by finally guessing right",
      _post(dashboard.login, form={"password": "correct-horse-battery"},
            ip="198.51.100.66").status_code == 429)
check("a different IP is unaffected",
      _post(dashboard.login, form={"password": "correct-horse-battery"},
            ip="198.51.100.77").status_code == 303)
dashboard._fails.clear()

# --- The session token itself ----------------------------------------------
token = dashboard.issue()
check("a freshly issued session is valid", dashboard.valid(token) is True, token)
check("a tampered signature is rejected",
      dashboard.valid(token[:-1] + ("0" if token[-1] != "0" else "1")) is False)
check("a forged expiry is rejected",
      dashboard.valid(f"{int(time.time() + 99999)}.{token.rpartition('.')[2]}") is False)
check("an expired session is rejected",
      dashboard.valid(f"{int(time.time() - 5)}.{dashboard._sign(str(int(time.time() - 5)))}")
      is False)
check("junk is rejected without blowing up",
      not any(dashboard.valid(v) for v in ("", None, "nodot", "abc.def", "..")))
config.DASHBOARD_PASSWORD = "rotated-password"
check("changing the password signs every existing session out",
      dashboard.valid(token) is False)
config.DASHBOARD_PASSWORD = "correct-horse-battery"
check("and restoring it brings them back", dashboard.valid(token) is True)

SESSION = {dashboard.COOKIE: dashboard.issue()}
panel = _get(cookies=SESSION)
check("with a session, the real panel renders",
      panel.status_code == 200 and 'name="system_prompt"' in _body_of(panel)
      and 'action="/dashboard/settings"' in _body_of(panel), panel.status_code)
check("the panel shows a real conversation from the DB",
      "919111111111" in _body_of(panel))

# ===========================================================================
# The panel writes straight into the settings the live agent reads, so a bad
# value here is a banned number or a silent agent. Whitelist, coerce, clamp.
print("\n12. What the panel is allowed to write")


def _settings_form(**over):
    """A full panel submission. Partial is meaningful: an unticked checkbox is
    simply not sent by the browser, so absence has to mean 'off'."""
    form = {"reply_auto": "true", "followup_auto": "true"}
    form.update({k: str(v) for k, v in over.items()})
    return form


dashboard.apply_settings(_settings_form(reply_workers="999",
                                       scheduler_interval_seconds="1"))
check("a wild worker count is clamped to the ceiling, not accepted",
      store.get("reply_workers") == "16", store.get("reply_workers"))
check("a 1-second scheduler interval is floored, not allowed to hot-loop",
      store.get("scheduler_interval_seconds") == "30",
      store.get("scheduler_interval_seconds"))

store.set("cold_daily_cap", "40")
dashboard.apply_settings(_settings_form(cold_daily_cap="lots"))
check("junk in a number field is ignored, old value kept",
      store.get("cold_daily_cap") == "40", store.get("cold_daily_cap"))

dashboard.apply_settings(_settings_form(**{"WHATSAPP_TOKEN": "stolen",
                                           "AGENT_DB_PATH": "/etc/passwd"}))
check("a key that isn't a setting is never written",
      store.get("WHATSAPP_TOKEN") is None and store.get("AGENT_DB_PATH") is None)
dashboard.apply_settings(_settings_form(brain="evil", gemini_model="hacked"))
check("even a real setting that isn't on the panel can't be moved",
      store.get("brain") == "gemini" and store.get("gemini_model") == "gemini-3.6-flash",
      (store.get("brain"), store.get("gemini_model")))

persona = store.get("system_prompt")
dashboard.apply_settings(_settings_form(system_prompt="   "))
check("blanking the persona is refused — an empty brief makes it unpredictable",
      store.get("system_prompt") == persona)
dashboard.apply_settings(_settings_form(system_prompt="You are Ravi. " + "x" * 9000))
check("an enormous persona is truncated, not stored whole",
      len(store.get("system_prompt")) == dashboard.MAX_TEXT,
      len(store.get("system_prompt")))
store.set("system_prompt", persona)

dashboard.apply_settings(_settings_form(typing_indicator="true"))
check("a ticked switch turns the feature on", store.get_bool("typing_indicator"))
dashboard.apply_settings(_settings_form())          # checkbox simply absent
check("an unticked switch turns it off", store.get_bool("typing_indicator") is False)

dashboard.apply_settings(_settings_form(reply_delay_min_seconds="90",
                                       reply_delay_max_seconds="10"))
check("an impossible delay window is made coherent, not left inverted",
      store.get_int("reply_delay_max_seconds") >= store.get_int("reply_delay_min_seconds"),
      (store.get("reply_delay_min_seconds"), store.get("reply_delay_max_seconds")))

store.set("reply_workers", "3")
denied = _post(dashboard.save_settings, form=_settings_form(reply_workers="16"))
check("a stranger's settings POST is turned away, and writes nothing",
      denied.status_code == 303 and store.get("reply_workers") == "3",
      (denied.status_code, store.get("reply_workers")))

# Nothing a caller puts in the URL should reach the page.
hostile = _get(cookies=SESSION, params={"ok": '<script>alert(1)</script>'})
check("an injected flash message is dropped, not rendered",
      "<script>alert(1)</script>" not in _body_of(hostile))
check("a real flash code still shows",
      "Saved" in _body_of(_get(cookies=SESSION, params={"ok": "saved"})))

# ===========================================================================
# These two endpoints send real WhatsApp messages to real people. Before this
# build they took no request object at all and were open to anyone who found
# the tunnel URL.
print("\n13. Nobody can make the agent send messages without signing in")
reset(reply_auto="true", brain_max_per_minute="60", cold_pacing_seconds="0",
      reply_delay_min_seconds="1", reply_delay_max_seconds="2")
pacer._gate._stamps.clear()      # section 10 left the sliding window full

check("POST /run-campaign without a session is 401",
      asyncio.run(main.run_campaign(_Web())).status_code == 401)
check("POST /run-followups without a session is 401",
      asyncio.run(main.run_followups_endpoint(_Web())).status_code == 401)
check("no message went out from either attempt", SENT == [], SENT)

_post(dashboard.mute_lead, form={"lead": "919111111111"})
check("a stranger cannot take over someone's conversation",
      store.is_muted("919111111111") is False)
_post(dashboard.run_campaign_now)
check("a stranger cannot start a campaign", SENT == [], SENT)

# --- Signed in, the client really can take a conversation over --------------
reset(reply_auto="true", brain_max_per_minute="60",
      reply_delay_min_seconds="1", reply_delay_max_seconds="2")
NEXT.update(intent="browsing", reply="Sure — which part of town?")
took = _post(dashboard.mute_lead, cookies=SESSION, form={"lead": "919111111111"})
check("taking a lead over redirects back to that same lead",
      took.status_code == 303 and "lead=919111111111" in took.headers.get("Location", ""),
      took.headers)
check("and the store agrees the human owns it now", store.is_muted("919111111111"))
deliver(inbound("919111111111", "can you call me now?", "wamid.60"))
check("a lead a human owns gets no AI call and no reply",
      len(GEMINI_CALLS) == 0 and SENT == [], (GEMINI_CALLS, SENT))
check("but the message is still logged, so the owner sees the whole thread",
      any(t["text"] == "can you call me now?" for t in store.transcript("919111111111")))

reset(reply_auto="true", brain_max_per_minute="60",
      reply_delay_min_seconds="1", reply_delay_max_seconds="2")
NEXT.update(intent="browsing", reply="Sure — which part of town?")
_post(dashboard.unmute_lead, cookies=SESSION, form={"lead": "919111111111"})
deliver(inbound("919111111111", "hello again", "wamid.61"))
check("handing it back to the agent resumes replies",
      len(to_lead("919111111111")) == 1, SENT)

# --- The Run-now buttons cannot outrun the daily ceiling --------------------
reset(cold_pacing_seconds="0", brain_max_per_minute="60")
store.set("cold_daily_cap", str(store.sent_today("cold")))
PENDING_LEADS[:] = [{"name": "Panel", "number": "919888880003"}]
capped = _post(dashboard.run_campaign_now, cookies=SESSION)
check("Run-now still stops at today's ceiling — the panel is no bypass",
      capped.headers.get("Location") == "/dashboard?ok=cold-0" and SENT == [],
      (capped.headers, SENT))

dashboard.apply_settings(_settings_form(cold_daily_cap=str(store.sent_today("cold") + 5)))
ran = _post(dashboard.run_campaign_now, cookies=SESSION)
check("raising the cap in the panel applies to the very next run, no restart",
      ran.headers.get("Location") == "/dashboard?ok=cold-1"
      and len(to_lead("919888880003")) == 1, (ran.headers, SENT))
check("the follow-up button is reachable once signed in",
      _post(dashboard.run_followups_now, cookies=SESSION).status_code == 303)

config.DASHBOARD_PASSWORD = ""
check("closing the panel re-locks the outbound endpoints too",
      asyncio.run(main.run_campaign(_Web(cookies=SESSION))).status_code == 401)

# ===========================================================================
# The behaviour change the client asked for by name. A hot lead used to silence
# the agent, on the theory that a human was about to take over. In practice the
# owner is on his personal phone and might be on site or driving, so the lead
# sat watching the conversation die at the exact moment they showed interest.
print("\n14. A hot lead is escalated but the agent KEEPS TALKING")
config.DASHBOARD_PASSWORD = "correct-horse-battery"   # section 13 closed the panel
reset(reply_auto="true", brain_max_per_minute="60", escalation_mutes_bot="false",
      escalation_alert_cooldown_hours="6",
      reply_delay_min_seconds="1", reply_delay_max_seconds="2")
pacer._gate._stamps.clear()
check("staying in the conversation is the shipped default",
      store.DEFAULTS["escalation_mutes_bot"] == "false",
      store.DEFAULTS["escalation_mutes_bot"])

HOT = "919777777001"
NEXT.update(intent="serious", reply="I'd love to help — which part of town is it in?")
deliver(inbound(HOT, "am interested", "wamid.70", name="Ravi"))
check("plain 'am interested' escalates — the verdict is the model's, not a keyword's",
      len(to_owner()) == 1, to_owner())
check("the lead still gets their reply", len(to_lead(HOT)) == 1, SENT)
check("and the agent is NOT muted", store.is_muted(HOT) is False)
check("the alert tells the owner the agent is still on it",
      lead_alerts() and "still replying" in lead_alerts()[-1][4], lead_alerts())

# The whole point: the owner is busy and never answers. The lead must not be
# left talking to a wall.
reset()
NEXT.update(intent="serious", reply="Absolutely — mornings or evenings for a call?")
deliver(inbound(HOT, "hello? what's happening", "wamid.71", name="Ravi"))
check("the lead's next message still gets a real answer", len(to_lead(HOT)) == 1, SENT)
check("and the owner is not buzzed again for the same lead", to_owner() == [], to_owner())
check("the lead is waiting in the owner's Needs-you list",
      any(e["lead"] == HOT for e in store.escalated_leads()), store.escalated_leads())
check("and counted in the dashboard header", store.stats()["escalated_open"] >= 1,
      store.stats())
# The panel used to offer "Give back to agent" on every hot lead, which now reads
# as nonsense on a lead the agent never stopped talking to.
panel = _body_of(_get(cookies=SESSION))
check("the panel offers to clear the lead, not to hand back an agent that never left",
      f"+{HOT}" in panel and "Done — clear this" in panel, f"+{HOT} in page: {f'+{HOT}' in panel}")

# Hours later and still nobody has stepped in. THAT is worth a second ping.
reset()
store._conn().execute("UPDATE lead_state SET alerted_at = ? WHERE lead = ?",
                      ("2020-01-01 00:00:00", HOT))
store._conn().commit()
NEXT.update(intent="serious", reply="Sure — I'll have them call you today.")
deliver(inbound(HOT, "can someone call me today?", "wamid.72", name="Ravi"))
check("once the cooldown has passed the owner is reminded",
      len(to_owner()) == 1, to_owner())
check("and the reminder reads STILL HOT, not a brand-new lead",
      lead_alerts() and lead_alerts()[-1][0].startswith("STILL HOT"), lead_alerts())

# The owner deals with it by hand from the panel.
_post(dashboard.unmute_lead, cookies=SESSION, form={"lead": HOT})
check("clearing it takes the lead out of Needs you",
      all(e["lead"] != HOT for e in store.escalated_leads()), store.escalated_leads())
check("clearing it does not gag the agent", store.is_muted(HOT) is False)

# A client who wants silence can still have it.
reset(escalation_mutes_bot="true")
QUIET = "919777777002"
NEXT.update(intent="serious", reply="Let me get our design lead to call you.")
deliver(inbound(QUIET, "send me a quote", "wamid.73", name="Meena"))
check("with the switch on, the agent does go quiet", store.is_muted(QUIET))
check("and the alert says so instead of promising a live agent",
      lead_alerts() and "gone quiet" in lead_alerts()[-1][4], lead_alerts())
# The fixed shape now lives in the approved template — its body prints the same
# sections (who, what they said, the conversation, what to do) in the same order
# every time. Here we prove all five variables are filled and the last one still
# lands the owner in the right chat.
body = lead_alerts()[-1] if lead_alerts() else []
check("the alert fills all five template variables, ending in the tappable link",
      len(body) == 5 and all(p for p in body)
      and body[4].rstrip().endswith(f"wa.me/{QUIET}"), body)

# One thing the switch must never reach.
reset(escalation_mutes_bot="false")
NEXT.update(intent="not_interested", reply="No problem at all — all the best!")
deliver(inbound("919777777003", "stop messaging me", "wamid.74"))
check("an opt-out is still muted unconditionally — that one is WhatsApp policy",
      store.is_muted("919777777003"))

# ===========================================================================
# Instagram is a second transport, not a second agent. The old standalone relay
# asked OpenClaw for the reply and scraped its transcript, which shipped an EMPTY
# message to a real lead. Everything below exists to prove Instagram now runs the
# same brain, the same pacing and the same escalation as WhatsApp — while staying
# out of the phone-number Google Sheet.
print("\n15. Instagram: same brain, same pacing, different transport")
IGSID = "1564850898722146"
IG_LEAD = channel.dm_key(IGSID)
reset(reply_auto="true", typing_indicator="true",
      reply_delay_min_seconds="25", reply_delay_max_seconds="75")
instagram_client._PROFILES.clear()
NEXT.update(intent="browsing", reply="We do homes and offices — which is yours?")
resp = deliver_ig(ig_dm(IGSID, "Hello", "ig.mid.1"))

check("the IG webhook ACKs 200 straight away", resp.status_code == 200, resp.status_code)
check("one AI call, from the same Gemini brain as WhatsApp", len(GEMINI_CALLS) == 1,
      len(GEMINI_CALLS))
check("exactly one DM goes out, to the sender's own id",
      IG_SENT == [("dm", IGSID, NEXT["reply"])], IG_SENT)
check("nothing was sent to WhatsApp by mistake", SENT == [], SENT)
# The whole point of request B3: a two-second reply is the tell that it's a bot.
check("the reply is paced like WhatsApp's, 25-75s",
      len(WAITS) >= 1 and 25 <= WAITS[0] <= 75, WAITS)
# Instagram's API has neither read receipts nor a typing indicator. Calling them
# anyway would be a failed request on every single reply.
check("no read receipt or typing call is attempted on Instagram",
      not [k for k, _ in SIGNALS if k in ("read", "typing")], SIGNALS)
check("the conversation is stored under an ig: key, not a bare number",
      [t["text"] for t in store.history(IG_LEAD)] == ["Hello", NEXT["reply"]],
      store.history(IG_LEAD))
check("a WhatsApp number that happens to match those digits is untouched",
      store.history(IGSID) == [], store.history(IGSID))
# The sheet matches rows by digits with a suffix comparison and APPENDS anything
# it doesn't recognise — a 16-digit id in the Number column is a corrupted tracker.
check("no Google Sheet write for an Instagram lead", SHEET_CALLS == [], SHEET_CALLS)

# Instagram's payload carries no profile name, unlike WhatsApp's. One lookup is
# the difference between "Instagram user 1564850898722146" and "@sohan.arch".
check("the sender's name is resolved once and used to greet them",
      IG_PROFILE_LOOKUPS == [IGSID]
      and "Sohan" in _json.dumps(GEMINI_CALLS[0]), IG_PROFILE_LOOKUPS)
reset()
NEXT.update(reply="Lovely — which city are you in?")
deliver_ig(ig_dm(IGSID, "a 3BHK in Jubilee Hills", "ig.mid.2"))
check("and the lookup is cached, not repeated on every message",
      IG_PROFILE_LOOKUPS == [], IG_PROFILE_LOOKUPS)
check("the second message continues the same thread",
      len(store.history(IG_LEAD, limit=50)) == 4, store.history(IG_LEAD, limit=50))

# --- Things that must NOT produce a reply ----------------------------------
reset()
deliver_ig(ig_dm(IGSID, "Hello", "ig.mid.2"))
check("Meta re-delivering the same mid costs nothing",
      IG_SENT == [] and GEMINI_CALLS == [], (IG_SENT, GEMINI_CALLS))
reset()
deliver_ig(ig_dm(IGSID, NEXT["reply"], "ig.mid.3", echo=True))
check("our own message echoed back does not start a conversation with ourselves",
      IG_SENT == [] and GEMINI_CALLS == [], (IG_SENT, GEMINI_CALLS))
reset()
deliver_ig(ig_dm(IGSID, "", "ig.mid.4", attachment=True))
check("an image with no caption is ignored rather than answered blankly",
      IG_SENT == [] and GEMINI_CALLS == [], (IG_SENT, GEMINI_CALLS))
reset()
deliver_ig(ig_dm(config.IG_BUSINESS_ACCOUNT_ID, "test", "ig.mid.5"))
check("an event whose sender is our own account is ignored",
      IG_SENT == [] and GEMINI_CALLS == [], (IG_SENT, GEMINI_CALLS))
reset()
resp = asyncio.run(main.receive_instagram_webhook(_Req({"entry": [{"id": "x"}]})))
check("a payload with no messaging or changes is a clean 200, not a 500",
      resp.status_code == 200, resp.status_code)

# --- A public comment is not a DM -------------------------------------------
# Keyed on the comment, not the commenter: the reply has to be POSTed to that
# comment's /replies, and a public comment and a private DM from the same person
# are not the same conversation.
COMMENT_ID = "17925000000000001"
COMMENT_LEAD = channel.comment_key(COMMENT_ID)
reset()
NEXT.update(intent="browsing", reply="Sent you a DM with the details!")
deliver_ig(ig_comment(COMMENT_ID, "998877665544", "how much for a 2BHK?"))
check("a comment is answered under the comment, not in the DM inbox",
      IG_SENT == [("comment", COMMENT_ID, NEXT["reply"])], IG_SENT)
check("and it is kept as its own igc: thread",
      len(store.history(COMMENT_LEAD)) == 2, store.history(COMMENT_LEAD))
reset()
deliver_ig(ig_comment(COMMENT_ID, "998877665544", "how much for a 2BHK?"))
check("a re-delivered comment is not answered twice",
      IG_SENT == [] and GEMINI_CALLS == [], (IG_SENT, GEMINI_CALLS))

# --- Instagram is REPLY-ONLY, by the client's decision ----------------------
# No owner alerts and no outbound on Instagram. Both assume a phone number he can
# ring and a row in the phone-number tracker; an Instagram lead has neither.
reset(escalation_mutes_bot="false")
NEXT.update(intent="serious", reply="Our design lead can call you — what suits?")
deliver_ig(ig_dm(IGSID, "send me a quote, urgent", "ig.mid.9"))
check("a hot Instagram lead still gets its reply", len(IG_SENT) == 1, IG_SENT)
check("but the owner is NOT buzzed for Instagram", to_owner() == [], to_owner())
check("and nothing at all is written to the phone-number tracker",
      SHEET_CALLS == [], SHEET_CALLS)
check("the verdict is still recorded, so the panel can badge the conversation",
      any(l["lead"] == IG_LEAD and l["last_intent"] == "serious"
          for l in store.list_leads()), store.list_leads())
check("it never shows up in the owner's Needs-you list",
      all(e["lead"] != IG_LEAD for e in store.escalated_leads()),
      store.escalated_leads())
check("the agent is not silenced on Instagram either",
      store.is_muted(IG_LEAD) is False)
# Cold and follow-up runs both read the sheet, and Instagram is excluded from it,
# so an IG lead cannot physically enter either outbound path.
check("Instagram is excluded from the sheet by design",
      channel.tracked_in_sheet(IG_LEAD) is False
      and channel.tracked_in_sheet("919111111111") is True)

# --- The owner takes an Instagram chat over from the panel -------------------
# The dashboard used to sanitise this field with a digits-only filter, which
# turned "ig:1564..." into "1564..." — the takeover looked like it worked and
# changed nothing, and the thread view came up empty.
check("an ig: key survives sanitising; junk still doesn't",
      channel.sanitise_key("ig:1564850898722146") == IG_LEAD
      and channel.sanitise_key(" igc:17925000000000001 ") == COMMENT_LEAD
      and channel.sanitise_key("+91 91111 11111") == "919111111111"
      and channel.sanitise_key("ig:'; DROP TABLE turns--") == "",
      channel.sanitise_key("ig:'; DROP TABLE turns--"))
took = _post(dashboard.mute_lead, cookies=SESSION, form={"lead": IG_LEAD})
check("taking over an Instagram chat redirects back to that same chat",
      took.status_code == 303 and "ig%3A" in took.headers.get("Location", ""),
      took.headers)
check("and the agent really is muted on the ig: key",
      store.is_muted(IG_LEAD) is True)
reset()
deliver_ig(ig_dm(IGSID, "are you there?", "ig.mid.10"))
check("a muted Instagram lead gets no AI call and no reply",
      IG_SENT == [] and GEMINI_CALLS == [], (IG_SENT, GEMINI_CALLS))
check("but the message is still logged, so the owner sees the whole thread",
      store.history(IG_LEAD, limit=1)[0]["text"] == "are you there?",
      store.history(IG_LEAD, limit=1))
panel = _body_of(_get(cookies=SESSION))
check("the panel names an Instagram lead by handle, not as a fake phone number",
      "@sohan.arch" in panel and f"+{IG_LEAD}" not in panel, "@sohan.arch" in panel)
check("and rendering that list makes no Graph lookups at all",
      IG_PROFILE_LOOKUPS == [], IG_PROFILE_LOOKUPS)
_post(dashboard.unmute_lead, cookies=SESSION, form={"lead": IG_LEAD})
check("handing it back resumes Instagram replies", store.is_muted(IG_LEAD) is False)

# --- Transport limits Instagram does not forgive ----------------------------
reset()
NEXT.update(intent="browsing", reply="A " * 700)              # 1400 characters
deliver_ig(ig_dm(IGSID, "tell me everything", "ig.mid.11"))
check("a reply over Instagram's 1000-char cap is clipped, not rejected outright",
      IG_SENT and len(IG_SENT[0][2]) == 1000 and IG_SENT[0][2].endswith("…"),
      len(IG_SENT[0][2]) if IG_SENT else IG_SENT)

# Meta batches events: two people's DMs can arrive in ONE webhook call. The old
# relay awaited them one after another, so the second lead sat through the first
# one's entire think-delay before the agent even started reading it.
reset(reply_workers="3", reply_delay_min_seconds="1", reply_delay_max_seconds="2")
NEXT.update(reply="One moment — which area is the site in?")
batched = ig_dm("1500000000000001", "hi", "ig.mid.20")
batched["entry"][0]["messaging"] += ig_dm(
    "1500000000000002", "hello?", "ig.mid.21")["entry"][0]["messaging"]
deliver_ig(batched)
check("two DMs in one payload are both answered, not just the first",
      sorted(t for _, t, _ in IG_SENT) == ["1500000000000001", "1500000000000002"],
      IG_SENT)

# --- Meta's subscription handshake ------------------------------------------
ok = asyncio.run(main.verify_instagram_webhook(_Web(params={
    "hub.mode": "subscribe", "hub.verify_token": "ig-verify-me",
    "hub.challenge": "42"})))
check("Instagram's webhook verifies against its own token",
      ok.status_code == 200 and ok.content == "42", (ok.status_code, ok.content))
bad = asyncio.run(main.verify_instagram_webhook(_Web(params={
    "hub.mode": "subscribe", "hub.verify_token": "verify-me",
    "hub.challenge": "42"})))
check("and the WhatsApp token does not open the Instagram webhook",
      bad.status_code == 403, bad.status_code)

# ===========================================================================
# The slow first reply is a feature; a slow TENTH reply is a broken chat. The
# 25-75s pause exists so a stranger's opening message doesn't look
# machine-answered — but a lead who is already typing to us expects an answer at
# conversation speed, and stretching every turn to a minute is what makes people
# give up mid-thread. So the window applies to first contact only, and history is
# what tells the two apart.
print("\n16. First reply waits; the rest of the conversation keeps up")
NEW = "919666000111"
reset(reply_auto="true", reply_delay_min_seconds="25", reply_delay_max_seconds="75",
      reply_delay_followup_seconds="5")
NEXT.update(intent="browsing", reply="Sure — which part of town is the site in?")
deliver(inbound(NEW, "hi, do you do false ceilings?", "wamid.160"))
check("a stranger's first message still waits 25-75s",
      WAITS and 25 <= WAITS[0] <= 75, WAITS)

reset()
deliver(inbound(NEW, "yes, Kondapur", "wamid.161"))
check("the same person's next message is answered in seconds, not a minute",
      WAITS and 3 <= WAITS[0] <= 7, WAITS)
check("and it is a real reply, not a shortcut past the brain",
      len(SENT) == 1 and len(GEMINI_CALLS) == 1, (SENT, GEMINI_CALLS))

reset()
deliver(inbound(NEW, "and the kitchen?", "wamid.162"))
first_follow = WAITS[0] if WAITS else None
reset()
deliver(inbound(NEW, "and lighting?", "wamid.163"))
check("the fast replies are jittered, not a metronome anyone could spot",
      WAITS and WAITS[0] != first_follow, (first_follow, WAITS))

# Instagram gets this free: the rule keys off stored history, not the transport.
reset(reply_delay_followup_seconds="5")
NEXT.update(intent="browsing", reply="Yes — we do full interiors.")
deliver_ig(ig_dm(IGSID, "still there?", "ig.mid.30"))
check("an Instagram thread already in flight also answers in seconds",
      WAITS and 3 <= WAITS[0] <= 7, WAITS)

# A cold template writes no turn, deliberately: someone replying to OUR opening
# message is still meeting the agent for the first time and must not be answered
# in five seconds flat.
reset(cold_pacing_seconds="0", cold_daily_cap="999", cold_batch_limit="10")
PENDING_LEADS[:] = [{"name": "Coldish", "number": "919666000222"}]
jobs.run_cold_batch(dry_run=False)
reset()
NEXT.update(intent="browsing", reply="Happy to help — how big is the space?")
deliver(inbound("919666000222", "who is this?", "wamid.164"))
check("a lead answering our cold template is still a first reply, so it waits",
      WAITS and 25 <= WAITS[0] <= 75, WAITS)

# The client owns the pace, and both numbers are visible on /health.
dashboard.apply_settings(_settings_form(reply_delay_followup_seconds="30"))
check("the follow-up pace is a dashboard knob, clamped like the others",
      store.get_int("reply_delay_followup_seconds") == 30,
      store.get("reply_delay_followup_seconds"))
reset()
deliver(inbound(NEW, "one more thing", "wamid.165"))
check("changing it applies to the very next message, with no restart",
      WAITS and 18 <= WAITS[0] <= 42, WAITS)
store.set("reply_delay_followup_seconds", "5")
check("health reports both speeds, so the pacing is never a guess",
      pacer.status()["follow_up_delay"] == "~5s"
      and pacer.status()["reply_delay"] == "25-75s", pacer.status())

# ===========================================================================
# The failure the client actually hit in production: read timeouts at 45s and
# 503 "This model is currently experiencing high demand", four attempts, then a
# silent lead. Retrying the same overloaded model harder cannot fix a capacity
# problem inside Google, so the answer is a different model and a deadline.
print("\n17. One model goes down, the lead still gets an answer")
PRIMARY = store.get("gemini_model")

reset(reply_auto="true", gemini_fallback_model="")
brain._MODEL_CACHE.update(at=0.0, models=None)
MODEL_STATUS.clear()
MODEL_STATUS[PRIMARY] = 503
NEXT.update(intent="browsing", reply="Yes — we do full interiors. Where's the site?")
deliver(inbound("919555000101", "do you do full interiors?", "wamid.170"))
check("a lead still gets an answer while the primary model is overloaded",
      to_lead("919555000101") == [NEXT["reply"]], SENT)
check("it retried the overloaded model, then handed the work to another",
      GEMINI_MODELS.count(PRIMARY) == 3 and GEMINI_MODELS[-1] != PRIMARY, GEMINI_MODELS)
check("the stand-in came from the API, so a retired id can't become a dead fallback",
      len(MODEL_LOOKUPS) == 1 and GEMINI_MODELS[-1] == "gemini-3.6-flash-lite",
      (MODEL_LOOKUPS, GEMINI_MODELS))
check("an embedding or image model is never picked to hold a conversation",
      not any(m in GEMINI_MODELS for m in ("text-embedding-005", "imagen-4.0-generate")),
      GEMINI_MODELS)

reset()
MODEL_STATUS[PRIMARY] = 503
deliver(inbound("919555000101", "and how long does that take?", "wamid.171"))
check("the model list is cached, so an outage adds no lookup per message",
      MODEL_LOOKUPS == [] and to_lead("919555000101") == [NEXT["reply"]],
      (MODEL_LOOKUPS, SENT))

reset(gemini_fallback_model="gemini-3.6-pro")
MODEL_STATUS[PRIMARY] = 503
deliver(inbound("919555000102", "hi", "wamid.172"))
check("an operator can pin the stand-in, and then nothing is guessed at all",
      GEMINI_MODELS[-1] == "gemini-3.6-pro" and MODEL_LOOKUPS == [],
      (GEMINI_MODELS, MODEL_LOOKUPS))
store.set("gemini_fallback_model", "")

# A wrong key answers the same way on every model, so trying the whole chain
# would just hide the real problem behind three more failures.
reset()
MODEL_STATUS.clear()
NEXT["status"] = 403
pacer._last_brain_alert = None
deliver(inbound("919555000103", "hello?", "wamid.173"))
check("a rejected key fails on the first call instead of touring every model",
      len(GEMINI_MODELS) == 1, GEMINI_MODELS)
check("the customer is still sent nothing, and the owner is told",
      to_lead("919555000103") == [] and len(to_owner()) == 1, SENT)
NEXT["status"] = 200

reset()
MODEL_STATUS.clear()
MODEL_STATUS[PRIMARY] = 404
NEXT.update(intent="browsing", reply="Sure — which area is it in?")
deliver(inbound("919555000104", "do you work in Gachibowli?", "wamid.174"))
check("a model id Google has retired is replaced at once, not retried three times",
      GEMINI_MODELS.count(PRIMARY) == 1 and GEMINI_MODELS[-1] == "gemini-3.6-flash-lite",
      GEMINI_MODELS)
check("and the lead never sees the difference",
      to_lead("919555000104") == [NEXT["reply"]], SENT)

# Loyal retrying is its own failure. On the other side of this call is somebody
# watching a typing bubble Meta drops after 25 seconds.
reset(gemini_deadline_seconds="10")
MODEL_STATUS.clear()
MODEL_STATUS[PRIMARY] = 503
NEXT["retry_after"] = 60
pacer._last_brain_alert = None
deliver(inbound("919555000105", "hi there", "wamid.175"))
check("when Google asks for a minute, we don't keep the lead waiting for it",
      len(GEMINI_MODELS) == 1, GEMINI_MODELS)
check("the owner hears about it instead of the lead being quietly ignored",
      len(to_owner()) == 1, to_owner())
NEXT["retry_after"] = None
store.set("gemini_deadline_seconds", "120")

reset(gemini_timeout_seconds="20")
MODEL_STATUS.clear()
deliver(inbound("919555000106", "hello", "wamid.176"))
check("the AI timeout is a knob, and split so an unreachable host fails fast",
      GEMINI_TIMEOUTS == [(brain.CONNECT_TIMEOUT, 20)], GEMINI_TIMEOUTS)
store.set("gemini_timeout_seconds", "30")

_SAVED_MODEL_LIST = MODEL_LIST
MODEL_LIST = None                      # even the lookup is down
brain._MODEL_CACHE.update(at=0.0, models=None)
reset()
MODEL_STATUS[PRIMARY] = 503
pacer._last_brain_alert = None
deliver(inbound("919555000107", "hi", "wamid.177"))
check("if we can't even ask what models exist, it fails cleanly and tells the owner",
      to_lead("919555000107") == [] and len(to_owner()) == 1, SENT)
MODEL_LIST = _SAVED_MODEL_LIST
brain._MODEL_CACHE.update(at=0.0, models=None)
MODEL_STATUS.clear()
check("and one command answers 'which ids will Google actually accept?'",
      "gemini-3.6-flash" in brain.list_models(), brain.list_models())

# ===========================================================================
# Two client complaints, one section. "It answers a 'hi' with a sales pitch" is a
# prompt problem; "someone chatted to it about cricket for twenty messages" is a
# money problem. Both are checked here because both are judged by the same call.
print("\n18. The agent answers what was asked, and stops paying for nonsense")

brief = brain._system_instruction("Sohan")
check("the shipped prompt tells it to answer the message it was actually sent",
      "ANSWER THE MESSAGE YOU WERE SENT" in brief)
check("a one-word 'hi' is not an opening for a pitch",
      "Do not pitch" in brief)
check("a returning lead is picked up mid-thread, not greeted from scratch",
      "pick the thread up where it stopped" in brief)
check("and everything outside the business is refused, not answered briefly",
      "STAY INSIDE THE BUSINESS" in brief and "who runs a country" in brief)
check("the verdict it must return for that is named in the contract",
      "time_waster" in brief and "time_waster" in brain.INTENTS)

# --- Strikes, not a hair trigger --------------------------------------------
# One silly message is not a time-waster: plenty of real customers open with
# something odd. The cost only becomes worth stopping when it keeps happening.
TROLL = "919555000201"
REDIRECT = "This number is only for Dunder Mifflin project enquiries — what are you looking to get done?"

reset(reply_auto="true", off_topic_strikes_max="3")
NEXT.update(intent="time_waster", reply=REDIRECT, status=200)
deliver(inbound(TROLL, "who is the president of india", "wamid.180", name="Rahul"))
check("the first off-topic message still gets one short redirect",
      to_lead(TROLL) == [REDIRECT], SENT)
check("nobody is woken up over one stray message", to_owner() == [], to_owner())
check("but it is counted", store.off_topic_strikes(TROLL) == 1,
      store.off_topic_strikes(TROLL))
check("and the agent is still in the conversation", store.is_muted(TROLL) is False)

reset()
deliver(inbound(TROLL, "ok then tell me a joke", "wamid.181", name="Rahul"))
check("a second one is redirected again, not escalated",
      to_lead(TROLL) == [REDIRECT] and to_owner() == [], SENT)
check("the count carries across messages", store.off_topic_strikes(TROLL) == 2,
      store.off_topic_strikes(TROLL))

# --- The limit: stop replying, tell the owner once, cost nothing after -------
reset()
deliver(inbound(TROLL, "and one more joke", "wamid.182", name="Rahul"))
alerts = owner_notices()
check("at the limit the agent stops answering this lead", store.is_muted(TROLL))
check("the owner is told once, and told it is about money not a lead",
      len(alerts) == 1 and "TIME-WASTER" in alerts[0][0] and "tokens" in alerts[0][0],
      alerts)
check("the alert says how to undo it, because the verdict can be wrong",
      alerts and "hand it back" in alerts[0][0], alerts)

reset()
deliver(inbound(TROLL, "hello? still there?", "wamid.183", name="Rahul"))
check("the next message from them costs no AI call at all",
      GEMINI_CALLS == [], GEMINI_CALLS)
check("and gets no reply", to_lead(TROLL) == [], SENT)
check("it is still written into the thread, so the owner sees the whole story",
      any("still there" in t["text"] for t in store.history(TROLL)),
      store.history(TROLL, limit=3))

# --- One real sentence forgives the rest ------------------------------------
# Consecutive by design. A lead who jokes once and then talks about their flat is
# a customer, and cutting them off two messages later would cost a real job.
MIXED = "919555000202"
reset()
NEXT.update(intent="time_waster", reply=REDIRECT)
deliver(inbound(MIXED, "do you know the cricket score", "wamid.184"))
check("a stray message from a real lead is counted like any other",
      store.off_topic_strikes(MIXED) == 1, store.off_topic_strikes(MIXED))

reset()
NEXT.update(intent="browsing", reply="Sure — is it a flat or an independent house?")
deliver(inbound(MIXED, "anyway, I need my 2BHK done in Kondapur", "wamid.185"))
check("talking about the project wipes the slate clean",
      store.off_topic_strikes(MIXED) == 0, store.off_topic_strikes(MIXED))
check("and the stale 'time_waster' badge goes with it",
      all(l["last_intent"] != "time_waster"
          for l in store.list_leads() if l["lead"] == MIXED), store.list_leads()[:3])
check("the lead is answered normally", to_lead(MIXED) == [NEXT["reply"]], SENT)

# --- Instagram trolls cost exactly the same money ---------------------------
# Instagram is a reply-only channel: no owner alerts, no sheet row, no handoff.
# This one alert is the exception, and it is not a contradiction — it is not a
# lead being handed over, it is the client's Gemini bill. An IG DM burns the same
# tokens as a WhatsApp message, so leaving the channel uncapped would leave the
# only real running cost of the system uncapped.
IG_TROLL_ID = "1500000000000009"
IG_TROLL = channel.dm_key(IG_TROLL_ID)
reset(off_topic_strikes_max="2")
NEXT.update(intent="time_waster", reply=REDIRECT)
deliver_ig(ig_dm(IG_TROLL_ID, "what's 2+2", "ig.mid.40"))
deliver_ig(ig_dm(IG_TROLL_ID, "no really, what's 2+2", "ig.mid.41"))
check("an Instagram troll is cut off on the same rule as WhatsApp",
      store.is_muted(IG_TROLL), store.off_topic_strikes(IG_TROLL))
check("the owner is told, because this alert is about the bill, not a handoff",
      len(owner_notices()) == 1, owner_notices())
check("and nothing is written to the phone-number sheet for an Instagram id",
      all(c[0] != "mark_status" for c in SHEET_CALLS), SHEET_CALLS)

reset()
deliver_ig(ig_dm(IG_TROLL_ID, "helloooo", "ig.mid.42"))
check("after the cutoff their DMs are free to receive",
      GEMINI_CALLS == [] and IG_SENT == [], (GEMINI_CALLS, IG_SENT))

# --- A client who would rather never cut anyone off can have that ------------
PATIENT = "919555000203"
reset(off_topic_strikes_max="0")
NEXT.update(intent="time_waster", reply=REDIRECT)
deliver(inbound(PATIENT, "tell me about the weather", "wamid.186"))
deliver(inbound(PATIENT, "no, the weather", "wamid.187"))
check("with the cutoff set to zero the agent never goes quiet",
      store.is_muted(PATIENT) is False and to_owner() == [], to_owner())
check("it just keeps redirecting", to_lead(PATIENT) == [REDIRECT, REDIRECT], SENT)

# --- The knobs behind all of this are clamped like every other one -----------
dashboard.apply_settings(_settings_form(off_topic_strikes_max="99",
                                        gemini_timeout_seconds="1"))
check("a runaway strike limit is clamped, not accepted",
      store.get("off_topic_strikes_max") == "20", store.get("off_topic_strikes_max"))
check("and the AI timeout has a floor, so a 1s ceiling can't starve every reply",
      store.get("gemini_timeout_seconds") == "5", store.get("gemini_timeout_seconds"))

# --- Handing a cut-off lead back has to actually work ------------------------
# Without clearing the strikes, an unmuted lead comes back still holding three,
# so the very next message re-silences them and the button looks broken.
import dashboard_view                                                # noqa: E402
check("a cut-off lead reads 'Stopped' in the panel, not 'Yours'",
      "Stopped" in dashboard_view._tag({"last_intent": "time_waster", "muted": True}),
      dashboard_view._tag({"last_intent": "time_waster", "muted": True}))

_post(dashboard.unmute_lead, cookies=SESSION, form={"lead": TROLL})
check("handing them back clears the strikes with the mute",
      store.is_muted(TROLL) is False and store.off_topic_strikes(TROLL) == 0,
      store.off_topic_strikes(TROLL))

reset(reply_auto="true", off_topic_strikes_max="3", typing_indicator="true")
NEXT.update(intent="browsing", reply="Of course — what are you looking to get done?")
deliver(inbound(TROLL, "sorry, I do actually need a quote for my flat", "wamid.188"))
check("and the very next message is answered instead of silently re-muted",
      to_lead(TROLL) == [NEXT["reply"]] and store.is_muted(TROLL) is False, SENT)

# ===========================================================================
print("\n19. The Instagram token renews itself instead of dying after 60 days")

import credentials                                                   # noqa: E402

# Its own fake: credentials.py holds a separate reference to `requests` from
# instagram_client.py, and the refresh is a GET to a different path again.
REFRESH_CALLS = []          # (url, params)
REFRESH_NEXT = {"status": 200, "payload": None, "raise": None}


def _refresh_get(url, params=None, timeout=None):
    REFRESH_CALLS.append((url, dict(params or {})))
    if REFRESH_NEXT["raise"]:
        raise Exception(REFRESH_NEXT["raise"])
    payload = REFRESH_NEXT["payload"]
    if payload is None:
        payload = {"access_token": "ig-token-renewed", "token_type": "bearer",
                   "expires_in": 5183944}
    return _HTTP(REFRESH_NEXT["status"], payload)


credentials.requests = types.SimpleNamespace(get=_refresh_get, RequestException=Exception)

DAY = 86400.0


def _ig_state(**kw):
    """Put the token bookkeeping into a known state. Cleared between sub-tests
    because these keys live in the settings table, which reset() does not wipe."""
    REFRESH_CALLS.clear()
    REFRESH_NEXT["status"] = kw.pop("status", 200)
    REFRESH_NEXT["payload"] = kw.pop("payload", None)
    REFRESH_NEXT["raise"] = kw.pop("raise_", None)
    for key in (credentials.KEY_TOKEN, credentials.KEY_EXPIRES, credentials.KEY_REFRESHED,
                credentials.KEY_CHECKED, credentials.KEY_ERROR, credentials.KEY_ALERTED):
        store.set(key, kw.pop(key, ""))
    assert not kw, f"unused: {kw}"


# --- Where the token comes from ---------------------------------------------
reset()
_ig_state()
check("with nothing stored yet, the token is the one from .env",
      credentials.ig_token() == "ig-test-token", credentials.ig_token())
check("and the dashboard says so, without printing it",
      credentials.status()["source"] == ".env", credentials.status())

store.set(credentials.KEY_TOKEN, "ig-token-renewed")
check("once a fresher token exists, that one wins",
      credentials.ig_token() == "ig-token-renewed", credentials.ig_token())
deliver_ig(ig_dm("1500000000000019", "hi, do you do 2bhk interiors?", "ig.m.19"))
check("and the very next DM is sent with it — no restart involved",
      IG_TOKENS_USED == ["ig-token-renewed"], IG_TOKENS_USED)

# --- The refresh itself ------------------------------------------------------
reset()
_ig_state()
outcome = credentials.maintain()
check("with no expiry on record it refreshes once, to take ownership of the clock",
      outcome == "refreshed", outcome)
check("it asks Meta's documented unversioned endpoint first",
      REFRESH_CALLS and REFRESH_CALLS[0][0] == "https://graph.instagram.com/refresh_access_token",
      REFRESH_CALLS)
check("with the grant type Meta requires for this exchange",
      REFRESH_CALLS[0][1].get("grant_type") == "ig_refresh_token", REFRESH_CALLS[0][1])
check("the new token is stored and used from now on",
      credentials.ig_token() == "ig-token-renewed", credentials.ig_token())
check("Meta's own expires_in sets the deadline, ~60 days out",
      59 <= credentials.days_left() <= 61, credentials.days_left())
check("and the panel now reports it as healthy and self-managed",
      credentials.status()["state"] == "ok"
      and credentials.status()["source"] == "auto-refreshed", credentials.status())

# The token must not be reachable from the browser. Not "hard to find" — absent.
check("the token value appears nowhere in what the dashboard is handed",
      "ig-token-renewed" not in _json.dumps(credentials.status()), credentials.status())
check("and it is not a setting, so no settings page can render it by accident",
      credentials.KEY_TOKEN not in store.all_settings()
      and credentials.KEY_TOKEN not in store.DEFAULTS, credentials.KEY_TOKEN)
_body = _body_of(_get(cookies=SESSION))
check("the rendered panel does not contain it either",
      "ig-token-renewed" not in _body and "ig-test-token" not in _body, len(_body))

# --- It must not thrash -----------------------------------------------------
REFRESH_CALLS.clear()
check("a second pass minutes later does nothing at all",
      credentials.maintain() == "skipped" and REFRESH_CALLS == [], REFRESH_CALLS)

_ig_state(**{credentials.KEY_EXPIRES: str(int(time.time() + 45 * DAY))})
check("with 45 days left it still does nothing — refreshing early wastes the window",
      credentials.maintain() == "skipped" and REFRESH_CALLS == [], REFRESH_CALLS)

_ig_state(**{credentials.KEY_EXPIRES: str(int(time.time() + 12 * DAY))})
check("but inside the last three weeks it renews, with slack to spare",
      credentials.maintain() == "refreshed", REFRESH_CALLS)
check("which pushes the expiry back out to ~60 days",
      59 <= credentials.days_left() <= 61, credentials.days_left())

# --- When it fails ----------------------------------------------------------
# Meta refuses a token younger than 24h. That is not a problem worth waking
# anyone for: it means we just generated one, so there are weeks of runway.
reset()
_ig_state(status=400, payload={"error": {"message": "token is not old enough"}},
          **{credentials.KEY_EXPIRES: str(int(time.time() + 15 * DAY))})
check("a refusal with two weeks left is our retry, not the owner's problem",
      credentials.maintain() == "failed" and to_owner() == [], to_owner())
check("the old token is left alone — it is still valid until its expiry",
      credentials.ig_token() == "ig-test-token", credentials.ig_token())
check("and the reason is kept for whoever looks",
      "not old enough" in credentials.status()["last_error"], credentials.status())
check("both endpoint spellings were tried before giving up",
      len(REFRESH_CALLS) == 2, REFRESH_CALLS)

reset()
_ig_state(status=400, payload={"error": {"message": "Error validating access token"}},
          **{credentials.KEY_EXPIRES: str(int(time.time() + 3 * DAY))})
check("the same failure with 3 days left does reach the owner",
      credentials.maintain() == "failed" and len(to_owner()) == 1, to_owner())
alert = to_owner()[0]
check("the alert names the channel, the deadline and what still works",
      "INSTAGRAM" in alert and "3 day" in alert and "WhatsApp is unaffected" in alert, alert)
check("and it never quotes the token in a WhatsApp message",
      "ig-test-token" not in alert, alert)

SENT.clear()
store.set(credentials.KEY_CHECKED, "")
check("a second failure the same day stays quiet — daily alerts get ignored",
      credentials.maintain() == "failed" and to_owner() == [], to_owner())

# --- The client's view ------------------------------------------------------
import dashboard_view as _dv                                         # noqa: E402
check("a dying token is spelled out on the panel in plain language",
      "3 day" in _dv._connection_notice(credentials.status()) and
      "renews itself" in _dv._connection_notice(credentials.status()),
      _dv._connection_notice(credentials.status()))
check("a healthy one shows nothing — a permanent green badge is just noise",
      _dv._connection_notice({"state": "ok", "days_left": 60}) == "")
check("an expired one says Instagram has stopped, and that WhatsApp has not",
      "WhatsApp is unaffected" in _dv._connection_notice({"state": "expired"}))

# --- The outreach guard rails -----------------------------------------------
# The ceiling still accepts 1000 (see dashboard.RANGES) because a number on a
# higher tier should not need a developer. What keeps a client from wandering
# into a ban is being told, in the moment of typing, what the number costs.
check("a starting cap of 20 a day is offered without a warning",
      _dv.band_for("cold_daily_cap", 20)[0] == "",
      _dv.band_for("cold_daily_cap", 20))
check("50 is still clean — that is the shipped default",
      _dv.band_for("cold_daily_cap", 50)[0] == "")
check("51 starts warning, because the ramp is no longer conservative",
      _dv.band_for("cold_daily_cap", 51)[0] == "warn")
check("250 — Meta's exact limit for an unverified number — warns but saves",
      _dv.band_for("cold_daily_cap", 250)[0] == "warn")
check("251 is the ban spot and is marked as one",
      _dv.band_for("cold_daily_cap", 251)[0] == "stop",
      _dv.band_for("cold_daily_cap", 251))
check("and the sentence tells the client what actually happens, not just 'careful'",
      "restricted" in _dv.band_for("cold_daily_cap", 500)[1],
      _dv.band_for("cold_daily_cap", 500)[1])
check("sending with no gap at all warns, since a burst is the loudest spam signal",
      _dv.band_for("cold_pacing_seconds", 0)[0] == "warn")
check("the 45-second default does not nag",
      _dv.band_for("cold_pacing_seconds", 45)[0] == "")
check("fields with nothing to say about them stay silent",
      _dv.band_for("followup_max", 99) == ("", ""))
check("a value that isn't a number can't break the page",
      _dv.band_for("cold_daily_cap", "lots") == ("", ""))

_risky = _dv._num_rows({"cold_daily_cap": {"value": "500"}}, _dv.OUTREACH)
check("the risky row is rendered red server-side, so it shows with JavaScript off",
      'class="row banded lvl-stop"' in _risky and "note stop" in _risky, _risky[:300])
check("its bands travel with the field so typing updates the verdict live",
      "data-bands=" in _risky and "&quot;stop&quot;" in _risky)
check("a missing setting renders a plain row instead of raising",
      'name="cold_pacing_seconds"' in _risky)
_safe = _dv._num_rows({"cold_daily_cap": {"value": "40"}}, _dv.OUTREACH)
check("a sensible cap renders with nothing flagged",
      "lvl-stop" not in _safe and "lvl-warn" not in _safe, _safe[:300])
check("and the confirm box lives in the one guard script, not in every row",
      "window.confirm" in _dv._GUARDS and "window.confirm" not in _risky)

# --- Nothing here may take the scheduler down -------------------------------
reset()
_ig_state(raise_="connection reset by peer")
check("a network explosion during refresh is caught, not raised",
      credentials.maintain() == "failed", "maintain() raised")
check("and it is recorded as a network problem",
      "network error" in credentials.status()["last_error"], credentials.status())

reset()
_ig_state()
_saved_env = config.IG_ACCESS_TOKEN
config.IG_ACCESS_TOKEN = ""
check("with Instagram not set up at all, the job is a no-op rather than an error",
      credentials.maintain() == "skipped", credentials.maintain())
check("and the panel says it is simply not connected",
      credentials.status()["state"] == "missing", credentials.status())
check("which the client reads as Instagram off, WhatsApp fine",
      "not connected" in _dv._connection_notice(credentials.status()),
      _dv._connection_notice(credentials.status()))
config.IG_ACCESS_TOKEN = _saved_env
_ig_state()

# ===========================================================================
print("\n20. Tapping a button on our own template is answered, not ignored")
# The full intro template — the first thing every cold lead receives — carries a
# HOME quick-reply. Meta delivers a tap as type `button`, with the label under
# `button.text` rather than `text.body`, and the handler used to drop everything
# that was not type `text`. So the single most likely response to the client's
# opening message was met with total silence. Confirmed against Meta's own copy
# of the template, via preflight.py, before this was fixed.
reset(reply_auto="true")
NEXT.update(intent="browsing", reply="Homes it is — which part of Hyderabad?")
resp = deliver(button_tap("919777777771", "HOME", "wamid.b1"))
check("webhook still ACKs 200", resp.status_code == 200, resp.status_code)
check("a button tap reaches the brain", len(GEMINI_CALLS) == 1, len(GEMINI_CALLS))
check("the label is what the brain is asked about",
      "HOME" in _json.dumps(GEMINI_CALLS), GEMINI_CALLS[:1])
check("and the lead gets a real reply",
      len(SENT) == 1 and SENT[0][1] == NEXT["reply"], SENT)

reset(reply_auto="true")
NEXT.update(intent="browsing", reply="ok")
deliver(button_tap("919777777772", "HOME", "wamid.b2", payload="HOME_FLOW_V3"))
check("the label the lead saw wins over an internal payload",
      "HOME" in _json.dumps(GEMINI_CALLS)
      and "HOME_FLOW_V3" not in _json.dumps(GEMINI_CALLS), GEMINI_CALLS[:1])

reset(reply_auto="true")
deliver(button_tap("919777777773", "HOME", "wamid.b3", bare=True))
check("a tap carrying neither label nor payload is dropped, not crashed on",
      len(GEMINI_CALLS) == 0 and SENT == [], (GEMINI_CALLS, SENT))

# The takeover rule has to hold for taps too, or a human mid-conversation gets
# talked over the moment the lead presses something.
reset(reply_auto="true")
store.set_muted("919777777774", True)
deliver(button_tap("919777777774", "HOME", "wamid.b4"))
check("a muted lead's tap stays silent", SENT == [], SENT)
check("but the tap is still in the transcript for the human to read",
      any(t["text"] == "HOME" for t in store.transcript("919777777774")),
      store.transcript("919777777774"))

# Opening up `button` must not have opened up everything else: an image or a
# voice note still carries nothing the brain can read.
reset(reply_auto="true")
deliver({"entry": [{"changes": [{"value": {
    "contacts": [{"profile": {"name": "Test Lead"}, "wa_id": "919777777775"}],
    "messages": [{"id": "wamid.b5", "from": "919777777775", "type": "image",
                  "image": {"id": "1234"}}]}}]}]})
check("an image is still skipped",
      len(GEMINI_CALLS) == 0 and SENT == [], (GEMINI_CALLS, SENT))

reset(reply_auto="true")
NEXT.update(intent="browsing", reply="ok")
deliver(button_tap("919777777776", "HOME", "wamid.b6"))
deliver(button_tap("919777777776", "HOME", "wamid.b6"))
check("a re-delivered tap does not answer twice", len(SENT) == 1, SENT)

shutil.rmtree(TMP, ignore_errors=True)
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
print("ALL HERMETIC TESTS PASSED — this build is safe to deploy.")

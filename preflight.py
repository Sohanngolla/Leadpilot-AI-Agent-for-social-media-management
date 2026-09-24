"""Round 1: every live credential checked, read-only. This sends nothing.

Nothing here messages a lead, writes to the sheet, or changes a setting — so it
is safe against production, and it is the one round worth re-running after the
docker compose migration and again after the handover switches.

    python preflight.py

After the docker compose migration this has to run INSIDE the container
(`docker compose exec agent python preflight.py`), because data/agent.db is owned
by UID 10001 and the settings read would otherwise fail on permissions.

Secrets are never printed. Only whether a value is present, how long it is, and
what the far end said about it.

The one call that is not free is the single Gemini generate at the bottom, and
that is deliberate: `list_models` succeeds on the free tier too, so listing
models proves the key is real and proves nothing at all about billing. Read the
comment above that call before trusting its verdict — a 200 there does not prove
billing either, only that quota was left at the time. It costs a fraction of a
paisa.
"""
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES = []


def ok(label, detail=""):
    print(f"  OK    {label}" + (f"  — {detail}" if detail else ""))


def bad(label, detail=""):
    FAILURES.append(label)
    print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def warn(label, detail=""):
    print(f"  warn  {label}" + (f"  — {detail}" if detail else ""))


print("\n1. Environment (presence and length only — no values printed)")

# DASHBOARD_PASSWORD is in here rather than in the optional list because the
# panel serves 503 without it by design, and the panel is the only way the client
# can pause the agent.
REQUIRED = ["WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "VERIFY_TOKEN", "GEMINI_API_KEY",
            "DASHBOARD_PASSWORD", "OWNER_NUMBER",
            "GOOGLE_CREDENTIALS_FILE", "GOOGLE_SHEET_ID"]
for key in REQUIRED:
    value = os.environ.get(key, "")
    if value:
        ok(f"{key} set", f"{len(value)} chars")
    else:
        bad(f"{key} missing or empty")

for key in ["IG_ACCESS_TOKEN", "IG_BUSINESS_ACCOUNT_ID", "FOLLOWUP_TEMPLATE_NAME"]:
    value = os.environ.get(key, "")
    ok(f"{key} set", f"{len(value)} chars") if value else warn(f"{key} not set")

creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "")
if creds_file and not os.path.exists(creds_file):
    bad("GOOGLE_CREDENTIALS_FILE points at a file that isn't there", creds_file)

import requests  # noqa: E402


def _load(name):
    """Import a project module without letting one missing dependency kill the
    whole run.

    requirements.txt shipped for a while without gspread and google-auth even
    though sheets.py imports both (requirements.docker.txt documents this), so a
    venv built from it works perfectly until the exact moment it reaches the
    sheet. One FAIL line there is worth far more than a traceback that hides
    every check below it.
    """
    try:
        return __import__(name)
    except Exception as e:
        bad(f"cannot import {name}.py", f"{type(e).__name__}: {e}")
        return None


config = _load("config")
if config is None:
    print("\nconfig.py itself will not load, and every check below reads from it "
          "— normally a key missing from .env. Fix that first.")
    sys.exit(1)
store = _load("store")
brain = _load("brain")
credentials = _load("credentials")
# sheets is deliberately NOT loaded here — section 5 imports it itself so a
# missing gspread costs one line there instead of two.

print("\n2. WhatsApp token and the template that reaches the lead first")

# The template's own ID, not the WABA's. Reading it by ID needs no extra
# permission and no WABA id in .env, and the payload carries the two fields that
# silently break every send: `language` and the body's variable count.
TEMPLATE_ID = os.environ.get("CAMPAIGN_TEMPLATE_ID", "1420649816826429")
try:
    r = requests.get(f"{config.GRAPH_API_BASE}/{TEMPLATE_ID}",
                     params={"fields": "name,language,status,category,components"},
                     headers={"Authorization": f"Bearer {config.WHATSAPP_TOKEN}"},
                     timeout=15)
    if r.status_code != 200:
        bad(f"Graph refused the template read ({r.status_code})", r.text[:300])
    else:
        t = r.json()
        ok("WhatsApp token accepted by Graph", f"read template {t.get('name')!r}")

        if t.get("status") == "APPROVED":
            ok("template approved", t.get("category"))
        else:
            bad("template is not APPROVED — sends will fail", t.get("status"))

        if t.get("language") == config.TEMPLATE_LANGUAGE_CODE:
            ok("language matches what the code sends", t.get("language"))
        else:
            bad("language mismatch — every send fails with 132001",
                f"Meta stores {t.get('language')!r}, we send "
                f"{config.TEMPLATE_LANGUAGE_CODE!r}")

        if t.get("name") == config.CAMPAIGN_TEMPLATE_NAME:
            ok("name matches CAMPAIGN_TEMPLATE_NAME", t.get("name"))
        else:
            bad("we are configured to send a different template",
                f"this ID is {t.get('name')!r}, config sends "
                f"{config.CAMPAIGN_TEMPLATE_NAME!r}")

        comps = {(c.get("type") or "").upper(): c for c in t.get("components") or []}
        body = (comps.get("BODY") or {}).get("text") or ""
        variables = sorted(set(re.findall(r"\{\{(\w+)\}\}", body)))
        if variables == ["1"]:
            ok("body takes exactly one numbered variable", "{{1}} = the lead's name")
        elif not variables:
            bad("body has no variable, but the code always sends one parameter")
        else:
            bad("body variables are not a single {{1}}",
                f"found {variables} — named variables and extra numbers both "
                f"fail, because send_template sends one positional parameter")

        header = comps.get("HEADER")
        if header and "{{" in (header.get("text") or ""):
            bad("the header contains a variable",
                "send_template builds no header component, so Meta rejects the send")
        elif header:
            ok("header is static", header.get("format"))

        buttons = (comps.get("BUTTONS") or {}).get("buttons") or []
        quick = [b.get("text") for b in buttons if (b.get("type") or "").upper() == "QUICK_REPLY"]
        if quick:
            ok(f"quick-reply buttons {quick} are handled",
               "taps arrive as type 'button' and main.py routes them to the brain")
        elif buttons:
            ok("buttons are static", [b.get("type") for b in buttons])
except Exception as e:
    bad("template read-back failed", e)

print("\n3. Gemini — the only paid call in this script")

model = store.get("gemini_model") if store else "gemini-3.6-flash"
try:
    models = brain.list_models()
    if model in models:
        ok("key works and the configured model is available", model)
    else:
        bad("the configured model is not in this account's list",
            f"{model!r} missing from {len(models)} models — the gemini_model "
            f"setting needs to change, not the key. It is not on the dashboard "
            f"form, so: docker compose exec agent python -c \"import store; "
            f"store.set('gemini_model', 'THE_NEW_ID')\"")
except Exception as e:
    bad("could not list models", e)

# The tier check — and read the comment before trusting a green line here.
#
# A 200 does NOT prove billing. It proves only that the project had quota left
# when this ran, and the FREE tier grants a daily allowance (20 requests/day per
# model on gemini-3.6-flash), so an untouched free project answers 200 quite
# happily for its first twenty calls of the day. This script claimed "billing is
# live" off exactly that and was wrong on 2026-09-05.
#
# The only proof available from the API is the quota metric, and it appears only
# in a 429: `generate_content_free_tier_requests` with a quotaId ending
# `-FreeTier` means no billing account is attached to the project that owns this
# key. So: 429 is diagnostic and 200 is merely encouraging.
try:
    r = requests.post(
        brain.GEMINI_URL.format(model=model),
        headers={"x-goog-api-key": config.GEMINI_API_KEY,
                 "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": "Reply with one word: ready"}]}]},
        timeout=30)
    if r.status_code == 200:
        ok("one real generate succeeded", "there is quota left right now")
        warn("this does NOT prove billing is attached",
             "the free tier also answers 200 — 20/day/model. Proof is either a "
             "429 naming a paid-tier metric, or the billing page for the Cloud "
             "project this key belongs to (aistudio.google.com/apikey names it)")
    elif r.status_code == 429:
        # brain._quota_id pulls the one field that separates a per-minute burst
        # (harmless, the retry clears it) from a spent day, and free tier from
        # paid. Without it the message body is the same sentence either way.
        quota = brain._quota_id(r) or "unknown quota"
        if "FreeTier" in quota or "free_tier" in quota:
            bad("429 on the FREE tier — billing is not attached to this project",
                f"{quota}: the daily free allowance is spent. Every reply the "
                f"agent owes a lead today will fail. Attach billing to the "
                f"project that owns this key, or issue a key inside the project "
                f"that is already billed, and restart the container")
        elif "PerDay" in quota:
            bad("429 on a per-DAY quota", f"{quota} — no more replies today")
        else:
            warn("429 on a per-minute burst limit",
                 f"{quota} — this one clears itself; brain.py retries and then "
                 f"falls back to another model")
    else:
        bad(f"generate returned {r.status_code}", r.text[:300])
except Exception as e:
    bad("generate call failed", e)

print("\n4. Instagram token")

try:
    st = credentials.status()
    left = credentials.days_left()
    state = st.get("state")
    detail = f"source={st.get('source')}" + (f", {left:.0f} days left" if left else "")
    if state == "ok":
        ok("Instagram token healthy", detail)
    elif state == "unknown":
        # Nothing has refreshed on THIS machine, so we genuinely do not know when
        # Meta minted the token in .env. Expected on the Mac, whose data/agent.db
        # is a different database from the server's — the run that matters is
        # `docker compose exec agent python preflight.py`.
        warn("Instagram token: never refreshed on this machine, so no expiry known",
             detail + " — expected locally; check the server's own run")
    elif state == "expiring":
        warn("Instagram token expiring", detail + " — the scheduler refreshes it")
    else:
        bad(f"Instagram token {state}", detail)
    if st.get("last_error"):
        warn("last refresh recorded an error", st["last_error"])
except Exception as e:
    bad("credentials.status() failed", e)

print("\n5. Google Sheet (read only — no row is touched)")

# Guarded, and not at the top of the file, because gspread and google-auth are
# the two libraries requirements.txt forgets (see the header of
# requirements.docker.txt). A missing one should cost this section, not the whole
# preflight — the sections above it are the ones that gate going live.
try:
    import sheets  # noqa: E402
except Exception as e:
    sheets = None
    bad("gspread/google-auth not importable",
        f"{e} — pip install -r requirements.docker.txt")

if sheets:
    try:
        leads = sheets.get_pending_leads()
        ok("sheet opens and pending rows read", f"{len(leads)} pending")
        for lead in leads[:3]:
            number = str(lead.get("number") or "")
            print(f"        pending: {lead.get('name')!r} ...{number[-4:]}")
        if not leads:
            warn("no pending leads", "Round 4 needs your own number as a pending row")
    except Exception as e:
        bad("sheet unreachable", e)

    # Follow-ups are the one thing that can send without anyone pressing
    # anything: `followup_auto` defaults on, and until FOLLOWUP_TEMPLATE_NAME
    # exists it re-sends whatever CAMPAIGN_TEMPLATE_NAME is. So the count of due
    # rows is the count of real people the scheduler will message next pass.
    try:
        due, exhausted = sheets.get_followup_candidates(
            store.get_float("followup_after_hours"), store.get_int("followup_max"))
        label = f"{len(due)} due now, {len(exhausted)} past the limit"
        if due and store.get_bool("followup_auto"):
            warn(f"the scheduler will send {len(due)} follow-up(s) on its next pass",
                 f"template {config.FOLLOWUP_TEMPLATE_NAME!r} — check that is what "
                 f"you want going to a real number before leaving this running")
        else:
            ok("follow-up queue read",
               label + f", followup_auto={store.get_bool('followup_auto')}")
    except Exception as e:
        bad("follow-up candidates unreadable", e)

print("\n6. /health, if the app is running on this machine")

try:
    r = requests.get("http://127.0.0.1:8001/health", timeout=5)
    payload = r.json()
    if r.status_code == 200 and payload.get("status") == "ok":
        ok("/health answers", f"queue={payload.get('queue')} today={payload.get('today')}")
    else:
        bad(f"/health returned {r.status_code}", str(payload)[:200])
except Exception:
    warn("/health not reachable on 127.0.0.1:8001",
         "expected if the app is not running here — start uvicorn and re-run")

print()
if FAILURES:
    print(f"{len(FAILURES)} PROBLEM(S) — do not move on to Round 2:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("ROUND 1 CLEAN — every credential is live and nothing was sent.")

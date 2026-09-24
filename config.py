"""
Central config loader. Everything sensitive comes from .env — never hardcode
tokens/ids in the other files.

Trimmed down for now: just what's needed to get inbound WhatsApp messages
answered by OpenClaw. Sheets/follow-ups/escalation get added back once this
loop is solid.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Meta / WhatsApp Cloud API ---
WHATSAPP_TOKEN = os.environ["WHATSAPP_TOKEN"]      # permanent System User token
PHONE_NUMBER_ID = os.environ["PHONE_NUMBER_ID"]    # the "from" number's ID (not the phone number itself)
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]          # your own made-up string, must match the Meta dashboard
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v22.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# --- Instagram: the second inbound channel ---------------------------------
# Same brain, same pacing, same escalation, same dashboard — only the transport
# differs. Two things here are not interchangeable with WhatsApp and both have
# cost us a debugging session before:
#   * the host is graph.instagram.com, not graph.facebook.com
#   * the token is the Instagram one; a WhatsApp token 400s on every IG call
# Values are read from the same names the standalone ig-relay used, so copying
# three lines out of that .env is all the migration there is.
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN", "")
IG_BUSINESS_ACCOUNT_ID = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "")
IG_GRAPH_VERSION = os.environ.get("IG_GRAPH_VERSION", "v26.0")
IG_GRAPH_BASE = f"https://graph.instagram.com/{IG_GRAPH_VERSION}"
# Instagram's webhook is verified separately and may sit in a different Meta app,
# so it gets its own token — falling back to WhatsApp's when one app serves both.
IG_VERIFY_TOKEN = (os.environ.get("IG_VERIFY_TOKEN")
                   or os.environ.get("META_VERIFY_TOKEN") or VERIFY_TOKEN)


# --- OpenClaw ---
OPENCLAW_AGENT_ID = os.environ.get("OPENCLAW_AGENT_ID", "whatsapp_leads")
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "openclaw")   # path to the CLI if not on PATH
OPENCLAW_TIMEOUT_SECONDS = int(os.environ.get("OPENCLAW_TIMEOUT_SECONDS", "120"))

# --- Gemini (the brain) ---
# The persona/prompt is NOT here — it lives in store.py so the client can edit
# it from the dashboard. Only the secret belongs in env.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# --- Dashboard ---
# Single shared password for the client's control panel. The panel can turn
# outbound messaging on and off and rewrite the agent's prompt, so it must never
# be exposed without this set.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DASHBOARD_SESSION_HOURS = int(os.environ.get("DASHBOARD_SESSION_HOURS", "12"))

# --- Brand ---
# The business name shown on the dashboard and used in the default persona. Read
# from the environment so this public codebase carries only a generic
# placeholder; a real deployment sets its own name in .env (BRAND_NAME).
BRAND_NAME = os.environ.get("BRAND_NAME", "Dunder Mifflin")

# --- Templates / outbound campaign ---
# The language code has to match the template's own language EXACTLY as Meta
# stores it, and the two look identical in the UI: "English" is `en`, "English
# (US)" is `en_US`. Get it wrong and every send fails with 132001, "template name
# does not exist in the translation" — which names the template rather than the
# language and sends you hunting in the wrong place. The intro template is
# registered as English, so `en`.
TEMPLATE_LANGUAGE_CODE = os.environ.get("TEMPLATE_LANGUAGE_CODE", "en")
# Defaulting this to Meta's `hello_world` sample was a live hazard rather than a
# safe fallback: hello_world is itself an approved template, so a missing .env
# line did not fail loudly — it quietly sent "Hello World" to a paying client's
# leads. The default here is a placeholder; a real deployment sets the approved
# template name in .env.
CAMPAIGN_TEMPLATE_NAME = os.environ.get("CAMPAIGN_TEMPLATE_NAME", "dundermifflin_full_intro")
DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "91")

# Alert templates. These deliver OUTSIDE the 24h customer-service window, which
# free-form text cannot — the developer and the client will not message the
# business number every 24h, so plain-text alerts were silently dropped. Both
# are approved (Marketing, English/en) in Meta Business Manager:
#   * SYSTEM_ALERT_TEMPLATE — errors.py + watchdog.sh. 2 body vars:
#       {{1}} one-line alert message, {{2}} timestamp.
#   * LEADS_ALERT_TEMPLATE  — escalation.py hot-lead handoff. 5 body vars:
#       {{1}} headline, {{2}} contact, {{3}} what they said,
#       {{4}} conversation (flattened), {{5}} what-to-do + dashboard link.
# The defaults below are placeholders; a real deployment sets its own approved
# template names in .env.
SYSTEM_ALERT_TEMPLATE = os.environ.get("SYSTEM_ALERT_TEMPLATE", "dundermifflin_system_alert")
LEADS_ALERT_TEMPLATE = os.environ.get("LEADS_ALERT_TEMPLATE", "dundermifflin_leads_alert")


# --- Phase 2: scheduler, follow-ups, escalation, greetings ---
def _csv_env(key, default_list):
    raw = os.environ.get(key)
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else default_list

SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "true").lower() == "true"
COLD_AUTO = os.environ.get("COLD_AUTO", "true").lower() == "true"
FOLLOWUP_AUTO = os.environ.get("FOLLOWUP_AUTO", "true").lower() == "true"
SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "300"))
SCHEDULER_STARTUP_DELAY_SECONDS = int(os.environ.get("SCHEDULER_STARTUP_DELAY_SECONDS", "15"))
COLD_BATCH_LIMIT = int(os.environ.get("COLD_BATCH_LIMIT", "10"))
COLD_PACING_SECONDS = int(os.environ.get("COLD_PACING_SECONDS", "4"))

FOLLOWUP_AFTER_HOURS = float(os.environ.get("FOLLOWUP_AFTER_HOURS", "24"))
FOLLOWUP_MAX = int(os.environ.get("FOLLOWUP_MAX", "2"))
# Inherits the campaign template so follow-ups never silently stop — but that
# fallback re-sends the entire Marketing brochure to someone who already ignored
# it once, twice over if FOLLOWUP_MAX is 2, which is how a template earns a
# quality flag. jobs.run_followups logs a warning while the two are still equal.
# Point this at a short Utility template before real follow-ups go out.
FOLLOWUP_TEMPLATE_NAME = os.environ.get("FOLLOWUP_TEMPLATE_NAME", CAMPAIGN_TEMPLATE_NAME)

GREETINGS = _csv_env("GREETINGS", ["Hi", "Hello", "Hey", "Namaste"])

OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "918919167539")

# --- Error alerting ---------------------------------------------------------
# The Doctor Desk (errors.py) records every fault; these say WHERE it also buzzes
# a phone, mirroring how a hot lead reaches the owner. Two audiences:
#   * DEVELOPER_NUMBER — the technical alert: module, level, stack trace. Yours.
#   * CLIENT_NUMBER    — plain-language business impact only. The client's.
# Both fall back to OWNER_NUMBER so a half-configured .env still reaches someone
# rather than dropping alerts on the floor. Set DEVELOPER_NUMBER to your own
# WhatsApp and CLIENT_NUMBER to the client's; leave one unset to send both to
# OWNER_NUMBER. Free-form WhatsApp needs an open 24h window with each recipient
# (same constraint as owner escalation alerts) — see jobs.notify_owner.
DEVELOPER_NUMBER = os.environ.get("DEVELOPER_NUMBER", "").strip() or OWNER_NUMBER
CLIENT_NUMBER = os.environ.get("CLIENT_NUMBER", "").strip() or OWNER_NUMBER
ESCALATION_KEYWORDS = _csv_env("ESCALATION_KEYWORDS", [
    "call me", "call back", "urgent", "asap", "budget", "quote", "quotation",
    "site visit", "appointment", "meeting", "book a", "when can you start", "phone number",
])

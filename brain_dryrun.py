"""Round 2: one real Gemini call per scenario, through the live persona. Sends nothing.

This is the round that answers "does the agent sound like the client, and does it
label a hot lead correctly" — before any of it can reach a real person.

    python brain_dryrun.py

It calls brain.get_reply() directly, which is the same function the webhook uses,
so the persona, the history window, the model, the fallback model and the rate
gate are all the real ones. What it deliberately does NOT call is
escalation.review(): that WhatsApps the owner and writes to the sheet, and
nothing in this round is allowed to leave the box. The intent label it prints is
exactly what review() would act on, so you can read the verdict without firing
it. Escalation side effects are covered by test_agent.py and go live in Round 3.

`reply_auto` is irrelevant here — that switch gates the sender, and this script
has no sender. Leave it alone.

One caveat worth reading before trusting the output: the persona lives in
data/agent.db, and the Mac's database is not the server's. If the client has
edited the prompt from the dashboard, only a run on the server sees it:
    docker compose exec agent python brain_dryrun.py
This script prints which of the two it is using.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain  # noqa: E402
import store  # noqa: E402

LEAD_PREFIX = "9199000000"   # never a real number — nothing here can be replied to


def line(char="-"):
    print(char * 78)


def show_setting(key, limit=None):
    live = store.get(key)
    default = store.DEFAULTS.get(key)
    origin = "DEFAULT (nobody has edited this)" if live == default else "edited in the dashboard"
    body = live if limit is None or len(live or "") <= limit else (live[:limit] + " …")
    print(f"\n{key}  [{origin}]\n{body}")


# Each tuple is (thread, label, what the lead says).
#
# The two that share the "3bhk" thread are deliberate: the second message only
# makes sense if the first is still in the history window, so if the agent
# answers it cold, `history_turns` is not reaching the model and every real
# conversation will feel like talking to a stranger.
SCENARIOS = [
    ("enquiry", "Ordinary first message",
     "Hi, saw your page. Do you do full home interiors?"),
    ("3bhk", "Serious lead — budget and a call request",
     "We just got possession of a 3BHK in Kokapet. What would full interiors "
     "cost roughly, and can someone call me today?"),
    ("3bhk", "Same lead, follow-up (proves the history window works)",
     "And how long does it take start to finish?"),
    ("hinglish", "Hinglish / Telugu-English mix, which is what Hyderabad sends",
     "Anna cost enta approx? 2bhk lo modular kitchen kavali"),
    ("timewaster", "Off-topic — should come back as time_waster or browsing",
     "bro do you know any good place for biryani near gachibowli"),
    ("stop", "Opt-out — must be read as not_interested, and must not argue",
     "STOP. Don't message me again."),
]

print("\nRound 2 — brain dry run. Nothing is sent to anyone.")
line("=")

print(f"\nsettings database : {store.DB_PATH}")
print("                    (the Mac's copy is NOT the server's — if this is your "
      "Mac,\n                     re-run it on the server before you trust the "
      "persona below)")
print(f"brain             : {store.get('brain')}")
fallback = (store.get("gemini_fallback_model") or "").strip()
print(f"model             : {store.get('gemini_model')}  "
      f"(fallback: {fallback or 'auto — picked from the API list on failure'})")
print(f"history_turns     : {store.get_int('history_turns')}")
print(f"rate gate         : {store.get_int('brain_max_per_minute')}/minute")
print(f"reply_auto        : {store.get_bool('reply_auto')}  "
      f"(not used by this script — there is no sender here)")

line("=")
print("\nWHAT THE AGENT HAS BEEN TOLD TO BE")
show_setting("system_prompt")
show_setting("escalation_criteria")
show_setting("handoff_message")

# What escalation.review() would do with each label, read out of escalation.py so
# you can see the consequence next to the verdict. review() is NOT called: it
# WhatsApps the owner and writes to the sheet, and this round sends nothing.
CONSEQUENCE = {
    "serious": ("owner gets an alert + the sheet row is marked 'escalated'"
                " (repeat alerts held for escalation_alert_cooldown_hours)"),
    "not_interested": ("lead is muted permanently and marked 'not_interested'"
                       " — unconditional, WhatsApp policy"),
    "time_waster": ("strike counted; at off_topic_strikes_max={} the lead is cut"
                    " off and the owner is told"),
    "browsing": "nothing happens — the agent just keeps talking",
}

line("=")
print("\nSIX REAL GEMINI CALLS — read the replies as if you were the lead")

seen = {}
failed = 0

for index, (thread, label, text) in enumerate(SCENARIOS, start=1):
    lead = f"{LEAD_PREFIX}{abs(hash(thread)) % 100:02d}"
    line()
    print(f"\n[{index}/{len(SCENARIOS)}] {label}")
    print(f"        thread {lead}")
    print(f"\n  LEAD  : {text}")
    try:
        result = brain.get_reply(lead, text, name="Test Lead")
    except brain.BrainError as e:
        failed += 1
        print(f"\n  FAILED: {type(e).__name__}: {e}")
        continue

    intent = result.get("intent") or "browsing"
    consequence = CONSEQUENCE.get(intent, "unknown label — check brain.py")
    if intent == "time_waster":
        consequence = consequence.format(store.get_int("off_topic_strikes_max"))

    print(f"\n  AGENT : {result.get('reply')}")
    print(f"\n  intent: {intent}")
    print(f"  → live: {consequence}")
    seen[intent] = seen.get(intent, 0) + 1

line("=")
print(f"\n{len(SCENARIOS) - failed}/{len(SCENARIOS)} calls answered."
      f"  labels: {seen or 'none'}")
if failed:
    print(f"{failed} call(s) failed — fix that before Round 3.")

print("""
Read it yourself, this part cannot be automated:

  1. Does it sound like the client's business, or like a chatbot?
  2. Did the 3BHK message come back `serious`? That is the money label — it is
     what puts the lead in front of the owner.
  3. Did the follow-up ("how long start to finish") answer in context, or ask
     what project you mean? Asking = the history window is not reaching Gemini.
  4. Did the Hinglish one get answered in kind, not in stiff English?
  5. Did STOP come back `not_interested`, with no argument and no pitch?
  6. Any price quoted that the client would not stand behind? Fix the prompt in
     the dashboard, not the code.

Anything wrong above is a system_prompt edit in the dashboard, then re-run this.

These six threads are now in the settings database as conversation history. They
are unreachable numbers so they can do no harm, but to clear them:
  sqlite3 data/agent.db "DELETE FROM turns WHERE lead LIKE '9199000000%';"
Never delete data/agent.db itself — it holds the persona and the IG token.
""")

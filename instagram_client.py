"""
instagram_client.py — the outbound half of the Instagram channel.

Deliberately the same shape as whatsapp_client.py: send a string to an id and
get out of the way, so channel.py can treat the two transports identically.

Replaces the old standalone ig-relay, which asked a local OpenClaw agent for the
reply and then scraped it out of that agent's session transcript. That failed in
a way worth remembering: OpenClaw answered by calling its own `sessions_send`
tool and finishing with the control token "ANNOUNCE_SKIP", so the transcript's
last text block was never the customer's reply. The relay read an empty string
and the lead got nothing. Instagram now uses brain.py — the same Gemini call,
persona and escalation as WhatsApp — and this module only carries the result.

Differences from WhatsApp that the code has to respect:
  * host is graph.instagram.com and the token is the Instagram one
  * no read receipts and no typing indicator on this integration
  * a DM body is capped at 1000 characters, a quarter of WhatsApp's limit
"""
import logging
from collections import OrderedDict

import requests

import config
import credentials

log = logging.getLogger("whatsapp-relay.ig")

MAX_BODY = 1000                    # Instagram rejects longer DMs outright


def _clip(body):
    body = body or ""
    return body if len(body) <= MAX_BODY else body[:MAX_BODY - 1] + "…"


def _messages_url():
    return f"{config.IG_GRAPH_BASE}/{config.IG_BUSINESS_ACCOUNT_ID}/messages"


def send_text(to_id: str, body: str) -> dict:
    """Reply to an Instagram DM. Only valid inside the same 24h window WhatsApp
    has; outside it Meta returns an error and there is no template equivalent."""
    payload = {
        "recipient": {"id": str(to_id)},
        "message": {"text": _clip(body)},
        "access_token": credentials.ig_token(),
    }
    resp = requests.post(_messages_url(), json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def reply_to_comment(comment_id: str, body: str) -> dict:
    """Public reply under a comment. A different endpoint, not a different kind
    of message: /replies posts as the account, in the thread the lead started."""
    resp = requests.post(
        f"{config.IG_GRAPH_BASE}/{comment_id}/replies",
        json={"message": _clip(body), "access_token": credentials.ig_token()},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# --- Who is 1564850898722146? ----------------------------------------------
# The webhook hands over a numeric sender id and nothing else — no name, unlike
# WhatsApp which puts the contact's profile name in the payload. One lookup
# turns that id into "Sohan (@sohan.arch)", which is the difference between an
# owner alert he can act on and one he has to go hunting through his inbox for.
# It also lets the brain greet people by name.
#
# Cached, including failures: a lookup that 400s (missing permission, an id from
# a different account) will keep 400ing, and repeating it on every message would
# add a round-trip to every reply for nothing.
_PROFILES: "OrderedDict[str, dict]" = OrderedDict()
_PROFILES_MAX = 500
_EMPTY = {"name": None, "username": None}


def profile(igsid: str) -> dict:
    igsid = str(igsid)
    cached = _PROFILES.get(igsid)
    if cached is not None:
        return cached

    found = dict(_EMPTY)
    if credentials.ig_token():
        try:
            resp = requests.get(
                f"{config.IG_GRAPH_BASE}/{igsid}",
                params={"fields": "name,username", "access_token": credentials.ig_token()},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                found = {"name": data.get("name"), "username": data.get("username")}
            else:
                log.debug("IG profile lookup for %s returned %s: %s",
                          igsid, resp.status_code, resp.text[:160])
        except requests.RequestException as e:
            log.debug("IG profile lookup for %s failed: %s", igsid, e)

    _PROFILES[igsid] = found
    if len(_PROFILES) > _PROFILES_MAX:
        _PROFILES.popitem(last=False)
    return found


def display_name(igsid: str):
    """Best available human name, or None. Never raises — a missing name must
    not stop a reply."""
    try:
        p = profile(igsid)
    except Exception:                       # pragma: no cover - belt and braces
        return None
    return p.get("name") or (f"@{p['username']}" if p.get("username") else None)


def username(igsid: str):
    try:
        return profile(igsid).get("username")
    except Exception:                       # pragma: no cover
        return None


def cached_username(igsid: str):
    """The handle only if we already looked it up. Never makes a request.

    For rendering LISTS. The dashboard shows up to 200 conversations at once, and
    calling username() for each one would fire up to 200 Graph requests inside a
    single page render — a page that would then take minutes on a cold cache.
    Owner alerts, which handle one lead at a time, use username() and do pay for
    the lookup.
    """
    entry = _PROFILES.get(str(igsid))
    return entry.get("username") if entry else None

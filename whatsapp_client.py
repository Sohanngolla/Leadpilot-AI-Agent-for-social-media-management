"""
Thin wrapper around the WhatsApp Cloud API /messages endpoint.
"""
import logging

import requests
import config

log = logging.getLogger("whatsapp-relay.wa")


def _headers():
    return {
        "Authorization": f"Bearer {config.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }


def _url():
    return f"{config.GRAPH_API_BASE}/{config.PHONE_NUMBER_ID}/messages"


def send_text(to_number: str, body: str) -> dict:
    """
    Free-form text reply. Only works inside the 24h customer-service window
    (i.e. the lead has messaged you in the last 24 hours).
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body},
    }
    resp = requests.post(_url(), headers=_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


# --- Read receipts + typing indicator --------------------------------------
# These are what make a slow reply read as "a busy human" instead of "a broken
# bot". Without them, a 60-second silence looks like nothing is happening; with
# them the lead sees blue ticks, then "typing…", then the message.
#
# Both are best-effort: if Meta rejects them (expired message id, older API
# version) we log and carry on — never let a cosmetic call block a real reply.

def _status_call(message_id: str, typing: bool) -> bool:
    if not message_id:
        return False
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    if typing:
        # Meta dismisses the indicator automatically after ~25s, or as soon as
        # we send the actual message — so fire it shortly before sending.
        payload["typing_indicator"] = {"type": "text"}
    try:
        resp = requests.post(_url(), headers=_headers(), json=payload, timeout=10)
        if resp.status_code != 200:
            log.debug("%s call rejected: %s %s",
                      "typing" if typing else "read", resp.status_code, resp.text[:160])
            return False
        return True
    except requests.RequestException as e:
        log.debug("%s call failed: %s", "typing" if typing else "read", e)
        return False


def mark_read(message_id: str) -> bool:
    """Blue ticks, no typing bubble."""
    return _status_call(message_id, typing=False)


def send_typing(message_id: str) -> bool:
    """Blue ticks + "typing…". Must reference the inbound message we're replying to."""
    return _status_call(message_id, typing=True)


def send_template(to_number: str, template_name: str, parameters: list[str] | None = None) -> dict:
    """
    Business-initiated message. Required for the first message to any lead,
    and for follow-ups sent outside the 24h window. template_name must exist
    and be APPROVED in Meta Business Manager, with the same number of
    {{1}}, {{2}}... variables as `parameters`.
    """
    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in parameters],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": config.TEMPLATE_LANGUAGE_CODE},
            "components": components,
        },
    }
    resp = requests.post(_url(), headers=_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()

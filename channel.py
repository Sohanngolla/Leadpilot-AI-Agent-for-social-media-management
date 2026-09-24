"""
channel.py — one lead key, two messaging channels.

The agent was built for WhatsApp, where a lead IS a phone number. Instagram ids
are numeric too, and letting the two share the `lead` column unprefixed would be
a quiet disaster: one person's history bleeding into another's, mute state
crossing channels, and cold outreach cheerfully firing a WhatsApp template at a
16-digit Instagram id. So the channel is carried in the key itself:

    919812345678          WhatsApp   (bare digits — nothing to migrate)
    ig:1564850898722146   Instagram DM
    igc:17925...          one Instagram comment thread

Everything downstream — history, mute, escalation, the dashboard, the daily
counters — already treats the key as an opaque string, so this module is the
only place that has to know the difference. Adding a third channel means adding
a prefix here, not touching the pacer or the brain.

Comments get their own key rather than being folded into the commenter's DM
thread: the reply has to go to the comment endpoint, not the inbox, and a public
comment and a private DM are not the same conversation even when they come from
the same person.
"""
import logging

import instagram_client
import whatsapp_client

log = logging.getLogger("whatsapp-relay.channel")

WHATSAPP = "whatsapp"
INSTAGRAM_DM = "instagram_dm"
INSTAGRAM_COMMENT = "instagram_comment"

DM_PREFIX = "ig:"
COMMENT_PREFIX = "igc:"


def dm_key(igsid):
    return f"{DM_PREFIX}{igsid}"


def comment_key(comment_id):
    return f"{COMMENT_PREFIX}{comment_id}"


def of(lead):
    lead = str(lead)
    if lead.startswith(COMMENT_PREFIX):
        return INSTAGRAM_COMMENT
    if lead.startswith(DM_PREFIX):
        return INSTAGRAM_DM
    return WHATSAPP


def native(lead):
    """The id the platform knows, with our prefix stripped off."""
    lead = str(lead)
    for prefix in (COMMENT_PREFIX, DM_PREFIX):
        if lead.startswith(prefix):
            return lead[len(prefix):]
    return lead


def is_whatsapp(lead):
    return of(lead) == WHATSAPP


def sanitise_key(raw):
    """Turn an untrusted lead key (a dashboard form field, a query string) into a
    canonical one, or "" if it isn't a plausible key at all.

    The dashboard used to run these through a digits-only filter, which was right
    when every lead was a phone number and quietly wrong the moment Instagram
    arrived: "ig:1564850898722146" came back as "1564850898722146", so opening an
    Instagram thread showed nothing and "I'll take this one" muted a key that
    doesn't exist — the takeover would have silently failed. Keep the prefix,
    keep only digits after it.
    """
    raw = str(raw or "").strip()
    for prefix in (COMMENT_PREFIX, DM_PREFIX):
        if raw.startswith(prefix):
            digits = "".join(ch for ch in raw[len(prefix):] if ch.isdigit())
            return f"{prefix}{digits}" if digits else ""
    return "".join(ch for ch in raw if ch.isdigit())


def label(lead, name=None):
    """How this lead is written in an owner alert and in the dashboard.

    "+919812345678" for WhatsApp because that is what he'll search for on his
    phone; a handle for Instagram because a raw 16-digit id is unsearchable.
    """
    kind, ident = of(lead), native(lead)
    if kind == WHATSAPP:
        return f"+{ident}"
    if kind == INSTAGRAM_COMMENT:
        return "Instagram comment"
    handle = instagram_client.username(ident)
    if handle:
        return f"@{handle} on Instagram"
    return f"Instagram user {ident}"


def short_label(lead):
    """A compact identifier for LISTS — the dashboard's inbox and alert rows.

    Same job as label(), minus the network: it uses an Instagram handle only if
    some earlier alert already resolved and cached it, because this runs once per
    row and the panel renders up to 200 rows at a time.
    """
    kind, ident = of(lead), native(lead)
    if kind == WHATSAPP:
        return f"+{ident}"
    if kind == INSTAGRAM_COMMENT:
        return "Instagram comment"
    handle = instagram_client.cached_username(ident)
    return f"@{handle}" if handle else f"Instagram {ident}"


def open_link(lead):
    """One tappable line that gets the owner into the right conversation.

    Instagram has no per-user deep link from a scoped id, so if we never managed
    to resolve the handle the honest answer is to say where to look rather than
    print a URL that goes nowhere.
    """
    kind, ident = of(lead), native(lead)
    if kind == WHATSAPP:
        return f"Open the chat: wa.me/{ident}"
    if kind == INSTAGRAM_COMMENT:
        return "Find it in Instagram → your post's comments"
    handle = instagram_client.username(ident)
    if handle:
        return f"Open the chat: ig.me/m/{handle}"
    return "Open Instagram → Inbox to find this one"


def inbound_name(lead, webhook_name=None):
    """The name to greet this lead by. WhatsApp puts it in the payload;
    Instagram makes us ask for it."""
    if webhook_name:
        return webhook_name
    if of(lead) == INSTAGRAM_DM:
        return instagram_client.display_name(native(lead))
    return None


def send_text(lead, body):
    """Send a reply on whichever channel the lead came in on. Raises like the
    underlying client does — the pacer already logs and gives up on failure."""
    kind, ident = of(lead), native(lead)
    if kind == INSTAGRAM_DM:
        return instagram_client.send_text(ident, body)
    if kind == INSTAGRAM_COMMENT:
        return instagram_client.reply_to_comment(ident, body)
    return whatsapp_client.send_text(ident, body)


def show_reading(lead, message_id, typing=True):
    """Blue ticks (and optionally the typing bubble) — WhatsApp only.

    Instagram's messaging integration exposes neither, so this is a no-op there
    rather than a failed API call on every single reply. The client asked for
    the pacing on Instagram, not the ticks.
    """
    if not message_id or not is_whatsapp(lead):
        return False
    # Both calls key off the inbound message id, not the lead.
    return (whatsapp_client.send_typing(message_id) if typing
            else whatsapp_client.mark_read(message_id))


def tracked_in_sheet(lead):
    """Whether this lead belongs in the Google Sheet.

    False for Instagram. That sheet is the phone-number campaign tracker: it
    matches rows by digits with a suffix comparison, and dropping 16-digit
    Instagram ids into the Number column invites a false match against a real
    phone number. Instagram conversations live in SQLite and the dashboard.
    """
    return is_whatsapp(lead)

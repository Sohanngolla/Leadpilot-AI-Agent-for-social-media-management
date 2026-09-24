"""
credentials.py — the live Instagram token, and how it stays alive.

WHY THIS FILE EXISTS
Every other credential in this system is set once and then forgotten. The
Instagram token is the exception: Meta issues it for 60 days and then it dies,
silently, and every DM reply starts failing with a 400. Fixing that by hand
means SSH-ing into the VPS, generating a new token in the Meta app, editing
`.env` and restarting the container — a chore that only announces itself when
the client notices Instagram has gone quiet.

Meta's answer is `refresh_access_token`: a long-lived token that is at least 24
hours old and not yet expired can be exchanged for a fresh one, valid another 60
days from the moment of refresh. Do that on a timer and the token never expires
at all. See:
https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token/

THE ONE STRUCTURAL CHANGE
A refreshed token has to be written somewhere, and `.env` is mounted read-only
into the container (deliberately — see docker-compose.yml), so it cannot be
rewritten from inside. The new token therefore lives in the settings table,
which is on the `./data` volume and survives restarts. So the resolution order
for the token is:

    settings table (refreshed, current)  ->  .env  (the one you pasted)

`.env` stays the source of truth for the FIRST token; after that this module
owns it. Reading a token is a SQLite hit per send, which is nothing next to the
Graph round-trip that follows, and every failure path falls back to `.env`
rather than to nothing.

SECURITY NOTE
Putting the token in the DB does not widen the blast radius: `data/agent.db` and
`.env` sit on the same disk with the same owner on the same host. What it must
never do is reach the dashboard. The keys below are deliberately NOT in
store.DEFAULTS, and store.all_settings() iterates DEFAULTS — so no settings page
can render this value even by accident. `status()` returns dates and state, and
never the token.
"""
import logging
import time

import requests

import config
import store

log = logging.getLogger("whatsapp-relay.credentials")

# Settings-table keys. Not in store.DEFAULTS on purpose: see the security note.
KEY_TOKEN = "ig_token_value"           # the refreshed token (secret)
KEY_EXPIRES = "ig_token_expires_at"    # epoch seconds
KEY_REFRESHED = "ig_token_refreshed_at"
KEY_CHECKED = "ig_token_checked_at"    # last ATTEMPT, success or not
KEY_ERROR = "ig_token_error"           # last failure, human-readable
KEY_ALERTED = "ig_token_alerted_at"    # owner alert de-duplication

LIFETIME_DAYS = 60                     # what Meta grants
REFRESH_WHEN_DAYS_LEFT = 20            # act with three weeks of slack, not three days
ALERT_WHEN_DAYS_LEFT = 7               # by now a human needs to know
CHECK_MIN_INTERVAL_SECONDS = 6 * 3600  # don't re-attempt more often than this
ALERT_MIN_INTERVAL_SECONDS = 24 * 3600
HTTP_TIMEOUT = 15

def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _read(key, default=""):
    """A setting read that cannot break a send. If the DB is locked or missing
    we want the env token, not an exception halfway through answering a DM."""
    try:
        return store.get(key, default)
    except Exception as e:                       # pragma: no cover - defensive
        log.warning("Could not read %s from the settings table: %s", key, e)
        return default


def _write(key, value):
    try:
        store.set(key, value)
        return True
    except Exception as e:                       # pragma: no cover - defensive
        log.error("Could not persist %s: %s", key, e)
        return False


def ig_token():
    """The Instagram token to actually use, refreshed value first.

    Every caller in instagram_client.py goes through here, so a refresh applies
    to the very next message with no restart.
    """
    stored = (_read(KEY_TOKEN) or "").strip()
    return stored or config.IG_ACCESS_TOKEN


def _expires_at():
    return _num(_read(KEY_EXPIRES, 0))


def days_left():
    """Days until the token dies, or None while we have never refreshed it and
    therefore genuinely do not know when Meta minted the one in `.env`."""
    expires = _expires_at()
    if not expires:
        return None
    return (expires - time.time()) / 86400.0


def status():
    """What the dashboard shows. Dates and state only — never the token."""
    token = ig_token()
    left = days_left()
    error = _read(KEY_ERROR) or ""
    refreshed = _num(_read(KEY_REFRESHED, 0))

    if not token:
        state = "missing"
    elif left is None:
        state = "unknown"
    elif left <= 0:
        state = "expired"
    elif left <= ALERT_WHEN_DAYS_LEFT:
        state = "expiring"
    else:
        state = "ok"

    return {
        "configured": bool(token),
        "source": "auto-refreshed" if (_read(KEY_TOKEN) or "").strip() else ".env",
        "state": state,
        "days_left": None if left is None else round(left, 1),
        "expires_at": _expires_at(),
        "refreshed_at": refreshed,
        "last_error": error,
        "last_checked": _num(_read(KEY_CHECKED, 0)),
    }


def _refresh_urls():
    """Meta documents this endpoint at the host root, unversioned. Some apps only
    answer on the versioned path, so try the documented one and then the
    versioned one rather than guessing which this app is."""
    base = config.IG_GRAPH_BASE.rstrip("/")
    host = base.rsplit("/", 1)[0] if "/v" in base else base
    urls = [f"{host}/refresh_access_token"]
    if base != host:
        urls.append(f"{base}/refresh_access_token")
    return urls


def refresh():
    """Exchange the current token for a fresh 60 days. Returns (outcome, detail).

    outcome is "refreshed", "skipped" or "failed". Never raises: this runs on a
    timer inside the scheduler, and a bad refresh must not take the pass down
    with it. The old token is left in place on failure — it is still valid until
    its expiry, so a failed refresh costs nothing but a retry.

    Unconditional by design. Whether it is time to do this is _due()'s job, so
    that maintain() decides and this function only carries it out — which also
    makes it the right thing to call by hand when checking the wiring works.
    """
    token = ig_token()
    if not token:
        return "skipped", "no Instagram token configured"

    now = time.time()
    _write(KEY_CHECKED, int(now))
    last_error = None

    for url in _refresh_urls():
        try:
            resp = requests.get(url, params={"grant_type": "ig_refresh_token",
                                             "access_token": token},
                                timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_error = f"network error: {e}"
            continue

        if resp.status_code == 200:
            data = resp.json()
            fresh = (data.get("access_token") or "").strip()
            if not fresh:
                last_error = "200 but no access_token in the response"
                continue
            # Trust Meta's own expires_in when it sends one; 60 days is the
            # documented grant and only a fallback.
            lifetime = _num(data.get("expires_in")) or LIFETIME_DAYS * 86400
            _write(KEY_TOKEN, fresh)
            _write(KEY_EXPIRES, int(now + lifetime))
            _write(KEY_REFRESHED, int(now))
            _write(KEY_ERROR, "")
            _write(KEY_ALERTED, 0)
            log.info("Instagram token refreshed — good for another %.0f days (len=%d).",
                     lifetime / 86400.0, len(fresh))
            return "refreshed", f"valid for {lifetime / 86400.0:.0f} more days"

        # A token younger than 24h is Meta's most common refusal here, and it is
        # not a problem: it means we generated it very recently, so there are ~60
        # days of runway and the next pass will succeed.
        last_error = f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"

    _write(KEY_ERROR, last_error or "unknown failure")
    log.warning("Instagram token refresh failed: %s", last_error)
    return "failed", last_error or "unknown failure"


def _due(now):
    """Should we attempt a refresh on this pass? (bool, reason)"""
    if now - _num(_read(KEY_CHECKED, 0)) < CHECK_MIN_INTERVAL_SECONDS:
        return False, "checked recently"
    if not ig_token():
        return False, "no Instagram token configured"

    left = days_left()
    if left is None:
        # We have never refreshed, so the age of the token in `.env` is unknown
        # and its real expiry could be tomorrow. One successful refresh replaces
        # that guesswork with a date we set ourselves, so attempt it once.
        return True, "expiry unknown — establishing our own 60-day window"
    if left <= REFRESH_WHEN_DAYS_LEFT:
        return True, f"{left:.1f} days left"
    return False, f"{left:.1f} days left"


def _alert_owner_if_needed(now, detail):
    """Tell the owner only when a human actually has to act: the token is nearly
    dead AND refreshing it is not working. A refresh that fails with 50 days on
    the clock is our problem to retry, not his to read about."""
    left = days_left()
    if left is not None and left > ALERT_WHEN_DAYS_LEFT:
        return False
    if now - _num(_read(KEY_ALERTED, 0)) < ALERT_MIN_INTERVAL_SECONDS:
        return False

    import jobs                     # lazy: keeps this module importable alone

    when = "has expired" if (left is not None and left <= 0) else \
           (f"expires in {left:.0f} day(s)" if left is not None else "may expire soon")
    try:
        jobs.notify_owner(
            "INSTAGRAM NEEDS ATTENTION\n\n"
            f"The Instagram access token {when} and could not be renewed "
            f"automatically.\n\nReason: {detail}\n\n"
            "Instagram DMs and comments will stop being answered. WhatsApp is "
            "unaffected. A new token has to be generated in the Meta app."
        )
    except Exception as e:                       # pragma: no cover - defensive
        log.warning("Could not alert the owner about the IG token: %s", e)
        return False
    _write(KEY_ALERTED, int(now))
    return True


def maintain():
    """One scheduler pass. Cheap by design: usually a couple of settings reads
    and an early return, so calling it every interval costs nothing."""
    now = time.time()
    due, reason = _due(now)
    if not due:
        log.debug("Instagram token: no refresh needed (%s).", reason)
        return "skipped"

    log.info("Instagram token: attempting refresh (%s).", reason)
    outcome, detail = refresh()
    if outcome == "failed":
        _alert_owner_if_needed(now, detail)
    return outcome

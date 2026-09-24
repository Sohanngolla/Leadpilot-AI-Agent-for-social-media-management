"""
The client's control panel: see what the agent did today, take over a hot lead,
and change how it behaves — without touching the server.

Security posture, because this panel can start outbound campaigns and rewrite the
agent's prompt:

  * One shared password (DASHBOARD_PASSWORD), which the operator sets and hands
    to the client. If it is unset the panel serves a "closed" page instead of
    opening up — an unset secret must never mean no secret.
  * The session cookie is an expiry timestamp plus an HMAC keyed off a hash of
    the password. Nothing is stored server-side, so restarts don't sign people
    out, and changing the password invalidates every existing session for free.
  * Failed sign-ins are counted per IP and throttled, because a single shared
    password behind a public tunnel is otherwise guessable at leisure.
  * Every write is whitelisted against store.DEFAULTS and clamped. The panel can
    only move knobs that already exist, only within sane ranges.

Only APIRouter/Request/Response are used from fastapi, and form bodies are parsed
with urllib rather than request.form(), so there is no python-multipart dependency
and the hermetic test suite can stub the framework in a few lines.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request, Response

import channel
import config
import credentials
import dashboard_view as view
import errors
import jobs
import pacer
import sheets
import store

log = logging.getLogger("whatsapp-relay.dashboard")

router = APIRouter()

COOKIE = "dm_session"
_MAX_FAILS = 8
_FAIL_WINDOW_SECONDS = 900
_fails: "dict[str, list[float]]" = {}


# --- Sessions ---------------------------------------------------------------

def locked():
    """True when no dashboard password is configured, which closes the panel."""
    return not (config.DASHBOARD_PASSWORD or "").strip()


def _key():
    # Derived, not the password itself, so the signing key never equals the secret.
    return hashlib.sha256(b"dm-dashboard-v1|" + config.DASHBOARD_PASSWORD.encode()).digest()


def _sign(payload):
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def issue():
    expires = int(time.time() + max(1, config.DASHBOARD_SESSION_HOURS) * 3600)
    return f"{expires}.{_sign(str(expires))}"


def valid(token):
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def authed(request):
    """The single source of truth for 'may this request act'. main.py imports it
    to protect the outbound job endpoints too."""
    if locked():
        return False
    cookies = getattr(request, "cookies", None) or {}
    return valid(cookies.get(COOKIE))


def _client_ip(request):
    hdrs = getattr(request, "headers", None) or {}
    fwd = hdrs.get("x-forwarded-for") if hasattr(hdrs, "get") else None
    if fwd:
        return fwd.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def _throttled(ip):
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW_SECONDS]
    _fails[ip] = hits
    return len(hits) >= _MAX_FAILS


def _record_fail(ip):
    _fails.setdefault(ip, []).append(time.time())
    if len(_fails) > 512:            # bounded: this is a dict on a long-lived process
        for stale in [k for k, v in _fails.items()
                      if not v or time.time() - v[-1] > _FAIL_WINDOW_SECONDS]:
            _fails.pop(stale, None)


# --- Response helpers -------------------------------------------------------

def _page(body, status=200, headers=None):
    return Response(content=body, status_code=status,
                    media_type="text/html; charset=utf-8", headers=headers or {})


def _to(path, headers=None):
    h = {"Location": path}
    h.update(headers or {})
    return Response(status_code=303, headers=h)


def _cookie_header(value, hours):
    parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax",
             f"Max-Age={int(hours * 3600)}"]
    return {"set-cookie": "; ".join(parts)}


async def _form(request):
    """Parse an urlencoded body without pulling in python-multipart. Plain HTML
    forms post urlencoded by default, so this covers every form in the panel."""
    raw = await request.body()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    pairs = parse_qs(raw or "", keep_blank_values=True)
    return {k: v[-1] for k, v in pairs.items()}


def _guard(request):
    """Returns a Response to send instead, or None when the caller may proceed."""
    if locked():
        return _page(view.render_locked(), 503)
    if not authed(request):
        return _to("/dashboard")
    return None


# --- Validation -------------------------------------------------------------

BOOL_KEYS = {k for k, _, _ in view.SWITCHES}
NUM_KEYS = {k for k, _, _ in view.PACING} | {k for k, _, _ in view.OUTREACH}
TEXT_KEYS = {k for k, _, _ in view.TEXTS}

# Clamps, not suggestions. reply_workers spawns threads and cold_daily_cap is the
# only thing standing between an enthusiastic client and a banned number. The
# table itself lives beside the form that draws these fields — see the comment on
# dashboard_view.RANGES — so the floor a stepper button stops at and the floor
# this clamps to cannot drift apart.
RANGES = view.RANGES
MAX_TEXT = view.MAX_TEXT


def _lead_key(raw):
    """Sanitise a lead key coming from the browser. Channel-aware — a bare digit
    filter would strip Instagram's "ig:" prefix and break the thread view and the
    takeover buttons. See channel.sanitise_key."""
    return channel.sanitise_key(raw)


def apply_settings(form):
    """Whitelist -> coerce -> clamp -> write. Returns how many knobs moved.

    A checkbox that is off simply isn't submitted, so absence means false here.
    That is why every switch must be rendered inside this one form.
    """
    changed = 0

    for key in BOOL_KEYS & set(store.DEFAULTS):
        want = "true" if str(form.get(key, "")).strip().lower() in ("1", "true", "yes", "on") else "false"
        if str(store.get(key)).strip().lower() != want:
            store.set(key, want)
            changed += 1

    for key in NUM_KEYS & set(store.DEFAULTS):
        raw = form.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            n = float(str(raw).strip())
        except ValueError:
            continue
        low, high = RANGES.get(key, (0, 10 ** 9))
        n = min(high, max(low, n))
        value = str(int(n)) if float(n).is_integer() else str(n)
        if str(store.get(key)) != value:
            store.set(key, value)
            changed += 1

    # Keep the reply window coherent no matter which end the client edited.
    if store.get_int("reply_delay_max_seconds") < store.get_int("reply_delay_min_seconds"):
        store.set("reply_delay_max_seconds", store.get_int("reply_delay_min_seconds"))
        changed += 1

    for key in TEXT_KEYS & set(store.DEFAULTS):
        if key not in form:
            continue
        text = str(form[key]).replace("\r\n", "\n").strip()[:MAX_TEXT]
        if not text:
            continue           # an empty persona would make the agent unpredictable
        if str(store.get(key)) != text:
            store.set(key, text)
            changed += 1

    return changed


_FLASH = {
    "saved": "Saved. Applies to the next message — nothing to restart.",
    "nochange": "Nothing had changed, so nothing was saved.",
    "muted": "This one is yours now. The agent will stay quiet on it.",
    "unmuted": "Handed back to the agent.",
    "failed": "That didn't go through. The server log has the reason.",
    "resolved": "Marked as resolved. It will reappear here only if it happens again.",
}


def _flash_for(code):
    """Codes, not free text, so nothing a caller puts in the URL reaches the page."""
    if not code:
        return None, False
    if code in _FLASH:
        return _FLASH[code], code == "failed"
    for prefix, noun in (("cold-", "new lead"), ("follow-", "follow-up")):
        if code.startswith(prefix):
            try:
                n = int(code[len(prefix):])
            except ValueError:
                return None, False
            if n == 0:
                return (f"No {noun}s went out — either none are waiting or today's "
                        "ceiling is already reached."), False
            return f"Sent {n} {noun}{'' if n == 1 else 's'}.", False
    return None, False


# --- Routes -----------------------------------------------------------------

@router.get("/dashboard")
async def page(request: Request):
    if locked():
        return _page(view.render_locked(), 503)
    if not authed(request):
        return _page(view.render_login())

    params = getattr(request, "query_params", None) or {}
    selected = _lead_key(params.get("lead")) or None
    flash, bad = _flash_for(str(params.get("ok") or "").strip())

    turns, muted = [], False
    if selected:
        turns = store.transcript(selected)
        muted = store.is_muted(selected)

    # The Doctor Desk lives here now: a plain-language "Health" tab for the client
    # and a technical "Developer" tab, both behind this one session. prune() is a
    # cheap opportunistic cleanup of long-resolved rows — see errors.py.
    errors.prune()

    return _page(view.render(
        stats=store.stats(),
        queue=pacer.status(),
        settings=store.all_settings(),
        leads=store.list_leads(),
 cold_leads=sheets.get_contacted_leads(limit=200),
        escalated=store.escalated_leads(),
        selected=selected,
        turns=turns,
        muted=muted,
        flash=flash,
        flash_bad=bad,
        connection=credentials.status(),
        err_summary=errors.summary(),
        err_recent=errors.recent(limit=200),
    ))


@router.post("/dashboard/login")
async def login(request: Request):
    if locked():
        return _page(view.render_locked(), 503)

    ip = _client_ip(request)
    if _throttled(ip):
        return _page(view.render_login(
            "Too many tries. Wait fifteen minutes, then try again."), 429)

    form = await _form(request)
    given = str(form.get("password") or "").encode("utf-8")
    if hmac.compare_digest(given, config.DASHBOARD_PASSWORD.encode("utf-8")):
        _fails.pop(ip, None)
        log.info("Dashboard sign-in from %s", ip)
        return _to("/dashboard", _cookie_header(issue(), config.DASHBOARD_SESSION_HOURS))

    _record_fail(ip)
    log.warning("Dashboard sign-in failed from %s", ip)
    return _page(view.render_login("That password isn't right."), 401)


@router.get("/dashboard/logout")
async def logout():
    return _to("/dashboard",
               {"set-cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})


@router.get("/dashboard/live")
async def live(request: Request):
    """The numbers in the masthead, so the panel can keep itself true without a
    reload. Behind the same guard as the page: /health carries the same figures
    and is public, but this one sits inside the session because the panel is the
    thing polling it and an authed page should not depend on an open endpoint.

    Read-only, and cheap — two SELECTs and a counter — so a page left open on a
    second screen all day costs nothing.
    """
    blocked = _guard(request)
    if blocked:
        return blocked
    # json.dumps into a plain Response rather than JSONResponse, so the set of
    # fastapi names this module touches stays at three and the hermetic tests can
    # keep stubbing the framework in a few lines.
    return Response(content=json.dumps({"queue": pacer.status(), "today": store.stats()}),
                    media_type="application/json",
                    headers={"cache-control": "no-store"})


@router.post("/dashboard/settings")
async def save_settings(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    changed = apply_settings(await _form(request))
    log.info("Dashboard saved %d setting(s)", changed)
    return _to(f"/dashboard?ok={'saved' if changed else 'nochange'}")


async def _set_mute(request, muted):
    blocked = _guard(request)
    if blocked:
        return blocked
    lead = _lead_key((await _form(request)).get("lead"))
    if not lead:
        return _to("/dashboard?ok=failed")
    store.set_muted(lead, muted)
    # Either way the owner has now dealt with this lead by hand, so it stops
    # asking for attention in "Needs you". Muting no longer does that on its own.
    store.mark_handled(lead)
    if not muted:
        # Handing a lead back has to clear the off-topic strikes too. Without
        # this, a lead the agent cut off comes back still holding three strikes,
        # so the very next stray message would silence it again immediately and
        # the button would look broken.
        store.clear_off_topic(lead)
    log.info("Dashboard %s %s", "took over" if muted else "handed back", lead)
    return _to(f"/dashboard?lead={quote(lead)}&ok={'muted' if muted else 'unmuted'}")


@router.post("/dashboard/lead/mute")
async def mute_lead(request: Request):
    return await _set_mute(request, True)


@router.post("/dashboard/lead/unmute")
async def unmute_lead(request: Request):
    return await _set_mute(request, False)


@router.post("/dashboard/errors/resolve")
async def resolve_error(request: Request):
    """Mark one issue (by fingerprint) or every open issue as dealt with, from the
    Developer tab. Behind the same session as everything else — the resolve form
    only shows once you are signed in. It drops off both Health and Developer views
    until the same failure happens again, at which point record() flips it back."""
    blocked = _guard(request)
    if blocked:
        return blocked
    form = await _form(request)
    if str(form.get("all") or "").strip().lower() in ("1", "true", "yes", "on"):
        errors.resolve(all_resolved=True)
    else:
        fp = str(form.get("fingerprint") or "").strip()
        if fp:
            errors.resolve(fingerprint=fp)
    # #dev returns them to the Developer tab they acted from.
    return _to("/dashboard?ok=resolved#dev")


@router.post("/dashboard/run/campaign")
async def run_campaign_now(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    try:
        # respect_window=False: a manual "Send now" from the panel is an explicit
        # human decision and bypasses the quiet-hours gate the scheduler honours.
        sent = await asyncio.to_thread(
            jobs.run_cold_batch, False, store.get_int("cold_batch_limit"), False)
    except Exception as e:
        log.error("Manual cold batch failed: %s", e)
        return _to("/dashboard?ok=failed")
    return _to(f"/dashboard?ok=cold-{int(sent or 0)}")


@router.post("/dashboard/run/followups")
async def run_followups_now(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    try:
        # respect_window=False — explicit manual trigger, see run_campaign_now.
        sent = await asyncio.to_thread(jobs.run_followups, False, False)
    except Exception as e:
        log.error("Manual follow-up run failed: %s", e)
        return _to("/dashboard?ok=failed")
    return _to(f"/dashboard?ok=follow-{int(sent or 0)}")

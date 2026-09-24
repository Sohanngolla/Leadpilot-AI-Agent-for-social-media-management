"""
The brain: turns an inbound message into a reply AND a verdict on how serious
the lead is — in ONE API call.

Replaces the OpenClaw CLI for two reasons. First, shelling out to a CLI doesn't
work in a container (no `openclaw` binary, no persona files, no home dir).
Second, the persona has to be editable by the client from the dashboard, and a
prompt living in a text file on someone's Mac can't be. So the persona is now a
setting in store.py and we talk to Gemini over plain HTTPS with `requests` —
no new dependency.

Two jobs, one call: asking the model to return
    {"intent": "...", "reply": "..."}
means we pay for one round-trip instead of two, and the verdict is made by the
same model that just read the whole conversation, so it's better informed than
a keyword match ever was.

Retries, and then changes model. The free Gemini tier throttles hard under
bursts (429) and Google's own capacity for a single model goes to 503 "this model
is currently experiencing high demand" for minutes at a time. A dropped reply is
a lost lead, so we back off and try again — and when the same model keeps
refusing, we ask a different one rather than waiting out an outage we cannot fix.
See _call_gemini.
"""
import json
import logging
import random
import re
import time

import requests

import config
import store

log = logging.getLogger("whatsapp-relay.brain")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Connecting is fast or broken; only the answer is slow. Splitting the two means a
# genuinely unreachable host fails in seconds instead of sitting on the full read
# budget, while a model that is thinking hard still gets its time.
CONNECT_TIMEOUT = 8

# 429 = out of quota, 5xx = transient on Google's side. Worth retrying on the
# same model. 404 means the model id is wrong or retired: retrying is pointless
# but another model may well work. Anything else (400/401/403) is our key or our
# request, so no model and no retry will help.
_RETRYABLE = (429, 500, 502, 503, 504)

# Not every model on the key can hold a conversation. Filtering by name is crude
# but it is the only signal ListModels gives us beyond generateContent support,
# and picking, say, an embedding model as a fallback would fail every time.
_UNSUITABLE = ("embedding", "embed", "aqa", "imagen", "image", "tts", "audio",
               "veo", "vision", "live", "gemma", "learnlm", "robotics",
               "computer-use", "guard")

_MODEL_CACHE = {"at": 0.0, "models": None}
_MODEL_CACHE_TTL = 6 * 3600          # model lists change on Google's timescale
_MODEL_CACHE_TTL_FAIL = 300          # but don't re-ask every message after a failure

# Kept deliberately small: these are the only verdicts the rest of the system
# knows how to act on. `time_waster` is a cost control, not a sales stage — see
# escalation.review.
INTENTS = ("serious", "browsing", "not_interested", "time_waster")

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": list(INTENTS)},
        "reply": {"type": "STRING"},
    },
    "required": ["intent", "reply"],
}


class BrainError(RuntimeError):
    pass


def _system_instruction(name=None):
    """Persona + escalation rules + output contract, all assembled live so an
    edit in the dashboard applies to the very next message."""
    parts = [store.get("system_prompt"), "", store.get("escalation_criteria"), ""]
    if name:
        parts.append(f"The person you are talking to is called {name}.")
    parts.append(
        "Return JSON with exactly two fields. `reply` is the message to send to "
        "the customer, written as a real person would text — short, warm, no "
        "greeting if the conversation is already going, and answering what they "
        "actually said rather than restarting the pitch. `intent` is your verdict "
        "on this lead: 'serious' if they meet the SERIOUS criteria above, "
        "'not_interested' if they ask to stop or say they don't want this, "
        "'time_waster' if the message meets the TIME_WASTER criteria — off the "
        "business entirely, with no project behind it — otherwise 'browsing'. "
        "When the verdict is 'time_waster' the reply must be the single-line "
        "redirect described above and nothing else. Never mention the verdict in "
        "the reply."
    )
    return "\n".join(p for p in parts if p is not None)


def _build_contents(lead, incoming_text):
    contents = []
    for turn in store.history(lead):
        role = "model" if turn["role"] == "model" else "user"
        contents.append({"role": role, "parts": [{"text": turn["text"]}]})
    contents.append({"role": "user", "parts": [{"text": incoming_text}]})
    return contents


def _retry_after_seconds(resp, attempt):
    """Honour the server's own advice when it gives it, otherwise exponential
    backoff with jitter so concurrent workers don't retry in lockstep."""
    header = resp.headers.get("Retry-After") if resp is not None else None
    if header:
        try:
            return min(60.0, float(header))
        except ValueError:
            pass
    if resp is not None:
        match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', resp.text or "")
        if match:
            return min(60.0, float(match.group(1)))
    return min(60.0, (2 ** attempt) + random.uniform(0, 1.5))


def _quota_id(resp):
    """The one field that makes a 429 actionable, or "".

    Google's 429 body is the same generic "you exceeded your current quota"
    sentence whether you have hit a per-MINUTE burst limit (harmless — the retry
    above clears it) or the per-DAY ceiling (the agent is done until midnight
    Pacific). The difference is only in `error.details[].violations[].quotaId`,
    which reads like `GenerateRequestsPerDayPerProjectPerModel-FreeTier` and
    names the window and the tier outright.

    Without this, a 429 in the log is unfalsifiable: it looks identical in both
    cases, and the wrong guess either wastes an afternoon or lets the client's
    agent sit silent for hours.
    """
    try:
        for detail in (resp.json().get("error") or {}).get("details") or []:
            for violation in detail.get("violations") or []:
                found = violation.get("quotaId") or violation.get("quotaMetric")
                if found:
                    return found
    except Exception:
        pass
    return ""


def _log_usage(payload):
    """Token accounting, one line per call. Worth having because on Gemini 3.x
    the OUTPUT number includes the hidden thinking tokens and output is priced
    ~5x input — so if the bill ever looks wrong, `thinking=` is the first thing
    to look at, and thinkingLevel is the knob."""
    usage = payload.get("usageMetadata") or {}
    if usage:
        log.info("tokens in=%s out=%s (thinking=%s) total=%s",
                 usage.get("promptTokenCount"), usage.get("candidatesTokenCount"),
                 usage.get("thoughtsTokenCount"), usage.get("totalTokenCount"))


def list_models():
    """Every model this key can actually call generateContent on, newest names as
    Google reports them today.

    Exists so nothing in this file has to hard-code a second model id. Google
    retires flash versions on its own schedule — 2.5-flash already went 404 for
    new keys — so a fallback we invented at deploy time would eventually be a
    fallback that fails. Asking is cheap, cached, and always current.

    Also useful by hand when a 404 needs explaining:
        python3 -c "import brain; print(brain.list_models())"
    """
    if not config.GEMINI_API_KEY:
        raise BrainError("GEMINI_API_KEY is not set — add it to .env")
    resp = requests.get(MODELS_URL,
                        headers={"x-goog-api-key": config.GEMINI_API_KEY},
                        timeout=(CONNECT_TIMEOUT, 20))
    if resp.status_code != 200:
        raise BrainError(f"Gemini ListModels {resp.status_code}: {resp.text[:200]}")
    names = []
    for model in resp.json().get("models") or []:
        if "generateContent" in (model.get("supportedGenerationMethods") or []):
            names.append((model.get("name") or "").split("/")[-1])
    return [n for n in names if n]


def _rank(name):
    """Sort key for choosing a stand-in. Cheap and chatty first, exotic last.

    flash-lite ahead of flash on purpose: it is a different capacity pool from
    the model that just refused us, and a two-line reply does not need more.
    Preview and experimental ids sort last because they can be withdrawn without
    notice, which is the one thing a fallback must not do.
    """
    low = name.lower()
    if "flash-lite" in low:
        score = 0
    elif "flash" in low:
        score = 1
    elif "pro" in low:
        score = 3
    else:
        score = 4
    if "preview" in low or "-exp" in low or "experimental" in low:
        score += 5
    return (score, len(low), low)


def _fallback_models(primary):
    """Up to two stand-ins for `primary`, or [] if we can't name one.

    An explicit `gemini_fallback_model` wins (comma-separated for more than one)
    so an operator can pin the chain. Otherwise the list comes from the API and
    is cached — the point of a fallback is to be fast, and this must not add a
    round-trip to every message. A failed lookup is cached too, briefly, so an
    outage doesn't turn into one extra doomed request per inbound message.
    """
    configured = (store.get("gemini_fallback_model") or "").strip()
    if configured:
        return [m.strip() for m in configured.split(",")
                if m.strip() and m.strip() != primary][:2]

    now = time.monotonic()
    cached = _MODEL_CACHE["models"]
    ttl = _MODEL_CACHE_TTL if cached else _MODEL_CACHE_TTL_FAIL
    if cached is None or now - _MODEL_CACHE["at"] > ttl:
        try:
            cached = [n for n in list_models()
                      if not any(bad in n.lower() for bad in _UNSUITABLE)]
        except Exception as e:
            # Never fatal. No fallback is a worse outcome than a wrong one, but
            # both are better than an exception on top of an existing failure.
            log.warning("Could not ask Gemini which models are available (%s) — "
                        "no fallback this time", e)
            cached = []
        _MODEL_CACHE.update(at=now, models=cached)
    return [n for n in sorted(cached, key=_rank) if n != primary][:2]


def _call_gemini(system_instruction, contents, attempts=3):
    """One reply out of Gemini, or BrainError.

    Three layers of patience, because they fail differently:

      * retry the same model on 429/5xx — most overloads clear in seconds;
      * then try a DIFFERENT model, because "this model is currently
        experiencing high demand" is a per-model capacity problem and no amount
        of retrying the same pool fixes it;
      * and give up at a deadline, because on the other side of this call is a
        person watching a typing bubble. Three minutes of loyal retrying reads
        as being ignored, and the owner would rather hear about it than have the
        lead wait.
    """
    if not config.GEMINI_API_KEY:
        raise BrainError("GEMINI_API_KEY is not set — add it to .env")

    body = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
            "temperature": 0.8,
            # Gemini 3.x thinks before answering, and those hidden thinking
            # tokens are charged against maxOutputTokens. At the old ceiling of
            # 400 the model can spend the whole budget reasoning and return
            # ZERO text parts with finishReason=MAX_TOKENS — a dead-silent
            # agent that looks like a bad key. So: keep thinking short, and
            # leave far more headroom than a 2-sentence reply needs. Nothing is
            # billed for headroom we don't use.
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    headers = {"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"}
    read_timeout = max(5, store.get_int("gemini_timeout_seconds"))
    deadline = time.monotonic() + max(10, store.get_int("gemini_deadline_seconds"))

    primary = (store.get("gemini_model") or "").strip()
    models = [primary]
    last_error = None
    expanded = False
    index = 0

    while index < len(models):
        model = models[index]
        index += 1
        for attempt in range(attempts):
            resp = None
            try:
                resp = requests.post(GEMINI_URL.format(model=model), headers=headers,
                                     json=body, timeout=(CONNECT_TIMEOUT, read_timeout))
            except requests.RequestException as e:
                last_error = f"{model}: network error: {e}"
            else:
                if resp.status_code == 200:
                    payload = resp.json()
                    _log_usage(payload)
                    if model != primary:
                        log.warning("Answered on fallback model %s — %s was unavailable",
                                    model, primary)
                    return payload
                last_error = f"{model}: Gemini {resp.status_code}: {resp.text[:200]}"
                if resp.status_code == 429:
                    # Name the quota, because the message body alone cannot tell
                    # a per-minute burst from a spent day — see _quota_id.
                    quota = _quota_id(resp)
                    if quota:
                        last_error = f"{model}: Gemini 429 on quota {quota}"
                if resp.status_code == 404:
                    log.error("Gemini has no model called %r — trying another. "
                              "Run brain.list_models() to see the current ids.", model)
                    break
                if resp.status_code not in _RETRYABLE:
                    # Bad key, bad request, wrong project. Another model would
                    # fail identically, so surface it now instead of burning the
                    # whole chain hiding it.
                    raise BrainError(f"Gemini {resp.status_code}: {resp.text[:300]}")

            if attempt == attempts - 1:
                break
            wait = _retry_after_seconds(resp, attempt)
            if time.monotonic() + wait >= deadline:
                raise BrainError(f"Gemini ran out of time ({last_error})")
            log.warning("%s — retrying in %.1fs (attempt %d/%d)",
                        last_error, wait, attempt + 1, attempts)
            time.sleep(wait)

        if not expanded:
            expanded = True
            if time.monotonic() >= deadline:
                break
            extra = _fallback_models(primary)
            if extra:
                log.warning("%s is not answering (%s) — asking %s instead",
                            primary, last_error, ", ".join(extra))
                models.extend(extra)

    raise BrainError(f"Gemini failed on {', '.join(models)}: {last_error}")


def _parse(payload):
    """Pull {intent, reply} out of Gemini's response, tolerating the ways it can
    come back empty (safety block, truncation) rather than crashing."""
    candidates = payload.get("candidates") or []
    if not candidates:
        reason = (payload.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise BrainError(f"Gemini returned nothing ({reason})")

    candidate = candidates[0]
    text = "".join(p.get("text", "") for p in (candidate.get("content") or {}).get("parts") or [])
    if not text.strip():
        finish = candidate.get("finishReason")
        if finish == "MAX_TOKENS":
            thoughts = (payload.get("usageMetadata") or {}).get("thoughtsTokenCount")
            raise BrainError(
                "Gemini spent the whole token budget thinking and returned no reply "
                f"(finishReason=MAX_TOKENS, thinking tokens={thoughts}). Raise "
                "maxOutputTokens or lower thinkingLevel in brain.py.")
        raise BrainError(f"Gemini returned empty text (finishReason={finish})")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise BrainError(f"Gemini did not return valid JSON: {text[:300]}") from e

    reply = (data.get("reply") or "").strip()
    intent = (data.get("intent") or "browsing").strip().lower()
    if not reply:
        raise BrainError("Gemini returned an empty reply")
    if intent not in INTENTS:
        log.warning("Unknown intent %r from model — treating as 'browsing'", intent)
        intent = "browsing"
    return {"reply": reply, "intent": intent}


def _keyword_intent(text):
    """Fallback verdict for the OpenClaw path, which only returns prose. This is
    the old keyword rule — kept only so switching brains never loses escalation."""
    low = (text or "").lower()
    for keyword in config.ESCALATION_KEYWORDS:
        if keyword in low:
            return "serious"
    return "browsing"


def get_reply(lead, incoming_text, name=None):
    """The one function the rest of the app calls.

    Returns {"reply": str, "intent": "serious"|"browsing"|"not_interested"|
    "time_waster"}.
    Records both sides of the exchange so the next message has context.
    Raises BrainError if no reply could be produced — the caller decides
    whether to send a fallback line or stay quiet.
    """
    lead = str(lead)
    which = (store.get("brain") or "gemini").strip().lower()

    if which == "openclaw":
        # Legacy path. Kept as an escape hatch: if Gemini has an outage or the
        # key is wrong, flipping this one setting in the dashboard restores
        # service without a deploy.
        from openclaw_client import get_reply as openclaw_reply, OpenClawError
        try:
            reply = openclaw_reply(lead, incoming_text)
        except OpenClawError as e:
            raise BrainError(f"OpenClaw failed: {e}") from e
        result = {"reply": reply.strip(), "intent": _keyword_intent(incoming_text)}
    else:
        contents = _build_contents(lead, incoming_text)
        payload = _call_gemini(_system_instruction(name), contents)
        result = _parse(payload)

    store.append_turn(lead, "user", incoming_text)
    store.append_turn(lead, "model", result["reply"])
    # Mark the cut explicitly. Without the ellipsis an 80-char slice reads like
    # the model truncated mid-sentence, which sends you hunting a bug that
    # isn't there.
    shown = result["reply"] if len(result["reply"]) <= 80 else result["reply"][:79] + "…"
    log.info("Brain (%s) -> intent=%s reply=%r", which, result["intent"], shown)
    return result

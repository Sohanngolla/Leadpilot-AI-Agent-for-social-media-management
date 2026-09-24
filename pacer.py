"""
pacer.py — the outbound queue that makes the agent feel like a person.

Three separate problems, one mechanism:

1. The client wants leads to believe a human is replying. Instant replies are
   the single biggest tell that it's a bot, so the FIRST reply to someone waits
   a randomised 25–75s — and the message is left on UNREAD for that whole wait,
   because blue ticks the second a message arrives followed by a minute of
   silence is just as robotic. Read receipt, typing bubble and reply all land
   together at the end, the way they do when a person actually picks up their
   phone. Once the conversation is running the wait drops to a few seconds: a
   person already typing to you answers quickly, and stretching every turn to a
   minute makes the chat unusable. See _think_delay.
2. Gemini's free tier throttles per MINUTE, and a burst of inbound messages was
   what produced the 429s earlier. A hard cap on AI calls/minute removes that
   class of failure entirely instead of relying on retries.
3. WhatsApp penalises numbers that behave mechanically. Spread-out, uneven
   sending is exactly what keeps the quality rating green.

Two design details that matter more than they look:

* **One reply per person, not per message.** If a lead fires off three
  messages while we're "thinking", we fold them into one prompt and answer
  once — which is what a human does, and it also cuts AI calls.
* **Mute is re-checked after the delay.** The owner may take the conversation
  over during those 60 seconds; if so the bot must go quiet mid-flight.

Every knob is read live from store.py, so the client can slow the agent down
from the dashboard while it is running.
"""
import logging
import queue
import random
import threading
import time
from collections import deque

import brain
import channel
import store

log = logging.getLogger("whatsapp-relay.pacer")


class _RateGate:
    """Sliding-window limiter on AI calls, shared by all workers.

    The cap is re-read on every acquire so lowering it in the dashboard takes
    effect immediately. Sleeping happens outside the lock, otherwise one
    waiting worker would block the others from even checking.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stamps = deque()

    def acquire(self):
        while True:
            with self._lock:
                cap = max(1, store.get_int("brain_max_per_minute"))
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= 60:
                    self._stamps.popleft()
                if len(self._stamps) < cap:
                    self._stamps.append(now)
                    return
                wait = 60 - (now - self._stamps[0]) + 0.05
            log.info("AI rate cap reached (%d/min) — holding %.1fs", cap, wait)
            _sleep(wait)


class _Job:
    """One pending conversation. `texts` grows if more messages arrive before
    we start generating."""

    __slots__ = ("lead", "name", "texts", "message_id", "queued_at")

    def __init__(self, lead, name, text, message_id):
        self.lead = lead
        self.name = name
        self.texts = [text]
        self.message_id = message_id
        self.queued_at = time.monotonic()

    def prompt(self):
        return "\n".join(t for t in self.texts if t)


_gate = _RateGate()
_lock = threading.Lock()
_pending: "dict[str, _Job]" = {}
_leads: "queue.Queue[str]" = queue.Queue()
_workers: "list[threading.Thread]" = []

# Test seam: the hermetic suite swaps these for fakes instead of monkeypatching
# modules, so a test can never accidentally hit the real Graph API.
_sleep = time.sleep


def submit(lead, text, name=None, message_id=None):
    """Queue an inbound message for a paced reply. Returns "queued" or "merged".

    Cheap and non-blocking: safe to call straight from the webhook handler.
    """
    lead = str(lead)
    with _lock:
        job = _pending.get(lead)
        if job is not None:
            job.texts.append(text)
            job.message_id = message_id or job.message_id
            job.name = name or job.name
            log.info("Folded a second message from %s into the pending reply", lead)
            return "merged"
        _pending[lead] = _Job(lead, name, text, message_id)
    _ensure_workers()
    _leads.put(lead)
    return "queued"


def _ensure_workers():
    """Start workers on first use and grow if the client raises `reply_workers`.

    It never shrinks — dropping the count takes effect on restart. Idle threads
    cost nothing (they're blocked on the queue), so this isn't worth the
    complexity of cancellation.
    """
    want = max(1, store.get_int("reply_workers"))
    with _lock:
        alive = [t for t in _workers if t.is_alive()]
        _workers[:] = alive
        for i in range(len(alive), want):
            t = threading.Thread(target=_worker, args=(i,), name=f"pacer-{i}", daemon=True)
            _workers.append(t)
            t.start()
        if want > len(alive):
            log.info("Reply workers running: %d", want)


def _worker(index):
    while True:
        lead = _leads.get()
        try:
            _process(lead)
        except Exception:
            # A worker must never die: one bad conversation would silently
            # reduce capacity for every future lead.
            log.exception("Reply worker %d failed on %s — continuing", index, lead)
            with _lock:
                _pending.pop(lead, None)
        finally:
            _leads.task_done()


def _think_delay(lead=None):
    """How long to sit on a reply before anything is shown to the lead.

    The 25–75s wait is what keeps a FIRST reply from looking machine-generated —
    nobody answers an unknown number in two seconds. Applying it to every turn
    was the wrong lesson to draw from that: once someone is mid-conversation, a
    minute of silence between each line is not human, it is a chat that feels
    broken. So a lead who already has history gets
    `reply_delay_followup_seconds` instead, jittered ±40% so consecutive turns
    are never identical to the second.

    Reading history here is safe and means what it looks like: brain.get_reply
    is what appends both turns, and it has not run yet, so an empty history is
    genuinely first contact. A cold template writes no turn either, so a lead
    answering our opening message still gets the slow, human-looking reply.
    """
    if lead is not None and store.history(lead, limit=1):
        base = max(0, store.get_int("reply_delay_followup_seconds"))
        return random.uniform(base * 0.6, base * 1.4)
    lo = store.get_int("reply_delay_min_seconds")
    hi = store.get_int("reply_delay_max_seconds")
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(max(0, lo), max(0, hi))


def _typing_pause(reply, already_elapsed):
    """Roughly how long a person takes to thumb-type this, minus the time the
    model already spent generating (the typing bubble is showing during that)."""
    target = min(12.0, len(reply or "") / 14.0)
    return max(0.0, target - already_elapsed)


_BRAIN_ALERT_GAP = 900          # don't nag the owner more than once per 15 min
_last_brain_alert = None        # None, not 0.0 — see below


def _alert_owner_brain_down(lead, error):
    """If the AI can't answer we stay SILENT to the customer rather than sending
    a robotic apology — but the owner gets told so a human can step in. Throttled,
    because an outage would otherwise mean one alert per inbound message.

    The sentinel has to be None: time.monotonic() is seconds-since-boot, so
    starting from 0.0 would swallow the very first alert on any machine that had
    been up for less than 15 minutes — i.e. exactly a freshly deployed container.
    """
    global _last_brain_alert
    now = time.monotonic()
    if _last_brain_alert is not None and now - _last_brain_alert < _BRAIN_ALERT_GAP:
        return
    _last_brain_alert = now
    try:
        import jobs
        jobs.notify_owner(f"Agent could not reply to {lead} — AI error: {str(error)[:200]}")
    except Exception as e:
        log.warning("Could not alert owner about brain failure: %s", e)


def _process(lead):
    with _lock:
        job = _pending.get(lead)
    if job is None:
        return

    # Stay INVISIBLE for the whole pause. The read receipt used to fire the
    # instant the webhook landed, so the lead saw blue ticks immediately and then
    # a reply a minute later — which is precisely what a bot looks like: seen
    # instantly, answered by a machine. A person picks up the phone, reads,
    # types and sends in one burst. So nothing at all happens until the pause is
    # over, and then the ticks, the typing bubble and the reply arrive together.
    delay = _think_delay(lead)
    log.info("Reply to %s in %.0fs (left on unread until then)", lead, delay)
    _sleep(delay)

    # Pop LAST, so anything that arrived during the pause is answered in one go.
    with _lock:
        job = _pending.pop(lead, None)
    if job is None:
        return

    if not store.get_bool("reply_auto"):
        log.info("reply_auto is off — not replying to %s", lead)
        return
    if store.is_muted(lead):
        log.info("%s was taken over by a human during the pause — staying quiet", lead)
        return

    # Gate BEFORE the ticks: Meta drops the typing bubble after ~25s, so a wait
    # for the AI rate cap must not burn that window.
    _gate.acquire()
    if job.message_id:
        # One API call carries both, so the ticks and the "typing…" bubble
        # appear at the same instant. With the indicator switched off the lead
        # still gets a read receipt — just not a minute early. Instagram has
        # neither, so this is a no-op there (see channel.show_reading).
        channel.show_reading(lead, job.message_id,
                             typing=store.get_bool("typing_indicator"))

    started = time.monotonic()
    try:
        result = brain.get_reply(lead, job.prompt(), name=job.name)
    except Exception as e:
        # Deliberately broad. BrainError is the expected failure, but an
        # unexpected one (bad payload shape, library change) must not silently
        # drop the lead — the owner has to hear about it either way.
        log.error("Brain failed for %s: %s", lead, e)
        _alert_owner_brain_down(lead, e)
        return

    _sleep(_typing_pause(result["reply"], time.monotonic() - started))

    try:
        channel.send_text(lead, result["reply"])
    except Exception as e:
        detail = getattr(getattr(e, "response", None), "text", str(e))
        log.error("Failed to send reply to %s: %s", lead, detail)
        return
    store.record_send(lead, "reply")

    try:
        import escalation
        escalation.review(lead, job.name, job.prompt(), result["intent"], result["reply"])
    except Exception:
        log.exception("Escalation review failed for %s (reply was still sent)", lead)


def status():
    """For the dashboard: what the queue is doing right now."""
    with _lock:
        pending = len(_pending)
        workers = sum(1 for t in _workers if t.is_alive())
    return {
        "queued_conversations": pending,
        "workers": workers,
        "ai_calls_last_minute": len(_gate._stamps),
        "reply_delay": f'{store.get_int("reply_delay_min_seconds")}-{store.get_int("reply_delay_max_seconds")}s',
        "follow_up_delay": f'~{store.get_int("reply_delay_followup_seconds")}s',
    }

"""
HTML for the client control panel. Pure Python on purpose — no Jinja, no template
files, nothing extra for the Dockerfile to copy or for a deploy to forget.

Kept apart from dashboard.py so the routing/auth logic stays readable: this file
only turns state into markup, and never touches the database or the network. The
one import beyond stdlib is channel.short_label, which is deliberately the
lookup-free variant — see channel.py.

VISUAL DIRECTION
The panel is now the same object as the studio's public site: identical type (Fraunces /
Inter / IBM Plex Mono), identical palette (stone, ink, brass), identical motion
curve (cubic-bezier(.16,1,.3,1)), and the site's signature devices reused rather
than imitated — the brass eyebrow with its glowing dot, the section rule that
draws itself to 64px, pill buttons with the fill sweeping up from below, the
crop-mark corners, the reveal-on-scroll with blur, the scroll-progress hairline
and the ambient cursor glow. The client sees their own brand when they open the
desk, not a developer tool bolted to the side of it.

The one deliberate departure: the masthead is a dark panel. The site uses
`.on-dark` for the sections it wants read first, and on this page the thing to
read first is today's numbers.

HOW IT STAYS HONEST WITH JAVASCRIPT OFF
Everything on this page is server-rendered and every action is a plain
urlencoded form, because dashboard.py parses bodies with urllib rather than
python-multipart. JavaScript only adds: the tab switcher (panels are all visible
without it), live counters (the numbers are already correct on load), the search
box and filter chips (the full list is already rendered), the sticky save bar
(a plain button at the end of the form without it), the guard-rail verdicts
under the outreach fields (already rendered server-side by _num_rows), and the
wheel guard on number inputs. Nothing is only reachable through script.

TWO RULES THAT MUST SURVIVE ANY EDIT
1. Every checkbox lives inside the single settings form. dashboard.apply_settings
   reads an absent key as false, so a switch rendered outside that form would be
   silently turned off on the next save. Tab panels are *inside* the form for
   exactly this reason — a hidden field still posts, a field outside the form
   does not.
2. The number-input wheel guard stays. See _GUARDS, which is also the only script
   preview/test_panel_script.js can run — keep it free of anything but
   querySelectorAll and window.confirm.
"""
import html as _html
import json as _json
import os as _os
from datetime import datetime as _dt

import channel

# --- Brand ------------------------------------------------------------------
# The business name shown throughout the panel is read from the environment so
# this codebase carries only a generic placeholder. A real deployment sets its
# own name in .env (BRAND_NAME); nothing here is client-specific.
_BRAND = (_os.environ.get("BRAND_NAME") or "Dunder Mifflin").strip() or "Dunder Mifflin"
_bp = _BRAND.upper().split(None, 1)
_BRAND_MAIN, _BRAND_REST = _bp[0], (_bp[1] if len(_bp) > 1 else "")

# --- Field metadata ---------------------------------------------------------
# Labels are written from the client's side of the screen: what they control, not
# how it is stored. The store.py key is an implementation detail they never see.

SWITCHES = [
    ("reply_auto", "Answer new messages",
     "When off, messages still arrive and are logged. The agent just stays quiet."),
    ("followup_auto", "Follow up on silence",
     "Nudges leads who never replied, up to the limit set under Outreach."),
    ("cold_auto", "Message new leads first",
     "Sends the opening template to leads in the sheet nobody has contacted yet."),
    ("typing_indicator", "Show typing dots",
     "Marks the message read and shows typing before the reply lands."),
    ("escalation_mutes_bot", "Go quiet after handoff",
     "Off by default: you get the alert and the agent keeps the lead warm until "
     "you step in. Turn on only if you want it silent the moment a lead goes hot."),
]

PACING = [
    ("reply_delay_min_seconds", "Wait at least", "seconds"),
    ("reply_delay_max_seconds", "and at most", "seconds"),
    ("reply_delay_followup_seconds", "Reply within, once chatting", "seconds"),
    ("reply_workers", "Conversations at once", "threads"),
    ("brain_max_per_minute", "AI replies per minute", "ceiling"),
    ("gemini_timeout_seconds", "Give the AI at most", "seconds to answer"),
]

OUTREACH = [
    ("cold_daily_cap", "New leads per day", "hard ceiling"),
    ("cold_batch_limit", "Sent per batch", "leads"),
    ("cold_pacing_seconds", "Gap between sends", "seconds"),
    ("cold_window_start_hour", "Only reach out from", "o’clock (24h, IST)"),
    ("cold_window_end_hour", "…and stop reaching out at", "o’clock (24h, IST)"),
    ("followup_after_hours", "Follow up after", "hours of silence"),
    ("followup_max", "Follow-ups per lead", "maximum"),
    ("scheduler_interval_seconds", "Check for work every", "seconds"),
    ("off_topic_strikes_max", "Stop time-wasters after", "off-topic messages"),
]

TEXTS = [
    ("system_prompt", "How the agent talks",
     "The agent's whole personality and rules. Edits apply to the very next message."),
    ("escalation_criteria", "When to hand a lead to you",
     "The agent judges every reply against this and alerts you when it matches."),
    ("handoff_message", "What it says when handing over",
     "The last thing the lead hears from the agent before you take the conversation."),
]

# The browser stops typing here and dashboard.py truncates on the way in, so the
# limit lives with the form that shows it. dashboard.MAX_TEXT reads this.
MAX_TEXT = 8000

# Clamps, not suggestions. reply_workers spawns threads and cold_daily_cap is the
# only thing standing between an enthusiastic client and a banned number.
#
# These used to live in dashboard.py, which is where they are still enforced —
# dashboard.RANGES is this dict. They moved here because the fields now carry
# their own floor and ceiling in the markup, and a stepper button that stops one
# short of what the server accepts (or one past it) is worse than no stepper: the
# client clicks down to 0 workers, saves, and the server quietly writes 1. One
# table, read by the form that draws the field and by the code that clamps it.
RANGES = {
    "reply_delay_min_seconds": (0, 3600),
    "reply_delay_max_seconds": (0, 3600),
    "reply_delay_followup_seconds": (0, 3600),
    "reply_workers": (1, 16),
    "brain_max_per_minute": (1, 120),
    # Below ~10s a slow-but-real answer gets thrown away for nothing; above ~90s
    # the lead has already watched the typing bubble expire and given up.
    "gemini_timeout_seconds": (5, 180),
    "cold_daily_cap": (0, 1000),
    "cold_batch_limit": (1, 200),
    "cold_pacing_seconds": (0, 3600),
    # Hours of the day, 24h IST. 0-23; set both to the same value to switch the
    # quiet-hours window off entirely (outreach at any hour).
    "cold_window_start_hour": (0, 23),
    "cold_window_end_hour": (0, 23),
    "followup_after_hours": (1, 720),
    "followup_max": (0, 10),
    "scheduler_interval_seconds": (30, 86400),
    # 0 means never cut anyone off. 1 would stop a conversation over a single
    # stray message, which is why the client can set it but rarely should.
    "off_topic_strikes_max": (0, 20),
}

# How much one press of a stepper button moves the field. Presentation only —
# nothing on the server reads it — but it is the difference between a control that
# feels made for the field and one that takes forty clicks to cross its range.
# Sized off the default: a nudge of the number the client will actually be sitting
# on, never so coarse that the value they want is unreachable by clicking.
STEPS = {
    "reply_delay_min_seconds": 5,
    "reply_delay_max_seconds": 5,
    "reply_delay_followup_seconds": 1,
    "reply_workers": 1,
    "brain_max_per_minute": 1,
    "gemini_timeout_seconds": 5,
    "cold_daily_cap": 10,
    "cold_batch_limit": 5,
    "cold_pacing_seconds": 5,
    "cold_window_start_hour": 1,
    "cold_window_end_hour": 1,
    "followup_after_hours": 6,
    "followup_max": 1,
    "scheduler_interval_seconds": 60,
    "off_topic_strikes_max": 1,
}

# --- Guard rails ------------------------------------------------------------
# The dangerous fields on this page are not dangerous to the server — they are
# dangerous to the phone number. Meta allows a number without connected status
# and an approved display name 250 business-initiated conversations in a rolling
# 24 hours, and only lifts that (250 -> 2,000 -> 10,000 -> ...) while the quality
# rating holds. So a client who types 500 here does not get 500 conversations; he
# gets 250 sends, a pile of failures, and a rating that a few days of unanswered
# cold openers can push into restriction.
#
# The clamp in dashboard.RANGES still permits up to 1000, deliberately: a number
# on a higher tier one day should not need a developer to raise a ceiling. What
# stops an accident is being told what the number costs, in the moment of typing
# it — a plain sentence under the field, and a confirm box before the save goes
# through. Advice, not a lock.
#
# Each band is (upper_bound_inclusive, level, sentence); None means open-ended,
# and level is "" (fine), "warn" (say it), or "stop" (also confirm on save).
BANDS = {
    "cold_daily_cap": [
        (0, "", "Off. New leads in the sheet are left alone."),
        (20, "", "Warm-up range. This is the right number for the first week."),
        (50, "", "Comfortable, and well inside what a new number is allowed."),
        (150, "warn", "Past warm-up. Only come here once WhatsApp Manager has "
                      "shown a green quality rating for a few days."),
        (250, "warn", "At the ceiling Meta gives a new number: 250 conversations "
                      "a day. At this volume a handful of blocks is enough to "
                      "drop the quality rating."),
        (None, "stop", "Over Meta's limit of 250 business-initiated conversations "
                       "in 24 hours. Everything past 250 is refused, and the "
                       "quality rating falls — this is how a number gets "
                       "restricted. Only set this once Meta has raised the tier."),
    ],
    "cold_batch_limit": [
        (50, "", ""),
        (None, "warn", "A batch this big is only safe if the gap below keeps it "
                       "spread out. Sent back to back it reads as a burst."),
    ],
    "cold_pacing_seconds": [
        (0, "warn", "No gap: the whole batch leaves at once. A burst of first "
                    "messages to strangers is the clearest spam signal there is."),
        (9, "warn", "Under ten seconds is close enough to a burst that it is not "
                    "worth the risk. 45 seconds is the default for a reason."),
        (None, "", ""),
    ],
}


def band_for(key, value):
    """(level, sentence) for a value, or ("", "") when the field has no bands."""
    bands = BANDS.get(key)
    if not bands:
        return "", ""
    try:
        n = float(str(value).strip())
    except (TypeError, ValueError):
        return "", ""
    for hi, level, message in bands:
        if hi is None or n <= hi:
            return level, message
    return "", ""

# --- Brand mark -------------------------------------------------------------
# The monogram lifted out of the client's logo file and masked to a circle. The
# wordmark under it in the original is dropped: it would sit two inches from the
# same words set in the nav's own display face.
#
# Inlined as a data URI rather than served as a file because this app has no
# static directory — a /static route plus a compose volume for one image is more
# moving parts than the image is worth, and a data URI cannot 404 after a
# rebuild. Drawn at 144px for a 36px slot: 72px looked soft on a 3x phone
# screen, where the slot really wants ~108px of pixels. Quantised to 96 colours,
# which costs 5.9KB instead of 32KB and is indistinguishable at this size — the
# circle's edge is masked at 4x and downsampled, so it has no jaggies.
LOGO_MARK = ("data:image/png;base64,"
             "iVBORw0KGgoAAAANSUhEUgAAAJAAAACQCAMAAADQmBKKAAABIFBMVEXi182xpJbdyarj2c6TdlLAnWxcUkWDYD12Wjjb0cazkGF2"
             "WTfHu67d0shwW0SjiGazppixo5XXyrjRxblVOx5BKRKEYzuRcUyFY0OvopTDs5/MvKhxTy92WDaQemCrjmbSx7tXPSLFp3zCtajl"
             "29Dq4Nbq4Nbd0sjSxbiXdky0lW3HuavYzcKohVi2p5Z0VzcyLCKLa0WpiWWpmYg6MidHOivl29FoSSqXh3WhfVBTRDHItJksJRt2"
             "ZVGKemjy6+LCrJLFpHe9saXe08i6raH69OpqWkeIZzzk2s9SOBzk2tDm1Lnk2s/Rxbnl29Hl29Gdk4bFuKzq4NZ2bGHp4NXq4NYk"
             "HBJ7cmjq4NUbFg+dgFq8oHnXzcLd08hWTUKbjoFathwlAAAAYHRSTlNfFPyMDf///+sn9xaUyAoEYqgbtBT/Dorryh9QXaxgnVhl"
             "Bcj+AP7+/v7+/v7+/v7//v/+//4F/v7+//7//v7//v/+Bf7//v7P/o3/TAQssv4FLf9L0P//s////gKw///w8LyEAAAVUklEQVR4"
             "2rWch2LbuLKGaTs9287unq2n3XshEuwiqUJJJFWs4po4dmynbJL3f4s7A4AkSELNZxeJ5W5+mvlnMBhA1FoPGL1ej72/fqsdnd3e"
             "3uo6YePk5OTsTLt41RM/1HvA39b2hhHv3x69RhD2Dx/kcfL67KInk/9lQOICb4+ARQzCH5vjhEPty6Ttb9N3enUQJQ8brzXO9NcB"
             "XdRpNgMhE9ppD6R9gK41XTXKi68B017tgaTtLOW37/R1PJssJMy0M5K2Y2Ctw9G3wuyJpO2Ec/1Or4WVro9tojcCfhPSbo7byWVn"
             "KssspjaD28VlfJz1yjT2UCD8E1rFP3niGeuPLUJ29xmXd2+rkbRtPK9O9Ka70ECHU4syvj2AyMmrbUba5rKjdVrWp1PLE8EurKTv"
             "6Lfew4Gub9fhLM4t63FIOcjO4S+M1Hso0FtFUi4MZFm5iYi+n5QuNglJ2+YuQlQGGoOBkMgxhGH2CDZw2wYhbQB6x7OM2mMjBhTa"
             "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")


# --- Style ------------------------------------------------------------------
# Tokens copied from the live site's css/style.css rather than re-picked, so the
# two never drift apart: if the brand changes there, the same six hex values
# change here. --live / --flag / --caution are the only additions, because a
# marketing site has no "this is on fire" state and a control panel needs one.
CSS = """
:root{
  color-scheme:light;
  --stone:#F6F6F3;   --stone-deep:#ECECE7;
  --ink:#0A0B0C;     --ink-soft:#64676A;
  --line:rgba(10,11,12,.10);
  --dark:#08090B;    --dark-2:#0F1113;
  --dark-line:rgba(255,255,255,.10);
  --paper-line:#F1F1EE;
  --brass:#A9814F;   --brass-soft:#E3C795;
  --brass-glow:rgba(169,129,79,.35);
  --live:#2E7A5E;    --flag:#9E3B2A;    --caution:#8A5514;
  --font-display:'Fraunces',Georgia,'Times New Roman',serif;
  --font-body:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  --font-mono:'IBM Plex Mono',ui-monospace,'SF Mono',SFMono-Regular,Menlo,monospace;
  --wrap:1320px;
  --ease:cubic-bezier(.16,1,.3,1);
  --ease-soft:cubic-bezier(.22,1,.36,1);
  --dur-s:.35s; --dur-m:.6s; --dur-l:1.1s;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
/* One flat sheet of near-white read as unfinished — cards at #fff on a #F6F6F3
   ground are three points apart, which is no contrast at all. The ground is now
   the deeper stone, with two soft washes laid over it (brass from the right,
   light from the left) so the cards sit on a shade of their own and the page has
   somewhere to recede to. Both washes are decoration: if the gradients are not
   understood the ground is still a solid, correct colour. */
body{
  margin:0;color:var(--ink);background:var(--stone-deep);
  background-image:
    radial-gradient(1180px 640px at 84% 300px,rgba(169,129,79,.06),rgba(169,129,79,0) 68%),
    radial-gradient(980px 580px at 2% 900px,rgba(255,255,255,.82),rgba(255,255,255,0) 66%);
  background-repeat:no-repeat;background-attachment:scroll;
  font-family:var(--font-body);font-size:15px;line-height:1.6;
  -webkit-font-smoothing:antialiased;overflow-x:hidden;
  animation:pageFadeIn .45s var(--ease) both;
}
@keyframes pageFadeIn{from{opacity:0}to{opacity:1}}
/* iOS Safari does two things Android does not: it widens the layout viewport to
   fit anything that overflows it — so one over-wide element zooms the whole page
   out — and it inflates body text when the phone is turned to landscape. Both
   are pinned here. `clip` rather than `hidden` so this can never quietly turn
   <html> into a scroll container and break position:sticky further down. */
html{overflow-x:clip;-webkit-text-size-adjust:100%;text-size-adjust:100%}
h1,h2,h3{margin:0;font-family:var(--font-display);font-weight:500;letter-spacing:-.015em}
p{margin:0}
a{color:inherit;text-decoration:none}
img{max-width:100%;display:block}
::selection{background:var(--ink);color:var(--stone)}
:focus-visible{outline:2px solid var(--brass);outline-offset:3px}
.wrap{max-width:var(--wrap);margin:0 auto;padding:0 34px}
@media (max-width:640px){.wrap{padding:0 18px}}

/* The site's brass eyebrow, dot and glow included — used here as the label on
   every card and section, so the panel is read in the same voice as the site. */
.eyebrow{display:flex;align-items:center;gap:9px;font-family:var(--font-mono);
  font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--brass);
  margin:0 0 14px}
.eyebrow::before{content:'';width:7px;height:7px;border-radius:50%;flex:none;
  background:var(--brass);box-shadow:0 0 0 4px var(--brass-glow)}

/* --- Global chrome: injected by script, purely decorative ---------------- */
.scroll-progress{position:fixed;top:0;left:0;height:2px;width:0;z-index:999;
  background:linear-gradient(90deg,var(--brass),var(--brass-soft));
  box-shadow:0 0 10px var(--brass-glow);transition:width .08s linear}
.cursor-glow{position:fixed;top:0;left:0;width:520px;height:520px;
  margin:-260px 0 0 -260px;pointer-events:none;z-index:0;opacity:0;
  will-change:transform;transition:opacity .4s ease;
  background:radial-gradient(circle,var(--brass-glow) 0%,rgba(169,129,79,0) 70%)}
.cursor-glow.is-active{opacity:.5}
@media (max-width:980px),(pointer:coarse){.cursor-glow{display:none}}

/* --- Nav: frosted, compresses on scroll, and stays put ------------------- */
/* The site's nav slides away on the way down. Here it carries the view tabs,
   so a nav that leaves takes the navigation with it. On a desk the bar is worth
   its 66px. It is also the page's lightest band, sitting a shade above the
   deeper ground below it. */
.site-nav{position:sticky;top:0;z-index:50;background:rgba(250,250,248,.9);
  backdrop-filter:saturate(150%) blur(14px);border-bottom:1px solid var(--line);
  transition:box-shadow var(--dur-s) var(--ease)}
.site-nav.is-scrolled{box-shadow:0 14px 38px -24px rgba(10,11,12,.4)}
.site-nav .wrap{display:flex;align-items:center;
  gap:20px;height:78px;transition:height var(--dur-s) var(--ease)}
.site-nav.is-scrolled .wrap{height:66px}
.logo{flex:none;margin-right:auto;display:flex;align-items:center;gap:11px;
  font-family:var(--font-display);font-size:19px;
  letter-spacing:.02em;white-space:nowrap}
/* The mark carries the cream paper it was drawn on, so against a near-white nav
   it needs the hairline to read as a badge instead of a smudge. flex:none
   matters: an <img> is a flex item with an intrinsic width, and left to shrink on
   a narrow phone a round one goes visibly oval. */
.logo-mark{flex:none;width:36px;height:36px;border-radius:50%;
  border:1px solid var(--line);object-fit:cover;
  transition:transform var(--dur-s) var(--ease)}
.logo:hover .logo-mark{transform:scale(1.06)}
/* Scoped to the direct child: .logo span would now also match .logo-type and
   turn the whole wordmark brass, the lead word included. */
.logo-type>span{color:var(--brass)}
.logo s{display:block;text-decoration:none;font-family:var(--font-mono);
  font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;color:var(--ink-soft);
  margin-top:2px}
.nav-right{display:flex;align-items:center;gap:14px;flex:none}
.nav-sep{flex:none;width:1px;height:22px;background:var(--line)}
/* Nothing to separate from with the script off: the tabs are not there and the
   panels are all on the page at once. */
body:not(.js) .nav-sep{display:none}

/* Live/paused pill. The dot breathes only while the agent is actually answering,
   so "is it on?" is legible from across a desk without reading a word. */
.state{display:inline-flex;align-items:center;gap:8px;font-family:var(--font-mono);
  font-size:10.5px;letter-spacing:.15em;text-transform:uppercase;color:var(--live);
  border:1px solid currentColor;border-radius:999px;padding:6px 13px;white-space:nowrap}
.state.off{color:var(--flag)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none;
  box-shadow:0 0 0 3px rgba(46,122,94,.18)}
.state.off .dot{box-shadow:0 0 0 3px rgba(158,59,42,.18)}
@media (prefers-reduced-motion:no-preference){
  .state:not(.off) .dot{animation:breathe 2.4s var(--ease-soft) infinite}
}
@keyframes breathe{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.82)}}
.clock{font-family:var(--font-mono);font-size:11px;color:var(--ink-soft);
  letter-spacing:.08em;font-variant-numeric:tabular-nums}
/* The nav now carries the view tabs as well, so the clock — the one thing on it
   nobody navigates by — is the first item given up for room. */
@media (max-width:1200px){.clock{display:none}}

/* --- Buttons: the site's pill with the fill sweeping up from below -------- */
.btn{display:inline-flex;align-items:center;gap:9px;position:relative;isolation:isolate;
  overflow:hidden;border:1px solid var(--ink);border-radius:999px;padding:12px 22px;
  font-family:var(--font-mono);font-size:11px;letter-spacing:.09em;text-transform:uppercase;
  background:transparent;color:var(--ink);cursor:pointer;
  transition:color var(--dur-s) var(--ease),border-color var(--dur-s) var(--ease),
             transform var(--dur-s) var(--ease),box-shadow var(--dur-s) var(--ease)}
.btn::before{content:'';position:absolute;inset:0;z-index:-1;background:var(--ink);
  transform:translateY(102%);transition:transform .45s var(--ease);border-radius:inherit}
.btn:hover{color:var(--stone);box-shadow:0 14px 30px -14px rgba(0,0,0,.35)}
.btn:hover::before{transform:translateY(0)}
.btn-solid{background:var(--ink);color:var(--stone)}
.btn-solid::before{background:var(--brass)}
.btn-solid:hover{border-color:var(--brass);color:#fff}
.btn-arrow{display:inline-block;transition:transform var(--dur-s) var(--ease)}
.btn:hover .btn-arrow{transform:translateX(5px)}
.btn-mini{padding:8px 15px;font-size:10px}
.btn-danger{border-color:var(--flag);color:var(--flag)}
.btn-danger::before{background:var(--flag)}
.btn-danger:hover{color:#fff;border-color:var(--flag)}
.on-dark .btn{border-color:var(--dark-line);color:var(--paper-line)}
.on-dark .btn::before{background:var(--brass-soft)}
.on-dark .btn:hover{border-color:var(--brass-soft);color:var(--dark)}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;
  padding:16px 20px;border-top:1px solid var(--line)}
.bar .hint{margin:0}

/* --- Crop marks: the site's one signature frame, reused on the masthead --- */
.crop-frame{position:relative}
.crop-frame .corner{position:absolute;width:20px;height:20px;pointer-events:none;
  border-color:var(--brass-soft)}
.corner.tl{top:-1px;left:-1px;border-top:2px solid;border-left:2px solid}
.corner.tr{top:-1px;right:-1px;border-top:2px solid;border-right:2px solid}
.corner.bl{bottom:-1px;left:-1px;border-bottom:2px solid;border-left:2px solid}
.corner.br{bottom:-1px;right:-1px;border-bottom:2px solid;border-right:2px solid}

/* --- Dark panels: the site's fractal-noise overlay, verbatim -------------- */
.on-dark{background:var(--dark);color:var(--stone);position:relative}
.on-dark::before{content:'';position:absolute;inset:0;opacity:.4;pointer-events:none;z-index:0;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/></filter><rect width='100%25' height='100%25' filter='url(%23n)' opacity='.05'/></svg>")}
.on-dark>.wrap{position:relative;z-index:1}
.on-dark .eyebrow{color:var(--brass-soft)}
.on-dark .eyebrow::before{background:var(--brass-soft)}

/* --- Masthead ------------------------------------------------------------- */
/* The first thing the client looks at, so it is built in layers rather than as
   one flat colour: the site's fractal noise on the plate (that one is .on-dark,
   verbatim from the public site), then a hairline grid that fades out before it
   reaches the type, then two brass orbs drifting behind everything. The grid and
   the orbs share one span — aria-hidden, pointer-events:none, z-index 0, so they
   land just above the noise, which is .on-dark::before at the same z-index but
   earlier in tree order. Type stays clear of all three because
   .on-dark>.wrap is z-index 1. They animate by transform alone, so a panel left
   open on the client's second screen all day is not repainting anything. */
.masthead{padding:52px 0 0;overflow:clip}
.mast-ground{position:absolute;inset:0;overflow:hidden;pointer-events:none;z-index:0}
/* 88px squares, barely there, masked so they are strongest behind the gauges and
   gone by the time they reach the headline. Graph paper under an editorial page. */
.mast-ground::before{content:'';position:absolute;inset:-1px;
  background-image:
    repeating-linear-gradient(90deg,rgba(255,255,255,.05) 0 1px,transparent 1px 88px),
    repeating-linear-gradient(180deg,rgba(255,255,255,.04) 0 1px,transparent 1px 88px);
  -webkit-mask-image:radial-gradient(90% 125% at 84% -12%,#000 0%,rgba(0,0,0,.45) 44%,transparent 78%);
  mask-image:radial-gradient(90% 125% at 84% -12%,#000 0%,rgba(0,0,0,.45) 44%,transparent 78%)}
/* Two orbs on one layer, drifting as a pair. This was a single static radial; the
   movement is the difference between a plate that is switched on and one that is
   printed. Thirty-four seconds is slower than anyone watches on purpose — it is
   meant to be noticed only by looking away and coming back. */
.mast-ground::after{content:'';position:absolute;top:-300px;right:-220px;
  width:820px;height:820px;will-change:transform;
  background:
    radial-gradient(circle at 34% 38%,var(--brass-glow) 0%,rgba(169,129,79,0) 62%),
    radial-gradient(circle at 71% 67%,rgba(169,129,79,.15) 0%,rgba(169,129,79,0) 58%)}
@media (prefers-reduced-motion:no-preference){
  .mast-ground::after{animation:orbDrift 34s var(--ease-soft) infinite alternate}
}
@keyframes orbDrift{
  0%{transform:translate3d(0,0,0) scale(1)}
  50%{transform:translate3d(-44px,28px,0) scale(1.07)}
  100%{transform:translate3d(26px,-20px,0) scale(.97)}}

.mast-top{display:flex;justify-content:space-between;align-items:flex-end;gap:48px;
  padding-bottom:34px}
.mast-top .eyebrow{animation:mastIn var(--dur-m) var(--ease) both}
.mast-top h1{font-size:clamp(30px,4.1vw,50px);line-height:1.06;letter-spacing:-.025em;
  margin-top:9px}
/* Per line, not per word and not per block: each line rises out from behind the
   one above it, which is the site's own transition applied to type. The
   padding/margin pair is not decoration — overflow:hidden at line-height 1.06
   would otherwise cut the descender off the y in "Every". */
.mast-top h1 .ln{display:block;overflow:hidden;padding-bottom:.14em;margin-bottom:-.14em}
.mast-top h1 .ln>span{display:block;animation:lineUp var(--dur-l) var(--ease) both}
.mast-top h1 .ln:nth-child(2)>span{animation-delay:.1s}
@keyframes lineUp{from{opacity:0;transform:translateY(105%)}
  to{opacity:1;transform:translateY(0)}}
.mast-top h1 em{font-style:italic;color:var(--brass-soft);position:relative}
/* A brass hairline drawn under the two words the sentence turns on, after the line
   carrying them has finished arriving. */
.mast-top h1 em::after{content:'';position:absolute;left:0;right:0;bottom:.02em;
  height:2px;background:var(--brass-soft);opacity:.45;transform-origin:left;
  animation:ruleIn .85s .6s var(--ease) both}
@keyframes ruleIn{from{transform:scaleX(0)}to{transform:scaleX(1)}}
.mast-top .lede{margin-top:18px;max-width:44ch;color:#9A9D9F;font-size:14.5px;
  animation:mastIn var(--dur-l) .26s var(--ease) both}
@keyframes mastIn{from{opacity:0;transform:translateY(16px);filter:blur(5px)}
  to{opacity:1;transform:none;filter:blur(0)}}

/* Two live gauges. Both read straight off pacer.status(), so what they show is
   the queue as it actually is, not a guess from the last page load. */
.gauges{display:grid;gap:14px;min-width:300px;
  animation:mastIn var(--dur-l) .34s var(--ease) both}
.gauge{position:relative;border:1px solid var(--dark-line);padding:15px 17px 16px;
  background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.013));
  transition:border-color var(--dur-s) var(--ease),transform var(--dur-s) var(--ease)}
/* One lit pixel along the top edge. It is the whole difference between something
   that reads as glass over the plate and a grey box drawn on it. */
.gauge::before{content:'';position:absolute;left:0;right:0;top:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.3) 20%,
    var(--brass-soft) 52%,rgba(255,255,255,.18) 80%,transparent);
  opacity:.5;transition:opacity var(--dur-s) var(--ease)}
.gauge:hover{border-color:rgba(255,255,255,.2);transform:translateY(-2px)}
.gauge:hover::before{opacity:1}
.gauge-top{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.gauge-k{font-family:var(--font-mono);font-size:9.5px;letter-spacing:.17em;
  text-transform:uppercase;color:#8B8E90}
.gauge-v{font-family:var(--font-mono);font-size:20px;color:var(--stone);
  font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.gauge-v s{text-decoration:none;font-size:12px;color:#7E8183;letter-spacing:.02em}
/* A number that moved on its own is the only thing on this plate the client did
   not cause, so it is worth a second of brass. The class goes on from the poll and
   comes off again — see `pulse` in the bundle — and the animation has no fill mode
   so a browser that never fires animationend still ends up with the right colour. */
.lit-change{animation:numPulse .9s var(--ease)}
@keyframes numPulse{
  0%,18%{color:var(--brass-soft);text-shadow:0 0 15px var(--brass-glow)}
  100%{color:inherit;text-shadow:0 0 0 rgba(169,129,79,0)}}
.meter{height:3px;margin-top:11px;background:var(--dark-line);overflow:hidden}
.meter i{display:block;height:100%;background:linear-gradient(90deg,var(--brass),var(--brass-soft));
  transform-origin:left;animation:meterIn var(--dur-l) var(--ease) both;
  transition:width .5s var(--ease)}
@keyframes meterIn{from{transform:scaleX(0)}to{transform:scaleX(1)}}
.meter.hot i{background:linear-gradient(90deg,#B4472F,#D9765C)}
/* Worker occupancy as discrete cells: three cells, two lit = two conversations
   in flight. Reads faster than "2/3" and needs no label. */
.cells-mini{display:flex;gap:4px;margin-top:11px}
.cells-mini b{flex:1;height:3px;background:var(--dark-line);
  transition:background .4s var(--ease),box-shadow .4s var(--ease)}
.cells-mini b.lit{background:var(--brass-soft);box-shadow:0 0 8px var(--brass-glow)}
.sparkline{display:none;margin-top:11px}
.sparkline.on{display:block}
.sparkline svg{width:100%;height:26px;display:block;overflow:visible}
.sparkline polyline{fill:none;stroke:var(--brass-soft);stroke-width:1.4;
  stroke-linejoin:round;stroke-linecap:round}
.spark-k,.gauge-note{font-family:var(--font-mono);font-size:8.5px;letter-spacing:.14em;
  text-transform:uppercase;color:#6E7173;margin-top:5px;display:block}
.gauge-note{margin-top:9px}

/* --- The day's five numbers ----------------------------------------------- */
.cells{display:grid;grid-template-columns:repeat(5,1fr);border-top:1px solid var(--dark-line)}
.cell{padding:23px 22px 27px;border-right:1px solid var(--dark-line);position:relative;
  animation:mastIn var(--dur-m) var(--ease) both;
  transition:background var(--dur-s) var(--ease)}
/* A brass hairline drawn across the top of each number in turn, left to right, as
   the strip arrives — the same sweep the site runs under its nav links, turned
   horizontal and given five numbers to introduce. It draws itself and then fades,
   so what is left is the plain rule; hover brings it back on the one being read. */
.cell::before{content:'';position:absolute;left:0;right:0;top:-1px;height:1px;
  background:var(--brass-soft);opacity:0;transform:scaleX(0);transform-origin:left;
  animation:cellRule 1.05s var(--ease) both;
  transition:opacity var(--dur-s) var(--ease)}
@keyframes cellRule{0%{transform:scaleX(0);opacity:0}26%{opacity:.85}
  100%{transform:scaleX(1);opacity:0}}
.cell:nth-child(1){animation-delay:.30s} .cell:nth-child(1)::before{animation-delay:.30s}
.cell:nth-child(2){animation-delay:.37s} .cell:nth-child(2)::before{animation-delay:.37s}
.cell:nth-child(3){animation-delay:.44s} .cell:nth-child(3)::before{animation-delay:.44s}
.cell:nth-child(4){animation-delay:.51s} .cell:nth-child(4)::before{animation-delay:.51s}
.cell:nth-child(5){animation-delay:.58s} .cell:nth-child(5)::before{animation-delay:.58s}
.cell:last-child{border-right:0}
.cell:hover{background:rgba(255,255,255,.026)}
.cell:hover::before{animation:none;transform:scaleX(1);opacity:.7}
.cell .k{display:flex;align-items:center;gap:7px;font-family:var(--font-mono);
  font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:#8B8E90}
/* The tick that was implied by the gap already in this rule. It grows under the
   pointer, which is the only thing five identical labels needed. */
.cell .k::before{content:'';flex:none;width:13px;height:1px;background:currentColor;
  opacity:.45;transition:width var(--dur-s) var(--ease),opacity var(--dur-s) var(--ease)}
.cell:hover .k::before{width:22px;opacity:1}
.cell .v{display:block;font-family:var(--font-display);font-size:clamp(30px,3.4vw,44px);
  line-height:1;letter-spacing:-.03em;margin-top:13px;font-variant-numeric:tabular-nums}
.cell .u{font-family:var(--font-mono);font-size:11px;letter-spacing:.06em;color:#7E8183;
  margin-left:7px;vertical-align:2px}
/* "Needs you" going from nought to one is the only number on this strip that is
   news. The wash fades in on the poll that reports it, so it arrives while the
   client is looking at another tab rather than waiting for a reload. */
.cell.flagged{background:linear-gradient(180deg,rgba(169,129,79,.11),rgba(169,129,79,0) 76%)}
.cell.flagged .v,.cell.flagged .k{color:var(--brass-soft)}
.cell.flagged .k::before{background:var(--brass-soft);opacity:1}
.cell.flagged::after{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;
  background:var(--brass-soft)}
@media (prefers-reduced-motion:no-preference){
  .cell.flagged::after{animation:edgePulse 2.2s var(--ease-soft) infinite}
}
@keyframes edgePulse{0%,100%{opacity:1}50%{opacity:.25}}
@media (max-width:1080px){
  .mast-top{flex-direction:column;align-items:flex-start;gap:30px}
  .gauges{min-width:0;width:100%;grid-template-columns:1fr 1fr}
  .cells{grid-template-columns:repeat(3,1fr)}
  .cell{border-bottom:1px solid var(--dark-line)}
}
@media (max-width:620px){
  .cells{grid-template-columns:repeat(2,1fr)}
  .gauges{grid-template-columns:1fr}
  .cell{padding:16px 16px 19px}
  /* The orbs are sized for a 1320px plate; at 360px they wash the whole thing out
     and the headline loses its contrast. */
  .mast-ground::after{top:-190px;right:-170px;width:540px;height:540px}
  .mast-ground::before{background-size:56px 56px,56px 56px}
}

/* --- View tabs: the site's nav links, in the nav -------------------------- */
/* Exactly the public-site mechanism — a 13.5px label, an ink hairline that
   sweeps in from the left, brass for the view you are on, and one solid pill at
   the end where the site puts START A PROJECT. They now sit in the nav bar
   itself, top right, so the desk is navigated where the site is navigated.
   Counts are brass chips rather than bare numerals: at 9px a numeral beside a
   label reads as part of the label. */
.deck{padding-top:34px;padding-bottom:96px}
.tabs{display:none;align-items:center;gap:30px;min-width:0}
body.js .tabs{display:flex}
.tab:not(.btn){position:relative;flex:none;border:0;border-radius:0;
  background:transparent;cursor:pointer;padding:9px 0;color:var(--ink-soft);
  font-family:var(--font-body);font-size:13.5px;font-weight:500;letter-spacing:.01em;
  white-space:nowrap;transition:color var(--dur-s) var(--ease)}
.tab:not(.btn)::after{content:'';position:absolute;left:0;right:100%;bottom:0;
  height:1px;background:var(--ink);transition:right .38s var(--ease)}
.tab:not(.btn):hover{color:var(--ink)}
.tab:not(.btn):hover::after,.tab:not(.btn).is-on::after{right:0}
.tab:not(.btn).is-on{color:var(--brass)}
.tab:not(.btn).is-on::after{background:var(--brass)}
.tab b{display:inline-block;min-width:17px;margin-left:8px;padding:0 4px;
  border-radius:999px;background:var(--brass);color:#fff;text-align:center;
  font-family:var(--font-mono);font-weight:500;font-size:9px;line-height:16px;
  letter-spacing:.02em;vertical-align:1.5px}
.tab b.hot{background:var(--flag);box-shadow:0 0 0 3px rgba(158,59,42,.14)}
.tab.btn{flex:none;margin-left:6px;padding:11px 20px;font-size:10.5px}
.tab.btn b{background:var(--brass-soft);color:var(--dark)}
/* The bar carries seven things now, so it gives them up in order of how little
   anyone navigates by them: the gaps tighten, then the clock goes, then the tabs
   drop onto a row of their own inside the same sticky bar — one bar, no gap,
   still no crooked band for the panels to scroll through. That row scrolls
   sideways rather than wrapping, so the nav never grows a third line.
   1080px is where the single row measures out at about 950px of content against
   1012px of wrap: below it the row would overflow and overflow-x:hidden on the
   body would quietly clip Sign out off the right edge. */
@media (max-width:1200px){
  .site-nav .wrap{gap:16px}
  .tabs{gap:24px}
}
@media (max-width:1080px){
  .site-nav .wrap,.site-nav.is-scrolled .wrap{height:auto;flex-wrap:wrap;
    padding-top:13px;padding-bottom:0}
  .tabs{order:3;flex:0 0 100%;margin:9px -34px 0;padding:0 34px 7px;
    gap:22px;overflow-x:auto;overflow-y:hidden;
    scrollbar-width:none;-ms-overflow-style:none;-webkit-overflow-scrolling:touch}
  .tabs::-webkit-scrollbar{display:none}
  .tab:not(.btn){padding:8px 0 10px}
  .deck{padding-top:26px}
}
@media (max-width:640px){
  .site-nav .wrap,.site-nav.is-scrolled .wrap{padding-top:11px}
  .tabs{margin:8px -18px 0;padding:0 18px 6px;gap:19px}
  .logo{font-size:17px;gap:9px}
  .logo-mark{width:31px;height:31px}
  .deck{padding-top:22px;padding-bottom:80px}
}
/* On a 360px phone the first row measures ~426px: the logo, the live pill and
   Sign out cannot all be on it. Sign out goes — it is the one of the three that
   is also in the footer, and "is it answering?" is the reason anyone opens this
   on a phone in the first place. */
@media (max-width:560px){
  .nav-right .btn-mini,.nav-sep{display:none}
  .state{padding:5px 11px;letter-spacing:.12em}
}

/* Panels are plain sections. Without script every one of them is on the page,
   stacked, and the tabs are the nav's own links — the panel still works, it is
   just longer. body.js is what turns them into tabs. */
.panel{padding-top:6px}
body.js .panel{display:none}
body.js .panel.is-on{display:block;animation:panelIn .5s var(--ease) both}
@keyframes panelIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.panel>.section-head{margin-bottom:26px}
.section-head{display:flex;justify-content:space-between;align-items:flex-end;gap:34px;
  padding-bottom:20px;border-bottom:1px solid var(--line);position:relative;margin-bottom:26px}
.section-head::after{content:'';position:absolute;left:0;bottom:-1px;height:1px;width:0;
  background:var(--brass);transition:width 1s var(--ease)}
.section-head.is-visible::after{width:64px}
.section-head h2{font-size:clamp(23px,2.5vw,31px)}
.section-head p{color:var(--ink-soft);max-width:40ch;font-size:13.5px}
body.js .panel:not(.is-on) .reveal{opacity:0}

/* --- Layout: two columns where it helps, one where it does not ------------ */
.split{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:24px;align-items:start}
.split-even{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:24px;
  align-items:start}
/* minmax(0,…) and not a bare `1fr`, which means `minmax(auto,1fr)` — and the
   auto floor is min-content. `.snip` is white-space:nowrap, so its min-content
   is the full width of the longest message preview in the inbox: that floor
   pushed this single-column track wider than the phone viewport, which is what
   cut the cards off on the right and left the sticky header floating over the
   content in landscape. The two-column rule above already guards against it. */
@media (max-width:1000px){.split{grid-template-columns:minmax(0,1fr)}}

/* --- Cards ---------------------------------------------------------------- */
/* Flat white squares read as a spreadsheet. The site's cards are rounded, sit
   on paper rather than pure white, carry a wash of brass light from the top
   left and lift on hover — so these do the same. `overflow:hidden` is what
   lets the header band and the accent bar follow the corner. */
.card{position:relative;min-width:0;margin-bottom:24px;border:1px solid var(--line);
  border-radius:20px;overflow:hidden;
  background:radial-gradient(120% 90% at 18% 0%,rgba(169,129,79,.07),rgba(255,255,255,0) 54%),
             linear-gradient(180deg,#fff,#FCFCFA);
  box-shadow:0 1px 2px rgba(10,11,12,.03),0 20px 44px -38px rgba(10,11,12,.22);
  transition:box-shadow var(--dur-m) var(--ease),border-color var(--dur-m) var(--ease),
             transform var(--dur-m) var(--ease)}
.card:hover{border-color:rgba(169,129,79,.38);transform:translateY(-2px);
  box-shadow:0 1px 2px rgba(10,11,12,.03),0 30px 70px -46px rgba(10,11,12,.42)}
/* Header: a tinted band with the site's brass eyebrow dot at its left. Now that
   the ground under the card is the deeper stone, the card is three steps rather
   than one: ground at #ECECE7, this band at about #F3F3F0, the body at #fff.
   Tinting the band the whole way to the ground colour made the card's top edge
   disappear into the page. */
.card>h2,.card>.card-head{position:relative;display:flex;justify-content:space-between;
  align-items:center;gap:14px;padding:15px 20px 15px 36px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(236,236,231,.66),rgba(246,246,243,.34));
  font-family:var(--font-mono);font-size:10.5px;font-weight:500;letter-spacing:.16em;
  text-transform:uppercase;color:var(--ink-soft)}
.card>h2::before,.card>.card-head::before{content:'';position:absolute;left:20px;top:50%;
  margin-top:-3px;width:5px;height:5px;border-radius:50%;background:var(--brass);
  box-shadow:0 0 10px var(--brass-glow)}
.card>h2 em,.card>.card-head em{font-style:normal;color:var(--brass);letter-spacing:.08em;
  white-space:nowrap}
.card>h2 em.hot,.card>.card-head em.hot{color:var(--flag)}
.card.accent{border-color:rgba(169,129,79,.4)}
.card.accent::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;z-index:1;
  background:linear-gradient(var(--brass),var(--brass-soft))}
.pad{padding:20px}
.hint{margin:0;color:var(--ink-soft);font-size:12.5px;line-height:1.55}
.hint-pad{padding:15px 20px 0}
.empty{padding:44px 20px;text-align:center;color:var(--ink-soft);font-size:13px}
.empty b{display:block;font-family:var(--font-display);font-size:19px;color:var(--ink);
  margin-bottom:6px;font-weight:500}
.empty .mark{display:block;width:26px;height:26px;margin:0 auto 14px;border:1px solid var(--brass);
  border-radius:50%;position:relative}
.empty .mark::after{content:'';position:absolute;inset:9px;background:var(--brass);border-radius:50%;
  opacity:.5}

/* --- Inbox: search, chips, rows ------------------------------------------- */
.listtools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:14px 20px;
  border-bottom:1px solid var(--line)}
body:not(.js) .listtools{display:none}
.search{flex:1 1 210px;min-width:0;font-family:var(--font-body);font-size:13.5px;
  color:var(--ink);background:var(--stone);border:1px solid var(--line);border-radius:999px;
  padding:9px 15px;transition:border-color var(--dur-s) var(--ease),background var(--dur-s) var(--ease)}
.search::placeholder{color:#9EA1A4}
.search:focus{background:#fff;border-color:var(--brass);outline:none}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);border-radius:999px;background:transparent;cursor:pointer;
  padding:7px 13px;color:var(--ink-soft);font-family:var(--font-mono);font-size:9.5px;
  letter-spacing:.12em;text-transform:uppercase;
  transition:all var(--dur-s) var(--ease)}
.chip:hover{color:var(--ink);border-color:rgba(10,11,12,.3)}
.chip.is-on{background:var(--brass);border-color:var(--brass);color:#fff}

.lead{display:block;padding:15px 20px;border-bottom:1px solid var(--line);position:relative;
  transition:background var(--dur-s) var(--ease),padding-left var(--dur-s) var(--ease)}
.lead::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--brass);
  transform:scaleY(0);transform-origin:top;transition:transform var(--dur-m) var(--ease)}
.lead:last-child{border-bottom:0}
.lead:hover{background:var(--paper-line);padding-left:26px}
.lead:hover::before{transform:scaleY(1)}
.lead.sel{background:var(--stone-deep)}
.lead.sel::before{transform:scaleY(1)}
.lead.hidden{display:none}
.lead-top{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.num{font-family:var(--font-mono);font-size:13.5px;letter-spacing:.01em;
  display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap}
.when{font-family:var(--font-mono);font-size:10.5px;color:var(--ink-soft);white-space:nowrap;
  letter-spacing:.05em}
.snip{display:block;margin:7px 0 0;color:var(--ink-soft);font-size:13px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Why a conversation is Stopped/Cold, one quiet line under the preview. The
   dot before it ties it to the amber accent without shouting. */
.lead-why{display:block;margin:6px 0 0;font-family:var(--font-mono);font-size:10px;
  letter-spacing:.04em;color:var(--caution)}
.lead-why::before{content:'•\\00a0';color:var(--brass)}
.tag{display:inline-block;font-family:var(--font-mono);font-size:9px;letter-spacing:.13em;
  text-transform:uppercase;padding:4px 7px;border:1px solid currentColor;border-radius:999px;
  color:var(--ink-soft)}
.tag.hot{color:var(--flag)}
.tag.mine{color:var(--brass)}
.tag.cool{color:var(--ink-soft)}
.tag.ok{color:var(--live)}
.tag.warn{color:var(--caution)}
.noresult{display:none;padding:34px 20px;text-align:center;color:var(--ink-soft);font-size:13px}
.noresult.on{display:block}

/* --- Transcript ----------------------------------------------------------- */
.turns{padding:20px;max-height:56vh;overflow:auto;background:
  linear-gradient(var(--stone) 0 1px,transparent 1px 100%) 0 0/100% 26px}
.turn{max-width:76%;padding:11px 14px;margin-bottom:12px;font-size:13.5px;line-height:1.55;
  overflow-wrap:anywhere;
  border:1px solid var(--line);background:#fff;border-radius:2px 14px 14px 14px;
  animation:bubbleIn .55s var(--ease) both;animation-delay:calc(var(--i,0)*45ms)}
.turn.model{margin-left:auto;background:var(--ink);color:var(--paper-line);
  border-color:var(--ink);border-radius:14px 2px 14px 14px}
.turn .at{display:block;margin-top:6px;font-family:var(--font-mono);font-size:9.5px;opacity:.6;
  letter-spacing:.06em}
@keyframes bubbleIn{from{opacity:0;transform:translateY(9px);filter:blur(3px)}
  to{opacity:1;transform:none;filter:blur(0)}}
.thread-head{align-items:flex-start!important;flex-wrap:wrap}
/* This header wraps onto two lines on a narrow column, so the eyebrow dot is
   pinned to the first line rather than to the middle of the whole band. */
.card>.thread-head::before{top:21px;margin-top:0}
.thread-who{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  font-family:var(--font-mono);font-size:12.5px;letter-spacing:.02em;
  text-transform:none;color:var(--ink)}
.copy{border:0;background:transparent;cursor:pointer;color:var(--ink-soft);padding:2px 0;
  font-family:var(--font-mono);font-size:9.5px;letter-spacing:.13em;text-transform:uppercase;
  transition:color var(--dur-s) var(--ease)}
.copy:hover{color:var(--brass)}
body:not(.js) .copy{display:none}

/* --- Switches: a real checkbox, drawn as a track --------------------------- */
/* Still one `input[type=checkbox]` and nothing else — the track, the knob and
   the ON/OFF legend are all drawn on the input itself, so the control the form
   posts is the control the client clicks. The track is recessed when off and
   lit brass with a glow ring when on, which is the difference that has to be
   readable at a glance from across a desk. */
label.sw{position:relative;display:flex;gap:18px;align-items:flex-start;padding:17px 20px;
  border-bottom:1px solid var(--line);cursor:pointer;
  transition:background var(--dur-s) var(--ease)}
/* Every other row a shade deeper: five identical white bands were impossible to
   keep your place in. */
label.sw:nth-of-type(even){background:rgba(236,236,231,.5)}
label.sw:hover{background:var(--paper-line)}
label.sw:last-of-type{border-bottom:0}
/* A brass edge marks the switches that are on, so the card answers "what is
   running?" before a single label is read. */
label.sw::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;
  background:var(--brass);opacity:0;transition:opacity var(--dur-s) var(--ease)}
label.sw:has(input:checked)::before{opacity:.85}
.sw input{appearance:none;-webkit-appearance:none;flex:none;margin:1px 0 0;
  width:52px;height:29px;border-radius:999px;border:1px solid rgba(10,11,12,.14);
  background:linear-gradient(180deg,var(--stone-deep),#E2E2DC);position:relative;
  cursor:pointer;overflow:hidden;
  box-shadow:inset 0 2px 4px rgba(10,11,12,.13),inset 0 -1px 0 rgba(255,255,255,.7);
  transition:background var(--dur-s) var(--ease),border-color var(--dur-s) var(--ease),
             box-shadow var(--dur-s) var(--ease)}
/* The legend sits behind the knob and swaps sides with it, so whichever word is
   showing is always the state the switch is in. Sized to clear the knob: OFF
   occupies x 29-44 of a 50px track against a knob that ends at 24, ON occupies
   10-20 against a knob that starts at 26. */
.sw input::before{content:'OFF';position:absolute;top:0;right:6px;
  font-family:var(--font-mono);font-size:7.5px;line-height:27px;letter-spacing:.08em;
  color:var(--ink-soft);transition:color var(--dur-s) var(--ease)}
.sw input::after{content:'';position:absolute;top:3px;left:3px;width:21px;height:21px;
  border-radius:50%;background:linear-gradient(180deg,#fff,#F3F3EF);
  box-shadow:0 1px 3px rgba(10,11,12,.3),0 0 0 1px rgba(10,11,12,.04);
  transition:transform .42s var(--ease),background var(--dur-s) var(--ease)}
.sw input:checked{border-color:var(--brass);
  background:linear-gradient(180deg,var(--brass-soft),var(--brass) 62%);
  box-shadow:inset 0 1px 2px rgba(10,11,12,.18),0 0 0 4px var(--brass-glow)}
.sw input:checked::before{content:'ON';right:auto;left:10px;color:rgba(255,255,255,.95)}
.sw input:checked::after{transform:translateX(23px);
  background:linear-gradient(180deg,#fff,#FBF6EE)}
.sw input:focus-visible{outline:2px solid var(--brass);outline-offset:3px}
.sw b{display:block;font-weight:600;font-size:14px;letter-spacing:-.005em}
.sw small{display:block;margin-top:3px;color:var(--ink-soft);font-size:12.5px;line-height:1.5}
@media (max-width:640px){
  label.sw{gap:14px;padding:15px 16px}
  .sw small{font-size:12px}
}

/* --- Number rows and the guard-rail notes --------------------------------- */
.row{display:flex;justify-content:space-between;align-items:center;gap:14px;
  padding:13px 20px;border-bottom:1px solid var(--line);
  transition:background var(--dur-s) var(--ease)}
/* Banded like the switches, and for the same reason: eleven numbers in a column
   of identical white rows is where a client loses which line they are editing.
   Both the hover and the warn/stop tints below have to beat this, so it is
   declared first and they are declared after. */
.row:nth-of-type(even){background:rgba(236,236,231,.5)}
.row:hover{background:var(--paper-line)}
.row:last-of-type{border-bottom:0}
.row .lbl{font-size:13.5px}
.row .u{font-family:var(--font-mono);font-size:10px;text-transform:uppercase;
  letter-spacing:.11em;color:var(--ink-soft);margin-left:9px}
/* A banded row keeps its one-line shape and drops the verdict underneath, so
   the column of numbers stays scannable until something needs saying. */
.row.banded{flex-wrap:wrap}
.note{flex:0 0 100%;margin:6px 0 2px;font-size:12px;line-height:1.55;color:var(--ink-soft);
  max-width:56ch;transition:color var(--dur-s) var(--ease)}
.note:empty{display:none}
.note.warn{color:var(--caution)}
.note.stop{color:var(--flag);font-weight:500}
.note.warn::before,.note.stop::before{content:"▲ ";font-size:9px;vertical-align:1.5px}
.row.lvl-warn{background:rgba(138,85,20,.045)}
.row.lvl-stop{background:rgba(158,59,42,.05)}
.row.lvl-warn input[type=number]{border-color:var(--caution)}
.row.lvl-stop input[type=number]{border-color:var(--flag);box-shadow:0 0 0 3px rgba(158,59,42,.1)}
input[type=number],input[type=password],textarea{font-family:var(--font-mono);font-size:13.5px;
  line-height:1.5;color:var(--ink);background:#fff;border:1px solid var(--line);
  padding:9px 11px;border-radius:3px;
  transition:border-color var(--dur-s) var(--ease),box-shadow var(--dur-s) var(--ease)}
input[type=number]{width:92px;text-align:right;font-variant-numeric:tabular-nums}
input[type=number]:focus,input[type=password]:focus,textarea:focus{border-color:var(--brass);
  box-shadow:0 0 0 3px var(--brass-glow);outline:none}

/* --- The stepper: one field, two buttons over it --------------------------- */
/* The browser's own up/down arrows are two 7px targets stacked inside the box,
   unhittable on a phone and invisible until you hover. This is the same input
   with a minus and a plus either side of it, drawn as one control: the buttons
   own the outer border and radius, the field in the middle keeps none of its own,
   and the focus ring goes round the whole group rather than the middle third.
   Nothing here is required to use the panel — with the script off the buttons are
   gone and the native arrows are back, which is why the arrows are only
   suppressed under body.js. */
.numset{display:inline-flex;align-items:center}
.stepper{display:inline-flex;align-items:stretch;position:relative;border-radius:4px;
  transition:box-shadow var(--dur-s) var(--ease)}
.stepper:has(input:focus){box-shadow:0 0 0 3px var(--brass-glow)}
.step{display:none;flex:none;position:relative;align-items:center;justify-content:center;
  width:32px;padding:0;
  border:1px solid var(--line);background:linear-gradient(180deg,#fff,var(--stone));
  color:var(--ink-soft);font-family:var(--font-mono);font-size:14px;line-height:1;
  cursor:pointer;-webkit-user-select:none;user-select:none;
  transition:background var(--dur-s) var(--ease),color var(--dur-s) var(--ease),
             border-color var(--dur-s) var(--ease)}
body.js .step{display:inline-flex}
.step[data-step="-1"]{border-radius:4px 0 0 4px;border-right:0}
.step[data-step="1"]{border-radius:0 4px 4px 0;border-left:0}
.step:hover{background:var(--ink);border-color:var(--ink);color:var(--stone)}
/* Pressed state is its own step down rather than a scale: the control is 32px
   wide and a transform on something that small reads as a wobble. */
.step:active{background:var(--brass);border-color:var(--brass);color:#fff}
.step:focus-visible{outline:2px solid var(--brass);outline-offset:2px;z-index:2}
/* At the floor or the ceiling the button says so and stops, rather than clicking
   to no effect — the one thing a stepper has to communicate that a bare field
   does not. dashboard.RANGES is where those two numbers come from. */
.step[disabled]{cursor:not-allowed;opacity:.32;background:var(--stone-deep)}
.step[disabled]:hover{background:var(--stone-deep);border-color:var(--line);
  color:var(--ink-soft)}
/* The field between the buttons gives up its radius and its own ring: the group
   above draws both, or a focused field would put a brass halo round the middle
   third of a control that reads as one object. */
body.js .stepper input[type=number]{border-radius:0;text-align:center;width:78px;
  padding-left:6px;padding-right:6px;box-shadow:none;
  -moz-appearance:textfield;appearance:textfield}
body.js .stepper input[type=number]::-webkit-outer-spin-button,
body.js .stepper input[type=number]::-webkit-inner-spin-button{
  -webkit-appearance:none;appearance:none;margin:0}
/* The whole group borrows the field's warning colour, or the two buttons stay
   stone while the number between them turns red. */
.row.lvl-warn .step{border-color:var(--caution);color:var(--caution)}
.row.lvl-stop .step{border-color:var(--flag);color:var(--flag)}
.row.lvl-stop .stepper{box-shadow:0 0 0 3px rgba(158,59,42,.1)}
/* A value that just moved on its own is worth half a second of the client's
   attention — it is the one edit on this page they did not type. */
@keyframes stepBlink{0%{background:rgba(169,129,79,.16)}100%{background:#fff}}
@media (prefers-reduced-motion:no-preference){
  body.js .stepper input.bumped{animation:stepBlink .5s var(--ease)}
}
textarea{width:100%;display:block;resize:vertical;min-height:150px;line-height:1.65;
  font-size:13px;background:var(--stone)}
textarea:focus{background:#fff}
.field{padding:20px;border-bottom:1px solid var(--line)}
.field:last-of-type{border-bottom:0}
.field>label{display:block;font-weight:600;font-size:14px;margin-bottom:4px}
.field .hint{margin-bottom:11px}
.charcount{display:block;margin-top:7px;text-align:right;font-family:var(--font-mono);
  font-size:9.5px;letter-spacing:.1em;color:var(--ink-soft)}
body:not(.js) .charcount{display:none}
.charcount.near{color:var(--caution)}

/* --- Save bar: a plain bar without script, a floating one with it ---------- */
.savebar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:16px 20px;border:1px solid var(--line);background:#fff;margin-bottom:24px}
body.js .savebar{position:fixed;left:50%;bottom:24px;z-index:60;margin:0;
  transform:translate(-50%,140%);opacity:0;pointer-events:none;
  border-radius:999px;padding:12px 12px 12px 24px;border-color:rgba(10,11,12,.16);
  box-shadow:0 26px 60px -26px rgba(10,11,12,.45);
  transition:transform .5s var(--ease),opacity .4s var(--ease)}
body.js.dirty .savebar{transform:translate(-50%,0);opacity:1;pointer-events:auto}
.savebar .what{font-family:var(--font-mono);font-size:10.5px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--ink-soft)}
.savebar .what b{color:var(--brass);font-weight:500}
/* On a phone a centred pill is both too wide to fit and wide enough to cover the
   field you were editing. It becomes a full-width bar pinned above the home
   area, with its own opaque backing so whatever it overlaps stays legible. Both
   rules below have to beat `body.js.dirty .savebar`, which is why they carry the
   same three classes and are declared after it. */
@media (max-width:700px){
  body.js .savebar{left:12px;right:12px;bottom:max(12px,env(safe-area-inset-bottom));
    transform:translateY(180%);border-radius:16px;padding:12px 14px;gap:10px;
    background:rgba(255,255,255,.97);backdrop-filter:saturate(140%) blur(10px);
    justify-content:space-between}
  body.js.dirty .savebar{transform:translateY(0)}
  .savebar .what{flex:1 1 100%;order:-1;font-size:9.5px}
  .savebar .btn{flex:1 1 auto;justify-content:center}
}

/* --- Read-out strip: what the desk is set to, in one glance ---------------- */
/* Tinted as a block rather than cell by cell: the strip is an auto-fit grid, so
   an alternating tint breaks wherever the row happens to wrap. */
.readout{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
  border-top:1px solid var(--line);
  background:linear-gradient(180deg,rgba(236,236,231,.6),rgba(236,236,231,.22))}
.readout div{padding:15px 20px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.readout div:last-child{border-right:0}
.readout .k{display:block;font-family:var(--font-mono);font-size:9px;letter-spacing:.15em;
  text-transform:uppercase;color:var(--ink-soft)}
.readout .v{display:block;margin-top:7px;font-family:var(--font-mono);font-size:15px;
  font-variant-numeric:tabular-nums}
.readout .v.on{color:var(--live)}
.readout .v.off{color:var(--flag)}
.readout .v.warn{color:var(--caution)}
/* The outreach scoreboard: the same readout grid, but each figure is bigger —
   it is the first thing the client reads on the card — and carries a plain
   sub-line under it so nobody has to know what "delivered" means in the abstract.
   Sits directly under the card header, above the search/chips row. */
.cold-summary{border-top:0;border-bottom:1px solid var(--line)}
.cold-summary .v{font-family:var(--font-display);font-size:26px;letter-spacing:-.02em;
  margin-top:9px;line-height:1}
.cold-summary .sub{display:block;margin-top:6px;font-family:var(--font-body);
  font-size:11.5px;line-height:1.4;color:var(--ink-soft);text-transform:none;
  letter-spacing:0}
/* The plain-English retry policy, sitting between the scoreboard and the list. */
.cold-legend{margin:0;padding:14px 20px;border-bottom:1px solid var(--line);
  font-size:12.5px;line-height:1.6}
.cold-legend b{color:var(--ink);font-weight:600}

/* --- Health & Developer: the Doctor Desk, folded into the panel ----------- */
/* The Health tab (the client's) and the Developer tab (mine) are two reads of
   the same errors table. The client's is reused brand — .card, .readout, .flash
   — plus one new thing, the severity chip that says in a word and a colour how
   much a logged issue matters. The developer's adds the one raw surface on the
   whole panel: a dark monospace block for a stack trace. */
.sev{display:inline-flex;align-items:center;gap:6px;flex:none;
  font-family:var(--font-mono);font-size:9px;letter-spacing:.13em;text-transform:uppercase;
  padding:4px 9px;border-radius:999px;border:1px solid currentColor}
.sev::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}
.sev.high{color:var(--flag)}
.sev.medium{color:var(--caution)}
.sev.low{color:var(--ink-soft)}
.sev.ok{color:var(--live)}
/* One issue is one row, with the same brass/amber/red edge the switches and the
   flagged number rows use — so "this is the one that matters" reads the same
   wherever it appears on the panel. */
.issue{position:relative;padding:16px 20px 16px 24px;border-bottom:1px solid var(--line)}
.issue:last-child{border-bottom:0}
.issue::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;opacity:.85}
.issue.high::before{background:var(--flag)}
.issue.medium::before{background:var(--caution)}
.issue.low::before{background:var(--ink-soft);opacity:.4}
.issue-top{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}
.issue-msg{margin:11px 0 0;font-size:13.5px;line-height:1.55}
.issue .src{font-family:var(--font-mono);font-size:12px;color:var(--ink-soft);
  overflow-wrap:anywhere}
.issue .meta{margin-top:9px;font-family:var(--font-mono);font-size:10px;letter-spacing:.05em;
  color:var(--ink-soft)}
.issue .meta b{color:var(--ink);font-weight:500}
.issue form{display:inline}
/* The trace: the panel's only raw text, on the same dark plate as the masthead
   so it reads as "under the hood" rather than as content. */
.trace{margin:12px 0 2px;padding:14px 16px;border-radius:12px;overflow:auto;
  background:var(--dark-2);color:var(--paper-line);
  font-family:var(--font-mono);font-size:11.5px;line-height:1.55;
  white-space:pre-wrap;overflow-wrap:anywhere;max-height:340px}
.resolve-all{display:flex;justify-content:flex-end;gap:12px;align-items:center;
  padding:14px 20px;border-bottom:1px solid var(--line)}

/* --- Flash + notices ------------------------------------------------------- */
.flash{display:flex;gap:12px;align-items:flex-start;border:1px solid var(--live);
  border-left-width:2px;border-radius:16px;color:var(--live);padding:14px 20px;margin:26px 0 0;
  background:linear-gradient(180deg,#fff,#FCFCFA);
  box-shadow:0 1px 2px rgba(10,11,12,.03),0 18px 40px -36px rgba(10,11,12,.24);
  font-size:13px;line-height:1.55;animation:flashIn .5s var(--ease) both}
.flash.bad{border-color:var(--flag);color:var(--flag)}
.flash::before{content:'';width:7px;height:7px;border-radius:50%;background:currentColor;
  flex:none;margin-top:7px;box-shadow:0 0 0 4px rgba(46,122,94,.16)}
.flash.bad::before{box-shadow:0 0 0 4px rgba(158,59,42,.16)}
.flash.warn{border-color:var(--caution);color:var(--caution)}
.flash.warn::before{box-shadow:0 0 0 4px rgba(138,85,20,.16)}
/* The deck's own top padding already stands the first notice off the nav; the
   26px is there to separate a second notice from the first. */
.flash:first-child{margin-top:0}
@keyframes flashIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}

/* --- Footer ---------------------------------------------------------------- */
.foot{margin-top:34px;padding-top:22px;border-top:1px solid var(--line);
  display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;
  font-family:var(--font-mono);font-size:10.5px;letter-spacing:.06em;color:var(--ink-soft)}
.foot span{display:flex;gap:16px;flex-wrap:wrap;align-items:center}
.foot a{border-bottom:1px solid var(--line);transition:border-color var(--dur-s) var(--ease)}
.foot a:hover{border-color:var(--brass);color:var(--brass)}
.foot .live-when{color:var(--brass)}

/* --- Reveal on scroll: the site's own transition, blur included ------------ */
.reveal{opacity:0;transform:translateY(26px);filter:blur(6px);
  transition:opacity .9s var(--ease),transform .9s var(--ease),filter .9s var(--ease)}
.reveal.is-visible{opacity:1;transform:none;filter:blur(0)}
body:not(.js) .reveal{opacity:1;transform:none;filter:none}

/* --- Sign-in / closed: one dark plate, crop-marked ------------------------- */
.gate{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:34px 20px}
.gate-card{width:100%;max-width:430px;border:1px solid var(--dark-line);
  background:var(--dark-2);position:relative;
  animation:mastIn var(--dur-l) var(--ease) both}
.gate-head{padding:30px 30px 0}
.gate-head h1{font-size:31px;margin-top:6px;color:var(--stone)}
.gate-head p{margin-top:12px;color:#9A9D9F;font-size:13.5px;line-height:1.6}
.gate form,.gate .gate-body{padding:24px 30px 30px}
.gate label{display:block;font-family:var(--font-mono);font-size:9.5px;letter-spacing:.16em;
  text-transform:uppercase;color:#8B8E90;margin-bottom:8px}
.gate input{width:100%;margin-bottom:18px;background:rgba(255,255,255,.04);
  border-color:var(--dark-line);color:var(--stone)}
.gate input:focus{background:rgba(255,255,255,.07)}
/* --- Show password: one button, two states, and the state is the icon -------- */
/* An open eye means "let me see it"; the same eye struck through means "put it
   back". What does not change with the state is the button's name: a toggle that
   renames itself is announced as a different control every time it is pressed, so
   the name stays "Show password" and aria-pressed carries whether it is on. With
   the script off the button is not rendered at all and the field is an ordinary
   password box, which is why the padding it needs is scoped to body.js. */
.pw{position:relative;display:block;margin-bottom:18px}
/* The field's own bottom margin moves out to the wrapper, and the field becomes a
   block. Both so the wrapper is exactly as tall as the box: a percentage height on
   the button is otherwise measured against a line box, and the button hangs into
   the gap under the field by however much the strut adds. */
.gate .pw input{display:block;margin-bottom:0}
.pw-eye{position:absolute;top:0;right:0;width:48px;height:100%;
  display:flex;align-items:center;justify-content:center;padding:0;
  border:0;background:none;color:#8B8E90;cursor:pointer;border-radius:0 4px 4px 0;
  transition:color var(--dur-s) var(--ease)}
body.js .pw input{padding-right:52px}
/* Edge puts its own reveal control inside password fields. Two eyes side by side
   is worse than either one. */
body.js .pw input::-ms-reveal{display:none}
.pw-eye:hover{color:var(--brass-soft)}
.pw-eye:focus-visible{outline:2px solid var(--brass);outline-offset:-3px;
  color:var(--brass-soft)}
.pw-eye svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.6;
  stroke-linecap:round;stroke-linejoin:round;overflow:visible;
  transition:transform var(--dur-s) var(--ease)}
.pw-eye:hover svg{transform:scale(1.07)}
.pw-eye:active svg{transform:scale(.93)}
/* The iris closes and the slash draws itself across, rather than one icon being
   swapped for another: the client is pressing this to watch something change. */
.pw-eye .iris{transform-origin:12px 12px;transition:transform var(--dur-s) var(--ease)}
.pw-eye .slash{stroke-dasharray:27;stroke-dashoffset:27;
  transition:stroke-dashoffset var(--dur-s) var(--ease)}
.pw-eye[aria-pressed="true"]{color:var(--brass-soft)}
.pw-eye[aria-pressed="true"] .iris{transform:scale(.46)}
.pw-eye[aria-pressed="true"] .slash{stroke-dashoffset:0}
body:not(.js) .pw-eye{display:none}
.gate .err{margin:0 0 16px;color:#E4A091;font-family:var(--font-mono);font-size:11.5px;
  line-height:1.5}
.gate code{font-family:var(--font-mono);font-size:12px;color:var(--brass-soft)}
.gate .fine{margin-top:16px;color:#7E8183;font-size:12px;line-height:1.6}

/* --- Narrow screens ------------------------------------------------------- */
/* The panel is read on a phone as often as at a desk, so this block is the one
   place the phone layout is decided. It is last in the sheet on purpose: every
   rule here has to beat its desktop counterpart at the same specificity. */
@media (max-width:760px){
  /* 320px minimum against a 324px column at a 360px viewport was one rounding
     error away from a sideways scroll. */
  .split-even{grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px}
  .split{gap:18px}
  .card{margin-bottom:18px;border-radius:16px}
  .card>h2,.card>.card-head{padding:13px 16px 13px 32px;font-size:9.5px;
    letter-spacing:.13em}
  .card>h2::before,.card>.card-head::before{left:16px}
  .pad,.field,.listtools,.bar,.hint-pad{padding-left:16px;padding-right:16px}
  .readout div{padding:13px 16px}
  .section-head{gap:16px;flex-wrap:wrap;align-items:flex-start;padding-bottom:16px}
  .section-head p{max-width:none}
  /* A label, a number and its unit will not sit on one 328px line once the
     label is longer than three words. The number keeps its own line, right
     where the thumb already is. */
  .row{flex-wrap:wrap;gap:6px 12px;padding:13px 16px}
  .row .lbl{flex:1 1 100%;font-size:13px}
  .row>span{display:flex;align-items:center}
  input[type=number]{width:104px;padding:11px}
  /* 44px of thumb per button, and the field narrows to pay for it: the stepper is
     the reason this row is worth opening on a phone at all, since the native
     arrows it replaces cannot be hit with a finger. */
  body.js .step{width:44px}
  body.js .stepper input[type=number]{width:76px;padding-top:12px;padding-bottom:12px}
  .row .u{margin-left:11px}
  .note{margin-top:4px;max-width:none}
  .turn{max-width:88%}
  .turns{max-height:none;padding:16px}
  .search{flex:1 1 100%}
  .foot{gap:12px;margin-top:26px}
}
/* Reserve the height of the floating save bar so the last row of a panel is
   never the thing it lands on. */
@media (max-width:700px){
  html{scroll-padding-bottom:104px}
  body.js .deck{padding-bottom:112px}
}
/* Landscape on a phone is about 850px wide, so it misses the 760px block above
   entirely and keeps the desktop 56vh cap — roughly 220px of thread on a 390px
   viewport, a letterbox you cannot read a conversation in. Here the constraint
   is height, not width, so it is keyed on height: let the thread run its full
   length and scroll with the page, the same as portrait. */
@media (max-height:560px){
  .turns{max-height:none;padding:14px 16px}
  .turn{max-width:88%}
}


@media (prefers-reduced-motion:reduce){
  html{scroll-behavior:auto}
  body,.mast-top .eyebrow,.mast-top h1,.mast-top h1 .ln>span,.mast-top h1 em::after,
  .mast-top .lede,.gauges,.cell,.cell::before,.lit-change,
  .turn,.flash,.gate-card,body.js .panel.is-on{animation:none}
  .reveal{opacity:1;transform:none;filter:none;transition:none}
  .meter i{animation:none;transition:none}
  .scroll-progress,.cursor-glow{display:none}
  .site-nav{transition:none}
  .btn,.btn::before,.btn-arrow,.lead,.lead::before,.chip,.tab,.card,.sw input,
  .sw input::after,.sw input::before,label.sw,label.sw::before,
  .section-head::after,.savebar,.step,.stepper,
  .gauge,.gauge::before,.cell,.cell::before,.cell .k::before,.cells-mini b,
  .pw-eye,.pw-eye svg,.pw-eye .iris,.pw-eye .slash{transition:none}
  /* The sweep is a fill-mode animation, so switching it off leaves the rule at
     scaleX(0). Put it back where hover expects to find it. */
  .cell::before{transform:scaleX(1)}
  .gauge:hover{transform:none}
  .tab:not(.btn),.tab:not(.btn)::after{transition:none}
  .lead:hover{padding-left:20px}
  .card:hover{transform:none}
  .lead::before,.lead:hover::before{transform:scaleY(0)}
  .lead.sel::before{transform:scaleY(1)}
  .section-head.is-visible::after{width:64px}
  body.js .savebar{transition:none}
  body.js:not(.dirty) .savebar{display:none}
}
"""

def esc(v):
    return _html.escape("" if v is None else str(v), quote=True)


# Loaded from Google's CDN, the same three families the live site loads, so the
# panel and the site render in the same type. Every family has a local fallback
# in --font-* above: if the CDN is blocked the panel loses the exact letterforms
# and nothing else. display=swap so text is never invisible while they load.
_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,500&'
    'family=IBM+Plex+Mono:wght@400;500&'
    'family=Inter:wght@400;500;600&display=swap">'
)


def _shell(title, body, body_class=""):
    cls = f' class="{esc(body_class)}"' if body_class else ""
    # The `js` class is added here, in the first bytes of the body, rather than at
    # the end of the page. Everything the panel hides (tab panels, the floating
    # save bar) is hidden by `body.js`, so setting it any later would show the
    # whole page and then collapse it in front of the client. Setting it from the
    # server instead is not an option: a browser with JavaScript off would get a
    # panel with four of its five sections permanently hidden.
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="theme-color" content="#08090B">'
        f'<title>{esc(title)}</title>{_FONTS}<style>{CSS}</style></head>'
        f'<body{cls}><script>document.body.className='
        '(document.body.className+" js").trim();'
        # Watchdog. `js` hides every tab panel, so if the enhancement bundle at
        # the bottom of the page dies before it opens one, the client would be
        # looking at an empty desk. If nothing has opened a panel a second and a
        # half in, take `js` back off and fall through to the plain long page,
        # where every panel is visible and every form still works.
        'setTimeout(function(){var b=document.body;'
        'if(b.querySelector(".panel")&&!b.querySelector(".panel.is-on"))'
        'b.className=b.className.replace(/\\bjs\\b/g,"").trim()},1600)</script>'
        f'{body}</body></html>'
    )


def _gate(heading, lede, inner, eyebrow=_BRAND):
    """The two pages a signed-out visitor can reach. Identical frame, so a wrong
    password and a closed panel look like the same object rather than two bugs."""
    return _shell("Lead desk", f"""
<div class="gate"><div class="gate-card crop-frame">
  <span class="corner tl"></span><span class="corner tr"></span>
  <span class="corner bl"></span><span class="corner br"></span>
  <div class="gate-head">
    <span class="eyebrow">{esc(eyebrow)}</span>
    <h1>{heading}</h1>
    <p>{esc(lede)}</p>
  </div>
  {inner}
</div></div>""", body_class="on-dark")


def render_login(error=None):
    """Deliberately says nothing about the business or whether a password exists."""
    err = f'<p class="err">{esc(error)}</p>' if error else ""
    # This page carries none of the panel's enhancement bundle — a sign-in form
    # should not depend on 900 lines of decoration loading — so the reveal button
    # brings its own six lines. Everything about the field is unchanged when they
    # do not run: same name, same type, same autocomplete, posted identically.
    return _gate(
        "Lead <em style=\"font-style:italic;color:var(--brass-soft)\">desk</em>",
        "Enter the password you were given to manage the WhatsApp and Instagram agent.",
        f"""<form method="post" action="/dashboard/login">
    {err}
    <label for="pw">Password</label>
    <span class="pw">
      <input id="pw" type="password" name="password" autocomplete="current-password"
             required autofocus>
      <button type="button" class="pw-eye" data-pw-eye aria-controls="pw"
              aria-pressed="false" aria-label="Show password">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M1.9 12S5.6 5.6 12 5.6 22.1 12 22.1 12 18.4 18.4 12 18.4 1.9 12 1.9 12Z"/>
          <circle class="iris" cx="12" cy="12" r="3.15"/>
          <line class="slash" x1="4.2" y1="19.8" x2="19.8" y2="4.2"/>
        </svg>
      </button>
    </span>
    <button class="btn btn-solid" type="submit">Sign in <span class="btn-arrow">&rarr;</span></button>
  </form>
  <script>(function(){{
    var f=document.getElementById("pw"),b=document.querySelector("[data-pw-eye]");
    if(!f||!b)return;
    b.addEventListener("click",function(){{
      var show=f.type==="password",at=f.selectionStart,to=f.selectionEnd;
      f.type=show?"text":"password";
      b.setAttribute("aria-pressed",show?"true":"false");
      /* Changing the type drops the caret to the end in several browsers, which
         sends the next character typed to the wrong place. Put it back, and keep
         the focus on the field rather than leaving it on the button. */
      f.focus();
      try{{f.setSelectionRange(at,to)}}catch(e){{}}
    }});
  }})();</script>""")


def render_locked():
    """Shown when DASHBOARD_PASSWORD is unset. The panel can start campaigns and
    rewrite the agent's prompt, so an unset password must close it, not open it."""
    return _gate(
        "Panel closed",
        "This panel is switched off because no password has been set for it.",
        """<div class="gate-body">
    <p class="fine">Set <code>DASHBOARD_PASSWORD</code> in the server's
    <code>.env</code> and restart. Until then the agent keeps running normally —
    answering messages, following up, escalating. Only this panel is closed.</p>
  </div>""", eyebrow=f"{_BRAND} &middot; closed")

# --- Pieces -----------------------------------------------------------------

def _ago(ts):
    """Relative time, because 'how long ago' is the only thing the client asks."""
    if not ts:
        return "—"
    try:
        t = _dt.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return esc(ts)
    secs = (_dt.now() - t).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 172800:
        return "yesterday"
    return f"{t.day} {t.strftime('%b')}"


def _val(settings, key):
    return (settings.get(key) or {}).get("value", "")


def _is_on(settings, key):
    return str(_val(settings, key)).strip().lower() in ("1", "true", "yes", "on")


def _int(value, default=0):
    """Settings and stats both arrive as strings often enough to be worth one
    helper rather than a try/except at every call site."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _pct(part, whole):
    if not whole:
        return 0
    return max(0, min(100, round(part * 100.0 / whole)))


def _meter(part, whole, hot=False, live=None):
    """A 3px bar that grows from zero on load. Width is inline because it is data;
    the growth is CSS because it is decoration. `live` names the key the poll reads
    to move it, and data-cap saves the script from scraping the ceiling off the
    label next to it."""
    at = f' data-meter="{esc(live)}" data-cap="{esc(whole)}"' if live else ""
    return (f'<div class="meter{" hot" if hot else ""}"{at}>'
            f'<i style="width:{_pct(part, whole)}%"></i></div>')

def _cell(label, value, unit="", flagged=False, live=None, live_unit=None, flag=None):
    """One number in the masthead.

    `live` names the key in /dashboard/live that replaces this number on each
    poll, so the strip stays true without a page reload. `flag` names the key
    that decides whether the cell turns brass — set on "Needs you", so a lead
    going hot lights the cell up while the client is looking at another tab.
    data-count is what the count-up animation reads.
    """
    live_at = f' data-live="{esc(live)}"' if live else ""
    v = f'<span data-count{live_at}>{esc(value)}</span>'
    if unit:
        attr = f' data-live-unit="{esc(live_unit)}"' if live_unit else ""
        v += f' <span class="u"{attr}>{esc(unit)}</span>'
    cls = "cell flagged" if flagged else "cell"
    at = f' data-flag="{esc(flag)}"' if flag else ""
    return (f'<div class="{cls}"{at}><span class="k">{esc(label)}</span>'
            f'<span class="v">{v}</span></div>')


def _masthead(stats, queue, settings, tabs=""):
    replying = _is_on(settings, "reply_auto")
    hot = _int(stats.get("escalated_open"))
    cap = _int(stats.get("cold_daily_cap"))
    cold = _int(stats.get("cold_sent_today"))
    queued = _int(queue.get("queued_conversations"))
    workers = _int(queue.get("workers"))
    ai = _int(queue.get("ai_calls_last_minute"))
    ai_cap = _int(_val(settings, "brain_max_per_minute"), 1) or 1

    # Worker cells: one per configured thread, lit up to the number in flight.
    slots = max(1, _int(_val(settings, "reply_workers"), workers or 1))
    busy = min(slots, queued if queued < slots else slots)
    mini = "".join(f'<b class="{"lit" if i < busy else ""}"></b>' for i in range(slots))

    # Two lines, each in its own overflow box, rather than one block with a <br>:
    # the CSS lifts them in one after the other. Marked up rather than split by
    # script so the reveal happens with JavaScript switched off too, and so the
    # break falls where the sentence wants it instead of where the column ends.
    headline = ('<span class="ln"><span>Every message,</span></span>'
                '<span class="ln"><span>answered <em>on time</em>.</span></span>'
                if replying else
                '<span class="ln"><span>The agent is <em>paused</em>.</span></span>'
                '<span class="ln"><span>Nothing is going out.</span></span>')
    lede = ("Replies are held back a random few seconds so they read as typed. "
            "Everything below is live — the numbers refresh on their own."
            if replying else
            "Messages still arrive and are still logged. Turn “Answer new messages” "
            "back on under Behaviour and the queue drains from where it stopped.")

    cells = (
        _cell("Replies sent", _int(stats.get("replies_sent_today")),
              live="replies_sent_today")
        + _cell("Total outreach per day", cold, f"of {cap}",
                live="cold_sent_today", live_unit="cold_daily_cap")
        + _cell("Follow-ups", _int(stats.get("followups_sent_today")),
                live="followups_sent_today")
        + _cell("Waiting to reply", queued, f"{workers} working",
                live="queued_conversations", live_unit="workers")
        + _cell("Needs you", hot, "", bool(hot), live="escalated_open",
                flag="escalated_open")
    )

    pill = ('<span class="state"><span class="dot"></span>Answering</span>' if replying
            else '<span class="state off"><span class="dot"></span>Paused</span>')

    return f"""
<nav class="site-nav"><div class="wrap">
  <a class="logo" href="/dashboard"><img class="logo-mark" src="{LOGO_MARK}" alt=""
       width="36" height="36" decoding="async"><span class="logo-type">{_BRAND_MAIN}
    <span>{_BRAND_REST}</span><s>Lead desk</s></span></a>
  {tabs}
  <div class="nav-right">
    <span class="nav-sep"></span>
    {pill}
    <span class="clock" data-clock></span>
    <a class="btn btn-mini" href="/dashboard/logout">Sign out</a>
  </div>
</div></nav>
<header class="masthead on-dark">
  <span class="mast-ground" aria-hidden="true"></span>
  <div class="wrap">
  <div class="mast-top">
    <div class="mast-head">
      <span class="eyebrow">Lead desk &middot; today</span>
      <h1>{headline}</h1>
      <p class="lede">{esc(lede)}</p>
    </div>
    <div class="gauges">
      <div class="gauge crop-frame">
        <span class="corner tl"></span><span class="corner br"></span>
        <div class="gauge-top"><span class="gauge-k">In the queue</span>
          <span class="gauge-v"><span data-live="queued_conversations">{queued}</span>
          <s>/ {slots} at once</s></span></div>
        <div class="cells-mini" data-slots="{slots}">{mini}</div>
        <div class="sparkline" data-spark>
          <svg viewBox="0 0 100 26" preserveAspectRatio="none" aria-hidden="true">
            <polyline points=""></polyline></svg>
          <span class="spark-k">Queue since you opened this page</span>
        </div>
      </div>
      <div class="gauge">
        <div class="gauge-top"><span class="gauge-k">AI replies this minute</span>
          <span class="gauge-v"><span data-live="ai_calls_last_minute">{ai}</span>
          <s>/ {ai_cap} allowed</s></span></div>
        {_meter(ai, ai_cap, hot=ai >= ai_cap, live="ai_calls_last_minute")}
        <span class="gauge-note">Rolling sixty seconds, not a daily total</span>
      </div>
    </div>
  </div>
  <div class="cells">{cells}</div>
</div></header>"""

def _lead_state(lead):
    """One word for what this conversation is, used by the filter chips. Same
    precedence as _tag — see the comment there, it is load-bearing."""
    intent = (lead.get("last_intent") or "").lower()
    # Anything the agent has deliberately stopped talking to belongs in Stopped,
    # not "Yours": a number we gave up on (undeliverable), a lead who asked to
    # STOP (opted_out), someone who said they're not interested, or a time-waster
    # the agent cut off. None of these is a conversation the owner must answer.
    if intent in ("time_waster", "undeliverable", "opted_out", "not_interested"):
        return "stopped"
    if lead.get("muted"):
        return "mine"
    if intent == "serious":
        return "hot"
    return ""


def _tag(lead):
    intent = (lead.get("last_intent") or "").lower()
    # Checked before `muted`, because a lead the agent cut off IS muted and
    # labelling it "Yours" would send the owner off to answer someone the agent
    # deliberately stopped paying for. store.clear_off_topic drops this verdict
    # when the lead is handed back, so the tag can't go stale.
    if intent == "time_waster":
        return '<span class="tag cool">Stopped</span>'
    # Gave-up-on numbers read as "Couldn't reach" and sit under Stopped, never
    # "Yours" — there is nothing for the owner to take over.
    if intent == "undeliverable":
        return '<span class="tag cool">Couldn’t reach</span>'
    # Checked before `muted` too: a lead who asked to STOP is muted, but labelling
    # them "Yours" would send the owner to answer someone who opted out.
    # Both mean the lead asked to stop — the brain reports the opt-out as
    # 'not_interested'; 'opted_out' is kept as an alias in case that ever splits.
    if intent in ("opted_out", "not_interested"):
        return '<span class="tag cool">Opted out</span>'
    if lead.get("muted"):
        return '<span class="tag mine">Yours</span>'
    if intent == "serious":
        return '<span class="tag hot">Hot</span>'
    return ""


def _when(ts):
    """A timestamp the script can keep fresh. The relative wording is rendered
    server-side too, so the panel is never blank without JavaScript — the script
    only re-runs the same arithmetic every minute so "just now" stops lying."""
    return f'<span class="when" data-at="{esc(ts or "")}">{esc(_ago(ts))}</span>'


def _stop_reason(lead):
    """One plain line saying WHY a conversation is Stopped or Cold, so the client
    can tell at a glance why the agent left it alone — shown under the row and in
    the thread header. Keyed on the same intents _tag/_lead_state read."""
    intent = (lead.get("last_intent") or "").lower()
    if intent == "time_waster":
        return "Stopped — too many off-topic messages"
    if intent == "undeliverable":
        return "Stopped — couldn’t be delivered after 3 attempts"
    if intent in ("opted_out", "not_interested"):
        return "Stopped — they asked not to be contacted"
    return ""


def _lead_row(lead, selected):
    num = lead.get("lead")
    who = "They said" if lead.get("last_role") == "user" else "Agent said"
    label = channel.short_label(num)
    sel = " sel" if str(num) == str(selected) else ""
    # data-find is what the search box matches on: the label the client can see
    # plus the message text, lowercased once here rather than per keystroke.
    find = f'{label} {lead.get("last_text") or ""}'.lower()
    reason = _stop_reason(lead)
    why = f'<span class="lead-why">{esc(reason)}</span>' if reason else ""
    return (
        f'<a class="lead{sel}" href="/dashboard?lead={esc(num)}" data-lead'
        f' data-state="{esc(_lead_state(lead))}" data-find="{esc(find)}">'
        f'<span class="lead-top">'
        f'<span class="num">{esc(label)}{_tag(lead)}</span>{_when(lead.get("last_at"))}</span>'
        f'<span class="snip">{esc(who)}: {esc(lead.get("last_text"))}</span>{why}</a>'
    )

def _needs_you(escalated, leads_by_num):
    if not escalated:
        return (
            '<div class="card"><h2>Needs you</h2><div class="empty">'
            '<span class="mark"></span><b>Nothing waiting</b>'
            'Hot leads land here the moment the agent spots one, and you get a '
            'WhatsApp alert at the same time.</div></div>'
        )
    rows = []
    for e in escalated:
        num = e.get("lead")
        last = (leads_by_num.get(str(num)) or {}).get("last_text") or ""
        # The agent normally keeps replying through an escalation, so most rows
        # here were never muted and "Give back to agent" would read as nonsense.
        # Same endpoint either way; only the label changes.
        if e.get("muted"):
            status = '<span class="tag mine">Yours</span>'
            action = "Give back to agent"
        else:
            status = '<span class="tag hot">Hot</span>'
            action = "Done — clear this"
        rows.append(
            f'<div class="lead">'
            f'<span class="lead-top"><span class="num">{esc(channel.short_label(num))}'
            f'{status}</span>{_when(e.get("escalated_at"))}</span>'
            f'<span class="snip">{esc(last)}</span>'
            f'<div class="bar" style="border-top:0;padding:12px 0 0">'
            f'<a class="btn btn-mini" href="/dashboard?lead={esc(num)}#inbox">Read thread</a>'
            f'<form method="post" action="/dashboard/lead/unmute" style="display:inline">'
            f'<input type="hidden" name="lead" value="{esc(num)}">'
            f'<button class="btn btn-mini" type="submit">{action}</button></form>'
            f'</div></div>'
        )
    return ('<div class="card accent"><h2>Needs you '
            f'<em class="hot">&middot; {len(escalated)}</em></h2>{"".join(rows)}</div>')


def _conversations(leads, selected):
    if not leads:
        return ('<div class="card"><h2>Conversations</h2><div class="empty">'
                '<span class="mark"></span><b>No conversations yet</b>'
                'The first WhatsApp message or Instagram DM shows up here, with the '
                'whole thread behind it.</div></div>')
    rows = "".join(_lead_row(l, selected) for l in leads)
    counts = {"hot": 0, "mine": 0, "stopped": 0}
    for l in leads:
        state = _lead_state(l)
        if state in counts:
            counts[state] += 1
    chips = "".join(
        f'<button type="button" class="chip{" is-on" if key == "all" else ""}"'
        f' data-filter="{key}">{label}{f" · {n}" if n else ""}</button>'
        for key, label, n in (
            ("all", "All", len(leads)), ("hot", "Hot", counts["hot"]),
            ("mine", "Yours", counts["mine"]), ("stopped", "Stopped", counts["stopped"]),
        )
    )
    return (
        f'<div class="card"><h2>Conversations <em>&middot; {len(leads)}</em></h2>'
        '<div class="listtools">'
        '<input class="search" type="search" data-search autocomplete="off"'
        ' placeholder="Search a number, a handle, a message… (press /)">'
        f'<div class="chips">{chips}</div></div>'
        f'{rows}<div class="noresult" data-noresult>Nothing matches that.</div></div>'
    )

def _clock(ts):
    """A bubble's own time, 12-hour.

    store._now() writes "2026-09-05 21:40:12" and the thread was printing that
    string verbatim, so every message carried a 24-hour clock, seconds, and a
    full ISO date. Inside one conversation the date is repetition — today's
    bubbles get the time alone, and only an older one keeps its date, which is
    also the line where a thread visibly spans two days."""
    if not ts:
        return ""
    try:
        t = _dt.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ts)
    # %I is 01-12, so lstrip can only ever take the hour's leading zero; the
    # minutes keep theirs or 9:05 pm reads as 9:5 pm.
    clock = f'{t.strftime("%I:%M").lstrip("0")} {t.strftime("%p").lower()}'
    if t.date() == _dt.now().date():
        return clock
    return f'{t.day} {t.strftime("%b")}, {clock}'


def _transcript(lead, turns, muted, intent=None):
    label = channel.short_label(lead)
    if not turns:
        bubbles = ('<div class="empty"><span class="mark"></span><b>No messages yet</b>'
                   'This conversation has no history on the agent.</div>')
    else:
        # --i staggers the bubbles in; capped so a 200-turn thread doesn't take
        # ten seconds to appear.
        bubbles = '<div class="turns" data-turns>' + "".join(
            f'<div class="turn {"model" if t.get("role") == "model" else "user"}"'
            f' style="--i:{min(i, 14)}">{esc(t.get("text"))}'
            f'<span class="at">{esc(_clock(t.get("at")))}</span></div>'
            for i, t in enumerate(turns)
        ) + "</div>"
    if muted:
        action = ('<form method="post" action="/dashboard/lead/unmute" style="display:inline">'
                  f'<input type="hidden" name="lead" value="{esc(lead)}">'
                  '<button class="btn" type="submit">Give back to agent</button></form>')
        # Muted WITH history = a human took the conversation over. Muted with no
        # history at all is a number the system gave up on after 3 failed
        # deliveries — never a conversation, so it must not read as one waiting
        # on the owner. And a lead the agent stopped for a REASON (opted out, not
        # interested, time-waster, undeliverable) reads as Stopped with that
        # reason, not "You are answering this one".
        stop_labels = {
            "time_waster":    "Stopped &middot; too many off-topic messages",
            "undeliverable":  "Stopped &middot; couldn’t be reached",
            "opted_out":      "Stopped &middot; they asked not to be contacted",
            "not_interested": "Stopped &middot; they asked not to be contacted",
        }
        stop = stop_labels.get((intent or "").lower())
        if stop:
            who = f'<span class="tag cool">{stop}</span>'
        elif turns:
            who = '<span class="tag mine">You are answering this one</span>'
        else:
            who = '<span class="tag cool">Stopped &middot; couldn’t be reached</span>'
    else:
        action = ('<form method="post" action="/dashboard/lead/mute" style="display:inline">'
                  f'<input type="hidden" name="lead" value="{esc(lead)}">'
                  '<button class="btn btn-danger" type="submit">I\'ll take this one</button></form>')
        who = '<span class="tag ok">The agent is answering this one</span>'
    return (
        '<div class="card accent"><div class="card-head thread-head">'
        f'<span class="thread-who">{esc(label)}{who}</span>'
        f'<button type="button" class="copy" data-copy="{esc(label)}">Copy</button>'
        f'</div>{bubbles}'
        f'<div class="bar">{action}'
        '<a class="btn btn-mini" href="/dashboard">Close</a></div></div>'
    )


def _num_rows(settings, fields):
    out = []
    for key, label, unit in fields:
        value = _val(settings, key)
        level, message = band_for(key, value)
        # Rendered server-side as well as live, so the verdict on the value the
        # client is looking at is already on the page with JavaScript switched
        # off — the script below only keeps it in step while they type.
        extra = note = ""
        if key in BANDS:
            extra = f' data-bands="{esc(_json.dumps(BANDS[key]))}"'
            note = f'<p class="note{f" {level}" if level else ""}">{esc(message)}</p>'
        cls = "row banded" if key in BANDS else "row"
        if level:
            cls += f" lvl-{level}"
        low, high = RANGES.get(key, (0, 10 ** 9))
        bump = STEPS.get(key, 1)
        # A real input[type=number] with two buttons over it, not a widget. The
        # field keeps its name, its value, its keyboard arrows and its screen
        # reader semantics; the buttons are an extra way in for a pointer, which
        # is why they are type="button" (one submit button in this form, see the
        # docstring) and out of the tab order (eleven fields would otherwise cost
        # thirty-three tab stops to walk past). With the script off they are
        # hidden and the browser's own spinners come back — see the CSS.
        #
        # step stays "any". Putting the bump in `step` would make the browser
        # refuse to submit anything off the grid, so a client who types 46 into a
        # field that steps by 5 would get a form that silently will not save.
        stepper = (
            f'<span class="stepper" data-bump="{bump}">'
            f'<button type="button" class="step" data-step="-1" tabindex="-1"'
            f' aria-label="Lower {esc(label)}">&minus;</button>'
            f'<input id="f_{key}" type="number" name="{key}" step="any"'
            f' min="{low}" max="{high}" inputmode="decimal"'
            f' value="{esc(value)}">'
            f'<button type="button" class="step" data-step="1" tabindex="-1"'
            f' aria-label="Raise {esc(label)}">+</button></span>'
        )
        out.append(
            f'<div class="{cls}"{extra}>'
            f'<label class="lbl" for="f_{key}">{esc(label)}</label>'
            f'<span class="numset">{stepper}'
            f'<span class="u">{esc(unit)}</span></span>{note}</div>'
        )
    return "".join(out)

# --- Behaviour --------------------------------------------------------------
# Two separate scripts, on purpose.
#
# _GUARDS is the one that protects the phone number: it repaints the band verdict
# while the client types and puts a confirm box in front of a dangerous save. It
# touches nothing but the banded rows and window.confirm, which is what lets
# preview/test_panel_script.js run it against a hand-built DOM in Node and prove
# the warnings without a browser. Keep it first on the page and keep it small.
#
# _UI is everything else, and every line of it is decoration or convenience. If
# it throws, the panel is still fully usable: the tabs are all open, the numbers
# are the ones the server rendered, and Save is an ordinary submit button.

_GUARDS = """
(function () {
  // Scrolling the settings column with the pointer over a number field must not
  // silently edit it — browsers step the value on wheel. Dropping focus stops it.
  var nums = document.querySelectorAll('input[type="number"]');
  for (var i = 0; i < nums.length; i++) {
    nums[i].addEventListener("wheel", function () { this.blur(); });
  }

  // Same table the server used, shipped down per field, so one edit to BANDS
  // moves the sentence under the field and the sentence in the confirm box.
  function verdict(bands, raw) {
    var s = String(raw === null || raw === undefined ? "" : raw).trim();
    if (s === "") return null;
    var n = parseFloat(s);
    if (isNaN(n)) return null;
    for (var i = 0; i < bands.length; i++) {
      if (bands[i][0] === null || n <= bands[i][0]) {
        return { level: bands[i][1], message: bands[i][2] };
      }
    }
    return null;
  }

  var rows = [], forms = [];
  var found = document.querySelectorAll(".row.banded");

  for (var r = 0; r < found.length; r++) {
    (function (el) {
      var input = el.querySelector("input");
      var note = el.querySelector(".note");
      var lbl = el.querySelector(".lbl");
      if (!input || !note) return;
      var bands;
      try { bands = JSON.parse(el.getAttribute("data-bands")); } catch (e) { return; }
      if (!bands || !bands.length) return;

      var row = { el: el, input: input, note: note, bands: bands, level: "",
                  label: (lbl && lbl.textContent ? lbl.textContent : "this field") };
      rows.push(row);
      paint(row);
      input.addEventListener("input", function () { paint(row); });

      var form = el.closest("form");
      if (form && forms.indexOf(form) < 0) forms.push(form);
    })(found[r]);
  }

  function paint(row) {
    var v = verdict(row.bands, row.input.value);
    var level = v ? v.level : "";
    row.level = level;
    row.el.className = "row banded" + (level ? " lvl-" + level : "");
    row.note.className = "note" + (level ? " " + level : "");
    row.note.textContent = v ? v.message : "";
  }

  for (var f = 0; f < forms.length; f++) {
    forms[f].addEventListener("submit", function (ev) {
      var stops = [];
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].level === "stop") stops.push(rows[i]);
      }
      if (!stops.length) return;
      // Amber never asks. Only "stop" — the band that ends in a restricted
      // number — is worth interrupting a save for, or the box gets clicked
      // through without being read.
      var lines = [stops.length === 1
        ? "This is how a WhatsApp number gets restricted."
        : "These are how a WhatsApp number gets restricted.", ""];
      for (var j = 0; j < stops.length; j++) {
        lines.push(stops[j].label + " \\u2014 " + String(stops[j].input.value).trim());
        lines.push(stops[j].note.textContent, "");
      }
      lines.push("Save anyway?");
      if (!window.confirm(lines.join("\\n"))) ev.preventDefault();
    });
  }
})();
"""

_UI = """
(function () {
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) {
    return Array.prototype.slice.call((r || document).querySelectorAll(s));
  };
  var body = document.body;
  var calm = window.matchMedia &&
             window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var fine = window.matchMedia && window.matchMedia("(pointer: fine)").matches;

  // ---- the site's two ambient devices, injected rather than marked up -------
  var progress = document.createElement("div");
  progress.className = "scroll-progress";
  body.appendChild(progress);

  var glow = null;
  if (fine && !calm && window.innerWidth > 980) {
    glow = document.createElement("div");
    glow.className = "cursor-glow";
    body.appendChild(glow);
    var gx = window.innerWidth / 2, gy = window.innerHeight / 2, cx = gx, cy = gy;
    document.addEventListener("mousemove", function (e) {
      gx = e.clientX; gy = e.clientY; glow.classList.add("is-active");
    });
    (function drift() {
      cx += (gx - cx) * 0.12; cy += (gy - cy) * 0.12;
      glow.style.transform = "translate(" + (cx - 260) + "px," + (cy - 260) + "px)";
      requestAnimationFrame(drift);
    })();
  }

  // ---- nav: compress on scroll, but never leave ----------------------------
  // It used to slide away on the way down. The tab strip sticks underneath it,
  // so a departing nav left a band the panels scrolled through. It stays.
  var nav = $(".site-nav");
  function onScroll() {
    var y = window.pageYOffset || document.documentElement.scrollTop || 0;
    var h = document.documentElement.scrollHeight - window.innerHeight;
    progress.style.width = (h > 0 ? Math.min(100, (y / h) * 100) : 0) + "%";
    if (nav) nav.classList.toggle("is-scrolled", y > 80);
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // ---- reveal on scroll, and count the numbers up once they are in view ----
  var counted = false;
  function countUp(el) {
    var target = parseFloat(String(el.textContent).replace(/,/g, "")) || 0;
    if (calm || target <= 0 || target > 100000) return;
    var t0 = 0;
    (function step(now) {
      if (!t0) t0 = now;
      var p = Math.min(1, (now - t0) / 1200);
      el.textContent = String(Math.round(target * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(step);
      else el.textContent = String(target);
    })(0);
  }

  if ("IntersectionObserver" in window) {
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        en.target.classList.add("is-visible");
        seen.unobserve(en.target);
      });
    }, { threshold: 0.14, rootMargin: "0px 0px -60px 0px" });
    $$(".reveal, .section-head").forEach(function (el, i) {
      el.style.transitionDelay = calm ? "0ms" : (i % 4) * 90 + "ms";
      seen.observe(el);
    });
  } else {
    $$(".reveal, .section-head").forEach(function (el) {
      el.classList.add("is-visible");
    });
  }
  if (!counted) { counted = true; $$("[data-count]").forEach(countUp); }

  // ---- magnetic buttons, straight off the site ------------------------------
  if (fine && !calm) {
    $$(".btn").forEach(function (b) {
      b.addEventListener("mousemove", function (e) {
        var r = b.getBoundingClientRect();
        var x = e.clientX - r.left - r.width / 2, y = e.clientY - r.top - r.height / 2;
        b.style.transform = "translate(" + x * 0.18 + "px," + y * 0.35 + "px)";
      });
      b.addEventListener("mouseleave", function () { b.style.transform = ""; });
    });
  }

  // ---- times: the server rendered "just now", this keeps it honest ---------
  function parseAt(s) {
    var m = /^(\\d{4})-(\\d{2})-(\\d{2})[ T](\\d{2}):(\\d{2})(?::(\\d{2}))?/.exec(s || "");
    return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : null;
  }
  var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function ago(t) {
    var s = (Date.now() - t.getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 172800) return "yesterday";
    return t.getDate() + " " + MON[t.getMonth()];
  }
  function ticks() {
    $$("[data-at]").forEach(function (el) {
      var t = parseAt(el.getAttribute("data-at"));
      if (t) el.textContent = ago(t);
    });
    var clock = $("[data-clock]");
    if (clock) {
      // 12-hour to match the rest of the panel. The hour loses its leading zero,
      // the minutes keep theirs — otherwise 9:05 reads as 9:5.
      var n = new Date(), h = n.getHours(), mm = n.getMinutes();
      var half = h < 12 ? "am" : "pm";
      h = h % 12 || 12;
      clock.textContent = h + ":" + (mm < 10 ? "0" : "") + mm + " " + half;
    }
  }
  ticks();
  setInterval(ticks, 30000);

  // ---- tabs: one page, five views ------------------------------------------
  var panels = $$(".panel[data-panel]");
  var tabs = $$(".tab[data-go]");
  var deck = $(".deck");

  // Where a tab click should land you. The tabs live in the nav now, so the old
  // "top of the strip minus a nav's worth" is wrong twice over: the strip's own
  // offsetTop is the nav's, about 70px, which clamps the target to 0 and sends
  // every click to the top of the page. The target is the top of the deck, less
  // the height of the sticky nav that would otherwise cover it. Measured per
  // click because the nav compresses on scroll. `|| 0` on both reads keeps this
  // a number on any DOM that does not implement offsets.
  function panelTop() {
    var top = (deck && deck.offsetTop) || 0;
    var navH = (nav && nav.offsetHeight) || 0;
    return Math.max(0, top - navH - 16);
  }

  function remember(name) { try { sessionStorage.setItem("dm-tab", name); } catch (e) {} }
  function recall() { try { return sessionStorage.getItem("dm-tab") || ""; }
                      catch (e) { return ""; } }

  function show(name, moveTo) {
    var hit = null;
    panels.forEach(function (p) {
      var on = p.getAttribute("data-panel") === name;
      p.classList.toggle("is-on", on);
      if (on) hit = p;
    });
    if (!hit) return false;
    // The panel was display:none until this instant, so its own reveal
    // observers never fired and everything inside would sit at opacity 0.
    $$(".reveal, .section-head", hit).forEach(function (el) {
      el.classList.add("is-visible");
    });
    tabs.forEach(function (t) {
      t.classList.toggle("is-on", t.getAttribute("data-go") === name);
    });
    remember(name);
    if (moveTo) {
      if (window.history && history.replaceState) {
        history.replaceState(null, "", "#" + name);
      }
      var y = panelTop();
      if ((window.pageYOffset || 0) > y) {
        try { window.scrollTo({ top: y, behavior: calm ? "auto" : "smooth" }); }
        catch (e) { window.scrollTo(0, y); }
      }
    }
    return true;
  }

  tabs.forEach(function (t) {
    t.addEventListener("click", function () { show(t.getAttribute("data-go"), true); });
  });
  window.addEventListener("hashchange", function () {
    show((location.hash || "").replace(/^#/, ""));
  });

  // ---- confirm before a bulk action ----------------------------------------
  // Only the Developer tab's "Mark everything resolved" carries data-confirm.
  // With the script off the form still posts — resolving is reversible (an
  // issue that happens again reappears on its own), so there is nothing to gate.
  $$("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (ev) {
      if (!window.confirm(f.getAttribute("data-confirm"))) ev.preventDefault();
    });
  });

  // A lead in the query string means the client clicked through to a thread, so
  // open on the inbox even though "Today" is the normal landing view. Otherwise
  // reopen whatever they were last looking at — a save redirects to /dashboard
  // with no hash, and landing back on Today after every save is maddening.
  var start = (location.hash || "").replace(/^#/, "");
  if (!start && /[?&]lead=/.test(location.search)) start = "inbox";
  if (!start) start = recall();
  if (!show(start)) show("today");

  // ---- find a conversation --------------------------------------------------
  var search = $("[data-search]");
  var chips = $$(".chip[data-filter]");
  var leadRows = $$("[data-lead]");
  var noresult = $("[data-noresult]");
  var filter = "all";

  function sift() {
    var q = search ? String(search.value).trim().toLowerCase() : "";
    var shown = 0;
    leadRows.forEach(function (row) {
      var okState = filter === "all" || row.getAttribute("data-state") === filter;
      var okText = !q || (row.getAttribute("data-find") || "").indexOf(q) > -1;
      var on = okState && okText;
      row.style.display = on ? "" : "none";
      if (on) shown++;
    });
    if (noresult) noresult.classList.toggle("on", shown === 0);
  }

  if (search) {
    search.addEventListener("input", sift);
    search.addEventListener("search", sift);
  }
  chips.forEach(function (c) {
    c.addEventListener("click", function () {
      filter = c.getAttribute("data-filter");
      chips.forEach(function (o) { o.classList.toggle("is-on", o === c); });
      sift();
    });
  });

  var coldSearch = $("[data-cold-search]");
  var coldChips = $$(".chip[data-cold-filter]");
  var coldRows = $$("[data-cold-lead]");
  var coldNoresult = $("[data-cold-noresult]");
  var coldFilter = "all";

  function coldSift() {
    var q = coldSearch ? String(coldSearch.value).trim().toLowerCase() : "";
    var shown = 0;
    coldRows.forEach(function (row) {
      var okState = coldFilter === "all" || row.getAttribute("data-cold-state") === coldFilter;
      var okText = !q || (row.getAttribute("data-cold-find") || "").indexOf(q) > -1;
      var on = okState && okText;
      row.style.display = on ? "" : "none";
      if (on) shown++;
    });
    if (coldNoresult) coldNoresult.classList.toggle("on", shown === 0);
  }

  if (coldSearch) {
    coldSearch.addEventListener("input", coldSift);
    coldSearch.addEventListener("search", coldSift);
  }
  coldChips.forEach(function (c) {
    c.addEventListener("click", function () {
      coldFilter = c.getAttribute("data-cold-filter");
      coldChips.forEach(function (o) { o.classList.toggle("is-on", o === c); });
      coldSift();
    });
  });

  // ---- copy a number --------------------------------------------------------
  $$(".copy[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var text = b.getAttribute("data-copy"), was = b.textContent;
      function done() {
        b.textContent = "Copied";
        setTimeout(function () { b.textContent = was; }, 1400);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {});
        return;
      }
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", "");
      ta.style.position = "fixed"; ta.style.left = "-9999px";
      body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      body.removeChild(ta);
    });
  });

  // ---- keyboard: "/" to search, Esc to clear -------------------------------
  document.addEventListener("keydown", function (e) {
    var tag = (e.target && e.target.tagName || "").toLowerCase();
    var typing = tag === "input" || tag === "textarea" || tag === "select";
    if (e.key === "/" && !typing && search) {
      e.preventDefault();
      show("inbox", true);
      search.focus();
      return;
    }
    if (e.key === "Escape" && search && e.target === search) {
      search.value = ""; sift(); search.blur();
    }
  });
  // ---- number steppers ------------------------------------------------------
  // A minus and a plus over an ordinary number field. The field is still the
  // field: this only reads and writes its value, so the guard script's verdict,
  // the unsaved-changes count and the save itself all carry on working through
  // the one event a keystroke would have raised.
  function announce(el) {
    // Nothing fires on a programmatic value change, and two listeners are waiting
    // on one: the guard script repaints this field's warning from "input", and the
    // tracker on the form counts it. Raise it, and let it bubble, or a stepper
    // click is an edit nobody hears.
    var ev = null;
    try { ev = new Event("input", { bubbles: true }); } catch (e) {}
    if (ev && el.dispatchEvent) el.dispatchEvent(ev);
    else if (el.dispatch) el.dispatch("input", { type: "input", target: el });
  }

  function num(v, fallback) {
    var n = parseFloat(String(v === null || v === undefined ? "" : v).trim());
    return isNaN(n) ? fallback : n;
  }

  $$(".stepper").forEach(function (box) {
    var input = box.querySelector("input");
    if (!input) return;
    var bump = num(box.getAttribute("data-bump"), 1) || 1;
    var low = num(input.getAttribute("min"), 0);
    var high = num(input.getAttribute("max"), 1e9);
    var down = box.querySelector('[data-step="-1"]');
    var up = box.querySelector('[data-step="1"]');
    var wait = null, beat = null, repeated = false;

    // Both ends of dashboard.RANGES, on the buttons. A control that stops and
    // says so is the whole advantage a stepper has over a bare field.
    function bounds() {
      var n = num(input.value, low);
      if (down) down.disabled = n <= low;
      if (up) up.disabled = n >= high;
    }

    function move(dir) {
      var now = num(input.value, low);
      // Snapped to the field's own grid on the way, so a value left on 46 by hand
      // lands on 50 rather than 51 — the numbers the buttons make stay round.
      var next = dir > 0 ? Math.floor(now / bump + 1e-9) * bump + bump
                         : Math.ceil(now / bump - 1e-9) * bump - bump;
      next = Math.min(high, Math.max(low, Math.round(next * 1000) / 1000));
      if (next === now) { bounds(); return; }
      input.value = String(next);
      bounds();
      announce(input);
      if (!calm && input.classList) {
        input.classList.remove("bumped");
        void input.offsetWidth;          // restarts the animation rather than skipping it
        input.classList.add("bumped");
      }
    }

    // Press and hold. One of these fields runs 0-1000: a client who would have to
    // click eighty times types instead, and the buttons were decoration.
    function hold(dir) {
      wait = setTimeout(function () {
        beat = setInterval(function () { repeated = true; move(dir); }, 70);
      }, 420);
    }
    function release() {
      if (wait) { clearTimeout(wait); wait = null; }
      if (beat) { clearInterval(beat); beat = null; }
    }

    [[down, -1], [up, 1]].forEach(function (pair) {
      var btn = pair[0], dir = pair[1];
      if (!btn) return;
      btn.addEventListener("click", function () {
        // A hold already moved it; the click that ends the hold must not add one.
        if (repeated) { repeated = false; return; }
        move(dir);
      });
      btn.addEventListener("mousedown", function () { hold(dir); });
      btn.addEventListener("touchstart", function () { hold(dir); });
      ["mouseup", "mouseleave", "touchend", "touchcancel", "blur"]
        .forEach(function (t) { btn.addEventListener(t, release); });
    });

    input.addEventListener("input", bounds);
    bounds();
  });

  // ---- unsaved changes ------------------------------------------------------
  // Behaviour and Limits are one form split across two tabs, so a client can
  // easily change a switch, move to another tab and forget. The bar follows them.
  var form = $("[data-settings]");
  var saving = false;
  if (form) {
    var fields = $$("input,textarea,select", form);
    var was = {};
    fields.forEach(function (el, i) {
      el.setAttribute("data-i", i);
      was[i] = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
    });
    var count = $("[data-dirty-count]");

    function audit() {
      var n = 0;
      fields.forEach(function (el) {
        var now = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
        if (now !== was[el.getAttribute("data-i")]) n++;
      });
      body.classList.toggle("dirty", n > 0);
      if (count) count.textContent = n === 1 ? "1 change" : n + " changes";
      return n;
    }
    form.addEventListener("input", audit);
    form.addEventListener("change", audit);
    form.addEventListener("submit", function () {
      saving = true;
      body.classList.remove("dirty");
    });
    window.addEventListener("beforeunload", function (e) {
      if (saving || !body.classList.contains("dirty")) return;
      e.preventDefault();
      e.returnValue = "";
      return "";
    });

    // ---- how much room is left in the brief ---------------------------------
    $$("textarea", form).forEach(function (ta) {
      var out = ta.parentNode.querySelector("[data-charcount]");
      if (!out) return;
      var max = parseInt(ta.getAttribute("maxlength"), 10) || 8000;
      function tally() {
        var n = ta.value.length;
        out.textContent = n.toLocaleString() + " of " + max.toLocaleString();
        out.classList.toggle("near", n > max * 0.9);
      }
      ta.addEventListener("input", tally);
      tally();
    });
  }
  // ---- live numbers ---------------------------------------------------------
  // /dashboard/live is behind the same session cookie as this page and returns
  // exactly what /health returns, minus nothing: {today: stats, queue: pacer}.
  var spark = $("[data-spark]");
  var line = spark ? spark.querySelector("polyline") : null;
  var slots = $("[data-slots]");
  var meters = $$("[data-meter]");
  var samples = [], misses = 0, poll = null;

  // A figure that moved on its own gets a second of brass, because it is the one
  // change on this page the client did not make and would otherwise have to catch
  // by staring. The class comes back off afterwards, or the next change would find
  // it already applied and animate nothing at all.
  function pulse(el) {
    if (calm || !el.classList) return;
    el.classList.remove("lit-change");
    void el.offsetWidth;                 // restarts the animation rather than skipping it
    el.classList.add("lit-change");
    setTimeout(function () { el.classList.remove("lit-change"); }, 980);
  }

  function paintLive(d) {
    var t = d.today || {}, q = d.queue || {};
    function pick(k) {
      if (t[k] !== undefined && t[k] !== null) return t[k];
      if (q[k] !== undefined && q[k] !== null) return q[k];
      return null;
    }
    $$("[data-live]").forEach(function (el) {
      var v = pick(el.getAttribute("data-live"));
      if (v !== null && String(el.textContent).trim() !== String(v)) {
        el.textContent = String(v);
        pulse(el);
      }
    });
    // The unit reads "of 250" or "4 working", so only the number in it moves.
    $$("[data-live-unit]").forEach(function (el) {
      var v = pick(el.getAttribute("data-live-unit"));
      if (v !== null) el.textContent = el.textContent.replace(/\\d+/, String(v));
    });
    $$("[data-flag]").forEach(function (el) {
      var v = pick(el.getAttribute("data-flag"));
      if (v !== null) el.classList.toggle("flagged", Number(v) > 0);
    });
    meters.forEach(function (m) {
      var v = Number(pick(m.getAttribute("data-meter")) || 0);
      var cap = Number(m.getAttribute("data-cap")) || 1;
      var bar = m.querySelector("i");
      if (bar) bar.style.width = Math.max(0, Math.min(100, Math.round(v * 100 / cap))) + "%";
      m.classList.toggle("hot", v >= cap);
    });
    var queued = Number(pick("queued_conversations") || 0);
    if (slots) {
      var cells = $$("b", slots), lit = Math.min(cells.length, queued);
      cells.forEach(function (c, i) { c.classList.toggle("lit", i < lit); });
    }
    if (line) {
      samples.push(queued);
      if (samples.length > 40) samples.shift();
      if (samples.length > 2) {
        var top = Math.max(1, Math.max.apply(null, samples));
        line.setAttribute("points", samples.map(function (v, i) {
          return ((i / (samples.length - 1)) * 100).toFixed(1) + "," +
                 (24 - (v / top) * 22).toFixed(1);
        }).join(" "));
        spark.classList.add("on");
      }
    }
  }

  function tick() {
    if (document.hidden) return;
    fetch("/dashboard/live", { credentials: "same-origin",
                               headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function (d) { misses = 0; paintLive(d); })
      .catch(function () {
        // Three misses running means the tunnel dropped or the session expired.
        // Stop polling rather than reload: there may be half a rewritten persona
        // sitting in the brief, and a reload would throw it away.
        if (++misses >= 3 && poll) {
          clearInterval(poll);
          poll = null;
          var pill = $(".state");
          if (pill) pill.title = "Live numbers have stopped. Reload the page.";
        }
      });
  }

  if (window.fetch) {
    // First poll is late on purpose — the count-up owns the first two seconds.
    setTimeout(tick, 2200);
    poll = setInterval(tick, 15000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && poll) tick();
    });
  }
})();
"""

def _switches(settings):
    rows = "".join(
        f'<label class="sw" for="s_{key}">'
        f'<input id="s_{key}" type="checkbox" name="{key}" value="true"'
        f'{" checked" if _is_on(settings, key) else ""}>'
        f'<span><b>{esc(label)}</b><small>{esc(hint)}</small></span></label>'
        for key, label, hint in SWITCHES
    )
    return f'<div class="card"><h2>What the agent does</h2>{rows}</div>'


def _texts(settings):
    out = []
    for key, label, hint in TEXTS:
        out.append(
            f'<div class="card reveal"><h2>{esc(label)}</h2>'
            f'<div class="field"><p class="hint">{esc(hint)}</p>'
            f'<textarea id="t_{key}" name="{key}" maxlength="{MAX_TEXT}" spellcheck="true"'
            f' rows="10">{esc(_val(settings, key))}</textarea>'
            '<span class="charcount" data-charcount></span></div></div>'
        )
    return "".join(out)


def _readout(settings):
    """The whole configuration in one strip, so nobody has to open two tabs and
    hold three numbers in their head to answer "what is it set to right now"."""
    lo = _int(_val(settings, "reply_delay_min_seconds"))
    hi = _int(_val(settings, "reply_delay_max_seconds"))
    strikes = _int(_val(settings, "off_topic_strikes_max"))
    cells = [
        ("Answering", "On" if _is_on(settings, "reply_auto") else "Off",
         "on" if _is_on(settings, "reply_auto") else "off"),
        ("Reply after", f"{lo}–{hi}s", ""),
        ("Conversations at once", _val(settings, "reply_workers"), ""),
        ("AI ceiling", f'{_val(settings, "brain_max_per_minute")}/min', ""),
        ("New leads first", "On" if _is_on(settings, "cold_auto") else "Off",
         "on" if _is_on(settings, "cold_auto") else "off"),
        ("New leads per day", _val(settings, "cold_daily_cap"), ""),
        ("Gap between sends", f'{_val(settings, "cold_pacing_seconds")}s', ""),
        ("Follow-ups", f'{_val(settings, "followup_max")} × after '
                       f'{_val(settings, "followup_after_hours")}h',
         "" if _is_on(settings, "followup_auto") else "off"),
        ("Stop time-wasters", f"{strikes} strikes" if strikes else "Never", ""),
    ]
    body = "".join(
        f'<div><span class="k">{esc(k)}</span>'
        f'<span class="v{f" {cls}" if cls else ""}">{esc(v)}</span></div>'
        for k, v, cls in cells
    )
    return (f'<div class="card"><h2>How the desk is set right now</h2>'
            f'<div class="readout">{body}</div></div>')

def _stamp(epoch):
    """Epoch seconds as a date the client recognises. 0 means it never happened."""
    n = _int(epoch)
    if n <= 0:
        return "never"
    try:
        t = _dt.fromtimestamp(n)
    except (OverflowError, OSError, ValueError):
        return "never"
    # 12-hour, and no leading zero on either the day or the hour: %d/%I would give
    # "05 Sep, 09:40 PM" where a person writes "5 Sep, 9:40 pm". lstrip is safe on
    # the hour because %I is 01-12, so it can only ever remove the one zero.
    clock = t.strftime("%I:%M").lstrip("0")
    return f"{t.day} {t.strftime('%b')}, {clock} {t.strftime('%p').lower()}"


# state -> (flash level, one sentence the client can act on). "ok" and "unknown"
# say nothing above the tabs; there is nothing for them to do about either.
_CONN_SAY = {
    "missing": ("bad", "Instagram is not connected. WhatsApp is unaffected — "
                       "Instagram DMs are not being answered."),
    "expired": ("bad", "The Instagram connection has expired, so Instagram DMs are "
                       "not being answered. WhatsApp is unaffected."),
    "expiring": ("warn", "The Instagram connection needs renewing shortly. It "
                         "renews itself; this is only worth chasing if it stops."),
}


def _connection_notice(conn):
    level, words = _CONN_SAY.get((conn or {}).get("state"), (None, None))
    if not level:
        return ""
    days = (conn or {}).get("days_left")
    tail = (f" About {days:g} day{'' if days == 1 else 's'} left."
            if level == "warn" and isinstance(days, (int, float)) else "")
    return f'<div class="flash {level}">{esc(words + tail)}</div>'


def _connection_card(conn):
    conn = conn or {}
    state = conn.get("state") or "unknown"
    words = {
        "ok": "Connected and healthy.",
        "expiring": "Connected. Renewing itself shortly.",
        "expired": "Expired — Instagram DMs are not being answered.",
        "missing": "Never connected on this server.",
        "unknown": "Connected. The expiry date is unknown until the first renewal.",
    }.get(state, "Connected.")
    days = conn.get("days_left")
    cls = {"ok": "on", "expiring": "", "expired": "off",
           "missing": "off", "unknown": ""}.get(state, "")
    cells = [
        ("Instagram", state.title(), cls),
        ("Days left", "unknown" if days is None else f"{days:g}", ""),
        ("Token from", conn.get("source") or "—", ""),
        ("Renews", _stamp(conn.get("expires_at")), ""),
        ("Last renewed", _stamp(conn.get("refreshed_at")), ""),
        ("Last checked", _stamp(conn.get("last_checked")), ""),
    ]
    strip = "".join(
        f'<div><span class="k">{esc(k)}</span>'
        f'<span class="v{f" {c}" if c else ""}">{esc(v)}</span></div>'
        for k, v, c in cells
    )
    err = conn.get("last_error") or ""
    tail = (f'<p class="hint" style="padding:15px 20px;border-top:1px solid var(--line)">'
            f'Last failure: {esc(err[:300])}</p>' if err else "")
    return (f'<div class="card"><h2>Connection</h2>'
            f'<p class="hint hint-pad" style="padding-bottom:15px">{esc(words)} '
            'WhatsApp does not expire — its token is set on the server and stays put.'
            f'</p><div class="readout">{strip}</div>{tail}</div>')

def _run_panel(stats, settings, connection):
    """The two buttons that actually spend money and send messages, kept in their
    own view rather than next to the knobs, so neither is ever a mis-click."""
    cap = _int(stats.get("cold_daily_cap"))
    sent = _int(stats.get("cold_sent_today"))
    left = max(0, cap - sent)
    batch = _int(_val(settings, "cold_batch_limit"))
    level, _ = band_for("cold_daily_cap", _val(settings, "cold_daily_cap"))
    warn = ('<p class="hint" style="color:var(--flag);padding:0 20px 15px">'
            'Today\'s ceiling is above what Meta allows this number. Fix it under '
            'Limits before sending a batch by hand.</p>' if level == "stop" else "")

    if not _is_on(settings, "cold_auto"):
        note = ('<p class="hint hint-pad" style="padding-bottom:15px">'
                '“Message new leads first” is switched off, so nothing goes out on '
                'its own. This button still sends one batch by hand.</p>')
    else:
        note = ('<p class="hint hint-pad" style="padding-bottom:15px">'
                'Normally the agent does this on its own schedule. Use this when you '
                'have just added rows to the sheet and do not want to wait.</p>')

    campaign = (
        f'<div class="card reveal"><h2>Message new leads <em>&middot; {left} left today</em></h2>'
        f'{note}{warn}'
        f'<div class="readout"><div><span class="k">Sent today</span>'
        f'<span class="v">{sent} of {cap}</span></div>'
        f'<div><span class="k">This batch sends</span>'
        f'<span class="v">up to {batch}</span></div></div>'
        '<form method="post" action="/dashboard/run/campaign" class="bar">'
        '<button class="btn btn-solid" type="submit">Send a batch now '
        '<span class="btn-arrow">&rarr;</span></button>'
        '</form></div>'
    )
    followups = (
        f'<div class="card reveal"><h2>Follow up on silence</h2>'
        '<p class="hint hint-pad" style="padding-bottom:15px">'
        f'Nudges leads who went quiet, at most {_int(_val(settings, "followup_max"))} '
        f'time{"" if _int(_val(settings, "followup_max")) == 1 else "s"} each and no '
        f'sooner than {_int(_val(settings, "followup_after_hours"))} hours after the '
        'last message. Nobody gets two in one run.</p>'
        f'<div class="readout"><div><span class="k">Sent today</span>'
        f'<span class="v">{_int(stats.get("followups_sent_today"))}</span></div>'
        f'<div><span class="k">Conversations on file</span>'
        f'<span class="v">{_int(stats.get("conversations"))}</span></div></div>'
        '<form method="post" action="/dashboard/run/followups" class="bar">'
        '<button class="btn btn-solid" type="submit">Run follow-ups now '
        '<span class="btn-arrow">&rarr;</span></button>'
        '</form></div>'
    )
    return (
        '<div class="section-head"><h2>Run something now</h2>'
        '<p>Both of these are the same jobs the agent runs on its own. Pressing them '
        'only makes one happen sooner — no limit is skipped.</p></div>'
        f'<div class="split-even">{campaign}{followups}</div>'
        f'<div class="reveal">{_connection_card(connection)}</div>'
    )


def _tabstrip(escalated, leads, err_summary=None):
    """The site's nav links, reused as the panel's view tabs and rendered inside
    the nav bar itself: four plain labels plus the one solid pill where the site
    puts START A PROJECT. `class="tab"` and `data-go` are the contract the script
    and both harnesses read; `.btn .btn-solid` is what makes the last one the
    site's pill. The Today count is flagged because it is the only one that means
    somebody is waiting on a human."""
    def count(n, hot=False):
        if not n:
            return ""
        cls = ' class="hot"' if hot else ""
        return f"<b{cls}>{n}</b>"
    # The Health tab carries the open-issue count, red when any of them is a
    # high-severity one, so "something needs looking at" is legible from the nav
    # before the tab is ever opened.
    issues = err_summary or []
    open_issues = sum(int(i.get("n") or 0) for i in issues)
    any_high = any(i.get("severity") == "high" for i in issues)
    tabs = (
        ("today", "Today", count(len(escalated), True), "tab"),
        ("inbox", "Conversations", count(len(leads)), "tab"),
        ("health", "Health", count(len(issues), any_high), "tab"),
        ("agent", "Behaviour", "", "tab"),
        ("limits", "Limits", "", "tab"),
        ("dev", "Developer", "", "tab"),
        ("run", "Run now", "", "tab btn btn-solid"),
    )
    return '<div class="tabs">' + "".join(
        f'<button type="button" class="{cls}" data-go="{key}">{esc(label)}{extra}</button>'
        for key, label, extra, cls in tabs
    ) + "</div>"


# --- Health & Developer views (the Doctor Desk, folded in) ------------------
# errors.py is the engine — capture, rollup, classify, resolve. These two turn
# what it holds into the panel's own brand: the client reads _health_panel (plain
# language, business impact); I read _dev_panel (source, trace, resolve). Both
# sit behind the one dashboard password, so there is one page and one login
# rather than a second tool at its own URL.

def _sev_chip(severity):
    sev = severity if severity in ("high", "medium", "low") else "medium"
    word = {"high": "Needs attention", "medium": "Worth a look", "low": "Minor"}[sev]
    return f'<span class="sev {sev}">{esc(word)}</span>'


def _health_stats(stats, settings, queue):
    """The client's 'very detailed stats' — every number the desk tracks, in one
    readout, rather than only today's five in the masthead."""
    cap = _int(stats.get("cold_daily_cap"))
    cells = [
        ("Replies sent today", _int(stats.get("replies_sent_today")), ""),
        ("New leads messaged today", f'{_int(stats.get("cold_sent_today"))} of {cap}', ""),
        ("Follow-ups sent today", _int(stats.get("followups_sent_today")), ""),
        ("Outreach left today", _int(stats.get("cold_budget_left")), ""),
        ("Conversations on file", _int(stats.get("conversations")), ""),
        ("Waiting on you", _int(stats.get("escalated_open")),
         "warn" if _int(stats.get("escalated_open")) else ""),
        ("In the reply queue", _int(queue.get("queued_conversations")), ""),
        ("Answering at once", _int(_val(settings, "reply_workers"), 1), ""),
    ]
    strip = "".join(
        f'<div><span class="k">{esc(k)}</span>'
        f'<span class="v{f" {c}" if c else ""}">{esc(v)}</span></div>'
        for k, v, c in cells
    )
    return (f'<div class="card"><h2>The numbers in full</h2>'
            '<p class="hint hint-pad" style="padding-bottom:15px">Everything the desk '
            'is tracking. The five in the header refresh on their own; these are '
            'correct as of when the page loaded.</p>'
            f'<div class="readout">{strip}</div></div>')


def _health_issues(err_summary):
    """One plain-language line per KIND of open issue, worst first. Empty means a
    clean bill of health — which is a card worth showing, not a blank space."""
    items = err_summary or []
    if not items:
        return (
            '<div class="card"><h2>Running smoothly <em>&middot; all clear</em></h2>'
            '<div class="empty"><span class="mark"></span>'
            '<b>Nothing needs your attention</b>'
            'No problems have been logged. If something does go wrong — a reply '
            'that could not be sent, the AI having trouble — it will appear here in '
            'plain language.</div></div>')
    rows = []
    for it in items:
        sev = it.get("severity") or "medium"
        rows.append(
            f'<div class="issue {esc(sev)}"><div class="issue-top">{_sev_chip(sev)}'
            f'<span class="meta">happened <b>{_int(it.get("n"))}</b> time(s), '
            f'last {esc(_ago(it.get("last")))}</span></div>'
            f'<p class="issue-msg">{esc(it.get("client_message"))}</p></div>')
    return (f'<div class="card accent"><h2>Needs a look <em class="hot">'
            f'{len(items)} open</em></h2>'
            '<p class="hint hint-pad" style="padding-bottom:4px">Each line is one '
            'kind of problem, worst first. Most clear up on their own — this is here '
            'so nothing fails silently.</p>'
            f'{"".join(rows)}</div>')


def _health_panel(err_summary, stats, settings, queue, connection):
    return (
        '<div class="section-head"><h2>Health</h2>'
        '<p>Is everything working? This is the whole system in plain language — what '
        'is running, what needs a look, and every number the desk tracks.</p></div>'
        f'{_health_issues(err_summary)}'
        f'<div class="split"><div>{_health_stats(stats, settings, queue)}</div>'
        f'<div class="reveal">{_connection_card(connection)}</div></div>'
    )


def _dev_panel(err_recent):
    """The technical read: every open issue with its source module, the raw log
    line, counts, and a stack trace where one was captured — each with a resolve
    button. resolve() flips it back the moment the same failure happens again."""
    rows = err_recent or []
    head = (
        '<div class="section-head"><h2>Developer</h2>'
        '<p>The same issues as Health, but with the source module, the raw log line '
        'and the stack trace — and a button to clear each once it is dealt with. '
        'Everything here is captured from the running logs.</p></div>')

    if not rows:
        body = ('<div class="card"><h2>Error log <em>&middot; empty</em></h2>'
                '<div class="empty"><span class="mark"></span>'
                '<b>No open issues</b>'
                'Nothing at WARNING or above is outstanding. Resolved issues drop off '
                'here until the same failure happens again.</div></div>')
        return head + body

    resolve_all = (
        '<div class="resolve-all">'
        '<form method="post" action="/dashboard/errors/resolve" data-confirm='
        '"Mark every open issue as resolved?">'
        '<input type="hidden" name="all" value="1">'
        '<button class="btn btn-mini btn-danger" type="submit">Mark everything resolved '
        '<span class="btn-arrow">&rarr;</span></button></form></div>')

    out = []
    for r in rows:
        sev = r.get("severity") or "medium"
        tb = (f'<pre class="trace">{esc(r.get("traceback"))}</pre>'
              if r.get("traceback") else "")
        out.append(
            f'<div class="issue {esc(sev)}"><div class="issue-top">'
            f'<span class="src">{esc(r.get("level"))} &middot; {esc(r.get("source"))}</span>'
            '<form method="post" action="/dashboard/errors/resolve">'
            f'<input type="hidden" name="fingerprint" value="{esc(r.get("fingerprint"))}">'
            '<button class="btn btn-mini" type="submit">Mark resolved</button></form></div>'
            f'<p class="issue-msg">{esc(r.get("tech_message"))}</p>'
            f'<div class="meta">seen <b>×{_int(r.get("count"))}</b> &middot; '
            f'category <b>{esc(r.get("category"))}</b> &middot; '
            f'first {esc(_ago(r.get("first_seen")))} &middot; '
            f'last {esc(_ago(r.get("last_seen")))}</div>{tb}</div>')

    body = (f'<div class="card"><h2>Error log <em>&middot; {len(rows)} open</em></h2>'
            f'{resolve_all}{"".join(out)}</div>')
    return head + body


def _footer():
    return ('<div class="foot"><span>' + _html.escape(_BRAND) + ' &middot; lead desk</span>'
            '<span><span class="live-when">Numbers refresh on their own</span>'
            '<a href="/dashboard">Reload</a>'
            '<a href="/dashboard/logout">Sign out</a></span></div>')

_GOOD_TO_KNOW = (
    '<div class="card"><h2>Worth knowing</h2>'
    '<div class="pad"><p class="hint">'
    '<b>Switching “Answer new messages” off loses nothing.</b> Messages still '
    'arrive and are still filed under Conversations. The agent simply stays quiet '
    'until you switch it back on, and then picks up where it stopped.</p>'
    '<p class="hint" style="margin-top:14px">'
    '<b>The brief below is the whole personality.</b> Write it the way you would '
    'brief a new person on the desk — what you sell, what you never promise, how '
    'to talk about price. It is read fresh on every single reply.</p>'
    '<p class="hint" style="margin-top:14px">'
    '<b>Instagram never hands over.</b> Hot leads from Instagram are logged and '
    'answered, but they do not raise an alert and do not reach “Needs you”. '
    'WhatsApp does both.</p></div></div>'
)


def render(stats=None, queue=None, settings=None, leads=None, cold_leads=None, escalated=None,
           selected=None, turns=None, muted=False, flash=None, flash_bad=False,
           connection=None, err_summary=None, err_recent=None):
    stats = stats or {}
    queue = queue or {}
    settings = settings or {}
    leads = leads or []
    cold_leads = cold_leads or []
    escalated = escalated or []
    turns = turns or []
    err_summary = err_summary or []
    err_recent = err_recent or []
    leads_by_num = {str(l.get("lead")): l for l in leads}

    flash_html = (f'<div class="flash{" bad" if flash_bad else ""}">{esc(flash)}</div>'
                  if flash else "")

    today = (
        '<div class="section-head"><h2>Today</h2>'
        '<p>Everything since midnight. The strip above refreshes on its own, so this '
        'page can be left open on a second screen.</p></div>'
        f'<div class="split"><div>{_needs_you(escalated, leads_by_num)}</div>'
        f'<div class="reveal">{_connection_card(connection)}</div></div>'
        f'<div class="reveal">{_readout(settings)}</div>'
        f'<div class="reveal">{_cold_sent(cold_leads)}</div>'
    )

    if selected:
        # Thread in the wide column: reading it is the reason the client clicked.
        inbox = (f'<div class="split">'
                 f'<div class="reveal">{_transcript(selected, turns, muted, intent=(leads_by_num.get(str(selected)) or {}).get("last_intent"))}</div>'
                 f'<div>{_conversations(leads, selected)}</div></div>')
    else:
        inbox = _conversations(leads, selected)
    inbox = ('<div class="section-head"><h2>Conversations</h2>'
             '<p>Every thread the agent has, newest first. Open one to read it, and '
             'take it over whenever you want to answer yourself.</p></div>') + inbox

    agent = (
        '<div class="section-head"><h2>Behaviour</h2>'
        '<p>What the agent does without being asked, and the words it does it in. '
        'Saved changes apply to the very next message — nothing restarts.</p></div>'
        f'<div class="split"><div>{_switches(settings)}</div>'
        f'<div class="reveal">{_GOOD_TO_KNOW}</div></div>'
        f'{_texts(settings)}'
    )

    limits = (
        '<div class="section-head"><h2>Limits &amp; pacing</h2>'
        '<p>These are what stand between a busy day and a restricted WhatsApp '
        'number. Where a number is risky, it says so under the field as you type.</p>'
        '</div><div class="split-even">'
        f'<div class="card reveal"><h2>Reply speed</h2>{_num_rows(settings, PACING)}</div>'
        '<div class="card reveal"><h2>Outreach &amp; follow-ups</h2>'
        f'{_num_rows(settings, OUTREACH)}</div></div>'
    )

    # Save lives inside the form and outside both panels, so a change made under
    # Behaviour and a change made under Limits go up in the same post — which is
    # also why the panels are inside the form: an unchecked box that is not
    # submitted is how apply_settings reads "off".
    savebar = (
        '<div class="savebar"><span class="what">Applies to the next message'
        ' <b data-dirty-count></b></span>'
        '<button class="btn btn-solid" type="submit">Save'
        ' <span class="btn-arrow">&rarr;</span></button></div>'
    )

    body = f"""{_masthead(stats, queue, settings, _tabstrip(escalated, leads, err_summary))}
<div class="wrap deck">
  {flash_html}{_connection_notice(connection)}
  <section class="panel" data-panel="today">{today}</section>
  <section class="panel" data-panel="inbox">{inbox}</section>
  <section class="panel" data-panel="health">{_health_panel(err_summary, stats, settings, queue, connection)}</section>
  <form method="post" action="/dashboard/settings" data-settings>
    <section class="panel" data-panel="agent">{agent}</section>
    <section class="panel" data-panel="limits">{limits}</section>
    {savebar}
  </form>
  <section class="panel" data-panel="dev">{_dev_panel(err_recent)}</section>
  <section class="panel" data-panel="run">{_run_panel(stats, settings, connection)}</section>
  {_footer()}
</div>
<script id="guards">{_GUARDS}</script>
<script>{_UI}</script>"""
    return _shell(f"Lead desk — {_BRAND}", body)



# --- Cold-outreach status model --------------------------------------------
# One table maps the raw Status cell (what sheets.get_contacted_leads returns,
# lowercased) to three things: the filter BUCKET a chip toggles on, the LABEL
# the row shows, and the tag COLOUR. Two facts make the table load-bearing:
#
#   * retry_1_queued and retry_2_queued are two different cells but one idea —
#     "waiting to try again" — so they share the "retrying" bucket and a single
#     chip filters both. The label counts the attempt that FAILED, the same
#     number the sheet's own status uses: main.py stamps retry_N_queued after
#     attempt N fails, so retry_2_queued reads "2nd try failed" — matching the
#     sheet exactly, rather than counting forward to the next attempt (which
#     made the dashboard say 3 where the sheet said 2, and looked like a bug).
#   * "sent" is only ever written on a delivered/read confirmation (mark_sent),
#     while "awaiting_delivery" is the in-flight state before that lands. The
#     client reads both as "it went out", so they share the "sent" bucket, but
#     the in-flight one says "Sending…" so a row mid-flight is not called done.
#
# Order here is also the chip order below. Anything not in the table falls
# through to a neutral "other" bucket rather than showing a raw cell value.
COLD_STATES = {
    "awaiting_delivery": ("sent",        "Sending…",       "cool"),
    "sent":              ("sent",        "Sent",           "ok"),
    "delivered":         ("sent",        "Sent",           "ok"),
    "read":              ("sent",        "Sent",           "ok"),
    "replied":           ("replied",     "Replied",        "ok"),
    "escalated":         ("replied",     "Hot",            "hot"),
    "retry_1_queued":    ("retrying",    "1st try failed", "warn"),
    "retry_2_queued":    ("retrying",    "2nd try failed", "warn"),
    # Both dead-ends share the "stopped" bucket so one chip gives the client a
    # single "how many did we stop on" number — but each row keeps its own tag,
    # so you can still see WHY (couldn't reach vs they opted out) at a glance.
    "failed_permanent":  ("stopped",     "Couldn't reach", "hot"),
    "no_response":       ("no_response", "No reply",       "cool"),
    "cold":              ("stopped",     "Opted out",      "cool"),
}

# (bucket key, chip label). "all" and "sent" always show; the rest only appear
# once at least one lead is in them, so the card stays uncluttered on day one
# and grows chips as states actually occur.
_COLD_CHIPS = [
    ("sent",        "Sent"),
    ("replied",     "Replied"),
    ("retrying",    "Retrying"),
    ("stopped",     "Stopped"),
    ("no_response", "No reply"),
]


def _cold_meta(status):
    """(bucket, label, colour) for a raw Status cell."""
    return COLD_STATES.get(status, ("other", status.title() or "Unknown", "cool"))


# A plain-English "why" for each state that isn't self-explanatory — shown as a
# quiet line under the row so the client never has to ask why a number stopped
# or what a retry is waiting on. Delivered/Replied need no line; they explain
# themselves.
COLD_REASONS = {
    "failed_permanent": "Couldn’t be delivered after 3 tries — the number may not be on WhatsApp",
    "cold":             "They replied asking not to be contacted",
    "retry_1_queued":   "1st attempt didn’t deliver — trying again automatically after 24h",
    "retry_2_queued":   "2nd attempt didn’t deliver — one final try with a different message",
}


def _cold_row(rec):
    name = rec.get("name") or "there"
    number = rec.get("number") or ""
    status = (rec.get("status") or "").strip().lower()
    last = rec.get("last_contacted") or ""
    label = channel.short_label(number) if number else name
    bucket, tag, colour = _cold_meta(status)
    # data-cold-state is the BUCKET, not the raw status, so the "Stopped" chip
    # catches couldn't-reach and opted-out alike — the JS matches this exactly.
    find = f"{name} {number} {status} {tag}".lower()
    reason = COLD_REASONS.get(status, "")
    why = f'<span class="lead-why">{esc(reason)}</span>' if reason else ""
    return (
        f'<div class="lead" data-cold-lead data-cold-state="{esc(bucket)}"'
        f' data-cold-find="{esc(find)}">'
        f'<span class="lead-top"><span class="num">{esc(name)} &middot; {esc(label)}'
        f' <span class="tag {colour}">{esc(tag)}</span></span>{_when(last)}</span>'
        f'{why}</div>'
    )


def _cold_summary(cold_leads, counts, raw):
    """A plain-language scoreboard above the list, so a non-technical client can
    read the whole campaign in one glance without touching a chip.

    Every number here is also a chip below, but a chip only tells you a count
    once you think to click it; this states all of them at once, in the order a
    person actually asks — how many did we reach, how many landed, how many wrote
    back, how many are still being retried, how many we had to give up on. The
    retry cell carries its own breakdown line because "3 retrying" begs the
    question the client will ask next: which attempt are they on."""
    total = len(cold_leads)
    delivered = counts.get("sent", 0)
    replied = counts.get("replied", 0)
    retrying = counts.get("retrying", 0)
    # Read the couldn't-reach number straight off the raw status, not the bucket:
    # failed_permanent now shares the "stopped" bucket with opt-outs, so the
    # bucket count would blur the two. The scoreboard wants the pure number.
    failed = raw.get("failed_permanent", 0)

    # Which retry attempt each queued lead is on, straight off the raw cells.
    r1 = raw.get("retry_1_queued", 0)
    r2 = raw.get("retry_2_queued", 0)
    retry_note = ""
    if retrying:
        bits = []
        if r1:
            bits.append(f"{r1} after the 1st try")
        if r2:
            bits.append(f"{r2} after the 2nd try")
        if bits:
            retry_note = " &middot; ".join(bits)

    # (label, value, colour class, sub-note). Colour only where it earns it: green
    # for the good outcomes, amber while retries are in flight, red for gave-up.
    # No "no reply yet" cell — it read 0 next to a full Delivered count and just
    # confused people, because a delivered-but-silent lead is status "sent", not
    # a separate state. The chips below still isolate the odd genuine "No reply".
    cells = [
        ("Total reached", total, "", "we've messaged so far"),
        ("Delivered", delivered, "on" if delivered else "", "arrived on WhatsApp"),
        ("Replied", replied, "on" if replied else "", "they wrote back"),
        ("Still trying", retrying, "warn" if retrying else "", retry_note),
        ("Couldn't reach", failed, "off" if failed else "", "stopped after 3 tries"),
    ]
    def cell_html(k, v, cls, note):
        sub = f'<span class="sub">{note}</span>' if note else ""
        vcls = f' {cls}' if cls else ""
        return (f'<div><span class="k">{esc(k)}</span>'
                f'<span class="v{vcls}">{esc(v)}</span>{sub}</div>')
    body = "".join(cell_html(*c) for c in cells)
    # A plain-English legend under the scoreboard: what the three attempts are,
    # and where a number goes when all three miss. This is the whole retry policy
    # in two sentences, so a non-technical client never has to ask.
    legend = (
        '<p class="hint cold-legend">Every new lead gets a <b>1st message</b>. '
        'If it can’t be delivered, we retry on our own — a <b>2nd attempt after '
        '24 hours</b> with the same message, then a <b>3rd attempt</b> with a '
        'different message. If all three can’t be delivered, we stop trying and '
        'quietly move the number to <b>Stopped</b> — no alert, nothing for you '
        'to do.</p>'
    )
    return f'<div class="readout cold-summary">{body}</div>{legend}'


def _cold_sent(cold_leads):
    if not cold_leads:
        return ('<div class="card"><h2>Cold outreach sent</h2><div class="empty">'
                '<span class="mark"></span><b>Nothing sent yet</b>'
                'Leads from the Google Sheet show up here once the first cold '
                'message goes out.</div></div>')
    rows = "".join(_cold_row(r) for r in cold_leads)
    counts = {}
    raw = {}
    for r in cold_leads:
        st = (r.get("status") or "").strip().lower()
        raw[st] = raw.get(st, 0) + 1
        bucket = _cold_meta(st)[0]
        counts[bucket] = counts.get(bucket, 0) + 1
    # All and Sent are always offered; every other chip appears only if it has
    # rows behind it, so "Couldn't reach" is not a dead button on a clean list.
    shown = [("all", "All", len(cold_leads))]
    for key, label in _COLD_CHIPS:
        n = counts.get(key, 0)
        if key == "sent" or n:
            shown.append((key, label, n))
    chips = "".join(
        f'<button type="button" class="chip{" is-on" if key == "all" else ""}"'
        f' data-cold-filter="{key}">{esc(label)}{f" - {n}" if n else ""}</button>'
        for key, label, n in shown
    )
    return (
        f'<div class="card"><h2>Cold outreach sent <em>&middot; {len(cold_leads)}</em></h2>'
        f'{_cold_summary(cold_leads, counts, raw)}'
        '<div class="listtools">'
        '<input class="search" type="search" data-cold-search autocomplete="off"'
        ' placeholder="Search a name or number">'
        f'<div class="chips">{chips}</div></div>'
        f'{rows}<div class="noresult" data-cold-noresult>Nothing matches that.</div></div>'
    )

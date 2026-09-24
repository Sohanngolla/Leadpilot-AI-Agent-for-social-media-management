#!/usr/bin/env bash
# watchdog.sh — host-side uptime alarm for the agent.
#
# WHY IT LIVES ON THE HOST, NOT IN docker-compose: it has to be able to shout
# when the containers themselves are down, so it must not share their fate. Run
# it from the host's crontab (see the install steps handed over with this file).
#
# Docker's `restart: unless-stopped` already brings the app BACK after a crash
# or a VPS reboot. This script is the part that TELLS YOU — and it also catches
# a crash-loop, which the restart policy alone would hide forever.
#
# It checks the PUBLIC url, so a pass means the whole chain works:
# Cloudflare -> tunnel -> cloudflared -> app. Any broken link fails the check.
#
# It alerts ONCE when it goes down and ONCE when it recovers — never on every
# run — so a two-hour outage is two messages, not sixty.
#
# NOTE ON DELIVERY: the alert now goes out as the approved system-alert
# (SYSTEM_ALERT_TEMPLATE) template, which delivers regardless of WhatsApp's 24h customer-service window —
# the developer will not message the business number every day, and a plain-text
# alert to a closed window is silently dropped. The external monitor (email/push)
# is still worth keeping as a second, non-WhatsApp backstop.

cd "$(dirname "$0")" || exit 0

FAILS_NEEDED=3                      # misses in a row before shouting
DOWN_FLAG="/tmp/wa_watchdog.down"
COUNT_FILE="/tmp/wa_watchdog.count"

# Pull WhatsApp creds + the alert target from the same .env the app uses. These
# are only read to talk to the Graph API; nothing is ever printed.
set -a
[ -f .env ] && . ./.env 2>/dev/null
set +a
# The public health URL and the business name are read from .env so this script
# carries no deployment-specific values. Set HEALTH_URL and BRAND_NAME there.
HEALTH_URL="${HEALTH_URL:-https://dundermifflin.example/health}"
BRAND="${BRAND_NAME:-Dunder Mifflin}"
ALERT_TO="${DEVELOPER_NUMBER:-${OWNER_NUMBER:-}}"
VER="${GRAPH_API_VERSION:-v22.0}"
TEMPLATE="${SYSTEM_ALERT_TEMPLATE:-dundermifflin_system_alert}"
LANG_CODE="${TEMPLATE_LANGUAGE_CODE:-en}"

# Sends the approved system-alert template. $1 is the one-line message that lands
# in {{1}}; {{2}} is the timestamp. Template variables can't hold newlines, so $1
# must stay a single line (the two callers below already are).
send() {
  [ -n "${WHATSAPP_TOKEN:-}" ] && [ -n "${PHONE_NUMBER_ID:-}" ] && [ -n "$ALERT_TO" ] || return 0
  NOW="$(date '+%d %b %Y, %I:%M %p')"
  curl -s -o /dev/null --max-time 20 -X POST \
    "https://graph.facebook.com/${VER}/${PHONE_NUMBER_ID}/messages" \
    -H "Authorization: Bearer ${WHATSAPP_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "{\"messaging_product\":\"whatsapp\",\"to\":\"${ALERT_TO}\",\"type\":\"template\",\"template\":{\"name\":\"${TEMPLATE}\",\"language\":{\"code\":\"${LANG_CODE}\"},\"components\":[{\"type\":\"body\",\"parameters\":[{\"type\":\"text\",\"text\":\"$1\"},{\"type\":\"text\",\"text\":\"${NOW}\"}]}]}}"
}

if curl -fs --max-time 15 "$HEALTH_URL" >/dev/null 2>&1; then
  # Healthy. If we were previously down, announce recovery and reset.
  if [ -f "$DOWN_FLAG" ]; then
    send "✅ ${BRAND} agent is BACK UP and answering again."
    rm -f "$DOWN_FLAG"
  fi
  echo 0 > "$COUNT_FILE"
else
  n=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" > "$COUNT_FILE"
  if [ "$n" -ge "$FAILS_NEEDED" ] && [ ! -f "$DOWN_FLAG" ]; then
    send "🚨 ${BRAND} agent is DOWN. The health check has failed ${n} times in a row — replies may not be going out. It is set to auto-restart; you'll get an 'up' message when it recovers."
    touch "$DOWN_FLAG"
  fi
fi
exit 0

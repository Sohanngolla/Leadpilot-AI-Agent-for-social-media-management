#!/usr/bin/env bash
# backup-db.sh — nightly, live-safe, rotating backups of the agent SQLite DB.
#
# WHY THIS APPROACH: the snapshot is taken INSIDE the container using SQLite's
# online-backup API. That means (a) it's the same user that owns the db, so no
# host permission games, (b) it's safe to run WHILE the app is writing — no
# corruption risk the way a plain `cp` has, and (c) it handles WAL correctly.
# The host then just compresses the snapshot and rotates old copies.
#
# INSTALL: place in ~/whatsapp-lead-agent, `chmod +x backup-db.sh`, then add to
# the host crontab (see the handover notes). Keeps the newest $KEEP snapshots.

set -euo pipefail
cd "$(dirname "$0")"

DEST="backups"
KEEP=14                                  # keep the newest 14 snapshots (~2 weeks daily)
STAMP="$(date '+%Y%m%d-%H%M%S')"
mkdir -p "$DEST"

# 1) consistent online snapshot into the mounted data dir (safe while the app runs)
docker exec wa-agent python -c "import sqlite3; s=sqlite3.connect('/app/data/agent.db'); d=sqlite3.connect('/app/data/_snap.db'); s.backup(d); d.close(); s.close()"

# 2) compress the snapshot into backups/ (host only READS the snapshot)
gzip -c data/_snap.db > "$DEST/agent-$STAMP.db.gz"

# 3) rotate — keep only the newest $KEEP, delete the rest
ls -1t "$DEST"/agent-*.db.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f

echo "backup ok: $DEST/agent-$STAMP.db.gz  (kept $(ls -1 "$DEST"/agent-*.db.gz 2>/dev/null | wc -l))"

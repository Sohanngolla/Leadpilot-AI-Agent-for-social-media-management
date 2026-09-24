"""Manual cold-outreach / follow-up runner. The scheduler in main.py does this
automatically; this is for controlled one-off runs.

    python campaign.py --dry-run      # preview, sends nothing
    python campaign.py --limit 1      # first pending lead only
    python campaign.py                # all pending leads
    python campaign.py --followups    # run the follow-up pass instead
"""
import argparse
import logging

import jobs

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--followups", action="store_true")
    args = ap.parse_args()
    if args.followups:
        jobs.run_followups(dry_run=args.dry_run)
    else:
        jobs.run_cold_batch(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()

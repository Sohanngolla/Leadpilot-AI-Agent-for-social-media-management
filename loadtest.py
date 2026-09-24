#!/usr/bin/env python3
"""Local load / concurrency tester for the WhatsApp and Instagram relays.

Fires N synthetic webhook POSTs (each with a DISTINCT sender + message id) at the
relay, all at once, so you can watch the logs handle many "people" simultaneously.
It hits the SAME /webhook endpoint Meta calls, so it exercises the real dedup +
background-task + concurrency path — including the fast-ACK.

Examples:
  # 10 Instagram DMs fired at the same instant
  python3 loadtest.py --platform instagram --count 10

  # 10 WhatsApp messages at once
  python3 loadtest.py --platform whatsapp --count 10

  # Send the SAME id twice to prove dedup (the 2nd must be skipped, no AI call)
  python3 loadtest.py --platform instagram --count 2 --dup

Notes:
  * Each DISTINCT message triggers one real Gemini reply, so --count 10 is ~10 AI
    calls, and they drain at the `AI replies per minute` ceiling in the panel.
  * The outbound SEND will fail for these fake user ids (expected, and the reason
    this is safe to run against the live number: a free-form reply to someone who
    never messaged in has no open 24-hour window, so Meta rejects it before it can
    reach anybody). You are testing INBOUND handling + concurrency.
  * Watch two things in the relay terminal: (a) all inbounds are logged
    interleaved, none blocking the next; (b) duplicates are skipped.
  * Watch this script's output: all messages should ACK in ~the same few ms
    (fast-ACK), and total wall time ≈ one request, not N in a row.
"""
import argparse, json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor


def ig_payload(i, mid):
    return {"object": "instagram", "entry": [{"messaging": [
        {"sender": {"id": f"tester_{i}"}, "recipient": {"id": "BOT"},
         "message": {"mid": mid, "text": f"Load test message #{i}"}}]}]}


def wa_payload(i, mid):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": f"Tester {i}"}}],
        "messages": [{"from": f"91999900{i:04d}", "id": mid, "type": "text",
                      "text": {"body": f"Load test message #{i}"}}]}}]}]}


def post(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, round((time.time() - t0) * 1000)
    except Exception as e:
        return f"ERR {e}", round((time.time() - t0) * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["instagram", "whatsapp"], required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--dup", action="store_true", help="reuse one message id to test dedup")
    args = ap.parse_args()

    # The two channels are separate endpoints on the same app. Instagram used to
    # live on its own service at :8000/webhook; it was folded into the main agent
    # and now answers on /webhook/instagram, so posting an `object: instagram`
    # payload to /webhook just gets ignored by the WhatsApp parser.
    path = "/webhook/instagram" if args.platform == "instagram" else "/webhook"
    url = f"http://{args.host}:{args.port}{path}"
    mk = ig_payload if args.platform == "instagram" else wa_payload
    base = f"loadtest_{int(time.time())}"
    jobs = [mk(i, base if args.dup else f"{base}_{i}") for i in range(args.count)]

    print(f"Firing {args.count} '{args.platform}' webhooks at {url} (dup={args.dup})\n")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.count) as ex:
        results = list(ex.map(lambda p: post(url, p), jobs))
    total = round((time.time() - t0) * 1000)

    for i, (status, ms) in enumerate(results):
        print(f"  msg #{i:2d}: HTTP {status}  ({ms} ms to ACK)")
    acks = sum(1 for s, _ in results if s == 200)
    print(f"\n{acks}/{args.count} ACKed 200 in {total} ms total wall time.")
    print("If total ≈ a single request's time, concurrency + fast-ACK are working.")


if __name__ == "__main__":
    main()

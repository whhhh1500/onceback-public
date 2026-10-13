"""Send a correctly signed Airwallex-shaped webhook to a LOCAL console (no tunnel needed).

usage:
  AWX_WEBHOOK_SECRET=... python scripts/send_test_webhook.py payment_dispute.requires_response \
      --pi int_xxx --dispute dst_xxx [--url http://127.0.0.1:8000/webhooks/airwallex]
  python scripts/send_test_webhook.py refund.accepted --object-id rfd_xxx
Exercises the same verification path a real Airwallex delivery takes.
"""
import argparse
import json
import os
import sys
import time
import uuid

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from onceback.console.webhooks import sign  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("name")
ap.add_argument("--url", default="http://127.0.0.1:8000/webhooks/airwallex")
ap.add_argument("--pi", help="payment_intent_id (dispute events)")
ap.add_argument("--dispute", help="dispute id (dispute events)")
ap.add_argument("--object-id", default="")
ap.add_argument("--status", default="")
a = ap.parse_args()
secret = os.environ.get("AWX_WEBHOOK_SECRET")
if not secret:
    sys.exit("set AWX_WEBHOOK_SECRET (same value the console was started with)")
obj = {"id": a.dispute or a.object_id, "status": a.status or ("REQUIRES_RESPONSE" if a.dispute else "")}
if a.pi:
    obj["payment_intent_id"] = a.pi
event = {"id": f"evt_local_{uuid.uuid4().hex[:12]}", "name": a.name, "data": {"object": obj},
         "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())}
body = json.dumps(event).encode()
ts = str(int(time.time() * 1000))
r = requests.post(a.url, data=body, headers={"content-type": "application/json", "x-timestamp": ts,
                                              "x-signature": sign(secret, ts, body)}, timeout=10)
print(r.status_code, r.text)

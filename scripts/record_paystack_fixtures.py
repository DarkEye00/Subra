"""Record Paystack test-mode responses as test fixtures (Amendment 2).

    .venv/bin/python scripts/record_paystack_fixtures.py --phone <Paystack's test M-Pesa number>

Reads PAYSTACK_SECRET_KEY from the environment or from .env, and refuses anything but a test key
(sk_test_...), so it can never move real money. It starts one KES 10 M-Pesa charge, polls
/transaction/verify until the charge settles (2 minutes at most), and writes the raw JSON to
billing/tests/fixtures/paystack/. Webhooks can't reach a laptop, so webhook_charge_success.json is
built from the verify response: a charge.success event carries the same transaction object as "data".
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "billing" / "tests" / "fixtures" / "paystack"
API = "https://api.paystack.co"
UNSETTLED = {"pending", "ongoing", "processing", "queued", "pay_offline", "send_otp"}


def secret_key():
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    env_file = ROOT / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("PAYSTACK_SECRET_KEY="):
                key = line.split("=", 1)[1].strip().strip("\"'")
    if not key.startswith("sk_test_"):
        sys.exit("PAYSTACK_SECRET_KEY must be a Paystack TEST key (sk_test_...). Live keys are refused.")
    return key


def save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {(OUT / name).relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description="Record Paystack test-mode fixtures")
    parser.add_argument("--phone", required=True, help="exactly as Paystack should receive it")
    parser.add_argument("--email", default="fixtures@example.com")
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {secret_key()}"}
    reference = f"subra-fixture-{uuid.uuid4().hex[:8]}"
    body = {"email": args.email, "amount": 1000, "currency": "KES", "reference": reference,
            "mobile_money": {"phone": args.phone, "provider": "mpesa"}}
    resp = requests.post(f"{API}/charge", json=body, headers=headers, timeout=30)
    print("charge:", resp.status_code, resp.text[:300])
    save("charge_request.json", body)
    save("charge_response.json", {"http_status": resp.status_code, "body": resp.json()})
    if resp.status_code >= 400 or not resp.json().get("status"):
        sys.exit("Paystack refused the charge; see charge_response.json")

    deadline = time.time() + 120
    while True:
        verify = requests.get(f"{API}/transaction/verify/{reference}", headers=headers, timeout=30).json()
        status = (verify.get("data") or {}).get("status")
        print("verify:", status)
        if status not in UNSETTLED or time.time() > deadline:
            break
        time.sleep(5)
    save(f"verify_{status}.json", verify)
    if status == "success":
        save("webhook_charge_success.json", {"event": "charge.success", "data": verify["data"]})


if __name__ == "__main__":
    main()

"""Create the sample files in data/ for the invoice checker.

The story: a customer made 10,000 API requests in October 2026 but was
billed for 10,400. The extra 400 are requests the billing system recorded
twice on October 13 and 14.

Run:  python3 make_sample_data.py
"""
import csv
import json
import random
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

DATA = Path(__file__).parent / "data"

SEED = 2026
CUSTOMER_ID = "acme-freight"
CUSTOMER_NAME = "Acme Freight"
UNIQUE_REQUESTS = 10_000
RECORDED_TWICE = 400      # requests the billing system logged a second time
SAME_SECOND_PAIRS = 30    # real requests sent in the same second (not duplicates)

MONTH_START = datetime(2026, 10, 1)
MONTH_END = datetime(2026, 11, 1)
INCIDENT_START = datetime(2026, 10, 13)
INCIDENT_END = datetime(2026, 10, 15)

PRICING = {
    "customer_id": CUSTOMER_ID,
    "customer_name": CUSTOMER_NAME,
    "billing_month": "October 2026",
    "currency": "USD",
    "platform_fee": "200.00",
    "included_requests": 1000,
    "price_per_extra_request": "0.25",
}


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    rng = random.Random(SEED)
    month_seconds = int((MONTH_END - MONTH_START).total_seconds())
    used_ids = set()
    used_slots = set()   # (endpoint, time) pairs, so only the planned pairs share a second

    def new_event_id():
        while True:
            event_id = f"evt_{rng.getrandbits(48):012x}"
            if event_id not in used_ids:
                used_ids.add(event_id)
                return event_id

    def new_event(endpoint, requested):
        recorded = requested + timedelta(seconds=rng.randint(0, 2))
        return {
            "event_id": new_event_id(),
            "customer_id": CUSTOMER_ID,
            "endpoint": endpoint,
            "requested_at": requested,
            "recorded_at": recorded,
        }

    # 1. Normal requests, spread across the month.
    events = []
    while len(events) < UNIQUE_REQUESTS - SAME_SECOND_PAIRS:
        endpoint = "/v1/parse" if rng.random() < 0.8 else "/v1/classify"
        requested = MONTH_START + timedelta(seconds=rng.randrange(month_seconds))
        if (endpoint, requested) in used_slots:
            continue
        used_slots.add((endpoint, requested))
        events.append(new_event(endpoint, requested))

    # 2. A few real requests that happen in the same second as another one.
    #    Each gets its own event_id, so these are separate, billable requests.
    for original in rng.sample(events, SAME_SECOND_PAIRS):
        events.append(new_event(original["endpoint"], original["requested_at"]))

    # 3. The billing bug: during the incident, some requests get recorded twice.
    #    The second record keeps the same event_id and is logged a bit later.
    in_incident = [e for e in events if INCIDENT_START <= e["requested_at"] < INCIDENT_END]
    assert len(in_incident) >= RECORDED_TWICE, "not enough requests in the incident window"
    rows = list(events)
    for original in rng.sample(in_incident, RECORDED_TWICE):
        second = dict(original)
        second["recorded_at"] = original["requested_at"] + timedelta(seconds=rng.randint(30, 120))
        rows.append(second)

    rows.sort(key=lambda r: (r["recorded_at"], r["event_id"]))

    DATA.mkdir(exist_ok=True)
    with open(DATA / "usage.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["event_id", "customer_id", "endpoint", "requested_at", "recorded_at"])
        for r in rows:
            writer.writerow([r["event_id"], r["customer_id"], r["endpoint"],
                             stamp(r["requested_at"]), stamp(r["recorded_at"])])

    with open(DATA / "pricing.json", "w") as f:
        json.dump(PRICING, f, indent=2)
        f.write("\n")

    # 4. The invoice the customer received. It counts every row in the log.
    fee = Decimal(PRICING["platform_fee"])
    price = Decimal(PRICING["price_per_extra_request"])
    included = PRICING["included_requests"]
    extra = len(rows) - included
    with open(DATA / "invoice.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["code", "description", "quantity", "unit_price", "amount"])
        writer.writerow(["platform_fee", f"Platform fee, includes {included:,} requests", 1, fee, fee])
        writer.writerow(["extra_requests", "Additional requests", extra, price, extra * price])

    print(f"Wrote {len(rows):,} usage records ({len(events):,} unique requests) to {DATA}/")


if __name__ == "__main__":
    main()

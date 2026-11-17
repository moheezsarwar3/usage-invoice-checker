"""Check a usage-based invoice against the raw usage log.

The question it answers:
    "We made 10,000 requests last month. Why were we billed for 10,400?"

Run:   python3 checker.py
Then open report.html in a browser.
"""
import argparse
import csv
import html
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).parent

# ---------------------------------------------------------------- SQL

CREATE_TABLE = """
CREATE TABLE usage (
    event_id     TEXT NOT NULL,  -- ID the customer's system gives each request
    customer_id  TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    requested_at TEXT NOT NULL,  -- when the customer sent the request
    recorded_at  TEXT NOT NULL   -- when the billing system logged it
)
"""

# One request should appear once. If the same event_id appears more than once,
# the billing system recorded one request several times.
FIND_DUPLICATES = """
SELECT event_id,
       MIN(endpoint)     AS endpoint,
       MIN(requested_at) AS requested_at,
       COUNT(*)          AS times_recorded,
       MIN(recorded_at)  AS first_recorded,
       MAX(recorded_at)  AS last_recorded
FROM usage
GROUP BY event_id
HAVING COUNT(*) > 1
ORDER BY requested_at, event_id
"""

# Two different requests can be sent in the same second. They have different
# event_ids, so they are separate requests and both should be billed.
FIND_SAME_SECOND_REQUESTS = """
SELECT endpoint, requested_at, COUNT(DISTINCT event_id) AS requests
FROM usage
GROUP BY customer_id, endpoint, requested_at
HAVING COUNT(DISTINCT event_id) > 1
ORDER BY requested_at
"""

# How many extra records each day produced. Shows whether the problem is
# spread across the month or tied to particular days.
EXTRA_RECORDS_BY_DAY = """
SELECT day, SUM(times_recorded - 1) AS extra_records
FROM (
    SELECT substr(MIN(requested_at), 1, 10) AS day, COUNT(*) AS times_recorded
    FROM usage
    GROUP BY event_id
    HAVING COUNT(*) > 1
)
GROUP BY day
ORDER BY day
"""

# ---------------------------------------------------------------- loading


def load_pricing(path):
    with open(path) as f:
        pricing = json.load(f)
    # Decimal, not float, so money adds up exactly.
    pricing["platform_fee"] = Decimal(pricing["platform_fee"])
    pricing["price_per_extra_request"] = Decimal(pricing["price_per_extra_request"])
    return pricing


def load_invoice(path, pricing):
    total = Decimal("0")
    extra_requests = 0
    with open(path, newline="") as f:
        for line in csv.DictReader(f):
            total += Decimal(line["amount"])
            if line["code"] == "extra_requests":
                extra_requests = int(line["quantity"])
    return {"total": total, "requests": pricing["included_requests"] + extra_requests}


def load_usage(conn, path):
    conn.execute(CREATE_TABLE)
    with open(path, newline="") as f:
        rows = [(r["event_id"], r["customer_id"], r["endpoint"], r["requested_at"], r["recorded_at"])
                for r in csv.DictReader(f)]
    conn.executemany("INSERT INTO usage VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()


# ---------------------------------------------------------------- checking


def expected_charge(requests, pricing):
    """What the pricing terms say this many requests should cost."""
    extra = max(0, requests - pricing["included_requests"])
    return pricing["platform_fee"] + extra * pricing["price_per_extra_request"]


def seconds_between(first, last):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return int((datetime.strptime(last, fmt) - datetime.strptime(first, fmt)).total_seconds())


def check(conn, pricing, invoice):
    conn.row_factory = sqlite3.Row
    records = conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    unique_requests = conn.execute("SELECT COUNT(DISTINCT event_id) FROM usage").fetchone()[0]
    duplicates = [dict(row) for row in conn.execute(FIND_DUPLICATES)]
    same_second = [dict(row) for row in conn.execute(FIND_SAME_SECOND_REQUESTS)]
    by_day = [dict(row) for row in conn.execute(EXTRA_RECORDS_BY_DAY)]

    for d in duplicates:
        d["gap_seconds"] = seconds_between(d["first_recorded"], d["last_recorded"])

    extra_records = records - unique_requests
    expected_total = expected_charge(unique_requests, pricing)
    overbilled_requests = invoice["requests"] - unique_requests

    return {
        "pricing": pricing,
        "records": records,
        "unique_requests": unique_requests,
        "extra_records": extra_records,
        "duplicates": duplicates,
        "same_second": same_second,
        "by_day": by_day,
        "invoice_requests": invoice["requests"],
        "invoice_total": invoice["total"],
        "expected_total": expected_total,
        "difference": invoice["total"] - expected_total,
        "overbilled_requests": overbilled_requests,
        # True when the extra records account for every overbilled request.
        "fully_explained": overbilled_requests == extra_records,
    }


# ---------------------------------------------------------------- output


def money(amount):
    return f"${amount:,.2f}"


def print_summary(r):
    print(f"Invoice check: {r['pricing']['customer_name']}, {r['pricing']['billing_month']}")
    lines = [
        ("Invoiced", r["invoice_requests"], r["invoice_total"]),
        ("Usage log, counted once", r["unique_requests"], r["expected_total"]),
        ("Difference", r["overbilled_requests"], r["difference"]),
    ]
    for label, requests, amount in lines:
        print(f"  {label + ':':<25}{requests:>7,} requests   {money(amount):>10}")
    print(f"  Requests recorded more than once: {len(r['duplicates']):,} ({r['extra_records']:,} extra records)")
    for day in r["by_day"]:
        print(f"    {day['day']}: {day['extra_records']:,} extra records")
    print(f"  Same-second requests with different IDs (billed correctly): {len(r['same_second']):,} cases")
    if r["fully_explained"]:
        print("  Result: the extra records explain the full difference.")
    else:
        left = r["overbilled_requests"] - r["extra_records"]
        print(f"  Result: {left:,} requests of the difference are still unexplained.")


def write_report(r, path):
    e = html.escape
    p = r["pricing"]
    dup = r["duplicates"]

    if r["difference"] == 0:
        verdict = "The invoice matches the usage log. No difference to explain."
    elif r["fully_explained"]:
        verdict = (f"{len(dup):,} requests were recorded more than once. Each has a single event ID "
                   f"but appears in the usage log more than one time. The invoice counted every "
                   f"record, so those requests were billed more than once. This explains the "
                   f"full difference of {money(r['difference'])}.")
    else:
        left = r["overbilled_requests"] - r["extra_records"]
        verdict = (f"Repeated records explain {r['extra_records']:,} of the "
                   f"{r['overbilled_requests']:,} extra requests. {left:,} are still unexplained "
                   f"and need a closer look.")

    timing = ""
    if dup:
        gaps = [d["gap_seconds"] for d in dup]
        timing = (f"In every case the repeat was logged between {min(gaps)} and {max(gaps)} seconds "
                  f"after the first record, with the same event ID and the same request time. "
                  f"That pattern is what a retry being counted as new usage looks like.")

    day_rows = "".join(f"<tr><td>{e(d['day'])}</td><td class='num'>{d['extra_records']:,}</td></tr>"
                       for d in r["by_day"])
    dup_rows = "".join(
        f"<tr><td><code>{e(d['event_id'])}</code></td><td>{e(d['endpoint'])}</td>"
        f"<td>{e(d['requested_at'])}</td><td>{e(d['first_recorded'])}</td>"
        f"<td>{e(d['last_recorded'])}</td><td class='num'>{d['times_recorded']}</td></tr>"
        for d in dup)
    same_rows = "".join(
        f"<tr><td>{e(s['requested_at'])}</td><td>{e(s['endpoint'])}</td>"
        f"<td class='num'>{s['requests']}</td></tr>" for s in r["same_second"][:10])

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Invoice check: {e(p['customer_name'])}, {e(p['billing_month'])}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1f2933;
         max-width: 860px; margin: 40px auto; padding: 0 20px; line-height: 1.5; }}
  h1 {{ font-size: 26px; margin-bottom: 4px; }}
  h2 {{ font-size: 18px; margin-top: 36px; }}
  .question {{ color: #52606d; font-size: 17px; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #e4e7eb; }}
  th {{ background: #f5f7fa; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .summary td, .summary th {{ font-size: 16px; }}
  .diff td {{ font-weight: 600; background: #fff8e6; }}
  .verdict {{ background: #f0f7f4; border-left: 4px solid #2f855a; padding: 12px 16px; }}
  code {{ font-size: 13px; }}
  .scroll {{ max-height: 420px; overflow-y: auto; border: 1px solid #e4e7eb; }}
  .scroll table {{ margin: 0; }}
  .scroll th {{ position: sticky; top: 0; }}
  .small {{ color: #52606d; font-size: 13px; }}
</style>
</head>
<body>
<h1>Invoice check: {e(p['customer_name'])}, {e(p['billing_month'])}</h1>
<p class="question">"We made {r['unique_requests']:,} requests last month. Why were we billed for {r['invoice_requests']:,}?"</p>

<table class="summary">
  <tr><th></th><th class="num">Requests</th><th class="num">Amount</th></tr>
  <tr><td>Invoice</td><td class="num">{r['invoice_requests']:,}</td><td class="num">{money(r['invoice_total'])}</td></tr>
  <tr><td>Usage log, each request counted once</td><td class="num">{r['unique_requests']:,}</td><td class="num">{money(r['expected_total'])}</td></tr>
  <tr class="diff"><td>Difference</td><td class="num">{r['overbilled_requests']:,}</td><td class="num">{money(r['difference'])}</td></tr>
</table>
<p class="small">Pricing terms: {money(p['platform_fee'])} platform fee with {p['included_requests']:,} requests included,
then {money(p['price_per_extra_request'])} for each additional request.</p>

<h2>What explains the difference</h2>
<p class="verdict">{e(verdict)}</p>
<p>{e(timing)}</p>

<h2>When it happened</h2>
<table>
  <tr><th>Day</th><th class="num">Extra records</th></tr>
  {day_rows}
</table>

<h2>Requests that look similar but are not duplicates</h2>
<p>{len(r['same_second']):,} times, two requests reached the same endpoint in the same second.
They have different event IDs, so they are separate requests and were billed correctly.
The check does not flag them.</p>
<table>
  <tr><th>Requested at</th><th>Endpoint</th><th class="num">Separate requests</th></tr>
  {same_rows}
</table>
<p class="small">Showing the first {min(10, len(r['same_second']))} of {len(r['same_second']):,}.</p>

<h2>The {len(dup):,} requests recorded more than once</h2>
<div class="scroll">
<table>
  <tr><th>Event ID</th><th>Endpoint</th><th>Requested at</th><th>First recorded</th><th>Recorded again</th><th class="num">Times</th></tr>
  {dup_rows}
</table>
</div>

<h2>Suggested next step</h2>
<p>Credit {e(p['customer_name'])} {money(r['difference'])} for {e(p['billing_month'])}, and ask the billing team
to check why repeat records on the days above were counted as new usage.</p>
</body>
</html>
"""
    Path(path).write_text(page)


def main():
    parser = argparse.ArgumentParser(description="Check a usage-based invoice against the usage log.")
    parser.add_argument("--usage", default=HERE / "data" / "usage.csv")
    parser.add_argument("--pricing", default=HERE / "data" / "pricing.json")
    parser.add_argument("--invoice", default=HERE / "data" / "invoice.csv")
    parser.add_argument("--report", default=HERE / "report.html")
    args = parser.parse_args()

    pricing = load_pricing(args.pricing)
    invoice = load_invoice(args.invoice, pricing)
    conn = sqlite3.connect(":memory:")
    load_usage(conn, args.usage)

    result = check(conn, pricing, invoice)
    print_summary(result)
    write_report(result, args.report)
    print(f"\nFull report: {args.report}")


if __name__ == "__main__":
    main()

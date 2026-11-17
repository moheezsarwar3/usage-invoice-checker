"""Tests for the invoice checker.

Run:  python3 -m unittest -v
"""
import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path

import checker

DATA = Path(__file__).parent / "data"

PRICING = {
    "customer_name": "Test Co",
    "billing_month": "October 2026",
    "platform_fee": Decimal("200.00"),
    "included_requests": 1000,
    "price_per_extra_request": Decimal("0.25"),
}


def usage_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(checker.CREATE_TABLE)
    conn.executemany("INSERT INTO usage VALUES (?, ?, ?, ?, ?)", rows)
    return conn


class ExpectedCharge(unittest.TestCase):
    def test_under_the_included_amount_costs_only_the_fee(self):
        self.assertEqual(checker.expected_charge(800, PRICING), Decimal("200.00"))

    def test_extra_requests_are_charged_per_request(self):
        self.assertEqual(checker.expected_charge(10_000, PRICING), Decimal("2450.00"))


class FindingDuplicates(unittest.TestCase):
    def test_same_event_id_twice_is_a_duplicate(self):
        conn = usage_db([
            ("evt_a", "c1", "/v1/parse", "2026-10-13T10:00:00Z", "2026-10-13T10:00:01Z"),
            ("evt_a", "c1", "/v1/parse", "2026-10-13T10:00:00Z", "2026-10-13T10:01:05Z"),
            ("evt_b", "c1", "/v1/parse", "2026-10-13T11:00:00Z", "2026-10-13T11:00:00Z"),
        ])
        invoice = {"total": Decimal("200.00"), "requests": 3}
        result = checker.check(conn, PRICING, invoice)
        self.assertEqual(result["unique_requests"], 2)
        self.assertEqual([d["event_id"] for d in result["duplicates"]], ["evt_a"])
        self.assertEqual(result["duplicates"][0]["gap_seconds"], 64)

    def test_two_requests_in_the_same_second_are_not_duplicates(self):
        conn = usage_db([
            ("evt_a", "c1", "/v1/parse", "2026-10-13T10:00:00Z", "2026-10-13T10:00:01Z"),
            ("evt_b", "c1", "/v1/parse", "2026-10-13T10:00:00Z", "2026-10-13T10:00:01Z"),
        ])
        invoice = {"total": Decimal("200.00"), "requests": 2}
        result = checker.check(conn, PRICING, invoice)
        self.assertEqual(result["duplicates"], [])
        self.assertEqual(result["unique_requests"], 2)
        self.assertEqual(len(result["same_second"]), 1)

    def test_a_gap_that_duplicates_do_not_cover_is_reported_as_unexplained(self):
        conn = usage_db([
            ("evt_a", "c1", "/v1/parse", "2026-10-13T10:00:00Z", "2026-10-13T10:00:01Z"),
        ])
        invoice = {"total": Decimal("200.00"), "requests": 5}
        result = checker.check(conn, PRICING, invoice)
        self.assertFalse(result["fully_explained"])


class SampleData(unittest.TestCase):
    def test_sample_invoice_is_overbilled_by_400_requests(self):
        pricing = checker.load_pricing(DATA / "pricing.json")
        invoice = checker.load_invoice(DATA / "invoice.csv", pricing)
        conn = sqlite3.connect(":memory:")
        checker.load_usage(conn, DATA / "usage.csv")
        result = checker.check(conn, pricing, invoice)

        self.assertEqual(result["records"], 10_400)
        self.assertEqual(result["unique_requests"], 10_000)
        self.assertEqual(len(result["duplicates"]), 400)
        self.assertEqual(result["invoice_total"], Decimal("2550.00"))
        self.assertEqual(result["expected_total"], Decimal("2450.00"))
        self.assertEqual(result["difference"], Decimal("100.00"))
        self.assertEqual(len(result["same_second"]), 30)
        self.assertTrue(result["fully_explained"])


if __name__ == "__main__":
    unittest.main()

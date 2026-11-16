"""离线验收：真实采集/数据库/导出代码，仅替换 HTTP 传输。"""
import csv
import json
import unittest
from unittest.mock import patch

import test_movie_history as fixtures
from test_movie_history import CODES, DAY, MOVIE, box
from zgdypf import db, national_history as national, history_roster
from zgdypf.cinema_detail import collect


class NationalHistoryTests(unittest.TestCase):
    setUp = fixtures.MovieHistoryTests.setUp
    fake_open = fixtures.MovieHistoryTests.fake_open
    client = fixtures.MovieHistoryTests.client

    def plan(self, **options):
        return national.make_plan(self.path, MOVIE, DAY, DAY, **options)

    def run_batch(self, plan=None, **options):
        return national.run_plan(plan or self.plan(), self.root / "raw", self.root / "out",
                                 client_factory=self.client, emit=lambda _: None, **options)

    def seed(self, codes=CODES[:2]):
        with self.conn:
            history_roster._merge(self.conn, DAY, dict.fromkeys(codes), "csv:test")
            history_roster._save_refresh(self.conn, DAY, "test", "thin", len(codes), 1, len(codes))

    def test_preview_has_no_network_or_database_writes(self):
        missing = self.root / "missing.db"
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")):
            first = national.make_plan(missing, MOVIE, DAY, DAY)
            second = national.make_plan(missing, MOVIE, DAY, DAY)
        self.assertFalse(missing.exists())
        self.assertEqual(first, second)
        self.assertIsNone(first["first_attempt_requests_exact"])
        self.assertEqual(first["first_attempt_requests_min"], 1)
        self.assertFalse(first["national_complete"])

    def test_changed_input_or_database_invalidates_review(self):
        plan = self.plan()
        plan["movie"] = "123"
        with self.assertRaisesRegex(ValueError, "计划"):
            self.run_batch(plan)
        plan = self.plan()
        self.seed()
        with self.assertRaisesRegex(ValueError, "计划"):
            self.run_batch(plan)
        self.assertFalse(self.calls)

    def test_batches_resume_and_cross_movie_cache(self):
        self.routes = [(200, {"salesRankList": [{"code": c} for c in CODES[:2]], "hasMore": False}),
                       (200, box()), (200, box("123"))]
        first = self.run_batch(self.plan(max_requests=2))
        self.assertEqual((first["requests"], first["saved"]), (2, 1))
        self.assertEqual(self.plan()["totals"]["pending"], 1)
        second = self.run_batch()
        self.assertEqual((second["requests"], second["saved"]), (1, 1))
        other = national.make_plan(self.path, "123", DAY, DAY)
        self.assertEqual(other["totals"]["cached"], 2)
        self.assertEqual(self.run_batch(other)["requests"], 0)
        self.assertFalse(second["reports"]["summary"]["national_complete"])

    def test_all_known_cinemas_across_dates_included_without_sampling(self):
        self.seed(CODES[:1])
        with self.conn:
            history_roster._merge(self.conn, "2026-05-01", {CODES[1]: "other day"}, "csv:other")
            self.conn.executemany("INSERT INTO dim_cinema(cinema_code,sample) VALUES (?,0)",
                                  [(str(40000000 + n),) for n in range(12001)])
        p = self.plan()
        self.assertEqual(p["totals"]["targets"], 12003)
        self.assertEqual(p["first_attempt_requests_exact"], 12003)
        self.assertFalse(p["national_complete"])

    def test_statuses_and_money_do_not_conflate_unknown_with_zero(self):
        self.seed()
        self.routes = [(200, box("123")), (204, None)]
        result = self.run_batch()
        with result["reports"]["paths"]["matrix"].open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual((rows[0]["status"], rows[0]["sales_fen"]), ("without_movie", "0"))
        self.assertEqual((rows[1]["status"], rows[1]["sales_fen"]), ("deferred_204", ""))
        self.assertFalse(result["reports"]["summary"]["days"][0]["known_roster_observed"])
        self.assertEqual(self.run_batch()["requests"], 0)
        self.routes = [(200, box())]
        retry = self.run_batch(self.plan(retry_empty=True))
        self.assertEqual(retry["requests"], 1)

    def test_errors_missing_and_inconsistent_are_blank_in_export(self):
        self.seed(CODES[:3])
        self.routes = [(200, box())]
        collect(self.conn, self.client(), CODES[0], DAY)
        with self.conn:
            self.conn.execute("UPDATE fact_cinema_movie SET movie_count=9")
            db.ledger_put(self.conn, "cinema_box", f"{CODES[1]}|{DAY}", "error", err="bad")
        result = national.export_audit(self.path, self.root, MOVIE, DAY, DAY)
        with result["paths"]["matrix"].open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual({r["status"] for r in rows}, {"inconsistent", "error", "missing"})
        self.assertTrue(all(r["sales_fen"] == "" for r in rows))
        self.assertTrue(all(r["cinema_code"] in CODES for r in rows))

    def test_cancel_before_start_still_exports_no_requests(self):
        result = self.run_batch(stopped=lambda: True)
        self.assertEqual(result["requests"], 0)
        self.assertIn("停止", result["reason"])
        self.assertTrue(result["reports"]["paths"]["audit"].exists())

    def test_pause_after_response_persists_and_resumes(self):
        self.seed()
        self.routes = [(200, box()), (200, box())]
        result = self.run_batch(stopped=lambda: len(self.calls) >= 1)
        self.assertEqual((result["requests"], result["saved"]), (1, 1))
        self.assertEqual(self.run_batch()["requests"], 1)
        self.assertNotEqual(self.calls[0], self.calls[1])

    def test_budget_includes_retry_and_roster_pagination(self):
        self.routes = [(503, {}), (200, {"salesRankList": [{"code": CODES[0]}], "hasMore": True})]
        result = self.run_batch(self.plan(max_requests=2))
        self.assertEqual((result["requests"], result["retries"]), (2, 1))
        self.routes = [(200, {"salesRankList": [{"code": CODES[1]}], "hasMore": False})]
        self.run_batch(self.plan(max_requests=1))
        self.assertIn("offset=1", self.calls[-1])
        self.assertEqual(self.plan()["totals"]["targets"], 2)

    def test_access_denial_does_not_continue_or_claim_complete(self):
        self.routes = [(429, {"Retry-After": "60"})]
        result = self.run_batch()
        self.assertEqual(result["requests"], 1)
        self.assertTrue(result["failed"])
        self.assertFalse(result["national_complete"])

    def test_detail_access_denial_is_error_not_missing(self):
        self.seed(CODES[:1])
        self.routes = [(403, {})]
        result = self.run_batch()
        self.assertEqual(result["reports"]["summary"]["states"], {"error": 1})
        self.assertEqual(result["requests"], 1)

    def test_invalid_inputs_fail_before_network(self):
        for options in (dict(rate=2), dict(rate=float("nan")), dict(minutes=0), dict(max_requests=0)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.plan(**options)
        self.assertFalse(self.calls)

    def test_plan_and_reports_serializable_and_reviewable(self):
        self.seed()
        p = self.plan()
        saved = national.save_plan(p, self.root)
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), p)
        self.assertIn("全国完整性未证实", national.format_plan(p))
        self.assertEqual(p["first_attempt_requests_exact"], 2)


if __name__ == "__main__":
    unittest.main()

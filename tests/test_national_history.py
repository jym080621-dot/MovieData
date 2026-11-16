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

    def test_empty_cache_estimates_full_range_and_scales_with_rate(self):
        estimates = [national.make_plan(self.root / "missing.db", MOVIE, DAY, "2026-05-01", rate=rate)
                     for rate in (1, 2, 3, 5)]
        times = []
        for plan in estimates:
            estimate = plan["collection_estimate"]
            self.assertEqual(estimate["full_requests"], 26_002)
            self.assertEqual(estimate["remaining_requests"], 26_002)
            self.assertEqual(plan["first_attempt_requests_min"], 2)
            self.assertFalse(plan["national_complete"])
            times.append(estimate["remaining_seconds"][0])
        self.assertTrue(all(a > b for a, b in zip(times, times[1:])))

    def test_estimate_uses_current_queue_and_rechecks_only_when_selected(self):
        self.seed()
        self.routes = [(200, box()), (204, None)]
        self.run_batch()
        normal = self.plan()["collection_estimate"]
        retried = self.plan(retry_empty=True)["collection_estimate"]
        self.assertEqual(normal["full_requests"], 3)  # 2 cinema days and 1 roster page
        self.assertEqual(normal["remaining_requests"], 0)
        self.assertEqual(normal["remaining_seconds"], [0, 0])
        self.assertEqual(retried["remaining_requests"], 1)
        self.assertIsNone(normal["reference_cinemas_per_day"])

    def test_estimate_counts_only_remaining_roster_pages_after_resume(self):
        self.routes = [(200, {"salesRankList": [{"code": CODES[0]}], "hasMore": True})]
        self.run_batch(self.plan(max_requests=1))
        estimate = self.plan()["collection_estimate"]
        self.assertEqual(estimate["roster_requests_full"], 2)
        self.assertEqual(estimate["roster_requests_remaining"], 1)
        self.assertEqual(estimate["remaining_requests"], 13_001)

    def test_empty_finished_roster_keeps_national_projection_but_has_no_queue(self):
        self.seed(())
        plan = self.plan()
        self.assertEqual(plan["collection_estimate"]["full_requests"], 13_001)
        self.assertEqual(plan["collection_estimate"]["remaining_requests"], 0)
        self.assertIn("当前采集队列为空", national.format_plan(plan))

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

    def test_coverage_callback_includes_saved_work_and_new_roster(self):
        self.routes = [(200, {"salesRankList": [{"code": c} for c in CODES[:2]], "hasMore": False}),
                       (200, box())]
        events = []
        self.run_batch(self.plan(max_requests=2), on_coverage=events.append)
        self.assertEqual((events[0]["targets"], events[0]["cached"]), (0, 0))
        self.assertTrue(any(e["targets"] == 2 and e["cached"] == 0 for e in events))
        self.assertEqual((events[-1]["targets"], events[-1]["cached"]), (2, 1))
        events.clear()
        self.routes = [(204, None)]
        self.run_batch(on_coverage=events.append)
        self.assertEqual(events[0]["cached"], 1)
        self.assertEqual(events[-1]["cached"], 1, "204 must not inflate verified coverage")
        self.assertEqual(events[-1]["deferred_204"], 1)

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

    def test_unlimited_run_continues_past_empty_streak_and_saves_audit(self):
        self.seed(CODES[:4])
        self.routes = [(204, None)] * 3 + [(200, box())]
        plan = self.plan(max_requests=0, minutes=0)
        result = self.run_batch(plan)
        self.assertEqual((result["requests"], result["empty"], result["saved"]), (4, 3, 1))
        self.assertFalse(result["national_complete"])
        self.assertIn("不限", national.format_plan(plan))

    def test_pause_during_retry_wait_leaves_delay_interruptible(self):
        self.seed(CODES[:1])
        self.routes = [(503, {})]
        wait_slices = []

        def client_with_retry(**kwargs):
            client = self.client(**kwargs)
            client.backoff = (10,)
            return client

        with patch("zgdypf.client.time.sleep", side_effect=wait_slices.append):
            result = national.run_plan(self.plan(max_requests=0, minutes=0), self.root / "raw", self.root / "out",
                                       stopped=lambda: bool(wait_slices), client_factory=client_with_retry,
                                       emit=lambda _: None)
        self.assertEqual(wait_slices, [0.1])
        self.assertTrue(result["paused"])
        self.assertEqual((result["requests"], result["retries"]), (1, 0))
        self.assertEqual(self.plan()["totals"]["pending"], 1)

    def test_access_denial_does_not_continue_or_claim_complete(self):
        self.routes = [(429, {"Retry-After": "60"})]
        settings = []

        def fast_client(**kwargs):
            settings.append(kwargs["rate"])
            return self.client(**kwargs)

        result = national.run_plan(self.plan(rate=5), self.root / "raw", self.root / "out",
                                   client_factory=fast_client, emit=lambda _: None)
        self.assertEqual(settings, [5])
        self.assertEqual(result["requests"], 1)
        self.assertIn("Retry-After=60", result["reason"])
        self.assertTrue(result["failed"])
        self.assertFalse(result["national_complete"])

    def test_detail_access_denial_is_error_not_missing(self):
        self.seed(CODES[:1])
        self.routes = [(403, {})]
        result = self.run_batch()
        self.assertEqual(result["reports"]["summary"]["states"], {"error": 1})
        self.assertEqual(result["requests"], 1)

    def test_invalid_inputs_fail_before_network(self):
        for options in (dict(rate=5.1), dict(rate=0), dict(rate=float("nan")), dict(minutes=-1), dict(max_requests=-1)):
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

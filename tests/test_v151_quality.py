"""New v1.5.1 offline regression tests; do not contact production API."""
import csv
import tempfile
import unittest
from pathlib import Path

from zgdypf import national_history, national_quality, history_roster, movie_history
import test_movie_history as fixtures


class QualityStateTests(unittest.TestCase):
    def test_204_is_unresolved_not_zero(self):
        result = national_quality.assess_day({"with_movie": 1, "without_movie": 1, "deferred_204": 1}, "listed", 3)
        self.assertEqual((result["known_verified"], result["known_unresolved"], result["retry_204"]), (2, 1, 1))
        self.assertFalse(result["known_scope_complete"])
        self.assertFalse(result["national_complete"])

    def test_no_candidates_cannot_be_called_complete(self):
        result = national_quality.assess_day({}, "listed", 0)
        self.assertTrue(result["roster_problem"])
        self.assertFalse(result["known_scope_complete"])

    def test_thin_rank_keeps_national_unverified(self):
        day = national_quality.assess_day({"without_movie": 4}, "thin", 4)
        total = national_quality.summarize_days([day])
        self.assertEqual(total["roster_problem_days"], 1)
        self.assertTrue(total["all_known_scope_verified"])
        self.assertFalse(total["national_complete"])

    def test_inconsistent_state_totals_fail_closed(self):
        with self.assertRaises(ValueError):
            national_quality.assess_day({"with_movie": 2}, "listed", 3)


class BatchExportTests(unittest.TestCase):
    def test_summary_only_does_not_write_matrix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # No database is created by the preview/export path.
            result = national_history.export_audit(root / "missing.db", root / "out", "123", "2026-04-30", "2026-04-30", write_matrix=False)
            self.assertNotIn("matrix", result["paths"])
            self.assertFalse(list((root / "out").glob("*_cinema_days.csv")))
            self.assertTrue(result["paths"]["audit"].is_file())
            self.assertFalse(result["summary"]["national_complete"])
            self.assertTrue(result["summary"]["followup"]["roster_problem_days"])
            self.assertTrue(result["summary"]["followup_message"])

    def test_batch_can_finish_without_full_matrix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            movie = "123"
            day = "2026-04-30"
            plan = national_history.make_plan(root / "boxoffice.db", movie, day, day)
            result = national_history.run_plan(plan, root / "raw", root / "out",
                                                stopped=lambda: True, full_matrix=False)
            self.assertNotIn("matrix", result["reports"]["paths"])
            self.assertTrue(result["reports"]["paths"]["audit"].exists())
            self.assertFalse(result["national_complete"])

    def test_manual_full_export_still_writes_matrix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = national_history.export_audit(root / "missing.db", root / "out", "123", "2026-04-30", "2026-04-30")
            self.assertTrue(result["paths"]["matrix"].is_file())
            with result["paths"]["matrix"].open(encoding="utf-8-sig", newline="") as fh:
                self.assertEqual(next(csv.reader(fh))[4], "status")


class FocusedReviewTests(unittest.TestCase):
    setUp = fixtures.MovieHistoryTests.setUp

    def test_only_abnormal_roster_avoids_requerying_listed_dates(self):
        with self.conn:
            history_roster._merge(self.conn, fixtures.DAY, {fixtures.CODES[0]: "影院"}, "csv:test")
            history_roster._save_refresh(self.conn, fixtures.DAY, "test", "listed", 1, 1, 1)
        focused = national_history.make_plan(self.path, fixtures.MOVIE, fixtures.DAY, fixtures.DAY,
                                             retry_empty=True, refresh_roster=True, review_only=True)
        forced = national_history.make_plan(self.path, fixtures.MOVIE, fixtures.DAY, fixtures.DAY,
                                            retry_empty=True, refresh_roster=True)
        self.assertEqual(focused["totals"]["roster_days"], 0)
        self.assertEqual(forced["totals"]["roster_days"], 1)
        with self.conn:
            history_roster._save_refresh(self.conn, fixtures.DAY, "test", "partial", 1, 1, 1)
        focused_partial = national_history.make_plan(self.path, fixtures.MOVIE, fixtures.DAY, fixtures.DAY,
                                                     retry_empty=True, refresh_roster=True, review_only=True)
        self.assertEqual(focused_partial["totals"]["roster_days"], 1)

    def test_review_only_without_roster_refresh_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "名单"):
            national_history.make_plan(self.path, fixtures.MOVIE, fixtures.DAY, fixtures.DAY,
                                       review_only=True, refresh_roster=False)


class Fair204ReviewTests(unittest.TestCase):
    """Real SQLite + fake HTTP: a small finite batch must not starve 204s."""
    setUp = fixtures.MovieHistoryTests.setUp
    client = fixtures.MovieHistoryTests.client
    fake_open = fixtures.MovieHistoryTests.fake_open

    def _seed(self):
        with self.conn:
            history_roster._merge(self.conn, fixtures.DAY, dict.fromkeys(fixtures.CODES), "csv:test")
            history_roster._save_refresh(self.conn, fixtures.DAY, "test", "listed", 5, 1, 5)

    def _run(self, cap):
        p = national_history.make_plan(self.path, fixtures.MOVIE, fixtures.DAY, fixtures.DAY,
                                       max_requests=cap, minutes=1, retry_empty=True)
        return national_history.run_plan(p, self.root / "raw", self.root / "out",
                                         client_factory=self.client, full_matrix=False,
                                         emit=lambda _: None)

    def test_finite_batches_rotate_persistent_204_queue(self):
        self._seed()
        self.routes = [(204, None)] * 6
        for index in range(3):
            result = self._run(2)
            self.assertEqual(result["requests"], 2)
            self.assertEqual(result["empty"], 2)
            with self.conn:
                count = self.conn.execute("SELECT count(*) FROM history_204_review_queue").fetchone()[0]
            self.assertEqual(count, min((index + 1) * 2, 5))
        called_codes = [code for url in self.calls for code in fixtures.CODES if f"cinemaCode={code}" in url]
        self.assertEqual(len(set(called_codes[:5])), 5, "A 204 must not starve an untried cinema")
        # Every 204 is still an unknown; never silently rewrite to zero.
        report = national_history.export_audit(self.path, self.root / "out", fixtures.MOVIE,
                                               fixtures.DAY, fixtures.DAY)
        with report["paths"]["matrix"].open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["sales_fen"] == "" for row in rows))
        self.assertEqual(report["summary"]["followup"]["retry_204"], 5)

    def test_budgeted_review_rotates_dates_not_only_cinemas(self):
        import datetime as dt
        self._seed()
        second_day = (dt.date.fromisoformat(fixtures.DAY) + dt.timedelta(days=1)).isoformat()
        with self.conn:
            history_roster._merge(self.conn, second_day, dict.fromkeys(fixtures.CODES), "csv:test")
            history_roster._save_refresh(self.conn, second_day, "test", "listed", 5, 1, 5)
        self.routes = [(204, None)] * 4
        for _ in range(2):
            plan = national_history.make_plan(self.path, fixtures.MOVIE, fixtures.DAY, second_day,
                                              max_requests=2, minutes=1, retry_empty=True)
            result = national_history.run_plan(plan, self.root / "raw", self.root / "out",
                                               client_factory=self.client, full_matrix=False,
                                               emit=lambda _: None)
            self.assertEqual(result["requests"], 2)
        self.assertTrue(all(fixtures.DAY in url for url in self.calls[:2]))
        self.assertTrue(all(second_day in url for url in self.calls[2:4]),
                        "An untried day must receive a budget before repeatedly reviewing old 204s")

    def test_review_does_not_stop_at_three_204_with_finite_budget(self):
        self._seed()
        self.routes = [(204, None)] * 5
        result = self._run(10)
        self.assertEqual((result["requests"], result["empty"]), (5, 5))
        self.assertEqual(result["reports"]["summary"]["followup"]["retry_204"], 5)
        self.assertFalse(result["national_complete"])


if __name__ == "__main__":
    unittest.main()

"""离线回归，HTTP 层使用预设响应。"""
import csv
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from zgdypf import datasets, db, history_roster, movie_history as history
from zgdypf.cinema_detail import collect, reusable_slices
from zgdypf.client import BadPayload, Client

DAY = "2026-04-30"
MOVIE = "00100890026"
CODES = ("31081501", "33019193", "00112233", "10112233", "20112233")
RANK = {"salesRankList": [{"code": CODES[0]}], "hasMore": False}


def box(code=MOVIE, sales=12503):
    return {"movieCount": 1, "sessionCount": 7, "newMovieCount": 0,
            "movieSalesList": [{"movieCode": code, "movieName": "示例影片",
                               "sales": sales, "releaseDay": 8, "salesRateDesc": "4.5%",
                               "sumSalesDesc": "1.22万"}]}


class Reply(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode() if body is not None else b"")
        self.status = status


class MovieHistoryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = self.root / "boxoffice.db"
        self.conn = db.connect(str(self.path))
        self.addCleanup(self.conn.close)
        self.routes = []
        self.calls = []

    def fake_open(self, req, timeout):
        self.calls.append(req.full_url)
        status, data = self.routes.pop(0)
        if status >= 300:
            raise urllib.error.HTTPError(req.full_url, status, "test", data, io.BytesIO(b""))
        return Reply(status, data)

    def client(self, **kwargs):
        client = Client(**kwargs)
        client._open = self.fake_open
        client._throttle = lambda: None
        client.backoff = (0,)
        return client

    def repair(self, responses, codes=CODES, scope="explicit", **options):
        self.routes = list(responses)
        if scope == "sample":
            with self.conn:
                self.conn.executemany("INSERT OR IGNORE INTO dim_cinema(cinema_code,sample) VALUES (?,1)",
                                      [(code,) for code in codes])
        return history.repair(
            self.path, self.root / "raw", MOVIE, DAY, DAY, scope, codes if scope == "explicit" else (),
            client_factory=self.client, emit=lambda message: None, **options,
        )

    def test_budget_and_resume_across_jobs_and_movies(self):
        self.routes = [(200, box())]
        collect(self.conn, self.client(), CODES[0], DAY, "ds:cinema_box")
        first = self.repair([(200, box("00100000001"))], max_requests=1)
        self.assertEqual((first["requests"], first["ok"]), (1, 1))
        self.assertIn(CODES[1], self.calls[-1])
        second = self.repair([(200, box())], max_requests=1)
        self.assertEqual((second["requests"], second["ok"]), (1, 1))
        self.assertIn(CODES[2], self.calls[-1])
        self.assertEqual(reusable_slices(self.conn, DAY, MOVIE),
                         {CODES[0]: True, CODES[1]: False, CODES[2]: True})
        third = self.repair([], codes=CODES[:3])
        self.assertEqual(third["requests"], 0)

    def test_request_cap_counts_retries_and_probe(self):
        result = self.repair([(503, {}), (200, RANK)], scope="sample", max_requests=2)
        self.assertEqual((result["requests"], result["retries"], len(self.calls)), (2, 1, 2))
        self.assertIn("请求上限", result["stopped"])
        self.assertTrue(all("getCinemaList" in url for url in self.calls))

    def test_limit_and_redirect_stop_immediately(self):
        for status in (302, 401, 403, 429):
            with self.subTest(status=status):
                result = self.repair([(status, {"Retry-After": "120"})])
                self.assertEqual((result["requests"], result["retries"], result["failed"]), (1, 0, 1))
                self.assertIn(str(status), result["stopped"])

    def test_thin_day_cache(self):
        thin = self.repair([(200, {"salesRankList": [], "hasMore": True})], scope="sample")
        self.assertEqual((thin["requests"], thin["skipped_days"]), (1, 1))
        self.assertEqual(self.repair([], scope="sample")["requests"], 0)
        retry = self.repair([(200, RANK), (200, box())], scope="sample", max_requests=2, recheck_days=True)
        self.assertEqual(retry["ok"], 1)

    def test_unverified_204_and_reprobe(self):
        result = self.repair([(200, RANK), (204, None), (204, None), (204, None)], scope="sample")
        self.assertEqual((result["empty"], result["requests"]), (3, 4))
        self.assertEqual(self.repair([], scope="sample")["requests"], 0)
        plan = history.plan_totals(self.path, MOVIE, DAY, DAY, "explicit", CODES)
        self.assertEqual((plan["gaps"], plan["deferred"]), (2, 3))
        retried = self.repair([(200, box()), (200, box()), (200, box())],
                              max_requests=3, retry_empty=True)
        self.assertEqual(retried["ok"], 3)
        self.assertIn(sorted(CODES)[0], self.calls[-1])

    def test_explicit_calls_cinema_with_thin_probe_cached_and_one_request(self):
        self.repair([(200, {"salesRankList": []})], scope="sample")
        self.calls.clear()
        plan = history.plan_totals(self.path, MOVIE, DAY, DAY, "explicit", CODES[:1])
        self.assertEqual((plan["plannable"], plan["probe_days"], plan["cooldown_days"]), (1, 0, 0))
        result = self.repair([(204, None)], codes=CODES[:1], max_requests=1)
        self.assertEqual((result["requests"], result["empty"]), (1, 1))
        self.assertIn("/getCinemaBoxData?", self.calls[0])
        status, http = self.conn.execute("SELECT status,http_status FROM collect_ledger").fetchone()
        self.assertEqual((status, http), ("empty_unverified", 204))
        self.assertEqual(self.repair([], codes=CODES[:1])["requests"], 0)

    def test_bad_payload_and_204_preserve_existing(self):
        self.routes = [(200, box())]
        collect(self.conn, self.client(), CODES[0], DAY)
        duplicate = box()
        duplicate["movieSalesList"] *= 2
        duplicate["movieCount"] = 2
        for reply in ((200, None), (200, {"movieCount": 0, "movieSalesList": []}),
                      (200, duplicate), (200, box(sales="12503")), (204, None)):
            with self.subTest(reply=reply), self.assertRaises(BadPayload):
                self.routes = [reply]
                collect(self.conn, self.client(), CODES[0], DAY)
        self.assertEqual(reusable_slices(self.conn, DAY, MOVIE), {CODES[0]: True})
        self.assertEqual(self.conn.execute("SELECT status FROM collect_ledger").fetchone()[0], "ok")

    def test_write_failure_rolls_back_entire_slice(self):
        self.routes = [(200, box()), (200, box("00100000001"))]
        collect(self.conn, self.client(), CODES[0], DAY)
        self.conn.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON collect_ledger "
                          "BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            collect(self.conn, self.client(), CODES[0], DAY)
        self.assertEqual(reusable_slices(self.conn, DAY, MOVIE), {CODES[0]: True})

    def test_ledger_aliases_inconsistent_row_and_csv(self):
        self.routes = [(200, box())]
        collect(self.conn, self.client(), CODES[0], DAY, "ds:cinema_box")
        with self.conn:
            db.put_date_status(self.conn, DAY, 12500, "full")
            db.ledger_put(self.conn, "cinema_box", f"{CODES[1]}|{DAY}", "empty", 204)
            db.ledger_put(self.conn, "ds:cinema_box", f"{DAY}|{CODES[2]}", "ok")
            db.ledger_put(self.conn, "ds:cinema_box", f"{DAY}|{CODES[3]}", "empty")
            db.upsert_cinema_day(self.conn, dict(cinema_code=CODES[0], date=DAY, sales_fen=10000,
                                                 rank_sales_fen=10000, sales_src="ranklist", sales_lossy=1))
        report = history.export_reports(self.path, self.root, MOVIE, DAY, DAY, "explicit", CODES)
        with report["details"].open(encoding="utf-8-sig", newline="") as file:
            row = next(csv.DictReader(file))
        self.assertEqual((row["movie_code"], row["sales_fen"], row["sales_yuan"]),
                         (MOVIE, "12503", "125.03"))
        with report["audit"].open(encoding="utf-8-sig", newline="") as file:
            row = next(csv.DictReader(file))
        self.assertEqual(tuple(row[s] for s in ("with_movie", "empty_204", "inconsistent", "deferred_204", "missing")),
                         ("1", "1", "1", "1", "1"))
        with report["gaps"].open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(len(list(csv.DictReader(file))), 4)

    def test_thin_local_rank_blocks_wide_collection(self):
        with self.conn:
            db.put_date_status(self.conn, DAY, 1000, "full")
            db.upsert_cinema_day(self.conn, dict(cinema_code=CODES[0], date=DAY, sales_src="ranklist"))
        plan = history.plan_totals(self.path, MOVIE, DAY, DAY, "ranklist")
        self.assertEqual((plan["targets"], plan["plannable"], plan["missing_scope_days"]), (1, 0, 1))
        run = history.repair(self.path, self.root, MOVIE, DAY, DAY, "ranklist", client_factory=self.client)
        self.assertEqual(run["requests"], 0)

    def test_existing_desktop_engine_resumes_cli_success(self):
        self.routes = [(200, box()), (200, RANK), (200, box())]
        collect(self.conn, self.client(), CODES[0], DAY)
        ctx = datasets.Ctx(self.conn, self.client(), {"start": DAY, "end": DAY}, lambda message: None)
        engine = datasets.Engine(ctx)
        plan = [("cinema_box", [datasets.Task("cinema_box", f"{DAY}|{code}", date=DAY, entity=code)
                               for code in CODES[:2]])]
        report = engine.run(plan, lambda done, total: None)
        self.assertEqual((report["skip"], report["ok"], len(self.calls)), (1, 1, 3))
        with closing(db.connect(str(self.path))) as other:
            self.assertEqual(set(reusable_slices(other, DAY)), set(CODES[:2]))

    def test_cancel_before_any_request(self):
        self.assertEqual(self.repair([], stopped=lambda: True)["requests"], 0)

    def roster_refresh(self, responses, **options):
        self.routes = list(responses)
        return history.refresh_roster(
            self.path, self.root / "raw", DAY, DAY, emit=lambda message: None,
            client_factory=self.client, **options,
        )

    def test_thin_roster_merges_sources_and_queues_new_cinemas(self):
        with self.conn:
            db.upsert_cinema_day(self.conn, dict(cinema_code=CODES[0], date=DAY, sales_src="ranklist",
                                                 rank_sales_fen=130000))
            db.put_date_status(self.conn, DAY, 1, "thin")
            db.put_doc(self.conn, history.PROBE, DAY, DAY, {"status": "thin"})
        self.routes = [(200, box())]
        collect(self.conn, self.client(), CODES[0], DAY)
        source = self.root / "cinemas.csv"
        source.write_text(f"date,cinema_code,cinema_name\n{DAY},{CODES[2]},测试影院\n", encoding="utf-8")
        self.assertEqual(history_roster.import_csv(self.path, source)["records"], 1)
        update = self.roster_refresh([(200, {"salesRankList": [{"code": CODES[1]}], "hasMore": False})])
        self.assertEqual((update["requests"], update["new_candidates"]), (1, 1))
        self.assertEqual(self.roster_refresh([])["requests"], 0)
        _, _, _, usable, states, sources = history._day_plan(self.conn, MOVIE, DAY, "roster", ())
        self.assertFalse(usable)
        self.assertEqual(states, {CODES[0]: "with_movie", CODES[1]: "missing", CODES[2]: "missing"})
        self.assertEqual(sources[CODES[0]], ["local_detail", "local_rank"])
        self.assertEqual(sources[CODES[1]], ["site_rank"])
        self.assertEqual(sources[CODES[2]], ["csv:cinemas.csv"])
        plan = history.plan_totals(self.path, MOVIE, DAY, DAY, "roster")
        self.assertEqual((plan["targets"], plan["plannable"], plan["uncertain_roster_days"]), (3, 2, 1))
        result = self.repair([(200, box()), (200, box())], scope="roster", max_requests=2)
        self.assertEqual((result["requests"], result["ok"]), (2, 2))
        self.assertTrue(all("/getCinemaBoxData?" in url for url in self.calls[-2:]))
        report = history.export_reports(self.path, self.root, MOVIE, DAY, DAY, "roster")
        with report["audit"].open(encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f))
        self.assertEqual((row["coverage_status"], row["roster_rank_snapshot_status"],
                          row["national_roster_coverage"]), ("scope_observed", "thin", "unverified"))
        with report["roster"].open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 3)

    def test_roster_refresh_resumes_pagination_and_retains_old_rows(self):
        first = {"salesRankList": [{"code": CODES[0]}, {"code": CODES[1]}], "hasMore": True}
        last = {"salesRankList": [{"code": CODES[2]}], "hasMore": False}
        result = self.roster_refresh([(200, first)], max_requests=1)
        self.assertEqual(result["requests"], 1)
        state = history_roster.stored_refreshes(self.conn)[DAY]
        self.assertEqual((state["status"], state["next_offset"]), ("fetching", 2))
        result = self.roster_refresh([(200, last)], max_requests=1)
        self.assertEqual(result["updated_days"], 1)
        self.assertIn("offset=2", self.calls[-1])
        state = history_roster.stored_refreshes(self.conn)[DAY]
        self.assertEqual((state["status"], state["row_count"]), ("thin", 3))
        self.roster_refresh([(200, last)], refresh_again=True)
        self.assertEqual(set(history_roster.candidates(self.conn, DAY, [], [])), set(CODES[:3]))

    def test_roster_refresh_flags_repeated_page(self):
        page = {"salesRankList": [{"code": CODES[0]}], "hasMore": True}
        result = self.roster_refresh([(200, page), (200, page)])
        self.assertEqual(result["requests"], 2)
        state = history_roster.stored_refreshes(self.conn)[DAY]
        self.assertEqual((state["status"], state["row_count"]), ("partial", 1))

    def test_roster_repair_prioritizes_current_snapshot_without_dropping_old_cinemas(self):
        with self.conn:
            db.upsert_cinema_day(self.conn, dict(cinema_code=CODES[2], date=DAY, sales_src="ranklist"))
        self.roster_refresh([(200, {"salesRankList": [{"code": CODES[0]}], "hasMore": False})])
        first = self.repair([(200, box())], scope="roster", max_requests=1)
        self.assertEqual((first["requests"], first["ok"]), (1, 1))
        self.assertIn(CODES[0], self.calls[-1])
        second = self.repair([(200, box())], scope="roster", max_requests=1)
        self.assertEqual((second["requests"], second["ok"]), (1, 1))
        self.assertIn(CODES[2], self.calls[-1])

    def test_invalid_roster_csv_has_no_partial_writes(self):
        source = self.root / "bad.csv"
        source.write_text(f"date,cinema_code\n{DAY},{CODES[0]}\n{DAY},ABC\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            history_roster.import_csv(self.path, source)
        self.assertEqual(history_roster.candidates(self.conn, DAY, [], []), {})

    def test_quick_batch_shares_budget_exports_and_resumes(self):
        self.routes = [(200, {"salesRankList": [
            {"code": CODES[0]}, {"code": CODES[1]},
        ], "hasMore": False}), (200, box())]
        opts = dict(max_requests=2, emit=lambda _: None, client_factory=self.client)
        args = (self.path, self.root / "raw", self.root, MOVIE, DAY, DAY)
        phases = []
        first = history.run_quick_batch(*args, progress=phases.append, **opts)
        self.assertEqual((first["requests"], first["saved"], first["plan"]["plannable"]), (2, 1, 1))
        self.assertEqual(first["reports"]["detail_rows"], 1)
        self.assertTrue(first["reports"]["roster"].is_file())
        self.assertEqual(len(phases), 3)
        self.routes = [(200, box())]
        second = history.run_quick_batch(*args, **opts)
        self.assertEqual((second["requests"], second["saved"], second["plan"]["observed"]), (1, 1, 2))
        self.assertEqual(second["plan"]["uncertain_roster_days"], 1)
        self.assertEqual(second["reports"]["detail_rows"], 2)
        self.assertIn("/getCinemaBoxData?", self.calls[-1])

    def test_quick_batch_stops_followup_requests_when_roster_access_denied(self):
        with self.conn:
            db.upsert_cinema_day(self.conn, dict(cinema_code=CODES[0], date=DAY, sales_src="ranklist"))
        self.routes = [(429, {"Retry-After": "120"})]
        result = history.run_quick_batch(
            self.path, self.root / "raw", self.root, MOVIE, DAY, DAY,
            emit=lambda _: None, client_factory=self.client,
        )
        self.assertEqual((result["requests"], result["saved"], result["failed"]), (1, 0, True))
        self.assertEqual(result["plan"]["plannable"], 1)
        self.assertTrue(result["reports"]["audit"].is_file())

    def test_quick_batch_cancel_still_exports(self):
        result = history.run_quick_batch(
            self.path, self.root / "raw", self.root, MOVIE, DAY, DAY,
            stopped=lambda: True, emit=lambda _: None, client_factory=self.client,
        )
        self.assertEqual((result["requests"], result["reason"]), (0, "用户停止"))
        self.assertTrue(result["reports"]["audit"].is_file())

    def test_quick_film_search_deduplicates_title_and_keeps_code(self):
        with self.conn:
            db.upsert(self.conn, "dim_movie", {"movie_code": MOVIE, "movie_name": "电影测试"})
            db.upsert(self.conn, "fact_year_movie",
                      {"year": 2026, "movie_code": MOVIE, "movie_name": "电影测试"})
            db.upsert(self.conn, "fact_year_movie",
                      {"year": 2025, "movie_code": "00000000001", "movie_name": "电影测试"})
        self.assertEqual(set(history.find_films(self.path, "电影测试")),
                         {(MOVIE, "电影测试"), ("00000000001", "电影测试")})
        self.routes = [(200, [{"code": MOVIE, "name": "电影测试"}, {"code": MOVIE, "name": "电影测试"}])]
        self.assertEqual(history.search_films(self.root, "电影测试", client_factory=self.client),
                         [(MOVIE, "电影测试")])


if __name__ == "__main__":
    unittest.main()

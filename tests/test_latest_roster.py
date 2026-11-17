import csv
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import gui
import test_movie_history as fixtures
from zgdypf import history_roster, latest_roster
from zgdypf.client import AccessLimited, BadPayload
from zgdypf.movie_history import BatchEnded


RANGE = {"startDate": "2017-01-01", "endDate": "2026-11-17"}


def page(codes, more=False):
    return {"salesRankList": [{"code": code, "name": f"影院 {code}"} for code in codes], "hasMore": more}


class LatestRosterTests(unittest.TestCase):
    setUp = fixtures.MovieHistoryTests.setUp
    fake_open = fixtures.MovieHistoryTests.fake_open
    client = fixtures.MovieHistoryTests.client

    def download(self, **options):
        return latest_roster.download(self.root / "lists", client_factory=self.client, emit=lambda _: None, **options)

    def test_reads_all_pages_with_actual_offsets_and_writes_importable_csv(self):
        self.routes = [(200, RANGE), (200, page(("00112233", "00112234"), True)),
                       (200, page(("00112235",)))]
        result = self.download()
        queries = [parse_qs(urlsplit(url).query) for url in self.calls[1:]]
        self.assertEqual([q["offset"][0] for q in queries], ["0", "2"])
        self.assertEqual([q["date"][0] for q in queries], ["2026-11-17"] * 2)
        self.assertEqual(result["count"], 3)
        self.assertTrue(result["path"].read_bytes().startswith(b"\xef\xbb\xbf"))
        with result["path"].open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([row["cinema_code"] for row in rows], ["00112233", "00112234", "00112235"])
        self.assertTrue(all(row["national_roster_coverage"] == "unverified" for row in rows))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM history_cinema_roster").fetchone()[0], 0)
        self.assertEqual(history_roster.import_csv(self.path, result["path"]), {"days": 1, "records": 3})

    def test_empty_latest_day_falls_back_and_labels_actual_date(self):
        self.routes = [(200, RANGE), (204, None), (200, page(())), (200, page(("00112233",)))]
        result = self.download()
        self.assertEqual(result["latest_date"], "2026-11-17")
        self.assertEqual(result["date"], "2026-11-15")
        self.assertEqual(result["path"].name, "cinema_roster_2026-11-15.csv")

    def test_source_anomalies_keep_valid_codes_with_explicit_counts(self):
        self.routes = [(200, RANGE), (200, page(("00112233", "-", "00112233"), True)),
                       (200, page(("00112233", "00112234")))]
        result = self.download()
        self.assertEqual((result["count"], result["invalid_source_rows"], result["duplicate_source_rows"]), (2, 1, 2))
        self.assertIn("offset=3", self.calls[-1])
        with result["path"].open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["cinema_code"] for r in rows], ["00112233", "00112234"])
        self.assertTrue(all((r["invalid_source_rows"], r["duplicate_source_rows"]) == ("1", "2") for r in rows))

    def test_failed_pagination_preserves_previous_export(self):
        folder = self.root / "lists"
        folder.mkdir()
        previous = folder / "cinema_roster_2026-11-17.csv"
        previous.write_bytes(b"existing CSV")
        for second_page in ((200, page(("00112233",))), (204, None), (200, page(()))):
            with self.subTest(second_page=second_page):
                self.routes = [(200, RANGE), (200, page(("00112233",), True)), second_page]
                with self.assertRaises(BadPayload):
                    self.download()
                self.assertEqual(previous.read_bytes(), b"existing CSV")
                self.assertEqual(list(folder.iterdir()), [previous])

    def test_all_dates_empty_or_start_is_invalid_does_not_write(self):
        self.routes = [(200, RANGE)] + [(204, None)] * latest_roster.MAX_DATES
        with self.assertRaisesRegex(BadPayload, "没有返回任何影院"):
            self.download()
        self.assertEqual(len(self.calls), 1 + latest_roster.MAX_DATES)
        self.routes = [(200, {"startDate": "broken", "endDate": RANGE["endDate"]})]
        with self.assertRaisesRegex(BadPayload, "日期范围"):
            self.download()
        self.assertFalse((self.root / "lists").exists())

    def test_cancel_or_rate_limit_does_not_continue_downloading(self):
        self.routes = [(200, RANGE), (200, page(("00112233",), True))]
        with self.assertRaises(BatchEnded):
            self.download(stopped=lambda: len(self.calls) == 2)
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        self.routes = [(200, RANGE), (429, {})]
        with self.assertRaises(AccessLimited):
            self.download()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.root / "lists").exists())

    def test_main_page_button_runs_download_and_displays_result(self):
        self.routes = [(200, RANGE), (200, page(("00112233",)))]
        download = latest_roster.download
        with patch.object(gui, "ROOT", str(self.root)), patch.object(
                latest_roster, "download", side_effect=lambda folder, **kw: download(folder, client_factory=self.client, **kw)):
            app = gui.App()
            self.addCleanup(app.destroy)
            app.withdraw()
            self.assertEqual(app.btn_roster.cget("text"), "获取最新影院名录")
            app.btn_roster.invoke()
            self.assertEqual(str(app.btn_roster["state"]), "disabled")
            deadline = time.monotonic() + 5
            while app._running and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            self.assertFalse(app._running)
            self.assertEqual(str(app.btn_roster["state"]), "normal")
            self.assertIn("全国完整性未核实", app.lbl_status.cget("text"))
            target = self.root / "output" / "cinema_roster" / "cinema_roster_2026-11-17.csv"
            self.assertTrue(target.is_file())

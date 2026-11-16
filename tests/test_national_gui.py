"""真实 Tk 窗口交互；仅 HTTP 响应使用离线夹具。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import gui
import test_movie_history as fixtures
from national_history_gui import NationalHistoryPage
from zgdypf import datasets, national_history


class NationalGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = gui.App()
        self.addCleanup(self.app.destroy)
        self.app.withdraw()
        self.page = NationalHistoryPage(self.app, self.root / "boxoffice.db", self.root / "raw",
                                        self.root / "out", fixtures.DAY, fixtures.DAY)
        self.page.movie.set(fixtures.MOVIE)
        self.app.update()

    def wait(self):
        deadline = time.monotonic() + 8
        while self.page.busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.app.update()
        self.assertFalse(self.page.busy, "UI worker timed out")

    def test_original_24_datasets_and_independent_page(self):
        self.assertEqual(len(datasets.SPECS), 24)
        self.assertEqual(len(self.app.ds_vars), 24)
        self.app._preset_ds("all")
        self.assertEqual(len(self.app._selected()), 24)
        self.assertIn("全国全影院", self.page.title())
        self.assertEqual(str(self.page.run_button["state"]), "disabled")
        self.assertEqual(len(self.page.tabs.tabs()), 3)

    def test_offline_plan_parameter_invalidation_and_export(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("unexpected network")):
            self.page.preview_button.invoke()
            self.wait()
            self.assertIsNotNone(self.page.plan, self.page.status.get())
            self.assertFalse((self.root / "boxoffice.db").exists())
            self.assertEqual(str(self.page.run_button["state"]), "normal")
            self.assertIn("全国完整性未证实", self.page.plan_text.get("1.0", "end"))
            self.page.cap.set("2")
            self.assertIsNone(self.page.plan)
            self.assertEqual(str(self.page.run_button["state"]), "disabled")
            self.page.export_button.invoke()
            self.wait()
            self.assertEqual(len(list((self.root / "out").glob("*.csv"))), 2)

    def test_real_button_flow_run_and_resume_with_fake_http(self):
        fake = fixtures.MovieHistoryTests()
        fake.setUp()
        self.addCleanup(fake.doCleanups)
        fake.routes = [(200, {"salesRankList": [{"code": fixtures.CODES[0]}], "hasMore": False}),
                       (200, fixtures.box())]
        original_run = national_history.run_plan
        def run(*args, **kwargs):
            return original_run(*args, client_factory=fake.client, **kwargs)
        with patch.object(national_history, "run_plan", side_effect=run):
            self.page.cap.set("1")
            for _ in range(2):
                self.page.preview_button.invoke()
                self.wait()
                self.assertIsNotNone(self.page.plan, self.page.status.get())
                self.page.run_button.invoke()
                self.assertTrue(self.app._running)
                self.wait()
                self.assertIsNone(self.page.plan)
                self.assertIn("CSV 已导出", self.page.status.get())
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse(self.app._running)
        self.assertTrue(list((self.root / "out").glob("*_daily_audit.csv")))

    def test_validation_error_recovers_controls(self):
        self.page.end.set("2026-99-99")
        self.page.preview_button.invoke()
        self.wait()
        self.assertIn("处理未完成", self.page.status.get())
        self.assertIsNone(self.page.plan)
        self.assertEqual(str(self.page.preview_button["state"]), "normal")


if __name__ == "__main__":
    unittest.main()

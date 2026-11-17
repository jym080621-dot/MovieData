"""真实 Tk 窗口交互；仅 HTTP 响应使用离线夹具。"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import gui
import test_movie_history as fixtures
from national_history_gui import NationalHistoryPage
from zgdypf import datasets, national_history, db, history_roster
from zgdypf.cinema_detail import collect


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

    def test_main_window_has_one_history_entry(self):
        def button_texts(widget):
            for child in widget.winfo_children():
                if isinstance(child, gui.tk.Toplevel):
                    continue
                if child.winfo_class() == "TButton":
                    yield child.cget("text")
                yield from button_texts(child)
        labels = [label for label in button_texts(self.app) if "历史" in label]
        self.assertEqual(labels, ["全国全影院历史票房采集 →"])

    def test_offline_plan_parameter_invalidation_and_export(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("unexpected network")):
            self.page.preview_button.invoke()
            self.wait()
            self.assertIsNotNone(self.page.plan, self.page.status.get())
            self.assertFalse((self.root / "boxoffice.db").exists())
            self.assertEqual(str(self.page.run_button["state"]), "normal")
            text = self.page.plan_text.get("1.0", "end")
            self.assertIn("全国完整性未证实", text)
            self.assertIn("全程估算", text)
            self.assertIn("从当前断点估算", text)
            self.assertEqual(self.page.plan["options"]["rate"], 2)
            self.page.rate_choice.current(4)
            self.assertIsNone(self.page.plan)
            self.page.preview_button.invoke()
            self.wait()
            self.assertEqual(self.page.plan["options"]["rate"], 5)
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

    def test_small_window_keeps_results_button_fully_visible(self):
        self.page.destroy()
        self.app.tk.call("tk", "scaling", 2.0)
        self.page = NationalHistoryPage(self.app, self.root / "boxoffice.db", self.root / "raw",
                                        self.root / "out", fixtures.DAY, fixtures.DAY)
        self.page.geometry("960x710+20+20")
        self.page.status.set("到达时间预算 · 请求 264 次 · 核查 264 个影院日 · CSV 已导出。继续时请重新生成计划。" * 2)
        self.app.update()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        button = next(w for w in descendants(self.page)
                      if w.winfo_class() == "TButton" and w.cget("text") == "打开计划与结果目录")
        self.assertTrue(button.winfo_ismapped(), "结果目录按钮被挤出窗口")
        self.assertEqual(button.winfo_height(), button.winfo_reqheight(), "结果目录按钮被裁切")
        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                             self.page.winfo_rooty() + self.page.winfo_height())
        for control in (self.page.unlimited_button, self.page.stop_button):
            self.assertTrue(control.winfo_ismapped())
            self.assertEqual(control.winfo_width(), control.winfo_reqwidth())

    def test_resume_progress_restores_cached_cinema_days(self):
        fake = fixtures.MovieHistoryTests()
        fake.setUp()
        self.addCleanup(fake.doCleanups)
        self.page.db_path = fake.path
        with fake.conn:
            history_roster._merge(fake.conn, fixtures.DAY, dict.fromkeys(fixtures.CODES[:2]), "csv:test")
            history_roster._save_refresh(fake.conn, fixtures.DAY, "test", "thin", 2, 1, 2)
        fake.routes = [(200, fixtures.box())]
        collect(fake.conn, fake.client(), fixtures.CODES[0], fixtures.DAY)
        self.page.preview_button.invoke()
        self.wait()
        self.assertEqual(self.page.progress.completed, 1, "断点未恢复到已核查影院日")
        self.assertEqual(self.page.progress.total, 2)
        fake.routes = [(200, fixtures.box())]
        original_run = national_history.run_plan
        def run(*args, **kwargs):
            return original_run(*args, client_factory=fake.client, **kwargs)
        with patch.object(national_history, "run_plan", side_effect=run):
            self.page.run_button.invoke()
            self.assertEqual(self.page.progress.completed, 1, "开始下一批时累计值被清零")
            self.wait()
        self.assertEqual(self.page.progress.completed, 2)
        self.assertEqual(len(fake.calls), 2)  # one seeded request and one resumed request
        self.page.destroy()
        self.page = NationalHistoryPage(self.app, fake.path, self.root / "raw", self.root / "out",
                                        fixtures.DAY, fixtures.DAY)
        self.page.movie.set(fixtures.MOVIE)
        self.page.preview_button.invoke()
        self.wait()
        self.assertEqual(self.page.progress.completed, 2, "重新打开页面未读取累计值")

    def test_request_counter_does_not_overwrite_coverage(self):
        self.page.show_coverage(dict(targets=410208, cached=484))
        self.page.batch_limit, self.page.batch_minutes = 3000, 5
        self.page.events.put(("progress", "正在采集", 264))
        self.app.after(160, lambda: None)
        deadline = time.monotonic() + .3
        while time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.assertEqual(self.page.progress.completed, 484)
        self.assertEqual(self.page.progress.total, 410208)
        self.assertIn("264 / 3,000", self.page.batch_text.get())
        self.page.cap.set("100")
        self.assertEqual(self.page.progress.completed, 484)
        self.page.movie.set("123")
        self.assertEqual(self.page.progress.completed, 0)

    def test_small_fraction_draws_a_visible_blue_segment_and_keeps_exact_percent(self):
        self.page.show_coverage(dict(targets=410304, cached=613))
        self.app.update()
        (segment,) = self.page.progress.find_withtag("coverage")
        start, _, end, _ = self.page.progress.coords(segment)
        self.assertGreaterEqual(end - start, 6)
        self.assertIn("0.15%", self.page.coverage_text.get())
        self.page.show_coverage(dict(targets=410304, cached=0))
        self.assertEqual(self.page.progress.itemcget(segment, "state"), "hidden")

    def test_unlimited_shortcut_pauses_inflight_request_and_resumes_from_saved_plan(self):
        fake = fixtures.MovieHistoryTests()
        fake.setUp()
        self.addCleanup(fake.doCleanups)
        self.page.db_path = fake.path
        with fake.conn:
            history_roster._merge(fake.conn, fixtures.DAY, dict.fromkeys(fixtures.CODES[:2]), "csv:test")
            history_roster._save_refresh(fake.conn, fixtures.DAY, "test", "thin", 2, 1, 2)
        fake.routes = [(200, fixtures.box()), (200, fixtures.box())]
        inflight, release = threading.Event(), threading.Event()
        original_open = fake.fake_open

        def hold_first(req, timeout):
            if not inflight.is_set():
                inflight.set()
                if not release.wait(4):
                    raise AssertionError("Test did not release the request")
            return original_open(req, timeout)

        fake.fake_open = hold_first
        original_run = national_history.run_plan
        def run(*args, **kwargs):
            return original_run(*args, client_factory=fake.client, **kwargs)

        with patch.object(national_history, "run_plan", side_effect=run):
            self.page.unlimited_button.invoke()
            self.wait()
            self.assertEqual(fake.calls, [])
            self.assertEqual((self.page.cap.get(), self.page.minutes.get()), ("0", "0"))
            self.assertIn("时间上限 不限", self.page.plan_text.get("1.0", "end"))
            self.page.unlimited_button.invoke()
            try:
                self.assertTrue(inflight.wait(4))
                self.assertEqual(str(self.page.stop_button["state"]), "normal")
                self.page.stop_button.invoke()
            finally:
                release.set()
            self.wait()
            self.assertIn("断点已保存", self.page.status.get())
            self.assertEqual(self.page.progress.completed, 1)
            self.assertEqual(self.page.plan["totals"]["pending"], 1)
            self.page.run_button.invoke()
            self.wait()
        self.assertEqual(self.page.progress.completed, 2)
        self.assertEqual(len(set(fake.calls)), 2)

    def test_review_button_plans_offline_and_resumes_remaining_roster_dates(self):
        fake = fixtures.MovieHistoryTests()
        fake.setUp()
        self.addCleanup(fake.doCleanups)
        self.page.db_path = fake.path
        self.page.end.set('2026-05-01')
        self.page.cap.set('1')
        with fake.conn:
            for day in (fixtures.DAY, '2026-05-01'):
                history_roster._merge(fake.conn, day, {fixtures.CODES[0]: '测试'}, 'csv:test')
                history_roster._save_refresh(fake.conn, day, 'seed', 'thin', 1, 1, 1)
        fake.routes = [(200, {'salesRankList': [], 'hasMore': False})] * 2
        original_run = national_history.run_plan
        def run(*args, **kwargs):
            return original_run(*args, client_factory=fake.client, **kwargs)
        with patch.object(national_history, 'run_plan', side_effect=run):
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network during preview')):
                self.page.review_button.invoke()
                self.wait()
            self.assertEqual(fake.calls, [])
            self.assertTrue(self.page.plan['options']['review_only'])
            self.page.run_button.invoke()
            self.wait()
            self.assertIsNotNone(self.page.plan, self.page.status.get())
            self.assertEqual(self.page.plan['options']['reviewed_roster_days'], [fixtures.DAY])
            self.page.run_button.invoke()
            self.wait()
        self.assertEqual(len(fake.calls), 2)
        self.assertIn('2026-05-01', fake.calls[1])
        self.assertFalse(list((self.root/'out').glob('*_cinema_days.csv')))

    def test_review_button_visible_in_supported_window(self):
        self.page.geometry('1100x780+20+20')
        self.app.update()
        button = self.page.review_button
        self.assertTrue(button.winfo_ismapped())
        self.assertEqual(button.winfo_width(), button.winfo_reqwidth())
        self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),
                             self.page.winfo_rootx()+self.page.winfo_width())

    def test_independent_registry_import_through_existing_button(self):
        import test_cinema_registry as registry_fixtures
        helper = registry_fixtures.RegistryTests()
        helper.root = self.root
        csv_path, manifest = helper.files()
        # Companion manifest is adjacent; this flow does not request the network.
        with patch('national_history_gui.filedialog.askopenfilename', return_value=str(csv_path)), \
             patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            self.page.import_button.invoke()
            self.wait()
            self.assertIn('独立名录', self.page.status.get())
            self.page.preview_button.invoke()
            self.wait()
        self.assertEqual(self.page.plan['totals']['targets'], 2)
        self.assertEqual(self.page.plan['days'][0]['registry']['reference_count'], 2)
        self.assertFalse(self.page.plan['national_complete'])
        self.page.export_button.invoke()
        self.wait()
        self.assertTrue(list((self.root/'out').rglob('registry_gaps.csv')))

    def test_registry_manifest_missing_recovers_controls_without_creating_database(self):
        path = self.root/'without-manifest.csv'
        path.write_text('cinema_code\n31081501\n')
        with patch('national_history_gui.filedialog.askopenfilename', return_value=str(path)):
            self.page.import_button.invoke()
            self.wait()
        self.assertIn('处理未完成', self.page.status.get())
        self.assertFalse(self.page.db_path.exists())
        self.assertEqual(str(self.page.import_button['state']), 'normal')


if __name__ == "__main__":
    unittest.main()

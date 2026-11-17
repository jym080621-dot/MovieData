"""独立全国历史页面；后台任务只通过队列更新界面。"""
from __future__ import annotations

import os
import csv
import queue
import sqlite3
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from zgdypf import __version__, db, history_roster, movie_history, cinema_registry
from zgdypf import national_history as national


class CoverageBar(tk.Canvas):
    """Draw a visible mark even when the proportional fill is only a few pixels."""

    def __init__(self, parent):
        super().__init__(parent, height=14, background="#dce5f0", highlightthickness=0, borderwidth=0)
        self.completed = self.total = 0
        self.segment = self.create_rectangle(0, 0, 0, 0, fill="#2563eb", width=0, tags="coverage", state="hidden")
        self.bind("<Configure>", self.redraw)

    def set_counts(self, completed, total):
        self.completed, self.total = completed, total
        self.redraw()

    def redraw(self, _event=None):
        if not self.completed or not self.total:
            self.itemconfigure(self.segment, state="hidden")
            return
        width, height = self.winfo_width(), self.winfo_height()
        fill = min(width, max(6, int(width * self.completed / self.total)))
        self.coords(self.segment, 0, 0, fill, height)
        self.itemconfigure(self.segment, state="normal")


class NationalHistoryPage(tk.Toplevel):
    def __init__(self, app, db_path, raw_dir, out_dir, start, end):
        super().__init__(app)
        self.app, self.db_path, self.raw_dir = app, db_path, raw_dir
        self.out_dir = Path(out_dir)
        self.title(f"全国全影院历史票房采集 · MovieData v{__version__}")
        self.fit_desktop()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.plan = None
        self.busy = False
        self.close_callback = None
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.selected = None
        self.results = []
        self.movie = tk.StringVar(self)
        self.start = tk.StringVar(self, start)
        self.end = tk.StringVar(self, end)
        self.cap = tk.StringVar(self, "3000")
        self.minutes = tk.StringVar(self, "60")
        self.rate = tk.StringVar(self, f"{national.DEFAULT_RATE:g}")
        self.retry_empty = tk.BooleanVar(self, False)
        self.refresh = tk.BooleanVar(self, False)
        self.status = tk.StringVar(self, "先填写电影及日期，再生成离线计划。")
        self.coverage_text = tk.StringVar(self, "已知名单累计核查：生成计划后读取断点")
        self.batch_text = tk.StringVar(self, "本批请求额度单独计数；累计核查进度从数据库恢复。")
        self.coverage_selection = None
        self.controls = []
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        # Reserve footer space before the expanding notebook. At higher display
        # scaling the old pack order gave all space to the notebook first.
        footer = ttk.Frame(body)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        self.open_button = ttk.Button(footer, text="打开计划与结果目录", command=self.open_results)
        self.open_button.pack(side="bottom", anchor="e", pady=(4, 0))
        self.coverage_label = ttk.Label(footer, textvariable=self.coverage_text)
        self.coverage_label.pack(fill="x")
        self.progress = CoverageBar(footer)
        self.progress.pack(fill="x", pady=(3, 4))
        self.batch_label = ttk.Label(footer, textvariable=self.batch_text)
        self.batch_label.pack(fill="x")
        self.status_label = ttk.Label(footer, textvariable=self.status)
        self.status_label.pack(fill="x", pady=(3, 0))
        self.notice_label = ttk.Label(footer, text="全国完整性未证实；蓝条仅表示已知名单的累计核查进度。", foreground="#9a3412")
        self.notice_label.pack(fill="x", pady=3)
        self.bind("<Configure>", self.resize_text, add="+")
        header = ttk.Frame(body)
        header.pack(fill="x")
        ttk.Label(header, text="全国全影院历史票房采集", style="H1.TLabel").pack(anchor="w")
        ttk.Label(header, text="固定范围：全国全部影院　｜　先审查计划，再启动网络采集", style="Sub.TLabel").pack(anchor="w", pady=(4, 10))
        form = ttk.Frame(header)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="电影名称 / 编码").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.film_entry = ttk.Entry(form, textvariable=self.movie)
        self.film_entry.grid(row=0, column=1, sticky="ew")
        self.controls.append(self.film_entry)
        for col, title, command in ((2, "本地查找", lambda: self.search(False)), (3, "联网查找电影", lambda: self.search(True))):
            button = ttk.Button(form, text=title, command=command)
            button.grid(row=0, column=col, padx=(8, 0))
            self.controls.append(button)
        self.choices = ttk.Combobox(form, state="readonly")
        self.choices.grid(row=1, column=1, columnspan=3, sticky="ew", pady=5)
        self.choices.bind("<<ComboboxSelected>>", self.choose)
        dates = ttk.Frame(header)
        dates.pack(fill="x", pady=5)
        budget = ttk.Frame(header)
        budget.pack(fill="x", pady=3)
        for row, fields in ((dates, (("开始日期", self.start, 13), ("结束日期", self.end, 13))),
                            (budget, (("请求上限", self.cap, 7), ("分钟", self.minutes, 6)))):
            for title, variable, width in fields:
                ttk.Label(row, text=title).pack(side="left", padx=(0, 5))
                entry = ttk.Entry(row, textvariable=variable, width=width)
                entry.pack(side="left", padx=(0, 12))
                self.controls.append(entry)
        self.unlimited_button = ttk.Button(budget, text="一键查询全量数据（无限制）", command=self.unlimited)
        self.unlimited_button.pack(side="left", padx=(0, 12))
        self.controls.append(self.unlimited_button)
        ttk.Label(budget, text="次/秒上限").pack(side="left", padx=(0, 5))
        self.rate_choice = ttk.Combobox(budget, textvariable=self.rate, values=(1, 2, 3, 4, 5), width=5)
        self.rate_choice.pack(side="left")
        self.controls.append(self.rate_choice)
        options = ttk.Frame(header)
        options.pack(fill="x", pady=4)
        for title, variable in (("重试已有 204（否则保留待复核）", self.retry_empty), ("重新核实名单（含薄名单 / 异常分页）", self.refresh)):
            check = ttk.Checkbutton(options, text=title, variable=variable)
            check.pack(side="left", padx=(0, 16))
            self.controls.append(check)
        self.import_button = ttk.Button(options, text="导入补充名单 CSV", command=self.import_roster)
        self.import_button.pack(side="right")
        self.controls.append(self.import_button)
        actions = ttk.Frame(header)
        actions.pack(fill="x", pady=(8, 10))
        self.preview_button = ttk.Button(actions, text="① 生成离线执行计划", command=self.preview)
        self.preview_button.pack(side="left")
        self.run_button = ttk.Button(actions, text="② 按计划启动 / 继续采集", command=self.run, style="Accent.TButton", state="disabled")
        self.run_button.pack(side="left", padx=8)
        self.review_button = ttk.Button(actions, text="③ 复核缺口", command=self.review)
        self.review_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="暂停并保存断点", command=self.stop, style="Stop.TButton", state="disabled")
        self.stop_button.pack(side="left")
        self.export_button = ttk.Button(actions, text="导出当前 CSV / 审计", command=self.export)
        self.export_button.pack(side="right")
        self.controls.extend((self.preview_button, self.review_button, self.export_button))
        self.tabs = ttk.Notebook(body)
        self.tabs.pack(fill="both", expand=True)
        overview = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(overview, text="执行计划")
        self.plan_text = tk.Text(overview, height=1, wrap="word", font=("Microsoft YaHei UI", 10), relief="flat", state="disabled")
        plan_scroll = ttk.Scrollbar(overview, command=self.plan_text.yview)
        self.plan_text.configure(yscrollcommand=plan_scroll.set)
        plan_scroll.pack(side="right", fill="y")
        self.plan_text.pack(fill="both", expand=True)
        self._write(self.plan_text, "计划为本地只读预览，不发出任何网络请求。\n\n" + national.NOTICE)
        detail = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(detail, text="逐日名单与缺口")
        columns = ("date", "targets", "cached", "pending", "empty", "roster")
        self.table = ttk.Treeview(detail, columns=columns, show="headings", height=1)
        for key, title, width in zip(columns, ("日期", "已知影院", "可复用", "待补", "204 待复核", "名单来源状态"), (115, 90, 90, 90, 100, 170)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor="center")
        scroll = ttk.Scrollbar(detail, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        logs = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(logs, text="执行日志")
        self.logs = tk.Text(logs, wrap="word", state="disabled", height=1)
        log_scroll = ttk.Scrollbar(logs, command=self.logs.yview)
        self.logs.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.logs.pack(fill="both", expand=True)
        for var in (self.movie, self.start, self.end, self.cap, self.minutes, self.rate, self.retry_empty, self.refresh):
            var.trace_add("write", self.invalidate)
        self.poll_id = self.after(100, self.poll)

    def fit_desktop(self):
        left, top, right, bottom = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight() - 40
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
            api = ctypes.windll.user32
            api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            api.MonitorFromWindow.restype = wintypes.HANDLE
            api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            monitor = api.MonitorFromWindow(self.app.winfo_id(), 2)
            if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
                left, top, right, bottom = info.work.left, info.work.top, info.work.right, info.work.bottom
        width, height = min(1120, right - left - 40), min(820, bottom - top - 70)
        self.minsize(min(900, width), min(600, height))
        x, y = left + (right-left-width)//2, top + 20
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def resize_text(self, event):
        if event.widget is self:
            width = max(200, event.width - 44)
            for label in (self.coverage_label, self.batch_label, self.status_label, self.notice_label):
                label.configure(wraplength=width)

    def show_coverage(self, totals):
        done, targets = totals.get("cached", 0), totals.get("targets", 0)
        self.coverage_selection = self.selection()
        self.progress.set_counts(done, targets)
        if not targets:
            percent = "（名单数量待核实）"
        elif 0 < done / targets < 0.0001:
            percent = "（<0.01%）"
        elif done < targets and done / targets >= 0.99995:
            percent = "（>99.99%）"
        else:
            percent = f"（{done / targets:.2%}）"
        suffix = "；名单仍待补充，分母可能增加" if totals.get("roster_days") else ""
        self.coverage_text.set(f"已知名单累计核查：{done:,} / {targets:,} 个影院日 {percent}{suffix}")

    @staticmethod
    def _write(widget, text, append=False):
        widget.configure(state="normal")
        if not append:
            widget.delete("1.0", "end")
        widget.insert("end", text + "\n")
        if append and int(widget.index("end-1c").split(".")[0]) > 2200:
            widget.delete("1.0", "201.0")
        widget.see("end" if append else "1.0")
        widget.configure(state="disabled")

    def invalidate(self, *_):
        self.plan = None
        self.run_button.configure(state="disabled")
        if self.coverage_selection != self.selection():
            self.progress.set_counts(0, 0)
            self.coverage_text.set("当前影片 / 日期的累计进度：生成计划后读取断点")
        if not self.busy:
            self.status.set("条件已改变，请重新生成离线执行计划。")

    def selection(self):
        value = self.movie.get().strip()
        code = self.selected[0] if self.selected and value == self.selected[1] else value
        return code, self.start.get().strip(), self.end.get().strip()

    def options(self):
        return dict(max_requests=int(self.cap.get()), minutes=float(self.minutes.get()), rate=float(self.rate.get()),
                    retry_empty=self.retry_empty.get(), refresh_roster=self.refresh.get(),
                    review_only=getattr(self, "review_only", False))

    def preview(self, review_mode=False):
        self.review_only = bool(review_mode)
        try:
            args, options = self.selection(), self.options()
        except ValueError:
            self.status.set("请检查批次请求上限、分钟数和请求频率。")
            return
        def work():
            plan = national.make_plan(self.db_path, *args, **options)
            national.save_plan(plan, self.out_dir)
            return plan
        self.begin("plan", work)

    def review(self):
        """One-click, explicit *planning* of 204 and questionable roster rechecks.

        This does not start any network requests. The operator must approve the
        newly generated plan, preserving a guard against endless 204 retries.
        """
        if self.busy or self.app._running:
            return
        self.retry_empty.set(True)
        self.refresh.set(True)
        self.preview(review_mode=True)

    def unlimited(self):
        if self.busy or self.app._running:
            return
        if self.plan and not (self.plan["options"]["max_requests"] or self.plan["options"]["minutes"]):
            self.run()
            return
        self.cap.set("0")
        self.minutes.set("0")
        self.preview()

    def request_count_text(self, count):
        limit = f" / {self.batch_limit:,}" if self.batch_limit else ""
        return f"本批请求 {count:,}{limit} 次"

    def run(self):
        if self.plan is None:
            self.status.set("请先生成并审查执行计划。")
            return
        approved = self.plan
        self.show_coverage(approved["totals"])
        self.batch_limit = approved["options"]["max_requests"]
        self.batch_minutes = approved["options"]["minutes"]
        budget = national.format_budget(self.batch_limit, self.batch_minutes)
        self.batch_text.set(f"{self.request_count_text(0)} · {budget}")

        def work():
            result = national.run_plan(
                approved, self.raw_dir, self.out_dir, stopped=self.stop_event.is_set,
                emit=lambda msg: self.events.put(("log", msg)),
                progress=lambda text, count: self.events.put(("progress", text, count)),
                on_coverage=lambda totals: self.events.put(("coverage", totals)),
                full_matrix=False,  # Per-batch audit only; explicit Export writes full matrix.
            )
            if result["paused"] or (approved["options"].get("review_only") and
                    result["reason"] in ("到达请求上限", "到达时间预算")):
                try:
                    opts = dict(approved["options"])
                    if opts.get("review_only"):
                        opts["reviewed_roster_days"] = result["reviewed_roster_days"]
                    else:
                        opts.update(refresh_roster=False, review_only=False, reviewed_roster_days=[])
                    resume = national.make_plan(self.db_path, approved["movie"], approved["start"], approved["end"], **opts)
                    national.save_plan(resume, self.out_dir)
                    result["resume_plan"] = resume
                except (OSError, ValueError, sqlite3.Error) as exc:
                    self.events.put(("log", f"断点已保存，续跑计划需手动生成：{exc}"))
            return result

        self.begin("run", work)

    def search(self, remote):
        query = self.movie.get().strip()
        if not query:
            self.status.set("请先输入电影名称或编码。")
            return
        self.begin("search", lambda: movie_history.search_films(self.raw_dir, query, stopped=self.stop_event.is_set)
                   if remote else movie_history.find_films(self.db_path, query))

    def choose(self, *_):
        index = self.choices.current()
        if index >= 0:
            self.selected = self.results[index]
            self.movie.set(self.selected[1])
            self.status.set(f"已选择 {self.selected[1]} / {self.selected[0]}，请生成计划。")

    def import_roster(self):
        if self.busy or self.app._running:
            return
        path = filedialog.askopenfilename(parent=self, filetypes=[("CSV 名单", "*.csv")])
        if path:
            def work():
                with open(path, encoding="utf-8-sig", newline="") as fh:
                    fields = csv.DictReader(fh).fieldnames or []
                if "date" in fields:
                    conn = db.connect(str(self.db_path))
                    conn.close()
                    return history_roster.import_csv(self.db_path, path)
                manifest = Path(path).with_suffix(".manifest.json")
                return cinema_registry.import_directory(self.db_path, path, manifest)
            self.begin("import", work)

    def export(self):
        args = self.selection()
        self.begin("export", lambda: national.export_audit(self.db_path, self.out_dir, *args))

    def begin(self, mode, work):
        if self.busy or self.app._running:
            self.status.set("另一个任务正在运行，请先暂停或等待结束。")
            return
        self.busy = self.app._running = True
        self.stop_event.clear()
        self.mode = mode
        for control in (*self.controls, self.choices, self.run_button):
            control.configure(state="disabled")
        self.stop_button.configure(state="normal" if mode in ("run", "search") else "disabled")
        self.status.set({"plan": "正在离线生成计划…", "run": "正在按计划执行…", "export": "正在离线导出…",
                         "import": "正在校验名单…", "search": "正在查找电影…"}[mode])
        def worker():
            try:
                self.events.put(("done", mode, work(), None))
            except Exception as exc:
                self.events.put(("done", mode, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            for _ in range(100):
                event = self.events.get_nowait()
                if event[0] == "done":
                    self.finish(*event[1:])
                    if self.close_callback:
                        self.close_callback()
                        return
                elif event[0] == "log":
                    self._write(self.logs, event[1], True)
                elif event[0] == "progress":
                    exporting = event[1] == "生成逐日审计及 CSV"
                    if exporting or not self.stop_event.is_set():
                        self.status.set(event[1])
                    if exporting:
                        self.stop_button.configure(state="disabled")
                    budget = national.format_budget(self.batch_limit, self.batch_minutes)
                    self.batch_text.set(f"{self.request_count_text(event[2])} · {budget}")
                elif event[0] == "coverage":
                    self.show_coverage(event[1])
        except queue.Empty:
            pass
        self.poll_id = self.after(100, self.poll)

    def finish(self, mode, result, error):
        self.busy = self.app._running = False
        for control in self.controls:
            control.configure(state="normal")
        self.choices.configure(state="readonly")
        self.stop_button.configure(state="disabled")
        self.invalidate()
        if error:
            self.status.set(f"处理未完成：{error}")
            self._write(self.logs, error, True)
            return
        if mode == "plan":
            self.display_plan(result)
            self.status.set(f"计划已保存 · {result['id'][:16]} · 审查后点击第二步启动。")
        elif mode == "search":
            self.results, self.selected = result, None
            self.choices.configure(values=[f"{name} [{code}]" for code, name in result])
            self.choices.set("")
            self.status.set(f"找到 {len(result)} 部影片，请在下拉框选择；也可直接输入数字编码。")
        elif mode == "run":
            self.refresh.set(False)
            self.show_coverage(national.coverage_from_summary(result["reports"]["summary"]))
            reason = "已暂停，断点已保存" if result["paused"] else result["reason"]
            self.batch_text.set(f"{self.request_count_text(result['requests'])} · {reason}")
            next_plan = result.get("resume_plan")
            if next_plan:
                next_step = "已准备续跑计划，审查后可继续。"
            elif result.get("needs_review"):
                next_step = "存在未闭环的 204 / 失败 / 名单问题，请点③复核缺口生成新计划。"
            else:
                next_step = "如需完整影院日矩阵，请点「导出当前 CSV / 审计」。"
            self.status.set(f"{'本批有异常 · ' if result['failed'] else ''}{reason} · 核查 {result['saved']:,} 个影院日 · 审计 CSV 已导出。{next_step}")
            self._write(self.logs, self.status.get(), True)
            if next_plan:
                self.display_plan(next_plan)
            else:
                self.tabs.select(2)
        elif mode == "export":
            self.show_coverage(national.coverage_from_summary(result["summary"]))
            self.status.set("已导出影院日明细、逐日审计和完整性说明。全国完整性仍未证实。")
        elif result.get("source_id"):
            self.status.set(f"导入独立名录 {result['records']:,} 家，新增 {len(result['new_codes']):,} 个编码；全国完整性尚未证实，请重新生成计划。")
        else:
            self.status.set(f"导入 {result['records']:,} 条名单记录，覆盖 {result['days']} 天；请重新生成计划。")

    def display_plan(self, plan):
        self.plan = plan
        self.show_coverage(plan["totals"])
        self._write(self.plan_text, national.format_plan(plan))
        self.table.delete(*self.table.get_children())
        for row in plan["days"]:
            states = row["states"]
            self.table.insert("", "end", values=(row["date"], row["targets"], row["cached"], row["pending"],
                states.get("empty_204", 0) + states.get("deferred_204", 0), row["roster_status"]))
        self.tabs.select(0)
        self.run_button.configure(state="normal")

    def stop(self):
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在暂停；等待当前请求结束后保存进度并导出。")

    def open_results(self):
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(self.out_dir)
        except OSError as exc:
            self.status.set(str(exc))

    def close(self, after=None):
        callback = after or self.destroy
        if self.busy:
            self.close_callback = callback
            if self.mode in ("run", "search"):
                self.stop()
        else:
            callback()

    def destroy(self):
        if hasattr(self, "poll_id"):
            self.after_cancel(self.poll_id)
        super().destroy()

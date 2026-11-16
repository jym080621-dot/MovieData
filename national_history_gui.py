"""独立全国历史页面；后台任务只通过队列更新界面。"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from zgdypf import db, history_roster, movie_history
from zgdypf import national_history as national


class NationalHistoryPage(tk.Toplevel):
    def __init__(self, app, db_path, raw_dir, out_dir, start, end):
        super().__init__(app)
        self.app, self.db_path, self.raw_dir = app, db_path, raw_dir
        self.out_dir = Path(out_dir)
        self.title("全国全影院历史票房采集 · MovieData v1.4.0")
        self.geometry("1120x820")
        self.minsize(960, 710)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.plan = None
        self.busy = False
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.selected = None
        self.results = []
        self.movie = tk.StringVar(self)
        self.start = tk.StringVar(self, start)
        self.end = tk.StringVar(self, end)
        self.cap = tk.StringVar(self, "3000")
        self.minutes = tk.StringVar(self, "60")
        self.rate = tk.StringVar(self, "1")
        self.retry_empty = tk.BooleanVar(self, False)
        self.refresh = tk.BooleanVar(self, False)
        self.status = tk.StringVar(self, "先填写电影及日期，再生成离线计划。")
        self.controls = []
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="全国全影院历史票房采集", style="H1.TLabel").pack(anchor="w")
        ttk.Label(body, text="固定范围：全国全部影院　｜　先审查计划，再启动网络采集", style="Sub.TLabel").pack(anchor="w", pady=(4, 10))
        form = ttk.Frame(body)
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
        dates = ttk.Frame(body)
        dates.pack(fill="x", pady=5)
        for title, variable, width in (("开始日期", self.start, 13), ("结束日期", self.end, 13),
                                        ("每批请求上限", self.cap, 7), ("分钟", self.minutes, 6), ("次/秒", self.rate, 5)):
            ttk.Label(dates, text=title).pack(side="left", padx=(0, 5))
            entry = ttk.Entry(dates, textvariable=variable, width=width)
            entry.pack(side="left", padx=(0, 12))
            self.controls.append(entry)
        options = ttk.Frame(body)
        options.pack(fill="x", pady=4)
        for title, variable in (("重试已有 204（否则保留待复核）", self.retry_empty), ("重新核实名单（含薄名单 / 异常分页）", self.refresh)):
            check = ttk.Checkbutton(options, text=title, variable=variable)
            check.pack(side="left", padx=(0, 16))
            self.controls.append(check)
        self.import_button = ttk.Button(options, text="导入补充名单 CSV", command=self.import_roster)
        self.import_button.pack(side="right")
        self.controls.append(self.import_button)
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(8, 10))
        self.preview_button = ttk.Button(actions, text="① 生成离线执行计划", command=self.preview)
        self.preview_button.pack(side="left")
        self.run_button = ttk.Button(actions, text="② 按计划启动 / 续跑一批", command=self.run, style="Accent.TButton", state="disabled")
        self.run_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(actions, text="暂停", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        self.export_button = ttk.Button(actions, text="导出当前 CSV / 审计", command=self.export)
        self.export_button.pack(side="right")
        self.controls.extend((self.preview_button, self.export_button))
        self.tabs = ttk.Notebook(body)
        self.tabs.pack(fill="both", expand=True)
        overview = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(overview, text="执行计划")
        self.plan_text = tk.Text(overview, height=11, wrap="word", font=("Microsoft YaHei UI", 10), relief="flat", state="disabled")
        self.plan_text.pack(fill="both", expand=True)
        self._write(self.plan_text, "计划为本地只读预览，不发出任何网络请求。\n\n" + national.NOTICE)
        detail = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(detail, text="逐日名单与缺口")
        columns = ("date", "targets", "cached", "pending", "empty", "roster")
        self.table = ttk.Treeview(detail, columns=columns, show="headings", height=9)
        for key, title, width in zip(columns, ("日期", "已知影院", "可复用", "待补", "204 待复核", "名单来源状态"), (115, 90, 90, 90, 100, 170)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor="center")
        scroll = ttk.Scrollbar(detail, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        logs = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(logs, text="执行日志")
        self.logs = tk.Text(logs, wrap="word", state="disabled", height=9)
        log_scroll = ttk.Scrollbar(logs, command=self.logs.yview)
        self.logs.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.logs.pack(fill="both", expand=True)
        self.progress = ttk.Progressbar(body, maximum=100)
        self.progress.pack(fill="x", pady=(10, 4))
        ttk.Label(body, textvariable=self.status, wraplength=1020).pack(anchor="w")
        ttk.Label(body, text=national.NOTICE, wraplength=1020, foreground="#9a3412").pack(anchor="w", pady=5)
        ttk.Button(body, text="打开计划与结果目录", command=self.open_results).pack(anchor="e")
        for var in (self.movie, self.start, self.end, self.cap, self.minutes, self.rate, self.retry_empty, self.refresh):
            var.trace_add("write", self.invalidate)
        self.poll_id = self.after(100, self.poll)

    @staticmethod
    def _write(widget, text, append=False):
        widget.configure(state="normal")
        if not append:
            widget.delete("1.0", "end")
        widget.insert("end", text + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def invalidate(self, *_):
        self.plan = None
        self.run_button.configure(state="disabled")
        if not self.busy:
            self.status.set("条件已改变，请重新生成离线执行计划。")

    def selection(self):
        value = self.movie.get().strip()
        code = self.selected[0] if self.selected and value == self.selected[1] else value
        return code, self.start.get().strip(), self.end.get().strip()

    def options(self):
        return dict(max_requests=int(self.cap.get()), minutes=float(self.minutes.get()), rate=float(self.rate.get()),
                    retry_empty=self.retry_empty.get(), refresh_roster=self.refresh.get())

    def preview(self):
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

    def run(self):
        if self.plan is None:
            self.status.set("请先生成并审查执行计划。")
            return
        approved = self.plan
        self.progress.configure(maximum=approved["options"]["max_requests"], value=0)
        self.begin("run", lambda: national.run_plan(
            approved, self.raw_dir, self.out_dir, stopped=self.stop_event.is_set,
            emit=lambda msg: self.events.put(("log", msg)),
            progress=lambda text, count: self.events.put(("progress", text, count))))

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
                conn = db.connect(str(self.db_path))
                conn.close()
                return history_roster.import_csv(self.db_path, path)
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
                elif event[0] == "log":
                    self._write(self.logs, event[1], True)
                elif event[0] == "progress":
                    self.status.set(f"{event[1]} · 本批已发 {event[2]:,} 次请求（不是全国完成率）")
                    self.progress.configure(value=event[2])
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
            self.plan = result
            self._write(self.plan_text, national.format_plan(result))
            self.table.delete(*self.table.get_children())
            for row in result["days"]:
                states = row["states"]
                self.table.insert("", "end", values=(row["date"], row["targets"], row["cached"], row["pending"],
                    states.get("empty_204", 0) + states.get("deferred_204", 0), row["roster_status"]))
            self.tabs.select(0)
            self.run_button.configure(state="normal")
            self.status.set(f"计划已保存 · {result['id'][:16]} · 审查后点击第二步启动。")
        elif mode == "search":
            self.results, self.selected = result, None
            self.choices.configure(values=[f"{name} [{code}]" for code, name in result])
            self.choices.set("")
            self.status.set(f"找到 {len(result)} 部影片，请在下拉框选择；也可直接输入数字编码。")
        elif mode == "run":
            self.refresh.set(False)
            self.progress.configure(value=result["requests"])
            self.status.set(f"{'本批有异常 · ' if result['failed'] else ''}{result['reason']} · 请求 {result['requests']:,} 次 · 核查 {result['saved']:,} 个影院日 · CSV 已导出。继续时请重新生成计划。")
            self._write(self.logs, self.status.get(), True)
            self.tabs.select(2)
        elif mode == "export":
            self.status.set("已导出影院日明细、逐日审计和完整性说明。全国完整性仍未证实。")
        else:
            self.status.set(f"导入 {result['records']:,} 条名单记录，覆盖 {result['days']} 天；请重新生成计划。")

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

    def close(self):
        if self.busy:
            self.stop()
        else:
            self.destroy()

    def destroy(self):
        if hasattr(self, "poll_id"):
            self.after_cancel(self.poll_id)
        super().destroy()

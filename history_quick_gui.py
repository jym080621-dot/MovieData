"""单片全国历史采集快捷窗口。"""
from __future__ import annotations

import datetime as dt
import os
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from zgdypf import movie_history

DEFAULT_BATCH = ("20", "10")
LONG_BATCH = ("3000", "60")


class QuickHistoryDialog(tk.Toplevel):
    def __init__(self, app, db_path, raw_dir, out_dir, defaults):
        super().__init__(app)
        self.app = app
        self.db_path, self.raw_dir, self.out_dir = db_path, raw_dir, Path(out_dir)
        self.title("单片全国历史")
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.busy = False
        self.results = []
        self.selected = None
        self.movie = tk.StringVar(self, defaults[0])
        self.start = tk.StringVar(self, defaults[1])
        self.end = tk.StringVar(self, defaults[2])
        self.cap = tk.StringVar(self, defaults[3])
        self.minutes = tk.StringVar(self, defaults[4] if len(defaults) > 4 else DEFAULT_BATCH[1])
        self.status = tk.StringVar(self, "填写影片和日期，可先查看当前覆盖。")
        self.summary = tk.StringVar(self, "")
        self.result_choice = tk.StringVar(self)

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="按日更新影院名单，补采逐影院票房并导出覆盖报告。").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 14))
        ttk.Label(body, text="影片名或编码").grid(row=2, column=0, sticky="w", padx=(0, 10))
        self.film_entry = ttk.Entry(body, textvariable=self.movie, width=36)
        self.film_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        self.search = ttk.Button(body, text="查找影片", command=self.lookup)
        self.search.grid(row=2, column=3, padx=(8, 0))
        self.choices = ttk.Combobox(body, textvariable=self.result_choice, state="readonly")
        self.choices.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(0, 5))
        self.choices.bind("<<ComboboxSelected>>", self.choose_film)
        self.choices.grid_remove()
        self.date_entries = []
        dates = ttk.Frame(body)
        dates.grid(row=4, column=0, columnspan=4, sticky="w", pady=5)
        for label, value in (("开始日期", self.start), ("结束日期", self.end)):
            ttk.Label(dates, text=label).pack(side="left", padx=(0, 8))
            entry = ttk.Entry(dates, textvariable=value, width=13)
            entry.pack(side="left", padx=(0, 16))
            self.date_entries.append(entry)
        ttk.Label(dates, text="YYYY-MM-DD").pack(side="left")
        batch = ttk.Frame(body)
        batch.grid(row=5, column=0, columnspan=4, sticky="w", pady=5)
        ttk.Label(batch, text="每批请求上限").pack(side="left", padx=(0, 8))
        self.limit = ttk.Entry(batch, textvariable=self.cap, width=6)
        self.limit.pack(side="left", padx=(0, 10))
        ttk.Label(batch, text="最多运行（分钟）").pack(side="left", padx=(0, 8))
        self.duration = ttk.Entry(batch, textvariable=self.minutes, width=6)
        self.duration.pack(side="left", padx=(0, 10))
        self.long_batch = ttk.Button(batch, text="长批次", command=self.set_long_batch)
        self.long_batch.pack(side="left")
        ttk.Label(body, text="每秒最多 1 次，名单、明细和重试共用请求额度。").grid(
            row=6, column=0, columnspan=4, sticky="w", pady=(0, 4))
        actions = ttk.Frame(body)
        actions.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(12, 8))
        self.run = ttk.Button(actions, text="开始 / 继续一批", style="Accent.TButton", command=lambda: self.launch("run"))
        self.run.pack(side="left", padx=(0, 8))
        self.preview = ttk.Button(actions, text="查看当前覆盖", command=lambda: self.launch("preview"))
        self.preview.pack(side="left", padx=(0, 8))
        self.stop = ttk.Button(actions, text="停止", command=self.stop_run, state="disabled")
        self.stop.pack(side="left")
        ttk.Separator(body).grid(row=8, column=0, columnspan=4, sticky="ew", pady=(7, 10))
        ttk.Label(body, textvariable=self.status, wraplength=570, justify="left").grid(
            row=9, column=0, columnspan=4, sticky="w", pady=(0, 5))
        ttk.Label(body, textvariable=self.summary, wraplength=570, justify="left").grid(
            row=10, column=0, columnspan=4, sticky="w")
        links = ttk.Frame(body)
        links.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.open_results = ttk.Button(links, text="打开结果目录", command=self.show_results,
                                       state="normal" if self.out_dir.is_dir() else "disabled")
        self.open_results.pack(side="right")
        self.advanced = ttk.Button(links, text="高级历史影院…", command=self.open_advanced)
        self.advanced.pack(side="left")
        self.run_controls = (self.film_entry, *self.date_entries, self.limit, self.duration,
                             self.long_batch, self.search, self.run, self.preview, self.advanced)
        self.film_entry.focus_set()

    def set_long_batch(self):
        self.cap.set(LONG_BATCH[0])
        self.minutes.set(LONG_BATCH[1])

    def choose_film(self, _event=None):
        index = self.choices.current()
        if index >= 0:
            self.selected = self.results[index]
            code, name = self.selected
            self.movie.set(name)
            self.status.set(f"已选 {name} · {code}")

    def lookup(self):
        query = self.movie.get().strip()
        if not query or self.busy:
            return
        self.begin("search", (query,))

    def _movie_code(self):
        value = self.movie.get().strip()
        if re.fullmatch(r"[0-9]{1,30}", value):
            return value
        if self.selected and value == self.selected[1]:
            return self.selected[0]
        raise ValueError("输入片码，或点「查找影片」后选择影片。")

    def launch(self, mode):
        if self.busy:
            return
        try:
            movie = self._movie_code()
            start, end = self.start.get().strip(), self.end.get().strip()
            movie_history.validate(movie, start, end, "roster")
            budget, minutes = map(int, DEFAULT_BATCH)
            if mode == "run":
                if dt.date.fromisoformat(end) >= dt.date.today():
                    raise ValueError("结束日期须早于今天。")
                try:
                    budget = int(self.cap.get())
                    if budget < 1:
                        raise ValueError
                except ValueError:
                    raise ValueError("每批请求上限须为正整数。") from None
                try:
                    minutes = int(self.minutes.get())
                    if minutes < 1:
                        raise ValueError
                except ValueError:
                    raise ValueError("运行时间须为正整数分钟。") from None
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.app.quick_defaults = (movie, start, end, self.cap.get(), self.minutes.get())
        self.begin(mode, (movie, start, end, budget, minutes))

    def begin(self, mode, args):
        if self.app._running:
            return
        self.busy = True
        self.app._running = True
        self.app._stop = threading.Event()
        self.summary.set("")
        self.status.set({"search": "正在查找影片…", "remote_search": "正在联网查找影片…",
                         "preview": "正在读取当前覆盖…", "run": "正在准备本批…"}[mode])
        for widget in (*self.run_controls, self.choices):
            widget.configure(state="disabled")
        self.stop.configure(state="normal" if mode == "run" else "disabled")
        threading.Thread(target=self._work, args=(mode, args, self.app._stop), daemon=True).start()

    def _work(self, mode, args, stop):
        try:
            if mode == "search":
                result = movie_history.find_films(self.db_path, args[0])
                result = (args[0], result)
            elif mode == "remote_search":
                result = (args[0], movie_history.search_films(self.raw_dir, args[0], stopped=stop.is_set))
            elif mode == "preview":
                movie, start, end, _, _ = args
                if Path(self.db_path).is_file():
                    result = movie_history.plan_totals(self.db_path, movie, start, end, "roster")
                else:
                    days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
                    result = {"pending_roster_days": days, "uncertain_roster_days": days}
            else:
                movie, start, end, budget, minutes = args
                result = movie_history.run_quick_batch(
                    self.db_path, self.raw_dir, self.out_dir, movie, start, end, max_requests=budget,
                    minutes=minutes,
                    stopped=stop.is_set, progress=lambda msg: self.app.q.put(("quick_status", self, msg)),
                    on_counts=lambda day, requests, saved, empty: self.app.q.put(
                        ("quick_counts", self, day, requests, budget, saved, empty)),
                    emit=self.app._emit,
                )
            self.app.q.put(("quick_result", self, mode, result, None))
        except Exception as exc:
            self.app.q.put(("quick_result", self, mode, None, str(exc)))

    def finish(self, mode, result, error):
        self.busy = False
        self.app._reset()
        for widget in self.run_controls:
            widget.configure(state="normal")
        self.choices.configure(state="readonly")
        self.stop.configure(state="disabled")
        if error:
            stage = {"search": "查找本地影片", "remote_search": "联网查找影片",
                     "preview": "读取当前覆盖", "run": "采集或导出"}[mode]
            self.status.set(f"{stage}失败 · {error}")
            self.app._log(f"{stage}失败 · {error}", "err")
            return
        if mode in ("search", "remote_search"):
            query, self.results = result
            self.selected = None
            self.choices.configure(values=[f"{name}  [{code}]" for code, name in self.results])
            if self.results:
                if len(self.results) == 1:
                    self.choices.grid_remove()
                    self.choices.current(0)
                    self.choose_film()
                else:
                    self.choices.grid()
                    self.result_choice.set("")
                    self.status.set(f"找到 {len(self.results)} 部影片，请从下拉列表选择。")
            else:
                self.choices.grid_remove()
                self.status.set("未找到影片。可直接填片码。")
                if mode == "search" and messagebox.askyesno(
                        "联网查找影片", "本地没有匹配影片。现在向数据站点查询一次？", parent=self):
                    self.begin("remote_search", (query,))
            return
        if mode == "run":
            if result["failed"]:
                state = "本批有请求失败，已导出当前结果"
            elif result["reason"] == "用户停止":
                state = "已停止并导出当前结果"
            elif result["reason"]:
                state = f"{result['reason']}，已导出当前结果"
            elif not result["requests"]:
                state = "当前没有可自动补采的影院日，已导出"
            else:
                state = "本批导出完成"
            self.status.set(f"{state} · 请求 {result['requests']:,} 次 · 本批核查 {result['saved']:,} 个影院日\n"
                            f"累计导出该片明细 {result['reports']['detail_rows']:,} 行")
            self.open_results.configure(state="normal")
            plan = result["plan"]
        else:
            self.status.set("当前覆盖 · 离线预览")
            plan = result
        self.summary.set(
            f"已知影院日 {plan.get('targets', 0):,} · 已核查 {plan.get('observed', 0):,} · "
            f"待补 {plan.get('plannable', 0):,} · 204 待复核 {plan.get('deferred', 0):,}\n"
            f"待更新名单 {plan.get('pending_roster_days', 0):,} 天 · "
            f"榜单证据不足 {plan.get('uncertain_roster_days', 0):,} 天\n全国名单覆盖待核验"
        )

    def show_counts(self, day, requests, budget, saved, empty):
        prefix = f"{day} · " if day else ""
        self.summary.set(f"{prefix}本批请求 {requests:,} / {budget:,}\n"
                         f"本批已核查 {saved:,} 个影院日 · 204 待复核 {empty:,}")

    def stop_run(self):
        self.app._stop.set()
        self.stop.configure(state="disabled")
        self.status.set("正在停止，已取得的结果会导出。")

    def show_results(self):
        try:
            os.startfile(self.out_dir)
        except OSError as exc:
            self.status.set(f"打开结果目录失败 · {exc}")

    def open_advanced(self):
        film = self.selected[0] if self.selected and self.movie.get() == self.selected[1] else self.movie.get()
        defaults = (film, self.start.get(), self.end.get(), self.cap.get())
        self.close()
        self.app._history_dialog(defaults)

    def close(self):
        if self.busy:
            self.stop_run()
        else:
            film = self.selected[0] if self.selected and self.movie.get() == self.selected[1] else self.movie.get()
            self.app.quick_defaults = (film, self.start.get(), self.end.get(), self.cap.get(), self.minutes.get())
            self.destroy()

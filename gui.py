#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui.py —— 中国电影票房采集器（Tkinter 桌面界面）。

纯标准库（tkinter），无第三方依赖。功能：
  * 24 个数据集自由勾选；
  * 日期区间 + 快捷预设（昨天/近7天/近30天/本月/上月）；
  * 限速、样本规模、Top N、周期粒度、搜索词等参数；
  * 采集前「估算」请求数与耗时；
  * 后台线程采集，界面实时进度 + 日志，可随时停止，支持断点续跑。
"""
from __future__ import annotations

import datetime as dt
import os
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zgdypf import __version__, app_root, datasets, db, movie_history
from zgdypf.client import Client
from national_history_gui import NationalHistoryPage

ROOT = app_root()
DB_PATH = os.path.join(ROOT, "boxoffice.db")
RAW_DIR = os.path.join(ROOT, "raw")

BG = "#eef1f5"
CARD = "#ffffff"
ACCENT = "#2563eb"
GREY = "#6b7280"
DARK = "#1f2937"
FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 9, "bold")


def _yesterday() -> str:
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


class App(tk.Tk):
    def __init__(self, selftest: bool = False):
        super().__init__()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.selftest = selftest
        self.title(f"中国电影票房采集器 · MovieData v{__version__}")
        self.geometry("1260x905+60+20")
        self.minsize(1140, 720)
        self.configure(bg=BG)
        self.q: queue.Queue = queue.Queue()
        self._engine = None
        self._stop = threading.Event()
        self._running = False
        self._t0 = 0.0
        self._total = 0

        self._init_style()
        self._build()
        self._poll()

    # ------------------------------------------------------------ 样式
    def _init_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        self.option_add("*Font", FONT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, font=FONT, foreground=DARK)
        st.configure("Card.TLabel", background=CARD, font=FONT, foreground=DARK)
        st.configure("H1.TLabel", background=BG, font=("Microsoft YaHei UI", 15, "bold"),
                     foreground="#0f172a")
        st.configure("Sub.TLabel", background=BG, font=("Microsoft YaHei UI", 9), foreground=GREY)
        st.configure("Grp.TLabel", background=CARD, font=FONT_B, foreground="#334155")
        st.configure("Hint.TLabel", background=CARD, font=("Microsoft YaHei UI", 8), foreground=GREY)
        st.configure("TButton", font=FONT, padding=(10, 4))
        st.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                     foreground="white", background=ACCENT, padding=(16, 7), borderwidth=0)
        st.map("Accent.TButton", background=[("active", "#1d4ed8"), ("disabled", "#9ca3af")])
        st.configure("Stop.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                     foreground="white", background="#dc2626", padding=(16, 7), borderwidth=0)
        st.map("Stop.TButton", background=[("active", "#b91c1c"), ("disabled", "#9ca3af")])
        st.configure("TEntry", padding=3)
        st.configure("TSpinbox", padding=3)
        st.configure("TCombobox", padding=3)
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#dde3ea",
                     borderwidth=0, thickness=10)

    # ------------------------------------------------------------ 布局
    def _build(self):
        self._build_status()          # 先占住底部，否则会被 expand 的主体挤掉
        head = ttk.Frame(self, padding=(18, 14, 18, 6))
        head.pack(fill="x")
        ttk.Label(head, text="采集控制台", style="H1.TLabel").pack(side="left")
        ttk.Label(head, text="   数据源 zgdypf.zgdypw.cn（专资办口径）· 纯标准库 · 断点续跑",
                  style="Sub.TLabel").pack(side="left", padx=(4, 0))
        database = os.path.basename(DB_PATH) if os.path.isfile(DB_PATH) else "尚未载入历史库"
        ttk.Label(head, text=f"库：{database}", style="Sub.TLabel").pack(side="right")
        national_bar = ttk.Frame(self, padding=(18, 0, 18, 5))
        national_bar.pack(fill="x")
        self.btn_national = ttk.Button(national_bar, text="全国全影院历史票房采集 →", command=self._national_history_page)
        self.btn_national.pack(side="left")
        ttk.Label(national_bar, text=f"  v{__version__} · 离线计划 / 分批续跑 / 逐日审计", style="Sub.TLabel").pack(side="left")

        body = ttk.Frame(self, padding=(18, 4, 18, 6))
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, style="Card.TFrame", padding=10)
        left.pack(side="left", fill="y", padx=(0, 10))
        self._build_dataset_panel(left)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        self._build_params(right)
        self._build_actions(right)
        self._build_log(right)

    def _build_dataset_panel(self, parent):
        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="数据集（24）", style="Grp.TLabel").pack(side="left")
        ttk.Button(bar, text="推荐", width=5, command=lambda: self._preset_ds("default")).pack(side="right")
        ttk.Button(bar, text="清空", width=5, command=lambda: self._preset_ds("none")).pack(side="right", padx=4)
        ttk.Button(bar, text="全选", width=5, command=lambda: self._preset_ds("all")).pack(side="right")

        self.lbl_sel = ttk.Label(parent, text="已选 6 / 24", style="Hint.TLabel")
        self.lbl_sel.pack(fill="x", pady=(0, 6))

        wrap = ttk.Frame(parent, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=CARD, highlightthickness=0, width=360,
                                yscrollincrement=24)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=CARD)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def wheel(e):
            try:
                w = self.canvas.winfo_containing(e.x_root, e.y_root)
            except Exception:
                return
            if w is not None and (str(w).startswith(str(self.canvas))
                                  or str(w).startswith(str(self.inner))):
                self.canvas.yview_scroll(int(-e.delta / 120), "units")
        self.bind_all("<MouseWheel>", wheel)

        self.ds_vars = {}
        idx = 0
        for g in datasets.GROUPS:
            items = [s for s in datasets.SPECS if s.group == g]
            if not items:
                continue
            tk.Label(self.inner, text=g, bg=CARD, fg="#94a3b8",
                     font=("Microsoft YaHei UI", 8, "bold"), anchor="w").pack(
                fill="x", pady=(10, 2), padx=(2, 0))
            for s in items:
                idx += 1
                v = tk.BooleanVar(value=s.default)
                self.ds_vars[s.id] = v
                cb = tk.Checkbutton(self.inner, text=f"{idx:02d}  {s.name}", variable=v,
                                    bg=CARD, activebackground=CARD, anchor="w",
                                    font=FONT, fg=DARK, selectcolor="#ffffff",
                                    highlightthickness=0, bd=0, padx=2,
                                    command=self._update_sel)
                cb.pack(fill="x", padx=(8, 0))
                cb.bind("<Enter>", lambda e, d=s.desc, n=s.name: self.hint_var.set(f"● {n}\n{d}"))

        self.hint_var = tk.StringVar(value="● 提示\n鼠标悬停任一数据集可查看说明与代价")
        tk.Label(parent, textvariable=self.hint_var, bg=CARD, fg="#64748b",
                 font=("Microsoft YaHei UI", 8), justify="left", anchor="nw",
                 wraplength=346, height=4).pack(fill="x", pady=(8, 0))
        self._update_sel()

    def _build_params(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=14)
        card.pack(fill="x")
        card.columnconfigure(3, weight=1)
        ttk.Label(card, text="采集参数", style="Grp.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        self.v_start = tk.StringVar(value=_yesterday())
        self.v_end = tk.StringVar(value=_yesterday())
        self.v_rate = tk.StringVar(value="2.0")
        self.v_sample = tk.StringVar(value="150")
        self.v_movies = tk.StringVar(value="10")
        self.v_chains = tk.StringVar(value="10")
        self.v_period = tk.StringVar(value="周")
        self.v_query = tk.StringVar(value="")
        self.v_film = tk.StringVar(value="")
        self.v_cinema = tk.StringVar(value="")
        self.v_all_cinema = tk.BooleanVar(value=False)
        self.v_budget = tk.StringVar(value="0")
        self.v_tables = tk.BooleanVar(value=True)
        self.v_correct = tk.BooleanVar(value=False)
        self.v_skip = tk.BooleanVar(value=True)

        ttk.Label(card, text="开始", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Entry(card, textvariable=self.v_start, width=13).grid(row=1, column=1, padx=(6, 20), sticky="w")
        ttk.Label(card, text="结束", style="Card.TLabel").grid(row=1, column=2, sticky="w")
        ttk.Entry(card, textvariable=self.v_end, width=13).grid(row=1, column=3, padx=(6, 0), sticky="w")

        pre = ttk.Frame(card, style="Card.TFrame")
        pre.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(pre, text="快捷", style="Card.TLabel").pack(side="left", padx=(0, 6))
        for txt, fn in (("昨天", self._preset_yesterday), ("近7天", self._preset_7),
                        ("近30天", self._preset_30), ("本月", self._preset_month),
                        ("上月", self._preset_lastmonth)):
            ttk.Button(pre, text=txt, width=7, command=fn).pack(side="left", padx=2)

        ttk.Separator(card).grid(row=3, column=0, columnspan=4, sticky="ew", pady=10)

        ttk.Label(card, text="限速", style="Card.TLabel").grid(row=4, column=0, sticky="w")
        box = ttk.Frame(card, style="Card.TFrame")
        box.grid(row=4, column=1, sticky="w", padx=(6, 20))
        ttk.Spinbox(box, from_=0.5, to=5.0, increment=0.5, width=6,
                    textvariable=self.v_rate).pack(side="left")
        ttk.Label(box, text=" req/s", style="Hint.TLabel").pack(side="left")

        ttk.Label(card, text="样本影院", style="Card.TLabel").grid(row=4, column=2, sticky="w")
        ttk.Spinbox(card, from_=10, to=500, increment=10, width=8,
                    textvariable=self.v_sample).grid(row=4, column=3, sticky="w", padx=(6, 0))

        ttk.Label(card, text="Top 影片", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(card, from_=1, to=50, width=8,
                    textvariable=self.v_movies).grid(row=5, column=1, sticky="w", padx=(6, 20), pady=(8, 0))
        ttk.Label(card, text="Top 院线", style="Card.TLabel").grid(row=5, column=2, sticky="w", pady=(8, 0))
        ttk.Spinbox(card, from_=1, to=50, width=8,
                    textvariable=self.v_chains).grid(row=5, column=3, sticky="w", padx=(6, 0), pady=(8, 0))

        ttk.Label(card, text="周期粒度", style="Card.TLabel").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(card, textvariable=self.v_period, values=["周", "月", "年"], width=6,
                     state="readonly").grid(row=6, column=1, sticky="w", padx=(6, 20), pady=(8, 0))
        ttk.Label(card, text="搜索词", style="Card.TLabel").grid(row=6, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(card, textvariable=self.v_query, width=15).grid(row=6, column=3, sticky="w",
                                                                 padx=(6, 0), pady=(8, 0))

        ttk.Label(card, text="指定影片", style="Card.TLabel").grid(row=7, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(card, textvariable=self.v_film, width=42).grid(
            row=7, column=1, columnspan=3, sticky="w", padx=(6, 0), pady=(8, 0))
        ttk.Label(card, text="↑ 片名或 8~12 位编码，逗号分隔；留空则取结束日的 Top N 影片",
                  style="Hint.TLabel").grid(row=8, column=1, columnspan=3, sticky="w", padx=(6, 0))

        ttk.Label(card, text="指定影院", style="Card.TLabel").grid(row=9, column=0, sticky="w", pady=(8, 0))
        self.ent_cinema = ttk.Entry(card, textvariable=self.v_cinema, width=42)
        self.ent_cinema.grid(row=9, column=1, columnspan=3, sticky="w", padx=(6, 0), pady=(8, 0))
        ttk.Label(card, text="↑ 影院名或 8 位编码；留空=150 家样本，填 * = 全部约 1.2 万家",
                  style="Hint.TLabel").grid(row=10, column=1, columnspan=3, sticky="w", padx=(6, 0))

        opt = ttk.Frame(card, style="Card.TFrame")
        opt.grid(row=11, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Checkbutton(opt, text="校正行政区码（每店多 1 次请求）",
                        variable=self.v_correct).pack(side="left")
        ttk.Checkbutton(opt, text="跳过已完成", variable=self.v_skip).pack(side="left", padx=(18, 0))
        ttk.Checkbutton(opt, text="全部影院（约 1.2 万家）", variable=self.v_all_cinema,
                        command=self._toggle_all).pack(side="left", padx=(18, 0))

        opt2 = ttk.Frame(card, style="Card.TFrame")
        opt2.grid(row=12, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(opt2, text="时间预算", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(opt2, from_=0, to=1440, increment=30, width=6,
                    textvariable=self.v_budget).pack(side="left", padx=(6, 4))
        ttk.Label(opt2, text="分钟（0=不限；到点自动停，下次续跑）",
                  style="Hint.TLabel").pack(side="left")
        ttk.Checkbutton(opt2, text="导出含整表", variable=self.v_tables).pack(side="left", padx=(18, 0))

    def _build_actions(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        card.pack(fill="x", pady=(10, 0))

        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        self.btn_start = ttk.Button(row, text="开始采集", style="Accent.TButton", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row, text="停止", style="Stop.TButton",
                                   command=self._stop_click, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.btn_est = ttk.Button(row, text="估算", width=8, command=self._estimate)
        self.btn_est.pack(side="left", padx=(4, 8))
        ttk.Button(row, text="打开目录", width=10, command=self._open_dir).pack(side="left")
        self.btn_export = ttk.Button(row, text="导出结果", width=10, command=self._export)
        self.btn_export.pack(side="left", padx=(8, 0))
        bar = ttk.Frame(card, style="Card.TFrame")
        bar.pack(fill="x", pady=(10, 0))
        self.pb = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True)
        self.lbl_prog = ttk.Label(bar, text="就绪", style="Card.TLabel", width=20, anchor="e")
        self.lbl_prog.pack(side="right", padx=(10, 0))

    def _build_log(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(10, 8, 10, 10))
        card.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(card, text="运行日志", style="Grp.TLabel").pack(anchor="w", pady=(0, 4))
        wrap = ttk.Frame(card, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        self.txt = tk.Text(wrap, height=12, wrap="none", bg="#0f172a", fg="#e2e8f0",
                           insertbackground="#e2e8f0", relief="flat",
                           font=("Consolas", 9), padx=10, pady=8)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("info", "#cbd5e1"), ("head", "#60a5fa"), ("ok", "#4ade80"),
                           ("warn", "#fbbf24"), ("err", "#f87171")):
            self.txt.tag_configure(tag, foreground=color)
        self.txt.configure(state="disabled")

    def _build_status(self):
        bar = tk.Frame(self, bg="#dbe2ea", height=26)
        bar.pack(fill="x", side="bottom")
        self.lbl_status = tk.Label(bar, text="就绪 · 勾选数据集后点「开始采集」", bg="#dbe2ea",
                                   fg="#334155", anchor="w", padx=14, font=FONT)
        self.lbl_status.pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------ 事件
    def _preset_ds(self, how: str):
        for s in datasets.SPECS:
            self.ds_vars[s.id].set(how == "all" or (how == "default" and s.default))
        self._update_sel()

    def _update_sel(self):
        n = sum(1 for v in self.ds_vars.values() if v.get())
        self.lbl_sel.configure(text=f"已选 {n} / 24")

    def _preset_yesterday(self):
        d = _yesterday()
        self.v_start.set(d)
        self.v_end.set(d)

    def _preset_7(self):
        self.v_end.set(_yesterday())
        self.v_start.set((dt.date.today() - dt.timedelta(days=7)).isoformat())

    def _preset_30(self):
        self.v_end.set(_yesterday())
        self.v_start.set((dt.date.today() - dt.timedelta(days=30)).isoformat())

    def _preset_month(self):
        t = dt.date.today()
        self.v_start.set(t.replace(day=1).isoformat())
        self.v_end.set(_yesterday())

    def _preset_lastmonth(self):
        t = dt.date.today().replace(day=1)
        last = t - dt.timedelta(days=1)
        self.v_start.set(last.replace(day=1).isoformat())
        self.v_end.set(last.isoformat())

    def _open_dir(self):
        try:
            os.startfile(ROOT)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def _toggle_all(self):
        on = self.v_all_cinema.get()
        self.ent_cinema.configure(state="disabled" if on else "normal")
        if on:
            self.v_cinema.set("")

    # ------------------------------------------------------------ 导出
    def _export(self):
        if self._running:
            messagebox.showinfo("提示", "采集进行中，结束或停止后再导出")
            return
        self.btn_export.configure(state="disabled")
        self._log("─" * 62, "head")
        self._log(f"开始导出到 output/ …（整表：{'含' if self.v_tables.get() else '不含'}）", "head")
        threading.Thread(target=self._export_worker, args=(bool(self.v_tables.get()),),
                         daemon=True).start()

    def _export_worker(self, with_tables: bool):
        try:
            from zgdypf import export
            res = export.export_all(
                out_root=os.path.join(ROOT, "output"),
                db_path=DB_PATH,
                raw_dir=RAW_DIR,
                with_tables=with_tables,
                log=lambda m: self.q.put(("log", m, "info")),
            )
            self.q.put(("exported", res))
        except Exception as exc:
            import traceback
            self.q.put(("export_err", f"{type(exc).__name__}: {exc}",
                        traceback.format_exc()[-800:]))

    def _selected(self):
        return [s.id for s in datasets.SPECS if self.ds_vars[s.id].get()]

    def _opts(self):
        try:
            d0 = dt.date.fromisoformat(self.v_start.get().strip())
            d1 = dt.date.fromisoformat(self.v_end.get().strip())
        except ValueError:
            messagebox.showerror("日期格式错误", "请使用 YYYY-MM-DD，例如 2026-04-01")
            return None
        if d1 < d0:
            d0, d1 = d1, d0
            self.v_start.set(d0.isoformat())
            self.v_end.set(d1.isoformat())
        try:
            rate = max(0.5, float(self.v_rate.get()))
            n = max(1, int(float(self.v_sample.get())))
            tm = max(1, int(float(self.v_movies.get() or 10)))
            tc = max(1, int(float(self.v_chains.get() or 10)))
            budget = max(0.0, float(self.v_budget.get() or 0))
        except ValueError:
            messagebox.showerror("参数错误", "限速 / 样本数 / TopN / 时间预算 必须是数字")
            return None
        return {
            "start": d0.isoformat(), "end": d1.isoformat(), "rate": rate, "n_sample": n,
            "top_movies": tm, "top_chains": tc,
            "period_type": {"周": 1, "月": 2, "年": 3}[self.v_period.get()],
            "query": self.v_query.get().strip(),
            "movie_query": self.v_film.get().strip(),
            "cinema_query": self.v_cinema.get().strip(),
            "all_cinemas": bool(self.v_all_cinema.get()),
            "budget_min": budget,
            "export_tables": bool(self.v_tables.get()),
            "correct_codes": bool(self.v_correct.get()),
            "skip_done": bool(self.v_skip.get()),
        }

    def _sample_count(self) -> int:
        try:
            conn = db.connect(DB_PATH)
            n = conn.execute("SELECT COUNT(*) FROM dim_cinema WHERE sample=1").fetchone()[0]
            conn.close()
            return n
        except Exception:
            return 0

    def _estimate(self):
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "请至少勾选一个数据集")
            return
        opts = self._opts()
        if not opts:
            return
        n, detail = datasets.estimate(sel, opts, self._sample_count())
        eff = max(opts["rate"] * 0.6, 0.5)          # 实测吞吐约为设定值的 0.6 倍
        mins = n / eff / 60
        self._log("─" * 62, "head")
        self._log(f"估算：{n:,} 次请求 ≈ {mins:.0f} 分钟（{mins/60:.1f} 小时）"
                  f"　按约 {eff:.1f} 次/秒计（设定 {opts['rate']}，实测约为其 0.6 倍）", "head")
        for d in detail:
            self._log("    " + d)
        self.lbl_prog.configure(text=f"预计 {n:,} 次 · {mins:.0f} 分钟")
        if n > 20000:
            self._log("提示：请求量过大，建议先用「近7天」，或分批运行（支持断点续跑）。", "warn")
        if n == 0:
            self._log("提示：所选数据集未产生任务，请检查日期区间。", "warn")

    def _start(self):
        if self._running:
            return
        sel = self._selected()
        if not sel:
            messagebox.showinfo("提示", "请至少勾选一个数据集")
            return
        opts = self._opts()
        if not opts:
            return
        n, _detail = datasets.estimate(sel, opts, self._sample_count())
        if n > 20000 and not self.selftest:
            eff = max(opts["rate"] * 0.6, 0.5)
            mins = n / eff / 60
            if not messagebox.askyesno(
                    "确认大批量",
                    f"预计 {n:,} 次请求，约 {mins/60:.1f} 小时。\n\n"
                    f"建议先设「时间预算」分批跑（到点自动停，下次从缺口继续）。\n\n"
                    f"仍要现在开始吗？"):
                return
        self._running = True
        self._stop = threading.Event()
        self._t0 = time.time()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_est.configure(state="disabled")
        self.pb.configure(value=0)
        self.lbl_prog.configure(text="启动中…")
        self.lbl_status.configure(text="采集中…")
        self._log("═" * 62, "head")
        self._log(f"开始 · {len(sel)} 个数据集 · {opts['start']} ~ {opts['end']} · "
                  f"{opts['rate']} req/s", "head")
        threading.Thread(target=self._worker, args=(sel, opts), daemon=True).start()

    def _national_history_page(self):
        if self._running:
            return
        page = getattr(self, "national_page", None)
        if page is not None and page.winfo_exists():
            page.lift()
            return
        self.national_page = NationalHistoryPage(self, DB_PATH, RAW_DIR,
            os.path.join(ROOT, "output", "national_history"), self.v_start.get(), self.v_end.get())

    def _check_data_folder(self):
        """将移动后无法打开数据库的错误显示在窗口中。"""
        if not os.path.isfile(DB_PATH):
            self.lbl_status.configure(text="新目录 · 将旧 boxoffice.db 复制到程序同一目录可继续采集")
            return
        try:
            with movie_history.open_readonly(DB_PATH) as conn:
                conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        except (sqlite3.Error, OSError) as exc:
            self.lbl_status.configure(text="无法读取数据库")
            messagebox.showerror("无法读取数据库", f"{DB_PATH}\n\n{exc}", parent=self)

    def _stop_click(self):
        self._stop.set()
        self._log("正在停止（当前请求完成后退出）…", "warn")
        self.btn_stop.configure(state="disabled")

    # ------------------------------------------------------------ 后台
    def _emit(self, msg: str):
        level = "info"
        if msg.startswith("▶") or msg.startswith("═"):
            level = "head"
        elif "⚠" in msg or "⏹" in msg:
            level = "warn"
        elif "失败" in msg or "异常" in msg or "错误" in msg:
            level = "err"
        self.q.put(("log", msg, level))

    def _worker(self, selected, opts):
        try:
            conn = db.connect(DB_PATH)
            client = Client(rate=opts["rate"], raw_dir=RAW_DIR)
            ctx = datasets.Ctx(conn, client, opts, self._emit)
            eng = datasets.Engine(ctx)
            eng.stop = self._stop
            self._engine = eng
            plan = eng.plan(selected)
            total = sum(len(t) for _, t in plan)
            self.q.put(("total", total))
            stats = eng.run(plan, lambda d, t: self.q.put(("progress", d, t)),
                            skip_done=opts["skip_done"],
                            budget_min=opts.get("budget_min", 0) or 0)
            self.q.put(("done", stats, client.stats.as_dict(), time.time() - self._t0))
        except Exception as exc:
            import traceback
            self.q.put(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()[-1500:]))

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "log":
                    self._log(m[1], m[2])
                elif k == "total":
                    self._total = m[1]
                    self.pb.configure(maximum=max(m[1], 1))
                    self.lbl_prog.configure(text=f"0 / {m[1]:,}")
                elif k == "progress":
                    done, total = m[1], m[2]
                    self.pb.configure(value=done)
                    pct = done / total * 100 if total else 0
                    self.lbl_prog.configure(text=f"{done:,} / {total:,}  {pct:.0f}%")
                elif k == "done":
                    self._finish(m[1], m[2], m[3])
                elif k == "exported":
                    self.btn_export.configure(state="normal")
                    self._log(f"导出完成：{m[1]['movies']} 部影片 · {m[1].get('cinemas', 0)} 家影院 · "
                              f"{m[1]['size_mb']} MB", "ok")
                    self._log(f"目录：{m[1]['root']}", "ok")
                    self.lbl_status.configure(text=f"已导出到 {m[1]['root']}")
                elif k == "export_err":
                    self.btn_export.configure(state="normal")
                    self._log("✗ 导出失败：" + m[1], "err")
                    self._log(m[2], "err")
                elif k == "error":
                    self._log("✗ " + m[1], "err")
                    self._log(m[2], "err")
                    self._reset()
                    if self.selftest:
                        self.after(200, self.destroy)
        except queue.Empty:
            pass
        self.poll_id = self.after(120, self._poll)

    def close(self):
        page = getattr(self, "national_page", None)
        if page is not None and page.winfo_exists() and page.busy:
            page.close(after=self.destroy)
        else:
            self.destroy()

    def destroy(self):
        if hasattr(self, "poll_id"):
            self.after_cancel(self.poll_id)
        super().destroy()

    def _finish(self, stats, net, elapsed):
        if stats.get("budget"):
            self._log("⏸ 时间预算用尽，已停止；再点「开始采集」会从缺口继续", "warn")
        self._log(f"完成 · 成功 {stats.get('ok',0):,} · 空 {stats.get('empty',0):,} · "
                  f"失败 {stats.get('err',0):,} · 跳过 {stats.get('skip',0):,}", "ok")
        self._log(f"网络 {net.get('requests',0):,} 次 · 重试 {net.get('retries',0)} · "
                  f"用时 {elapsed/60:.1f} 分钟", "ok")
        self.pb.configure(value=self.pb.cget("maximum"))
        self.lbl_prog.configure(text=f"完成 · 成功 {stats.get('ok',0):,}")
        self.lbl_status.configure(
            text=f"完成 · 请求 {net.get('requests',0):,} 次 · 失败 {stats.get('err',0):,} · "
                 f"用时 {elapsed/60:.1f} 分钟")
        self._reset()
        print(f"[selftest] stats={stats} net={net} elapsed={elapsed:.1f}s", flush=True)
        if self.selftest:
            self.after(300, self.destroy)

    def _reset(self):
        self._running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.btn_est.configure(state="normal")

    def _log(self, msg, level="info"):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.txt.configure(state="normal")
        self.txt.insert("end", f"{ts}  {msg}\n", level)
        self.txt.see("end")
        self.txt.configure(state="disabled")


def main() -> int:
    selftest = "--selftest" in sys.argv
    demo = "--demo" in sys.argv
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = App(selftest=selftest)
    if not (selftest or demo):
        app.after(150, app._check_data_folder)
    if "--national-history" in sys.argv or "--quick-history" in sys.argv:
        app.after(100, app._national_history_page)
    if selftest:
        app.withdraw()
        app.v_start.set(_yesterday())
        app.v_end.set(_yesterday())
        app.v_skip.set(False)
        app._preset_ds("none")
        app.ds_vars["date_range"].set(True)
        app.ds_vars["national"].set(True)
        app._update_sel()
        app.after(300, app._start)
        app.after(90000, app.destroy)
    elif demo:
        app.v_start.set((dt.date.today() - dt.timedelta(days=5)).isoformat())
        app.v_end.set(_yesterday())
        app.v_skip.set(False)          # 演示模式强制重抓，否则会秒过、截不到进度
        app._preset_ds("none")
        for k in ("date_range", "national", "day_data", "province_list", "chain_list"):
            app.ds_vars[k].set(True)
        app._update_sel()
        app.after(600, app._start)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa.py —— 采集后的质量校验与报告。

五项必做校验（对应方案第六节）：
  Q1 逐影院逐日：sum(movieSalesList.sales) 与 getCinemaSumData.sales 比对（容差 1%）
  Q2 movieCount 与 movieSalesList 长度相等
  Q3 抽样影院当日 getCinemaSumData.sales 与当天影院榜 sales 一致
  Q4 样本合计占全国合计的比例在一个月内应稳定
  Q5 手工挑 2~3 家，与网页展示对齐

另加三项本方案自己发现的检查：
  Q6 口径判定：影院榜/分片的合计到底落在「含服务费」还是「分账」
  Q7 单位体检：票价、单座产出是否落在合理区间（防单位错位）
  Q8 覆盖度：逐日可采集率、204 率、可疑日
"""
from __future__ import annotations

import os
import random
import statistics
import sys
from typing import List, Optional

from zgdypf import api, db
from zgdypf.client import Client, HardError, RetryExhausted
from zgdypf.parse import parse_count, parse_money, unit_money


def _fmt(x, nd=2):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:,.{nd}f}"
    return f"{x:,}"


def run(db_path: str, raw_dir: str, start: Optional[str], end: Optional[str],
        md_path: Optional[str] = None) -> int:
    conn = db.connect(db_path)
    client = Client(rate=2.0, raw_dir=raw_dir)
    lines: List[str] = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("# 中国电影票房（专资办口径）采集质量报告")
    out()
    out(f"生成时间：{db.now()}　数据库：`{os.path.basename(db_path)}`")
    out()

    # ---------------------------------------------------------------- 1 规模
    out("## 1. 数据规模")
    out()
    tabs = [
        ("fact_cinema_day", "影院×日（全量榜）"),
        ("fact_cinema_movie", "影院×影片×日（样本分片）"),
        ("fact_province", "省份×日"),
        ("fact_chain", "院线×日"),
        ("fact_national", "全国×日"),
        ("dim_cinema", "影院维表"),
    ]
    out("| 表 | 含义 | 行数 | 覆盖天数 |")
    out("|---|---|---:|---:|")
    for t, desc in tabs:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if t == "fact_cinema_day":
            nd = conn.execute("SELECT COUNT(DISTINCT date) FROM fact_cinema_day").fetchone()[0]
        elif t == "fact_cinema_movie":
            nd = conn.execute("SELECT COUNT(DISTINCT date) FROM fact_cinema_movie").fetchone()[0]
        elif t == "dim_cinema":
            nd = "-"
        else:
            nd = conn.execute(f"SELECT COUNT(DISTINCT date) FROM {t}").fetchone()[0]
        out(f"| `{t}` | {desc} | {n:,} | {nd} |")
    out()

    # ---------------------------------------------------------------- 2 覆盖度
    out("## 2. 覆盖度与缺口（Q8）")
    out()
    rows = conn.execute(
        "SELECT date, COUNT(*) n FROM fact_cinema_day WHERE sales_src='ranklist' "
        "GROUP BY date ORDER BY date").fetchall()
    out(f"- 全量影院榜覆盖 **{len(rows)}** 天。")
    if rows:
        counts = [r[1] for r in rows]
        out(f"- 每日影院数：中位 {int(statistics.median(counts)):,}，区间 "
            f"{min(counts):,} ~ {max(counts):,}。")
    led = conn.execute(
        "SELECT status, COUNT(*) FROM collect_ledger WHERE job='cinema_box' GROUP BY status"
    ).fetchall()
    if led:
        d = dict(led)
        total = sum(d.values())
        out(f"- 分片任务台账：共 {total:,} 个 key，"
            + "，".join(f"{k}={v:,}" for k, v in sorted(d.items())))
        empty_rate = d.get("empty", 0) / max(total, 1)
        out(f"- 204（无数据）占比 **{empty_rate:.2%}**。")
    thin = conn.execute("SELECT date FROM date_status WHERE status IN ('thin','error') "
                        "ORDER BY date").fetchall()
    if thin:
        out(f"- 站点侧不完整日期 **{len(thin)}** 天：{', '.join(r[0] for r in thin)}")
        out("  > 这些日期影院维度整片塌缩（全国/省份仍正常），已按日跳过，未写入残缺数据。")
    out()

    # ---------------------------------------------------------------- 3 自洽
    out("## 3. 片目自洽（Q2）")
    out()
    rows = conn.execute("""
        SELECT m.cinema_code, m.date, SUM(m.sales_fen), COUNT(*),
               d.sales_fen, d.movie_count
        FROM fact_cinema_movie m
        JOIN fact_cinema_day d ON d.cinema_code=m.cinema_code AND d.date=m.date
        GROUP BY 1,2
    """).fetchall()
    bad_cnt = bad_sum = 0
    for code, date, s, n, day_sales, mcount in rows:
        if mcount is not None and mcount != n:
            bad_cnt += 1
        if day_sales is not None and abs((s or 0) - day_sales) > 1:
            bad_sum += 1
    out(f"- 有分片的 (影院,日) 组合 **{len(rows):,}** 个。")
    out(f"- `movieCount != 片目行数`：**{bad_cnt}** 个。")
    out(f"- `sum(片目) != 分片接口当日合计`（>1 分）：**{bad_sum}** 个。")
    out("  > 两者同源，理论上应逐分相等；不为 0 说明写入链路有丢行。")
    out()

    # ---------------------------------------------------------------- 4 独立端点交叉
    out("## 4. 独立端点交叉校验（Q1 / Q3）")
    out()
    pairs = conn.execute("""
        SELECT m.cinema_code, m.date, SUM(m.sales_fen) s
        FROM fact_cinema_movie m GROUP BY 1,2 ORDER BY RANDOM() LIMIT 40
    """).fetchall()
    if not pairs:
        out("- 尚无分片数据，跳过。")
    else:
        q1_n = q1_bad = q3_n = q3_bad = 0
        details = []
        for code, date, s in pairs:
            try:
                sm = api.cinema_sum(client, code, date)
            except (HardError, RetryExhausted) as exc:
                details.append((code, date, "ERR", str(exc)[:60]))
                continue
            m = unit_money(sm.get("salesDesc"))
            if not m:
                continue
            q1_n += 1
            # getCinemaSumData 的 value 恒以「万」为单位、保留 2 位小数，
            # 舍入误差 ±0.005 万 = ±50 元 = ±5000 分，小影院上可达 3%。故用绝对+相对双阈值。
            tol = max(5000, s * 0.01)
            if s and abs(m.fen - s) > tol:
                q1_bad += 1
                details.append((code, date, f"{s} vs {m.fen}", f">{(abs(m.fen-s)/s):.2%}"))
            rk = conn.execute(
                "SELECT rank_sales_fen FROM fact_cinema_day WHERE cinema_code=? AND date=?",
                (code, date)).fetchone()
            if rk and rk[0]:
                q3_n += 1
                if abs(rk[0] - m.fen) > max(5000, m.fen * 0.01):
                    q3_bad += 1
        out(f"- **Q1** `sum(片目) vs getCinemaSumData.sales`：比对 {q1_n} 组，"
            f"超阈值（1% 或 ±5000 分，取大）**{q1_bad}** 组。")
        out(f"- **Q3** `getCinemaSumData.sales vs 当天影院榜 salesDesc`：比对 {q3_n} 组，"
            f"超阈值 **{q3_bad}** 组。")
        out("  > 阈值必须带绝对值：`getCinemaSumData` 的 value 固定保留「万」两位小数，")
        out("  > 对日票房 1600 元的影院，舍入就有 ±50 元 = 3%，纯 1% 相对阈值会全员误判。")
        for d in details[:5]:
            out(f"  - {d[0]} {d[1]}  {d[2]}  {d[3]}")
    out()

    # ---------------------------------------------------------------- 5 口径判定
    out("## 5. 口径判定（Q6）—— 这是原方案没解决、又必须钉死的一环")
    out()
    rows = conn.execute("""
        SELECT d.date, SUM(d.sales_fen) fen, n.sales_yuan, n.split_sales_yuan
        FROM fact_cinema_day d JOIN fact_national n ON n.date=d.date
        WHERE d.sales_src='ranklist' AND d.sales_fen IS NOT NULL
        GROUP BY d.date ORDER BY d.date
    """).fetchall()
    if rows:
        rs = [ (r[1]/100.0)/r[2] for r in rows if r[2] ]
        rp = [ (r[1]/100.0)/r[3] for r in rows if r[3] ]
        out(f"- 影院榜合计 / 全国含服务费：中位 **{statistics.median(rs):.4f}**")
        out(f"- 影院榜合计 / 全国分账：中位 **{statistics.median(rp):.4f}**")
        closer = "含服务费" if abs(statistics.median(rs)-1) < abs(statistics.median(rp)-1) else "分账"
        out(f"- 结论：影院榜（及由它汇总的分片）口径 = **{closer}**。")
        out(f"  > 两者差 {(1-statistics.median(rp)/statistics.median(rs)):.2%}，"
            f"若面板混用会直接变成系统性偏差。")
    else:
        out("- 数据不足，无法判定。")
    out()

    # ---------------------------------------------------------------- 6 样本份额稳定性
    out("## 6. 样本份额稳定性（Q4）")
    out()
    sample_n = conn.execute("SELECT COUNT(*) FROM dim_cinema WHERE sample=1").fetchone()[0]
    cov = dict(conn.execute("""
        SELECT substr(key, instr(key,'|')+1) d, SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)
        FROM collect_ledger WHERE job='cinema_box' GROUP BY d
    """).fetchall())
    full_dates = sorted(d for d, n in cov.items() if sample_n and n >= sample_n * 0.99)
    out(f"- 样本 {sample_n} 家；完整覆盖的日期 **{len(full_dates)}** 天"
        f"（仅用这些天算份额，避免半日覆盖污染）。")
    if full_dates:
        marks = ",".join("?" * len(full_dates))
        rows = conn.execute(f"""
            SELECT d.date, SUM(d.sales_fen) s, n.sales_yuan
            FROM fact_cinema_day d
            JOIN dim_cinema c ON c.cinema_code=d.cinema_code AND c.sample=1
            JOIN fact_national n ON n.date=d.date
            WHERE d.sales_fen IS NOT NULL AND d.date IN ({marks})
            GROUP BY d.date ORDER BY d.date
        """, full_dates).fetchall()
        shares = [r[1] / 100.0 / r[2] for r in rows if r[2]]
        if len(shares) >= 3:
            out(f"- 样本占全国比例：均值 **{statistics.mean(shares):.4f}**，"
                f"标准差 {statistics.pstdev(shares):.5f}，"
                f"区间 {min(shares):.4f} ~ {max(shares):.4f}。")
            jumps = [(rows[i][0], abs(shares[i] - shares[i - 1]) / shares[i - 1])
                     for i in range(1, len(shares))]
            worst = max(jumps, key=lambda x: x[1])
            out(f"- 最大日间跳变 **{worst[1]:.2%}**（{worst[0]}）。")
            out("  > 样本固定不变时，份额只应随影片档期缓慢漂移；单日跳变 >10% 即当日抓漏。")
        else:
            out("- 完整日期不足 3 天。")
    out()

    # ---------------------------------------------------------------- 7 单位体检
    out("## 7. 单位体检（Q7）")
    out()
    r = conn.execute("""
        SELECT COUNT(*) n,
               SUM(CASE WHEN sales_fen IS NULL THEN 1 ELSE 0 END) nulls,
               MIN(sales_fen), MAX(sales_fen)
        FROM fact_cinema_day WHERE sales_src='ranklist'
    """).fetchone()
    out(f"- 影院日行 {r[0]:,}，金额为空 {r[1] or 0:,}，区间 {_fmt(r[2])} ~ {_fmt(r[3])} 分。")
    r = conn.execute("""
        SELECT COUNT(*) FROM fact_cinema_movie WHERE sales_fen IS NULL OR sales_fen < 0
    """).fetchone()
    out(f"- 分片行中金额异常（空/负）：{r[0]:,}。")
    r = conn.execute("""
        SELECT AVG(1.0*sales_fen/100/tickets) FROM fact_cinema_day
        WHERE tickets > 20 AND sales_fen > 0
    """).fetchone()
    out(f"- 平均票价（总额/人次）≈ **{r[0]:.2f} 元**（合理区间 25~90，越界说明单位错位）。")
    out()

    # ---------------------------------------------------------------- 8 手检
    out("## 8. 人工抽查线索（Q5）")
    out()
    rows = conn.execute("""
        SELECT d.cinema_code, c.cinema_name, d.date, d.sales_fen, d.tickets, d.sessions,
               d.movie_count, d.sales_src
        FROM fact_cinema_day d JOIN dim_cinema c ON c.cinema_code=d.cinema_code
        WHERE d.sales_src='box' ORDER BY d.sales_fen DESC LIMIT 3
    """).fetchall()
    for r in rows:
        out(f"- `{r[0]}` {r[1]} @ {r[2]}：{_fmt(r[3]/100, 2)} 元，{_fmt(r[4])} 人次，"
            f"{_fmt(r[5])} 场，{r[6]} 部（{r[7]} 口径）")
    out()
    out("> 抽查方法：打开 https://zgdypf.zgdypw.cn/cinema/detail/<影院编码>?tab=1 ，"
        "切到对应日期，核对档期票房与片目票房。")

    md = "\n".join(lines)
    if md_path:
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"\n报告已写入 {md_path}")
    return 0

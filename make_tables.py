#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_tables.py —— 把本次实跑结果整理成供人阅读的 Markdown 表格。"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = "boxoffice.db"
OUT = "TABLES.md"
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
lines = []


def w(s=""):
    lines.append(s)


def fmt(x, nd=0):
    if x is None:
        return "-"
    if nd == 0:
        return f"{x:,.0f}"
    return f"{x:,.{nd}f}"


def wan(fen, nd=2):
    return fmt((fen or 0) / 1e6, nd)


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
D = "2026-04-30"

w("# 采集结果表格（2026-04-01 ~ 04-30 实跑）")
w()
w(f"> 生成时间 {dt.datetime.now():%Y-%m-%d %H:%M}　数据源：中国电影票房（专资办口径，含服务费）")
w("> 金额单位：表中统一换算为**万元**；库内一律存**分**。")
w()

# ---------------------------------------------------------------- 1 概览
w("## 表 1　运行概览")
w()
w("| 项目 | 数值 |")
w("|---|---:|")
n_days = conn.execute("SELECT COUNT(DISTINCT date) FROM fact_cinema_day "
                      "WHERE sales_src='ranklist' AND date BETWEEN '2026-04-01' AND '2026-04-30'").fetchone()[0]
n_cin = conn.execute("SELECT COUNT(*) FROM dim_cinema WHERE sample=1").fetchone()[0]
n_box = conn.execute("SELECT COUNT(*) FROM fact_cinema_movie").fetchone()[0]
n_cd = conn.execute("SELECT COUNT(*) FROM fact_cinema_day WHERE sales_src='box'").fetchone()[0]
ok = conn.execute("SELECT COUNT(*) FROM collect_ledger WHERE job='cinema_box' AND status='ok'").fetchone()[0]
em = conn.execute("SELECT COUNT(*) FROM collect_ledger WHERE job='cinema_box' AND status='empty'").fetchone()[0]
er = conn.execute("SELECT COUNT(*) FROM collect_ledger WHERE job='cinema_box' AND status='error'").fetchone()[0]
w(f"| 面板窗口 | 2026-04-01 ~ 04-30（{n_days} 天） |")
w(f"| 样本影院 | {n_cin} 家 / 31 个省 |")
w(f"| 采集任务 | {ok+em+er:,} 个（成功 {ok:,} / 空 {em} / 失败 {er}） |")
w(f"| 影院×日（样本） | {n_cd:,} 组合 |")
w(f"| 影院×影片×日 | {n_box:,} 行 |")
w(f"| 空值率 | {em/(ok+em+er):.2%} |")
w()

# ---------------------------------------------------------------- 2 全国
w("## 表 2　全国大盘（30 天逐日）")
w()
w("| 日期 | 星期 | 票房(万元) | 分账(万元) | 服务费占比 | 较前日 |")
w("|---|---|---:|---:|---:|---:|")
rows = conn.execute("SELECT * FROM fact_national WHERE date BETWEEN '2026-04-01' AND '2026-04-30' "
                    "ORDER BY date").fetchall()
prev = None
for r in rows:
    d = dt.date.fromisoformat(r["date"])
    chg = "-" if not prev else f"{(r['sales_yuan']/prev-1):+.1%}"
    w(f"| {r['date']} | {WEEK[d.weekday()]} | {r['sales_yuan']/1e4:,.2f} | "
      f"{r['split_sales_yuan']/1e4:,.2f} | {r['svc_fee_ratio']:.2%} | {chg} |")
    prev = r["sales_yuan"]
tot = sum(r["sales_yuan"] for r in rows)
w(f"| **合计** | | **{tot/1e4:,.2f}** | | | |")
w()

# ---------------------------------------------------------------- 3 省份
w(f"## 表 3　省份榜 Top 10（{D}）")
w()
w("| # | 省份 | 票房(万元) | 人次 | 场次 | 场均人次 | 单座产出 | 网售占比 |")
w("|---:|---|---:|---:|---:|---:|---:|---:|")
for i, r in enumerate(conn.execute(
        "SELECT * FROM fact_province WHERE date=? ORDER BY sales_fen DESC LIMIT 10", (D,)), 1):
    w(f"| {i} | {r['name']} | {wan(r['sales_fen'])} | {r['tickets_desc']} | {r['sessions_desc']} | "
      f"{r['avg_per_session']} | {r['avg_seat_output']} | {r['online_rate']:.2%} |")
w()

# ---------------------------------------------------------------- 4 院线
w(f"## 表 4　院线榜 Top 10（{D}）")
w()
w("| # | 院线 | 票房(万元) | 人次 | 场次 | 场均人次 | 网售占比 |")
w("|---:|---|---:|---:|---:|---:|---:|")
for i, r in enumerate(conn.execute(
        "SELECT * FROM fact_chain WHERE date=? ORDER BY sales_fen DESC LIMIT 10", (D,)), 1):
    w(f"| {i} | {r['name']} | {wan(r['sales_fen'])} | {r['tickets_desc']} | {r['sessions_desc']} | "
      f"{r['avg_per_session']} | {r['online_rate']:.2%} |")
w()

# ---------------------------------------------------------------- 5 影院
w(f"## 表 5　影院榜 Top 10（{D}）")
w()
w("| # | 影院 | 编码 | 票房(万元) | 人次 | 场次 | 场均 | 单座产出 | 网售占比 |")
w("|---:|---|---|---:|---:|---:|---:|---:|---:|")
for i, r in enumerate(conn.execute(
        "SELECT * FROM fact_cinema_day WHERE date=? AND sales_fen IS NOT NULL "
        "ORDER BY sales_fen DESC LIMIT 10", (D,)), 1):
    nm = conn.execute("SELECT cinema_name FROM dim_cinema WHERE cinema_code=?",
                      (r["cinema_code"],)).fetchone()
    w(f"| {i} | {(nm[0] if nm else '')[:22]} | {r['cinema_code']} | {wan(r['sales_fen'])} | "
      f"{fmt(r['tickets'])} | {fmt(r['sessions'])} | {fmt(r['avg_per_session'])} | "
      f"{r['avg_seat_output'] or '-'} | {r['online_rate']:.2%} |" if r["online_rate"] is not None
      else f"| {i} | {(nm[0] if nm else '')[:22]} | {r['cinema_code']} | {wan(r['sales_fen'])} | - | - | - | - | - |")
w()

# ---------------------------------------------------------------- 6 样本构成
w("## 表 6　样本构成（150 家）")
w()
w("### 6.1 按规模三分位")
w()
w("| 分层 | 影院数 | 当日票房合计(万元) | 占比 |")
w("|---|---:|---:|---:|")
tot_s = conn.execute("SELECT SUM(list_sales_fen) FROM dim_cinema WHERE sample=1").fetchone()[0] or 1
for t in ("T1", "T2", "T3"):
    r = conn.execute("SELECT COUNT(*), SUM(list_sales_fen) FROM dim_cinema "
                     "WHERE sample=1 AND stratum LIKE ?", (f"%|{t}",)).fetchone()
    label = {"T1": "T1 小（后 1/3）", "T2": "T2 中", "T3": "T3 大（前 1/3）"}[t]
    w(f"| {label} | {r[0]} | {wan(r[1])} | {(r[1] or 0)/tot_s:.1%} |")
w()
w("### 6.2 省份分布 Top 12")
w()
w("| # | 省份 | 样本影院数 |")
w("|---:|---|---:|")
for i, r in enumerate(conn.execute(
        "SELECT province, COUNT(*) n FROM dim_cinema WHERE sample=1 "
        "GROUP BY province ORDER BY n DESC LIMIT 12"), 1):
    w(f"| {i} | {r['province']} | {r['n']} |")
w()

# ---------------------------------------------------------------- 7 单片明细
w(f"## 表 7　单影院片目明细：上海市上海影城（31081501）· {D}")
w()
w("| # | 影片 | 影片编码 | 上映第 N 天 | 单日(元) | 占本店 | 排片占比 | 本店累计 |")
w("|---:|---|---|---:|---:|---:|---:|---:|")
rows = conn.execute("SELECT * FROM fact_cinema_movie WHERE cinema_code='31081501' AND date=? "
                    "ORDER BY sales_fen DESC", (D,)).fetchall()
for i, r in enumerate(rows, 1):
    w(f"| {i} | {r['movie_name']} | {r['movie_code']} | {r['release_day']} | "
      f"{r['sales_fen']/100:,.2f} | {(r['sales_rate'] or 0):.2%} | "
      f"{(r['session_count_rate'] or 0):.2%} | {r['sum_sales_text']} |")
ss = sum(r["sales_fen"] for r in rows)
w(f"| | **合计 {len(rows)} 部** | | | **{ss/100:,.2f}** | 100.00% | 100.00% | |")
w()

# ---------------------------------------------------------------- 8 影片逐日
w("## 表 8　影片逐日：《哪吒之魔童闹海》（00100072025）")
w()
w("| 上映第 N 天 | 日期 | 星期 | 单日(亿元) | 单日(万元) | 人次 | 场次 | 排片占比 | 票房占比 | 上座率 |")
w("|---:|---|---|---:|---:|---:|---:|---:|---:|---:|")
for r in conn.execute("SELECT * FROM fact_movie_day WHERE movie_code='00100072025' "
                      "AND release_days BETWEEN 1 AND 8 ORDER BY release_days"):
    w(f"| {r['release_days']} | {r['show_date']} | {r['week']} | {r['sales_fen']/1e10:.2f} | "
      f"{r['sales_fen']/1e6:,.2f} | {r['sales_count']:,} | {r['session_count']:,} | "
      f"{(r['session_rate'] or 0):.2%} | {(r['sales_rate'] or 0):.2%} | {(r['seat_rate'] or 0):.2%} |")
w()

# ---------------------------------------------------------------- 9 覆盖
w("## 表 9　逐日采集覆盖（样本 150 家）")
w()
w("| 日期 | 成功 | 无数据 | 完成时刻 |")
w("|---|---:|---:|---|")
for r in conn.execute("""
    SELECT substr(key, instr(key,'|')+1) d,
           SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok,
           SUM(CASE WHEN status='empty' THEN 1 ELSE 0 END) em,
           MAX(checked_at) ts
    FROM collect_ledger WHERE job='cinema_box' GROUP BY d ORDER BY d"""):
    w(f"| {r['d']} | {r['ok']} | {r['em']} | {str(r['ts'])[11:]} |")
w()

# ---------------------------------------------------------------- 10 质检
w("## 表 10　质检结果汇总")
w()
w("| 编号 | 检查项 | 方法 | 结果 | 结论 |")
w("|---|---|---|---|---|")
w("| Q1 | 片目合计 vs 影院日报 | 40 组随机抽样，比对 `getCinemaSumData.sales` | 25 组中 6 组超阈 | 超阈均为小影院，差值 2%~3%，源自「万」两位小数截断 |")
w("| Q2 | `movieCount` vs 片目行数 | 4,493 组全量比对 | **0 组不一致** | 通过 |")
w("| Q2b | 片目合计 vs 接口当日合计 | 4,493 组全量比对 | **0 组不一致** | 通过（同源逐分相等） |")
w("| Q3 | 影院日报 vs 影院榜 | 25 组比对 | 6 组超阈 | 同上，截断导致 |")
w("| Q4 | 样本占全国比稳定性 | 29 个完整日 | 3.3%~5.8%，最大跳变 20.25% | **未通过**，见表 13 |")
w("| Q5 | 网页人工抽查 | 3 家 | 待人工 | 给出编码与数值 |")
w("| Q6 | 口径判定 | 全量榜合计 ÷ 全国 | 0.9605（含服务费）/ 1.0731（分账） | **含服务费** |")
w("| Q7 | 单位体检 | 总额÷人次 | 平均票价 35.36 元 | 正常（合理区间 25~90） |")
w("| Q8 | 覆盖度 | 台账统计 | 空值率 0.16%，0 错误 | 通过 |")
w()

# ---------------------------------------------------------------- 11 口径
w("## 表 11　口径与偏差")
w()
w("| 指标 | 数值 | 说明 |")
w("|---|---:|---|")
r = conn.execute("""SELECT
      (SELECT SUM(sales_fen) FROM fact_cinema_day
        WHERE date BETWEEN '2026-04-01' AND '2026-04-30' AND sales_src='ranklist') s,
      (SELECT SUM(sales_yuan) FROM fact_national
        WHERE date BETWEEN '2026-04-01' AND '2026-04-30') nat_svc,
      (SELECT SUM(split_sales_yuan) FROM fact_national
        WHERE date BETWEEN '2026-04-01' AND '2026-04-30') nat_spl""").fetchone()
svc_wan = r["nat_svc"] / 1e4
spl_wan = r["nat_spl"] / 1e4
w(f"| 影院榜合计 | {r['s']/1e6:,.0f} 万元 | 12,000+ 家逐日加总 |")
w(f"| 全国含服务费 | {svc_wan:,.0f} 万元 | `withSvcFee=true` |")
w(f"| 比值 | **{(r['s']/100.0)/r['nat_svc']:.4f}** | 稳定缺 4% → 榜单未穷尽全部票房 |")
w(f"| 全国分账 | {spl_wan:,.0f} 万元 | `withSvcFee=false` |")
w(f"| 服务费占比 | {1-r['nat_spl']/r['nat_svc']:.2%} | 含服务费比分离口径高约 10% |")
w()

# ---------------------------------------------------------------- 12 字典
w("## 表 12　字段字典（常用表）")
w()
w("| 表 | 字段 | 单位/类型 | 含义 |")
w("|---|---|---|---|")
for t, f, u, d in [
    ("fact_cinema_day", "sales_fen", "分", "影院当日票房（分片来源时精确）"),
    ("fact_cinema_day", "rank_sales_fen", "分", "影院榜解析值（被截断，仅作对照）"),
    ("fact_cinema_day", "sales_lossy", "0/1", "1 = 金额来自字符串截断"),
    ("fact_cinema_day", "tickets", "人次", "由 Desc 解析，大数值有舍入"),
    ("fact_cinema_day", "sessions", "场", "分片来源时为精确整数"),
    ("fact_cinema_movie", "sales_fen", "分", "该片在本店当日票房"),
    ("fact_cinema_movie", "sales_rate", "小数", "占本店当日票房比例"),
    ("fact_cinema_movie", "session_count_rate", "小数", "排片场次占比"),
    ("fact_cinema_movie", "sum_sales_yuan", "元", "该片在本店累计票房"),
    ("fact_movie_day", "sales_fen", "分", "单片全国当日票房"),
    ("fact_national", "svc_fee_ratio", "小数", "服务费占含服务费票房比例"),
    ("collect_ledger", "status", "枚举", "ok / empty(204) / error"),
]:
    w(f"| `{t}` | `{f}` | {u} | {d} |")
w()

# ---------------------------------------------------------------- 13 未决
w("## 表 13　未收敛的问题")
w()
w("| # | 问题 | 现象 | 可能原因 | 下一步 |")
w("|---|---|---|---|---|")
w("| 1 | 样本份额漂移 | 4/01 的 3.3% → 4/30 的 5.8% | 抽样锚定日（4/30）选择偏差；或档期结构变化 | 改用月中锚定日重抽，或加规模匹配对照组 |")
w("| 2 | 榜单合计缺 4% | 12,000 家合计 = 全国 96% | 榜单未穷尽 + Desc 截断 | 未纳入影院的名单需另寻来源 |")
w("| 3 | 1/3 日期塌缩 | 影院维度整片缺失 | 站点侧数据重建 | 采前 `verify`，按日开闸 |")

open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"已生成 {OUT}，共 {len(lines)} 行")

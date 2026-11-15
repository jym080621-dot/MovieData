#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_gap.py —— 扫清「影院维度按日缺失」的范围与影响面。

要回答四件事：
  1. 缺口日期清单（哪个范围、什么规律）；
  2. 缺口日上 getCinemaBoxData 是否也空（即面板能否绕开）；
  3. 缺口日上影片 SSR 序列是否完整（影片维度是否幸免）；
  4. 缺口日上 getCinemaManageData 是否也缺（与之前观察到的缺行是否同源）。
"""
from __future__ import annotations

import datetime as dt
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zgdypf import api
from zgdypf.client import Client, HardError, RetryExhausted

c = Client(rate=2.0)
START = dt.date(2026, 8, 15)
END = dt.date(2026, 11, 13)


def day_has_cinema(date: str) -> tuple:
    try:
        d = api.cinema_list(c, date, limit=2, offset=0)
        rows = d.get("salesRankList") or []
        return len(rows), bool(d.get("hasMore"))
    except (HardError, RetryExhausted):
        return -1, None


print("== 1. 逐日探测影院榜是否存在（2026-08-15 .. 2026-11-13）")
bad, good = [], []
d = START
while d <= END:
    ds = d.isoformat()
    n, more = day_has_cinema(ds)
    (bad if (n is not None and n >= 0 and not more and n <= 5) else good).append(ds)
    d += dt.timedelta(days=1)
print(f"  正常 {len(good)} 天 / 异常 {len(bad)} 天")
print("  异常日期:", ", ".join(bad))
# 间隔规律
if bad:
    gaps = []
    prev = None
    for b in bad:
        cur = dt.date.fromisoformat(b)
        if prev:
            gaps.append((cur - prev).days)
        prev = cur
    print("  相邻间隔:", gaps)

print("\n== 2. 缺口日 vs 正常日：影院分片是否也空")
sample_dates = (bad[:3] + good[-3:]) if bad else good[-3:]
for date in sample_dates:
    for code in ("33019193", "11061601"):
        try:
            box = api.cinema_box(c, code, date)
            n = len((box or {}).get("movieSalesList") or [])
            print(f"  {date}  {code}  box={'204/None' if box is None else f'{n} 部'}")
        except (HardError, RetryExhausted) as e:
            print(f"  {date}  {code}  ERR {e}")

print("\n== 3. 缺口日：影片 SSR 序列是否有该日")
state, payload = api.movie_detail(c, "00102260026")  # 神探之痕迹
idx = payload.get("index") or []
have = {r.get("showDate") for r in idx if isinstance(r, dict)}
print(f"  神探之痕迹 序列 {len(idx)} 条, 覆盖 {min(have) if have else '-'} .. {max(have) if have else '-'}")
for date in sample_dates:
    print(f"    {date} 在序列中: {date in have}")

print("\n== 4. 缺口日：影院趋势接口是否也缺")
trend = api.cinema_manage(c, "33019193")
tdates = {r.get("dateUp") for r in trend if isinstance(r, dict)}
print(f"  manage 返回 {len(trend)} 条: {sorted(tdates)}")
for date in sample_dates:
    print(f"    {date} 在趋势中: {date in tdates}")

print("\n== 5. 缺口日：全国/省份/影片榜是否正常")
for date in sample_dates:
    nat = api.national(c, date)
    pv = api.province_list(c, date, limit=3)
    dd = api.day_data(c, date)
    print(f"  {date}  national={bool(nat.get('salesDesc'))} "
          f"province={len(pv.get('salesRankList') or [])} "
          f"dayData={len(dd.get('list') or [])}")

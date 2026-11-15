#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_list2.py —— 继续定位：是全量排序塌了，还是该日数据本身缺失。"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zgdypf import api
from zgdypf.client import Client, HardError, RetryExhausted

c = Client(rate=2.0)


def n_of(date, **kw):
    try:
        d = api.cinema_list(c, date, **kw)
        rows = d.get("salesRankList") or []
        return len(rows), d.get("hasMore")
    except (HardError, RetryExhausted) as e:
        return -1, str(e)[:60]


print("== A. 坏日期带城市筛选（广州 440100）")
for date in ("2026-10-14", "2026-10-17", "2026-10-20"):
    print(f"  {date} city=440100 ->", n_of(date, limit=500, geo_code=440100, geo_type=3))

print("\n== B. 坏日期 withSvcFee=false")
for date in ("2026-10-14", "2026-10-20"):
    print(f"  {date} svc=false ->", n_of(date, with_svc=False, limit=500))
    print(f"  {date} svc=true  ->", n_of(date, with_svc=True, limit=500))

print("\n== C. 前后日期扫描（limit=500）")
for d in range(1, 21):
    date = f"2026-10-{d:02d}"
    n, more = n_of(date, limit=500)
    print(f"  {date} n={n:<6} hasMore={more}")

print("\n== D. 更早日期")
for date in ("2025-11-12", "2023-06-01", "2024-11-12"):
    print(f"  {date} ->", n_of(date, limit=500))

print("\n== E. 坏日期的省级榜 / 城市榜是否有数")
for date in ("2026-10-14", "2026-10-20"):
    pv = api.province_list(c, date, limit=50)
    rows = pv.get("salesRankList") or []
    print(f"  {date} 省份 {len(rows)} 首={rows[0].get('name') if rows else '-'} "
          f"销售={rows[0].get('sales') if rows else '-'}")

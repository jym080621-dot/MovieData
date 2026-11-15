#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_list.py —— 排查「部分日期全量影院榜只返回个位数」的原因。"""
from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zgdypf import api
from zgdypf.client import Client, HardError, RetryExhausted

BAD = ["2026-10-14", "2026-10-17", "2026-10-20"]
GOOD = ["2026-11-12", "2026-10-18"]


def probe(c, date, limit, offset=0):
    t0 = time.time()
    try:
        d = api.cinema_list(c, date, limit=limit, offset=offset)
        rows = d.get("salesRankList") or []
        return dict(date=date, limit=limit, offset=offset, n=len(rows),
                    hasMore=d.get("hasMore"), ms=int((time.time() - t0) * 1000),
                    first=(rows[0].get("name") if rows else None),
                    last=(rows[-1].get("name") if rows else None))
    except (HardError, RetryExhausted) as e:
        return dict(date=date, limit=limit, offset=offset, n=-1,
                    ms=int((time.time() - t0) * 1000), err=str(e)[:120])


def main():
    c = Client(rate=2.0)
    print("== A. 同一日期、不同 limit 的返回条数")
    for date in BAD + GOOD:
        for limit in (20000, 5000, 1000, 500, 100):
            r = probe(c, date, limit)
            print(f"  {date} limit={limit:<6} n={r['n']:<6} hasMore={r.get('hasMore')!s:<6} "
                  f"{r['ms']}ms  {r.get('first') or ''}")

    print("\n== B. 坏日期重试 3 次（limit=20000），看是否随机")
    for date in BAD[:2]:
        for i in range(3):
            r = probe(c, date, 20000)
            print(f"  {date} try#{i+1} n={r['n']} ms={r['ms']}")

    print("\n== C. 坏日期改用分页 limit=500 能拉多少")
    for date in BAD:
        rows, trunc = api.cinema_list_all(c, date, page=500, max_pages=40)
        print(f"  {date} limit=500 分页 -> {len(rows)} 家  truncated={trunc}")

    print("\n== D. 好日期 limit=500 分页对照")
    rows, trunc = api.cinema_list_all(c, GOOD[-1], page=500, max_pages=40)
    print(f"  {GOOD[-1]} limit=500 分页 -> {len(rows)} 家  truncated={trunc}")


if __name__ == "__main__":
    main()

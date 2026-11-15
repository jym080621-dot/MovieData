#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_rotation.py —— 观测「影院维度按日可用性」随时间的变化。

同一个日期在不同时刻可能：全量可用 / 塌成个位数 / 完全 204。
若是滚动重建，则面板采集必须做「完整性闸门 + 事后修复」，不能一把梭。
"""
from __future__ import annotations

import datetime as dt
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zgdypf import api
from zgdypf.client import Client, HardError, RetryExhausted

START = dt.date(2026, 10, 25)
END = dt.date(2026, 11, 13)


def scan(c, tag):
    out = {}
    d = START
    while d <= END:
        ds = d.isoformat()
        try:
            j = api.cinema_list(c, ds, limit=2, offset=0)
            rows = j.get("salesRankList") or []
            if j.get("hasMore"):
                out[ds] = "FULL"
            elif len(rows) <= 5:
                out[ds] = f"THIN({len(rows)})"
            else:
                out[ds] = f"PART({len(rows)})"
        except (HardError, RetryExhausted):
            out[ds] = "ERR"
        d += dt.timedelta(days=1)
    print(f"\n[{tag}] {dt.datetime.now():%H:%M:%S}")
    for k, v in out.items():
        print(f"   {k}  {v}")
    return out


def main():
    c = Client(rate=2.0)
    a = scan(c, "T0")
    time.sleep(90)
    b = scan(c, "T1 (+90s)")
    print("\n== 变化")
    for k in a:
        if a[k] != b[k]:
            print(f"   {k}: {a[k]} -> {b[k]}")
    stable = sum(1 for k in a if a[k] == b[k])
    print(f"   未变 {stable}/{len(a)}")


if __name__ == "__main__":
    main()

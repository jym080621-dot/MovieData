#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diag_flaky.py —— 判断「同一天返回条数漂移」是后端不一致还是缓存。"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://zgdypf.zgdypw.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
H = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/",
     "Accept": "application/json, text/plain, */*"}


def hit(date, limit=20000, extra=None):
    p = {"date": date, "withSvcFee": "true", "limit": limit, "offset": 0}
    if extra:
        p.update(extra)
    url = BASE + "/getCinemaList?" + urllib.parse.urlencode(p)
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=H)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            hdrs = {k: v for k, v in r.headers.items()}
            st = r.status
    except urllib.error.HTTPError as e:
        return dict(status=e.code, n=-1, ms=0, hdr={})
    ms = int((time.time() - t0) * 1000)
    try:
        d = json.loads(body)
        rows = d.get("salesRankList") or []
        n = len(rows)
        more = d.get("hasMore")
    except Exception:
        n, more = -1, None
    keep = {k: hdrs.get(k) for k in ("Server", "Via", "X-Cache", "Age", "Cache-Control",
                                     "ETag", "Content-Length", "X-Cache-Status") if hdrs.get(k)}
    return dict(status=st, n=n, more=more, ms=ms, bytes=len(body), hdr=keep)


for date in ("2026-11-12", "2026-11-10", "2026-10-14"):
    print(f"\n== {date}  连续 6 次 limit=20000")
    for i in range(6):
        r = hit(date)
        print(f"  #{i+1} n={r['n']:<6} hasMore={str(r.get('more')):<6} {r['ms']:<6}ms "
              f"{r.get('bytes')}B  {r.get('hdr')}")
        time.sleep(0.6)

print("\n== 2026-11-12 加随机 query 破缓存 4 次")
for i in range(4):
    r = hit("2026-11-12", extra={"_r": f"{time.time():.6f}"})
    print(f"  #{i+1} n={r['n']:<6} hasMore={r.get('more')} {r['ms']}ms")
    time.sleep(0.6)

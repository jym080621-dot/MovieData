#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contract_test.py —— 把 P0 的实测裁决固化成可执行断言。

任何一条挂掉，说明站点契约变了，后续采集一律停止。共 26 条。
用法： python contract_test.py [--no-heavy]
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta

from zgdypf import api
from zgdypf.client import Client, HardError
from zgdypf.parse import parse_count, parse_money, parse_ratio, unit_money
from zgdypf.ssr import extract_state, movie_payload, parse_pub_date

D = "2026-11-12"          # 昨日
TODAY = date(2026, 11, 13)
PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   :: " + str(detail)) if detail else ""))


def main():
    heavy = "--no-heavy" not in sys.argv
    c = Client(rate=2.0)
    t0 = time.time()

    print("\n[1] 元数据与大盘")
    dr = api.date_range(c)
    ok("getDateRange 有 startDate/endDate",
       bool(dr.get("startDate")) and bool(dr.get("endDate")), dr)
    ok("dateRange.startDate == 2017-01-01", dr.get("startDate") == "2017-01-01", dr.get("startDate"))
    ok("dateRange.endDate >= 今日（实测含今日，非“昨日”）",
       str(dr.get("endDate")) >= TODAY.isoformat(), dr.get("endDate"))

    nat_svc = api.national(c, D, with_svc=True)
    nat_split = api.national(c, D, with_svc=False)
    sd = nat_svc.get("salesDesc")
    ok("national.salesDesc 是 dict{value,unit}（榜单里却是 str）",
       isinstance(sd, dict) and "value" in sd and "unit" in sd, sd)
    m_svc = unit_money(sd)
    m_split = unit_money(nat_split.get("splitSalesDesc"))
    ok("含服务费 > 分账（差值即服务费）",
       m_svc and m_split and m_svc.yuan > m_split.yuan,
       f"{m_svc} vs {m_split}")
    ok("updateGapSecond 为 int（实测 5）",
       isinstance(nat_svc.get("updateGapSecond"), int), nat_svc.get("updateGapSecond"))

    print("\n[2] 榜单与 Desc 单位")
    pv = api.province_list(c, D, limit=5)
    plist = pv.get("salesRankList") or []
    ok("provinceList 非空且含 sales 整数字段",
       bool(plist) and isinstance(plist[0].get("sales"), int), len(plist))
    ok("provinceList.salesDesc 是 str（与大盘 dict 不同型）",
       isinstance(plist[0].get("salesDesc"), str), type(plist[0].get("salesDesc")).__name__)
    m = parse_money(plist[0].get("salesDesc"))
    ok("省份 salesDesc 解析成交叉验证一致（|Δ|<0.01%）",
       m and abs(m.fen - plist[0]["sales"]) <= max(2, plist[0]["sales"] * 1e-4),
       f"{m.fen if m else None} vs {plist[0]['sales']}")

    print("\n[3] geoType 语义（本方案最危险的静默陷阱）")
    ch_all = api.chain_list(c, D, 0, 0)
    ch_reg = api.chain_list(c, D, 1, 1)
    a = ch_all.get("salesRankList") or []
    b = ch_reg.get("salesRankList") or []
    sum_a = sum(x.get("sales") or 0 for x in a)
    sum_b = sum(x.get("sales") or 0 for x in b)
    ok("chainList(0,0) 返回全国院线（>=40 条）", len(a) >= 40, len(a))
    ok("chainList(1,1) 不是全国（合计 < 全国的 40%）",
       sum_b < sum_a * 0.4, f"{sum_b/1e6:.2f}万 vs {sum_a/1e6:.2f}万")
    try:
        api.chain_list(c, D, None, None)
        ok("chainList 缺 geoCode/geoType 返回 400", False, "未抛异常")
    except HardError:
        ok("chainList 缺 geoCode/geoType 返回 400", True)

    print("\n[4] 搜索参数名")
    try:
        res = api.search_movie(c, "哪吒之魔童闹海")
        ok("searchMovie(movieName) 命中", bool(res) and res[0].get("code") == "00100072025",
           res[0].get("code") if res else None)
    except HardError as e:
        ok("searchMovie(movieName) 命中", False, e)
    try:
        c.get_json("/searchMovie", {"keyword": "哪吒", "limit": 5})
        ok("searchMovie(keyword) 应 400", False, "未抛异常")
    except HardError:
        ok("searchMovie(keyword) 应 400", True)
    sc = api.search_cinema(c, "英嘉")
    ok("searchCinema(cinemaName) 返回含 cityCode/hallCount",
       bool(sc) and "cityCode" in sc[0], sc[0].get("code") if sc else None)

    print("\n[5] 影院四件套与 204/“--”两种空值")
    box = api.cinema_box(c, "33019193", D) or {}
    ml = box.get("movieSalesList") or []
    ok("cinemaBox.movieCount == len(movieSalesList)",
       box.get("movieCount") == len(ml), f"{box.get('movieCount')} vs {len(ml)}")
    ssum = sum(x.get("sales") or 0 for x in ml)
    ok("杭州星光嘉映 11-12 片目合计 == 5,650,940 分", ssum == 5650940, ssum)
    ok("神探之痕迹 单店单日 == 3,677,250 分",
       any(x.get("movieCode") == "00102260026" and x.get("sales") == 3677250 for x in ml), "")

    box2 = api.cinema_box(c, "11061601", D) or {}
    small = (box2.get("movieSalesList") or [{}])[0]
    parsed = parse_money(small.get("salesDesc"))
    ok("小额 salesDesc 裸数字按【元】解析（非万/非分）",
       parsed and abs(parsed.fen - (small.get("sales") or 0)) <= 100,
       f"{small.get('salesDesc')} -> {parsed.fen if parsed else None} vs {small.get('sales')}")

    ok("无数据日 cinemaBox 返回 204/None", api.cinema_box(c, "11061601", "2017-01-05") is None)
    empty_sum = api.cinema_sum(c, "11061601", "2017-01-05")
    ok("无数据日 cinemaSum 是 200 + value='--'（不是 204）",
       isinstance(empty_sum.get("salesDesc"), dict)
       and empty_sum["salesDesc"].get("value") == "--"
       and unit_money(empty_sum["salesDesc"]) is None,
       empty_sum.get("salesDesc"))

    trend = api.cinema_manage(c, "11061601")
    dates = [x.get("dateUp") for x in trend if x.get("dateUp")]
    span = 0
    if len(dates) >= 2:
        d0 = date.fromisoformat(min(dates))
        d1 = date.fromisoformat(max(dates))
        span = (d1 - d0).days + 1
    ok("cinemaManage 返回 list 且「条数 != 日历天数」（缺行≠0）",
       isinstance(trend, list) and len(trend) > 0 and span > len(dates),
       f"{len(dates)} 条 / {span} 天")

    print("\n[6] 参数被忽略的接口")
    a1 = api.single_day(c)
    a2 = c.get_json("/getSingleDayData", {"date": "2025-02-01", "limit": 5})
    ok("getSingleDayData 参数被忽略（两次结果完全相同）",
       a1.get("rankList") == a2.get("rankList") and bool(a1.get("rankList")),
       len(a1.get("rankList") or []))

    pw = api.period_data(c, "2026-11-09", 1)
    pm = api.period_data(c, "2026-11-11", 1)
    ok("getPeriodData 只认周期起始日（周一有数、周三为空）",
       bool(pw.get("list")) and not pm.get("list"),
       f"{len(pw.get('list') or [])} vs {len(pm.get('list') or [])}")

    print("\n[7] 影片维度只有 SSR")
    try:
        c.get_json("/getMovieData", {"movieCode": "00100072025"})
        ok("getMovieData 应为 404", False, "未抛异常")
    except HardError:
        ok("getMovieData 应为 404", True)

    state, payload = api.movie_detail(c, "00100072025")
    ok("SSR __INITIAL_STATE__ 可解析", state is not None, "state is None" if state is None else "ok")
    series = payload.get("index") or []
    ok("movieIndexData 为逐日数组（>=150 条）", len(series) >= 150, len(series))
    info = payload.get("info") or {}
    ok("movieInfoData.name == 哪吒之魔童闹海", info.get("name") == "哪吒之魔童闹海", info.get("name"))
    ok("pubDesc -> 上映日 2025-01-29", parse_pub_date(info.get("pubDesc")) == "2025-01-29",
       info.get("pubDesc"))
    by_day = {r.get("releaseDays"): r for r in series if isinstance(r, dict)}
    expect = {1: 48747600000, 2: 48034180000, 3: 61915020000, 4: 73156380000}  # 分
    bad = []
    for d, want in expect.items():
        got = (by_day.get(d) or {}).get("sales")
        if got is None or abs(got - want) > want * 0.005:
            bad.append((d, got, want))
    ok("哪吒2 D1-D4 票房与基准一致（容差 0.5%）", not bad, bad or "4/4 命中")
    sums = {s.get("title"): s for s in (payload.get("box_sum") or [])}
    ok("movieBoxSum 含 累计/首周/首日 三项",
       {"累计票房", "首周票房", "首日票房"} <= set(sums), list(sums))

    if heavy:
        print("\n[8] 全量影院榜规模")
        t = time.time()
        rows, trunc = api.cinema_list_all(c, D, page=20000, max_pages=1)
        ok("cinemaList limit=20000 一次拿全量（>=12000 家, hasMore=false）",
           len(rows) >= 12000 and not trunc, f"{len(rows)} 家 {time.time()-t:.1f}s")

    print("\n" + "=" * 70)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}   耗时 {time.time()-t0:.1f}s   "
          f"请求 {c.stats.requests} 次  重试 {c.stats.retries}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

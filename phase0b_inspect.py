#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phase0b_inspect.py — 细读原始响应，确认字段类型/单位/结构/204 语义。"""
import os
import sys
import json
import time
import random
import urllib.parse
import urllib.request
import urllib.error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://zgdypf.zgdypw.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
           "Referer": BASE + "/", "Accept": "application/json, text/plain, */*"}
D = "2026-11-12"


def get(path, params=None):
    url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read()
            st = r.status
    except urllib.error.HTTPError as e:
        body = e.read()
        st = e.code
    except Exception as e:
        return {"_err": repr(e), "_ms": int((time.time() - t0) * 1000)}
    out = {"_status": st, "_ms": int((time.time() - t0) * 1000), "_bytes": len(body)}
    try:
        out["_json"] = json.loads(body.decode("utf-8"))
    except Exception:
        out["_text"] = body.decode("utf-8", "replace")[:300]
    time.sleep(0.25 + random.random() * 0.25)
    return out


def show(title, obj, keys=None, depth=0):
    print("\n--- %s" % title)
    if keys and isinstance(obj, dict):
        for k in keys:
            print("   %-26s = %s" % (k, json.dumps(obj.get(k), ensure_ascii=False)[:300]))
        return
    print(json.dumps(obj, ensure_ascii=False, indent=1)[:2600])


print("### A. 日期范围")
dr = get("/getDateRange")
show("getDateRange", dr.get("_json"))

print("\n### B. 全国大盘（昨日）")
show("getNational 2026-11-12", get("/getNational", {"date": D, "withSvcFee": "true"}).get("_json"))

print("\n### C. 院线榜 geoType 差异")
for gc, gt in ((0, 0), (1, 1), (1, 0), (0, 1)):
    j = get("/getChainList", {"date": D, "geoCode": gc, "geoType": gt}).get("_json") or {}
    lst = j.get("salesRankList") or []
    print("   geoCode=%s geoType=%s -> n=%-3d 首=%s 末=%s 合计=%.2f万"
          % (gc, gt, len(lst), (lst[0]["name"] if lst else None),
             (lst[-1]["name"] if lst else None),
             sum(x.get("sales") or 0 for x in lst) / 1e6))
    if (gc, gt) == (0, 0) and lst:
        show("chainList[0] 全字段", lst[0])

print("\n### D. 影院榜")
j = get("/getCinemaList", {"date": D, "withSvcFee": "true", "limit": 3, "offset": 0}).get("_json") or {}
lst = j.get("salesRankList") or []
print("   n=%d hasMore=%s updateTimeDesc=%s" % (len(lst), j.get("hasMore"), j.get("updateTimeDesc")))
if lst:
    show("cinemaList[0] 全字段", lst[0])
j2 = get("/getCinemaList", {"date": D, "withSvcFee": "true", "limit": 3, "offset": 3}).get("_json") or {}
print("   offset=3 ->", [x["name"] for x in (j2.get("salesRankList") or [])])

print("\n### E. 省份榜")
j = get("/getProvinceList", {"date": D, "withSvcFee": "true", "limit": 3, "offset": 0}).get("_json") or {}
lst = j.get("salesRankList") or []
print("   n=%d updateTimeDesc=%s" % (len(lst), j.get("updateTimeDesc")))
if lst:
    show("provinceList[0] 全字段", lst[0])

print("\n### F. 影院日报")
show("getCinemaSumData 11061601", get("/getCinemaSumData", {"cinemaCode": "11061601", "date": D}).get("_json"))

print("\n### G. 影院分片（两家）")
for code in ("11061601", "33019193"):
    j = get("/getCinemaBoxData", {"cinemaCode": code, "date": D}).get("_json") or {}
    ml = j.get("movieSalesList") or []
    print("   %s movieCount=%s newMovieCount=%s sessionCount=%s salesDesc=%s len=%d sum(sales)=%d 分 = %.4f 万"
          % (code, j.get("movieCount"), j.get("newMovieCount"), j.get("sessionCount"),
             json.dumps(j.get("salesDesc"), ensure_ascii=False), len(ml),
             sum(m.get("sales") or 0 for m in ml), sum(m.get("sales") or 0 for m in ml) / 1e6))
    if ml:
        show("   movieSalesList[0]", ml[0])
    hit = [m for m in ml if "神探" in (m.get("movieName") or "")]
    if hit:
        show("   神探之痕迹 条目", hit[0])

print("\n### H. 影院近14天走势")
j = get("/getCinemaManageData", {"cinemaCode": "11061601", "dateType": 0}).get("_json")
if isinstance(j, list):
    print("   n=%d 日期=%s" % (len(j), [x.get("dateUp") for x in j]))
    if j:
        show("trend[0] 全字段", j[0])

print("\n### I. 历史单日纪录（参数是否被忽略）")
a = get("/getSingleDayData").get("_json") or {}
b = get("/getSingleDayData", {"date": "2025-02-01"}).get("_json") or {}
ra, rb = a.get("rankList") or [], b.get("rankList") or []
print("   title=%s n=%d" % (a.get("title"), len(ra)))
print("   两次结果完全相同:", ra == rb)
if ra:
    show("rankList[0]", ra[0])

print("\n### J. searchMovie / searchCinema")
show("searchMovie 哪吒之魔童闹海", get("/searchMovie", {"movieName": "哪吒之魔童闹海", "limit": 5, "offset": 0}).get("_json"))
show("searchCinema 英嘉", get("/searchCinema", {"cinemaName": "英嘉", "limit": 5, "offset": 0}).get("_json"))

print("\n### K. getDayData（全国单日+分片榜）")
j = get("/getDayData", {"date": D, "withSvcFee": "true"}).get("_json") or {}
lst = j.get("list") or []
print("   n=%d nationalSales=%s" % (len(lst), json.dumps(j.get("nationalSales"), ensure_ascii=False)))
if lst:
    show("list[0] 全字段", lst[0])

print("\n### L. getCityData（用当日 top 影片）")
top = (lst[0].get("code") if lst else None)
print("   movieCode=", top)
j = get("/getCityData", {"date": D, "movieCode": top}).get("_json") or {}
cr = j.get("cityRank") or []
print("   cityRank n=%d movieList n=%d" % (len(cr), len(j.get("movieList") or [])))
if cr:
    show("cityRank[0] 全字段", cr[0])

print("\n### M. 院线四件套")
show("getChainSumData 5", get("/getChainSumData", {"cinemaChainCode": "5", "date": D}).get("_json"))
j = get("/getChainBoxData", {"cinemaChainCode": "5", "date": D}).get("_json") or {}
print("   chainBox movieCount=%s len=%d" % (j.get("movieCount"), len(j.get("movieSalesList") or [])))
show("getChainYesterday 5", get("/getChainYesterday", {"cinemaChainCode": "5", "geoCode": 1, "geoType": 1}).get("_json"))
j = get("/getChainManageData", {"cinemaChainCode": "5", "dateType": 0}).get("_json")
print("   chainManage n=%s" % (len(j) if isinstance(j, list) else j))

print("\n### N. 5 个历史纪录接口的 title/长度")
for p in ("/getReleaseDayData", "/getNationSingleDayData", "/getNationSingleWeekData",
          "/getNationSingleMonthData", "/getSingleDayData"):
    j = get(p).get("_json") or {}
    rl = j.get("rankList") or []
    print("   %-28s title=%-18s n=%d  item0=%s"
          % (p, j.get("title"), len(rl), json.dumps(rl[0], ensure_ascii=False)[:180] if rl else "-"))

print("\n### O. 204 语义（未开业/无数据日）")
for code, dt in (("11061601", "2017-01-05"), ("11061601", "2017-01-01"), ("33019193", "2016-06-01")):
    for p in ("/getCinemaBoxData", "/getCinemaSumData"):
        r = get(p, {"cinemaCode": code, "date": dt})
        print("   %-22s %s %s -> status=%s bytes=%s json=%s"
              % (p, code, dt, r.get("_status"), r.get("_bytes"),
                 json.dumps(r.get("_json"), ensure_ascii=False)[:120] if r.get("_json") is not None else r.get("_text", "")[:80]))

print("\n### P. 已证伪端点（看错误类型）")
for p in ("/getMovieData", "/getMovieBoxData"):
    r = get(p, {"movieCode": "00100072025"})
    print("   %-18s status=%s err=%s text=%s" % (p, r.get("_status"), r.get("_err"), r.get("_text", "")[:100]))

print("\n### Q. 全量影院榜规模")
t0 = time.time()
r = get("/getCinemaList", {"date": D, "withSvcFee": "true", "limit": 20000, "offset": 0})
j = r.get("_json") or {}
print("   status=%s n=%d hasMore=%s bytes=%s ms=%s"
      % (r.get("_status"), len(j.get("salesRankList") or []), j.get("hasMore"), r.get("_bytes"), r.get("_ms")))

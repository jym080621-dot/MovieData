#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phase0_probe.py — 对 zgdypf.zgdypw.cn 做端点/参数/字段类型实测仲裁。

只读、只探测、不落业务数据；原始响应全部存到 raw_probe/。
目的：裁决前序对话中的 6 处冲突（参数名、geoCode/geoType、getSingleDayData 参数、
salesDesc 类型、updateGapSecond、日期上限），并确认 24 个端点是否真实存在。
"""
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
HEADERS = {
    "User-Agent": UA,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE + "/",
    "Accept": "application/json, text/plain, */*",
}
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_probe")
os.makedirs(OUTDIR, exist_ok=True)

REPORT = []
SEQ = [0]


def shape(v, depth=0, maxdepth=4):
    """把 JSON 压成可读的结构描述：类型 + 短样例。"""
    if depth > maxdepth:
        return "…"
    if isinstance(v, dict):
        return {k: shape(x, depth + 1, maxdepth) for k, x in list(v.items())[:30]}
    if isinstance(v, list):
        if not v:
            return "list[0]"
        return {"__list_len__": len(v), "__item0__": shape(v[0], depth + 1, maxdepth)}
    if isinstance(v, str):
        s = v if len(v) <= 48 else v[:48] + "…"
        return "str(" + str(len(v)) + "):" + s
    if isinstance(v, bool):
        return "bool:" + str(v)
    if isinstance(v, (int, float)):
        return type(v).__name__ + ":" + repr(v)
    if v is None:
        return "null"
    return type(v).__name__ + ":" + repr(v)


def get(path, params=None, label="", save=True):
    """单次请求，不重试；返回 dict(label,status,ms,bytes,obj,raw,error)。"""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    SEQ[0] += 1
    rec = {"n": SEQ[0], "label": label or path, "url": url}
    t0 = time.time()
    body, status, ctype, err = b"", None, None, None
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=25) as r:
            status = r.status
            ctype = r.headers.get("Content-Type")
            body = r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        ctype = e.headers.get("Content-Type") if e.headers else None
        body = e.read()
    except Exception as e:
        err = repr(e)
    rec["status"] = status
    rec["ms"] = int((time.time() - t0) * 1000)
    rec["bytes"] = len(body)
    rec["ctype"] = ctype
    rec["error"] = err
    rec["raw"] = body
    rec["obj"] = None
    if body:
        try:
            rec["obj"] = json.loads(body.decode("utf-8"))
        except Exception:
            rec["obj"] = None
    if save:
        fn = "%02d_%s" % (SEQ[0], "".join(c if c.isalnum() else "_" for c in rec["label"])[:60])
        if rec["obj"] is not None:
            with open(os.path.join(OUTDIR, fn + ".json"), "w", encoding="utf-8") as f:
                json.dump(rec["obj"], f, ensure_ascii=False, indent=1)
        else:
            with open(os.path.join(OUTDIR, fn + ".txt"), "wb") as f:
                f.write(body)
    REPORT.append({k: v for k, v in rec.items() if k != "raw"})
    time.sleep(0.30 + random.random() * 0.25)
    return rec


def brief(rec, keys=None):
    """打印一条探测结果的一行摘要。"""
    st = rec["status"] if rec["status"] is not None else "ERR"
    o = rec["obj"]
    if isinstance(o, dict):
        top = "dict" + str(list(o.keys())[:12])
    elif isinstance(o, list):
        top = "list[%d]" % len(o)
    elif o is None:
        top = "non-json"
    else:
        top = type(o).__name__
    print("  %-46s %-5s %5dms %7dB  %s" % (rec["label"][:46], st, rec["ms"], rec["bytes"], top))
    return rec


def sect(t):
    print("\n" + "=" * 78)
    print("## " + t)
    print("=" * 78)


D_HIST = "2026-11-12"   # 昨日
D_TODAY = "2026-11-13"


def main():
    print("target:", BASE)
    print("outdir:", OUTDIR)

    sect("0. 基础：日期范围 / 全国大盘 / 日期上限")
    brief(get("/getDateRange", None, "getDateRange"))
    n1 = brief(get("/getNational", {"date": D_HIST, "withSvcFee": "true"}, "getNational 昨日"))
    n2 = brief(get("/getNational", {"date": D_TODAY, "withSvcFee": "true"}, "getNational 今日"))
    n3 = brief(get("/getNational", {"date": "2023-06-01", "withSvcFee": "true"}, "getNational 老日期"))

    sect("1. 榜单：salesDesc 类型 / sales 单位 / updateTimeDesc")
    brief(get("/getProvinceList", {"date": D_HIST, "withSvcFee": "true", "limit": 3, "offset": 0},
              "getProvinceList withSvcFee"))
    brief(get("/getProvinceList", {"date": D_HIST, "limit": 3, "offset": 0},
              "getProvinceList 无withSvcFee"))
    cl = brief(get("/getCinemaList", {"date": D_HIST, "withSvcFee": "true", "limit": 3, "offset": 0},
                    "getCinemaList l=3 o=0"))
    brief(get("/getCinemaList", {"date": D_HIST, "withSvcFee": "true", "limit": 3, "offset": 3},
              "getCinemaList l=3 o=3"))
    brief(get("/getCinemaList", {"date": D_HIST, "withSvcFee": "true", "limit": 3, "offset": 0,
                                 "geoCode": 440100, "geoType": 3}, "getCinemaList 城市440100"))
    brief(get("/getCinemaList", {"date": D_HIST, "limit": 3, "geoCode": 440000, "geoType": 2},
              "getCinemaList geoType=2 省级"))

    sect("2. 冲突B：getChainList 的 geoCode/geoType")
    brief(get("/getChainList", {"date": D_HIST, "geoCode": 0, "geoType": 0}, "getChainList 0/0"))
    brief(get("/getChainList", {"date": D_HIST, "geoCode": 1, "geoType": 1}, "getChainList 1/1"))
    brief(get("/getChainList", {"date": D_HIST}, "getChainList 无参数"))
    brief(get("/getChainList", {"date": D_HIST, "geoCode": 1, "geoType": 0}, "getChainList 1/0"))
    brief(get("/getChainList", {"date": D_HIST, "geoCode": 0, "geoType": 1}, "getChainList 0/1"))

    sect("3. 冲突A：searchMovie 参数名")
    s1 = brief(get("/searchMovie", {"movieName": "哪吒之魔童闹海", "limit": 5, "offset": 0},
                   "searchMovie movieName"))
    s2 = brief(get("/searchMovie", {"keyword": "哪吒", "limit": 5, "offset": 0},
                   "searchMovie keyword"))
    brief(get("/searchMovie", {"name": "哪吒", "limit": 5, "offset": 0}, "searchMovie name"))
    brief(get("/searchCinema", {"cinemaName": "英嘉", "limit": 5, "offset": 0}, "searchCinema cinemaName"))

    sect("4. 影院四件套：字段与口径")
    brief(get("/getCinemaSumData", {"cinemaCode": "11061601", "date": D_HIST}, "getCinemaSumData 11061601"))
    brief(get("/getCinemaBoxData", {"cinemaCode": "11061601", "date": D_HIST}, "getCinemaBoxData 11061601"))
    brief(get("/getCinemaManageData", {"cinemaCode": "11061601", "dateType": 0}, "getCinemaManageData dt=0"))
    brief(get("/getCinemaSumData", {"cinemaCode": "33019193", "date": D_HIST}, "getCinemaSumData 33019193"))
    brief(get("/getCinemaBoxData", {"cinemaCode": "33019193", "date": D_HIST}, "getCinemaBoxData 33019193"))

    sect("5. 冲突C：getSingleDayData 是否接受参数")
    a = brief(get("/getSingleDayData", None, "getSingleDayData 无参数"))
    b = brief(get("/getSingleDayData", {"date": "2025-02-01", "withSvcFee": "true", "limit": 100},
                  "getSingleDayData 带参数"))
    c = brief(get("/getSingleDayData", {"date": "2026-01-01", "limit": 5}, "getSingleDayData limit=5"))

    sect("6. 其余数据面端点")
    brief(get("/getDayData", {"date": D_HIST, "withSvcFee": "true"}, "getDayData"))
    brief(get("/getPeriodData", {"date": "2026-11-09", "dateType": 1}, "getPeriodData 周(周一)"))
    brief(get("/getPeriodData", {"date": "2026-11-11", "dateType": 1}, "getPeriodData 周(周三)"))
    brief(get("/getCityData", {"date": D_HIST, "movieCode": "00100072025"}, "getCityData"))
    brief(get("/getYearTabs", {"year": 2025}, "getYearTabs"))
    brief(get("/getYearData", {"year": 2025}, "getYearData"))
    brief(get("/getChainSumData", {"cinemaChainCode": "5", "date": D_HIST}, "getChainSumData 5"))
    brief(get("/getChainBoxData", {"cinemaChainCode": "5", "date": D_HIST}, "getChainBoxData 5"))
    brief(get("/getChainManageData", {"cinemaChainCode": "5", "dateType": 0}, "getChainManageData 5"))
    brief(get("/getChainYesterday", {"cinemaChainCode": "5", "geoCode": 1, "geoType": 1}, "getChainYesterday 5"))
    brief(get("/getReleaseDayData", None, "getReleaseDayData"))
    brief(get("/getNationSingleDayData", None, "getNationSingleDayData"))
    brief(get("/getNationSingleWeekData", None, "getNationSingleWeekData"))
    brief(get("/getNationSingleMonthData", None, "getNationSingleMonthData"))

    sect("7. 证伪：影片维度是否有 JSON 接口")
    for p in ["getMovieData", "getMovieDayData", "getMovieBoxData",
              "getMovieInfoData", "getMovieIndexData", "getMovieBoxSumData"]:
        brief(get(p, {"movieCode": "00100072025"}, p))

    sect("8. SSR：影片详情页")
    rec = get("/movie/detail/00100072025", None, "movie_detail_00100072025", save=False)
    html = rec["raw"].decode("utf-8", "replace")
    print("  status=%s bytes=%d ctype=%s" % (rec["status"], rec["bytes"], rec["ctype"]))
    print("  含 __INITIAL_STATE__:", "__INITIAL_STATE__" in html)
    idx = html.find("__INITIAL_STATE__")
    if idx >= 0:
        print("  片段:", html[idx:idx + 160].replace("\n", " "))
    with open(os.path.join(OUTDIR, "08_movie_detail_00100072025.html"), "w", encoding="utf-8") as f:
        f.write(html)

    sect("9. 字段级结论（自动判定）")
    conc = []

    def add(k, v):
        conc.append((k, v))
        print("  %-38s %s" % (k, v))

    o = n1["obj"] or {}
    sd = o.get("salesDesc")
    add("national.salesDesc 类型", type(sd).__name__ + " -> " + json.dumps(shape(sd), ensure_ascii=False)[:160])
    add("national.updateGapSecond", repr(o.get("updateGapSecond")))
    add("national 顶层键", str(list(o.keys())))

    if isinstance(n3["obj"], dict):
        add("老日期 2023-06-01 national", "OK " + str(n3["obj"].get("salesDesc")))
    if isinstance(n2["obj"], dict):
        add("今日 2026-11-13 可否查", "OK " + str(n2["obj"].get("salesDesc")))
    else:
        add("今日 2026-11-13 可否查", "status=%s" % n2["status"])

    pv = (get("/getProvinceList", {"date": D_HIST, "limit": 1, "offset": 0}, "tmp_prov", save=False)["obj"] or {})
    lst = (pv.get("salesRankList") or [{}])[0] if isinstance(pv, dict) else {}
    add("province.salesRankList[0] 字段", str(list(lst.keys())))
    add("province.salesDesc 类型", type(lst.get("salesDesc")).__name__ + " = " + repr(lst.get("salesDesc")))

    if isinstance(cl["obj"], dict):
        add("cinemaList 顶层键", str(list(cl["obj"].keys())))
        add("cinemaList.hasMore", repr(cl["obj"].get("hasMore")))

    for nm, r in (("0/0", None),):
        pass
    add("chainList 结论", "见上：0/0 vs 1/1 状态码差异")

    add("searchMovie movieName", "status=%s -> %s" % (s1["status"], str(shape(s1["obj"]))[:120]))
    add("searchMovie keyword", "status=%s -> %s" % (s2["status"], str(shape(s2["obj"]))[:120]))

    add("singleDay 无参数", "status=%s len=%s" % (a["status"], len(a["obj"]) if isinstance(a["obj"], list) else shape(a["obj"])[:60]))
    add("singleDay 带参数", "status=%s len=%s" % (b["status"], len(b["obj"]) if isinstance(b["obj"], list) else shape(b["obj"])[:60]))
    add("singleDay limit=5", "status=%s len=%s" % (c["status"], len(c["obj"]) if isinstance(c["obj"], list) else shape(c["obj"])[:60]))

    with open(os.path.join(OUTDIR, "_report.json"), "w", encoding="utf-8") as f:
        json.dump({"report": REPORT, "conclusions": conc}, f, ensure_ascii=False, indent=1)
    print("\n报告已存:", os.path.join(OUTDIR, "_report.json"))


if __name__ == "__main__":
    main()

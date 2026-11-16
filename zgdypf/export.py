#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export.py —— 把库里的结果规范化导出到 output/。

目录约定（按影片分文件夹）：

    output/
    ├── README.txt                      口径说明 + 字段字典
    ├── index.csv                       影片索引（一部一行）
    ├── movies/
    │   └── 00100072025_哪吒之魔童闹海/
    │       ├── daily.csv               逐日票房（标准列）
    │       └── summary.json            汇总 + 地区/院线 Top
    ├── tables/                         （可选）整表导出
    │   ├── national.csv
    │   ├── province.csv
    │   ├── chain.csv
    │   ├── cinema_day_2026-04.csv
    │   ├── cinema_movie_2026-04.csv
    │   └── city.csv
    └── logs/                           采集台账（可选）

格式约定：
  * CSV 一律 UTF-8 **带 BOM**，Excel 双击不乱码；
  * 金额同时给「元」和「万元」，保留 2 位小数，避免二次换算；
  * 比例写成百分数（12.34%），不是小数；
  * 缺失值写空字符串，不写 NULL / nan。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
from typing import Dict, List, Optional

from . import app_root
from . import db
from .ssr import extract_state, movie_payload

ROOT = app_root()
OUT = os.path.join(ROOT, "output")

_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')
SOURCE_NOTE = "中国电影票房（zgdypf.zgdypw.cn，专资办口径，含服务费）"


# ------------------------------------------------------------------ 工具
def sanitize(name: str, limit: int = 48) -> str:
    s = _BAD.sub("_", (name or "").strip())
    s = s.replace(" ", "")
    return s[:limit] or "未命名"


def _pct(v, nd: int = 2) -> str:
    return "" if v is None else f"{v*100:.{nd}f}%"


def _num(v, nd: int = 2) -> str:
    return "" if v is None else f"{v:.{nd}f}"


def _int(v) -> str:
    return "" if v is None else f"{int(v)}"


def _fen_to_yuan(fen) -> Optional[float]:
    return None if fen is None else fen / 100.0


def write_csv(path: str, header: List[str], rows: List[list]) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        n = 0
        for r in rows:
            w.writerow(r)
            n += 1
    return n


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ 影片
def _ssr_extras(movie_code: str, raw_dir: Optional[str]) -> dict:
    """从 raw/movie_detail/<code>.html 里补出 box_sum / 地区Top / 院线Top。"""
    if not raw_dir:
        return {}
    path = os.path.join(raw_dir, "movie_detail", f"{movie_code}.html")
    if not os.path.exists(path):
        return {}
    try:
        html = open(path, encoding="utf-8", errors="replace").read()
        state = extract_state(html)
        if not state:
            return {}
        p = movie_payload(state)
        sums = [{"项目": s.get("title"), "数值": f"{s.get('value')}{s.get('unit')}"}
                for s in p["box_sum"] if isinstance(s, dict)]
        region = [{"城市": s.get("name"), "累计(万元)": s.get("value")}
                  for s in p["box_top"] if isinstance(s, dict) and s.get("title") == "地区"]
        chain = [{"院线": s.get("name"), "累计(万元)": s.get("value")}
                 for s in p["box_top"] if isinstance(s, dict) and s.get("title") == "院线"]
        return {"票房汇总": sums, "地区Top": region, "院线Top": chain}
    except Exception:
        return {}


def export_movie(conn, movie_code: str, out_root: str = OUT,
                 raw_dir: Optional[str] = None) -> Optional[dict]:
    rows = list(conn.execute(
        "SELECT * FROM fact_movie_day WHERE movie_code=? ORDER BY release_days", (movie_code,)))
    if not rows:
        return None
    meta = conn.execute("SELECT movie_name, pub_desc FROM dim_movie WHERE movie_code=?",
                        (movie_code,)).fetchone()
    name = (meta["movie_name"] if meta and meta["movie_name"] else movie_code)
    pub = (meta["pub_desc"] if meta else "") or ""

    folder = f"{movie_code}_{sanitize(name)}"
    mdir = os.path.join(out_root, "movies", folder)

    header = ["日期", "上映第N天", "星期", "单日票房(元)", "单日票房(万元)", "人次",
              "场次", "座位数", "场均人次", "当日票房占比(%)", "排片占比(%)", "上座率(%)"]
    data = []
    total_fen = 0
    for r in rows:
        fen = r["sales_fen"] or 0
        total_fen += fen
        data.append([
            r["show_date"], r["release_days"], r["week"] or "",
            _num(fen / 100.0), _num(fen / 1e6),
            _int(r["sales_count"]), _int(r["session_count"]), _int(r["seat_count"]),
            _int(r["avg_per_session"]),
            _pct(r["sales_rate"]), _pct(r["session_rate"]), _pct(r["seat_rate"]),
        ])
    write_csv(os.path.join(mdir, "daily.csv"), header, data)

    peak = max(rows, key=lambda r: r["sales_fen"] or 0)
    summary = {
        "影片编码": movie_code,
        "片名": name,
        "上映信息": pub,
        "数据来源": SOURCE_NOTE,
        "导出时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "统计": {
            "天数": len(rows),
            "首日": rows[0]["show_date"],
            "末日": rows[-1]["show_date"],
            "累计票房(万元)": round(total_fen / 1e6, 2),
            "首日票房(万元)": round((rows[0]["sales_fen"] or 0) / 1e6, 2),
            "单日最高(万元)": round((peak["sales_fen"] or 0) / 1e6, 2),
            "峰值日期": peak["show_date"],
            "日均(万元)": round(total_fen / 1e6 / max(len(rows), 1), 2),
        },
    }
    summary.update(_ssr_extras(movie_code, raw_dir))
    _write_json(os.path.join(mdir, "summary.json"), summary)

    return {"code": movie_code, "name": name, "dir": mdir, "days": len(rows),
            "total_wan": round(total_fen / 1e6, 2), "first": rows[0]["show_date"],
            "peak_wan": round((peak["sales_fen"] or 0) / 1e6, 2)}


def export_movies(conn, out_root: str = OUT, raw_dir: Optional[str] = None) -> List[dict]:
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT movie_code FROM fact_movie_day ORDER BY movie_code")]
    out = []
    for c in codes:
        r = export_movie(conn, c, out_root, raw_dir)
        if r:
            out.append(r)
    write_index(out, out_root)
    return out


def write_index(items: List[dict], out_root: str = OUT) -> str:
    header = ["影片编码", "片名", "天数", "首日", "累计(万元)", "单日最高(万元)", "文件目录"]
    rows = [[i["code"], i["name"], i["days"], i["first"], f"{i['total_wan']:.2f}",
             f"{i['peak_wan']:.2f}", os.path.relpath(i["dir"], out_root).replace("\\", "/")]
            for i in items]
    path = os.path.join(out_root, "index.csv")
    write_csv(path, header, rows)
    return path


# ------------------------------------------------------------------ 整表
def export_tables(conn, out_root: str = OUT) -> List[str]:
    made = []
    tdir = os.path.join(out_root, "tables")

    rows = []
    for r in conn.execute("SELECT * FROM fact_national ORDER BY date"):
        svc = r["sales_yuan"] or 0
        spl = r["split_sales_yuan"] or 0
        rows.append([r["date"], _num(svc / 1e4), _num(spl / 1e4),
                     _pct(r["svc_fee_ratio"]), _int(r["gap_second"])])
    made.append(write_csv(os.path.join(tdir, "national.csv"),
                          ["日期", "票房(万元)", "分账(万元)", "服务费占比(%)", "更新间隔(秒)"], rows))

    rows = []
    for r in conn.execute("SELECT * FROM fact_province ORDER BY date, sales_fen DESC"):
        rows.append([r["date"], r["code"], r["name"], _num((r["sales_fen"] or 0) / 1e6),
                     r["tickets_desc"] or "", r["sessions_desc"] or "",
                     _int(r["avg_per_session"]), _num(r["avg_seat_output"]),
                     _pct(r["online_rate"])])
    made.append(write_csv(os.path.join(tdir, "province.csv"),
                          ["日期", "省份编码", "省份", "票房(万元)", "人次", "场次",
                           "场均人次", "单座产出", "网售占比(%)"], rows))

    rows = []
    for r in conn.execute("SELECT * FROM fact_chain ORDER BY date, sales_fen DESC"):
        rows.append([r["date"], r["code"], r["name"], _num((r["sales_fen"] or 0) / 1e6),
                     r["tickets_desc"] or "", r["sessions_desc"] or "",
                     _int(r["avg_per_session"]), _num(r["avg_seat_output"]),
                     _pct(r["online_rate"])])
    made.append(write_csv(os.path.join(tdir, "chain.csv"),
                          ["日期", "院线编码", "院线", "票房(万元)", "人次", "场次",
                           "场均人次", "单座产出", "网售占比(%)"], rows))

    # 影院×日 按月份文件，避免单文件过大
    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) FROM fact_cinema_day ORDER BY 1")]
    for m in months:
        rows = []
        for r in conn.execute("""
            SELECT f.date, f.cinema_code, c.cinema_name, c.province, c.city,
                   f.sales_fen, f.sales_src, f.sales_lossy, f.tickets, f.sessions,
                   f.avg_per_session, f.avg_seat_output, f.online_rate, f.movie_count
            FROM fact_cinema_day f LEFT JOIN dim_cinema c ON c.cinema_code=f.cinema_code
            WHERE substr(f.date,1,7)=? ORDER BY f.date, f.sales_fen DESC""", (m,)):
            rows.append([r["date"], r["cinema_code"], r["cinema_name"] or "",
                         r["province"] or "", r["city"] or "",
                         _num((r["sales_fen"] or 0) / 1e6),
                         _int(r["tickets"]), _int(r["sessions"]), _int(r["avg_per_session"]),
                         _num(r["avg_seat_output"]), _pct(r["online_rate"]),
                         _int(r["movie_count"]),
                         "精确" if r["sales_src"] == "box" else "榜单解析",
                         "是" if r["sales_lossy"] else "否"])
        made.append(write_csv(os.path.join(tdir, f"cinema_day_{m}.csv"),
                              ["日期", "影院编码", "影院", "省份", "城市", "票房(万元)", "人次",
                               "场次", "场均", "单座产出", "网售占比(%)", "在映片数",
                               "金额来源", "是否截断"], rows))

    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) FROM fact_cinema_movie ORDER BY 1")]
    for m in months:
        rows = []
        for r in conn.execute("""
            SELECT m.*, c.cinema_name FROM fact_cinema_movie m
            LEFT JOIN dim_cinema c ON c.cinema_code=m.cinema_code
            WHERE substr(m.date,1,7)=? ORDER BY m.date, m.cinema_code, m.sales_fen DESC""", (m,)):
            rows.append([r["date"], r["cinema_code"], r["cinema_name"] or "",
                         r["movie_code"], r["movie_name"] or "", _int(r["release_day"]),
                         _num((r["sales_fen"] or 0) / 100.0),
                         _pct(r["sales_rate"]), _pct(r["session_count_rate"]),
                         r["sum_sales_text"] or ""])
        made.append(write_csv(os.path.join(tdir, f"cinema_movie_{m}.csv"),
                              ["日期", "影院编码", "影院", "影片编码", "片名", "上映第N天",
                               "单日(元)", "占本店(%)", "排片占比(%)", "本店累计"], rows))

    rows = []
    for r in conn.execute("""
        SELECT f.date, f.movie_code, m.movie_name, f.sales_wan, f.sales_rate,
               f.session_rate, f.seat_rate
        FROM fact_movie_national_day f
        LEFT JOIN dim_movie m ON m.movie_code=f.movie_code
        ORDER BY f.date, f.sales_wan DESC"""):
        rows.append([r["date"], r["movie_code"], r["movie_name"] or "",
                     _num(r["sales_wan"]), _pct(r["sales_rate"]),
                     _pct(r["session_rate"]), _pct(r["seat_rate"])])
    made.append(write_csv(os.path.join(tdir, "movie_national_day.csv"),
                          ["日期", "影片编码", "片名", "票房(万元)", "票房占比(%)",
                           "排片占比(%)", "上座率(%)"], rows))

    rows = []
    for r in conn.execute("SELECT * FROM fact_city ORDER BY date, movie_code, value_yuan DESC"):
        rows.append([r["date"], r["movie_code"], r["city_name"], r["city_code"] or "",
                     _num(r["value_yuan"]), _pct(r["rate"])])
    made.append(write_csv(os.path.join(tdir, "city.csv"),
                          ["日期", "影片编码", "城市", "城市编码", "当日票房(元)", "占该片(%)"], rows))
    return made


# ------------------------------------------------------------------ 影院
def export_cinema(conn, code: str, out_root: str = OUT) -> Optional[dict]:
    meta = conn.execute("SELECT cinema_name, province, city FROM dim_cinema WHERE cinema_code=?",
                        (code,)).fetchone()
    name = (meta["cinema_name"] if meta and meta["cinema_name"] else code)
    days = list(conn.execute(
        "SELECT * FROM fact_cinema_day WHERE cinema_code=? AND sales_fen IS NOT NULL "
        "ORDER BY date", (code,)))
    movies = list(conn.execute(
        "SELECT * FROM fact_cinema_movie WHERE cinema_code=? ORDER BY date, sales_fen DESC",
        (code,)))
    if not days and not movies:
        return None

    folder = f"{code}_{sanitize(name)}"
    cdir = os.path.join(out_root, "cinemas", folder)

    if days:
        header = ["日期", "票房(元)", "票房(万元)", "人次", "场次", "场均人次",
                  "单座产出", "网售占比(%)", "在映片数", "金额来源", "是否截断"]
        rows = [[r["date"], _num((r["sales_fen"] or 0) / 100.0),
                 _num((r["sales_fen"] or 0) / 1e6), _int(r["tickets"]), _int(r["sessions"]),
                 _int(r["avg_per_session"]), _num(r["avg_seat_output"]),
                 _pct(r["online_rate"]), _int(r["movie_count"]),
                 "精确" if r["sales_src"] == "box" else "榜单解析",
                 "是" if r["sales_lossy"] else "否"] for r in days]
        write_csv(os.path.join(cdir, "daily.csv"), header, rows)

    if movies:
        header = ["日期", "影片编码", "片名", "上映第N天", "单日(元)",
                  "占本店(%)", "排片占比(%)", "本店累计"]
        rows = [[r["date"], r["movie_code"], r["movie_name"] or "", _int(r["release_day"]),
                 _num((r["sales_fen"] or 0) / 100.0), _pct(r["sales_rate"]),
                 _pct(r["session_count_rate"]), r["sum_sales_text"] or ""] for r in movies]
        write_csv(os.path.join(cdir, "movies.csv"), header, rows)

    total_fen = sum(r["sales_fen"] or 0 for r in days)
    summary = {
        "影院编码": code,
        "影院名称": name,
        "省份": (meta["province"] if meta else "") or "",
        "城市": (meta["city"] if meta else "") or "",
        "数据来源": SOURCE_NOTE,
        "导出时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "统计": {
            "天数": len(days),
            "片目行数": len(movies),
            "票房合计(万元)": round(total_fen / 1e6, 2),
            "日均(万元)": round(total_fen / 1e6 / max(len(days), 1), 2),
            "首日": days[0]["date"] if days else "",
            "末日": days[-1]["date"] if days else "",
        },
    }
    _write_json(os.path.join(cdir, "summary.json"), summary)
    return {"code": code, "name": name, "dir": cdir, "days": len(days),
            "movies": len(movies), "total_wan": round(total_fen / 1e6, 2)}


def export_cinemas(conn, out_root: str = OUT) -> List[dict]:
    codes = [r[0] for r in conn.execute("""
        SELECT DISTINCT cinema_code FROM fact_cinema_day WHERE sales_src='box'
        UNION
        SELECT DISTINCT cinema_code FROM fact_cinema_movie
        ORDER BY 1""")]
    out = []
    for c in codes:
        r = export_cinema(conn, c, out_root)
        if r:
            out.append(r)
    header = ["影院编码", "影院", "天数", "片目行数", "票房合计(万元)", "文件目录"]
    rows = [[i["code"], i["name"], i["days"], i["movies"], f"{i['total_wan']:.2f}",
             os.path.relpath(i["dir"], out_root).replace("\\", "/")] for i in out]
    write_csv(os.path.join(out_root, "cinemas_index.csv"), header, rows)
    return out


# ------------------------------------------------------------------ 说明
README = """中国电影票房数据导出包
================================================================
数据来源：{source}
导出时间：{ts}
数据库：boxoffice.db（金额在库内一律以「分」存储，本导出已换算）

一、目录
----------------------------------------------------------------
index.csv                影片索引（一部一行，含文件目录）
cinemas_index.csv        影院索引（一家一行）
movies/<编码>_<片名>/     每部影片一个文件夹
    daily.csv            逐日票房（按上映第 N 天升序）
    summary.json         汇总、地区 Top、院线 Top（若有原始页面）
cinemas/<编码>_<名称>/    每家影院一个文件夹（仅已采集过的影院）
    daily.csv            逐日票房 / 人次 / 场次 / 网售占比
    movies.csv           该店逐日片目明细
    summary.json         汇总
tables/                  整表导出（可选）

二、字段与单位
----------------------------------------------------------------
daily.csv
    日期                  YYYY-MM-DD
    上映第N天             1 = 首映日；0 或负数 = 点映/尚未正式上映
    星期
    单日票房(元)          精确到分后两位
    单日票房(万元)        元 / 10000，两位小数
    人次 / 场次 / 座位数
    场均人次
    当日票房占比(%)       该片当日票房 / 全国当日票房
    排片占比(%)           该片场次 / 全国场次
    上座率(%)             人次 / 座位数

cinemas/daily.csv
    日期 / 票房(元) / 票房(万元) / 人次 / 场次 / 场均人次 / 单座产出 /
    网售占比(%) / 在映片数 / 金额来源 / 是否截断
    金额来源：精确（影院分片接口求和）或 榜单解析（字符串截断，误差 ≤ 99 元）

cinemas/movies.csv
    日期 / 影片编码 / 片名 / 上映第N天 / 单日(元) / 占本店(%) /
    排片占比(%) / 本店累计（该字段自带单位：纯数字为元，带「万」为万元）

tables/national.csv       全国大盘，含服务费与分账两套口径
tables/province.csv       省份榜（31 省）
tables/chain.csv          院线榜（全国 50 家）
tables/cinema_day_*.csv   影院 × 日；「金额来源」列区分
                          「精确」（分片接口求和）与「榜单解析」（字符串截断，误差 ≤ 99 元）
tables/cinema_movie_*.csv 影院 × 影片 × 日（样本影院）
tables/movie_national_day.csv  影片 × 日（全国）
tables/city.csv           单片城市分布

三、口径提醒
----------------------------------------------------------------
1. 全站为专资办口径，默认含服务费；与猫眼/灯塔差几百万至千万级。
2. 影院榜的 salesDesc 是**向下截断**到 0.01 万（100 元），本包已标注「是否截断」。
3. 当日数据滚动更新，历史日 T+1 定版；建议只取「昨天及以前」。
4. 约 1/3 的历史日期站点影院维度整片缺失，本包不包含这些日期。

四、编码
----------------------------------------------------------------
所有 CSV 为 UTF-8 with BOM，Excel 直接双击打开不乱码；
JSON 为 UTF-8 无 BOM。
"""


def write_readme(out_root: str = OUT) -> str:
    path = os.path.join(out_root, "README.txt")
    os.makedirs(out_root, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(README.format(source=SOURCE_NOTE,
                               ts=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return path


def export_all(out_root: str = OUT, db_path: Optional[str] = None,
               raw_dir: Optional[str] = None, with_tables: bool = True,
               log=None) -> dict:
    """一站式导出：影片 + 索引 + 说明（+ 整表）。返回统计摘要。"""
    conn = db.connect(db_path or os.path.join(ROOT, "boxoffice.db"))
    raw_dir = raw_dir or os.path.join(ROOT, "raw")
    if log:
        log("导出影片…")
    items = export_movies(conn, out_root, raw_dir)
    if log:
        log("导出影院…")
    cins = export_cinemas(conn, out_root)
    files = [write_readme(out_root)]
    if with_tables:
        if log:
            log("导出整表（数据量较大，稍候）…")
        export_tables(conn, out_root)
    conn.close()
    size = 0
    for base, _, fs in os.walk(out_root):
        for f in fs:
            size += os.path.getsize(os.path.join(base, f))
    return {"movies": len(items), "cinemas": len(cins),
            "size_mb": round(size / 1e6, 1), "root": out_root}


if __name__ == "__main__":
    import argparse
    import sys as _sys
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="导出结果为规范化的 CSV / JSON")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--db", default=os.path.join(ROOT, "boxoffice.db"))
    ap.add_argument("--movie", help="只导出指定影片编码")
    ap.add_argument("--no-tables", action="store_true", help="只导影片，不导整表")
    a = ap.parse_args()
    if a.movie:
        conn = db.connect(a.db)
        r = export_movie(conn, a.movie, a.out, os.path.join(ROOT, "raw"))
        write_readme(a.out)
        print("完成：", r)
    else:
        res = export_all(a.out, a.db, os.path.join(ROOT, "raw"), not a.no_tables, print)
        print("完成：", res)

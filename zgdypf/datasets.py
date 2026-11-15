#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""datasets.py —— 24 个数据集的注册表 + 执行引擎（UI 与命令行共用）。

设计：
  * 每个数据集声明自己的「任务粒度」（按日 / 单次 / 按年 / 按周期 / 按影院 / 按院线 / 按影片），
    引擎据此展开任务列表；
  * 执行前自动准备前置数据（样本影院、院线清单、影片清单），缺什么先补什么；
  * 每个任务写 collect_ledger（job = "ds:<id>"），中断后原样续跑；
  * 所有任务返回一行状态字符串，UI 直接展示。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import api, db
from . import sample as samp
from .common import MIN_CINEMAS, date_is_full, daterange, rank_row, to_float
from .client import BadPayload, Client, HardError, RetryExhausted
from .parse import parse_count, parse_money, parse_ratio, unit_money


# ------------------------------------------------------------------ 规格
@dataclass
class Task:
    dataset: str
    key: str
    date: Optional[str] = None
    entity: Optional[str] = None
    label: str = ""


@dataclass
class Spec:
    id: str
    name: str
    group: str
    mode: str          # daily / once / per_year / per_period / per_cinema /
                       # per_chain / per_movie / daily_cinema / daily_chain
    desc: str
    default: bool = False


SPECS: List[Spec] = [
    Spec("date_range", "可查询日期范围", "元信息", "once", "startDate / endDate，无需参数", True),
    Spec("national", "全国大盘（含服务费/分账）", "大盘", "daily", "每日一次，双口径", True),
    Spec("day_data", "全国单日影片榜", "大盘", "daily", "各片当日票房/排片/占比", True),
    Spec("province_list", "省份榜", "榜单", "daily", "31 省当日票房与人次", True),
    Spec("chain_list", "院线榜（全国）", "榜单", "daily", "50 家院线当日票房", True),
    Spec("cinema_list", "影院榜（全量名单）", "榜单", "daily", "万级影院名单，含完整性闸门", True),
    Spec("cinema_sum", "影院日报合计", "影院", "daily_cinema", "逐日逐店：票房/在映片数/场次"),
    Spec("cinema_box", "影院 × 影片 × 日", "影院", "daily_cinema", "核心交叉表（最耗时）", True),
    Spec("cinema_manage", "影院近 14 天走势", "影院", "per_cinema", "一次请求返回近期逐日序列"),
    Spec("search_cinema", "影院搜索（名称→编码）", "搜索", "once", "按关键词反查影院编码与行政区"),
    Spec("chain_sum", "院线日报合计", "院线", "daily_chain", "逐日逐院线"),
    Spec("chain_box", "院线 × 影片 × 日", "院线", "daily_chain", "逐日逐院线的片目明细"),
    Spec("chain_manage", "院线趋势", "院线", "per_chain", "近期逐日序列"),
    Spec("chain_yesterday", "院线昨日对比", "院线", "per_chain", "含环比与排名变化"),
    Spec("city_data", "单片城市分布", "影片", "daily_movie", "逐日 Top N 影片的城市榜"),
    Spec("search_movie", "影片搜索（名称→编码）", "搜索", "once", "按片名反查 movieCode"),
    Spec("movie_detail", "影片详情页（逐日序列）", "影片", "per_movie", "SSR，一部片一次拿全历史", True),
    Spec("period", "周期榜（周/月/年）", "榜单", "per_period", "必须传周期起始日，否则返回空"),
    Spec("year", "年度票房榜", "榜单", "per_year", "按年拉取"),
    Spec("single_day", "影片单日票房纪录", "纪录", "once", "全历史 Top20，参数无效"),
    Spec("release_day", "影片首日票房纪录", "纪录", "once", "全历史 Top20"),
    Spec("nation_day", "大盘单日纪录", "纪录", "once", "全历史 Top20"),
    Spec("nation_week", "大盘单周纪录", "纪录", "once", "全历史 Top20"),
    Spec("nation_month", "大盘单月纪录", "纪录", "once", "全历史 Top20"),
]
BY_ID: Dict[str, Spec] = {s.id: s for s in SPECS}
GROUPS = ["元信息", "大盘", "榜单", "影院", "院线", "影片", "搜索", "纪录"]


# ------------------------------------------------------------------ 上下文
class Ctx:
    def __init__(self, conn, client: Client, opts: dict, emit: Callable[[str], None]):
        self.conn = conn
        self.client = client
        self.opts = opts
        self.emit = emit
        self._sample: Optional[List[str]] = None
        self._chains: Optional[List[tuple]] = None
        self._movies: Dict[str, List[tuple]] = {}

    # ---- 基础参数
    @property
    def days(self) -> List[str]:
        return daterange(self.opts["start"], self.opts["end"])

    @property
    def ref_date(self) -> str:
        return self.opts["end"]

    # ---- 前置数据
    def _ensure_provinces(self, date: str) -> List[dict]:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT code,name FROM fact_province WHERE date=? ORDER BY code", (date,))]
        if rows:
            return rows
        pv = api.province_list(self.client, date, True, 50, 0)
        out = []
        for r in (pv.get("salesRankList") or []):
            out.append({"code": r.get("code"), "name": r.get("name")})
            db.upsert_province(self.conn, rank_row(r, date))
        self.conn.commit()
        return out or [dict(r) for r in self.conn.execute(
            "SELECT code,name FROM fact_province ORDER BY date DESC, code")]

    def ensure_sample(self) -> List[str]:
        if self._sample is not None:
            return self._sample
        codes = [r[0] for r in self.conn.execute(
            "SELECT cinema_code FROM dim_cinema WHERE sample=1 ORDER BY province_code, cinema_code")]
        if codes:
            self._sample = codes
            return codes

        n = int(self.opts.get("n_sample", 150))
        date = self._find_listed_date()
        rows = [dict(r) for r in self.conn.execute(
            "SELECT cinema_code AS code, cinema_name AS name, list_sales_fen AS sales "
            "FROM dim_cinema WHERE list_sales_fen IS NOT NULL")]
        if not rows:
            self.emit(f"  样本不存在，先拉 {date} 全量影院榜…")
            self._run_cinema_list(date)
            rows = [dict(r) for r in self.conn.execute(
                "SELECT cinema_code AS code, cinema_name AS name, list_sales_fen AS sales "
                "FROM dim_cinema WHERE list_sales_fen IS NOT NULL")]
        provs = self._ensure_provinces(date)
        chosen = samp.build_sample(rows, provs, n)
        for item in chosen:
            self.conn.execute(
                "INSERT INTO dim_cinema(cinema_code,cinema_name,province,province_code,city,"
                "is_direct_city,first_seen_date,sample,stratum,list_sales_fen,updated_at) "
                "VALUES(?,?,?,?,?,?,?,1,?,?,?) ON CONFLICT(cinema_code) DO UPDATE SET "
                "sample=1, stratum=excluded.stratum, province=COALESCE(excluded.province,dim_cinema.province),"
                "list_sales_fen=COALESCE(excluded.list_sales_fen,dim_cinema.list_sales_fen)",
                (item["code"], item.get("name"), item.get("province"), None, item.get("city"),
                 item.get("is_direct_city") or 0, date, item.get("stratum"),
                 item.get("sales"), db.now()))
        if self.opts.get("correct_codes"):
            self.emit(f"  校正 {len(chosen)} 家影院的行政区码…")
            for item in chosen:
                try:
                    res = api.search_cinema(self.client, item.get("name") or "")
                    hit = next((x for x in res if x.get("code") == item["code"]), None)
                    if hit and hit.get("cityCode"):
                        pc = samp.province_by_code(hit["cityCode"], provs)
                        self.conn.execute(
                            "UPDATE dim_cinema SET province_code=?, province=COALESCE(?,province) "
                            "WHERE cinema_code=?", (samp.province_by_code(hit["cityCode"], provs),
                                                    pc, item["code"]))
                except (HardError, RetryExhausted):
                    pass
        self.conn.commit()
        self._sample = [c["code"] for c in chosen]
        self.emit(f"  样本已建立：{len(self._sample)} 家")
        return self._sample

    def _find_listed_date(self) -> str:
        r = self.conn.execute(
            "SELECT date FROM date_status WHERE status='full' ORDER BY date DESC LIMIT 1").fetchone()
        return r[0] if r else self.ref_date

    def chains(self) -> List[tuple]:
        if self._chains is not None:
            return self._chains
        date = self.ref_date
        rows = [tuple(r) for r in self.conn.execute(
            "SELECT code,name FROM fact_chain WHERE date=? ORDER BY sales_fen DESC", (date,))]
        if not rows:
            data = api.chain_list(self.client, date, 0, 0)
            rows = []
            for r in (data.get("salesRankList") or []):
                rows.append((str(r.get("code")), r.get("name")))
                db.upsert_chain(self.conn, rank_row(r, date))
            self.conn.commit()
        n = int(self.opts.get("top_chains", 10))
        self._chains = rows[:n] if n > 0 else rows
        return self._chains

    def movies(self, date: str) -> List[tuple]:
        if date in self._movies:
            return self._movies[date]
        rows = [tuple(r) for r in self.conn.execute(
            "SELECT movie_code,movie_name FROM fact_movie_national_day WHERE date=? "
            "ORDER BY sales_wan DESC", (date,))]
        if not rows:
            data = api.day_data(self.client, date)
            for r in (data.get("list") or []):
                sw = parse_money(r.get("salesInWanDesc"), "wan")
                db.upsert(self.conn, "fact_movie_national_day", {
                    "date": date, "movie_code": r.get("code"), "movie_name": r.get("name"),
                    "release_days": r.get("releaseDays"),
                    "sales_wan": sw.yuan / 1e4 if sw else None,
                })
            self.conn.commit()
            rows = [tuple(r) for r in self.conn.execute(
                "SELECT movie_code,movie_name FROM fact_movie_national_day WHERE date=? "
                "ORDER BY sales_wan DESC", (date,))]
        n = int(self.opts.get("top_movies", 10))
        self._movies[date] = rows[:n] if n > 0 else rows
        return self._movies[date]

    def resolve_cinemas(self, text: str) -> List[tuple]:
        """把「影院名或编码，逗号/空格分隔」解析成 [(code, name)]。

        8 位纯数字直接当专资办编码；否则调 /searchCinema 反查。
        影院重名极多（「英嘉」全国 5 家），命中多家时取第一条并打印候选，避免静默取错。
        """
        out, seen = [], set()
        for raw in re.split(r"[,，;；、\s]+", (text or "").strip()):
            raw = raw.strip()
            if not raw or raw in seen:
                continue
            seen.add(raw)
            if raw.isdigit() and 6 <= len(raw) <= 10:
                out.append((raw, raw))
                continue
            try:
                res = api.search_cinema(self.client, raw, 20)
            except (HardError, RetryExhausted):
                self.emit(f"    影院「{raw}」搜索失败")
                continue
            if not res:
                self.emit(f"    影院「{raw}」未找到")
                continue
            hit = next((x for x in res if x.get("name") == raw), res[0])
            code = hit.get("code")
            if not code:
                continue
            if len(res) > 1:
                names = "；".join(f"{x.get('name')}({x.get('code')})" for x in res[:4])
                self.emit(f"    「{raw}」命中 {len(res)} 家，取第 1 家：{hit.get('name')}（{code}）")
                self.emit(f"      其余：{names}")
            out.append((str(code), hit.get("name") or raw))
        return out

    def target_cinemas(self) -> List[tuple]:
        """本次要采的影院：优先「指定影院」，否则用分层样本。

        特殊值 `*`（或 ALL / 全部）表示库里已知的全部影院（约 1.2 万家），
        用于「全量片目」这种重活；此时务必配合断点续跑，跑不完下次接着跑。
        """
        q = (self.opts.get("cinema_query") or "").strip()
        if self.opts.get("all_cinemas") or q in ("*", "ALL", "all", "全部"):
            rows = [tuple(r) for r in self.conn.execute(
                "SELECT cinema_code, cinema_name FROM dim_cinema "
                "WHERE list_sales_fen IS NOT NULL ORDER BY cinema_code")]
            self.emit(f"  全量影院：{len(rows):,} 家")
            return rows
        if q:
            return self.resolve_cinemas(q)
        return [(c, c) for c in self.ensure_sample()]

    def resolve_movies(self, text: str) -> List[tuple]:
        """把「片名或编码，逗号/空格分隔」解析成 [(code, name)]。

        纯数字（8~12 位）当作 movieCode 直接用；否则调 /searchMovie 反查，
        同名优先，其次取第一条。任一失败只跳过该条，不影响其余。
        """
        out, seen = [], set()
        for raw in re.split(r"[,，;；、\s]+", (text or "").strip()):
            raw = raw.strip()
            if not raw or raw in seen:
                continue
            seen.add(raw)
            if raw.isdigit() and 8 <= len(raw) <= 12:
                out.append((raw, raw))
                continue
            try:
                res = api.search_movie(self.client, raw, 5)
            except (HardError, RetryExhausted):
                self.emit(f"    影片「{raw}」搜索失败")
                continue
            if not res:
                self.emit(f"    影片「{raw}」未找到")
                continue
            hit = next((x for x in res if x.get("name") == raw), res[0])
            code = hit.get("code")
            if code:
                out.append((code, hit.get("name") or raw))
                if hit.get("name") != raw:
                    self.emit(f"    「{raw}」→ {hit.get('name')}（{code}）")
        return out

    # ---- 内部：拉一天全量影院榜（供样本建立复用）
    def _run_cinema_list(self, date: str) -> str:
        return _run_cinema_list(self, Task("cinema_list", date, date=date))


# ------------------------------------------------------------------ 单任务执行
def _finish(conn, dataset: str, task: Task, status: str) -> str:
    db.ledger_put(conn, f"ds:{dataset}", task.key, status)
    return status


def _run_date_range(ctx: Ctx, t: Task) -> str:
    d = api.date_range(ctx.client)
    db.put_doc(ctx.conn, "date_range", "range", None, d)
    return f"{d.get('startDate')} ~ {d.get('endDate')}"


def _run_national(ctx: Ctx, t: Task) -> str:
    date = t.date
    svc = api.national(ctx.client, date, True)
    spl = api.national(ctx.client, date, False)
    m_svc = unit_money(svc.get("salesDesc"))
    m_spl = unit_money(spl.get("splitSalesDesc"))
    db.upsert_national(ctx.conn, {
        "date": date,
        "sales_yuan": m_svc.yuan if m_svc else None,
        "split_sales_yuan": m_spl.yuan if m_spl else None,
        "svc_fee_ratio": (1 - m_spl.yuan / m_svc.yuan) if (m_svc and m_spl and m_svc.yuan) else None,
        "gap_second": svc.get("updateGapSecond"),
        "update_ts": svc.get("updateTimestamp"),
        "ingested_at": db.now(),
    })
    return f"{m_svc.yuan/1e4:,.0f} 万" if m_svc else "空"


def _run_day_data(ctx: Ctx, t: Task) -> str:
    date = t.date
    data = api.day_data(ctx.client, date)
    rows = data.get("list") or []
    for r in rows:
        sw = parse_money(r.get("salesInWanDesc"), "wan")
        sp = parse_money(r.get("splitSalesInWanDesc"), "wan")
        db.upsert(ctx.conn, "fact_movie_national_day", {
            "date": date, "movie_code": r.get("code"), "movie_name": r.get("name"),
            "release_days": r.get("releaseDays"),
            "sales_wan": sw.yuan / 1e4 if sw else None,
            "sales_rate": parse_ratio(r.get("salesRateDesc")),
            "session_rate": parse_ratio(r.get("sessionRateDesc")),
            "seat_rate": parse_ratio(r.get("seatRateDesc")),
            "online_rate": parse_ratio(r.get("onlineSalesRateDesc")),
            "sum_sales_text": r.get("sumSalesDesc"),
            "split_sales_wan": sp.yuan / 1e4 if sp else None,
            "split_sales_rate": parse_ratio(r.get("splitSalesRateDesc")),
            "split_online_rate": parse_ratio(r.get("splitOnlineSalesRateDesc")),
        })
    return f"{len(rows)} 部"


def _run_province(ctx: Ctx, t: Task) -> str:
    data = api.province_list(ctx.client, t.date, True, 50, 0)
    rows = data.get("salesRankList") or []
    for r in rows:
        db.upsert_province(ctx.conn, rank_row(r, t.date))
    return f"{len(rows)} 省"


def _run_chain_list(ctx: Ctx, t: Task) -> str:
    data = api.chain_list(ctx.client, t.date, 0, 0)
    rows = data.get("salesRankList") or []
    for r in rows:
        db.upsert_chain(ctx.conn, rank_row(r, t.date))
    return f"{len(rows)} 家院线"


def _run_cinema_list(ctx: Ctx, t: Task) -> str:
    date = t.date
    rows, truncated = api.cinema_list_all(ctx.client, date, page=20000, max_pages=3)
    if len(rows) < MIN_CINEMAS or truncated:
        db.put_date_status(ctx.conn, date, len(rows), "thin", f"{len(rows)} 家")
        db.purge_ranklist_day(ctx.conn, date)
        return f"⚠ 仅 {len(rows)} 家，跳过"
    db.put_date_status(ctx.conn, date, len(rows), "full")
    db.purge_ranklist_day(ctx.conn, date)

    provs = [(r["code"], r["name"]) for r in ctx._ensure_provinces(date)]
    aliases = samp.build_alias_map([{"name": n, "code": c} for c, n in provs])
    dim, facts = [], []
    for r in rows:
        info = samp.parse_province_city(r.get("name") or "", aliases)
        pcode = None
        if info.get("province"):
            for c, n in provs:
                if n == info["province"]:
                    pcode = str(c)
                    break
        rr = rank_row(r, date)
        dim.append({
            "cinema_code": r.get("code"), "cinema_name": r.get("name"),
            "province": info.get("province"), "province_code": pcode,
            "city": info.get("city"), "is_direct_city": info.get("is_direct_city"),
            "first_seen_date": date, "list_sales_fen": rr["sales_fen"],
        })
        facts.append({
            "cinema_code": r.get("code"), "date": date,
            "sales_fen": rr["sales_fen"], "sales_src": "ranklist",
            "sales_lossy": rr["sales_lossy"], "rank_sales_fen": rr["sales_fen"],
            "tickets": rr["_tickets"], "tickets_desc": rr["tickets_desc"],
            "tickets_lossy": rr["_tickets_lossy"],
            "sessions": rr["_sessions"], "sessions_desc": rr["sessions_desc"],
            "sessions_lossy": rr["_sessions_lossy"],
            "online_rate": rr["online_rate"],
            "online_rate_desc": r.get("onlineSalesRateDesc"),
            "avg_seat_output": rr["avg_seat_output"],
            "avg_seat_desc": r.get("avgSalesPerSeatDesc"),
            "avg_per_session": rr["avg_per_session"],
        })
    db.upsert_dim_cinema(ctx.conn, dim)
    for f in facts:
        db.upsert_cinema_day(ctx.conn, f)
    return f"{len(rows):,} 家"


def _run_cinema_sum(ctx: Ctx, t: Task) -> str:
    s = api.cinema_sum(ctx.client, t.entity, t.date)
    m = unit_money(s.get("salesDesc"))
    q = s.get("nationalQuotaInfo") or {}
    db.upsert(ctx.conn, "fact_cinema_sum", {
        "cinema_code": t.entity, "date": t.date,
        "sales_yuan": m.yuan if m else None,
        "sales_cmp": m.cmp if m else None,
        "movie_count": q.get("movieCount"), "new_movie_count": q.get("newMovieCount"),
        "session_count": q.get("sessionCount"),
    })
    if not m:
        return "无数据"
    if m.cmp == "<":
        return f"<{m.yuan:,.0f} 元（上界）"
    return f"{m.yuan:,.0f} 元"


def _run_cinema_box(ctx: Ctx, t: Task) -> str:
    box = api.cinema_box(ctx.client, t.entity, t.date)
    if box is None:
        return "无数据"
    movies = box.get("movieSalesList") or []
    mcount = box.get("movieCount")
    db.clear_cinema_day_movies(ctx.conn, t.entity, t.date, mcount)
    total = 0
    for m in movies:
        fen = int(m["sales"]) if isinstance(m.get("sales"), (int, float)) else None
        total += fen or 0
        sm = parse_money(m.get("sumSalesDesc"))
        db.upsert_cinema_movie(ctx.conn, {
            "cinema_code": t.entity, "date": t.date,
            "movie_code": m.get("movieCode"), "movie_name": m.get("movieName"),
            "release_day": m.get("releaseDay"), "sales_fen": fen,
            "sales_rate": parse_ratio(m.get("salesRateDesc")),
            "session_count_rate": parse_ratio(m.get("sessionCountRateDesc")),
            "sum_sales_text": m.get("sumSalesDesc"),
            "sum_sales_yuan": sm.yuan if sm else None,
            "movie_count": mcount,
        })
    db.upsert_cinema_day(ctx.conn, {
        "cinema_code": t.entity, "date": t.date, "sales_fen": total, "sales_src": "box",
        "sales_lossy": 0, "sessions": box.get("sessionCount"), "sessions_lossy": 0,
        "movie_count": mcount, "new_movie_count": box.get("newMovieCount"),
    })
    return f"{len(movies)} 部 / {total/100:,.0f} 元"


def _run_cinema_manage(ctx: Ctx, t: Task) -> str:
    rows = api.cinema_manage(ctx.client, t.entity)
    for r in rows:
        db.upsert(ctx.conn, "fact_cinema_trend", {
            "cinema_code": t.entity, "show_date": r.get("dateUp"), "week": r.get("dateDown"),
            "sales_fen": int(r["sales"]) if isinstance(r.get("sales"), (int, float)) else None,
            "tickets_desc": r.get("salesCountDesc"), "sessions_desc": r.get("showCountDesc"),
            "avg_per_session": r.get("avgSalesCount"),
            "avg_seat_output": to_float(r.get("avgSalesPerSeatDesc")),
            "online_rate": parse_ratio(r.get("onlineSalesRateDesc")),
        })
    return f"{len(rows)} 天"


def _run_chain_sum(ctx: Ctx, t: Task) -> str:
    s = api.chain_sum(ctx.client, t.entity, t.date)
    m = unit_money(s.get("salesDesc"))
    q = s.get("nationalQuotaInfo") or {}
    name = dict(ctx.chains()).get(t.entity) if ctx.chains() else None
    db.upsert(ctx.conn, "fact_chain_day", {
        "date": t.date, "chain_code": t.entity, "name": name,
        "sales_fen": m.fen if m else None,
        "movie_count": q.get("movieCount"), "new_movie_count": q.get("newMovieCount"),
        "session_count": q.get("sessionCount"),
    })
    return f"{m.yuan/1e4:,.1f} 万" if m else "空"


def _run_chain_box(ctx: Ctx, t: Task) -> str:
    box = api.chain_box(ctx.client, t.entity, t.date)
    if box is None:
        return "无数据"
    movies = box.get("movieSalesList") or []
    for m in movies:
        db.upsert(ctx.conn, "fact_chain_movie", {
            "date": t.date, "chain_code": t.entity,
            "movie_code": m.get("movieCode"), "movie_name": m.get("movieName"),
            "release_day": m.get("releaseDay"),
            "sales_fen": int(m["sales"]) if isinstance(m.get("sales"), (int, float)) else None,
            "sales_rate": parse_ratio(m.get("salesRateDesc")),
            "session_rate": parse_ratio(m.get("sessionCountRateDesc")),
            "sum_sales_text": m.get("sumSalesDesc"),
        })
    return f"{len(movies)} 部"


def _run_chain_manage(ctx: Ctx, t: Task) -> str:
    rows = api.chain_manage(ctx.client, t.entity)
    for r in rows:
        db.upsert(ctx.conn, "fact_chain_trend", {
            "chain_code": t.entity, "show_date": r.get("dateUp"), "week": r.get("dateDown"),
            "sales_fen": int(r["sales"]) if isinstance(r.get("sales"), (int, float)) else None,
            "tickets_desc": r.get("salesCountDesc"), "sessions_desc": r.get("showCountDesc"),
            "avg_per_session": r.get("avgSalesCount"),
            "avg_seat_output": to_float(r.get("avgSalesPerSeatDesc")),
            "online_rate": parse_ratio(r.get("onlineSalesRateDesc")),
        })
    return f"{len(rows)} 天"


def _run_chain_yesterday(ctx: Ctx, t: Task) -> str:
    v = api.chain_yesterday(ctx.client, t.entity)
    db.put_doc(ctx.conn, "chain_yesterday", t.entity, None, v)
    return f"{v.get('name') or '-'} 排名 {v.get('rank') or '-'}"


def _run_city(ctx: Ctx, t: Task) -> str:
    data = api.city_data(ctx.client, t.date, t.entity)
    rows = data.get("cityRank") or []
    for r in rows:
        if not r.get("name"):
            continue
        db.upsert(ctx.conn, "fact_city", {
            "date": t.date, "movie_code": t.entity,
            "city_name": r.get("name"), "city_code": r.get("code"),
            "value_yuan": r.get("value"), "rate": parse_ratio(r.get("rateDesc")),
        })
    return f"{len(rows)} 城"


def _run_search_cinema(ctx: Ctx, t: Task) -> str:
    q = (ctx.opts.get("cinema_query") or "").strip() or ctx.opts.get("query") or "英嘉"
    res = api.search_cinema(ctx.client, q)
    db.put_doc(ctx.conn, "search_cinema", q, None, res)
    for r in res:
        ctx.conn.execute(
            "INSERT INTO dim_cinema(cinema_code,cinema_name,province,province_code,city,"
            "first_seen_date,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(cinema_code) DO UPDATE SET cinema_name=excluded.cinema_name",
            (r.get("code"), r.get("name"), None, None, None, None, db.now()))
    head = "；".join(f"{r.get('name')}({r.get('code')})" for r in res[:2])
    return f"{len(res)} 条" + (f"：{head}" if head else "")


def _run_search_movie(ctx: Ctx, t: Task) -> str:
    mq = (ctx.opts.get("movie_query") or "").strip()
    parts = [p for p in re.split(r"[,，;；、]+", mq) if p.strip()] if mq else []
    if not parts:
        parts = [ctx.opts.get("query") or "哪吒"]
    total, names = 0, []
    for q in parts:
        res = api.search_movie(ctx.client, q)
        db.put_doc(ctx.conn, "search_movie", q, None, res)
        for r in res:
            db.upsert(ctx.conn, "dim_movie", {
                "movie_code": r.get("code"), "movie_name": r.get("name"),
                "pub_desc": r.get("pubDesc"), "first_seen": db.now()})
        total += len(res)
        if res:
            names.append(f"{res[0].get('name')}（{res[0].get('code')}）")
    return f"{total} 条" + ("：" + "、".join(names[:3]) if names else "")


def _run_movie_detail(ctx: Ctx, t: Task) -> str:
    state, payload = api.movie_detail(ctx.client, t.entity)
    if state is None:
        return "SSR 解析失败"
    info = payload.get("info") or {}
    db.upsert(ctx.conn, "dim_movie", {
        "movie_code": t.entity, "movie_name": info.get("name"),
        "pub_desc": info.get("pubDesc"), "first_seen": db.now()})
    n = 0
    for r in (payload.get("index") or []):
        if not isinstance(r, dict) or r.get("sales") is None:
            continue
        db.upsert_movie_day(ctx.conn, {
            "movie_code": t.entity, "show_date": r.get("showDate"),
            "release_days": r.get("releaseDays"), "sales_fen": r.get("sales"),
            "sales_count": r.get("salesCount"), "session_count": r.get("sessionCount"),
            "seat_count": r.get("seatCount"),
            "sales_rate": parse_ratio(r.get("salesRateDesc")),
            "session_rate": parse_ratio(r.get("sessionRateDesc")),
            "seat_rate": parse_ratio(r.get("seatRateDesc")),
            "avg_per_session": r.get("avgSalesCount"), "week": r.get("weekDate"),
        })
        n += 1
    return f"{info.get('name') or ''} {n} 天"


def _run_period(ctx: Ctx, t: Task) -> str:
    date_type = int(ctx.opts.get("period_type", 1))
    data = api.period_data(ctx.client, t.date, date_type)
    rows = data.get("list") or []
    for r in rows:
        sw = parse_money(r.get("salesInWanDesc"), "wan")
        sp = parse_money(r.get("splitSalesInWanDesc"), "wan")
        db.upsert(ctx.conn, "fact_period_movie", {
            "period_type": date_type, "period_start": t.date,
            "movie_code": r.get("code"), "movie_name": r.get("name"),
            "release_date": r.get("releaseDate"),
            "sales_wan": sw.yuan / 1e4 if sw else None,
            "sales_rate": parse_ratio(r.get("salesRateDesc")),
            "avg_price": r.get("avgPrice"), "avg_split_price": r.get("avgSplitPrice"),
            "avg_per_session": r.get("avgSalesCount"),
            "split_sales_wan": sp.yuan / 1e4 if sp else None,
            "split_sales_rate": parse_ratio(r.get("splitSalesRateDesc")),
        })
    return f"{len(rows)} 部" if rows else "空（非周期起始日）"


def _run_year(ctx: Ctx, t: Task) -> str:
    year = int(t.entity)
    tabs = api.year_tabs(ctx.client, year)
    db.put_doc(ctx.conn, "year_tabs", year, None, tabs)
    data = api.year_data(ctx.client, year)
    rows = data.get("rankList") or []
    for r in rows:
        db.upsert(ctx.conn, "fact_year_movie", {
            "year": year, "movie_code": r.get("movieCode"), "movie_name": r.get("name"),
            "premiere_date": r.get("premiereDate"),
            "sales_wan": to_float(r.get("salesInWan")),
            "avg_price": r.get("avgPrice"), "avg_per_session": r.get("avgSalesCount"),
            "rank": r.get("rank"),
        })
    return f"{len(rows)} 部"


def _run_history(ctx: Ctx, t: Task) -> str:
    fn = {
        "single_day": api.single_day,
        "release_day": api.release_day,
        "nation_day": api.nation_single_day,
        "nation_week": api.nation_single_week,
        "nation_month": api.nation_single_month,
    }[t.dataset]
    data = fn(ctx.client)
    rows = data.get("rankList") or []
    for r in rows:
        db.upsert(ctx.conn, "fact_history", {
            "dataset": t.dataset, "rank": r.get("rank"),
            "title": r.get("name") or r.get("weekNum") or r.get("showDate"),
            "show_date": r.get("showDate") or r.get("startDate"),
            "week": r.get("week"),
            "release_days": r.get("releaseDays"),
            "sales_wan": to_float(r.get("salesInWan") or r.get("releaseDaySalesInWan")),
            "sales_count_wan": to_float(r.get("salesCountInWan")),
            "session_count": r.get("sessionCount"),
            "movie_code": r.get("movieCode"),
            "extra": json.dumps({"title": data.get("title"), "end": r.get("endDate")},
                                ensure_ascii=False),
        })
    return f"{data.get('title') or t.dataset} {len(rows)} 条"


RUNNERS: Dict[str, Callable[[Ctx, Task], str]] = {
    "date_range": _run_date_range, "national": _run_national, "day_data": _run_day_data,
    "province_list": _run_province, "chain_list": _run_chain_list, "cinema_list": _run_cinema_list,
    "cinema_sum": _run_cinema_sum, "cinema_box": _run_cinema_box, "cinema_manage": _run_cinema_manage,
    "search_cinema": _run_search_cinema, "chain_sum": _run_chain_sum, "chain_box": _run_chain_box,
    "chain_manage": _run_chain_manage, "chain_yesterday": _run_chain_yesterday,
    "city_data": _run_city, "search_movie": _run_search_movie, "movie_detail": _run_movie_detail,
    "period": _run_period, "year": _run_year, "single_day": _run_history,
    "release_day": _run_history, "nation_day": _run_history, "nation_week": _run_history,
    "nation_month": _run_history,
}


# ------------------------------------------------------------------ 任务展开
def build_tasks(ctx: Ctx, ds_id: str) -> List[Task]:
    spec = BY_ID[ds_id]
    days = ctx.days
    m = spec.mode
    if m == "once":
        return [Task(ds_id, "once")]
    if m == "daily":
        return [Task(ds_id, d, date=d) for d in days]
    if m == "per_year":
        years = sorted({int(d[:4]) for d in days})
        return [Task(ds_id, str(y), entity=str(y)) for y in years]
    if m == "per_period":
        pt = int(ctx.opts.get("period_type", 1))
        starts = []
        if pt == 1:
            d0 = dt.date.fromisoformat(ctx.opts["start"])
            d0 -= dt.timedelta(days=d0.weekday())
            d1 = dt.date.fromisoformat(ctx.opts["end"])
            while d0 <= d1:
                starts.append(d0.isoformat())
                d0 += dt.timedelta(days=7)
        elif pt == 2:
            y, mo = int(ctx.opts["start"][:4]), int(ctx.opts["start"][5:7])
            end = dt.date.fromisoformat(ctx.opts["end"])
            while dt.date(y, mo, 1) <= end:
                starts.append(f"{y:04d}-{mo:02d}-01")
                mo += 1
                if mo > 12:
                    mo, y = 1, y + 1
        else:
            for y in range(int(ctx.opts["start"][:4]), int(ctx.opts["end"][:4]) + 1):
                starts.append(f"{y:04d}-01-01")
        return [Task(ds_id, s, date=s) for s in starts]
    if m == "per_cinema":
        return [Task(ds_id, c, entity=c, label=n or c) for c, n in ctx.target_cinemas()]
    if m == "per_chain":
        return [Task(ds_id, c, entity=c) for c, _ in ctx.chains()]
    if m == "per_movie":
        mq = (ctx.opts.get("movie_query") or "").strip()
        if mq:
            pairs = ctx.resolve_movies(mq)
            return [Task(ds_id, c, entity=c, label=n or c) for c, n in pairs]
        return [Task(ds_id, c, entity=c) for c, _ in ctx.movies(ctx.ref_date)]
    if m == "daily_cinema":
        targets = ctx.target_cinemas()
        n = len(days) * len(targets)
        if n > 100000:
            ctx.emit(f"  ⚠ 任务量 {n:,}（{len(targets):,} 家 × {len(days)} 天），"
                     f"按 1.4 req/s 约需 {n/1.4/3600:.0f} 小时；建议分批 + 断点续跑")
        return [Task(ds_id, f"{d}|{c}", date=d, entity=c, label=n2 or c)
                for d in days for c, n2 in targets]
    if m == "daily_chain":
        chains = ctx.chains()
        return [Task(ds_id, f"{d}|{c}", date=d, entity=c) for d in days for c, _ in chains]
    if m == "daily_movie":
        out = []
        for d in days:
            for c, _ in ctx.movies(d):
                out.append(Task(ds_id, f"{d}|{c}", date=d, entity=c))
        return out
    return []


# ------------------------------------------------------------------ 估算（不发请求）
def estimate(selected: List[str], opts: dict, sample_n: int = 0) -> tuple:
    """纯算术估算请求数与耗时，用于 UI 的「估算」按钮。返回 (requests, detail)。"""
    days = max(len(daterange(opts["start"], opts["end"])), 1)
    n_sample = sample_n or int(opts.get("n_sample", 150))
    top_m = int(opts.get("top_movies", 10))
    top_c = int(opts.get("top_chains", 10))
    pt = int(opts.get("period_type", 1))
    y0, y1 = int(opts["start"][:4]), int(opts["end"][:4])
    n_year = y1 - y0 + 1
    if pt == 1:
        n_period = days // 7 + 1
    elif pt == 2:
        n_period = (y1 - y0) * 12 + int(opts["end"][5:7]) - int(opts["start"][5:7]) + 1
    else:
        n_period = n_year
    n_movie = top_m
    mq = (opts.get("movie_query") or "").strip()
    if mq:
        n_movie = max(1, len([x for x in re.split(r"[,，;；、\s]+", mq) if x.strip()]))
    n_cin = n_sample
    cq = (opts.get("cinema_query") or "").strip()
    if opts.get("all_cinemas") or cq in ("*", "ALL", "all", "全部"):
        n_cin = 12588          # 全量影院量级，实测 2026-04-30 为 12,588 家
    elif cq:
        n_cin = max(1, len([x for x in re.split(r"[,，;；、\s]+", cq) if x.strip()]))
    per = {
        "once": 1, "daily": days, "per_year": n_year, "per_period": max(n_period, 1),
        "per_cinema": n_cin, "per_chain": top_c, "per_movie": n_movie,
        "daily_cinema": days * n_cin, "daily_chain": days * top_c,
        "daily_movie": days * top_m,
    }
    total = 0
    detail = []
    for sid in selected:
        spec = BY_ID.get(sid)
        if not spec:
            continue
        k = per.get(spec.mode, 0)
        total += k
        detail.append(f"{spec.name} {k}")
    return total, detail


# ------------------------------------------------------------------ 引擎
class Engine:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.stop = threading.Event()

    def plan(self, selected: List[str]) -> List[tuple]:
        """返回 [(dataset, [Task])]，会触发前置数据准备。"""
        needs = {BY_ID[s].mode for s in selected}
        if needs & {"per_cinema", "daily_cinema"}:
            self.ctx.ensure_sample()
        if needs & {"per_chain", "daily_chain"}:
            self.ctx.chains()
        out = []
        for s in selected:
            try:
                tasks = build_tasks(self.ctx, s)
            except (HardError, RetryExhausted, BadPayload) as exc:
                self.ctx.emit(f"[{s}] 任务展开失败：{exc}")
                continue
            out.append((s, tasks))
        return out

    def run(self, plan, on_progress: Callable[[int, int], None], skip_done: bool = True,
            budget_min: float = 0.0) -> dict:
        """budget_min > 0 时，到点后不再发起新请求（已完成的任务已落库，可续跑）。"""
        conn = self.ctx.conn
        total = sum(len(t) for _, t in plan)
        done = 0
        t0 = time.time()
        stat = {"ok": 0, "empty": 0, "err": 0, "skip": 0, "budget": 0}

        def over_budget() -> bool:
            return bool(budget_min) and (time.time() - t0) / 60 >= budget_min

        for ds_id, tasks in plan:
            if self.stop.is_set() or over_budget():
                break
            finished = set()
            if skip_done:
                finished = db.ledger_done(conn, f"ds:{ds_id}")
            self.ctx.emit(f"▶ {BY_ID[ds_id].name}：{len(tasks)} 个任务")
            for i, t in enumerate(tasks, 1):
                if self.stop.is_set():
                    self.ctx.emit("⏹ 已停止")
                    break
                if over_budget():
                    stat["budget"] = 1
                    self.ctx.emit(f"⏸ 时间预算 {budget_min:.0f} 分钟用尽，"
                                  f"已处理 {done}/{total}；再次开始会从缺口继续")
                    break
                if t.key in finished:
                    stat["skip"] += 1
                    done += 1
                    on_progress(done, total)
                    continue
                try:
                    status = RUNNERS[ds_id](self.ctx, t)
                    st = "empty" if status in ("无数据", "空") else "ok"
                    stat[st] += 1
                    _finish(conn, ds_id, t, "ok" if st == "ok" else "empty")
                except HardError as exc:
                    db.ledger_put(conn, f"ds:{ds_id}", t.key, "error", err=str(exc)[:200])
                    status, stat["err"] = "参数错误", stat["err"] + 1
                except (RetryExhausted, BadPayload) as exc:
                    db.ledger_put(conn, f"ds:{ds_id}", t.key, "error", err=str(exc)[:200])
                    status, stat["err"] = "失败", stat["err"] + 1
                except Exception as exc:  # 软失败：单点异常不中断整批
                    db.ledger_put(conn, f"ds:{ds_id}", t.key, "error", err=repr(exc)[:200])
                    status, stat["err"] = f"异常:{type(exc).__name__}", stat["err"] + 1
                done += 1
                on_progress(done, total)
                if i % 20 == 0 or i == len(tasks):
                    conn.commit()
                self.ctx.emit(f"  [{i}/{len(tasks)}] {t.label or t.key}  {status}")
            conn.commit()
            if stat["budget"]:
                break
        return stat

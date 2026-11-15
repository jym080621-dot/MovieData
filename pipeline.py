#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py —— 采集主程序（断点续跑）。

子命令：
  init                                    建库
  day     --start --end                   逐日拉：全国双口径 / 省份榜 / 院线榜 / 全量影院榜
  sample  --date --n                      分层抽样（省份 × 规模三分位）+ 行政区码校正
  box     --start --end [--budget-min]    样本 × 日 的 影院×影片 分片（断点续跑）
  movie   --name/--code                   影片 SSR 逐日序列（含基准回归）
  qa                                      质量校验 + 报告

所有响应原样落盘 raw/；金额一律以「分」入库。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import time
from typing import List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zgdypf import api, db
from zgdypf import app_root as _app_root
from zgdypf import sample as samp
from zgdypf.client import Client, HardError, RetryExhausted
from zgdypf.parse import parse_count, parse_money, parse_ratio, unit_money

ROOT = _app_root()
DB_PATH = os.path.join(ROOT, "boxoffice.db")
RAW_DIR = os.path.join(ROOT, "raw")


def make_client(rate: float = 2.0) -> Client:
    return Client(rate=rate, raw_dir=RAW_DIR)


def daterange(start: str, end: str) -> List[str]:
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    out = []
    while a <= b:
        out.append(a.isoformat())
        a += dt.timedelta(days=1)
    return out


def to_float(v) -> Optional[float]:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def rank_row(r: dict, date: str) -> dict:
    """榜单行 -> 通用行。

    P0 追加裁决：**影院榜行没有 `sales` 整数字段**（91/91 原始响应确认），
    只有 salesDesc 字符串（"5.65万" / "101"，单位在万与元之间漂移）。
    因此金额只能解析，并标记 lossy；影院级的精确值来自分片接口的求和。
    """
    tickets = parse_count(r.get("salesCountDesc"))
    sessions = parse_count(r.get("showCountDesc"))
    sales_fen, sales_lossy = None, 1
    if isinstance(r.get("sales"), (int, float)):
        sales_fen, sales_lossy = int(r["sales"]), 0
    else:
        m = parse_money(r.get("salesDesc"))
        if m:
            sales_fen, sales_lossy = m.fen, (1 if m.lossy else 0)
    return {
        "date": date,
        "code": r.get("code"),
        "name": r.get("name"),
        "sales_fen": sales_fen,
        "sales_lossy": sales_lossy,
        "tickets_desc": r.get("salesCountDesc"),
        "sessions_desc": r.get("showCountDesc"),
        "avg_per_session": r.get("avgSalesCount"),
        "avg_seat_output": to_float(r.get("avgSalesPerSeatDesc")),
        "online_rate": parse_ratio(r.get("onlineSalesRateDesc")),
        "_tickets": int(round(tickets.value)) if tickets else None,
        "_tickets_lossy": 1 if (tickets and tickets.lossy) else 0,
        "_sessions": int(round(sessions.value)) if sessions else None,
        "_sessions_lossy": 1 if (sessions and sessions.lossy) else 0,
    }


# ---------------------------------------------------------------- init
def cmd_init(args) -> int:
    conn = db.connect(DB_PATH)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("DB:", DB_PATH)
    print("tables:", ", ".join(tables))
    print("raw dir:", RAW_DIR)
    return 0


# ---------------------------------------------------------------- day
MIN_CINEMAS = 11000   # 完整性闸门：低于此值视为该日影院维度不可用（实测存在 1~8916 家的塌缩）


def cmd_day(args) -> int:
    conn = db.connect(DB_PATH)
    client = make_client(args.rate)
    dates = daterange(args.start, args.end)
    batch = f"day:{dt.datetime.now():%Y%m%d%H%M%S}"
    print(f"== day backfill {args.start} .. {args.end}  ({len(dates)} 天)")
    n_full = n_thin = 0

    for i, date in enumerate(dates, 1):
        t0 = time.time()
        try:
            nat = api.national(client, date, True)
            split = api.national(client, date, False)
            m_svc = unit_money(nat.get("salesDesc"))
            m_split = unit_money(split.get("splitSalesDesc"))
            db.upsert_national(conn, {
                "date": date,
                "sales_yuan": m_svc.yuan if m_svc else None,
                "split_sales_yuan": m_split.yuan if m_split else None,
                "svc_fee_ratio": (1 - m_split.yuan / m_svc.yuan) if (m_svc and m_split and m_svc.yuan) else None,
                "gap_second": nat.get("updateGapSecond"),
                "update_ts": nat.get("updateTimestamp"),
                "ingested_at": db.now(),
            })

            pv = api.province_list(client, date, True, 50, 0)
            for r in (pv.get("salesRankList") or []):
                db.upsert_province(conn, rank_row(r, date))

            ch = api.chain_list(client, date, 0, 0)
            for r in (ch.get("salesRankList") or []):
                db.upsert_chain(conn, rank_row(r, date))

            rows, truncated = api.cinema_list_all(client, date, page=20000, max_pages=3)
            if len(rows) < MIN_CINEMAS or truncated:
                # 站点侧重建窗口 / 真实缺口：拒绝写入残缺榜单，并清掉上一轮的残缺行
                db.put_date_status(conn, date, len(rows), "thin",
                                   f"{len(rows)} 家 (截断={truncated})")
                db.purge_ranklist_day(conn, date)
                db.log(conn, "day", "warn", f"{date} 影院榜不完整: {len(rows)} 家，跳过")
                conn.commit()
                n_thin += 1
                print(f"  [{i}/{len(dates)}] {date}  ⚠ 影院榜仅 {len(rows)} 家，跳过（留待修复）"
                      f"  全国 {m_svc.yuan/1e4 if m_svc else '-'} 万")
                continue

            db.put_date_status(conn, date, len(rows), "full")
            db.purge_ranklist_day(conn, date)

            provs = [(r["code"], r["name"]) for r in (pv.get("salesRankList") or [])]
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
                    "cinema_code": r.get("code"),
                    "cinema_name": r.get("name"),
                    "province": info.get("province"),
                    "province_code": pcode,
                    "city": info.get("city"),
                    "is_direct_city": info.get("is_direct_city"),
                    "first_seen_date": date,
                    "list_sales_fen": rr["sales_fen"],
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
            db.upsert_dim_cinema(conn, dim)
            for f in facts:
                db.upsert_cinema_day(conn, f)
            conn.commit()
            n_full += 1
            print(f"  [{i}/{len(dates)}] {date}  影院 {len(rows)}  省份 {len(pv.get('salesRankList') or [])}  "
                  f"院线 {len(ch.get('salesRankList') or [])}  全国 {m_svc.yuan/1e4 if m_svc else '-'} 万  "
                  f"{time.time()-t0:.1f}s")
        except (HardError, RetryExhausted) as exc:
            db.put_date_status(conn, date, None, "error", str(exc))
            db.log(conn, "day", "error", f"{date}: {exc}")
            conn.commit()
            print(f"  [{i}/{len(dates)}] {date}  FAILED {exc}")

    db.log(conn, "day", "info", f"batch {batch} {args.start}..{args.end}")
    conn.commit()
    print(f"✓ 完整 {n_full} 天 / 残缺 {n_thin} 天")
    return 0


# ---------------------------------------------------------------- verify
def cmd_verify(args) -> int:
    """探测每个日期的影院维度可用性（轻量：limit=2）。"""
    conn = db.connect(DB_PATH)
    client = make_client(args.rate)
    dates = daterange(args.start, args.end)
    print(f"== verify {args.start} .. {args.end}  ({len(dates)} 天)")
    bad = []
    for i, date in enumerate(dates, 1):
        try:
            full, n = date_is_full(client, date)
            db.put_date_status(conn, date, n, "full" if full else "thin")
            if not full:
                bad.append(date)
        except (HardError, RetryExhausted) as exc:
            db.put_date_status(conn, date, None, "error", str(exc))
            bad.append(date)
        if i % 10 == 0:
            conn.commit()
    conn.commit()
    print(f"✓ 异常 {len(bad)} 天: {', '.join(bad) if bad else '无'}")
    return 0


# ---------------------------------------------------------------- sample
def cmd_sample(args) -> int:
    conn = db.connect(DB_PATH)
    client = make_client(args.rate)
    date = args.date

    rows = [dict(r) for r in conn.execute(
        "SELECT cinema_code AS code, cinema_name AS name, list_sales_fen AS sales "
        "FROM dim_cinema WHERE list_sales_fen IS NOT NULL",
    )]
    if not rows:
        print("dim_cinema 为空，先跑 day")
        return 1
    provs = [dict(r) for r in conn.execute(
        "SELECT code, name FROM fact_province WHERE date=? ORDER BY code", (date,))]
    if not provs:
        latest = conn.execute("SELECT MAX(date) FROM fact_province").fetchone()[0]
        provs = [dict(r) for r in conn.execute(
            "SELECT code, name FROM fact_province WHERE date=? ORDER BY code", (latest,))]
    print(f"候选影院 {len(rows)}  省份 {len(provs)}  目标样本 {args.n}")

    chosen = samp.build_sample(rows, provs, args.n)
    print(f"初抽 {len(chosen)} 家，开始用 /searchCinema 校正行政区码…")

    conn.execute("UPDATE dim_cinema SET sample=0, stratum=NULL")
    fixed = missed = 0
    for i, item in enumerate(chosen, 1):
        code = item["code"]
        prov = item.get("province")
        pcode = None
        try:
            res = api.search_cinema(client, item.get("name") or "")
            hit = next((x for x in res if x.get("code") == code), None)
            if hit and hit.get("cityCode"):
                pcode = samp.province_by_code(hit.get("cityCode"), provs)
                if pcode:
                    prov = pcode
                    fixed += 1
        except (HardError, RetryExhausted) as exc:
            missed += 1
        conn.execute(
            "UPDATE dim_cinema SET sample=1, stratum=?, province=COALESCE(?, province),"
            " province_code=COALESCE(?, province_code) WHERE cinema_code=?",
            (item.get("stratum"), prov, pcode, code))
        if i % 25 == 0:
            conn.commit()
            print(f"   {i}/{len(chosen)}  已校正 {fixed}  失败 {missed}")
    conn.commit()

    out = os.path.join(ROOT, "sample.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cinema_code", "cinema_name", "province", "province_code", "city",
                    "stratum", "list_sales_fen"])
        for r in conn.execute(
                "SELECT cinema_code,cinema_name,province,province_code,city,stratum,list_sales_fen "
                "FROM dim_cinema WHERE sample=1 ORDER BY province_code, cinema_code"):
            w.writerow(list(r))
    n = conn.execute("SELECT COUNT(*) FROM dim_cinema WHERE sample=1").fetchone()[0]
    print(f"✓ 样本锁定 {n} 家（行政区码校正 {fixed}，失败 {missed}）-> {out}")
    return 0


# ---------------------------------------------------------------- box
def date_is_full(client: Client, date: str) -> tuple:
    """廉价精确探针：从大偏移往回探，定位该日影院榜的真实规模档位。

    limit 很小时接口的 hasMore 是假阳性（limit=2 返回 n=2/hasMore=True，而底层只有 25 行），
    所以不能用小 limit 判完整。改为直接跳偏移取 10 行：
      offset=11000 有行 -> 该日 >=11010 家（完整）
      offset=3000  有行 -> 该日 3000~11000 家（部分塌缩）
      否则              -> 该日 <3000 家（严重缺口）
    全量日期实测 11.2k~12.7k，缺口日期 1~1100，区分度充足。
    """
    for off in (11000, 3000):
        j = api.cinema_list(client, date, limit=10, offset=off)
        rows = j.get("salesRankList") or []
        if rows:
            return (off == 11000), off + len(rows)
    return False, 0


# ---------------------------------------------------------------- box
def resolve_cinemas(client, text: str) -> list:
    """「影院名或 8 位编码，逗号分隔」-> [code]；名字走 /searchCinema 反查。"""
    out = []
    for raw in (text or "").replace("，", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if raw.isdigit() and 6 <= len(raw) <= 10:
            out.append(raw)
            continue
        try:
            res = api.search_cinema(client, raw, 20)
        except (HardError, RetryExhausted):
            print(f"  影院「{raw}」搜索失败，跳过")
            continue
        if not res:
            print(f"  影院「{raw}」未找到，跳过")
            continue
        hit = next((x for x in res if x.get("name") == raw), res[0])
        if len(res) > 1:
            print(f"  「{raw}」命中 {len(res)} 家，取第 1 家：{hit.get('name')}（{hit.get('code')}）")
        out.append(str(hit.get("code")))
    return out


def cmd_box(args) -> int:
    conn = db.connect(DB_PATH)
    client = make_client(args.rate)
    dates = args.dates.split(",") if getattr(args, "dates", None) else daterange(args.start, args.end)
    if getattr(args, "all_cinemas", False):
        codes = [r[0] for r in conn.execute(
            "SELECT cinema_code FROM dim_cinema WHERE list_sales_fen IS NOT NULL "
            "ORDER BY cinema_code")]
        print(f"全量影院 {len(codes):,} 家")
    elif getattr(args, "cinema", None):
        codes = resolve_cinemas(client, args.cinema)
        print(f"指定影院 {len(codes)} 家：{', '.join(codes)}")
    else:
        codes = [r[0] for r in conn.execute(
            "SELECT cinema_code FROM dim_cinema WHERE sample=1 ORDER BY province_code, cinema_code")]

    est = len(codes) * len(dates)
    if est > 50000 and not getattr(args, "yes", False):
        print(f"⚠ 预计 {est:,} 次请求，按 1.4 req/s 约需 {est/1.4/3600:.1f} 小时。")
        print("  确认执行请加 --yes；或先用 --dates / 更短的日期区间试跑。")
        return 1
    if not codes:
        print("没有样本，先跑 sample（或用 --cinema 指定影院）")
        return 1

    job = "cinema_box"
    done = db.ledger_done(conn, job)
    total_plan = len(dates) * len(codes)
    print(f"样本 {len(codes)} × 天 {len(dates)} = {total_plan} 个任务；已完成 {len(done & {f'{c}|{d}' for d in dates for c in codes})}")
    print(f"限速 {args.rate} req/s，预算 {args.budget_min if args.budget_min else '∞'} 分钟")

    started = time.time()
    n_ok = n_empty = n_err = n_warn = n_skip_date = 0
    for di, date in enumerate(dates, 1):
        pending = [c for c in codes if f"{c}|{date}" not in done]
        if not pending:
            continue
        if args.budget_min and (time.time() - started) / 60 >= args.budget_min:
            print(f"⏸ 预算用尽，停在 {date} 之前")
            break

        # —— 按日开闸：该日影院数据若正处重建窗口，整日跳过，绝不写入 204
        try:
            full, n_probe = date_is_full(client, date)
        except (HardError, RetryExhausted) as exc:
            db.put_date_status(conn, date, None, "error", str(exc))
            conn.commit()
            n_skip_date += 1
            print(f"  [{di}/{len(dates)}] {date}  探测失败，跳过: {exc}")
            continue
        if not full:
            db.put_date_status(conn, date, n_probe, "thin", "开闸探测不完整")
            db.log(conn, job, "warn", f"{date} 开闸不完整({n_probe})，整日跳过")
            conn.commit()
            n_skip_date += 1
            print(f"  [{di}/{len(dates)}] {date}  ⚠ 该日影院维度不完整({n_probe})，整日跳过")
            continue

        d_ok = d_empty = d_err = 0
        t_date = time.time()
        for code in pending:
            if args.budget_min and (time.time() - started) / 60 >= args.budget_min:
                break
            key = f"{code}|{date}"
            try:
                box = api.cinema_box(client, code, date)
            except HardError as exc:
                db.ledger_put(conn, job, key, "error", err=str(exc)[:300])
                d_err += 1
                continue
            except RetryExhausted as exc:
                db.ledger_put(conn, job, key, "error", err=str(exc)[:300])
                d_err += 1
                continue

            if box is None:
                db.ledger_put(conn, job, key, "empty", http_status=204)
                d_empty += 1
            else:
                movies = box.get("movieSalesList") or []
                mcount = box.get("movieCount")
                if mcount is not None and mcount != len(movies):
                    n_warn += 1
                    db.log(conn, job, "warn", f"{key} movieCount={mcount} != rows={len(movies)}")
                db.clear_cinema_day_movies(conn, code, date, mcount)
                total = 0
                for m in movies:
                    fen = int(m["sales"]) if isinstance(m.get("sales"), (int, float)) else None
                    total += fen or 0
                    sm = parse_money(m.get("sumSalesDesc"))
                    db.upsert_cinema_movie(conn, {
                        "cinema_code": code, "date": date,
                        "movie_code": m.get("movieCode"), "movie_name": m.get("movieName"),
                        "release_day": m.get("releaseDay"), "sales_fen": fen,
                        "sales_rate": parse_ratio(m.get("salesRateDesc")),
                        "session_count_rate": parse_ratio(m.get("sessionCountRateDesc")),
                        "sum_sales_text": m.get("sumSalesDesc"),
                        "sum_sales_yuan": sm.yuan if sm else None,
                        "movie_count": mcount, "cinema_sales_fen": None,
                    })
                db.upsert_cinema_day(conn, {
                    "cinema_code": code, "date": date, "sales_fen": total, "sales_src": "box",
                    "sales_lossy": 0,
                    "sessions": box.get("sessionCount"), "sessions_lossy": 0,
                    "movie_count": mcount, "new_movie_count": box.get("newMovieCount"),
                })
                db.ledger_put(conn, job, key, "ok", http_status=200)
                d_ok += 1

        # —— 可疑日回滚：空值率过高说明该日在采集中途塌陷，撤销本题
        n_ok += d_ok
        n_empty += d_empty
        n_err += d_err
        seen = d_ok + d_empty
        suspect = seen >= 10 and d_empty / max(seen, 1) > 0.5
        if suspect:
            conn.execute(
                "DELETE FROM collect_ledger WHERE job=? AND key LIKE ? AND status='empty'",
                (job, f"%|{date}"))
            db.log(conn, job, "warn",
                   f"{date} 空值率 {d_empty}/{seen}，判定为采集期塌陷，已撤销待重抓")
        if not suspect:
            db.put_date_status(conn, date, None, "full")
        conn.commit()
        print(f"  [{di}/{len(dates)}] {date}  ok={d_ok} empty={d_empty} err={d_err}  "
              f"{time.time()-t_date:.0f}s {'⚠SUSPECT' if suspect else ''}", flush=True)

    conn.commit()
    print(f"✓ 结束：ok={n_ok} empty={n_empty} err={n_err} warn={n_warn} 跳过日={n_skip_date}  "
          f"用时 {(time.time()-started)/60:.1f} min")
    db.log(conn, job, "info", f"ok={n_ok} empty={n_empty} err={n_err} skip_date={n_skip_date}")
    conn.commit()
    return 0


# ---------------------------------------------------------------- movie
BENCH = {"00100072025": {1: 48747600000, 2: 48034180000, 3: 61915020000, 4: 73156380000}}


def cmd_movie(args) -> int:
    conn = db.connect(DB_PATH)
    client = make_client(args.rate)
    code = args.code
    name = args.name
    if not code:
        res = api.search_movie(client, name)
        exact = [r for r in res if r.get("name") == name] or res
        if not exact:
            print("没搜到")
            return 1
        code = exact[0]["code"]
        name = exact[0].get("name")
    state, payload = api.movie_detail(client, code)
    if state is None:
        print("SSR 解析失败")
        return 2
    info = payload.get("info") or {}
    series = payload.get("index") or []
    db.execute = None  # guard: 下面用 conn.execute
    conn.execute(
        "INSERT INTO dim_movie(movie_code,movie_name,pub_desc,first_seen) VALUES(?,?,?,?)"
        " ON CONFLICT(movie_code) DO UPDATE SET movie_name=excluded.movie_name,"
        " pub_desc=excluded.pub_desc",
        (code, info.get("name") or name, info.get("pubDesc"), db.now()))
    n = 0
    for r in series:
        if not isinstance(r, dict) or r.get("sales") is None:
            continue
        db.upsert_movie_day(conn, {
            "movie_code": code, "show_date": r.get("showDate"),
            "release_days": r.get("releaseDays"), "sales_fen": r.get("sales"),
            "sales_count": r.get("salesCount"), "session_count": r.get("sessionCount"),
            "seat_count": r.get("seatCount"),
            "sales_rate": parse_ratio(r.get("salesRateDesc")),
            "session_rate": parse_ratio(r.get("sessionRateDesc")),
            "seat_rate": parse_ratio(r.get("seatRateDesc")),
            "avg_per_session": r.get("avgSalesCount"), "week": r.get("weekDate"),
        })
        n += 1
    conn.commit()

    print(f"《{info.get('name')}》 {info.get('pubDesc')}  movieCode={code}")
    sums = {s.get("title"): f"{s.get('value')}{s.get('unit')}" for s in payload.get("box_sum") or []}
    print("汇总：", "  ".join(f"{k} {v}" for k, v in sums.items()))
    print(f"逐日 {n} 条")
    bench = BENCH.get(code)
    if bench:
        by = {r.get("releaseDays"): r for r in series if isinstance(r, dict)}
        print("\n回归基准（P0 记录的 D1-D4）：")
        for d, want in bench.items():
            got = (by.get(d) or {}).get("sales")
            diff = (abs(got - want) / want * 100) if got else None
            print(f"  D{d}  {by.get(d, {}).get('showDate')}  {got:>14,} 分  期望 {want:>14,}  "
                  f"{'OK' if diff is not None and diff < 0.5 else 'MISMATCH'} ({diff:.3f}%)"
                  if got else f"  D{d} 缺失")
    return 0


# ---------------------------------------------------------------- qa
def cmd_qa(args) -> int:
    from zgdypf import qa
    return qa.run(DB_PATH, RAW_DIR, args.start, args.end, args.md)


# ---------------------------------------------------------------- export
def cmd_export(args) -> int:
    from zgdypf import export
    res = export.export_all(out_root=args.out, db_path=DB_PATH, raw_dir=RAW_DIR,
                            with_tables=not args.no_tables, log=print)
    print("完成：", res)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=2.0)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.set_defaults(func=cmd_init)

    p = sub.add_parser("day")
    p.add_argument("--start", required=True); p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_day)

    p = sub.add_parser("verify")
    p.add_argument("--start", required=True); p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("sample")
    p.add_argument("--date", required=True); p.add_argument("--n", type=int, default=150)
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("box")
    p.add_argument("--start"); p.add_argument("--end")
    p.add_argument("--dates", help="逗号分隔的日期清单，优先于 start/end")
    p.add_argument("--cinema", help="指定影院：名称或 8 位编码，逗号分隔（缺省用样本）")
    p.add_argument("--all-cinemas", action="store_true", help="全量影院（约 1.2 万家，耗时以小时计）")
    p.add_argument("--yes", action="store_true", help="跳过大批量二次确认")
    p.add_argument("--budget-min", type=float, default=0)
    p.set_defaults(func=cmd_box)

    p = sub.add_parser("movie")
    p.add_argument("--code"); p.add_argument("--name")
    p.set_defaults(func=cmd_movie)

    p = sub.add_parser("qa")
    p.add_argument("--start"); p.add_argument("--end")
    p.add_argument("--md", default=os.path.join(ROOT, "QA_REPORT.md"))
    p.set_defaults(func=cmd_qa)

    p = sub.add_parser("export")
    p.add_argument("--out", default=os.path.join(ROOT, "output"))
    p.add_argument("--no-tables", action="store_true")
    p.set_defaults(func=cmd_export)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

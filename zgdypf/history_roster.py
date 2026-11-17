"""按日期合并影院候选，保留来源及榜单翻页进度。"""
from __future__ import annotations

import csv
import datetime as dt
import re
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from . import db
from .cinema_detail import RANK_EVIDENCE
from .client import BadPayload
from .common import MIN_CINEMAS

SITE = "site_rank"
PAGE_SIZE = 20000
MAX_PAGES = 80


def check_dates(start, end):
    for day in (start, end):
        if dt.date.fromisoformat(day).isoformat() != day:
            raise ValueError("日期格式应为 YYYY-MM-DD")
    if start > end:
        raise ValueError("开始日期应早于或等于结束日期")


def valid_code(code):
    return isinstance(code, str) and re.fullmatch(r"[0-9]{6,10}", code) is not None


def import_csv(db_path, csv_path):
    """导入逐日影院编码。整个文件校验通过后一次提交。"""
    path = Path(csv_path)
    rows = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not {"date", "cinema_code"} <= set(fields):
            raise ValueError("每日名单 CSV 需要 date 和 cinema_code 两列")
        for row in reader:
            day = (row.get("date") or "").strip()
            code = (row.get("cinema_code") or "").strip()
            if None in row or not valid_code(code):
                raise ValueError(f"CSV 第 {reader.line_num} 行的影院编码或列数有误")
            try:
                check_dates(day, day)
            except ValueError as exc:
                raise ValueError(f"CSV 第 {reader.line_num} 行日期无效") from exc
            name = (row.get("cinema_name") or "").strip()
            rows[(day, code)] = name or rows.get((day, code), "")
    if not rows:
        raise ValueError("每日名单 CSV 没有数据行")
    daily = defaultdict(dict)
    for (day, code), name in rows.items():
        daily[day][code] = name
    with closing(db.connect(str(Path(db_path).resolve(strict=True)))) as conn, conn:
        for day, candidates in daily.items():
            _merge(conn, day, candidates, f"csv:{path.name}")
    return {"days": len(daily), "records": len(rows)}


def _merge(conn, day, candidates, source, run_id=None):
    timestamp = db.now()
    conn.executemany(
        """INSERT INTO history_cinema_roster(date,cinema_code,source,run_id,seen_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(date,cinema_code,source) DO UPDATE SET
               run_id=excluded.run_id, seen_at=excluded.seen_at""",
        ((day, code, source, run_id, timestamp) for code in candidates),
    )
    conn.executemany(
        """INSERT INTO dim_cinema(cinema_code,cinema_name,first_seen_date,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(cinema_code) DO UPDATE SET
               cinema_name=coalesce(nullif(dim_cinema.cinema_name,''),excluded.cinema_name),
               first_seen_date=min(coalesce(dim_cinema.first_seen_date,excluded.first_seen_date),
                                   excluded.first_seen_date)""",
        ((code, name or None, day, timestamp) for code, name in candidates.items()),
    )


def _exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


def stored_refreshes(conn):
    if not _exists(conn, "history_roster_refresh"):
        return {}
    return {r["date"]: r for r in conn.execute("SELECT * FROM history_roster_refresh")}


def candidates(conn, day, rank_codes, detail_codes, *, include_registry=False):
    sources = defaultdict(set)
    for code in rank_codes:
        sources[code].add("local_rank")
    for code in detail_codes:
        if valid_code(code):
            sources[code].add("local_detail")
    if _exists(conn, "history_cinema_roster"):
        for code, source in conn.execute(
            "SELECT cinema_code,source FROM history_cinema_roster WHERE date=? ORDER BY source",
            (day,),
        ):
            sources[code].add(source)
    if include_registry:
        # A day ranking can omit cinemas with no takings. Retain every known ID,
        # including IDs learned on other days; do not infer opening/closing dates.
        registry_sql = """
            SELECT cinema_code FROM dim_cinema
            UNION SELECT cinema_code FROM fact_cinema_day
            UNION SELECT cinema_code FROM fact_cinema_movie
        """
        if _exists(conn, "history_cinema_roster"):
            registry_sql += " UNION SELECT cinema_code FROM history_cinema_roster"
        for (code,) in conn.execute(registry_sql):
            if valid_code(code):
                sources[code].add("known_registry_unverified_activity")
    if include_registry and _exists(conn, "cinema_registry_entry"):
        for code, source in conn.execute("SELECT cinema_code,source_id FROM cinema_registry_entry"):
            if valid_code(code):
                sources[code].add("registry:" + source)
    return {code: sorted(sources[code]) for code in sorted(sources)}


def latest_site_codes(conn, day):
    """优先验证最近一次榜单实际列出的影院。"""
    return {r[0] for r in conn.execute(
        """SELECT r.cinema_code FROM history_cinema_roster r
           JOIN history_roster_refresh h ON h.date=r.date AND h.run_id=r.run_id
           WHERE r.date=? AND r.source=?""",
        (day, SITE),
    )}


def _known_codes(conn, day):
    return {r[0] for r in conn.execute(
        f"""SELECT cinema_code FROM fact_cinema_day WHERE date=? AND ({RANK_EVIDENCE} OR sales_src='box')
            UNION SELECT cinema_code FROM fact_cinema_movie WHERE date=?
            UNION SELECT cinema_code FROM history_cinema_roster WHERE date=?""",
        (day, day, day),
    )}


def _save_refresh(conn, day, run_id, status, offset, pages, count):
    conn.execute(
        """INSERT INTO history_roster_refresh(date,run_id,status,next_offset,pages,row_count,checked_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET run_id=excluded.run_id, status=excluded.status,
               next_offset=excluded.next_offset, pages=excluded.pages,
               row_count=excluded.row_count, checked_at=excluded.checked_at""",
        (day, run_id, status, offset, pages, count, db.now()),
    )


def fetch_site_page(client, day, offset, *, raw_rel=None, allow_initial_204=False):
    """Read one validated page of the public cinema ranking."""
    response = client.fetch("/getCinemaList", {
        "date": day, "withSvcFee": "true", "limit": PAGE_SIZE, "offset": offset,
    }, raw_rel=raw_rel)
    if allow_initial_204 and offset == 0 and response.status == 204:
        return [], False
    body = response.data
    rows = body.get("salesRankList") if isinstance(body, dict) else None
    if (response.status != 200 or not isinstance(rows, list) or len(rows) > PAGE_SIZE
            or type(body.get("hasMore")) is not bool):
        raise BadPayload(f"{day} 影院榜响应格式异常（偏移量 {offset}）")
    return rows, body["hasMore"]


def refresh_day(conn, client, day, previous, emit):
    """从已提交的页接着翻。一次成功响应和进度在同一事务提交。"""
    if previous and previous["status"] == "fetching":
        run_id, offset, pages, count = (previous[k] for k in ("run_id", "next_offset", "pages", "row_count"))
    else:
        run_id, offset, pages, count = uuid4().hex, 0, 0, 0
    known = _known_codes(conn, day)
    new_total = 0
    while pages < MAX_PAGES:
        rows, has_more = fetch_site_page(client, day, offset, raw_rel=f"history_roster/{day}/{run_id}/{offset}")
        page = {}
        invalid = False
        for row in rows:
            code = row.get("code") if isinstance(row, dict) else None
            if not valid_code(code) or code in page:
                invalid = True
                continue
            name = row.get("name")
            page[code] = name if isinstance(name, str) else None
        pages += 1
        offset += len(rows)
        with conn:
            if pages == 1:
                _save_local_rank(conn, day)
            _merge(conn, day, page, SITE, run_id)
            found = conn.execute(
                """SELECT count(*) FROM history_cinema_roster
                   WHERE date=? AND source=? AND run_id=?""",
                (day, SITE, run_id),
            ).fetchone()[0]
            if invalid or found - count != len(page) or has_more and not rows:
                status = "partial"
            elif not has_more:
                status = "listed" if found >= MIN_CINEMAS else "thin"
            else:
                status = "fetching" if pages < MAX_PAGES else "partial"
            _save_refresh(conn, day, run_id, status, offset, pages, found)
        added = len(page.keys() - known)
        known.update(page)
        new_total += added
        emit(f"{day} 榜单第 {pages} 页 · 本轮发现 {found:,} 家 · 本页新入队 {added:,} 家 · {status}")
        if status != "fetching":
            return new_total
        count = found
    return new_total


def _save_local_rank(conn, day):
    conn.execute(
        f"""INSERT OR IGNORE INTO history_cinema_roster(date,cinema_code,source,seen_at)
            SELECT date,cinema_code,'local_rank',? FROM fact_cinema_day
            WHERE date=? AND length(cinema_code) BETWEEN 6 AND 10
                AND cinema_code NOT GLOB '*[^0-9]*' AND {RANK_EVIDENCE}""",
        (db.now(), day),
    )

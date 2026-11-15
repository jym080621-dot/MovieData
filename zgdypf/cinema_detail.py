"""影院日分片验证、原子写入与旧台账识别。"""
from __future__ import annotations

import re

from . import api, db
from .client import BadPayload, Client
from .parse import parse_money, parse_ratio

CLI_JOB = "cinema_box"
GUI_JOB = "ds:cinema_box"
RANK_EVIDENCE = """(sales_src='ranklist' OR rank_sales_fen IS NOT NULL
                     OR (sales_src='box' AND (tickets_desc IS NOT NULL OR sessions_desc IS NOT NULL)))"""

SLICE_STATS = """
    SELECT cinema_code, count(*) AS n, count(sales_fen) AS n_money,
        sum(sales_fen) AS total, min(sales_fen) AS min_sales,
        sum(movie_code IS NULL OR movie_code='') AS missing_code,
        count(movie_count) AS n_counts, min(movie_count) AS min_count, max(movie_count) AS max_count,
        max(CASE WHEN movie_code=:movie THEN 1 ELSE 0 END) AS has_movie
    FROM fact_cinema_movie WHERE date=:day GROUP BY cinema_code
"""


def defer_empty(conn, code: str, day: str) -> bool:
    """续跑默认暂缓已有空响应，分片冲突仍需修复。"""
    row = conn.execute(
        """SELECT status FROM collect_ledger
           WHERE ((job=? AND key=?) OR (job=? AND key=?))
             AND NOT EXISTS (SELECT 1 FROM fact_cinema_movie WHERE cinema_code=? AND date=?)
             AND NOT EXISTS (SELECT 1 FROM fact_cinema_day WHERE cinema_code=? AND date=? AND sales_src='box')
           ORDER BY checked_at DESC, (status='empty') ASC LIMIT 1""",
        (CLI_JOB, f"{code}|{day}", GUI_JOB, f"{day}|{code}", code, day, code, day),
    ).fetchone()
    return row is not None and row[0] in ("empty", "empty_unverified")


def collect(conn, client: Client, code: str, day: str, job: str = CLI_JOB) -> tuple[str, int]:
    """调用一次分片端点，校验全片目，再原子替换该影院日分片与台账。"""
    response = api.cinema_box_response(client, code, day)
    key = f"{code}|{day}" if job == CLI_JOB else f"{day}|{code}"
    if response.status == 204:
        with conn:
            # 现有明细与 204 冲突时，保留明细并留下异常记录。
            has_facts = conn.execute(
                "SELECT 1 FROM fact_cinema_movie WHERE cinema_code=? AND date=? LIMIT 1",
                (code, day),
            ).fetchone()
            has_day = conn.execute(
                "SELECT 1 FROM fact_cinema_day WHERE cinema_code=? AND date=? AND sales_src='box'",
                (code, day),
            ).fetchone()
            if has_facts or has_day:
                raise BadPayload(f"{code}|{day} 返回 204，和已有分片冲突")
            db.ledger_put(conn, job, key, "empty_unverified", 204, response.attempts, response.elapsed_ms)
        return "empty", 0

    box = response.data
    if response.status != 200 or not isinstance(box, dict):
        raise BadPayload("影院分片响应缺少有效对象")
    movies = box.get("movieSalesList")
    count = box.get("movieCount")
    if not isinstance(movies, list) or not movies or type(count) is not int or count != len(movies):
        raise BadPayload("影院分片片目为空或与 movieCount 不符")
    rows = []
    seen = set()
    total = 0
    for movie in movies:
        if not isinstance(movie, dict):
            raise BadPayload("影院分片片目格式异常")
        movie_code = movie.get("movieCode")
        sales = movie.get("sales")
        if (not isinstance(movie_code, str) or not re.fullmatch(r"[0-9]+", movie_code)
                or movie_code in seen or type(sales) is not int or not 0 <= sales < 2**63):
            raise BadPayload("影院分片存在片码缺失、片码重复或金额异常")
        seen.add(movie_code)
        total += sales
        cumulative = parse_money(movie.get("sumSalesDesc"))
        rows.append(dict(
            cinema_code=code, date=day, movie_code=movie_code, movie_name=movie.get("movieName"),
            release_day=movie.get("releaseDay"), sales_fen=sales,
            sales_rate=parse_ratio(movie.get("salesRateDesc")),
            session_count_rate=parse_ratio(movie.get("sessionCountRateDesc")),
            sum_sales_text=movie.get("sumSalesDesc"),
            sum_sales_yuan=cumulative.yuan if cumulative else None, movie_count=count,
        ))
    if total >= 2**63:
        raise BadPayload("影院分片金额超出整数范围")
    for field in ("sessionCount", "newMovieCount"):
        value = box.get(field)
        if value is not None and (type(value) is not int or not 0 <= value < 2**63):
            raise BadPayload(f"影院分片 {field} 格式异常")
    for row in rows:
        for field in ("movie_name", "sum_sales_text", "release_day"):
            value = row[field]
            if value is not None and not isinstance(value, (str, int, float)):
                raise BadPayload(f"影院分片 {field} 格式异常")
    with conn:
        db.clear_cinema_day_movies(conn, code, day, count)
        for row in rows:
            db.upsert_cinema_movie(conn, row)
        db.upsert_cinema_day(conn, dict(
            cinema_code=code, date=day, sales_fen=total, sales_src="box", sales_lossy=0,
            sessions=box.get("sessionCount"), sessions_lossy=0,
            movie_count=count, new_movie_count=box.get("newMovieCount"),
        ))
        db.ledger_put(conn, job, key, "ok", 200, response.attempts, response.elapsed_ms)
    return "ok", count


def reusable_slices(conn, day: str, movie: str = "") -> dict[str, bool]:
    """返回自洽的影院日分片，值为所选影片是否出现在片目中。"""
    rows = conn.execute(f"""
        WITH s AS ({SLICE_STATS})
        SELECT s.cinema_code, s.has_movie FROM s JOIN fact_cinema_day d
            ON d.date=:day AND d.cinema_code=s.cinema_code
        WHERE d.sales_src='box' AND d.movie_count=s.n AND d.sales_fen=s.total
            AND s.n=s.n_money AND s.n=s.n_counts AND s.n=s.min_count AND s.n=s.max_count
            AND s.min_sales>=0 AND s.missing_code=0
    """, {"day": day, "movie": movie})
    return {r[0]: bool(r[1]) for r in rows}

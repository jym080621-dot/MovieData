"""单片影院明细、按日审计和限量补采。"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import db
from .cinema_detail import CLI_JOB, GUI_JOB, RANK_EVIDENCE, collect, reusable_slices
from .client import AccessLimited, BadPayload, Client, HardError, RetryExhausted
from .common import MIN_CINEMAS, daterange
from .export import write_csv

PROBE = "history_cinema_probe"
SCOPES = ("sample", "ranklist", "explicit")
DAY_STATES = ("with_movie", "without_movie", "empty_204", "deferred_204", "inconsistent", "error", "missing")


def validate(movie, start, end, scope, codes=()):
    if not re.fullmatch(r"[0-9]{1,30}", movie):
        raise ValueError("请填写数字影片编码，并保留前导零")
    for day in (start, end):
        if dt.date.fromisoformat(day).isoformat() != day:
            raise ValueError("日期格式应为 YYYY-MM-DD")
    if start > end:
        raise ValueError("开始日期应早于或等于结束日期")
    if scope not in SCOPES:
        raise ValueError("未知的影院范围")
    if scope == "explicit" and not codes:
        raise ValueError("指定影院模式需要至少一个影院编码")
    if codes and scope != "explicit":
        raise ValueError("填写影院编码时请选择指定影院模式")
    if any(not re.fullmatch(r"[0-9]{6,10}", code) for code in codes):
        raise ValueError("影院编码应为 6 至 10 位数字")
    if len(codes) != len(set(codes)):
        raise ValueError("影院编码重复")


def parse_codes(text):
    return tuple(dict.fromkeys(code.strip() for code in text.replace("，", ",").split(",") if code.strip()))


@contextmanager
def open_readonly(path):
    uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("BEGIN")
        yield conn


def _rank_rows(conn, day):
    return [
        row[0] for row in conn.execute(
            f"SELECT cinema_code FROM fact_cinema_day WHERE date=? AND {RANK_EVIDENCE} ORDER BY cinema_code",
            (day,),
        ) if row[0] and re.fullmatch(r"[0-9]{6,10}", row[0])
    ]


def _day_plan(conn, movie, day, scope, codes):
    known = conn.execute("SELECT status,n_cinemas FROM date_status WHERE date=?", (day,)).fetchone()
    probe = conn.execute(
        "SELECT ingested_at,payload FROM fact_doc WHERE dataset=? AND key=?",
        (PROBE, day),
    ).fetchone()
    probe = ({"ingested_at": probe[0], "status": json.loads(probe[1])["status"]}
             if probe else None)
    rank = _rank_rows(conn, day)
    usable_rank = len(rank) >= MIN_CINEMAS and (
        not known or known["status"] == "full" and
        (known["n_cinemas"] is None or known["n_cinemas"] >= MIN_CINEMAS)
    )
    if scope == "ranklist":
        target = rank
    elif scope == "explicit":
        target = codes
    else:
        target = [r[0] for r in conn.execute(
            "SELECT cinema_code FROM dim_cinema WHERE sample=1 ORDER BY cinema_code"
        ) if r[0] and re.fullmatch(r"[0-9]{6,10}", r[0])]

    good = reusable_slices(conn, day, movie)
    existing = {r[0] for r in conn.execute(
        """SELECT cinema_code FROM fact_cinema_movie WHERE date=?
           UNION SELECT cinema_code FROM fact_cinema_day WHERE date=? AND sales_src='box'""",
        (day, day),
    )}
    ledger = {}
    for row in conn.execute(
        """SELECT job,key,status,http_status FROM collect_ledger
           WHERE (job=? AND key LIKE ?) OR (job=? AND key>=? AND key<?)
           ORDER BY checked_at DESC, (status='empty') ASC""",
        (CLI_JOB, "%|" + day, GUI_JOB, day + "|", day + "}"),
    ):
        first, sep, last = row["key"].partition("|")
        if sep and (row["job"] == CLI_JOB and last == day or row["job"] == GUI_JOB and first == day):
            code = first if row["job"] == CLI_JOB else last
            ledger.setdefault(code, row)

    states = {}
    for code in target:
        entry = ledger.get(code)
        if code in good:
            state = "with_movie" if good[code] else "without_movie"
        elif code in existing or entry and entry["status"] == "ok":
            state = "inconsistent"
        elif entry and entry["status"] in ("empty", "empty_unverified"):
            confirmed = entry["status"] == "empty" and entry["http_status"] == 204 and known and known["status"] == "full"
            state = "empty_204" if confirmed else "deferred_204"
        elif entry:
            state = "error"
        else:
            state = "missing"
        states[code] = state
    return known, probe, rank, usable_rank, states


DETAIL_FIELDS = (
    "date", "cinema_code", "cinema_name", "province", "city", "movie_code", "movie_name",
    "release_day", "sales_fen", "sales_yuan", "sales_rate", "session_count_rate",
    "sum_sales_text", "movie_count", "ingested_at",
)
AUDIT_FIELDS = (
    "date", "scope", "local_date_status", "local_date_reported_count", "local_rank_candidates",
    "local_rank_usable", "latest_probe_status", "latest_probe_at", "target_count", *DAY_STATES,
    "coverage_status", "details_all_local", "details_within_scope", "scope_sales_fen",
    "national_day_list_sales_wan", "national_movie_detail_sales_fen",
)
GAP_FIELDS = ("date", "scope", "cinema_code", "status", "local_rank_usable")


def _audit_days(conn, movie, start, end, scope, codes, gaps_writer):
    for day in daterange(start, end):
        known, probe, rank, usable_rank, states = _day_plan(conn, movie, day, scope, codes)
        counts = Counter(states.values())
        n = len(states)
        if not n:
            coverage = "no_candidates"
        elif scope == "ranklist" and not usable_rank:
            coverage = "local_rank_insufficient"
        elif counts["with_movie"] + counts["without_movie"] == n:
            coverage = "scope_observed"
        else:
            coverage = "scope_incomplete"
        gaps_writer.writerows(
            (day, scope, code, state, int(usable_rank))
            for code, state in states.items() if state not in ("with_movie", "without_movie")
        )
        details = conn.execute(
            "SELECT cinema_code,sales_fen FROM fact_cinema_movie WHERE movie_code=? AND date=?",
            (movie, day),
        ).fetchall()
        in_scope = [r["sales_fen"] for r in details if r["cinema_code"] in states]
        national_rank = conn.execute(
            "SELECT sales_wan FROM fact_movie_national_day WHERE date=? AND movie_code=?", (day, movie)
        ).fetchone()
        national_detail = conn.execute(
            "SELECT sales_fen FROM fact_movie_day WHERE show_date=? AND movie_code=?", (day, movie)
        ).fetchone()
        yield (
            day, scope, known[0] if known else None, known[1] if known else None, len(rank),
            int(usable_rank), probe["status"] if probe else None, probe["ingested_at"] if probe else None,
            n, *(counts[s] for s in DAY_STATES), coverage, len(details), len(in_scope),
            sum(in_scope) if in_scope and all(v is not None for v in in_scope) else None,
            national_rank[0] if national_rank else None, national_detail[0] if national_detail else None,
        )


@contextmanager
def _report_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as temp:
        scratch = Path(temp.name)
    try:
        yield scratch
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def _detail_rows(conn, movie, start, end):
    rows = conn.execute(
        """SELECT f.date,f.cinema_code,c.cinema_name,c.province,c.city,f.movie_code,f.movie_name,
                  f.release_day,f.sales_fen,f.sales_rate,f.session_count_rate,f.sum_sales_text,
                  f.movie_count,f.ingested_at
           FROM fact_cinema_movie f LEFT JOIN dim_cinema c USING(cinema_code)
           WHERE f.movie_code=? AND f.date BETWEEN ? AND ? ORDER BY f.date,f.cinema_code""",
        (movie, start, end),
    )
    for row in rows:
        cents = row["sales_fen"]
        yuan = None if cents is None else f"{'-' if cents < 0 else ''}{abs(cents) // 100}.{abs(cents) % 100:02}"
        yield (*tuple(row)[:9], yuan, *tuple(row)[9:])


def export_reports(db_path, out_root, movie, start, end, scope, codes=(), *, details=True):
    validate(movie, start, end, scope, codes)
    prefix = Path(out_root) / f"{movie}_{start}_{end}"
    audit = prefix.with_name(prefix.name + f"_{scope}_audit.csv")
    gaps = prefix.with_name(prefix.name + f"_{scope}_gaps.csv")
    detail = prefix.with_name(prefix.name + "_details.csv")
    with open_readonly(db_path) as conn:
        n = None
        if details:
            with _report_file(detail) as tmp_detail:
                n = write_csv(str(tmp_detail), DETAIL_FIELDS, _detail_rows(conn, movie, start, end))
        with _report_file(audit) as tmp_audit, _report_file(gaps) as tmp_gaps:
            with tmp_gaps.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(GAP_FIELDS)
                ndays = write_csv(str(tmp_audit), AUDIT_FIELDS, _audit_days(conn, movie, start, end, scope, codes, writer))
    return {"details": detail if details else None, "audit": audit, "gaps": gaps,
            "detail_rows": n, "days": ndays}


def _pending(states, retry_empty):
    order = ("missing", "inconsistent", "error")
    if retry_empty:
        order += ("deferred_204", "empty_204")
    queues = {state: [] for state in order}
    for code, state in states.items():
        if state in queues:
            queues[state].append(code)
    return [code for state in order for code in queues[state]]


def plan_totals(db_path, movie, start, end, scope, codes=(), *, retry_empty=False, recheck_days=False):
    validate(movie, start, end, scope, codes)
    totals = Counter()
    with open_readonly(db_path) as conn:
        for day in daterange(start, end):
            _, probe, _, usable, states = _day_plan(conn, movie, day, scope, codes)
            pending = _pending(states, retry_empty)
            totals.update(targets=len(states), gaps=len(pending),
                          deferred=sum(s in ("empty_204", "deferred_204") for s in states.values()))
            if not states or scope == "ranklist" and not usable:
                totals.update(missing_scope_days=1)
            elif pending:
                if scope == "explicit":
                    totals.update(plannable=len(pending))
                elif not recheck_days and _recent_unavailable(probe):
                    totals.update(cooldown_days=1)
                else:
                    totals.update(plannable=len(pending), probe_days=1)
    return totals


class BatchEnded(Exception):
    pass


def _probe(conn, client, day):
    response = client.fetch("/getCinemaList", {
        "date": day, "withSvcFee": "true", "limit": 10, "offset": MIN_CINEMAS,
    }, raw_rel=f"{PROBE}/{day}")
    data = response.data
    rows = data.get("salesRankList") if isinstance(data, dict) else None
    if response.status != 200 or not isinstance(rows, list) or len(rows) > 10:
        raise BadPayload("影院榜日探针响应格式异常")
    seen = set()
    for row in rows:
        code = row.get("code") if isinstance(row, dict) else None
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6,10}", code) or code in seen:
            raise BadPayload("影院榜日探针返回无效或重复影院编码")
        seen.add(code)
    status = "available" if rows else "thin"
    with conn:
        db.put_doc(conn, PROBE, day, day, {"status": status, "offset": MIN_CINEMAS, "rows": len(rows)})
    return status


def _recent_unavailable(probe):
    return (probe and probe["status"] in ("thin", "suspect_204") and
            dt.datetime.now() - dt.datetime.fromisoformat(probe["ingested_at"]) < dt.timedelta(hours=24))


def repair(db_path, raw_dir, movie, start, end, scope, codes=(), *, max_requests=20,
           rate=1.0, minutes=10, retry_empty=False, recheck_days=False,
           stopped=lambda: False, emit=print, client_factory=Client):
    validate(movie, start, end, scope, codes)
    if max_requests < 1 or not math.isfinite(rate) or not 0 < rate <= 1:
        raise ValueError("请求上限须为正整数，每秒请求数须大于 0 且不超过 1")
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("时间预算须为正数")
    if dt.date.fromisoformat(end) >= dt.date.today():
        raise ValueError("历史补采的结束日期须早于今天")
    db_path = Path(db_path).resolve(strict=True)
    totals = Counter()
    deadline = time.monotonic() + minutes * 60

    def guard():
        if stopped():
            raise BatchEnded("用户停止")
        if time.monotonic() >= deadline:
            raise BatchEnded("到达时间预算")
        if client.stats.requests >= max_requests:
            raise BatchEnded("到达请求上限")

    client = client_factory(rate=rate, retries=2, timeout=15, raw_dir=str(raw_dir), request_guard=guard)
    with closing(db.connect(str(db_path))) as conn:
        try:
            for day in daterange(start, end):
                _, probe, _, usable_rank, states = _day_plan(conn, movie, day, scope, codes)
                codes_to_fetch = _pending(states, retry_empty)
                if not codes_to_fetch or scope == "ranklist" and not usable_rank:
                    continue
                if scope != "explicit":
                    if not recheck_days and _recent_unavailable(probe):
                        totals["skipped_days"] += 1
                        continue
                    guard()
                    try:
                        probe_status = _probe(conn, client, day)
                    except (HardError, RetryExhausted, BadPayload):
                        with conn:
                            db.put_doc(conn, PROBE, day, day, {"status": "error"})
                        raise
                    if probe_status != "available":
                        totals["skipped_days"] += 1
                        emit(f"{day} 高偏移影院榜未返回记录，暂停该日批量补采，可用指定影院逐家验证")
                        continue
                consecutive_empty = 0
                consecutive_bad = 0
                for code in codes_to_fetch:
                    guard()
                    try:
                        result, count = collect(conn, client, code, day)
                    except (BadPayload, HardError, RetryExhausted) as exc:
                        if isinstance(exc, AccessLimited):
                            raise
                        with conn:
                            db.ledger_put(conn, CLI_JOB, f"{code}|{day}", "error", err=str(exc)[:300])
                        totals["errors"] += 1
                        consecutive_empty = 0
                        consecutive_bad += 1
                        emit(f"{day} {code} 分片请求失败 · {exc}")
                        if not isinstance(exc, BadPayload) or consecutive_bad >= 3:
                            raise
                        continue
                    consecutive_bad = 0
                    if result == "empty":
                        totals["empty"] += 1
                        consecutive_empty += 1
                        emit(f"{day} {code} HTTP 204，记录为待复核")
                        if consecutive_empty >= 3:
                            if scope != "explicit":
                                with conn:
                                    db.put_doc(conn, PROBE, day, day, {"status": "suspect_204"})
                            totals["skipped_days"] += 1
                            emit(f"{day} 连续 3 个 204，暂停该日")
                            break
                    else:
                        totals["ok"] += 1
                        consecutive_empty = 0
                        emit(f"{day} {code} 已保存 {count} 条片目")
        except (BatchEnded, HardError, RetryExhausted, BadPayload) as exc:
            emit(f"本批停止 · {exc}")
            totals["stopped"] = str(exc)
            if not isinstance(exc, BatchEnded):
                totals["failed"] = 1
    totals["requests"] = client.stats.requests
    totals["retries"] = client.stats.retries
    return dict(totals)

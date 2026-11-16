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
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import db, history_roster
from .cinema_detail import CLI_JOB, GUI_JOB, RANK_EVIDENCE, collect, reusable_slices
from .client import AccessLimited, BadPayload, Client, HardError, RetryExhausted
from .common import MIN_CINEMAS, daterange
from .export import write_csv

PROBE = "history_cinema_probe"
SCOPES = ("sample", "ranklist", "explicit", "roster", "national")
DAY_STATES = ("with_movie", "without_movie", "empty_204", "deferred_204", "inconsistent", "error", "missing")


def validate(movie, start, end, scope, codes=()):
    if not re.fullmatch(r"[0-9]{1,30}", movie):
        raise ValueError("请填写数字影片编码，并保留前导零")
    history_roster.check_dates(start, end)
    if scope not in SCOPES:
        raise ValueError("未知的影院范围")
    if scope == "explicit" and not codes:
        raise ValueError("指定影院模式需要至少一个影院编码")
    if codes and scope != "explicit":
        raise ValueError("填写影院编码时请选择指定影院模式")
    if any(not history_roster.valid_code(code) for code in codes):
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
        ) if history_roster.valid_code(row[0])
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
    existing = {r[0] for r in conn.execute(
        """SELECT cinema_code FROM fact_cinema_movie WHERE date=?
           UNION SELECT cinema_code FROM fact_cinema_day WHERE date=? AND sales_src='box'""",
        (day, day),
    )}
    sources = {}
    if scope == "ranklist":
        target = rank
    elif scope in ("roster", "national"):
        sources = history_roster.candidates(conn, day, rank, existing, include_registry=scope == "national")
        target = sources
    elif scope == "explicit":
        target = codes
    else:
        target = [r[0] for r in conn.execute(
            "SELECT cinema_code FROM dim_cinema WHERE sample=1 ORDER BY cinema_code"
        ) if history_roster.valid_code(r[0])]

    good = reusable_slices(conn, day, movie)
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
    return known, probe, rank, usable_rank, states, sources


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
    "roster_rank_snapshot_status", "roster_rank_snapshot_at", "roster_rank_snapshot_rows",
    "roster_local_rank_count", "roster_site_rank_count", "roster_csv_count",
    "roster_detail_count", "national_roster_coverage",
)
GAP_FIELDS = ("date", "scope", "cinema_code", "status", "local_rank_usable", "roster_sources")
ROSTER_FIELDS = ("date", "cinema_code", "sources", "slice_status")


def _audit_days(conn, movie, start, end, scope, codes, gaps_writer, roster_writer, plan=None):
    refreshes = history_roster.stored_refreshes(conn) if scope in ("roster", "national") else {}
    for day in daterange(start, end):
        known, probe, rank, usable_rank, states, sources = _day_plan(conn, movie, day, scope, codes)
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
            (day, scope, code, state, int(usable_rank), "|".join(sources.get(code, ())))
            for code, state in states.items() if state not in ("with_movie", "without_movie")
        )
        if roster_writer:
            roster_writer.writerows(
                (day, code, "|".join(origin), states[code]) for code, origin in sources.items()
            )
        refresh = refreshes.get(day)
        if plan is not None:
            _count_day_plan(plan, scope, usable_rank, probe, refresh, counts)
        origin_counts = Counter()
        for origin in sources.values():
            origin_counts.update({s.split(":", 1)[0] for s in origin})
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
            refresh["status"] if refresh else None, refresh["checked_at"] if refresh else None,
            refresh["row_count"] if refresh else None,
            *(origin_counts[k] if scope in ("roster", "national") else None
              for k in ("local_rank", history_roster.SITE, "csv", "local_detail")),
            "unverified" if scope in ("roster", "national") else None,
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


def export_reports(db_path, out_root, movie, start, end, scope, codes=(), *, details=True, plan=None):
    validate(movie, start, end, scope, codes)
    prefix = Path(out_root) / f"{movie}_{start}_{end}"
    audit = prefix.with_name(prefix.name + f"_{scope}_audit.csv")
    gaps = prefix.with_name(prefix.name + f"_{scope}_gaps.csv")
    detail = prefix.with_name(prefix.name + "_details.csv")
    roster = prefix.with_name(prefix.name + "_roster.csv") if scope in ("roster", "national") else None
    with open_readonly(db_path) as conn:
        n = None
        if details:
            with _report_file(detail) as tmp_detail:
                n = write_csv(str(tmp_detail), DETAIL_FIELDS, _detail_rows(conn, movie, start, end))
        roster_file = _report_file(roster) if roster else nullcontext()
        with _report_file(audit) as tmp_audit, _report_file(gaps) as tmp_gaps, roster_file as tmp_roster:
            roster_stream = tmp_roster.open("w", encoding="utf-8-sig", newline="") if tmp_roster else nullcontext()
            with tmp_gaps.open("w", encoding="utf-8-sig", newline="") as handle, roster_stream as roster_handle:
                writer = csv.writer(handle)
                writer.writerow(GAP_FIELDS)
                roster_writer = csv.writer(roster_handle) if roster_handle else None
                if roster_writer:
                    roster_writer.writerow(ROSTER_FIELDS)
                rows = _audit_days(conn, movie, start, end, scope, codes, writer, roster_writer, plan)
                ndays = write_csv(str(tmp_audit), AUDIT_FIELDS, rows)
    return {"details": detail if details else None, "audit": audit, "gaps": gaps,
            "detail_rows": n, "days": ndays, "roster": roster}


def _pending(states, retry_empty):
    order = ("missing", "inconsistent", "error")
    if retry_empty:
        order += ("deferred_204", "empty_204")
    queues = {state: [] for state in order}
    for code, state in states.items():
        if state in queues:
            queues[state].append(code)
    return [code for state in order for code in queues[state]]


def _count_day_plan(totals, scope, usable, probe, refresh, counts, retry_empty=False, recheck_days=False):
    count = sum(counts.values())
    deferred = counts["empty_204"] + counts["deferred_204"]
    pending = counts["missing"] + counts["inconsistent"] + counts["error"]
    if retry_empty:
        pending += deferred
    totals.update(targets=count, gaps=pending, observed=counts["with_movie"] + counts["without_movie"],
                  with_movie=counts["with_movie"], deferred=deferred)
    if scope in ("roster", "national"):
        if not refresh or refresh["status"] == "fetching":
            totals["pending_roster_days"] += 1
        if (refresh and refresh["status"] != "listed") or (not refresh and not usable):
            totals["uncertain_roster_days"] += 1
    if not count or scope == "ranklist" and not usable:
        totals["missing_scope_days"] += 1
    elif pending:
        if scope in ("explicit", "roster", "national"):
            totals["plannable"] += pending
        elif not recheck_days and _recent_unavailable(probe):
            totals["cooldown_days"] += 1
        else:
            totals.update(plannable=pending, probe_days=1)


def plan_totals(db_path, movie, start, end, scope, codes=(), *, retry_empty=False, recheck_days=False):
    validate(movie, start, end, scope, codes)
    totals = Counter()
    with open_readonly(db_path) as conn:
        refreshes = history_roster.stored_refreshes(conn) if scope in ("roster", "national") else {}
        for day in daterange(start, end):
            _, probe, _, usable, states, _ = _day_plan(conn, movie, day, scope, codes)
            _count_day_plan(totals, scope, usable, probe, refreshes.get(day), Counter(states.values()),
                            retry_empty, recheck_days)
    return totals


class BatchEnded(Exception):
    pass


def _batch_client(raw_dir, rate, max_requests, minutes, stopped, client_factory):
    if type(max_requests) is not int or max_requests < 1 or not math.isfinite(rate) or not 0 < rate <= 1:
        raise ValueError("请求上限须为正整数，每秒请求数须大于 0 且不超过 1")
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("时间预算须为正数")
    deadline = time.monotonic() + minutes * 60

    def guard():
        if stopped():
            raise BatchEnded("用户停止")
        if time.monotonic() >= deadline:
            raise BatchEnded("到达时间预算")
        if client.stats.requests >= max_requests:
            raise BatchEnded("到达请求上限")

    client = client_factory(rate=rate, retries=2, timeout=15, raw_dir=str(raw_dir), request_guard=guard)
    return client, guard


def refresh_roster(db_path, raw_dir, start, end, *, max_requests=20, rate=1.0, minutes=10,
                   refresh_again=False, stopped=lambda: False, emit=print, client_factory=Client):
    history_roster.check_dates(start, end)
    if dt.date.fromisoformat(end) >= dt.date.today():
        raise ValueError("历史名单更新的结束日期须早于今天")
    db_path = Path(db_path).resolve(strict=True)
    client, guard = _batch_client(raw_dir, rate, max_requests, minutes, stopped, client_factory)
    return _refresh_roster(db_path, start, end, client, guard, refresh_again, emit)


def _refresh_roster(db_path, start, end, client, guard, refresh_again, emit):
    totals = Counter()
    with closing(db.connect(str(db_path))) as conn:
        previous = history_roster.stored_refreshes(conn)
        try:
            for day in daterange(start, end):
                record = previous.get(day)
                if record and record["status"] != "fetching" and not refresh_again:
                    totals["skipped_days"] += 1
                    continue
                guard()
                totals["new_candidates"] += history_roster.refresh_day(conn, client, day, record, emit)
                totals["updated_days"] += 1
        except (BatchEnded, HardError, RetryExhausted, BadPayload) as exc:
            emit(f"名单更新停止 · {exc}")
            totals["stopped"] = str(exc)
            if not isinstance(exc, BatchEnded):
                totals["failed"] = 1
    return {**totals, "requests": client.stats.requests, "retries": client.stats.retries}


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
    if dt.date.fromisoformat(end) >= dt.date.today():
        raise ValueError("历史补采的结束日期须早于今天")
    db_path = Path(db_path).resolve(strict=True)
    client, guard = _batch_client(raw_dir, rate, max_requests, minutes, stopped, client_factory)
    return _repair(db_path, movie, start, end, scope, codes, client, guard, retry_empty, recheck_days, emit)


def _repair(db_path, movie, start, end, scope, codes, client, guard, retry_empty, recheck_days, emit,
            on_progress=None):
    totals = Counter()
    with closing(db.connect(str(db_path))) as conn:
        try:
            for day in daterange(start, end):
                _, probe, _, usable_rank, states, _ = _day_plan(conn, movie, day, scope, codes)
                codes_to_fetch = _pending(states, retry_empty)
                if not codes_to_fetch or scope == "ranklist" and not usable_rank:
                    continue
                if scope in ("roster", "national"):
                    current = history_roster.latest_site_codes(conn, day)
                    codes_to_fetch.sort(key=lambda code: (
                        states[code] in ("deferred_204", "empty_204"), code not in current,
                    ))
                if scope not in ("explicit", "roster", "national"):
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
                    if on_progress:
                        on_progress(day, totals)
                    guard()
                    try:
                        result, count = collect(conn, client, code, day)
                    except (BadPayload, HardError, RetryExhausted) as exc:
                        with conn:
                            db.ledger_put(conn, CLI_JOB, f"{code}|{day}", "error", err=str(exc)[:300])
                        totals["errors"] += 1
                        if isinstance(exc, AccessLimited):
                            raise
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
                            if scope not in ("explicit", "roster", "national"):
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


def find_films(db_path, query):
    """按标题搜索已有的影片记录，相同片码只显示一次。"""
    if not Path(db_path).is_file():
        return []
    query = query.strip()
    with open_readonly(db_path) as conn:
        rows = conn.execute("""
            WITH films AS (
                SELECT movie_code,movie_name FROM dim_movie
                UNION SELECT movie_code,movie_name FROM fact_year_movie
                UNION SELECT movie_code,movie_name FROM fact_period_movie
                UNION SELECT movie_code,movie_name FROM fact_movie_national_day
                UNION SELECT movie_code,movie_name FROM fact_cinema_movie
            )
            SELECT movie_code, movie_name FROM films
            WHERE movie_code IS NOT NULL AND movie_name IS NOT NULL
                AND (instr(movie_name,:query)>0 OR movie_code=:query)
            ORDER BY movie_name=:query DESC, movie_name, movie_code
        """, {"query": query})
        return _film_hits(rows)


def search_films(raw_dir, query, *, stopped=lambda: False, client_factory=Client):
    """一次受限联网搜索，结果由用户选择。"""
    client, _ = _batch_client(raw_dir, 1, 1, 1, stopped, client_factory)
    data = client.fetch("/searchMovie", {"movieName": query, "limit": 20, "offset": 0}).data
    if not isinstance(data, list):
        raise BadPayload("影片搜索返回格式异常")
    return _film_hits((row.get("code"), row.get("name")) for row in data if isinstance(row, dict))


def _film_hits(rows):
    hits = {}
    for code, name in rows:
        if isinstance(code, str) and re.fullmatch(r"[0-9]{1,30}", code) and isinstance(name, str) and name.strip():
            hits.setdefault(code, name.strip())
            if len(hits) >= 20:
                break
    return [(code, name) for code, name in hits.items()]


def run_quick_batch(db_path, raw_dir, out_dir, movie, start, end, *, max_requests=20, minutes=10,
                    stopped=lambda: False, progress=lambda message: None, on_counts=None, emit=print,
                    client_factory=Client):
    """用同一个限速器和预算更新名单、补采，随后导出当前结果。"""
    validate(movie, start, end, "roster")
    if dt.date.fromisoformat(end) >= dt.date.today():
        raise ValueError("结束日期须早于今天")
    client, guard = _batch_client(raw_dir, 1, max_requests, minutes, stopped, client_factory)
    db_path = Path(db_path).resolve()

    last_update = 0.0

    def report(day=None, totals=None, *, force=False):
        nonlocal last_update
        now = time.monotonic()
        if on_counts and (force or now - last_update >= 1):
            totals = totals or {}
            on_counts(day, client.stats.requests, totals.get("ok", 0), totals.get("empty", 0))
            last_update = now

    def log_roster(message):
        emit(message)
        report()

    progress("正在更新每日影院名单…")
    roster = _refresh_roster(db_path, start, end, client, guard, False, log_roster)
    details = {}
    if not roster.get("stopped") and client.stats.requests < max_requests:
        progress("正在补采影院票房…")
        details = _repair(db_path, movie, start, end, "roster", (), client, guard, False, False, emit,
                          on_progress=report if on_counts else None)
    report(totals=details, force=True)
    progress("正在导出票房和覆盖报告…")
    plan = Counter()
    reports = export_reports(db_path, out_dir, movie, start, end, "roster", plan=plan)
    return {
        "reports": reports, "plan": plan, "requests": client.stats.requests,
        "saved": details.get("ok", 0), "empty": details.get("empty", 0),
        "new_candidates": roster.get("new_candidates", 0),
        "failed": bool(roster.get("failed") or details.get("failed") or details.get("errors")),
        "reason": roster.get("stopped") or details.get("stopped") or (
            "到达请求上限" if client.stats.requests >= max_requests else None
        ),
    }

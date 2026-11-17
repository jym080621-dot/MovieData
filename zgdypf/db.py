#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存储层。

表设计要点：
  * fact_cinema_day 同时承载两个来源：影院榜（覆盖全量影院、含 Desc 舍入）与
    分片接口（仅样本、场次数为精确整数）。用 *_exact / *_lossy 标记精度，
    upsert 时「精确值覆盖舍入值」，绝不反向覆盖。
  * collect_ledger 是断点续跑的权威台账：status ∈ {ok, empty, error, skipped}。
    注意 empty 有两种来源：HTTP 204（getCinemaBoxData）与 200+"--"（getCinemaSumData）。
  * 所有金额对外统一为「分」(sales_fen)；yuan 仅作展示。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from typing import Any, Iterable, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS dim_cinema (
    cinema_code     TEXT PRIMARY KEY,
    cinema_name     TEXT,
    province        TEXT,
    province_code   TEXT,
    city            TEXT,
    is_direct_city  INTEGER DEFAULT 0,
    first_seen_date TEXT,
    sample          INTEGER DEFAULT 0,
    stratum         TEXT,
    list_sales_fen  INTEGER,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS dim_movie (
    movie_code  TEXT PRIMARY KEY,
    movie_name  TEXT,
    pub_desc    TEXT,
    first_seen  TEXT
);

CREATE TABLE IF NOT EXISTS fact_cinema_day (
    cinema_code     TEXT,
    date            TEXT,
    sales_fen       INTEGER,
    sales_src       TEXT,
    sales_lossy     INTEGER DEFAULT 0,
    rank_sales_fen  INTEGER,
    tickets         INTEGER,
    tickets_desc    TEXT,
    tickets_lossy   INTEGER DEFAULT 0,
    sessions        INTEGER,
    sessions_desc   TEXT,
    sessions_lossy  INTEGER DEFAULT 0,
    online_rate     REAL,
    online_rate_desc TEXT,
    avg_seat_output REAL,
    avg_seat_desc   TEXT,
    avg_per_session INTEGER,
    movie_count     INTEGER,
    new_movie_count INTEGER,
    ingested_at     TEXT,
    PRIMARY KEY (cinema_code, date)
);
CREATE INDEX IF NOT EXISTS idx_fcd_date ON fact_cinema_day(date);

CREATE TABLE IF NOT EXISTS fact_cinema_movie (
    cinema_code       TEXT,
    date              TEXT,
    movie_code        TEXT,
    movie_name        TEXT,
    release_day       INTEGER,
    sales_fen         INTEGER,
    sales_rate        REAL,
    session_count_rate REAL,
    sum_sales_text    TEXT,
    sum_sales_yuan    REAL,
    movie_count       INTEGER,
    cinema_sales_fen  INTEGER,
    ingested_at       TEXT,
    PRIMARY KEY (cinema_code, date, movie_code)
);
CREATE INDEX IF NOT EXISTS idx_fcm_date ON fact_cinema_movie(date);
CREATE INDEX IF NOT EXISTS idx_fcm_movie ON fact_cinema_movie(movie_code);

CREATE TABLE IF NOT EXISTS fact_province (
    date             TEXT,
    code             TEXT,
    name             TEXT,
    sales_fen        INTEGER,
    tickets_desc     TEXT,
    sessions_desc    TEXT,
    avg_per_session  INTEGER,
    avg_seat_output  REAL,
    online_rate      REAL,
    ingest_batch     TEXT,
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS fact_chain (
    date             TEXT,
    code             TEXT,
    name             TEXT,
    sales_fen        INTEGER,
    tickets_desc     TEXT,
    sessions_desc    TEXT,
    avg_per_session  INTEGER,
    avg_seat_output  REAL,
    online_rate      REAL,
    ingest_batch     TEXT,
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS fact_national (
    date             TEXT PRIMARY KEY,
    sales_yuan       REAL,
    split_sales_yuan REAL,
    svc_fee_ratio    REAL,
    gap_second       INTEGER,
    update_ts        INTEGER,
    ingested_at      TEXT
);

CREATE TABLE IF NOT EXISTS fact_movie_day (
    movie_code       TEXT,
    show_date        TEXT,
    release_days     INTEGER,
    sales_fen        INTEGER,
    sales_count      INTEGER,
    session_count    INTEGER,
    seat_count       INTEGER,
    sales_rate       REAL,
    session_rate     REAL,
    seat_rate        REAL,
    avg_per_session  INTEGER,
    week             TEXT,
    ingested_at      TEXT,
    PRIMARY KEY (movie_code, show_date)
);

CREATE TABLE IF NOT EXISTS collect_ledger (
    job          TEXT,
    key          TEXT,
    status       TEXT,
    http_status  INTEGER,
    attempts     INTEGER,
    elapsed_ms   INTEGER,
    err          TEXT,
    checked_at   TEXT,
    PRIMARY KEY (job, key)
);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON collect_ledger(job, status);
-- Index legacy CLI task keys by date suffix for large historical batches.
CREATE INDEX IF NOT EXISTS idx_ledger_job_day_suffix ON collect_ledger(job, substr(key,-10));
-- Durable attempt order (including success/failure), retaining the v1.5.1 handoff table name.
CREATE TABLE IF NOT EXISTS history_204_review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    cinema_code TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    UNIQUE(date,cinema_code)
);
CREATE INDEX IF NOT EXISTS idx_history_204_review_day_id ON history_204_review_queue(date,id);

CREATE TABLE IF NOT EXISTS history_cinema_roster (
    date TEXT NOT NULL,
    cinema_code TEXT NOT NULL,
    source TEXT NOT NULL,
    run_id TEXT,
    seen_at TEXT NOT NULL,
    PRIMARY KEY(date, cinema_code, source)
);

CREATE TABLE IF NOT EXISTS history_roster_refresh (
    date TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    next_offset INTEGER NOT NULL,
    pages INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cinema_registry_source (
    source_id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cinema_registry_entry (
    source_id TEXT NOT NULL REFERENCES cinema_registry_source(source_id),
    cinema_code TEXT NOT NULL,
    cinema_name TEXT,
    province_code TEXT,
    business_status TEXT,
    PRIMARY KEY(source_id,cinema_code)
);
CREATE INDEX IF NOT EXISTS idx_registry_code ON cinema_registry_entry(cinema_code);

CREATE TABLE IF NOT EXISTS date_status (
    date         TEXT PRIMARY KEY,
    n_cinemas    INTEGER,
    status       TEXT,          -- full / thin / error
    checked_at   TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS fact_movie_national_day (
    date TEXT, movie_code TEXT, movie_name TEXT, release_days INTEGER,
    sales_wan REAL, sales_rate REAL, session_rate REAL, seat_rate REAL,
    online_rate REAL, sum_sales_text TEXT, split_sales_wan REAL,
    split_sales_rate REAL, split_online_rate REAL,
    PRIMARY KEY(date, movie_code)
);

CREATE TABLE IF NOT EXISTS fact_chain_day (
    date TEXT, chain_code TEXT, name TEXT, sales_fen INTEGER,
    tickets_desc TEXT, sessions_desc TEXT, avg_per_session INTEGER,
    avg_seat_output REAL, online_rate REAL, movie_count INTEGER,
    new_movie_count INTEGER, session_count INTEGER,
    PRIMARY KEY(date, chain_code)
);

CREATE TABLE IF NOT EXISTS fact_chain_movie (
    date TEXT, chain_code TEXT, movie_code TEXT, movie_name TEXT,
    release_day INTEGER, sales_fen INTEGER, sales_rate REAL,
    session_rate REAL, sum_sales_text TEXT,
    PRIMARY KEY(date, chain_code, movie_code)
);

CREATE TABLE IF NOT EXISTS fact_cinema_trend (
    cinema_code TEXT, show_date TEXT, week TEXT, sales_fen INTEGER,
    tickets_desc TEXT, sessions_desc TEXT, avg_per_session INTEGER,
    avg_seat_output REAL, online_rate REAL,
    PRIMARY KEY(cinema_code, show_date)
);

CREATE TABLE IF NOT EXISTS fact_chain_trend (
    chain_code TEXT, show_date TEXT, week TEXT, sales_fen INTEGER,
    tickets_desc TEXT, sessions_desc TEXT, avg_per_session INTEGER,
    avg_seat_output REAL, online_rate REAL,
    PRIMARY KEY(chain_code, show_date)
);

CREATE TABLE IF NOT EXISTS fact_cinema_sum (
    cinema_code TEXT, date TEXT, sales_yuan REAL, sales_cmp TEXT,
    movie_count INTEGER, new_movie_count INTEGER, session_count INTEGER,
    PRIMARY KEY(cinema_code, date)
);

CREATE TABLE IF NOT EXISTS fact_city (
    date TEXT, movie_code TEXT, city_name TEXT, city_code TEXT,
    value_yuan REAL, rate REAL,
    PRIMARY KEY(date, movie_code, city_name)
);

CREATE TABLE IF NOT EXISTS fact_period_movie (
    period_type INTEGER, period_start TEXT, movie_code TEXT, movie_name TEXT,
    release_date TEXT, sales_wan REAL, sales_rate REAL, avg_price REAL,
    avg_split_price REAL, avg_per_session INTEGER, split_sales_wan REAL,
    split_sales_rate REAL,
    PRIMARY KEY(period_type, period_start, movie_code)
);

CREATE TABLE IF NOT EXISTS fact_year_movie (
    year INTEGER, movie_code TEXT, movie_name TEXT, premiere_date TEXT,
    sales_wan REAL, avg_price REAL, avg_per_session INTEGER, rank INTEGER,
    PRIMARY KEY(year, movie_code)
);

CREATE TABLE IF NOT EXISTS fact_history (
    dataset TEXT, rank INTEGER, title TEXT, show_date TEXT, week TEXT,
    release_days INTEGER, sales_wan REAL, sales_count_wan REAL,
    session_count INTEGER, movie_code TEXT, extra TEXT,
    PRIMARY KEY(dataset, rank)
);

CREATE TABLE IF NOT EXISTS fact_doc (
    dataset TEXT, key TEXT, date TEXT, payload TEXT, ingested_at TEXT,
    PRIMARY KEY(dataset, key)
);

CREATE TABLE IF NOT EXISTS run_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT,
    job       TEXT,
    level     TEXT,
    message   TEXT
);
"""


def now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：给已存在的库补列。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fact_cinema_day)")}
    for col, ddl in (
        ("sales_lossy", "ALTER TABLE fact_cinema_day ADD COLUMN sales_lossy INTEGER DEFAULT 0"),
        ("rank_sales_fen", "ALTER TABLE fact_cinema_day ADD COLUMN rank_sales_fen INTEGER"),
    ):
        if col not in cols:
            conn.execute(ddl)
    scols = {r[1] for r in conn.execute("PRAGMA table_info(fact_cinema_sum)")}
    if "sales_cmp" not in scols:
        conn.execute("ALTER TABLE fact_cinema_sum ADD COLUMN sales_cmp TEXT")
    conn.commit()


def log(conn: sqlite3.Connection, job: str, level: str, message: str) -> None:
    conn.execute("INSERT INTO run_log(ts, job, level, message) VALUES(?,?,?,?)",
                 (now(), job, level, str(message)[:2000]))


def ledger_put(conn: sqlite3.Connection, job: str, key: str, status: str,
               http_status: Optional[int] = None, attempts: int = 1,
               elapsed_ms: Optional[int] = None, err: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO collect_ledger(job,key,status,http_status,attempts,elapsed_ms,err,checked_at)"
        " VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(job,key) DO UPDATE SET status=excluded.status,"
        " http_status=excluded.http_status, attempts=excluded.attempts,"
        " elapsed_ms=excluded.elapsed_ms, err=excluded.err, checked_at=excluded.checked_at",
        (job, key, status, http_status, attempts, elapsed_ms, err, now()),
    )


def ledger_done(conn: sqlite3.Connection, job: str) -> set:
    """已完成（含 empty）的 key 集合；error 不算完成，会被重试。"""
    rows = conn.execute(
        "SELECT key FROM collect_ledger WHERE job=? AND status IN ('ok','empty')", (job,)
    ).fetchall()
    return {r[0] for r in rows}


def upsert_dim_cinema(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    n = 0
    ts = now()
    sql = (
        "INSERT INTO dim_cinema(cinema_code,cinema_name,province,province_code,city,"
        "is_direct_city,first_seen_date,list_sales_fen,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cinema_code) DO UPDATE SET cinema_name=excluded.cinema_name,"
        " province=COALESCE(excluded.province, dim_cinema.province),"
        " province_code=COALESCE(excluded.province_code, dim_cinema.province_code),"
        " city=COALESCE(excluded.city, dim_cinema.city),"
        " first_seen_date=COALESCE(dim_cinema.first_seen_date, excluded.first_seen_date),"
        " list_sales_fen=excluded.list_sales_fen, updated_at=excluded.updated_at"
    )
    for r in rows:
        conn.execute(sql, (
            r.get("cinema_code"), r.get("cinema_name"), r.get("province"),
            r.get("province_code"), r.get("city"), int(bool(r.get("is_direct_city"))),
            r.get("first_seen_date"), r.get("list_sales_fen"), ts,
        ))
        n += 1
    return n


def upsert_cinema_day(conn: sqlite3.Connection, r: dict) -> None:
    """优先保留精确值：sessions_lossy=0 的记录不会被 lossy 覆盖。"""
    conn.execute(
        "INSERT INTO fact_cinema_day(cinema_code,date,sales_fen,sales_src,sales_lossy,"
        "rank_sales_fen,tickets,"
        "tickets_desc,tickets_lossy,sessions,sessions_desc,sessions_lossy,"
        "online_rate,online_rate_desc,avg_seat_output,avg_seat_desc,avg_per_session,"
        "movie_count,new_movie_count,ingested_at)"
        " VALUES(:cinema_code,:date,:sales_fen,:sales_src,:sales_lossy,:rank_sales_fen,"
        ":tickets,:tickets_desc,"
        ":tickets_lossy,:sessions,:sessions_desc,:sessions_lossy,:online_rate,"
        ":online_rate_desc,:avg_seat_output,:avg_seat_desc,:avg_per_session,"
        ":movie_count,:new_movie_count,:ingested_at)"
        " ON CONFLICT(cinema_code,date) DO UPDATE SET"
        " sales_fen=CASE WHEN fact_cinema_day.sales_src='box' AND excluded.sales_src='ranklist'"
        "   THEN fact_cinema_day.sales_fen"
        "   ELSE COALESCE(excluded.sales_fen, fact_cinema_day.sales_fen) END,"
        " sales_src=CASE WHEN excluded.sales_src='box' THEN 'box' ELSE fact_cinema_day.sales_src END,"
        " sales_lossy=CASE WHEN excluded.sales_src='box' THEN 0"
        "   ELSE MIN(excluded.sales_lossy, COALESCE(fact_cinema_day.sales_lossy,1)) END,"
        " rank_sales_fen=COALESCE(excluded.rank_sales_fen, fact_cinema_day.rank_sales_fen),"
        " tickets=COALESCE(excluded.tickets, fact_cinema_day.tickets),"
        " tickets_desc=COALESCE(excluded.tickets_desc, fact_cinema_day.tickets_desc),"
        " tickets_lossy=MIN(excluded.tickets_lossy, fact_cinema_day.tickets_lossy),"
        " sessions=CASE WHEN excluded.sessions_lossy=0 OR fact_cinema_day.sessions IS NULL"
        "   THEN COALESCE(excluded.sessions, fact_cinema_day.sessions)"
        "   ELSE fact_cinema_day.sessions END,"
        " sessions_desc=COALESCE(excluded.sessions_desc, fact_cinema_day.sessions_desc),"
        " sessions_lossy=MIN(excluded.sessions_lossy, fact_cinema_day.sessions_lossy),"
        " online_rate=COALESCE(excluded.online_rate, fact_cinema_day.online_rate),"
        " online_rate_desc=COALESCE(excluded.online_rate_desc, fact_cinema_day.online_rate_desc),"
        " avg_seat_output=COALESCE(excluded.avg_seat_output, fact_cinema_day.avg_seat_output),"
        " avg_seat_desc=COALESCE(excluded.avg_seat_desc, fact_cinema_day.avg_seat_desc),"
        " avg_per_session=COALESCE(excluded.avg_per_session, fact_cinema_day.avg_per_session),"
        " movie_count=COALESCE(excluded.movie_count, fact_cinema_day.movie_count),"
        " new_movie_count=COALESCE(excluded.new_movie_count, fact_cinema_day.new_movie_count)",
        {**{k: None for k in (
            "sales_fen", "sales_src", "sales_lossy", "rank_sales_fen",
            "tickets", "tickets_desc", "tickets_lossy",
            "sessions", "sessions_desc", "sessions_lossy", "online_rate",
            "online_rate_desc", "avg_seat_output", "avg_seat_desc", "avg_per_session",
            "movie_count", "new_movie_count")},
         "ingested_at": r.get("ingested_at") or now(), **r},
    )


def upsert_cinema_movie(conn: sqlite3.Connection, r: dict) -> None:
    conn.execute(
        "INSERT INTO fact_cinema_movie(cinema_code,date,movie_code,movie_name,release_day,"
        "sales_fen,sales_rate,session_count_rate,sum_sales_text,sum_sales_yuan,movie_count,"
        "cinema_sales_fen,ingested_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cinema_code,date,movie_code) DO UPDATE SET"
        " movie_name=excluded.movie_name, release_day=excluded.release_day,"
        " sales_fen=excluded.sales_fen, sales_rate=excluded.sales_rate,"
        " session_count_rate=excluded.session_count_rate,"
        " sum_sales_text=excluded.sum_sales_text, sum_sales_yuan=excluded.sum_sales_yuan,"
        " movie_count=excluded.movie_count, cinema_sales_fen=excluded.cinema_sales_fen,"
        " ingested_at=excluded.ingested_at",
        (r.get("cinema_code"), r.get("date"), r.get("movie_code"), r.get("movie_name"),
         r.get("release_day"), r.get("sales_fen"), r.get("sales_rate"),
         r.get("session_count_rate"), r.get("sum_sales_text"), r.get("sum_sales_yuan"),
         r.get("movie_count"), r.get("cinema_sales_fen"), r.get("ingested_at") or now()),
    )


def clear_cinema_day_movies(conn: sqlite3.Connection, cinema_code: str, date: str,
                            movie_count: Optional[int]) -> None:
    """重抓时清掉该 (影院,日) 的旧分片，避免片目变动留下幽灵行。
    仅当新数据的 movie_count == 旧行数时才安全跳过；这里一律先删。
    """
    conn.execute("DELETE FROM fact_cinema_movie WHERE cinema_code=? AND date=?",
                 (cinema_code, date))


def upsert_province(conn: sqlite3.Connection, r: dict) -> None:
    conn.execute(
        "INSERT INTO fact_province(date,code,name,sales_fen,tickets_desc,sessions_desc,"
        "avg_per_session,avg_seat_output,online_rate,ingest_batch)"
        " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,code) DO UPDATE SET"
        " name=excluded.name, sales_fen=excluded.sales_fen, tickets_desc=excluded.tickets_desc,"
        " sessions_desc=excluded.sessions_desc, avg_per_session=excluded.avg_per_session,"
        " avg_seat_output=excluded.avg_seat_output, online_rate=excluded.online_rate,"
        " ingest_batch=excluded.ingest_batch",
        (r.get("date"), r.get("code"), r.get("name"), r.get("sales_fen"),
         r.get("tickets_desc"), r.get("sessions_desc"), r.get("avg_per_session"),
         r.get("avg_seat_output"), r.get("online_rate"), r.get("ingest_batch")),
    )


def upsert_chain(conn: sqlite3.Connection, r: dict) -> None:
    conn.execute(
        "INSERT INTO fact_chain(date,code,name,sales_fen,tickets_desc,sessions_desc,"
        "avg_per_session,avg_seat_output,online_rate,ingest_batch)"
        " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,code) DO UPDATE SET"
        " name=excluded.name, sales_fen=excluded.sales_fen, tickets_desc=excluded.tickets_desc,"
        " sessions_desc=excluded.sessions_desc, avg_per_session=excluded.avg_per_session,"
        " avg_seat_output=excluded.avg_seat_output, online_rate=excluded.online_rate,"
        " ingest_batch=excluded.ingest_batch",
        (r.get("date"), r.get("code"), r.get("name"), r.get("sales_fen"),
         r.get("tickets_desc"), r.get("sessions_desc"), r.get("avg_per_session"),
         r.get("avg_seat_output"), r.get("online_rate"), r.get("ingest_batch")),
    )


def put_date_status(conn: sqlite3.Connection, date: str, n: Optional[int],
                    status: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO date_status(date,n_cinemas,status,checked_at,note) VALUES(?,?,?,?,?)"
        " ON CONFLICT(date) DO UPDATE SET n_cinemas=excluded.n_cinemas,"
        " status=excluded.status, checked_at=excluded.checked_at, note=excluded.note",
        (date, n, status, now(), note[:300]),
    )


def purge_ranklist_day(conn: sqlite3.Connection, date: str) -> None:
    """重抓前清掉该日由影院榜写入的旧行，避免上一轮的残缺结果残留。

    只删 sales_src='ranklist'，保留分片接口写入的精确行。
    """
    conn.execute("DELETE FROM fact_cinema_day WHERE date=? AND sales_src='ranklist'", (date,))


def upsert_national(conn: sqlite3.Connection, r: dict) -> None:
    conn.execute(
        "INSERT INTO fact_national(date,sales_yuan,split_sales_yuan,svc_fee_ratio,"
        "gap_second,update_ts,ingested_at) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(date) DO UPDATE SET sales_yuan=excluded.sales_yuan,"
        " split_sales_yuan=excluded.split_sales_yuan, svc_fee_ratio=excluded.svc_fee_ratio,"
        " gap_second=excluded.gap_second, update_ts=excluded.update_ts,"
        " ingested_at=excluded.ingested_at",
        (r.get("date"), r.get("sales_yuan"), r.get("split_sales_yuan"),
         r.get("svc_fee_ratio"), r.get("gap_second"), r.get("update_ts"),
         r.get("ingested_at") or now()),
    )


def upsert_movie_day(conn: sqlite3.Connection, r: dict) -> None:
    conn.execute(
        "INSERT INTO fact_movie_day(movie_code,show_date,release_days,sales_fen,sales_count,"
        "session_count,seat_count,sales_rate,session_rate,seat_rate,avg_per_session,week,"
        "ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(movie_code,show_date) DO UPDATE SET"
        " release_days=excluded.release_days, sales_fen=excluded.sales_fen,"
        " sales_count=excluded.sales_count, session_count=excluded.session_count,"
        " seat_count=excluded.seat_count, sales_rate=excluded.sales_rate,"
        " session_rate=excluded.session_rate, seat_rate=excluded.seat_rate,"
        " avg_per_session=excluded.avg_per_session, week=excluded.week,"
        " ingested_at=excluded.ingested_at",
        (r.get("movie_code"), r.get("show_date"), r.get("release_days"), r.get("sales_fen"),
         r.get("sales_count"), r.get("session_count"), r.get("seat_count"),
         r.get("sales_rate"), r.get("session_rate"), r.get("seat_rate"),
         r.get("avg_per_session"), r.get("week"), r.get("ingested_at") or now()),
    )


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


_PK_CACHE: dict = {}


def _table_meta(conn: sqlite3.Connection, table: str):
    if table not in _PK_CACHE:
        info = list(conn.execute(f"PRAGMA table_info({table})"))
        cols = [r[1] for r in info]
        pk = [r[1] for r in info if r[5]]
        _PK_CACHE[table] = (cols, pk)
    return _PK_CACHE[table]


def upsert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    """通用 upsert：按表结构自动裁字段、自动取主键。

    数据集很多且结构各异，逐表写 upsert 既啰嗦又容易错列；这里用 PRAGMA 反射生成 SQL，
    只写入表里真实存在的列，冲突时更新非主键列。
    """
    cols, pk = _table_meta(conn, table)
    keys = [k for k in row if k in cols]
    if not keys:
        return
    sets = [f"{k}=excluded.{k}" for k in keys if k not in pk]
    sql = (f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})"
           + (" ON CONFLICT DO UPDATE SET " + ",".join(sets) if sets else " ON CONFLICT DO NOTHING"))
    conn.execute(sql, [row[k] for k in keys])


def put_doc(conn: sqlite3.Connection, dataset: str, key: str, date: Any, payload: Any) -> None:
    conn.execute(
        "INSERT INTO fact_doc(dataset,key,date,payload,ingested_at) VALUES(?,?,?,?,?)"
        " ON CONFLICT(dataset,key) DO UPDATE SET payload=excluded.payload,"
        " date=excluded.date, ingested_at=excluded.ingested_at",
        (dataset, str(key), date, json.dumps(payload, ensure_ascii=False), now()),
    )

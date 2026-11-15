#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共工具：日期、金额行归一、完整性探针。

被 pipeline.py（命令行）与 datasets.py（UI 引擎）共用，避免两份实现漂移。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional

from . import api
from .client import Client, HardError, RetryExhausted
from .parse import parse_count, parse_money, parse_ratio

# 完整性闸门：低于此值视为该日影院维度不可用（实测存在 1~8916 家的塌缩）
MIN_CINEMAS = 11000


def daterange(start: str, end: str) -> List[str]:
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    if b < a:
        a, b = b, a
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


def week_of(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str)
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]


def rank_row(r: dict, date: str) -> dict:
    """榜单行 -> 通用行。

    影院榜没有 `sales` 整数字段（91/91 原始响应确认），只能从 salesDesc 解析，
    单位在「元/万」间漂移，故标记 lossy；省份榜/院线榜有整数 `sales`（单位分）。
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


def date_is_full(client: Client, date: str) -> tuple:
    """廉价精确探针：从大偏移往回探，定位该日影院榜的真实规模档位。

    limit 很小时接口的 hasMore 是假阳性（limit=2 返回 n=2/hasMore=True，
    而底层只有 25 行），所以不能用小 limit 判完整。
    """
    for off in (11000, 3000):
        j = api.cinema_list(client, date, limit=10, offset=off)
        rows = j.get("salesRankList") or []
        if rows:
            return (off == 11000), off + len(rows)
    return False, 0

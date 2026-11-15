#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端点封装：一个数据集一个函数，签名与参数名按 P0 实测裁决固定。

参数红线（写错就是 400 或静默错数）：
  * /searchMovie      -> movieName   （keyword / name 均 400）
  * /searchCinema     -> cinemaName
  * /getChainList     -> geoCode=0 & geoType=0 才是全国；geoType=1 是「区域」，不是全国。
  * /getProvinceList  -> date + limit + offset
  * /getSingleDayData -> 参数被忽略，恒返回 20 条历史纪录
"""
from __future__ import annotations

from typing import Any, Optional

from . import ssr as ssr_mod
from .client import Client


def _rel(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p)


def date_range(c: Client) -> dict:
    return c.get_json("/getDateRange", {}, raw_rel=_rel("date_range")) or {}


def national(c: Client, date: str, with_svc: bool = True) -> dict:
    return c.get_json(
        "/getNational",
        {"date": date, "withSvcFee": "true" if with_svc else "false"},
        raw_rel=_rel("national", f"{date}_{'svc' if with_svc else 'split'}"),
    ) or {}


def province_list(c: Client, date: str, with_svc: bool = True, limit: int = 50, offset: int = 0) -> dict:
    return c.get_json(
        "/getProvinceList",
        {"date": date, "withSvcFee": "true" if with_svc else "false",
         "limit": limit, "offset": offset},
        raw_rel=_rel("province", f"{date}_{offset}"),
    ) or {}


def chain_list(c: Client, date: str, geo_code: int = 0, geo_type: int = 0) -> dict:
    return c.get_json(
        "/getChainList",
        {"date": date, "geoCode": geo_code, "geoType": geo_type},
        raw_rel=_rel("chain", f"{date}_{geo_code}_{geo_type}"),
    ) or {}


def cinema_list(c: Client, date: str, with_svc: bool = True, limit: int = 500,
                offset: int = 0, geo_code: Optional[int] = None,
                geo_type: Optional[int] = None) -> dict:
    params: dict = {"date": date, "withSvcFee": "true" if with_svc else "false",
                    "limit": limit, "offset": offset}
    if geo_code is not None:
        params["geoCode"] = geo_code
    if geo_type is not None:
        params["geoType"] = geo_type
    return c.get_json("/getCinemaList", params,
                      raw_rel=_rel("cinema_list", f"{date}_{offset}")) or {}


def cinema_list_all(c: Client, date: str, with_svc: bool = True, page: int = 500,
                    max_pages: int = 80) -> tuple[list, bool]:
    """翻页拉全量影院榜。返回 (rows, truncated)。"""
    out: list = []
    seen: set = set()
    for i in range(max_pages):
        data = cinema_list(c, date, with_svc=with_svc, limit=page, offset=i * page)
        rows = data.get("salesRankList") or []
        for r in rows:
            code = r.get("code")
            if code and code not in seen:
                seen.add(code)
                out.append(r)
        if not data.get("hasMore") or not rows:
            return out, False
    return out, True


def cinema_box(c: Client, code: str, date: str) -> Optional[dict]:
    """影院 × 影片 × 日。无数据时返回 None（HTTP 204）。"""
    return c.get_json("/getCinemaBoxData", {"cinemaCode": code, "date": date},
                      raw_rel=_rel("cinema_box", date, code))


def cinema_sum(c: Client, code: str, date: str) -> dict:
    return c.get_json("/getCinemaSumData", {"cinemaCode": code, "date": date},
                      raw_rel=_rel("cinema_sum", date, code)) or {}


def cinema_manage(c: Client, code: str, date_type: int = 0) -> list:
    v = c.get_json("/getCinemaManageData", {"cinemaCode": code, "dateType": date_type},
                   raw_rel=_rel("cinema_manage", code))
    return v if isinstance(v, list) else []


def search_movie(c: Client, name: str, limit: int = 200) -> list:
    v = c.get_json("/searchMovie", {"movieName": name, "limit": limit, "offset": 0},
                   raw_rel=_rel("search_movie", name))
    return v if isinstance(v, list) else []


def search_cinema(c: Client, name: str, limit: int = 200) -> list:
    v = c.get_json("/searchCinema", {"cinemaName": name, "limit": limit, "offset": 0},
                   raw_rel=_rel("search_cinema", name))
    return v if isinstance(v, list) else []


def movie_detail(c: Client, code: str) -> tuple[Optional[dict], dict]:
    """SSR 影片详情页。返回 (state, payload)。"""
    html = c.get_html("/movie/detail/" + str(code), raw_rel=_rel("movie_detail", str(code)))
    state = ssr_mod.extract_state(html)
    return state, ssr_mod.movie_payload(state)


def day_data(c: Client, date: str, with_svc: bool = True) -> dict:
    return c.get_json("/getDayData",
                      {"date": date, "withSvcFee": "true" if with_svc else "false"},
                      raw_rel=_rel("day_data", date)) or {}


def period_data(c: Client, date: str, date_type: int) -> dict:
    return c.get_json("/getPeriodData", {"date": date, "dateType": date_type},
                      raw_rel=_rel("period", f"{date}_{date_type}")) or {}


def city_data(c: Client, date: str, movie_code: str) -> dict:
    return c.get_json("/getCityData", {"date": date, "movieCode": movie_code},
                      raw_rel=_rel("city", f"{date}_{movie_code}")) or {}


def single_day(c: Client) -> dict:
    return c.get_json("/getSingleDayData", {}, raw_rel=_rel("single_day")) or {}


def year_data(c: Client, year: int) -> dict:
    return c.get_json("/getYearData", {"year": year}, raw_rel=_rel("year", str(year))) or {}


# ---------------------------------------------------------------- 院线四件套 / 年度 / 历史纪录

def chain_sum(c: Client, code: str, date: str) -> dict:
    return c.get_json("/getChainSumData", {"cinemaChainCode": code, "date": date},
                      raw_rel=_rel("chain_sum", date, str(code))) or {}


def chain_box(c: Client, code: str, date: str) -> Optional[dict]:
    return c.get_json("/getChainBoxData", {"cinemaChainCode": code, "date": date},
                      raw_rel=_rel("chain_box", date, str(code)))


def chain_manage(c: Client, code: str, date_type: int = 0) -> list:
    v = c.get_json("/getChainManageData", {"cinemaChainCode": code, "dateType": date_type},
                   raw_rel=_rel("chain_manage", str(code)))
    return v if isinstance(v, list) else []


def chain_yesterday(c: Client, code: str, geo_code: int = 0, geo_type: int = 0) -> dict:
    v = c.get_json("/getChainYesterday",
                   {"cinemaChainCode": code, "geoCode": geo_code, "geoType": geo_type},
                   raw_rel=_rel("chain_yesterday", str(code)))
    return v if isinstance(v, dict) else {}


def year_tabs(c: Client, year: int) -> list:
    v = c.get_json("/getYearTabs", {"year": year}, raw_rel=_rel("year_tabs", str(year)))
    return v if isinstance(v, list) else []


def release_day(c: Client) -> dict:
    return c.get_json("/getReleaseDayData", {}, raw_rel=_rel("release_day")) or {}


def nation_single_day(c: Client) -> dict:
    return c.get_json("/getNationSingleDayData", {}, raw_rel=_rel("nation_single_day")) or {}


def nation_single_week(c: Client) -> dict:
    return c.get_json("/getNationSingleWeekData", {}, raw_rel=_rel("nation_single_week")) or {}


def nation_single_month(c: Client) -> dict:
    return c.get_json("/getNationSingleMonthData", {}, raw_rel=_rel("nation_single_month")) or {}

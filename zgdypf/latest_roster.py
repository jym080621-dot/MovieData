"""Download the latest public cinema ranking as an importable candidate CSV."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import history_roster
from .client import BadPayload, Client
from .export import write_csv
from .movie_history import BatchEnded, USER_STOPPED, _batch_client, _report_file

SOURCE = "中国电影票房·公开影院票房榜"
MAX_DATES = 7
FIELDS = ("date", "cinema_code", "cinema_name", "source", "source_url", "national_roster_coverage",
          "invalid_source_rows", "duplicate_source_rows")


def download(out_dir, *, stopped=lambda: False, emit=print, client_factory=Client):
    """Use the site's most recent nonempty day, without touching the database."""
    client, _ = _batch_client(None, 1.0, 2 * (MAX_DATES + history_roster.MAX_PAGES), 5,
                              stopped, client_factory)
    response = client.fetch("/getDateRange")
    data = response.data
    try:
        start, end = data["startDate"], data["endDate"]
        history_roster.check_dates(start, end)
        earliest, latest = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except (KeyError, TypeError, ValueError) as exc:
        raise BadPayload("站点没有返回有效的可查询日期范围") from exc
    if response.status != 200:
        raise BadPayload("站点没有返回有效的可查询日期范围")

    for ago in range(min(MAX_DATES, (latest - earliest).days + 1)):
        day = (latest - dt.timedelta(days=ago)).isoformat()
        rows, invalid, duplicates = _read_day(client, day, emit)
        if not rows:
            emit(f"{day} 暂无可用影院榜，检查前一日。")
            continue
        if stopped():
            raise BatchEnded(USER_STOPPED)
        if invalid or duplicates:
            emit(f"⚠ 源站榜单含 {invalid:,} 条无有效编码记录、{duplicates:,} 条重复编码记录，CSV 按有效编码去重。")
        path = Path(out_dir) / f"cinema_roster_{day}.csv"
        url = client.base + "/cinema/"
        with _report_file(path) as temp:
            write_csv(str(temp), FIELDS, ((day, code, name, SOURCE, url, "unverified", invalid, duplicates)
                                         for code, name in sorted(rows.items())))
            if stopped():
                raise BatchEnded(USER_STOPPED)
        return {"path": path, "date": day, "latest_date": end, "count": len(rows), "source_url": url,
                "invalid_source_rows": invalid, "duplicate_source_rows": duplicates}
    raise BadPayload("最近可查询日期没有返回任何影院；未生成新 CSV")


def _read_day(client, day, emit):
    cinemas = {}
    offset = 0
    invalid = duplicates = 0
    for page in range(1, history_roster.MAX_PAGES + 1):
        rows, has_more = history_roster.fetch_site_page(client, day, offset, allow_initial_204=True)
        if not rows and (has_more or offset):
            raise BadPayload(f"{day} 影院榜分页中断，未生成新 CSV")
        before = len(cinemas)
        for row in rows:
            code = row.get("code") if isinstance(row, dict) else None
            if not history_roster.valid_code(code):
                invalid += 1
                continue
            if code in cinemas:
                duplicates += 1
            name = row.get("name")
            if not cinemas.get(code):
                cinemas[code] = name if isinstance(name, str) else ""
        if rows and len(cinemas) == before:
            raise BadPayload(f"{day} 影院榜第 {page} 页未增加有效编码，可能是重复页或响应异常，未生成新 CSV")
        if rows:
            emit(f"{day} 影院榜已读取 {page} 页，共 {len(cinemas):,} 个影院编码。")
        if not has_more:
            return cinemas, invalid, duplicates
        offset += len(rows)
    raise BadPayload(f"{day} 影院榜分页超出保护上限，未生成新 CSV")

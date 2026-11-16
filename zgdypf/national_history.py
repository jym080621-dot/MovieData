"""全国历史页面的离线计划、受限执行和严格审计。不会推断全国名单完整。"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from . import __version__, db, history_roster, movie_history as history
from .client import Client, DEFAULT_JITTER
from .common import daterange

NOTICE = "全国完整性未证实：站点逐日榜单和补充名单无法证明覆盖全国每一家影院；不得作为全国完整数据。"
DEFAULT_RATE = 2.0
MAX_RATE = 5.0
# Rounded from the 12,819 distinct candidate IDs in the supplied v1.4.0 run.
REFERENCE_CINEMAS = 13_000
TURNAROUND_SECONDS = (0.15, 0.50)
LABELS = {
    "with_movie": "有目标影片", "without_movie": "完整片目中无目标影片",
    "empty_204": "HTTP 204 待复核", "deferred_204": "HTTP 204 待复核",
    "inconsistent": "明细不自洽", "error": "请求异常", "missing": "未采集",
}


@contextmanager
def snapshot(path):
    if Path(path).is_file():
        with history.open_readonly(path) as conn:
            yield conn
    else:
        # A preview must neither create a database nor construct a network client.
        with closing(db.connect(":memory:")) as conn:
            yield conn


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def make_plan(db_path, movie, start, end, *, max_requests=3000, minutes=60,
              rate=DEFAULT_RATE, retry_empty=False, refresh_roster=False):
    history.validate(movie, start, end, "roster")
    if dt.date.fromisoformat(end) >= dt.date.today():
        raise ValueError("历史采集的结束日期必须早于今天")
    if type(max_requests) is not int or max_requests < 0:
        raise ValueError("请求上限必须为非负整数，0 表示不限")
    if not math.isfinite(minutes) or minutes < 0 or not math.isfinite(rate) or not 0.1 <= rate <= MAX_RATE:
        raise ValueError(f"分钟数须为非负数，0 表示不限。频率须为 0.1–{MAX_RATE:g} 次/秒")
    options = dict(max_requests=max_requests, minutes=minutes, rate=rate,
                   retry_empty=bool(retry_empty), refresh_roster=bool(refresh_roster))
    days, totals = [], Counter()
    with snapshot(db_path) as conn:
        refreshes = history_roster.stored_refreshes(conn)
        for day in daterange(start, end):
            _, _, _, _, states, sources = history._day_plan(conn, movie, day, "national", ())
            counts = Counter(states.values())
            record = refreshes.get(day)
            refresh = bool(refresh_roster or not record or record["status"] == "fetching")
            pending = len(history._pending(states, retry_empty))
            row = dict(date=day, targets=len(states), cached=counts["with_movie"] + counts["without_movie"],
                       pending=pending, states=dict(counts), roster_request_needed=refresh,
                       roster_status=record["status"] if record else "not_collected",
                       roster_rows=record["row_count"] if record else 0,
                       roster_offset=record["next_offset"] if record else 0,
                       roster_pages=record["pages"] if record else 0,
                       evidence_hash=_hash([states, sources, dict(record) if record else None]),
                       national_complete=False)
            days.append(row)
            totals.update(targets=len(states), cached=row["cached"], pending=pending,
                          roster_days=int(refresh), deferred_204=counts["empty_204"] + counts["deferred_204"])
    logical_min = totals["pending"] + totals["roster_days"]
    plan = dict(version=__version__, scope="national_all_cinemas", database=str(Path(db_path).resolve()),
                movie=movie, start=start, end=end, options=options, days=days, totals=dict(totals),
                first_attempt_requests_min=logical_min,
                first_attempt_requests_exact=None if totals["roster_days"] else logical_min,
                rate_only_minutes_min=max(0, logical_min - 1) / rate / 60,
                collection_estimate=estimate_collection(days, totals, rate),
                national_complete=False, notice=NOTICE,
                execution="先续取逐日名单全部分页，再逐家补采；新发现影院全部入队。批次额度仅限制本批请求，不限制目标影院总数。重试也计入额度。")
    plan["id"] = _hash(plan)
    return plan


def estimate_collection(days, totals, rate):
    """Estimate a workload for the selected dates, based on the evidence available offline."""
    roster_total = roster_remaining = 0
    for day in days:
        pages = day["roster_pages"]
        fetching = day["roster_status"] == "fetching"
        expected_pages = max(1, pages + int(fetching))
        roster_total += expected_pages
        if day["roster_request_needed"]:
            roster_remaining += expected_pages - pages if fetching else expected_pages

    use_reference = bool(totals["roster_days"] or not totals["targets"])
    projected_targets = totals["targets"]
    if use_reference:
        projected_targets = max(projected_targets, len(days) * REFERENCE_CINEMAS)
    extra_candidates = projected_targets - totals["targets"]
    full = projected_targets + roster_total
    remaining = totals["pending"] + roster_remaining
    if totals["roster_days"]:
        remaining += extra_candidates

    def estimate(requests, turnaround, jitter):
        if requests == 0:
            return 0.0
        # A serial request and the next start interval overlap.
        return round(turnaround + (requests - 1) * max(turnaround, 1 / rate + jitter), 3)

    return dict(reference_cinemas_per_day=REFERENCE_CINEMAS if use_reference else None,
                projected_cinema_days=projected_targets, projected_additional_candidates=extra_candidates,
                full_requests=full, remaining_requests=remaining,
                roster_requests_full=roster_total, roster_requests_remaining=roster_remaining,
                turnaround_seconds=list(TURNAROUND_SECONDS),
                full_seconds=[estimate(full, lag, jitter) for lag, jitter in zip(TURNAROUND_SECONDS, DEFAULT_JITTER)],
                remaining_seconds=[estimate(remaining, lag, jitter)
                                   for lag, jitter in zip(TURNAROUND_SECONDS, DEFAULT_JITTER)])


def _duration_range(seconds):
    lo, hi = seconds
    if not hi:
        return "0 秒"
    if hi < 60:
        unit, scale = "秒", 1
    elif hi < 3600:
        unit, scale = "分钟", 60
    else:
        unit, scale = "小时", 3600
    return f"{lo / scale:,.1f}–{hi / scale:,.1f} {unit}"


def format_budget(max_requests, minutes):
    requests = f"{max_requests:,} 次" if max_requests else "不限"
    duration = f"{minutes:g} 分钟" if minutes else "不限"
    return f"请求次数上限 {requests}，时间上限 {duration}"


def save_plan(plan, out_dir):
    dest = Path(out_dir) / f"plan_{plan['id'][:16]}.json"
    with history._report_file(dest) as temp:
        temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def format_plan(plan):
    t, o = plan["totals"], plan["options"]
    exact = plan["first_attempt_requests_exact"]
    known = f"{exact:,} 次" if exact is not None else f"至少 {plan['first_attempt_requests_min']:,} 次"
    estimate = plan["collection_estimate"]
    reference = estimate["reference_cinemas_per_day"]
    if not t["targets"] and not t["roster_days"]:
        basis = (f"目前没有已知影院，从零估算参考每天约 {reference:,} 家。需重查或导入名单，"
                 "当前采集队列为空。")
    elif reference:
        basis = (f"名单尚需核实，按每天约 {reference:,} 家与已有候选数取较大值。"
                 "额外分页或新增影院会改变估算。")
    else:
        basis = "按本地已知名单估算，后续发现新影院会改变估算。"
    empty_note = (f"本次续跑估算排除了 {t['deferred_204']:,} 个暂缓的 204。\n"
                  if t["deferred_204"] and not o["retry_empty"] else "")
    timing_basis = "预计耗时按串行请求、0.15–0.50 秒响应及处理时间和限速间隔估算，另计重试、导出及批次间等待。"
    return (f"影片编码 {plan['movie']}　|　{plan['start']} 至 {plan['end']}（含首尾）\n"
            f"全程估算（从零开始，{o['rate']:g} 次/秒上限）：约 {estimate['full_requests']:,} 次请求"
            f" · {_duration_range(estimate['full_seconds'])}。\n"
            f"从当前断点估算（按本计划）：约 {estimate['remaining_requests']:,} 次请求"
            f" · {_duration_range(estimate['remaining_seconds'])}。\n"
            f"{basis}\n{timing_basis}\n{empty_note}"
            f"固定范围：全国全部影院；不抽样、不设 Top N、不按票房覆盖率截断。\n"
            f"当前已知 {t.get('targets', 0):,} 个影院日；可复用 {t.get('cached', 0):,}；"
            f"本轮待补 {t.get('pending', 0):,}；204 待复核 {t.get('deferred_204', 0):,}。\n"
            f"需获取或续取名单 {t.get('roster_days', 0)} 天；仅本地已知的首轮必要请求 {known}。\n"
            f"{format_budget(o['max_requests'], o['minutes'])}，每秒最多 {o['rate']:g} 次。\n"
            f"204 重试：{'是' if o['retry_empty'] else '否'}；重新核实名单：{'是' if o['refresh_roster'] else '否'}。\n"
            f"{plan['execution']}\n{NOTICE}")


def export_audit(db_path, out_dir, movie, start, end):
    history.validate(movie, start, end, "roster")
    root = Path(out_dir) / f"{movie}_{start}_{end}"
    paths = {key: root.with_name(root.name + suffix) for key, suffix in (
        ("matrix", "_cinema_days.csv"), ("audit", "_daily_audit.csv"), ("summary", "_summary.json"))}
    totals = Counter()
    daily = []
    with snapshot(db_path) as conn, history._report_file(paths["matrix"]) as temp:
        refreshes = history_roster.stored_refreshes(conn)
        with temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("date", "cinema_code", "cinema_name", "movie_code", "status", "status_zh",
                             "sales_fen", "sales_yuan", "roster_sources", "national_complete"))
            for day in daterange(start, end):
                _, _, _, _, states, sources = history._day_plan(conn, movie, day, "national", ())
                values = {r[0]: r[1] for r in conn.execute(
                    "SELECT cinema_code,sales_fen FROM fact_cinema_movie WHERE date=? AND movie_code=?", (day, movie))}
                names = dict(conn.execute("SELECT cinema_code,cinema_name FROM dim_cinema"))
                counts = Counter(states.values())
                for code, state in states.items():
                    # Only a validated full slice can establish absence. Unknown is never zero.
                    cents = values.get(code) if state == "with_movie" else 0 if state == "without_movie" else None
                    yuan = None if cents is None else f"{cents // 100}.{cents % 100:02}"
                    writer.writerow((day, code, names.get(code), movie, state, LABELS[state], cents,
                                     yuan, "|".join(sources.get(code, ())), "false"))
                observed = counts["with_movie"] + counts["without_movie"]
                refresh = refreshes.get(day)
                daily.append(dict(date=day, target_count=len(states), observed=observed,
                                  **{s: counts[s] for s in history.DAY_STATES},
                                  known_roster_observed=bool(states) and observed == len(states),
                                  roster_status=refresh["status"] if refresh else "not_collected",
                                  roster_rows=refresh["row_count"] if refresh else 0,
                                  national_complete=False, national_roster_coverage="unverified"))
                totals.update(counts)
    with history._report_file(paths["audit"]) as temp:
        with temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(daily[0]))
            writer.writeheader()
            writer.writerows(daily)
    summary = dict(movie=movie, start=start, end=end, states=dict(totals), days=daily,
                   national_complete=False, notice=NOTICE)
    with history._report_file(paths["summary"]) as temp:
        temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(paths=paths, summary=summary)


def run_plan(plan, raw_dir, out_dir, *, stopped=lambda: False, emit=print, progress=lambda *args: None,
             on_coverage=lambda coverage: None,
             client_factory=Client):
    # Recompute before constructing a client: altered inputs or changed cache require a new review.
    current = make_plan(plan["database"], plan["movie"], plan["start"], plan["end"], **plan["options"])
    if current != plan:
        raise ValueError("计划已变更或本地数据已更新，请重新生成并审查执行计划")
    on_coverage(dict(current["totals"]))
    db_path = plan["database"]
    with closing(db.connect(db_path)):
        pass
    save_plan(plan, out_dir)
    o = plan["options"]
    client, guard = history._batch_client(raw_dir, o["rate"], o["max_requests"], o["minutes"], stopped, client_factory,
                                         max_rate=MAX_RATE, allow_unlimited=True)
    progress("核实逐日影院名单", 0)
    def roster_log(message):
        emit(message)
        progress("核实逐日影院名单", client.stats.requests)

    roster = history._refresh_roster(db_path, plan["start"], plan["end"], client, guard,
                                     o["refresh_roster"], roster_log)
    # Discovering new IDs changes the denominator; never use the per-batch HTTP
    # allowance as the total work. Reuse the validated plan if no roster was read.
    baseline = (make_plan(db_path, plan["movie"], plan["start"], plan["end"], **o)
                if client.stats.requests else current)["totals"]
    on_coverage(dict(baseline))

    def detail_progress(day, counts):
        on_coverage(dict(baseline, cached=baseline.get("cached", 0) + counts.get("ok", 0)))
        progress(day, client.stats.requests)

    detail = {}
    if not roster.get("stopped"):
        progress("逐影院补采完整片目", client.stats.requests)
        detail = history._repair(db_path, plan["movie"], plan["start"], plan["end"], "national", (),
                                 client, guard, o["retry_empty"], False, emit,
                                 on_progress=detail_progress,
                                 defer_empty_streak=bool(o["max_requests"] or o["minutes"]))
    failed = bool(roster.get("failed") or detail.get("failed") or detail.get("errors"))
    reason = roster.get("stopped") or detail.get("stopped")
    if reason is None and stopped():
        reason = history.USER_STOPPED
    result = dict(requests=client.stats.requests, retries=client.stats.retries,
                  saved=detail.get("ok", 0), empty=detail.get("empty", 0),
                  failed=failed, paused=reason == history.USER_STOPPED,
                  reason=reason or "本批处理结束",
                  national_complete=False)
    on_coverage(dict(baseline, cached=baseline.get("cached", 0) + result["saved"]))
    progress("生成逐日审计及 CSV", client.stats.requests)
    result["reports"] = export_audit(db_path, out_dir, plan["movie"], plan["start"], plan["end"])
    on_coverage(coverage_from_summary(result["reports"]["summary"]))
    manifest = Path(out_dir) / f"batch_{plan['id'][:16]}_{uuid4().hex[:8]}.json"
    with history._report_file(manifest) as temp:
        temp.write_text(json.dumps(dict(plan_id=plan["id"], finished_at=db.now(), **result),
                                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    result["manifest"] = manifest
    return result


def coverage_from_summary(summary):
    states = summary["states"]
    return dict(targets=sum(states.values()), cached=states.get("with_movie", 0) + states.get("without_movie", 0),
                deferred_204=states.get("empty_204", 0) + states.get("deferred_204", 0),
                roster_days=sum(day["roster_status"] in ("not_collected", "fetching") for day in summary["days"]))

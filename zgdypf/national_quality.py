"""v1.5.1: explicit, conservative assessment of known cinema-day gaps.

The site ranking and local registry are not authoritative national coverage.
A completed local queue must never imply countrywide completeness.
"""

VERIFIED = ("with_movie", "without_movie")
OPEN_STATES = ("missing", "error", "inconsistent", "empty_204", "deferred_204")


def assess_day(states, roster_status, target_count):
    """Pure function: a day summary suitable for CSV and programmatic review."""
    count = {key: int(states.get(key, 0)) for key in (*VERIFIED, *OPEN_STATES)}
    if any(value < 0 for value in count.values()):
        raise ValueError("state counts must be nonnegative")
    if target_count < 0:
        raise ValueError("target_count must be nonnegative")
    verified = sum(count[key] for key in VERIFIED)
    unresolved = sum(count[key] for key in OPEN_STATES)
    if verified + unresolved != target_count:
        raise ValueError("state counts do not match target_count")
    roster_problem = roster_status != "listed" or not target_count
    return {
        "known_verified": verified,
        "known_unresolved": unresolved,
        "retry_204": count["empty_204"] + count["deferred_204"],
        "repair_failures": count["missing"] + count["error"] + count["inconsistent"],
        "roster_problem": bool(roster_problem),
        "known_scope_complete": bool(target_count and unresolved == 0),
        "national_complete": False,
    }


def summarize_days(days):
    """Counts *actionable* outstanding problems; national completeness stays unverified."""
    return {
        "known_unresolved": sum(day["known_unresolved"] for day in days),
        "retry_204": sum(day["retry_204"] for day in days),
        "repair_failures": sum(day["repair_failures"] for day in days),
        "roster_problem_days": sum(bool(day["roster_problem"]) for day in days),
        "known_scope_complete_days": sum(bool(day["known_scope_complete"]) for day in days),
        "all_known_scope_verified": bool(days) and all(day["known_scope_complete"] for day in days),
        "national_complete": False,
    }


def followup_message(summary):
    """Return a non-misleading next-action message, not an assertion of completeness."""
    issues = []
    if summary["retry_204"]:
        issues.append(f"{summary['retry_204']:,} 个 HTTP 204 待复核")
    if summary["repair_failures"]:
        issues.append(f"{summary['repair_failures']:,} 个失败/缺失/冲突影院日")
    if summary["roster_problem_days"]:
        issues.append(f"{summary['roster_problem_days']} 天名单异常或未核实")
    if issues:
        return "；".join(issues) + "。可生成复核计划；复核后仍须核对全国名单来源。"
    return "已知候选任务暂未发现缺口，但全国影院名单完整性仍未得到证明。"

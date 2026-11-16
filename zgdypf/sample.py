#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分层抽样：省份 × 规模三分位。

维度选择与限制（必须写进报告）：
  * 省份：可从「专资办行政区码前两位」精确推出（440113 -> 440000 广东省），
    但影院榜接口不返回行政区码，只能：全量名单用名称前缀解析（有误差），
    抽样名单再逐个调 /searchCinema 拿 cityCode 做精确校正（共 n 次请求）。
  * 院线归属：24 个端点里**没有**「影院 -> 院线」的映射，
    /getChainBoxData 只到院线 × 影片。故院线维度**无法**用于影院抽样，
    这是本方案相对原始设想的实质缺口，需在报告中声明。
  * 规模：用当日 sales（分）在**省内**分三分位，保证每个省内部都有大中小。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

_PROV_SUFFIX = ("维吾尔自治区", "壮族自治区", "回族自治区", "特别行政区", "自治区", "省", "市")
DIRECT_CITIES = {"北京市", "上海市", "天津市", "重庆市"}
_CITY_RE = re.compile(r"^([\u4e00-\u9fa5]{2,9}?(?:市|自治州|地区|盟))")
_ANY_CITY_RE = re.compile(r"([\u4e00-\u9fa5]{2,4}市)")


def short_name(full: str) -> str:
    if not isinstance(full, str):
        return ""
    for suf in _PROV_SUFFIX:
        if full.endswith(suf) and len(full) > len(suf):
            return full[: -len(suf)]
    return full


def build_alias_map(provinces: Sequence[dict]) -> List[tuple]:
    """[(alias, province_name)]，长别名优先，避免「内蒙古」被「内蒙」之类截断。"""
    pairs = []
    for p in provinces:
        name = p.get("name") or ""
        if not name:
            continue
        for alias in {name, short_name(name)}:
            if alias:
                pairs.append((alias, name))
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


def parse_province_city(cinema_name: str, aliases: Sequence[tuple]) -> Dict[str, Any]:
    """从影院名解析省份与城市。失败返回 None 值（软失败）。"""
    name = cinema_name or ""
    head = name[:16]
    best: Optional[tuple] = None
    for alias, prov in aliases:
        idx = head.find(alias)
        if idx < 0:
            continue
        if best is None or (idx, -len(alias)) < (best[0], -len(best[1])):
            best = (idx, alias, prov)
    if best is None:
        return {"province": None, "city": None, "is_direct_city": 0, "matched": None}

    idx, alias, prov = best
    rest = head[idx + len(alias):]
    if prov in DIRECT_CITIES:
        return {"province": prov, "city": prov, "is_direct_city": 1, "matched": alias}

    m = _CITY_RE.match(rest)
    if m:
        return {"province": prov, "city": m.group(1), "is_direct_city": 0, "matched": alias}
    m = _ANY_CITY_RE.search(rest[:9])
    if m:
        return {"province": prov, "city": m.group(1), "is_direct_city": 0, "matched": alias}
    return {"province": prov, "city": None, "is_direct_city": 0, "matched": alias}


def province_by_code(code: Optional[str], provinces: Sequence[dict]) -> Optional[str]:
    """440113 -> 440000 -> 广东省（专资办 6 位行政区码）。"""
    if not code or not isinstance(code, str) or len(code) < 2:
        return None
    pcode = code[:2] + "0000"
    for p in provinces:
        if str(p.get("code")) == pcode:
            return p.get("name")
    return None


def _tercile_labels(n: int) -> List[int]:
    """按排序位次给 1（小）/2（中）/3（大）标签，三等分。

    入参 lst 是**按票房降序**排的，所以 i=0 是最大的一家，应得 3。
    （早期版本这里写反了，导致 T1 实际是最大的三分之一；已修正并回填库内标签。）
    """
    labels = []
    for i in range(n):
        frac = (i + 0.5) / max(n, 1)
        labels.append(3 if frac < 1 / 3 else (2 if frac < 2 / 3 else 1))
    return labels


def build_sample(rows: Sequence[dict], provinces: Sequence[dict], n: int) -> List[dict]:
    """rows: [{code,name,sales,...}] 全量影院榜；返回带 province/stratum 标记的样本。"""
    aliases = build_alias_map(provinces)
    by_prov: Dict[str, List[dict]] = {}
    unparsed = 0
    for r in rows:
        if not r.get("code"):
            continue
        info = parse_province_city(r.get("name") or "", aliases)
        prov = info["province"]
        if not prov:
            unparsed += 1
            continue
        r2 = dict(r)
        r2.update(info)
        by_prov.setdefault(prov, []).append(r2)

    total_sales = sum((x.get("sales") or 0) for lst in by_prov.values() for x in lst) or 1
    provs = sorted(by_prov.items(),
                   key=lambda kv: -sum((x.get("sales") or 0) for x in kv[1]))

    # 按票房份额分配配额，下限 2、上限 20
    quotas: Dict[str, int] = {}
    for prov, lst in provs:
        share = sum((x.get("sales") or 0) for x in lst) / total_sales
        q = int(round(n * share))
        quotas[prov] = max(2, min(20, q))

    # 归一化到 n
    diff = n - sum(quotas.values())
    order = [p for p, _ in provs]
    i = 0
    while diff != 0 and order:
        p = order[i % len(order)]
        step = 1 if diff > 0 else -1
        if quotas[p] + step >= 2:
            quotas[p] += step
            diff -= step
        i += 1
        if i > 10000:
            break

    sample: List[dict] = []
    for prov, _ in provs:
        lst = sorted(by_prov[prov], key=lambda x: -(x.get("sales") or 0))
        q = min(quotas.get(prov, 0), len(lst))
        if q <= 0:
            continue
        labels = _tercile_labels(len(lst))
        buckets: Dict[int, List[dict]] = {1: [], 2: [], 3: []}
        for lab, item in zip(labels, lst):
            buckets[lab].append(item)
        base, rem = divmod(q, 3)
        take = {1: base, 2: base, 3: base}
        for t in (3, 2, 1)[:rem]:
            take[t] += 1
        for t in (3, 2, 1):
            for item in buckets[t][: take[t]]:
                item = dict(item)
                item["stratum"] = f"{prov}|T{t}"
                sample.append(item)

    # 若配额不足（省少人少），用全体按规模补齐
    if len(sample) < n:
        have = {x["code"] for x in sample}
        pool = sorted([r for lst in by_prov.values() for r in lst],
                      key=lambda x: -(x.get("sales") or 0))
        for r in pool:
            if len(sample) >= n:
                break
            if r["code"] in have:
                continue
            item = dict(r)
            item["stratum"] = f"{r.get('province')}|T?"
            sample.append(item)
            have.add(r["code"])
    sample.sort(key=lambda x: x["stratum"])
    sample[0]["_unparsed_names"] = unparsed if sample else 0
    return sample[:n]

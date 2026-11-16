#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字段解析：单位、占比、软取值。

P0 裁决的关键事实（务必保留注释，否则后人必踩）：

  * 裸数字（无单位）在**票房类 Desc 字段**里是「元」，不是「万」，也不是「分」。
      影院 11061601 / 2026-11-12 : salesDesc="9453"   , sales=945380 分 = 9453.8 元   ✔
      影院 33019193 / 2026-11-12 : salesDesc="5.65万", sales=5650940 分 = 56509.4 元 ✔
    同一个字段在同一天的不同影院上，单位会在「元」和「万」之间漂移。

  * 例外：字段名自带 InWan 的（salesInWanDesc / salesInWan），裸数字是「万」。

  * 整数字段（sales / showCount / sessionCount / salesCount）单位是「分」或「个/人次」，
    能用整数就绝不用 Desc —— Desc 是四舍五入后的展示串。

  * "9.76万"、"3.46万" 这类计数 Desc 同样是舍入值，标注 lossy=True。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

_NUM = r"[+-]?\d+(?:[.,]\d+)*"
_RE_SCALED = re.compile(r"^\s*(" + _NUM + r")\s*(万|亿|元)?\s*$")
_RE_RATIO = re.compile(r"^\s*(" + _NUM + r")\s*(%|％)?\s*$")
_SCALE = {None: 1.0, "元": 1.0, "万": 1e4, "亿": 1e8}
_NULLISH = {"", "--", "-", "—", "null", "None", "nan"}


@dataclass(frozen=True)
class Money:
    """解析后的金额。yuan 是唯一权威数值，fen 由它换算（可能 ±1 分误差）。

    cmp: '=' 精确 / '<' 上界 / '>' 下界。实测 getCinemaSumData 对日票房不足
    1000 元的影院返回 {"unit":"万","value":"<0.1"}，这是第三种非数字形态。
    """

    yuan: float
    unit: str          # 原始单位：'元' / '万' / '亿'
    scaled: bool       # 是否发生 万/亿 缩放（= 一定伴随舍入）
    raw: str
    cmp: str = "="

    @property
    def fen(self) -> int:
        return int(round(self.yuan * 100.0))

    @property
    def lossy(self) -> bool:
        return self.scaled


@dataclass(frozen=True)
class Count:
    value: float
    unit: str
    scaled: bool
    raw: str

    @property
    def lossy(self) -> bool:
        return self.scaled


def _clean(text: Any) -> Optional[str]:
    if text is None:
        return None
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return str(text)
    if not isinstance(text, str):
        return None
    s = text.strip().replace("\u00a0", " ").replace(" ", "")
    s = s.replace(",", "")
    if s in _NULLISH:
        return None
    return s


def parse_money(text: Any, bare_unit: str = "yuan") -> Optional[Money]:
    """解析金额 Desc。

    bare_unit: 'yuan'（默认，裸数字按元）或 'wan'（字段名含 InWan 时用）。
    无单位时按 bare_unit 解释；有「万/亿」时以字串单位为准。
    """
    s = _clean(text)
    if not s:
        return None
    m = _RE_SCALED.match(s)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    if unit is None:
        factor = 1e4 if bare_unit == "wan" else 1.0
        return Money(value * factor, "万" if bare_unit == "wan" else "元", False, text)
    return Money(value * _SCALE[unit], unit, unit != "元", text)


def parse_count(text: Any) -> Optional[Count]:
    """解析人次/场次 Desc（"101" / "9.76万"）。"""
    s = _clean(text)
    if not s:
        return None
    m = _RE_SCALED.match(s)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    if unit == "元":
        return None
    factor = _SCALE[unit]
    return Count(value * factor, unit or "", unit is not None, text)


def parse_ratio(text: Any) -> Optional[float]:
    """'20.63%' -> 0.2063；已是小数('0.2063')时原样返回。"""
    s = _clean(text)
    if not s:
        return None
    m = _RE_RATIO.match(s)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    if m.group(2):
        return value / 100.0
    return value


def parse_int(text: Any) -> Optional[int]:
    s = _clean(text)
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def pick(d: Any, *keys: str, default: Any = None) -> Any:
    """多候选键软取值：任一键命中即返回，缺失返回 default（不抛异常）。"""
    if not isinstance(d, Mapping):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def as_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def unit_money(d: Any, bare_unit: str = "wan") -> Optional[Money]:
    """解析 {value, unit} 结构（getNational / getCinemaSumData 的 salesDesc）。

    实测三种形态：
      {"unit":"万","value":"1714.11"}   正常
      {"unit":"万","value":"--"}        无数据
      {"unit":"万","value":"<0.1"}      有数据但不足 0.1 万（上界）
    """
    if not isinstance(d, Mapping):
        # 少数接口直接给字符串
        return parse_money(d, bare_unit=bare_unit)
    value = d.get("value")
    unit = d.get("unit")
    if isinstance(value, str):
        s = value.strip()
        if s in _NULLISH:
            return None
        cmp = "="
        for ch in ("<", "＜"):
            if s.startswith(ch):
                s, cmp = s[len(ch):].strip(), "<"
                break
        for ch in (">", "＞"):
            if s.startswith(ch):
                s, cmp = s[len(ch):].strip(), ">"
                break
        if unit and not re.search(r"[万亿元]", s):
            m = parse_money(s + str(unit), bare_unit=bare_unit)
        else:
            m = parse_money(s, bare_unit=bare_unit)
        if m is None:
            return None
        return Money(m.yuan, m.unit, m.scaled, d.get("value"), cmp) if cmp != "=" else m
    if isinstance(value, (int, float)):
        factor = _SCALE.get(unit or "万", 1e4)
        return Money(float(value) * factor, unit or "万", unit is not None, str(value))
    return None

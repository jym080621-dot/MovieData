#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSR 抽取：从 /movie/detail/<code> 的 HTML 中取出 window.__INITIAL_STATE__。

前序脚本的缺陷：正则匹配失败时直接 raise，所谓「括号配对兜底」根本没机会执行。
这里反过来 —— 先定位标记，再做**字符串感知的括号配对**切片，最后才退化为正则。
这样站点结构轻微变化（换引号、加空格、改分号、嵌套 {}）都不会整批中断。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_MARKER = "__INITIAL_STATE__"


def _slice_balanced(text: str, start: int) -> Optional[str]:
    """从 text[start] == '{' 起做括号配对，正确跳过 JSON 字符串内的 {}[]。"""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_state(html: str) -> Optional[dict]:
    """返回解析后的 __INITIAL_STATE__ 字典；失败返回 None（软失败）。"""
    if not html:
        return None
    idx = html.find(_MARKER)
    if idx < 0:
        return None
    brace = html.find("{", idx)
    if brace >= 0:
        snippet = _slice_balanced(html, brace)
        if snippet:
            try:
                obj = json.loads(snippet)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    # 退化路径：截到 </script> 再试
    end = html.find("</script>", idx)
    if end < 0:
        end = len(html)
    seg = html[idx:end]
    m = re.search(r"=\s*(\{.*\})\s*;?\s*$", seg, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def movie_payload(state: Optional[dict]) -> dict:
    """把 __INITIAL_STATE__ 归一化成我们关心的四块，缺失即空。"""
    st = state or {}
    info = st.get("movieInfoData") or {}
    sums = st.get("movieBoxSum") or []
    tops = st.get("movieBoxTop") or []
    rows = st.get("movieIndexData") or []
    if not isinstance(rows, list):
        rows = []
    return {
        "info": info if isinstance(info, dict) else {},
        "box_sum": sums if isinstance(sums, list) else [],
        "box_top": tops if isinstance(tops, list) else [],
        "index": rows,
    }


def parse_pub_date(pub_desc: Any) -> Optional[str]:
    """'2025-01-29 09:00中国大陆上映' -> '2025-01-29'。"""
    if not isinstance(pub_desc, str):
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", pub_desc)
    return m.group(1) if m else None

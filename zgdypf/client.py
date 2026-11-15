#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 客户端：全局限速、分级重试、原始响应落盘。

重试语义（P0 裁决，替换掉前序脚本的错误实现）：
  200        -> 正常
  204        -> 正常但无数据，不重试
  400 / 404  -> 参数错误 / 端点错误，**不重试**，抛 HardError
  5xx / 超时 / 连接错误 -> 重试 3 次，退避 1s / 4s / 16s
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

BASE = "https://zgdypf.zgdypw.cn"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_NO_RETRY_CODES = {400, 404, 405, 410, 422}


class HardError(Exception):
    """不可重试错误（参数/端点错误）。"""


class RetryExhausted(Exception):
    """可重试错误用尽重试次数。"""


class BadPayload(Exception):
    """响应 200 但内容无法解析为 JSON（站点结构变更）。"""


@dataclass
class Response:
    path: str
    url: str
    status: Optional[int]
    body: bytes
    data: Any
    attempts: int
    elapsed_ms: int

    @property
    def is_empty(self) -> bool:
        return self.status == 204

    @property
    def ok(self) -> bool:
        return self.status in (200, 204)


@dataclass
class Stats:
    requests: int = 0
    retries: int = 0
    hard_errors: int = 0
    soft_errors: int = 0
    bytes: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class Client:
    """串行 + 全局限速的客户端。

    rate: 请求/秒的上限（P0 建议 1~2 req/s）。实际间隔 = 1/rate + jitter。
    """

    def __init__(
        self,
        base: str = BASE,
        rate: float = 2.0,
        jitter: tuple = (0.05, 0.20),
        timeout: float = 25.0,
        retries: int = 3,
        backoff: tuple = (1, 4, 16),
        raw_dir: Optional[str] = None,
        dry: bool = False,
    ) -> None:
        self.base = base.rstrip("/")
        self.rate = max(rate, 0.0)
        self.jitter = jitter
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = backoff
        self.raw_dir = raw_dir
        self.dry = dry
        self.headers = {
            "User-Agent": _USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base + "/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        self.stats = Stats()
        self._next_at = 0.0
        self.last_body: bytes = b""

    # ---------- 节流 ----------
    def _throttle(self) -> None:
        if self.dry or self.rate <= 0:
            return
        now = time.monotonic()
        if self._next_at > now:
            time.sleep(self._next_at - now)
        step = 1.0 / self.rate + random.uniform(*self.jitter)
        self._next_at = max(time.monotonic(), self._next_at) + step

    # ---------- 落盘 ----------
    def dump(self, rel: str, body: bytes, ext: str = ".json") -> None:
        if not self.raw_dir or self.dry or not body:
            return
        path = os.path.join(self.raw_dir, rel + ext)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)

    # ---------- 请求 ----------
    def fetch(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        raw_rel: Optional[str] = None,
        want_json: bool = True,
        raw_ext: str = ".json",
    ) -> Response:
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        last = "unknown"
        for attempt in range(1, self.retries + 1):
            self._throttle()
            t0 = time.time()
            self.stats.requests += 1
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    body = resp.read()
                elapsed = int((time.time() - t0) * 1000)
                self.stats.bytes += len(body)
                if raw_rel:
                    self.dump(raw_rel, body, raw_ext)
                if status == 204 or not body:
                    return Response(path, url, status, body, None, attempt, elapsed)
                if not want_json:
                    return Response(path, url, status, body, None, attempt, elapsed)
                try:
                    data = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BadPayload(f"{url}: {exc}") from exc
                return Response(path, url, status, body, data, attempt, elapsed)

            except urllib.error.HTTPError as exc:
                body = exc.read() or b""
                elapsed = int((time.time() - t0) * 1000)
                if raw_rel:
                    self.dump(raw_rel, body, raw_ext)
                if exc.code in _NO_RETRY_CODES:
                    self.stats.hard_errors += 1
                    raise HardError(f"HTTP {exc.code} {url} :: {body[:200]!r}") from exc
                last = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = repr(exc)
            except BadPayload:
                raise

            if attempt < self.retries:
                self.stats.retries += 1
                time.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])

        self.stats.soft_errors += 1
        raise RetryExhausted(f"{url} :: {last}")

    def get_json(self, path: str, params=None, *, raw_rel: Optional[str] = None) -> Any:
        return self.fetch(path, params or {}, raw_rel=raw_rel).data

    def get_html(self, path: str, *, raw_rel: Optional[str] = None) -> str:
        resp = self.fetch(path, {}, raw_rel=raw_rel, want_json=False, raw_ext=".html")
        self.last_body = resp.body
        return resp.body.decode("utf-8", "replace")

"""东方财富 push2 / push2his 请求节流、短缓存与失败率统计。"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional, TypeVar

import httpx

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_LAST_REQUEST_MONO = 0.0

MIN_INTERVAL_SEC = float(os.environ.get("EM_KLINE_MIN_INTERVAL_SEC", "0.35"))
MEM_TTL_SEC = float(os.environ.get("EM_KLINE_MEM_TTL_SEC", "300"))
MAX_RETRIES = int(os.environ.get("EM_KLINE_MAX_RETRIES", "4"))

_MEM_CACHE: dict[str, tuple[float, Any]] = {}
_FAIL_TIMES: list[float] = []
_SUCCESS_TIMES: list[float] = []
_STATS_WINDOW_SEC = 60.0
_LAST_STATS_LOG = 0.0

T = TypeVar("T")

RETRYABLE_EXC = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.NetworkError,
)


def wait_turn() -> None:
    """串行化东财 K 线类请求，保证相邻请求间隔 ≥ MIN_INTERVAL_SEC。"""
    global _LAST_REQUEST_MONO
    with _LOCK:
        now = time.monotonic()
        gap = MIN_INTERVAL_SEC - (now - _LAST_REQUEST_MONO)
        if gap > 0:
            time.sleep(gap)
        _LAST_REQUEST_MONO = time.monotonic()


def backoff_sleep(attempt: int) -> None:
    """指数退避：1s → 2s → 4s（上限 8s）。"""
    if attempt > 0:
        time.sleep(min(8.0, float(2 ** (attempt - 1))))


def mem_get(key: str) -> Optional[Any]:
    if MEM_TTL_SEC <= 0:
        return None
    now = time.monotonic()
    with _LOCK:
        hit = _MEM_CACHE.get(key)
        if not hit:
            return None
        exp, val = hit
        if now >= exp:
            _MEM_CACHE.pop(key, None)
            return None
        return val


def mem_set(key: str, value: Any) -> None:
    if MEM_TTL_SEC <= 0 or value is None:
        return
    with _LOCK:
        _MEM_CACHE[key] = (time.monotonic() + MEM_TTL_SEC, value)


def _prune_stats(now: float) -> None:
    cutoff = now - _STATS_WINDOW_SEC
    while _FAIL_TIMES and _FAIL_TIMES[0] < cutoff:
        _FAIL_TIMES.pop(0)
    while _SUCCESS_TIMES and _SUCCESS_TIMES[0] < cutoff:
        _SUCCESS_TIMES.pop(0)


def record_outcome(ok: bool) -> None:
    global _LAST_STATS_LOG
    now = time.monotonic()
    with _LOCK:
        _prune_stats(now)
        if ok:
            _SUCCESS_TIMES.append(now)
        else:
            _FAIL_TIMES.append(now)
        total = len(_FAIL_TIMES) + len(_SUCCESS_TIMES)
        if total < 5:
            return
        fail_rate = len(_FAIL_TIMES) / total
        if now - _LAST_STATS_LOG < _STATS_WINDOW_SEC:
            return
        if fail_rate >= 0.5:
            _LAST_STATS_LOG = now
            logger.warning(
                "东财 K 线近 %ds 失败率 %.0f%%（成功 %d / 失败 %d）；"
                "日 K 将自动改用新浪/腾讯，一般不影响看图",
                int(_STATS_WINDOW_SEC),
                fail_rate * 100,
                len(_SUCCESS_TIMES),
                len(_FAIL_TIMES),
            )


def request_json(
    *,
    client: httpx.Client,
    url: str,
    params: dict[str, str],
    label: str,
) -> Optional[dict]:
    """带节流 + 指数退避的 GET JSON。"""
    last_err: Exception | None = None
    for attempt in range(max(1, MAX_RETRIES)):
        wait_turn()
        try:
            resp = client.get(url, params=params)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            data = resp.json()
            record_outcome(True)
            return data if isinstance(data, dict) else None
        except httpx.HTTPStatusError as e:
            last_err = e
            record_outcome(False)
            if attempt + 1 < MAX_RETRIES and (
                e.response is not None
                and e.response.status_code in (429, 500, 502, 503, 504)
            ):
                backoff_sleep(attempt + 1)
                continue
            break
        except RETRYABLE_EXC as e:
            last_err = e
            record_outcome(False)
            if attempt + 1 < MAX_RETRIES:
                backoff_sleep(attempt + 1)
        except Exception as e:
            last_err = e
            record_outcome(False)
            break
    if last_err is not None:
        logger.warning("%s 失败（已重试 %d 次）: %s", label, MAX_RETRIES, last_err)
    return None

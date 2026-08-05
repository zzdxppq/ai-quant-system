"""东方财富妙想 Skills Hub API（`mkapi2.dfcfs.com`）。

是东财的官方 NLP 数据接口，境内外可达、稳定。免费 key 申请：
https://dl.dfcfs.com/m/itc4  ·  配置：环境变量 `MX_APIKEY`

提供两个端点（POST，header `apikey: <key>`）：
    /finskillshub/api/claw/query         body: {"toolQuery": "..."}
    /finskillshub/api/claw/news-search   body: {"query": "..."}

本项目主要使用 `query`，主要用法：
    - resolve_entity(name)            中文名（含错字）→ [{fullName, secuCode, entityType}]
    - fetch_snapshot(code_or_name)    个股画像：价/市值/PE/PB/行业/主营业务

行为约定：
    - 缺失 / 401 / 403 / 网络异常 → 静默返回空（不抛异常）
    - 30 分钟内存 TTL（复用 em_request_guard.mem_get/set）
    - 调用方不应把"妙想失败"当作"系统失败"
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

BASE = "https://mkapi2.dfcfs.com/finskillshub/api/claw"
QUERY_URL = f"{BASE}/query"
NEWS_URL = f"{BASE}/news-search"

_TIMEOUT = 15
_ATTEMPTS = 2
_TTL_SEC = 30 * 60  # 30 min

_MEM: dict[str, tuple[float, Any]] = {}
_MEM_LOCK = threading.Lock()
_LAST_STATUS: dict[str, str] = {"query": "disabled"}  # disabled / ok / error


def get_last_status() -> str:
    return _LAST_STATUS.get("query", "disabled")


def available() -> bool:
    return bool(os.getenv("MX_APIKEY", "").strip())


def _mem_get(key: str) -> Optional[Any]:
    now = time.monotonic()
    with _MEM_LOCK:
        hit = _MEM.get(key)
        if hit is None:
            return None
        ts, val = hit
        if now - ts > _TTL_SEC:
            return None
        return val


def _mem_set(key: str, val: Any) -> None:
    with _MEM_LOCK:
        _MEM[key] = (time.monotonic(), val)


def _post(url: str, body: dict, api_key: str) -> dict:
    last_err: Optional[str] = None
    headers = {"Content-Type": "application/json", "apikey": api_key}
    for i in range(_ATTEMPTS):
        try:
            with httpx.Client(timeout=_TIMEOUT, headers=headers, trust_env=False) as c:
                r = c.post(url, json=body)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (401, 403):
                    _LAST_STATUS["query"] = "error"
                    break
                time.sleep(1.0 * (i + 1))
                continue
            _LAST_STATUS["query"] = "ok"
            return r.json()
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
            time.sleep(1.0 * (i + 1))
    _LAST_STATUS["query"] = "error"
    logger.debug("[mx-api] %s failed: %s", url, last_err)
    return {"error": last_err or "unknown"}


def query(tool_query: str) -> dict:
    """自然语言查询。返回原始 JSON dict（带 `data`/`status` 嵌套）。失败/无 key 返回 `{}`。"""
    if not available():
        return {}
    cache_key = f"q::{tool_query[:120]}"
    cached = _mem_get(cache_key)
    if cached is not None:
        return cached
    api_key = os.getenv("MX_APIKEY", "").strip()
    res = _post(QUERY_URL, {"toolQuery": tool_query}, api_key)
    if not res.get("error") and res.get("status") in (0, None):
        _mem_set(cache_key, res)
    return res


def news_search(query_text: str) -> dict:
    """近期新闻搜索。返回原始 JSON（结构因 endpoint 不同而变）。失败/无 key 返回 `{}`。

    常见返回：data.data.searchDataResultDTO.newsList: [{title, date, ...}]
    """
    if not available():
        return {}
    cache_key = f"n::{query_text[:120]}"
    cached = _mem_get(cache_key)
    if cached is not None:
        return cached
    api_key = os.getenv("MX_APIKEY", "").strip()
    res = _post(NEWS_URL, {"query": query_text}, api_key)
    if not res.get("error") and res.get("status") in (0, None):
        _mem_set(cache_key, res)
    return res


# ─────────────────────────── 高层 helper ───────────────────────────

_NAME_PAREN_RE = re.compile(r"^(.+?)\s*[（(][^)）]+[)）]\s*$")


def resolve_entity(name: str) -> list[dict]:
    """中文名（含错字 / 简称）→ 候选股票列表。

    返回：[{"fullName", "secuCode", "entityType"}]，失败/无 key 返回 []
    """
    if not name or not available():
        return []
    res = query(f"{name} 股票代码 所属行业")
    if not res or res.get("error"):
        return []

    data = res.get("data") or {}
    inner = data.get("data") or {}
    sr = inner.get("searchDataResultDTO") or {}

    out: list[dict] = []
    seen: set[str] = set()

    for t in sr.get("entityTagDTOList") or []:
        if not isinstance(t, dict):
            continue
        full_name = (t.get("fullName") or t.get("shortName") or "").strip()
        code = (t.get("secuCode") or t.get("code") or "").strip()
        etype = t.get("entityTypeName") or t.get("entityType") or ""
        if full_name and code and code not in seen:
            seen.add(code)
            out.append({"fullName": full_name, "secuCode": code, "entityType": etype})

    for dto in sr.get("dataTableDTOList") or []:
        if not isinstance(dto, dict):
            continue
        code = (dto.get("code") or "").strip()
        entity_name = (dto.get("entityName") or "").strip()
        if not code or code in seen:
            continue
        m = _NAME_PAREN_RE.match(entity_name)
        clean_name = m.group(1).strip() if m else entity_name
        seen.add(code)
        out.append({
            "fullName": clean_name,
            "secuCode": code,
            "entityType": dto.get("dataType", "") or "股票",
        })

    return out


def fetch_snapshot(code_or_name: str) -> dict:
    """个股画像：最新价 / 市值 / PE / PB / 行业 / 主营业务。

    返回平铺 dict（键为中文 label，如 `最新价` / `总市值` / `PE(TTM)`）。
    失败 / 无 key / 妙想返回非结构化 → 返回 {}。
    """
    if not code_or_name or not available():
        return {}
    res = query(f"{code_or_name} 最新价 总市值 PE PB 所属行业 主营业务")
    if not res or res.get("error"):
        return {}

    data = res.get("data") or {}
    inner = data.get("data") or {}
    sr = inner.get("searchDataResultDTO") or {}
    dto_list = sr.get("dataTableDTOList") or []
    if not dto_list or not isinstance(dto_list[0], dict):
        return {}
    dto = dto_list[0]
    table = dto.get("table")
    if not isinstance(table, dict):
        return {}

    name_map = dto.get("nameMap") or {}
    if isinstance(name_map, list):
        name_map = {str(i): v for i, v in enumerate(name_map)}

    out: dict[str, Any] = {"_mx_entity": (dto.get("entityName") or "").strip()}
    for key, values in table.items():
        if key == "headName":
            continue
        label = name_map.get(key) or name_map.get(str(key)) or str(key)
        if isinstance(values, list) and values:
            # 最新一列
            out[str(label)] = values[-1]
        else:
            out[str(label)] = values
    return out


# ─────────────────────────── 自测 ───────────────────────────

if __name__ == "__main__":
    import json
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "北部湾港"
    print(f"available: {available()}")
    if not available():
        print("Set MX_APIKEY to test.")
        sys.exit(0)
    print(f"\n── resolve_entity('{q}') ──")
    print(json.dumps(resolve_entity(q), ensure_ascii=False, indent=2))
    print(f"\n── fetch_snapshot('{q}') ──")
    print(json.dumps(fetch_snapshot(q), ensure_ascii=False, indent=2))

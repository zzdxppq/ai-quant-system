"""日 K 本地缓存：仅写入 quant 库 `daily_kline` / `kline_series_meta`（`quant.duckdb`）。

供 sina_kline_api.fetch_kline 在命中 TTL 时跳过网络，失败时回读过期缓存。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import DATA_DIR, now_cn

# 可通过环境变量覆盖：KLINE_CACHE_ENABLED=0 关闭；KLINE_CACHE_TTL_SECONDS=7200
KLINE_CACHE_ENABLED = os.environ.get("KLINE_CACHE_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
KLINE_CACHE_TTL_SECONDS = int(os.environ.get("KLINE_CACHE_TTL_SECONDS", str(4 * 3600)))


def _cache_root() -> Path:
    p = DATA_DIR / "kline_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def normalize_code6(code: str) -> str:
    d = re.sub(r"\D", "", str(code or ""))
    if len(d) >= 6:
        return d[-6:]
    return ""


def cache_path(code: str, scale: str, datalen: int) -> Path:
    """逻辑路径（与旧 JSON 文件名一致）；数据实际仅存 quant 库。"""
    c = normalize_code6(code)
    return _cache_root() / f"{c}_{str(scale)}_{int(datalen)}.json"


def try_read_cache(
    code: str,
    scale: str,
    datalen: int,
    *,
    allow_stale: bool = False,
) -> Optional[pd.DataFrame]:
    """读取缓存 DataFrame；未命中或过期返回 None（allow_stale=True 时过期仍返回）。"""
    if not KLINE_CACHE_ENABLED:
        return None
    c = normalize_code6(code)
    if len(c) != 6 or not c.isdigit():
        return None
    from src.data.structured_store import try_read_kline_dataframe

    return try_read_kline_dataframe(
        c, scale, int(datalen), allow_stale=allow_stale, ttl_seconds=KLINE_CACHE_TTL_SECONDS
    )


def write_cache(code: str, scale: str, datalen: int, df: pd.DataFrame) -> None:
    """将成功拉取的 K 线写入 quant 库。"""
    if not KLINE_CACHE_ENABLED or df is None or df.empty:
        return
    c = normalize_code6(code)
    ts = now_cn().isoformat()
    if len(c) != 6 or not c.isdigit():
        return
    from src.data.structured_store import replace_kline_series

    replace_kline_series(c, str(scale), int(datalen), df, cached_at_iso=ts)

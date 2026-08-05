"""全市场股票代码/名称搜索（看板「我的持仓」用）

真源优先 **DuckDB `stock_basic` 全表**（≥2500 条即命中）；表不足时再拉新浪/本地 JSON 并回写表。
内存仅做短 TTL 减负，刷新全量请用「拉取全市场」或等服务端后台同步。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import pandas as pd

_CACHE: tuple[float, pd.DataFrame | None] = (0.0, None)
_MEM_TTL_SEC = 180


def _load_universe_from_disk_cache() -> pd.DataFrame:
    try:
        from src.config import DATA_DIR

        p = DATA_DIR / "_stock_list_cache.json"
        if not p.is_file():
            return pd.DataFrame(columns=["code", "name"])
        raw = p.read_bytes()
        for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk"):
            try:
                arr = json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(arr, list):
                continue
            rows: list[tuple[str, str]] = []
            for it in arr:
                if not isinstance(it, dict):
                    continue
                c = re.sub(r"\D", "", str(it.get("code") or ""))[-6:].zfill(6)
                nm = str(it.get("name") or "").strip()
                if len(c) == 6 and c.isdigit() and nm:
                    rows.append((c, nm))
            if not rows:
                return pd.DataFrame(columns=["code", "name"])
            df = pd.DataFrame(rows, columns=["code", "name"]).drop_duplicates(subset=["code"])
            return df.reset_index(drop=True)
    except Exception as e:
        print(f"[stock-search] 本地列表缓存加载失败: {e}")
    return pd.DataFrame(columns=["code", "name"])


def _load_universe_fresh() -> pd.DataFrame:
    try:
        from src.data.sina_spot_api import fetch_a_share_list_sina

        spot = fetch_a_share_list_sina()
        if spot is None or spot.empty:
            return _load_universe_from_disk_cache()
        df = spot[["code", "name"]].drop_duplicates(subset=["code"]).copy()
        df["code"] = (
            df["code"].astype(str).str.replace(r"\D", "", regex=True).str[-6:]
        )
        df["name"] = df["name"].astype(str)
        out = df[df["code"].str.len() == 6].reset_index(drop=True)
        if out.empty:
            return _load_universe_from_disk_cache()
        return out
    except Exception as e:
        print(f"[stock-search] 全市场表加载失败: {e}")
        return _load_universe_from_disk_cache()


def get_search_universe() -> pd.DataFrame:
    global _CACHE
    ts, df = _CACHE
    now = time.time()
    if df is not None and not df.empty and (now - ts) < _MEM_TTL_SEC:
        return df
    try:
        from src.data.structured_store import load_stock_basic_full_df

        dbdf = load_stock_basic_full_df(min_rows=2500)
        if dbdf is not None and not dbdf.empty:
            _CACHE = (now, dbdf)
            return dbdf
    except Exception as e:
        print(f"[stock-search] 读 stock_basic 全表失败: {e}")
    df = _load_universe_fresh()
    if df is not None and not df.empty:
        try:
            from src.data.structured_store import replace_stock_basic

            n = replace_stock_basic(df)
            if n:
                print(f"[stock-search] 已写入 stock_basic {n} 条")
        except Exception as e:
            print(f"[stock-search] 写入 stock_basic 失败: {e}")
    _CACHE = (now, df)
    return df


def refresh_universe_table() -> dict[str, Any]:
    """同步拉取全市场列表并写入 stock_basic（阻塞，供 API 线程池调用）。"""
    df = _load_universe_fresh()
    n = 0
    if df is not None and not df.empty:
        from src.data.structured_store import replace_stock_basic

        n = replace_stock_basic(df)
    global _CACHE
    _CACHE = (0.0, None)
    return {"rows": len(df) if df is not None else 0, "inserted": n}


def search_stocks(query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if len(q) < 1:
        return []
    limit = max(1, min(50, int(limit)))
    df = get_search_universe()
    if df is None or df.empty:
        return []

    code_s = df["code"].astype(str)
    name_s = df["name"].astype(str)
    ql = q.lower()

    if re.fullmatch(r"\d{1,6}", q):
        sub = _match_numeric_code(df, code_s, q)
    else:
        try:
            pat = re.escape(ql)
        except re.error:
            pat = re.escape(q)
        nm = name_s.str.lower().str.contains(pat, na=False)
        cd = code_s.str.contains(q, case=False, na=False)
        sub = df[nm | cd]

    out = sub.head(limit)
    return [{"code": str(r["code"]), "name": str(r["name"])} for _, r in out.iterrows()]


def _match_numeric_code(df: pd.DataFrame, code_s: pd.Series, q: str) -> pd.DataFrame:
    """6 位全码精确匹配；否则左前缀；再否则右后缀（省略前导 0，如 2918→002918）。"""
    if len(q) == 6:
        m = code_s == q
        if m.any():
            return df[m]
        return df[code_s.str.startswith(q)]

    padded = q.zfill(6)
    if len(padded) == 6:
        m = code_s == padded
        if m.any():
            return df[m]

    pre = df[code_s.str.startswith(q)]
    if not pre.empty:
        return pre

    suf = df[code_s.str.endswith(q)]
    if not suf.empty:
        return suf

    return df[code_s.str.contains(q, case=False, na=False)]


def warm_daily_klines(codes: list[str], *, datalen: int = 500, force_network: bool = False) -> None:
    """阻塞拉取日 K（供盘后 15:30 预热 / BackgroundTasks），写入 DuckDB 缓存。"""
    from src.data.sina_kline_api import SCALE_DAILY, fetch_daily_kline_robust, fetch_kline

    seen: set[str] = set()
    for raw in codes:
        c = re.sub(r"\D", "", str(raw or ""))
        if len(c) >= 6:
            c = c[-6:]
        else:
            continue
        if len(c) != 6 or c in seen:
            continue
        seen.add(c)
        try:
            if force_network:
                df = fetch_daily_kline_robust(
                    c, min_bars=35, datalen=int(datalen), skip_cache_read=True
                )
            else:
                df = fetch_kline(c, SCALE_DAILY, datalen=int(datalen), skip_cache_read=False)
            n = len(df) if df is not None else 0
            print(f"[warm-kline] {c} 日K条数={n}")
        except Exception as e:
            print(f"[warm-kline] {c} 失败: {e}")

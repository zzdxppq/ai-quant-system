"""选股结果"昨日量能"富化 —— 注入 prev_day_turnover / prev_day_yizi /
prev_amount_ratio（昨日成交额 ÷ 前日成交额），用于 2进3 缩量换手板过滤。

数据源：本地 K 线缓存 → 东财日 K → 新浪回退。
仅在 scheduler 写盘前 in-place 注入；compute_per_stock_decision 读取这些字段。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed


def _fetch_recent_kline(code: str, limit: int = 6, *, cache_only: bool = False):
    """拉最近 limit 根日K（含今日运行bar，若交易中）

    优先本地缓存，再东财（含 turnover / amount），失败时回退新浪。
    cache_only=True 时仅读本地缓存，不打外部接口（历史离线重算用）。
    """
    import time

    # 1) 本地日 K 缓存（避免 9:27 对每只 hit 打东财）
    try:
        from src.data.kline_file_cache import try_read_cache
        from src.data.sina_kline_api import SCALE_DAILY

        cached = try_read_cache(code, SCALE_DAILY, max(limit, 10), allow_stale=True)
        if cached is not None and not cached.empty and len(cached) >= 3:
            return cached.tail(limit).copy()
    except Exception:
        pass

    if cache_only:
        return None

    # 2) 东财 with 2 retries（缩短 sleep）
    try:
        from src.data.eastmoney_api import fetch_kline as em_fetch
        for attempt in range(2):
            df = em_fetch(code, klt=101, fqt=1, limit=limit)
            if df is not None and not df.empty:
                return df
            if attempt == 0:
                time.sleep(0.15)
    except Exception:
        pass

    # 3) 回退新浪
    try:
        from src.data.sina_kline_api import fetch_kline as sina_fetch
        df = sina_fetch(code, datalen=limit)
        if df is not None and not df.empty:
            df = df.copy()
            df["turnover"] = None
            df["change_pct"] = ((df["close"] - df["open"]) / df["open"] * 100).round(2)
            if "amount" not in df.columns:
                df["amount"] = df["volume"].astype(float) * df["close"].astype(float) * 100.0
            return df
    except Exception:
        pass
    return None


def _is_yizi_board(yest, prev=None) -> bool:
    """一字板判定：四值近等 + 相对前日收盘涨幅≥9.5%"""
    try:
        op, hi, lo, cl = float(yest["open"]), float(yest["high"]), float(yest["low"]), float(yest["close"])
    except (TypeError, ValueError, KeyError):
        return False
    if hi <= 0:
        return False
    chg = None
    try:
        chg = float(yest.get("change_pct"))
    except (TypeError, ValueError, KeyError):
        chg = None
    if (chg is None or chg == 0) and prev is not None:
        try:
            pc = float(prev["close"])
            if pc > 0:
                chg = (cl / pc - 1) * 100
        except (TypeError, ValueError, KeyError):
            pass
    if chg is None or chg < 9.5:
        return False
    if op == hi == lo == cl:
        return True
    if (hi - lo) / hi < 0.003:
        return True
    return False


def _enrich_one_hit(h: dict, raw_date: str, *, cache_only: bool = False) -> None:
    """就地写入单只 hit 的昨日量能字段。"""
    code = str(h.get("code", "")).zfill(6)
    if not code:
        return
    df = _fetch_recent_kline(code, limit=6, cache_only=cache_only)
    if df is None or len(df) < 3:
        h["prev_day_turnover"] = None
        h["prev_amount_ratio"] = None
        h["prev_day_yizi"] = None
        return
    df = df.copy()
    df["_d"] = df["date"].astype(str).str[:10]
    df_prior = df[df["_d"] < raw_date]
    if len(df_prior) < 2:
        h["prev_day_turnover"] = None
        h["prev_amount_ratio"] = None
        h["prev_day_yizi"] = None
        return
    yesterday = df_prior.iloc[-1]
    prev = df_prior.iloc[-2]

    try:
        prev_turnover = float(yesterday["turnover"])
        prev_turnover = round(prev_turnover, 2) if prev_turnover > 0 else None
    except (TypeError, ValueError, KeyError):
        prev_turnover = None
    if prev_turnover is None:
        try:
            mc_yi = float(h.get("market_cap"))
            v_y_close = float(yesterday["close"])
            v_y_vol = float(yesterday["volume"])
            if mc_yi and mc_yi > 0 and v_y_close > 0:
                prev_turnover = round(v_y_vol * v_y_close / (mc_yi * 1e8) * 100, 2)
        except (TypeError, ValueError, KeyError):
            pass
    try:
        amt_y = float(yesterday["amount"])
        amt_p = float(prev["amount"])
        amount_ratio = round(amt_y / amt_p, 3) if amt_p > 0 else None
    except (TypeError, ValueError, KeyError):
        amount_ratio = None

    h["prev_day_turnover"] = prev_turnover
    h["prev_amount_ratio"] = amount_ratio
    h["prev_day_yizi"] = _is_yizi_board(yesterday, prev)


def enrich_hits_with_prev_day_kline(
    hits_data: dict, *, max_workers: int = 6, cache_only: bool = False,
) -> None:
    """为 hits_data['hits'] 注入：
       prev_day_turnover (%), prev_amount_ratio (昨额/前额), prev_day_yizi (bool)

    "昨日" 锚定到 **screener 当日的前一交易日**。命中多只时线程池并行拉取。
    cache_only=True：仅本地 K 线缓存，不访问外网。
    """
    from src.config import now_cn

    raw_date = str(hits_data.get("date") or now_cn().strftime("%Y-%m-%d"))[:10]
    hits = hits_data.get("hits") or []
    if not hits:
        return
    if len(hits) == 1:
        _enrich_one_hit(hits[0], raw_date, cache_only=cache_only)
        return

    workers = max(1, min(int(max_workers or 6), len(hits), 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [
            pool.submit(_enrich_one_hit, h, raw_date, cache_only=cache_only)
            for h in hits
        ]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass

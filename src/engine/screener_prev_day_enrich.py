"""选股结果"昨日量能"富化 —— 注入 prev_day_turnover / prev_day_yizi /
prev_volume_ratio（昨日成交量 ÷ 前日成交量），用于 2进3 缩量换手板过滤。

数据源：东财日 K（含 turnover / volume / open / high / low / close / change_pct）。
仅在 scheduler 写盘前 in-place 注入；compute_per_stock_decision 读取这些字段。
"""
from __future__ import annotations
from typing import Optional


def _fetch_recent_kline(code: str, limit: int = 4):
    """拉最近 limit 根日K（含今日运行bar，若交易中）

    优先东财（含 turnover），失败时回退到新浪（仅 volume，turnover 留空）。
    东财同 SSL/超时偶发，做 2 次重试。
    """
    import time
    # 东财 with 2 retries
    try:
        from src.data.eastmoney_api import fetch_kline as em_fetch
        for _ in range(2):
            df = em_fetch(code, klt=101, fqt=1, limit=limit)
            if df is not None and not df.empty:
                return df
            time.sleep(0.3)
    except Exception:
        pass
    # 回退新浪：缺 turnover 列，下游需容错 None
    try:
        from src.data.sina_kline_api import fetch_kline as sina_fetch
        df = sina_fetch(code, datalen=limit)
        if df is not None and not df.empty:
            df = df.copy()
            df["turnover"] = None  # sina 不提供
            df["change_pct"] = ((df["close"] - df["open"]) / df["open"] * 100).round(2)
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
    # 涨幅校验：优先 change_pct，无则用 prev.close 推
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
    # 严格一字：四值完全相等
    if op == hi == lo == cl:
        return True
    # 准一字：振幅 <0.3%
    if (hi - lo) / hi < 0.003:
        return True
    return False


def enrich_hits_with_prev_day_kline(hits_data: dict) -> None:
    """为 hits_data['hits'] 注入：
       prev_day_turnover (%), prev_volume_ratio (vol/prev_vol), prev_day_yizi (bool)

    "昨日" 锚定到 **screener 当日的前一交易日**：
      · 9:27 实时跑：screener_date = 今天 → 昨日 = 最近已闭合 bar
      · 离线重算：screener_date = hits_data['date'] → 取严格 < 此日的最后一根
    """
    from src.config import now_cn
    # screener 锚定日：优先 hits_data['date']，无则取今日
    raw_date = str(hits_data.get("date") or now_cn().strftime("%Y-%m-%d"))[:10]
    hits = hits_data.get("hits") or []
    for h in hits:
        code = str(h.get("code", "")).zfill(6)
        if not code:
            continue
        df = _fetch_recent_kline(code, limit=6)
        if df is None or len(df) < 3:
            h["prev_day_turnover"] = None
            h["prev_volume_ratio"] = None
            h["prev_day_yizi"] = None
            continue
        # 仅保留 < screener_date 的 bar，最末两根即昨日/前日
        df = df.copy()
        df["_d"] = df["date"].astype(str).str[:10]
        df_prior = df[df["_d"] < raw_date]
        if len(df_prior) < 2:
            h["prev_day_turnover"] = None
            h["prev_volume_ratio"] = None
            h["prev_day_yizi"] = None
            continue
        yesterday = df_prior.iloc[-1]
        prev = df_prior.iloc[-2]

        try:
            prev_turnover = float(yesterday["turnover"])
            prev_turnover = round(prev_turnover, 2) if prev_turnover > 0 else None
        except (TypeError, ValueError, KeyError):
            prev_turnover = None
        # 东财 turnover 缺失 → 按"成交额 ÷ 流通市值"近似估算
        # 公式：换手率(%) ≈ vol(股) * close(元) / (流通市值(亿) * 1e8) * 100
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
            v_y = float(yesterday["volume"])
            v_p = float(prev["volume"])
            ratio = round(v_y / v_p, 3) if v_p > 0 else None
        except (TypeError, ValueError, KeyError):
            ratio = None

        h["prev_day_turnover"] = prev_turnover
        h["prev_volume_ratio"] = ratio
        h["prev_day_yizi"] = _is_yizi_board(yesterday, prev)

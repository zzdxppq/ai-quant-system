"""日 K 多源合并与陈旧判定"""
import pandas as pd

from src.data.sina_kline_api import (
    _is_daily_kline_stale,
    _merge_daily_kline_candidates,
    prev_close_from_daily_df,
)


def test_merge_daily_prefers_em_on_same_date():
    sina = pd.DataFrame([
        {"date": "2026-05-13", "open": 10, "high": 11, "low": 9.5, "close": 10.5, "volume": 1000},
        {"date": "2026-05-14", "open": 10.5, "high": 11.2, "low": 10, "close": 11, "volume": 800},
    ])
    em = pd.DataFrame([
        {"date": "2026-05-13", "open": 10, "high": 11, "low": 9.5, "close": 10.8, "volume": 1200},
        {"date": "2026-05-15", "open": 11, "high": 12, "low": 10.8, "close": 11.5, "volume": 900},
    ])
    merged = _merge_daily_kline_candidates([sina, em], priorities=[1, 3])
    assert len(merged) == 3
    row13 = merged[merged["date"] == "2026-05-13"].iloc[0]
    assert float(row13["close"]) == 10.8
    assert merged["date"].tolist() == ["2026-05-13", "2026-05-14", "2026-05-15"]


def test_merge_dedupes_duplicate_dates():
    a = pd.DataFrame([
        {"date": "2026-05-10", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 10},
        {"date": "2026-05-10", "open": 1, "high": 2, "low": 1, "close": 1.6, "volume": 50},
    ])
    out = _merge_daily_kline_candidates([a])
    assert len(out) == 1
    assert float(out.iloc[0]["close"]) == 1.6


def test_prev_close_uses_last_closed_session_not_iloc_minus_two(monkeypatch):
    """库内最新 K 为上一交易日时，昨收应为该日收盘，而非 iloc[-2]。"""
    import src.data.sina_kline_api as sk

    df = pd.DataFrame([
        {"date": "2026-05-15", "open": 14.88, "high": 15.61, "low": 12.77, "close": 13.10, "volume": 1},
        {"date": "2026-05-18", "open": 12.60, "high": 13.86, "low": 12.35, "close": 13.49, "volume": 1},
    ])
    monkeypatch.setattr(sk, "_expected_last_daily_bar_date", lambda: pd.Timestamp("2026-05-18"))
    assert prev_close_from_daily_df(df) == 13.49


def test_stale_when_behind_expected_trading_day():
    df = pd.DataFrame([
        {"date": "2026-05-10", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 10},
    ])
    assert _is_daily_kline_stale(df) is True

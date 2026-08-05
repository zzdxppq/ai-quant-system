"""市场广度涨跌家数统计"""
import pandas as pd

from src.engine.market_insight import (
    _breadth_looks_stale,
    _count_advance_decline_from_spot,
)


def test_breadth_looks_stale():
    assert _breadth_looks_stale(None) is True
    assert _breadth_looks_stale({"sh_close": 4100, "advance": 0, "decline": 0}) is True
    assert _breadth_looks_stale({"sh_close": 4100, "advance": 100, "decline": 200}) is False


def test_count_advance_decline_from_change_pct():
    df = pd.DataFrame({
        "code": ["600000", "600001", "600002", "600003"],
        "name": ["A", "B", "ST股", "C"],
        "change_pct": [1.0, -2.0, 5.0, 0.0],
        "close": [10, 10, 10, 10],
        "pre_close": [10, 10, 10, 10],
    })
    adv, dec, flat = _count_advance_decline_from_spot(df)
    assert adv == 1
    assert dec == 1
    assert flat == 1

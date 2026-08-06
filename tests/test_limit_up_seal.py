"""涨停价封板判定：差一档（如 9.99%）不得计入连板。"""
import pandas as pd

from src.data.limit_up_seal import (
    is_limit_up_sealed,
    limit_up_price,
)
from src.engine.screener import _detect_continuous_limit_up, _filter_sealed_limit_up_df


def test_limit_up_price_main_board():
    # 昨收 10.00 → 涨停价 11.00
    assert limit_up_price("001229", 10.0) == 11.0


def test_near_limit_9_99_not_sealed():
    """收盘差一档（低于涨停价 0.01）→ 非封板；真封板涨幅也可能显示约 9.99%。"""
    pre = 10.01
    # 涨停价 = round(10.01 * 1.1, 2) = 11.01
    assert limit_up_price("001229", pre) == 11.01
    assert not is_limit_up_sealed("001229", pre, 11.00)  # 差一档
    assert is_limit_up_sealed("001229", pre, 11.01)  # 封板（涨幅约 9.99%）
    assert round((11.01 / pre - 1) * 100, 2) == 9.99


def test_true_seal_can_print_as_9_99():
    """真封板因四舍五入涨幅也可能显示约 9.98/9.99%，须按价判定。"""
    pre = 12.12
    lim = limit_up_price("600000", pre)  # round(12.12*1.1,2)=13.33
    assert lim == 13.33
    pct = round((lim / pre - 1) * 100, 2)
    assert pct in (9.98, 9.99, 10.0) or abs(pct - 10.0) < 0.05
    assert is_limit_up_sealed("600000", pre, lim)


def test_filter_drops_unsealed_priced_rows():
    df = pd.DataFrame(
        [
            {"code": "001229", "name": "x", "pre_close": 10.01, "close": 11.0, "change_pct": 9.89},
            {"code": "000001", "name": "y", "pre_close": 10.0, "close": 11.0, "change_pct": 10.0},
        ]
    )
    out = _filter_sealed_limit_up_df(df)
    assert list(out["code"]) == ["000001"]


def test_detect_continuous_excludes_yesterday_near_limit(monkeypatch):
    """昨日差一档未封板不应进入连板 map（有价时）。"""
    from datetime import datetime

    from src.config import TZ_CN

    monkeypatch.setattr(
        "src.engine.screener.now_cn",
        lambda: datetime(2026, 8, 6, 9, 30, tzinfo=TZ_CN),
    )
    monkeypatch.setattr("src.config.is_trading_day", lambda _dt=None: True)

    hist = {
        "20260806": pd.DataFrame([{"code": "999999", "name": "today"}]),
        "20260805": pd.DataFrame(
            [
                {
                    "code": "001229",
                    "name": "差一档",
                    "pre_close": 10.01,
                    "close": 11.0,
                    "change_pct": 9.89,
                }
            ]
        ),
        "20260804": pd.DataFrame(
            [
                {
                    "code": "001229",
                    "name": "昨昨封板",
                    "pre_close": 9.1,
                    "close": 10.01,
                    "change_pct": 10.0,
                }
            ]
        ),
    }
    continuous = _detect_continuous_limit_up(hist)
    assert "001229" not in continuous

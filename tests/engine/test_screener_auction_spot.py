"""9:26：全市场竞价快照一次；选股候选仍来自昨日涨停池。"""
from unittest.mock import patch

import pandas as pd
import pytest

from src.engine import screener


def test_collect_auction_quote_codes_from_limit_up_history():
    hist = {
        "20260515": pd.DataFrame({"code": ["600001", "000001"]}),
        "20260514": pd.DataFrame({"code": ["600001", "600002"]}),
    }
    with patch("src.config.is_trading_day", return_value=True):
        with patch("src.engine.screener.now_cn") as mock_now:
            from datetime import datetime, timezone

            mock_now.return_value = datetime(2026, 5, 16, 9, 26, tzinfo=timezone.utc)
            q = screener.qualified_codes_from_history(hist, min_continuous=2)
    assert q.get("600001") == 2
    assert "000001" not in q

    with patch("src.config.is_trading_day", return_value=True):
        with patch("src.engine.screener.now_cn") as mock_now:
            from datetime import datetime, timezone

            mock_now.return_value = datetime(2026, 5, 16, 9, 26, tzinfo=timezone.utc)
            codes = screener.collect_auction_quote_codes(
                hist, ranking_file=None, include_anchor_pools=False,
            )
    assert "600001" in codes
    assert "000001" in codes


def test_fetch_auction_spot_full_tencent_universe_fallback(monkeypatch):
    hist = {
        "20260522": pd.DataFrame({"code": ["600001", "600002"]}),
        "20260521": pd.DataFrame({"code": ["600001", "600002"]}),
    }
    tencent_df = pd.DataFrame([
        {
            "code": "600001",
            "name": "A",
            "close": 10.0,
            "pre_close": 9.5,
            "open": 10.2,
            "change_pct": 5.0,
            "high": 10.5,
            "low": 9.8,
            "volume": 1e6,
            "amount": 1e7,
            "volume_ratio": 1.2,
            "market_cap_yi": 50.0,
            "turnover": 2.0,
            "pe": 10.0,
            "pb": 1.0,
        },
    ])

    monkeypatch.setattr(
        "src.data.sina_spot_api.fetch_a_share_list_sina",
        lambda: pd.DataFrame(),
    )
    monkeypatch.setattr(
        "src.data.ranking_scanner.fetch_full_market_spot",
        lambda: pd.DataFrame(),
    )
    monkeypatch.setattr(
        "src.engine.screener.collect_auction_quote_codes",
        lambda *a, **k: ["600001", "600002"],
    )
    monkeypatch.setattr(
        "src.data.tencent_api.fetch_stock_details",
        lambda codes: tencent_df,
    )

    out = screener.fetch_auction_spot_full(limit_up_history=hist)
    assert len(out) == 1
    assert out.attrs.get("source") == "tencent_universe"


def test_detect_continuous_limit_up_prefers_lbc_from_yesterday_pool():
    """池内 lbc=3 时，不应因更早某日 cache 缺码而递推成 2 板。"""
    from datetime import datetime

    from src.config import TZ_CN

    hist = {
        "20260522": pd.DataFrame([
            {"code": "002442", "name": "龙星科技", "continuous_limit_up": 3},
        ]),
        "20260521": pd.DataFrame([{"code": "002442", "name": "龙星科技"}]),
        # 20260520 缺 002442 → 纯递推仅得 2
    }
    with patch("src.config.is_trading_day", return_value=True):
        with patch("src.engine.screener.now_cn") as mock_now:
            mock_now.return_value = datetime(2026, 5, 25, 9, 27, tzinfo=TZ_CN)
            m = screener._detect_continuous_limit_up(hist)
    assert m.get("002442") == 3


def test_fetch_screener_data_uses_one_full_auction_snapshot():
    from src import scheduler

    fake_hist = {"20260515": pd.DataFrame({"code": ["600156"]})}
    full_df = pd.DataFrame(
        [{"code": f"{i:06d}", "open": 10.0, "pre_close": 9.0} for i in range(1200)]
    )

    with patch("src.data.fetcher.fetch_limit_up_history", return_value=fake_hist):
        with patch(
            "src.engine.screener.fetch_auction_spot_full", return_value=full_df,
        ) as mock_full:
            with patch("src.data.fetcher.fetch_realtime_spot") as mock_em_top100:
                spot, hist = scheduler._fetch_screener_data()
    mock_em_top100.assert_not_called()
    mock_full.assert_called_once()
    assert len(spot) == 1200
    assert hist is fake_hist


def test_run_screener_filters_full_spot_by_qualified_only():
    hist = {
        "20260515": pd.DataFrame({"code": ["600001", "600002"]}),
        "20260514": pd.DataFrame({"code": ["600001", "600002"]}),
    }
    # 竞价涨幅 5%（落在默认 4%~7.5% 窗内）
    full_spot = pd.DataFrame([
        {"code": "600001", "name": "A", "open": 10.5, "pre_close": 10.0,
         "amount": 5e7, "volume": 1e6, "turnover": 0, "market_cap": 5e9, "volume_ratio": 0},
        {"code": "999999", "name": "X", "open": 10.5, "pre_close": 10.0,
         "amount": 5e7, "volume": 1e6, "turnover": 0, "market_cap": 5e9, "volume_ratio": 0},
    ])
    with patch("src.config.is_trading_day", return_value=True):
        with patch("src.engine.screener.now_cn") as mock_now:
            from datetime import datetime, timezone

            mock_now.return_value = datetime(2026, 5, 16, 9, 26, tzinfo=timezone.utc)
            with patch("src.engine.screener._get_avg_volume_5d", return_value=None):
                hits = screener.run_screener(full_spot, hist, cycle_codes=[])
    codes = {h.code for h in hits}
    assert "600001" in codes or "600002" in codes
    assert "999999" not in codes

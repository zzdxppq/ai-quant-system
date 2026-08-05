"""09:15 前看板竞价跌停应展示上一交易日落盘。"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.engine.dashboard_decision import resolve_auction_market_for_dashboard

CN = timezone.utc


def test_resolve_auction_market_uses_prev_day_before_915(monkeypatch):
    n = datetime(2026, 5, 20, 9, 0, tzinfo=CN)
    monkeypatch.setattr("src.market_schedule.now_cn", lambda: n)
    monkeypatch.setattr("src.market_schedule.is_trading_day", lambda _d: True)
    monkeypatch.setattr(
        "src.data.analytics_store.load_prev_trading_day_sentiment_market",
        lambda: (
            {
                "limit_down": 12,
                "drop_over_9pct": 18,
                "limit_down_list": [{"code": "600000", "name": "X", "auction_pct": -10}],
            },
            "2026-05-19",
        ),
    )
    mkt = resolve_auction_market_for_dashboard({"limit_down": 0})
    assert mkt.get("auction_market_as_of") == "2026-05-19"
    assert mkt.get("limit_down") == 12


def test_open_pct_from_auction_tick(monkeypatch):
    from src.engine.daily_review import open_pct_for_session

    monkeypatch.setattr(
        "src.data.structured_store.load_auction_session",
        lambda code, _d: {"open_tick": {"price": 55.84}} if code == "001211" else None,
    )
    monkeypatch.setattr(
        "src.engine.daily_review._pre_close_before_session",
        lambda _c, _s: 50.0,
    )
    monkeypatch.setattr(
        "src.engine.daily_review._build_spot_map_for_codes",
        lambda _codes: {},
    )
    pct = open_pct_for_session("001211", "20260513")
    assert pct == pytest.approx(11.68, abs=0.1)

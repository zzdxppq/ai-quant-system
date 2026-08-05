"""看板 1进2 取自上一交易日复盘 scorecard，不用当日 latest_review。"""
import pytest

from src.engine import dashboard_decision as dd


def test_b1_uses_prev_trading_day_review_not_today_latest(monkeypatch):
    monkeypatch.setattr(dd, "_prev_trading_day_iso", lambda: "2026-05-14")
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_prev_trading_day_review_document",
        lambda: {
            "date": "2026-05-14",
            "scorecard": {"indicators": [{"label": "1进2成功率", "raw": 16.2}]},
        },
    )
    assert dd._b1_rate_from_review() == pytest.approx(16.2)

"""复盘/看板 1进2 日期口径：盘前用上一交易日快照，不用当日 latest_review。"""
from datetime import datetime, timezone

import pytest

from src.engine import dashboard_decision as dd
from src.engine.screener_market_env import resolve_review_document_for_api

CN = timezone.utc


def test_b1_not_from_today_latest_review(monkeypatch):
    monkeypatch.setattr(dd, "_prev_trading_day_iso", lambda: "2026-05-18")
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_prev_trading_day_review_document",
        lambda: {
            "date": "2026-05-18",
            "scorecard": {"indicators": [{"label": "1进2成功率", "raw": 16.2}]},
        },
    )
    assert dd._b1_rate_from_review() == pytest.approx(16.2)


def test_resolve_review_before_15_uses_prev_day(monkeypatch):
    n = datetime(2026, 5, 19, 10, 0, tzinfo=CN)
    monkeypatch.setattr(
        "src.engine.screener_market_env.now_cn", lambda: n,
    )
    monkeypatch.setattr(
        "src.engine.screener_market_env._prev_trading_day_iso",
        lambda: "2026-05-18",
    )
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_latest_review_document",
        lambda: {
            "date": "2026-05-19",
            "scorecard": {"indicators": [{"label": "1进2成功率", "raw": 26.2}]},
        },
    )
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_prev_trading_day_review_document",
        lambda: {
            "date": "2026-05-18",
            "scorecard": {"indicators": [{"label": "1进2成功率", "raw": 16.2}]},
        },
    )
    doc = resolve_review_document_for_api(n)
    assert doc.get("date") == "2026-05-18"
    ind = next(
        i for i in (doc.get("scorecard") or {}).get("indicators") or []
        if i.get("label") == "1进2成功率"
    )
    assert ind.get("raw") == pytest.approx(16.2)

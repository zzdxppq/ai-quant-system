"""月度复盘：涨停转化率与 monthly-review API 口径。"""
from src.engine.screener_history import (
    _signal_to_limit_pct,
    build_monthly_review,
    calc_win_stats,
)


def test_signal_to_limit_counts_all_signals_not_only_judged():
    """涨停记录 is_win=None 时仍应计入转化率分子与分母。"""
    records = [
        {
            "date": "2026-05-10",
            "code": "000001",
            "is_win": None,
            "is_limit_up": True,
            "day_change": 10.0,
        },
        {
            "date": "2026-05-12",
            "code": "000002",
            "is_win": False,
            "is_limit_up": False,
            "day_change": 3.0,
        },
    ]
    assert _signal_to_limit_pct(records) == 50.0


def test_build_monthly_review_structure(monkeypatch):
    monkeypatch.setattr(
        "src.engine.screener_history._load",
        lambda: [
            {
                "date": "2026-05-10",
                "code": "000001",
                "continuous_limit_up": 2,
                "board_label": "2进3",
                "is_win": False,
                "is_limit_up": True,
                "day_change": 10.0,
                "close_price": 11.0,
                "next_day_close_gain": -2.0,
                "market_limit_down": 5,
            },
            {
                "date": "2026-05-15",
                "code": "000002",
                "continuous_limit_up": 3,
                "board_label": "3进4",
                "is_win": True,
                "is_limit_up": False,
                "day_change": 5.0,
                "close_price": 10.0,
                "next_day_close_gain": 3.0,
                "market_limit_down": 4,
            },
        ],
    )
    payload = build_monthly_review(2026, 5)
    assert payload["current_month"] == "2026-05"
    assert "metrics" in payload
    assert "signal_to_limit_pct" in payload["metrics"]
    assert payload["metrics"]["signal_to_limit_pct"] == 50.0
    assert payload["metrics"]["total_trades"] == 2
    assert isinstance(payload.get("improvement_suggestions"), list)
    assert "board_breakdown" in payload


def test_calc_win_stats_monthly_zt_rate_uses_all_signals(monkeypatch):
    monkeypatch.setattr(
        "src.engine.screener_history._load",
        lambda: [
            {
                "date": "2026-05-20",
                "code": "600303",
                "is_win": None,
                "is_limit_up": True,
                "day_change": 10.0,
                "close_price": 4.0,
                "next_day_close_gain": 1.0,
            },
        ],
    )
    stats = calc_win_stats(filter_year=2026, filter_month=5)
    assert stats["monthly"]["signal_count"] == 1
    assert stats["monthly"]["signal_to_limit_pct"] == 100.0
    assert stats["monthly"]["zt_rate"] == 100.0

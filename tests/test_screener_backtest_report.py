"""screener_backtest_report 单元测试。"""
import pytest

from src.engine.screener_backtest_report import (
    _assemble_report,
    _auction_bucket,
    _b1_chart_tier,
    _build_equity_curve,
    _calc_max_drawdown,
    _compound_factor_by_day,
    _daily_portfolio_return_pct,
    _record_b1_rate,
    _return_stat_block,
    _trade_return_pct,
    _trades_by_date_from_rows,
    build_screener_backtest_report,
    invalidate_screener_backtest_cache,
)


def test_trade_return_pct_close_to_next_close():
    r = {
        "open_price": 10.0,
        "close_price": 10.0,
        "next_day_close_gain": 5.0,
    }
    # 10 * 1.05 = 10.5 → 收益 5%
    assert _trade_return_pct(r) == 5.0


def test_entry_close_from_day_change():
    from src.engine.screener_backtest_report import _entry_close_price

    r = {"pre_close": 10.0, "day_change": 10.0}
    assert _entry_close_price(r) == 11.0


def test_auction_bucket_edges():
    assert _auction_bucket(4.0) == "4~5%"
    assert _auction_bucket(4.99) == "4~5%"
    assert _auction_bucket(5.0) == "5~6%"
    assert _auction_bucket(6.0) == "6~7.5%"
    assert _auction_bucket(7.5) == "6~7.5%"


def test_daily_portfolio_third_position():
    assert _daily_portfolio_return_pct([30.0]) == 10.0
    assert _daily_portfolio_return_pct([10.0, 10.0]) == pytest.approx(20 / 3, abs=0.01)
    assert _daily_portfolio_return_pct([10.0, 10.0, 10.0]) == 10.0
    assert _daily_portfolio_return_pct([10.0, 10.0, 10.0, 10.0]) == 10.0


def test_equity_curve_weighted_position_by_day():
    rows = [
        {
            "date": "2026-01-02", "_ret": 10.0,
            "decision": {"can_open": True, "position_pct": 30},
        },
        {
            "date": "2026-01-03", "_ret": -5.0,
            "decision": {"can_open": True, "position_pct": 30},
        },
    ]
    curve = _build_equity_curve(rows)
    assert len(curve) == 2
    # day1 +10*0.3=3%, day2 -5*0.3=-1.5% → (1.03)*(0.985)-1 ≈ 1.455%
    assert curve[-1]["cumulative_pct"] == pytest.approx(1.46, abs=0.05)


def test_profit_loss_ratio():
    rows = [
        {"_ret": 10.0},
        {"_ret": -5.0},
    ]
    blk = _return_stat_block(rows)
    assert blk["profit_loss_ratio"] == 2.0


def test_max_drawdown_range():
    curve = [
        {"date": "2026-01-01", "cumulative_pct": 0},
        {"date": "2026-01-02", "cumulative_pct": 10},
        {"date": "2026-01-03", "cumulative_pct": 2},
    ]
    dd, rng = _calc_max_drawdown(curve)
    assert abs(dd - 7.27) < 0.05
    assert rng["peak_date"] == "2026-01-02"
    assert rng["trough_date"] == "2026-01-03"


def test_max_drawdown_wealth_not_arithmetic_spread():
    curve = [
        {"date": "2026-01-01", "cumulative_pct": 100},
        {"date": "2026-01-02", "cumulative_pct": -50},
    ]
    dd, _ = _calc_max_drawdown(curve)
    assert dd == 75.0
    assert dd < 100.0


def test_record_b1_rate_is_archived_yesterday_b1():
    assert _record_b1_rate({"b1_rate": 16.5}) == 16.5
    assert _record_b1_rate({"b1_rate": None}) is None
    assert _b1_chart_tier(16.5) == "≥15%"


def test_assemble_only_counts_openable_as_trades():
    rows = [
        {
            "date": "2026-04-17",
            "code": "000001",
            "b1_rate": 16.0,
            "continuous_limit_up": 2,
            "auction_gain": 5.0,
            "_ret": 5.0,
            "decision": {"can_open": True, "position_pct": 20},
        },
        {
            "date": "2026-04-17",
            "code": "000002",
            "b1_rate": 16.0,
            "continuous_limit_up": 3,
            "auction_gain": 5.5,
            "_ret": -7.0,
            "decision": {"can_open": False, "position_pct": 0},
        },
    ]
    rep = _assemble_report(rows)
    assert rep["summary"]["sample_count"] == 2
    assert rep["summary"]["return_settled"] == 1
    assert rep["summary"]["trade_sample_ratio"] == 50.0
    assert rep["by_b1_tier"]["≥15%"]["trades"] == 1
    assert rep["by_b1_tier"]["≥15%"]["sample_count"] == 2


def test_assemble_by_b1_tier_uses_record_b1_rate_only():
    rows = [
        {
            "date": "2026-04-17",
            "code": "000001",
            "b1_rate": 16.0,
            "continuous_limit_up": 2,
            "auction_gain": 5.0,
            "_ret": 5.0,
            "decision": {"can_open": True, "position_pct": 20},
        },
        {
            "date": "2026-04-17",
            "code": "000002",
            "b1_rate": 16.0,
            "continuous_limit_up": 3,
            "auction_gain": 5.5,
            "_ret": -7.0,
            "decision": {"can_open": True, "position_pct": 30},
        },
    ]
    rep = _assemble_report(rows)
    assert rep["by_b1_tier"]["≥15%"]["trades"] == 2
    assert rep["by_b1_tier"]["<12%"]["trades"] == 0


def test_build_report_has_summary():
    invalidate_screener_backtest_cache()
    rep = build_screener_backtest_report()
    assert "建议仓位" in rep["summary"]["rule"] or "开仓" in rep["summary"]["rule"]
    assert "by_board" in rep
    assert "by_b1_tier" in rep
    assert "by_board_cross_auction" in rep
    assert "equity_curve" in rep
    assert "trading_guidance" in rep
    assert "latest_trade_date" in rep
    assert "meta" in rep
    assert "sample_count" in rep["summary"]

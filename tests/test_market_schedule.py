"""盘内/盘后刷新策略单元测试。"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.config import TZ_CN
from src.market_schedule import (
    allow_dashboard_live_poll,
    allow_market_breadth_network_refresh,
    allow_minute_series_refresh,
    allow_screener_history_light_write,
    allow_trend_history_kline_backfill,
    is_intraday_trading_session,
    is_post_market_data_window,
    set_eod_bundle_running,
    startup_advice_refresh_enabled,
)


def _cn(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TZ_CN)


def test_intraday_weekday_morning(monkeypatch):
    monkeypatch.setattr("src.market_schedule.is_trading_day", lambda _d=None: True)
    monkeypatch.setattr("src.market_schedule.now_cn", lambda: _cn(2026, 5, 19, 10, 30))
    assert is_intraday_trading_session() is True
    assert is_post_market_data_window() is False
    assert allow_market_breadth_network_refresh(True) is False
    assert allow_trend_history_kline_backfill(True) is False


def test_post_market_weekday(monkeypatch):
    monkeypatch.setattr("src.market_schedule.is_trading_day", lambda _d=None: True)
    monkeypatch.setattr("src.market_schedule.now_cn", lambda: _cn(2026, 5, 19, 16, 0))
    assert is_intraday_trading_session() is False
    assert is_post_market_data_window() is True
    assert allow_market_breadth_network_refresh(True) is True
    assert allow_trend_history_kline_backfill(True) is True


def test_no_auto_breadth_without_explicit(monkeypatch):
    monkeypatch.setattr("src.market_schedule.is_trading_day", lambda _d=None: True)
    monkeypatch.setattr("src.market_schedule.now_cn", lambda: _cn(2026, 5, 19, 16, 0))
    assert allow_market_breadth_network_refresh(False) is False


def test_startup_advice_default_off(monkeypatch):
    monkeypatch.delenv("STARTUP_ADVICE_REFRESH", raising=False)
    assert startup_advice_refresh_enabled() is False


def test_dashboard_poll_intraday_only(monkeypatch):
    monkeypatch.setattr("src.market_schedule.is_trading_day", lambda _d=None: True)
    monkeypatch.setattr("src.market_schedule.now_cn", lambda: _cn(2026, 5, 19, 10, 30))
    set_eod_bundle_running(False)
    assert allow_dashboard_live_poll() is True
    assert allow_minute_series_refresh() is True
    assert allow_screener_history_light_write() is True


def test_dashboard_poll_off_post_market(monkeypatch):
    monkeypatch.setattr("src.market_schedule.is_trading_day", lambda _d=None: True)
    monkeypatch.setattr("src.market_schedule.now_cn", lambda: _cn(2026, 5, 19, 18, 0))
    set_eod_bundle_running(False)
    assert allow_dashboard_live_poll() is False
    assert allow_minute_series_refresh() is False
    assert allow_screener_history_light_write() is False


def test_screener_light_write_blocked_during_eod(monkeypatch):
    monkeypatch.setattr("src.market_schedule.is_trading_day", lambda _d=None: True)
    monkeypatch.setattr("src.market_schedule.now_cn", lambda: _cn(2026, 5, 19, 10, 30))
    set_eod_bundle_running(True)
    try:
        assert allow_dashboard_live_poll() is True
        assert allow_screener_history_light_write() is False
        assert allow_minute_series_refresh() is False
    finally:
        set_eod_bundle_running(False)

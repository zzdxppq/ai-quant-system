"""盘内/盘后行情刷新策略（避免盘内 akshare 全市场 + K 线回填拖垮进程）。

- 盘内（交易日 09:15–15:00）：禁止自动拉全市场 spot、禁止 trend K 线回填、复盘只读落盘快照
- 盘后（≥15:00 交易日）或显式 refresh：允许 breadth / 复盘 run 等重任务
"""
from __future__ import annotations

import os
import threading
from typing import Callable, TypeVar

from src.config import is_trading_day, now_cn

T = TypeVar("T")

# 全市场 spot / DuckDB / K 线等重 IO 互斥（看板多接口并发时避免 Windows 原生崩溃）
HEAVY_MARKET_NETWORK_LOCK = threading.Lock()

# 盘后串行 eod_bundle 运行中（由 scheduler 置位，供 API 跳过 light 写库）
_EOD_BUNDLE_RUNNING = False
_EOD_LOCK = threading.Lock()

# 交易日集合竞价至收盘（北京时间，分钟自 0:00）
_INTRADAY_START_MIN = 9 * 60 + 15   # 09:15
_INTRADAY_END_MIN = 15 * 60         # 15:00


def is_before_trading_auction_open(d=None) -> bool:
    """交易日 09:15 集合竞价开盘前（应展示上一交易日竞价跌停等快照）。"""
    d = d or now_cn()
    if not is_trading_day(d):
        return False
    t = d.hour * 60 + d.minute
    return t < _INTRADAY_START_MIN


def is_intraday_trading_session(d=None) -> bool:
    """交易日且处于 09:15–15:00（含连续竞价），盘内不重拉复盘/洞察/趋势回填。"""
    d = d or now_cn()
    if not is_trading_day(d):
        return False
    t = d.hour * 60 + d.minute
    return _INTRADAY_START_MIN <= t < _INTRADAY_END_MIN


def set_eod_bundle_running(running: bool) -> None:
    """标记盘后串行任务是否在执行（周期扫描 / 复盘写库等）。"""
    global _EOD_BUNDLE_RUNNING
    with _EOD_LOCK:
        _EOD_BUNDLE_RUNNING = bool(running)


def is_eod_bundle_running() -> bool:
    with _EOD_LOCK:
        return _EOD_BUNDLE_RUNNING


def allow_dashboard_live_poll(d=None) -> bool:
    """首页 hit-live / 持仓与选股实时价：仅交易日 09:15–15:00 轮询。"""
    return is_intraday_trading_session(d)


def allow_screener_history_light_write(d=None) -> bool:
    """GET screener-history?light=1 是否允许连板校正 / 轻量刷新收盘价等写库。"""
    if is_eod_bundle_running():
        return False
    return is_intraday_trading_session(d)


def allow_minute_series_refresh(d=None) -> bool:
    """分时 refresh=1 / 无缓存拉网：仅盘内且非 EOD。"""
    if is_eod_bundle_running():
        return False
    return is_intraday_trading_session(d)


def is_post_market_data_window(d=None) -> bool:
    """允许更新复盘/洞察 breadth 的时间：非交易日任意时刻，或交易日 15:00 后。"""
    d = d or now_cn()
    if not is_trading_day(d):
        return True
    t = d.hour * 60 + d.minute
    return t >= _INTRADAY_END_MIN


def allow_market_breadth_network_refresh(explicit_refresh: bool) -> bool:
    """是否允许 /api/market-insight 拉全市场算涨跌家数。

    仅当调用方显式 refresh_breadth=1 且已过 15:00（或非交易日）时为 True；
    永不因缓存 stale 在盘内自动触发。
    """
    if not explicit_refresh:
        return False
    if is_intraday_trading_session():
        return False
    return is_post_market_data_window()


def allow_review_live_hydration() -> bool:
    """GET /api/review 是否允许 hydrate_relay_env 打行情。"""
    return not is_intraday_trading_session()


def allow_trend_history_kline_backfill(explicit_full: bool) -> bool:
    """GET /api/trend-history 是否执行 reconcile + K 线回填。"""
    if not explicit_full:
        return False
    if is_intraday_trading_session():
        return False
    return is_post_market_data_window()


def startup_advice_refresh_enabled() -> bool:
    """启动时是否后台跑 write_advice_snapshot。

    STARTUP_ADVICE_REFRESH:
      0/off/false（默认）— 关闭
      1/on/true — 盘外或竞价窗 09:15–09:35 才跑
      auction — 仅竞价窗
    """
    v = os.getenv("STARTUP_ADVICE_REFRESH", "0").strip().lower()
    if v in ("0", "false", "no", "off", ""):
        return False
    if v in ("1", "true", "yes", "on"):
        if not is_trading_day():
            return True
        if is_intraday_trading_session():
            t = now_cn().hour * 60 + now_cn().minute
            return t <= 9 * 60 + 35
        return not is_intraday_trading_session()
    if v == "auction":
        if not is_trading_day():
            return False
        t = now_cn().hour * 60 + now_cn().minute
        return _INTRADAY_START_MIN <= t <= 9 * 60 + 35
    return False


def run_with_heavy_market_lock(fn: Callable[[], T]) -> T:
    with HEAVY_MARKET_NETWORK_LOCK:
        return fn()

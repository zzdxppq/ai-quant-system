"""模拟数据生成器 — 用于开发测试和演示

当无法访问东方财富/AKShare接口时使用
"""
import random
from datetime import datetime, timedelta

import pandas as pd


def generate_mock_ranking(scenario: str = "small_cycle_start") -> pd.DataFrame:
    """生成模拟10日涨幅排行数据

    Args:
        scenario: 模拟场景
            - breeding: 孕育期（无标的>45%）
            - small_cycle_start: 小周期启动（有标的>45%）
            - small_cycle_done: 小周期完成（标的>100%站榜首）
            - full_cycle: 完整周期（榜首>120%持续3天+）
            - chaos: 混沌期（多个标的轮流但没到100%）
            - ebb: 退潮期
    """
    scenarios = {
        "breeding": _scenario_breeding,
        "small_cycle_start": _scenario_small_start,
        "small_cycle_done": _scenario_small_done,
        "full_cycle": _scenario_full_cycle,
        "chaos": _scenario_chaos,
        "ebb": _scenario_ebb,
    }

    gen = scenarios.get(scenario, _scenario_small_start)
    return gen()


def _scenario_breeding() -> pd.DataFrame:
    """孕育期：最高涨幅不超过45%"""
    stocks = [
        ("600123", "测试股A", 32.5, True),
        ("300456", "创业板B", 28.3, False),
        ("600789", "测试股C", 25.1, True),
        ("688001", "科创板D", 22.8, False),
        ("601234", "测试股E", 18.6, True),
    ]
    return _build_df(stocks)


def _scenario_small_start() -> pd.DataFrame:
    """小周期启动：有标的>45%"""
    stocks = [
        ("600519", "贵州茅台", 68.5, True),
        ("300750", "宁德时代", 55.2, False),
        ("600036", "招商银行", 48.3, True),
        ("601318", "中国平安", 42.1, True),
        ("000858", "五粮液", 35.6, True),
        ("600276", "恒瑞医药", 28.9, True),
        ("300059", "东方财富", 22.4, False),
        ("601012", "隆基绿能", 18.7, True),
        ("600900", "长江电力", 12.3, True),
        ("000001", "平安银行", 8.5, True),
    ]
    return _build_df(stocks)


def _scenario_small_done() -> pd.DataFrame:
    """小周期完成：榜首标的涨幅>100%"""
    stocks = [
        ("600519", "贵州茅台", 108.5, True),
        ("300750", "宁德时代", 82.3, False),
        ("600036", "招商银行", 65.1, True),
        ("601318", "中国平安", 52.8, True),
        ("000858", "五粮液", 45.2, True),
        ("600276", "恒瑞医药", 38.6, True),
        ("300059", "东方财富", 32.1, False),
        ("601012", "隆基绿能", 25.4, True),
    ]
    return _build_df(stocks)


def _scenario_full_cycle() -> pd.DataFrame:
    """完整周期：榜首>120%"""
    stocks = [
        ("600519", "贵州茅台", 135.2, True),
        ("300750", "宁德时代", 95.6, False),
        ("600036", "招商银行", 72.3, True),
        ("601318", "中国平安", 58.1, True),
        ("000858", "五粮液", 48.5, True),
        ("600276", "恒瑞医药", 42.3, True),
    ]
    return _build_df(stocks)


def _scenario_chaos() -> pd.DataFrame:
    """混沌期：多个标的轮流但没到100%"""
    stocks = [
        ("601318", "中国平安", 72.5, True),  # 新标的站榜首但没到100%
        ("600519", "贵州茅台", 65.3, True),   # 前任从高位回落
        ("300750", "宁德时代", 58.1, False),
        ("000858", "五粮液", 45.2, True),
        ("600036", "招商银行", 38.6, True),
    ]
    return _build_df(stocks)


def _scenario_ebb() -> pd.DataFrame:
    """退潮期：原龙头大幅回落"""
    stocks = [
        ("300750", "宁德时代", 42.1, False),
        ("601318", "中国平安", 38.5, True),
        ("600519", "贵州茅台", 35.2, True),  # 原龙头大幅回落
        ("000858", "五粮液", 28.6, True),
        ("600036", "招商银行", 22.3, True),
    ]
    return _build_df(stocks)


def _build_df(stocks: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(stocks, columns=["code", "name", "gain_10d", "is_main_board"])


def generate_mock_spot() -> pd.DataFrame:
    """生成模拟实时快照"""
    stocks = [
        ("600519", "贵州茅台", 1850.0, 1880.0, 1890.0, 1845.0, 1800.0, 15000, 28000000, 2.3, 23000e8, 1.5, 4.4),
        ("600036", "招商银行", 38.5, 40.2, 40.5, 38.0, 36.8, 80000, 3200000, 3.5, 4200e8, 1.8, 5.2),
        ("601318", "中国平安", 52.0, 54.5, 55.0, 51.5, 50.0, 65000, 3500000, 2.8, 3800e8, 1.6, 5.0),
        ("000858", "五粮液", 165.0, 172.0, 173.0, 164.0, 160.0, 25000, 4300000, 1.8, 2500e8, 2.1, 6.2),
    ]
    return pd.DataFrame(stocks, columns=[
        "code", "name", "close", "open", "high", "low", "pre_close",
        "volume", "amount", "turnover", "market_cap", "volume_ratio", "change_pct"
    ])


def generate_mock_limit_up_history() -> dict[str, pd.DataFrame]:
    """生成模拟涨停历史"""
    dates = []
    d = datetime.now()
    for _ in range(5):
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    dates = sorted(dates)

    result = {}
    # 模拟600036连续3天涨停
    for date_str in dates[-3:]:
        result[date_str] = pd.DataFrame({
            "代码": ["600036", "601318", "000858"],
            "名称": ["招商银行", "中国平安", "五粮液"],
        })

    # 前2天只有601318涨停
    for date_str in dates[:2]:
        if date_str not in result:
            result[date_str] = pd.DataFrame({
                "代码": ["601318"],
                "名称": ["中国平安"],
            })

    return result

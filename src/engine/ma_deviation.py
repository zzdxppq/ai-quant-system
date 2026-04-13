"""240周线偏离度过滤模块

逻辑：
    选股公式选出标的后，检查其周线级别的偏离度：
    如果当前价格偏离240周均线太远（过度拉升），则不参与。

    240周线 ≈ 近5年均价，代表长期价值中枢。
    短期暴涨远离均线的股票，回归风险大。

    偏离度 = (当前价 - MA240W) / MA240W * 100%

    过滤规则：
    - 偏离度 > 阈值 → 标记"偏离过大"，降级或排除
    - 偏离度在合理范围内 → 通过
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.config import DATA_DIR


# 默认偏离度阈值（%），超过此值标记为偏离过大
DEFAULT_DEVIATION_MAX = 100.0


@dataclass
class DeviationResult:
    """偏离度检测结果"""
    code: str
    name: str
    current_price: float
    ma240w: float                 # 240周均线价格
    deviation_pct: float          # 偏离度(%)
    is_excessive: bool            # 是否偏离过大
    weekly_data_points: int       # 可用的周线数据点数


def check_deviation(
    code: str,
    name: str,
    current_price: float,
    weekly_closes: list[float],
    max_deviation: float = DEFAULT_DEVIATION_MAX,
) -> DeviationResult:
    """检查单只股票的240周线偏离度

    Args:
        code: 股票代码
        name: 名称
        current_price: 当前价格
        weekly_closes: 周线收盘价列表（最新在最后），至少需要240个数据点
        max_deviation: 最大允许偏离度(%)

    Returns:
        DeviationResult
    """
    data_points = len(weekly_closes)

    if data_points < 20:
        # 数据太少，无法计算，默认通过
        return DeviationResult(
            code=code, name=name, current_price=current_price,
            ma240w=0, deviation_pct=0, is_excessive=False,
            weekly_data_points=data_points,
        )

    # 取可用的周数（最多240周）
    period = min(240, data_points)
    ma240w = sum(weekly_closes[-period:]) / period

    if ma240w <= 0:
        return DeviationResult(
            code=code, name=name, current_price=current_price,
            ma240w=0, deviation_pct=0, is_excessive=False,
            weekly_data_points=data_points,
        )

    deviation_pct = (current_price - ma240w) / ma240w * 100

    return DeviationResult(
        code=code,
        name=name,
        current_price=round(current_price, 2),
        ma240w=round(ma240w, 2),
        deviation_pct=round(deviation_pct, 2),
        is_excessive=deviation_pct > max_deviation,
        weekly_data_points=data_points,
    )


def batch_check_deviation(
    stocks: list[dict],
    max_deviation: float = DEFAULT_DEVIATION_MAX,
) -> list[DeviationResult]:
    """批量检查偏离度

    Args:
        stocks: [{"code": "600xxx", "name": "xxx", "current_price": 100.0}]
        max_deviation: 最大允许偏离度(%)

    Returns:
        偏离度结果列表
    """
    results = []

    for stock in stocks:
        code = stock["code"]
        name = stock.get("name", "")
        price = stock.get("current_price", 0)

        # 获取周线数据
        weekly_closes = _get_weekly_closes(code)

        result = check_deviation(code, name, price, weekly_closes, max_deviation)
        results.append(result)

    return results


def _get_weekly_closes(code: str) -> list[float]:
    """获取股票周线收盘价

    优先级：新浪K线（scale=1200=周K） → akshare(东方财富, 可能被WAF封)
    """
    import os
    use_mock = os.getenv("MOCK", "0") == "1"

    if use_mock:
        return _mock_weekly_closes(code)

    # 优先新浪周K（稳定可用）
    try:
        from src.data.sina_kline_api import fetch_kline, SCALE_WEEKLY
        df = fetch_kline(code, scale=SCALE_WEEKLY, datalen=260)
        if df is not None and not df.empty and "close" in df.columns:
            return df["close"].astype(float).tolist()
    except Exception as e:
        print(f"  新浪周K{code}失败: {e}")

    # 兜底 akshare 东财
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(
            symbol=code, period="weekly", start_date="20210101", adjust="qfq",
        )
        if df is not None and not df.empty:
            close_col = "收盘" if "收盘" in df.columns else "close"
            return df[close_col].tolist()
    except Exception as e:
        print(f"  akshare周K{code}失败: {e}")

    return []


def _mock_weekly_closes(code: str) -> list[float]:
    """生成模拟周线数据"""
    import random
    random.seed(hash(code) % 10000)

    base = random.uniform(10, 100)
    closes = []
    price = base
    for _ in range(260):  # 约5年周线
        price *= (1 + random.uniform(-0.05, 0.05))
        closes.append(round(price, 2))

    return closes

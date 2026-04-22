"""选股引擎 — Python版通达信公式

执行时间：每日 9:27（竞价结束后）
筛选逻辑：
1. 前2日连续涨停（主板10%）
2. 竞价涨幅 4% ~ 7.5%
3. 竞价换手率 > 0.5%
4. 竞价金额 > 1000万
5. 流通市值 < 100亿
6. 量比 > 1
7. 排除 ST / 科创板 / 创业板 / 北交所
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.config import SCREENER_CONFIG, now_cn


@dataclass
class ScreenerHit:
    """选股命中结果"""
    code: str
    name: str
    continuous_limit_up: int    # 连板数
    open_price: float           # 开盘价（竞价价格）
    auction_gain: float         # 竞价涨幅(%)
    auction_turnover: float     # 竞价换手率(%)
    auction_amount: float       # 竞价金额(万元)
    market_cap: float           # 流通市值(亿)
    volume_ratio: float         # 量比
    matched_cycle: bool = False # 是否匹配周期代表股


def run_screener(
    realtime_df: pd.DataFrame,
    limit_up_history: dict[str, pd.DataFrame],
    cycle_codes: Optional[list[str]] = None,
) -> list[ScreenerHit]:
    """执行选股筛选

    Args:
        realtime_df: 实时行情快照（9:27时刻），需要列：
            code, name, open, pre_close, close, volume, amount,
            turnover, market_cap, volume_ratio
        limit_up_history: 最近N天涨停股池 {date_str: DataFrame}
        cycle_codes: 周期代表股/候选股代码列表，用于交叉验证

    Returns:
        命中结果列表
    """
    cfg = SCREENER_CONFIG
    if cycle_codes is None:
        cycle_codes = []

    # Step 1: 检测连板（前2日连续涨停）
    continuous_map = _detect_continuous_limit_up(limit_up_history)

    # 只保留 >= min_continuous_limit_up 的
    qualified_codes = {
        code: days
        for code, days in continuous_map.items()
        if days >= cfg["min_continuous_limit_up"]
    }

    if not qualified_codes:
        return []

    # Step 2: 从实时行情中筛选
    results = []
    for _, row in realtime_df.iterrows():
        code = str(row.get("code", ""))

        # 必须在连板名单中
        if code not in qualified_codes:
            continue

        # 排除规则
        name = str(row.get("name", ""))
        if not _pass_exclusion(code, name, cfg):
            continue

        # 计算竞价指标
        pre_close = float(row.get("pre_close", 0))
        open_price = float(row.get("open", 0))
        if pre_close <= 0 or open_price <= 0:
            continue

        auction_gain = (open_price / pre_close - 1) * 100

        # 竞价涨幅过滤
        if not (cfg["auction_gain_min"] <= auction_gain <= cfg["auction_gain_max"]):
            continue

        # 竞价换手率 — 软过滤：缺 market_cap 和 turnover 时放行
        auction_turnover = float(row.get("turnover", 0))
        market_cap_yuan = float(row.get("market_cap", 0))
        volume = float(row.get("volume", 0))

        if market_cap_yuan > 0:
            # 竞价换手率 ≈ 竞价成交额 / 流通市值
            auction_amount_yuan = float(row.get("amount", 0))
            auction_turnover_calc = auction_amount_yuan / market_cap_yuan * 100
        else:
            auction_turnover_calc = auction_turnover

        # 只在有效值下做下限检查，避免数据缺失误杀
        if auction_turnover_calc > 0 and auction_turnover_calc < cfg["auction_turnover_min"]:
            continue

        # PD1: 竞价成交量(手) > 阈值
        # 通达信 JJL := DYNAINFO(15)/DYNAINFO(4)/100 = 成交额/昨收/100 ≈ 成交量(手)
        # 1 手 = 100 股
        if pre_close > 0:
            auction_amount_yuan = float(row.get("amount", 0))
            auction_lots = auction_amount_yuan / pre_close / 100
        else:
            auction_lots = float(row.get("volume", 0)) / 100
        if auction_lots < cfg["auction_volume_lots_min"]:
            continue
        # 折算为万元，仅用于报告展示
        auction_amount = float(row.get("amount", 0)) / 10000

        # 流通市值（亿）— 软过滤：缺字段（market_cap<=0）则放行
        # PD4: 流通市值 > 20亿 AND 流通市值 < 100亿
        market_cap_yi = market_cap_yuan / 1e8 if market_cap_yuan > 1e6 else float(row.get("market_cap", 0))
        if market_cap_yi > 0:
            if market_cap_yi > cfg["market_cap_max"] or market_cap_yi < cfg.get("market_cap_min", 0):
                continue

        # 量比 — 软过滤：缺字段则放行
        volume_ratio = float(row.get("volume_ratio", 0))
        if volume_ratio > 0 and volume_ratio < cfg["volume_ratio_min"]:
            continue

        # 通过所有筛选
        hit = ScreenerHit(
            code=code,
            name=name,
            continuous_limit_up=qualified_codes[code],
            open_price=round(open_price, 2),
            auction_gain=round(auction_gain, 2),
            auction_turnover=round(auction_turnover_calc, 2),
            auction_amount=round(auction_amount, 2),
            market_cap=round(market_cap_yi, 2),
            volume_ratio=round(volume_ratio, 2),
            matched_cycle=code in cycle_codes,
        )
        results.append(hit)

    # 按连板数降序排列
    results.sort(key=lambda x: (-x.continuous_limit_up, -x.auction_gain))
    return results


def _detect_continuous_limit_up(limit_up_history: dict[str, pd.DataFrame]) -> dict[str, int]:
    """检测「昨日起向前」的连续涨停天数（严格对齐通达信 LIANBAN 语义）

    通达信公式: LIANBAN := REF(ZT0,1) AND REF(ZT0,2)
    要求：**昨日涨停 AND 前日涨停**（今日涨停状态不参与判定）

    算法：
    1. 排除今日（最大日期如果等于当日）
    2. 从昨日（day -1）涨停股开始，继续向前回溯
    3. 每只股票遇到非涨停日时「封档」——streak 终结，不再递增，但保留已有计数

    Returns:
        {code: consecutive_days_prior_to_today}
        只包含昨日在涨停池的股票；值为 1 代表只昨日涨停，2 代表昨+前两日连板，依此类推
    """
    if not limit_up_history:
        return {}

    from datetime import datetime
    today_str = now_cn().strftime("%Y%m%d")

    # 降序，排除今日
    all_dates = sorted(limit_up_history.keys(), reverse=True)
    past_dates = [d for d in all_dates if d != today_str]
    if not past_dates:
        return {}

    # 昨日涨停池
    yesterday_df = limit_up_history[past_dates[0]]
    if yesterday_df.empty:
        return {}
    col = _find_code_column(yesterday_df)
    if not col:
        return {}

    continuous: dict[str, int] = {}
    active: set[str] = set()  # 仍在连续中的 code
    for code in yesterday_df[col].astype(str).tolist():
        continuous[code] = 1
        active.add(code)

    # 向前回溯
    for date_str in past_dates[1:]:
        if not active:
            break
        df = limit_up_history[date_str]
        if df.empty:
            break
        col = _find_code_column(df)
        if not col:
            break
        day_codes = set(df[col].astype(str).tolist())

        still_active: set[str] = set()
        for code in active:
            if code in day_codes:
                continuous[code] += 1
                still_active.add(code)
            # 非涨停 → streak 终结；continuous[code] 保留已有值，不再处理
        active = still_active

    return continuous


def _find_code_column(df: pd.DataFrame) -> Optional[str]:
    """找到代码列"""
    for col in ["code", "代码", "股票代码"]:
        if col in df.columns:
            return col
    return df.columns[0] if len(df.columns) > 0 else None


def _pass_exclusion(code: str, name: str, cfg: dict) -> bool:
    """排除规则"""
    # ST
    if cfg["exclude_st"] and ("ST" in name.upper() or "*ST" in name.upper()):
        return False

    # 科创板 688
    if cfg["exclude_kcb"] and code.startswith("688"):
        return False

    # 创业板 300/301
    if cfg["exclude_cyb"] and code.startswith(("300", "301")):
        return False

    # 北交所 8/4 开头
    if cfg["exclude_bse"] and code.startswith(("8", "4")):
        return False

    return True

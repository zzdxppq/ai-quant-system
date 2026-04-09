"""高标龙头竞价反馈模块

逻辑：
    每日9:25竞价结束后，查看10日涨幅榜排名第一的个股（高标龙头）的竞价表现：
    - 深水开（低开>3%）或跌停 → 当日不操作（一票否决）
    - 平开或微幅低开（-3%~0%）→ 谨慎参与
    - 红开（高开0%~3%）→ 正常参与
    - 大幅高开（>3%）→ 积极参与

    高标龙头的竞价是整个市场情绪的"晴雨表"：
    它代表当前最强做多力量的延续性，如果连最强的标的都被抛弃，说明市场情绪转弱。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd


class LeaderSignal(str, Enum):
    """龙头反馈信号"""
    STRONG_POSITIVE = "强正反馈"   # 大幅高开 >3%
    POSITIVE = "正反馈"           # 红开 0%~3%
    NEUTRAL = "中性"              # 平开或微幅低开 -3%~0%
    NEGATIVE = "负反馈"           # 深水开 <-3%
    LIMIT_DOWN = "跌停"           # 一字跌停或竞价跌停


@dataclass
class LeaderFeedback:
    """高标龙头反馈结果"""
    leader_code: str              # 龙头代码
    leader_name: str              # 龙头名称
    leader_gain_10d: float        # 龙头10日涨幅(%)
    pre_close: float              # 昨收
    auction_open: float           # 竞价开盘价
    auction_change_pct: float     # 竞价涨跌幅(%)
    signal: LeaderSignal          # 信号
    can_trade: bool               # 是否可以操作
    aggression: str               # 激进程度建议
    reason: str                   # 判断理由


def evaluate_leader(
    leader_code: str,
    leader_name: str,
    leader_gain_10d: float,
    realtime_df: pd.DataFrame,
) -> LeaderFeedback:
    """评估高标龙头竞价反馈

    Args:
        leader_code: 龙头代码（10日涨幅榜第一）
        leader_name: 龙头名称
        leader_gain_10d: 龙头10日涨幅
        realtime_df: 实时行情快照（竞价后），需要 code, open, pre_close 列

    Returns:
        LeaderFeedback
    """
    # 在实时行情中找到龙头
    row = realtime_df[realtime_df["code"].astype(str) == str(leader_code)]

    if row.empty:
        return LeaderFeedback(
            leader_code=leader_code,
            leader_name=leader_name,
            leader_gain_10d=leader_gain_10d,
            pre_close=0,
            auction_open=0,
            auction_change_pct=0,
            signal=LeaderSignal.NEUTRAL,
            can_trade=False,
            aggression="观望",
            reason=f"未找到{leader_name}({leader_code})的竞价数据，建议观望",
        )

    row = row.iloc[0]
    pre_close = float(row.get("pre_close", 0))
    auction_open = float(row.get("open", 0))

    if pre_close <= 0:
        return LeaderFeedback(
            leader_code=leader_code,
            leader_name=leader_name,
            leader_gain_10d=leader_gain_10d,
            pre_close=0,
            auction_open=0,
            auction_change_pct=0,
            signal=LeaderSignal.NEUTRAL,
            can_trade=False,
            aggression="观望",
            reason="昨收价异常，无法判断",
        )

    change_pct = (auction_open / pre_close - 1) * 100

    # 判断是否跌停
    # 主板10%，创业板/科创板20%
    code_str = str(leader_code)
    if code_str.startswith(("300", "301", "688")):
        limit_down_pct = -20.0
    else:
        limit_down_pct = -10.0

    signal, can_trade, aggression, reason = _classify(
        change_pct, limit_down_pct, leader_name, leader_gain_10d
    )

    return LeaderFeedback(
        leader_code=leader_code,
        leader_name=leader_name,
        leader_gain_10d=leader_gain_10d,
        pre_close=round(pre_close, 2),
        auction_open=round(auction_open, 2),
        auction_change_pct=round(change_pct, 2),
        signal=signal,
        can_trade=can_trade,
        aggression=aggression,
        reason=reason,
    )


def _classify(
    change_pct: float,
    limit_down_pct: float,
    name: str,
    gain_10d: float,
) -> tuple[LeaderSignal, bool, str, str]:
    """分类信号"""

    # 跌停
    if change_pct <= limit_down_pct + 0.5:
        return (
            LeaderSignal.LIMIT_DOWN,
            False,
            "不操作",
            f"高标{name}(10日涨幅{gain_10d:.1f}%)竞价跌停({change_pct:+.1f}%)，"
            f"市场最强标的被抛弃，情绪极度恶化，当日不操作",
        )

    # 深水开 < -3%
    if change_pct < -3.0:
        return (
            LeaderSignal.NEGATIVE,
            False,
            "不操作",
            f"高标{name}竞价深水开({change_pct:+.1f}%)，"
            f"做多力量严重不足，当日不操作",
        )

    # 微幅低开 -3% ~ 0%
    if change_pct < 0:
        return (
            LeaderSignal.NEUTRAL,
            True,
            "谨慎",
            f"高标{name}竞价微幅低开({change_pct:+.1f}%)，"
            f"情绪偏弱但未崩，可谨慎参与，控制仓位",
        )

    # 红开 0% ~ 3%
    if change_pct <= 3.0:
        return (
            LeaderSignal.POSITIVE,
            True,
            "正常",
            f"高标{name}竞价红开({change_pct:+.1f}%)，"
            f"情绪正常偏暖，可正常参与",
        )

    # 大幅高开 > 3%
    return (
        LeaderSignal.STRONG_POSITIVE,
        True,
        "积极",
        f"高标{name}竞价大幅高开({change_pct:+.1f}%)，"
        f"做多力量强劲，积极参与",
    )


def find_leader_from_snapshot(snapshot: dict) -> Optional[tuple[str, str, float]]:
    """从周期快照中找到高标龙头（10日涨幅榜第一）

    Returns:
        (code, name, gain_10d) or None
    """
    # 优先用代表股
    if snapshot.get("representative"):
        rep = snapshot["representative"]
        return rep["code"], rep["name"], rep["gain_10d"]

    # 否则用候选池中涨幅最高的
    candidates = snapshot.get("candidates", [])
    if candidates:
        top = max(candidates, key=lambda c: c.get("gain_10d", 0))
        return top["code"], top["name"], top["gain_10d"]

    return None

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

    # 全市场快照未命中时，定向查询该股（科创板等在竞价时段可能不在全市场快照中）
    if row.empty:
        try:
            from src.data.fetcher import fetch_realtime_batch
            fallback_df = fetch_realtime_batch([str(leader_code)])
            if not fallback_df.empty:
                row = fallback_df[fallback_df["code"].astype(str) == str(leader_code)]
                print(f"  [龙头反馈] 全市场快照未命中{leader_name}({leader_code})，定向查询成功")
        except Exception as e:
            print(f"  [龙头反馈] 定向查询{leader_code}失败: {e}")

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
    """从周期快照中找到高标龙头（10日涨幅榜第一，全市场）

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


def find_main_board_leader_from_snapshot(
    snapshot: dict,
) -> Optional[tuple[str, str, float]]:
    """从周期快照中找到主板最高标（排除创业板/科创板/北交所）

    Returns:
        (code, name, gain_10d) or None
    """
    def _is_main_board(code: str) -> bool:
        code = str(code)
        if code.startswith(("300", "301")):
            return False
        if code.startswith("688"):
            return False
        if code.startswith(("8", "4")):
            return False
        return True

    # 代表股如果是主板，直接返回
    if snapshot.get("representative"):
        rep = snapshot["representative"]
        if _is_main_board(rep["code"]):
            return rep["code"], rep["name"], rep["gain_10d"]

    # 从候选池中找主板涨幅最高的
    candidates = snapshot.get("candidates", [])
    main_board = [c for c in candidates if _is_main_board(c.get("code", ""))]
    if main_board:
        top = max(main_board, key=lambda c: c.get("gain_10d", 0))
        return top["code"], top["name"], top["gain_10d"]

    return None


def evaluate_lianban_leader(
    leader_code: str,
    leader_name: str,
    board_count: int,
    realtime_df: pd.DataFrame,
) -> LeaderFeedback:
    """评估主板连板高标竞价反馈（用连板数代替10日涨幅作为情绪锚定）"""
    row = realtime_df[realtime_df["code"].astype(str) == str(leader_code)]
    if row.empty:
        try:
            from src.data.fetcher import fetch_realtime_batch
            fallback_df = fetch_realtime_batch([str(leader_code)])
            if not fallback_df.empty:
                row = fallback_df[fallback_df["code"].astype(str) == str(leader_code)]
        except Exception:
            pass

    if row.empty:
        return LeaderFeedback(
            leader_code=leader_code, leader_name=leader_name,
            leader_gain_10d=float(board_count), pre_close=0, auction_open=0,
            auction_change_pct=0, signal=LeaderSignal.NEUTRAL, can_trade=False,
            aggression="观望",
            reason=f"未找到{leader_name}({leader_code})的竞价数据，建议观望",
        )

    row = row.iloc[0]
    pre_close = float(row.get("pre_close", 0))
    auction_open = float(row.get("open", 0))

    if pre_close <= 0:
        return LeaderFeedback(
            leader_code=leader_code, leader_name=leader_name,
            leader_gain_10d=float(board_count), pre_close=0, auction_open=0,
            auction_change_pct=0, signal=LeaderSignal.NEUTRAL, can_trade=False,
            aggression="观望", reason="昨收价异常，无法判断",
        )

    change_pct = (auction_open / pre_close - 1) * 100
    # 主板连板高标按定义只来自主板，跌停 -10%
    limit_down_pct = -10.0

    signal, can_trade, aggression, reason = _classify_lianban(
        change_pct, limit_down_pct, leader_name, board_count
    )

    return LeaderFeedback(
        leader_code=leader_code, leader_name=leader_name,
        leader_gain_10d=float(board_count),  # 复用字段，承载连板数
        pre_close=round(pre_close, 2),
        auction_open=round(auction_open, 2),
        auction_change_pct=round(change_pct, 2),
        signal=signal, can_trade=can_trade, aggression=aggression, reason=reason,
    )


def _classify_lianban(
    change_pct: float,
    limit_down_pct: float,
    name: str,
    board_count: int,
) -> tuple[LeaderSignal, bool, str, str]:
    """连板高标信号分类（措辞围绕连板情绪）"""
    if change_pct <= limit_down_pct + 0.5:
        return (
            LeaderSignal.LIMIT_DOWN, False, "不操作",
            f"主板{board_count}连板{name}竞价跌停({change_pct:+.1f}%)，"
            f"连板高标崩塌，接力情绪极度恶化",
        )
    if change_pct < -3.0:
        return (
            LeaderSignal.NEGATIVE, False, "不操作",
            f"主板{board_count}连板{name}竞价深水开({change_pct:+.1f}%)，"
            f"接力情绪严重不足",
        )
    if change_pct < 0:
        return (
            LeaderSignal.NEUTRAL, True, "谨慎",
            f"主板{board_count}连板{name}竞价微幅低开({change_pct:+.1f}%)，"
            f"接力情绪偏弱但未崩",
        )
    if change_pct <= 3.0:
        return (
            LeaderSignal.POSITIVE, True, "正常",
            f"主板{board_count}连板{name}竞价红开({change_pct:+.1f}%)，"
            f"接力情绪正常偏暖",
        )
    return (
        LeaderSignal.STRONG_POSITIVE, True, "积极",
        f"主板{board_count}连板{name}竞价大幅高开({change_pct:+.1f}%)，"
        f"接力情绪强劲延续",
    )


def _is_main_board_code(code: str) -> bool:
    code = str(code)
    if code.startswith(("300", "301", "688", "8", "4")):
        return False
    return True


def find_main_board_lianban_leaders(
    limit_up_history: dict,
    spot_df: pd.DataFrame,
) -> list[tuple[str, str, int]]:
    """找到「主板」昨日连板数最高的所有股票（>=2连板，平局全返回）

    Returns:
        [(code, name, board_count), ...]，按 code 升序；无候选返回 []
    """
    if not limit_up_history:
        return []

    from src.engine.screener import _detect_continuous_limit_up
    continuous = _detect_continuous_limit_up(limit_up_history)
    if not continuous:
        return []

    main_board = {c: d for c, d in continuous.items() if _is_main_board_code(c)}
    main_board = {c: d for c, d in main_board.items() if d >= 2}
    if not main_board:
        return []

    max_count = max(main_board.values())
    top_codes = sorted([c for c, d in main_board.items() if d == max_count])

    results: list[tuple[str, str, int]] = []
    for code in top_codes:
        name = ""
        if spot_df is not None and not spot_df.empty:
            match = spot_df[spot_df["code"].astype(str) == str(code)]
            if not match.empty:
                name = str(match.iloc[0].get("name", ""))
        results.append((code, name, max_count))
    return results


def find_main_board_lianban_leader(
    limit_up_history: dict,
    spot_df: pd.DataFrame,
) -> Optional[tuple[str, str, int]]:
    """单只兼容包装：返回平局中 code 最小的一只。"""
    leaders = find_main_board_lianban_leaders(limit_up_history, spot_df)
    return leaders[0] if leaders else None


def compute_yesterday_main_board_auction(
    limit_up_history: dict,
    spot_df: pd.DataFrame,
) -> Optional[dict]:
    """计算「昨日主板涨停股」今日竞价的平均表现，用于接力情绪锚定

    Returns:
        {date, sample_count, avg_change_pct, positive_count, negative_count, limit_down_count}
        或 None
    """
    if not limit_up_history:
        return None

    from src.config import now_cn
    today_str = now_cn().strftime("%Y%m%d")

    all_dates = sorted(limit_up_history.keys(), reverse=True)
    past_dates = [d for d in all_dates if d != today_str]
    if not past_dates:
        return None

    yesterday_str = past_dates[0]
    df = limit_up_history[yesterday_str]
    if df is None or df.empty:
        return None

    code_col = None
    for col in ["code", "代码", "股票代码"]:
        if col in df.columns:
            code_col = col
            break
    if code_col is None:
        if len(df.columns) == 0:
            return None
        code_col = df.columns[0]

    codes = [
        str(c).zfill(6)
        for c in df[code_col].astype(str).tolist()
        if _is_main_board_code(c)
    ]
    if not codes:
        return None

    # 传入 spot_df 通常是东财 top500 局部样本，无法覆盖全部昨日涨停股。
    # 检查命中率，命中率<70% 时主动用新浪全市场（5000+ 只）兜底，确保样本完整。
    def _spot_hit_rate(s):
        if s is None or s.empty:
            return 0.0
        s_codes = set(s["code"].astype(str))
        hits = sum(1 for c in codes if c in s_codes)
        return hits / len(codes) if codes else 0.0

    hit_rate = _spot_hit_rate(spot_df)
    if hit_rate < 0.7:
        try:
            from src.data.sina_spot_api import fetch_a_share_list_sina
            full_df = fetch_a_share_list_sina()
            if full_df is not None and not full_df.empty:
                full_hit_rate = _spot_hit_rate(full_df)
                if full_hit_rate > hit_rate:
                    print(f"[昨日主板涨停均价] 局部 spot 命中率 {hit_rate:.0%} 不足，"
                          f"切换新浪全市场（命中率 {full_hit_rate:.0%}）")
                    spot_df = full_df
        except Exception as e:
            print(f"[昨日主板涨停均价] 新浪全市场拉取失败: {e}")

    if spot_df is None or spot_df.empty:
        return None

    changes = []
    pos = neg = ld = 0
    high5_count = 0   # 高开 >5%
    flat2_count = 0   # 平开附近 ±2%
    low5_count = 0    # 低开 <-5%
    for code in codes:
        match = spot_df[spot_df["code"].astype(str) == code]
        if match.empty:
            continue
        row = match.iloc[0]
        pre_close = float(row.get("pre_close", 0))
        open_price = float(row.get("open", 0))
        if pre_close <= 0 or open_price <= 0:
            continue
        chg = (open_price / pre_close - 1) * 100
        changes.append(chg)
        if chg > 0:
            pos += 1
        elif chg < 0:
            neg += 1
        if chg <= -9.5:
            ld += 1
        if chg > 5:
            high5_count += 1
        elif chg < -5:
            low5_count += 1
        if -2 <= chg <= 2:
            flat2_count += 1

    if not changes:
        return None

    avg = sum(changes) / len(changes)
    sorted_chg = sorted(changes)
    n = len(sorted_chg)
    median = (sorted_chg[n // 2] + sorted_chg[(n - 1) // 2]) / 2 if n else 0
    return {
        "date": yesterday_str,
        "sample_count": len(changes),
        "avg_change_pct": round(avg, 2),
        "median_change_pct": round(median, 2),
        "high5_count": high5_count,
        "flat2_count": flat2_count,
        "low5_count": low5_count,
        "positive_count": pos,
        "negative_count": neg,
        "limit_down_count": ld,
    }


def compute_yesterday_limit_down_today_auction(
    spot_df: pd.DataFrame,
) -> Optional[dict]:
    """昨日竞价跌停股 今日竞价均价 — 弱势股反弹/续跌信号

    数据源：sentiment_history.json 中昨日保存的 limit_down_codes
    """
    if spot_df is None or spot_df.empty:
        return None
    from src.engine.sentiment_pool import get_prev_limit_down_codes
    codes = get_prev_limit_down_codes()
    if not codes:
        return None

    spot_codes = set(spot_df["code"].astype(str))
    changes = []
    pos = neg = ld = 0
    for c in codes:
        c = str(c).zfill(6)
        if c not in spot_codes:
            continue
        row = spot_df[spot_df["code"].astype(str) == c].iloc[0]
        pre_close = float(row.get("pre_close", 0))
        open_price = float(row.get("open", 0))
        if pre_close <= 0 or open_price <= 0:
            continue
        chg = (open_price / pre_close - 1) * 100
        changes.append(chg)
        if chg > 0:
            pos += 1
        elif chg < 0:
            neg += 1
        if chg <= -9.5:
            ld += 1

    if not changes:
        return None

    avg = sum(changes) / len(changes)
    return {
        "pool_size": len(codes),
        "sample_count": len(changes),
        "avg_change_pct": round(avg, 2),
        "positive_count": pos,
        "negative_count": neg,
        "limit_down_count": ld,
    }


def compute_yesterday_zb_today_auction(
    spot_df: pd.DataFrame,
) -> Optional[dict]:
    """昨日炸板股 今日竞价均价 — 接力情绪反向锚定

    炸板池来自东财 push2ex（与一字涨停池同源）。
    一字涨停 vs 炸板的差异：
      - 一字涨停：盘中无炸开，封板成功
      - 炸板：盘中曾涨停但被砸开，最终未涨停
    炸板股次日的接力情绪通常更弱（卖盘高位换手），其今日竞价表现是 重要负向信号。

    Returns:
        {date, sample_count, avg_change_pct, positive_count, negative_count, limit_down_count}
    """
    if spot_df is None or spot_df.empty:
        return None

    from src.config import now_cn
    from src.data.zt_pool_api import fetch_zb_pool

    today_str = now_cn().strftime("%Y%m%d")
    # 昨日炸板（取最近一交易日。当前 today 的 zb 池在收盘后才有，所以 9:27 时拉昨日）
    # 简单做法：先试今天再倒推（外层调用通常已有昨日缓存习惯）
    from datetime import timedelta
    base = now_cn().date()
    pool = {}
    yesterday_str = None
    for back in range(1, 8):
        d = (base - timedelta(days=back)).strftime("%Y%m%d")
        pool = fetch_zb_pool(d)
        if pool:
            yesterday_str = d
            break
    if not pool:
        return None

    spot_codes = set(spot_df["code"].astype(str))
    changes = []
    pos = neg = ld = 0
    for code in pool.keys():
        c = str(code).zfill(6)
        if c not in spot_codes:
            continue
        row = spot_df[spot_df["code"].astype(str) == c].iloc[0]
        pre_close = float(row.get("pre_close", 0))
        open_price = float(row.get("open", 0))
        if pre_close <= 0 or open_price <= 0:
            continue
        chg = (open_price / pre_close - 1) * 100
        changes.append(chg)
        if chg > 0:
            pos += 1
        elif chg < 0:
            neg += 1
        if chg <= -9.5:
            ld += 1

    if not changes:
        return None

    avg = sum(changes) / len(changes)
    return {
        "date": yesterday_str,
        "pool_size": len(pool),
        "sample_count": len(changes),
        "avg_change_pct": round(avg, 2),
        "positive_count": pos,
        "negative_count": neg,
        "limit_down_count": ld,
    }


def find_main_board_leader_from_ranking(
    ranking_file: str,
) -> Optional[tuple[str, str, float]]:
    """从排行数据中找到主板最高标（快照无主板候选时的兜底）

    Args:
        ranking_file: latest_ranking.json 的路径

    Returns:
        (code, name, gain_10d) or None
    """
    import json
    from pathlib import Path

    path = Path(ranking_file)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
    except Exception:
        return None

    for item in data.get("ranking", []):
        code = str(item.get("code", ""))
        if item.get("is_main_board", False):
            return code, item.get("name", ""), item.get("gain_10d", 0)

    return None

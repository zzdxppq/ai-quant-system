"""交叉验证引擎

六层评估交叉：
1. 周期状态（大环境）
2. 高标龙头竞价反馈（市场10日涨幅高标当日操作建议）
3. 梯队情绪池（top30 龙头竞价分布 → 当日接力意愿）
4. 选股公式命中（标的筛选）
5. 市场风向（弱势/中性/强势 — 来自全市场竞价分布）
6. 昨日主板连板高标今日竞价（核按钮跌停/深水低开 一票否决）
"""
from dataclasses import dataclass
from typing import Optional

from src.engine.cycle import CycleSnapshot, CyclePhase
from src.engine.screener import ScreenerHit
from src.engine.leader_feedback import LeaderFeedback, LeaderSignal
from src.engine.sentiment_pool import PoolSentiment, MarketAuctionStats


@dataclass
class Signal:
    """交叉验证信号"""
    code: str
    name: str
    level: str           # "strong" / "normal" / "watch" / "avoid"
    reason: str
    cycle_phase: str     # 当前周期阶段
    leader_signal: str   # 龙头反馈
    is_cycle_rep: bool   # 是否周期代表股
    is_cycle_candidate: bool  # 是否周期候选股
    screener_hit: Optional[ScreenerHit] = None
    suggested_position: str = ""  # 建议仓位


def cross_validate(
    cycle: CycleSnapshot,
    screener_hits: list[ScreenerHit],
    leader_fb: Optional[LeaderFeedback] = None,
    pool_sentiment: Optional[PoolSentiment] = None,
    market_stats: Optional[MarketAuctionStats] = None,
    main_board_leaders: Optional[list[LeaderFeedback]] = None,
) -> list[Signal]:
    """六层交叉验证

    Args:
        cycle: 周期快照
        screener_hits: 选股结果
        leader_fb: 市场高标(10日涨幅) 龙头竞价反馈（可选）
        pool_sentiment: 梯队情绪池（可选）
        market_stats: 全市场竞价风向（含 drop_over_9pct, limit_down 等）
        main_board_leaders: 昨日主板连板高标 list（平局多只）

    Returns:
        信号列表，按强度排序
    """
    signals = []

    rep_code = cycle.representative["code"] if cycle.representative else None
    candidate_codes = {c["code"] for c in cycle.candidates}
    phase = cycle.phase
    leader_sig = leader_fb.signal if leader_fb else None

    for hit in screener_hits:
        is_rep = hit.code == rep_code
        is_candidate = hit.code in candidate_codes

        level, reason, position = _evaluate(
            phase, is_rep, is_candidate, hit, leader_fb
        )

        # 第一层：梯队情绪调节
        level, reason, position = _apply_sentiment(
            level, reason, position, pool_sentiment
        )

        # 第二层：市场风向 + 主板连板高标 三维联动
        # （核按钮跌停一票否决 / 双警戒强压 / 单警戒降档 / 全强加成）
        level, reason, position = _apply_market_lianban(
            level, reason, position,
            market_stats, main_board_leaders, pool_sentiment,
        )

        signal = Signal(
            code=hit.code,
            name=hit.name,
            level=level,
            reason=reason,
            cycle_phase=phase,
            leader_signal=leader_sig.value if leader_sig else "未知",
            is_cycle_rep=is_rep,
            is_cycle_candidate=is_candidate,
            screener_hit=hit,
            suggested_position=position,
        )
        signals.append(signal)

    # 排序：strong > normal > watch > avoid
    level_order = {"strong": 0, "normal": 1, "watch": 2, "avoid": 3}
    signals.sort(key=lambda s: level_order.get(s.level, 99))

    return signals


def _apply_market_lianban(
    level: str,
    reason: str,
    position: str,
    market_stats: Optional[MarketAuctionStats],
    main_board_leaders: Optional[list[LeaderFeedback]],
    pool_sent: Optional[PoolSentiment],
) -> tuple[str, str, str]:
    """市场风向 + 主板连板高标 + 加权竞价 三维联动调节

    优先级：核按钮 > 双警戒 > 单警戒 > 全强

    1. 核按钮（一票否决）：主板连板高标任一跌停 / 深水低开<-5% → 强压 avoid 不开仓
    2. 双警戒（任二命中）：market弱势 / 加权<0 / 高标负反馈 → strong/normal/watch 各降一档
    3. 单警戒：strong→normal仓位减半 / normal→watch
    4. 全强加成：连板高标全部强正反馈+其它两维不警戒 → 维持 + 备注
    """
    if not market_stats and not main_board_leaders and not pool_sent:
        return level, reason, position

    market_weak = bool(market_stats) and market_stats.verdict == "弱势"
    pool_negative = (
        pool_sent is not None
        and isinstance(pool_sent.weighted_auction_gain, (int, float))
        and pool_sent.weighted_auction_gain < 0
    )

    mb_list = main_board_leaders or []
    lianban_limit_down = [fb for fb in mb_list if fb.signal == LeaderSignal.LIMIT_DOWN]
    lianban_deep_low = [
        fb for fb in mb_list
        if fb.signal != LeaderSignal.LIMIT_DOWN and fb.auction_change_pct <= -5
    ]
    lianban_negative = [fb for fb in mb_list if fb.signal == LeaderSignal.NEGATIVE]
    lianban_all_strong = (
        len(mb_list) > 0
        and all(fb.signal == LeaderSignal.STRONG_POSITIVE for fb in mb_list)
    )

    # ---- 1. 核按钮一票否决：连板高标跌停 ----
    if lianban_limit_down:
        names = "/".join(fb.leader_name for fb in lianban_limit_down)
        tag = f"[核按钮: {names} 跌停，接力标杆崩塌]"
        if level in ("strong", "normal", "watch"):
            return "avoid", f"{reason}；{tag} 建议空仓避险", "不开仓"
        return level, f"{reason}；{tag}", position

    # ---- 1b. 核按钮前奏：连板高标深水低开 < -5% ----
    if lianban_deep_low:
        names = "/".join(
            f"{fb.leader_name}({fb.auction_change_pct:+.1f}%)"
            for fb in lianban_deep_low
        )
        tag = f"[连板高标深水低开: {names}]"
        if level == "strong":
            return "watch", f"{reason}；{tag} 标杆松动，压至观察", "1层试探"
        if level == "normal":
            return "watch", f"{reason}；{tag} 标杆松动，降至观察", _half_position(position)
        if level == "watch":
            return "avoid", f"{reason}；{tag} 标杆松动，建议观望", "不开仓"
        return level, f"{reason}；{tag}", position

    # ---- 收集轻度警戒 ----
    warnings = []
    if market_weak:
        warnings.append("市场弱势")
    if pool_negative:
        warnings.append(f"加权竞价{pool_sent.weighted_auction_gain:+.1f}%(<0)")
    if lianban_negative:
        names = "/".join(fb.leader_name for fb in lianban_negative)
        warnings.append(f"连板高标负反馈({names})")

    # ---- 2. 双警戒：任二命中 → 强压 ----
    if len(warnings) >= 2:
        tag = f"[多维警戒: {' + '.join(warnings)}]"
        if level == "strong":
            return "watch", f"{reason}；{tag} 多维偏弱，压至观察", "1-2层试探"
        if level == "normal":
            return "watch", f"{reason}；{tag} 多维偏弱，降至观察", _half_position(position)
        if level == "watch":
            return "avoid", f"{reason}；{tag} 多维偏弱，建议观望", "不开仓"
        return level, f"{reason}；{tag}", position

    # ---- 3. 单警戒：降一档 + 仓位减半 ----
    if len(warnings) == 1:
        tag = f"[单维警戒: {warnings[0]}]"
        if level == "strong":
            return "normal", f"{reason}；{tag} 略偏弱，降一档", _half_position(position)
        if level == "normal":
            return "watch", f"{reason}；{tag} 偏弱，降至观察", position
        return level, f"{reason}；{tag}", position

    # ---- 4. 全强加成：连板高标全员强正反馈 + 其它两维不警戒 ----
    if lianban_all_strong:
        names = "/".join(fb.leader_name for fb in mb_list)
        return level, f"{reason}；[连板高标全员强正反馈({names})] 接力情绪强劲", position

    return level, reason, position


def _apply_sentiment(
    level: str,
    reason: str,
    position: str,
    sent: Optional[PoolSentiment],
) -> tuple[str, str, str]:
    """梯队情绪池对信号的调节规则

    规则：
    - 不操作：不改等级，但追加"梯队情绪压制"标签，仓位打对折
    - 谨慎：strong→normal / normal→watch；仓位写"减半"
    - 正常：不变
    - 积极：avoid→watch（破例开观察仓）；watch→normal
    """
    if sent is None or not sent.verdict:
        return level, reason, position

    verdict = sent.verdict
    wavg = sent.weighted_auction_gain
    tag = f"[情绪:{verdict} {wavg:+.1f}%]"

    if verdict == "不操作":
        # 不强行改 avoid，但把 strong/normal 强压成 watch
        if level in ("strong", "normal"):
            return "watch", f"{reason}；{tag} 梯队接力意愿极弱，压至观察仓", "1-2层试探（情绪压制）"
        return level, f"{reason}；{tag}", position

    if verdict == "谨慎":
        if level == "strong":
            return "normal", f"{reason}；{tag} 梯队偏弱，降一档", _half_position(position)
        if level == "normal":
            return "watch", f"{reason}；{tag} 梯队偏弱，降至观察", _half_position(position)
        return level, f"{reason}；{tag}", position

    if verdict == "积极":
        if level == "avoid":
            # 周期不支持但梯队强势 → 破例开试探
            return "watch", f"{reason}；{tag} 梯队强势，破例开 1 层试探", "1层试探（情绪破例）"
        if level == "watch":
            return "normal", f"{reason}；{tag} 梯队强势，升一档", position
        return level, f"{reason}；{tag}", position

    # 正常：保持
    return level, f"{reason}；{tag}", position


def _half_position(position: str) -> str:
    """在仓位建议后追加"减半"标注，不重写具体数字"""
    if not position or position == "不开仓":
        return position
    return f"{position}（情绪压制减半）"


def _evaluate(
    phase: str,
    is_rep: bool,
    is_candidate: bool,
    hit: ScreenerHit,
    leader_fb: Optional[LeaderFeedback],
) -> tuple[str, str, str]:
    """评估单个标的的信号强度（已排除龙头否决场景）

    Returns:
        (level, reason, suggested_position)
    """
    # 龙头反馈对仓位的调节系数
    aggression = "正常"
    if leader_fb:
        aggression = leader_fb.aggression  # "积极"/"正常"/"谨慎"/"观望"

    # === 完整周期/小周期完成 — 最佳阶段 ===
    if phase in (CyclePhase.FULL_CYCLE.value, CyclePhase.SMALL_CYCLE_DONE.value):
        if is_rep:
            base_reason = f"周期代表股 × 选股命中，{phase}阶段，双重确认"
            if aggression == "积极":
                return ("strong", base_reason + "，龙头强正反馈", "6-9层（可加仓至接近满仓）")
            if aggression == "谨慎":
                return ("normal", base_reason + "，但龙头反馈偏弱", "3-4层（控仓）")
            return ("strong", base_reason, "6层（底仓3+确认加仓3）")
        if is_candidate:
            return ("normal", f"周期候选股 × 选股命中，{phase}阶段", "3层底仓")
        return ("watch", f"选股命中但非周期标的，{phase}阶段可适度参与", "2层试探")

    # === 小周期启动 — 可参与但控仓 ===
    if phase == CyclePhase.SMALL_CYCLE_START.value:
        if is_rep or is_candidate:
            if aggression == "积极":
                return ("normal", "周期候选 × 选股命中，小周期启动+龙头强反馈", "3-4层底仓")
            if aggression == "谨慎":
                return ("watch", "周期候选 × 选股命中，小周期启动但龙头偏弱", "2层观察")
            return ("normal", "周期候选 × 选股命中，小周期启动阶段", "3层底仓")
        return ("watch", "选股命中，小周期刚启动，非周期标的需谨慎", "1-2层观察仓")

    # === 混沌期 — 极度谨慎 ===
    if phase == CyclePhase.CHAOS.value:
        if is_candidate:
            return ("watch", "混沌期，资金畏高，周期候选但确定性不足", "1-2层控仓")
        return ("avoid", "混沌期，无明确周期方向，建议观望", "不开仓")

    # === 余温期 — 持股为主不追高 ===
    if phase == CyclePhase.AFTERGLOW.value:
        return ("watch", "余温期，周期趋弱，不宜新开仓", "持有为主，不加仓")

    # === 退潮期/孕育期 — 不操作 ===
    if phase in (CyclePhase.EBB.value, CyclePhase.BREEDING.value):
        return ("avoid", f"{phase}，等待新周期信号", "不开仓")

    return ("watch", f"当前阶段{phase}，谨慎参与", "1层观察")

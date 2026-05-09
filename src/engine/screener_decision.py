"""每只股票按梯队 + 竞价 + 环境的开仓决策（用户口径）

规则
====
环境硬性:
  · 1进2 成功率 < 15%   → 空仓（不论梯队）
  · 跌停潮 ≥9            → 所有梯队仓位减半

2进3 (board=2) 全部满足才开:
  · 竞价涨幅 4% ~ 8%
  · 竞价换手 > 0.7%
  · 竞价量比 > 5%

3进4 及以上 (board≥3) 全部满足才开:
  · 竞价涨幅 4% ~ 8%
  · 竞价换手 > 0.5%
  · 昨日最高板（空间板）今日不跌停（可以小跌）
  · 5板+ 额外：唯一空间板 OR 同概念涨停梯队≥3只

仓位档位（基于 b1_rate）
  >30%:    2进3=40%  3进4=30%  4进5+=30%
  25-30%:  30% / 25% / 25%
  20-25%:  15% / 20% / 20%
  15-20%:  10% / 15% / 15%
  10-15%:  0%  / 10% / 10%
  <10%:    0%  / 0%  / 0%
"""
from __future__ import annotations
from typing import Optional


# (b1_min_inclusive, b1_max_exclusive, pos_2to3, pos_3to4, pos_4plus)
POSITION_TABLE = [
    (30.0, 1000.0, 40, 30, 30),
    (25.0, 30.0,   30, 25, 25),
    (20.0, 25.0,   15, 20, 20),
    (15.0, 20.0,   10, 15, 15),
    (10.0, 15.0,    0, 10, 10),
    (0.0,  10.0,    0,  0,  0),
]


def _lookup_positions(b1_rate: Optional[float]) -> tuple[int, int, int]:
    if b1_rate is None:
        return (0, 0, 0)
    for lo, hi, p1, p2, p3 in POSITION_TABLE:
        if lo <= b1_rate < hi:
            return (p1, p2, p3)
    return (0, 0, 0)


def compute_per_stock_decision(
    hit: dict,
    market_env: dict,
    concept_zt_stats: Optional[list[dict]] = None,
    space_board_today: Optional[dict] = None,
    market_highest_board: Optional[int] = None,
) -> dict:
    """对单只 hit 按梯队规则出决策

    Args:
        hit: ScreenerHit asdict —— 含 continuous_limit_up / auction_gain /
             auction_turnover / auction_volume_ratio / top_concepts
        market_env: 当日市场环境 {b1_rate, market_limit_down, ...}
        concept_zt_stats: latest_review.concept_zt_stats（用于 5板+ 概念梯队检查）
        space_board_today: latest_review.relay_env.prev_space_board_today
                           （用于 3+板 空间板今日表现检查）
        market_highest_board: 今日全市场最高连板数（用于 5+板 唯一空间板判定）

    Returns: {
        action: "开仓" / "空仓" / "观察",
        position_pct: int,
        position_text: str,
        reason: str,
        can_open: bool,
        ladder_label: "2进3" / "3进4" / ...,
        veto_reason: str | None,
        halved: bool,
    }
    """
    board = int(hit.get("continuous_limit_up", 0) or 0)
    ladder_label = f"{board}进{board + 1}" if board >= 1 else "首板"

    auction_gain = hit.get("auction_gain")
    auction_turnover = hit.get("auction_turnover")
    auction_vr = hit.get("auction_volume_ratio")
    try:
        ag = float(auction_gain) if auction_gain is not None else None
    except (TypeError, ValueError):
        ag = None
    try:
        at = float(auction_turnover) if auction_turnover is not None else None
    except (TypeError, ValueError):
        at = None
    try:
        avr = float(auction_vr) if auction_vr is not None else None
    except (TypeError, ValueError):
        avr = None

    b1_rate = market_env.get("b1_rate")
    try:
        b1 = float(b1_rate) if b1_rate is not None else None
    except (TypeError, ValueError):
        b1 = None
    limit_down = market_env.get("market_limit_down")
    try:
        ld = int(limit_down) if limit_down is not None else None
    except (TypeError, ValueError):
        ld = None

    # ── 硬性否决 1: b1_rate < 15% ──
    if b1 is not None and b1 < 15:
        return _veto(ladder_label, f"硬性否决：1进2 成功率 {b1:.1f}% < 15%", "b1_rate_low")

    # ── 仅覆盖 ≥2板（首板不在规则内）──
    if board < 2:
        return {
            "action": "观察",
            "position_pct": 0,
            "position_text": "0% (规则未覆盖)",
            "reason": "首板不在本梯队规则范围（仅 2进3 及以上）",
            "can_open": False,
            "ladder_label": ladder_label,
            "veto_reason": None,
            "halved": False,
        }

    # ── 仓位查表 ──
    pos_2to3, pos_3to4, pos_4plus = _lookup_positions(b1)
    if board == 2:
        target_pct = pos_2to3
    elif board == 3:
        target_pct = pos_3to4
    else:  # board >= 4
        target_pct = pos_4plus

    # ── 跌停潮 → 减半 ──
    halved = False
    if ld is not None and ld >= 9:
        target_pct = target_pct // 2
        halved = True

    # 表查得 0 → 直接空仓（无须再校验）
    if target_pct <= 0:
        msg = f"1进2 {b1:.1f}% 档位下 {ladder_label} 不开仓" if b1 is not None else "数据不足"
        return _veto(ladder_label, msg, "table_zero", halved=halved)

    # ── 竞价校验 ──
    fails: list[str] = []
    if ag is None:
        fails.append("竞价涨幅缺失")
    elif not (4 <= ag <= 8):
        fails.append(f"竞价涨幅 {ag:.2f}% 不在 [4,8]")

    if board == 2:
        # 2进3: 换手>0.7 + 量比>5
        if at is None:
            fails.append("竞价换手缺失")
        elif at <= 0.7:
            fails.append(f"竞价换手 {at:.2f}% ≤ 0.7%")
        if avr is None:
            fails.append("竞价量比缺失")
        elif avr <= 5:
            fails.append(f"竞价量比 {avr:.2f}% ≤ 5%")
    else:  # board >= 3
        # 3进4+: 换手>0.5 + 空间板不跌停
        if at is None:
            fails.append("竞价换手缺失")
        elif at <= 0.5:
            fails.append(f"竞价换手 {at:.2f}% ≤ 0.5%")
        # 空间板今日跌停校验
        if space_board_today:
            today_pct = space_board_today.get("today_pct")
            try:
                tp = float(today_pct) if today_pct is not None else None
            except (TypeError, ValueError):
                tp = None
            if tp is not None and tp <= -9.5:
                space_name = space_board_today.get("name", "空间板")
                fails.append(f"昨日空间板{space_name}今日跌停 ({tp:.1f}%)")

    # ── 5板+ 额外检查：唯一空间板 OR 同概念涨停梯队 ≥3只 ──
    if board >= 5:
        is_unique_space = False
        if market_highest_board is not None:
            is_unique_space = (board == market_highest_board)
        concept_ladder_ok = False
        if concept_zt_stats:
            top_concepts = list(hit.get("top_concepts") or [])
            for c in concept_zt_stats:
                if c.get("name") in top_concepts:
                    if int(c.get("limit_up_count", 0) or 0) >= 3:
                        concept_ladder_ok = True
                        break
        if not (is_unique_space or concept_ladder_ok):
            fails.append("5板+ 额外条件未达（既非唯一空间板，同概念涨停 <3只）")

    if fails:
        return _veto(ladder_label, "竞价条件未达：" + "；".join(fails), "auction_fail", halved=halved)

    # ── 通过：开仓 ──
    pos_text = f"{target_pct}%" + ("（跌停潮减半）" if halved else "")
    reason_bits = [
        f"{ladder_label}",
        f"1进2 {b1:.1f}%" if b1 is not None else "1进2 —",
        f"竞价 {ag:.1f}%" if ag is not None else None,
        f"换手 {at:.2f}%" if at is not None else None,
    ]
    reason = " · ".join(b for b in reason_bits if b)
    return {
        "action": "开仓",
        "position_pct": target_pct,
        "position_text": pos_text,
        "reason": reason,
        "can_open": True,
        "ladder_label": ladder_label,
        "veto_reason": None,
        "halved": halved,
    }


def _veto(ladder_label: str, reason: str, veto_code: str, halved: bool = False) -> dict:
    return {
        "action": "空仓",
        "position_pct": 0,
        "position_text": "0% (空仓)",
        "reason": reason,
        "can_open": False,
        "ladder_label": ladder_label,
        "veto_reason": veto_code,
        "halved": halved,
    }

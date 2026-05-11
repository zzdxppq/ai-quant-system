"""每只股票按梯队 + 竞价 + 环境的开仓决策（v3.3 — 在 v3.2 基础上加缩量换手板过滤）

核心认知（半年真实晋级率）
==========================
  · 1进2 16.2%（中枢约 16%，并非旧假设的 22%）
  · 2进3 31.7%（相对 1进2 翻倍，胜率较优）
  · 3进4 49.0%（接近一半，最佳接力梯队）
  · 4进5 39.9%

环境分级（按 1进2 成功率）
  · 极弱势 < 12%        → 仅高位 3进4+，仓位极小
  · 弱势   12% – 15%    → 接力困难
  · 常态   15% – 18%    → 历史中枢
  · 偏强   18% – 22%    → 机会增多
  · 极强   > 22%        → 重仓出击

2进3 (board=2) 开仓条件（全部满足）
  · 1进2 ≥ 12%（< 12% 不做 2进3）
  · 板块集中度 ≥ 30% 或 所属概念涨停 ≥ 3 只
  · 竞价换手 > 0.6%
  · v3.3 新增：二板非一字板时，需另外同时满足以下两条（避免缩量换手板）
      · 昨日换手率 ≥ 8%（针对 20-100 亿市值）
      · 昨日成交量 / 前日成交量 ≥ 1.2（温和放量 20%+，所有市值通用）

3进4+ (board ≥ 3) 开仓条件（全部满足）
  · 板块集中度 ≥ 25% 或 所属概念涨停 ≥ 2 只
  · 竞价换手 > 0.5%
  · 昨日最高板（空间板）今日不跌停（小跌可接受）

仓位档位（基于 1进2 成功率）
  >22%:    2进3=35%  3进4=35%  4进5+=30%
  18-22%:  25% / 30% / 25%
  15-18%:  15% / 25% / 20%
  12-15%:   5% / 15% / 10%
  <12%:     0% / 10% /  5%

跌停潮 ≥9 → 所有梯队仓位减半
"""
from __future__ import annotations
from typing import Optional


# (b1_min_inclusive, b1_max_exclusive, pos_2to3, pos_3to4, pos_4plus)
POSITION_TABLE = [
    (22.0, 1000.0, 35, 35, 30),  # 极强
    (18.0, 22.0,   25, 30, 25),  # 偏强
    (15.0, 18.0,   15, 25, 20),  # 常态
    (12.0, 15.0,    5, 15, 10),  # 弱势
    (0.0,  12.0,    0, 10,  5),  # 极弱势（2进3 强制 0）
]


def _lookup_positions(b1_rate: Optional[float]) -> tuple[int, int, int]:
    if b1_rate is None:
        return (0, 0, 0)
    for lo, hi, p1, p2, p3 in POSITION_TABLE:
        if lo <= b1_rate < hi:
            return (p1, p2, p3)
    return (0, 0, 0)


def _env_level(b1_rate: Optional[float]) -> str:
    if b1_rate is None:
        return "未知"
    if b1_rate < 12: return "极弱势"
    if b1_rate < 15: return "弱势"
    if b1_rate < 18: return "常态"
    if b1_rate < 22: return "偏强"
    return "极强"


def _concept_zt_max(hit: dict, concept_zt_stats: list[dict]) -> int:
    """该股 top_concepts 中涨停股最多的那个概念的涨停数"""
    if not concept_zt_stats:
        return 0
    top_concepts = list(hit.get("top_concepts") or [])
    if not top_concepts:
        return 0
    best = 0
    for c in concept_zt_stats:
        if c.get("name") in top_concepts:
            best = max(best, int(c.get("limit_up_count", 0) or 0))
    return best


def compute_per_stock_decision(
    hit: dict,
    market_env: dict,
    concept_zt_stats: Optional[list[dict]] = None,
    space_board_today: Optional[dict] = None,
    market_highest_board: Optional[int] = None,
) -> dict:
    """对单只 hit 按梯队规则出决策（v3.2）

    Args:
        hit: ScreenerHit asdict —— 含 continuous_limit_up / auction_gain /
             auction_turnover / auction_volume_ratio / top_concepts
        market_env: {b1_rate, concentration, market_limit_down, ...}
        concept_zt_stats: latest_review.concept_zt_stats（[{name, limit_up_count}]）
        space_board_today: latest_review.relay_env.prev_space_board_today
        market_highest_board: 保留兼容（v3.2 不再使用）

    Returns: {
        action, position_pct, position_text, reason, can_open,
        ladder_label, veto_reason, halved
    }
    """
    board = int(hit.get("continuous_limit_up", 0) or 0)
    ladder_label = f"{board}进{board + 1}" if board >= 1 else "首板"

    # ── 抽取数值 ──
    auction_turnover = hit.get("auction_turnover")
    try:
        at = float(auction_turnover) if auction_turnover is not None else None
    except (TypeError, ValueError):
        at = None

    b1_rate = market_env.get("b1_rate")
    try:
        b1 = float(b1_rate) if b1_rate is not None else None
    except (TypeError, ValueError):
        b1 = None

    concentration = market_env.get("concentration")
    try:
        conc = float(concentration) if concentration is not None else None
    except (TypeError, ValueError):
        conc = None

    limit_down = market_env.get("market_limit_down")
    try:
        ld = int(limit_down) if limit_down is not None else None
    except (TypeError, ValueError):
        ld = None

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

    # 表查得 0 → 直接空仓
    if target_pct <= 0:
        env_lbl = _env_level(b1)
        msg = (
            f"{env_lbl}（1进2 {b1:.1f}%）下 {ladder_label} 不开仓"
            if b1 is not None else "数据不足"
        )
        return _veto(ladder_label, msg, "table_zero", halved=halved)

    # ── 概念涨停热度（板块集中度的备选条件）──
    concept_zt_max = _concept_zt_max(hit, concept_zt_stats or [])

    # ── 竞价校验 ──
    fails: list[str] = []

    if board == 2:
        # 2进3：板块集中度≥30% OR 概念涨停≥3 + 换手>0.6
        sec_ok = (conc is not None and conc >= 30) or (concept_zt_max >= 3)
        if not sec_ok:
            fails.append(
                f"板块集中度 {conc:.1f}%<30% 且概念涨停≤2（{concept_zt_max}只）"
                if conc is not None
                else f"板块集中度未知，概念涨停 {concept_zt_max} 只 <3"
            )
        if at is None:
            fails.append("竞价换手缺失")
        elif at <= 0.6:
            fails.append(f"竞价换手 {at:.2f}% ≤ 0.6%")
        # ── 缩量换手板过滤（非一字板时附加 v3.3）──
        # 二板非一字板 → 必须满足：昨日换手 ≥ 8%（针对 20-100 亿市值）
        # 且 昨日成交量 / 前日成交量 ≥ 1.2（温和放量 20%+）
        prev_yizi = hit.get("prev_day_yizi")
        if prev_yizi is False:
            mc = hit.get("market_cap")
            try:
                mc_v = float(mc) if mc is not None else None
            except (TypeError, ValueError):
                mc_v = None
            prev_to = hit.get("prev_day_turnover")
            try:
                pt = float(prev_to) if prev_to is not None else None
            except (TypeError, ValueError):
                pt = None
            prev_vr = hit.get("prev_volume_ratio")
            try:
                pvr = float(prev_vr) if prev_vr is not None else None
            except (TypeError, ValueError):
                pvr = None
            # 换手率 8% 阈值：20-100 亿市值适用
            if mc_v is not None and 20 <= mc_v <= 100:
                if pt is None:
                    fails.append("昨日换手率缺失（20-100亿需 ≥ 8%）")
                elif pt < 8:
                    fails.append(f"昨日换手率 {pt:.2f}% < 8%（缩量换手板）")
            # 成交量比 1.2 阈值：所有市值通用
            if pvr is None:
                fails.append("昨日/前日量比缺失（需 ≥ 1.2）")
            elif pvr < 1.2:
                fails.append(f"昨日/前日量比 {pvr:.2f} < 1.2（未放量）")

    else:  # board >= 3
        # 3进4+：板块集中度≥25% OR 概念涨停≥2 + 换手>0.5 + 空间板不跌停
        sec_ok = (conc is not None and conc >= 25) or (concept_zt_max >= 2)
        if not sec_ok:
            fails.append(
                f"板块集中度 {conc:.1f}%<25% 且概念涨停≤1（{concept_zt_max}只）"
                if conc is not None
                else f"板块集中度未知，概念涨停 {concept_zt_max} 只 <2"
            )
        if at is None:
            fails.append("竞价换手缺失")
        elif at <= 0.5:
            fails.append(f"竞价换手 {at:.2f}% ≤ 0.5%")
        # 昨日空间板今日跌停校验
        if space_board_today:
            today_pct = space_board_today.get("today_pct")
            try:
                tp = float(today_pct) if today_pct is not None else None
            except (TypeError, ValueError):
                tp = None
            if tp is not None and tp <= -9.5:
                space_name = space_board_today.get("name", "空间板")
                fails.append(f"昨日空间板 {space_name} 今日跌停 ({tp:.1f}%)")

    if fails:
        return _veto(
            ladder_label, "条件未达：" + "；".join(fails),
            "auction_fail", halved=halved,
        )

    # ── 通过：开仓 ──
    pos_text = f"{target_pct}%" + ("（跌停潮减半）" if halved else "")
    env_lbl = _env_level(b1)
    reason_bits = [
        ladder_label,
        f"{env_lbl}（1进2 {b1:.1f}%）" if b1 is not None else env_lbl,
        f"换手 {at:.2f}%" if at is not None else None,
        f"集中度 {conc:.1f}%" if conc is not None else None,
        f"概念涨停 {concept_zt_max}只" if concept_zt_max else None,
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

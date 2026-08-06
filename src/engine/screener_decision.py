"""每只股票按梯队 + 竞价涨幅 + 环境的开仓决策（v4.0 — 离散仓位层）

v4.0 仓位层
====================
  · 2进3：重点参与 / 正常参与 → 3 层=30%；规则内轻仓试错 / 边缘票 → 2 层=20%
  · 2进3 单票未达标兜底 / 1进2 零票公式兜底 → 1 层=10%
  · 3进4 / 4进5+ 基础：3 层=30%；升仓 4 层=40%
  · 兼容读侧：旧 1.5 层=15% 文案仍可识别

升仓（3→4 层，满足任一组全部条件）
  · 4进5：竞价 5~6% 且晋级率 12~15%
  · 3进4：竞价 6~7.5% 且晋级率 ≥12%
  · 4进5：晋级率 12~15% 且板块集中度 ≥40%

全局：跌停潮≥9 仓位减半；晋级率>25% 仓位×0.7
禁止：主流程 1进2 不做；2进3 硬门槛未过不做（单票/零票兜底除外）；5板+ 竞价>6% 不做
"""
from __future__ import annotations

from typing import Optional

RULES_VERSION = "v4.0"

LAYER_1_PCT = 10  # 2进3 单票兜底 / 1进2 零票公式兜底
LAYER_2_PCT = 20  # 2进3 规则内「轻仓试错 / 边缘票」
LAYER_1_5_PCT = 15  # 兼容旧数据/读侧
LAYER_3_PCT = 30  # 正常参与 / 重点参与 / 3进4+ 基础
LAYER_4_PCT = 40

# 2进3 竞价涨幅窗口（含边界）— 硬门槛下限放宽至 4%
AUCTION_2TO3_MIN = 4.0
AUCTION_2TO3_MAX = 7.5
AUCTION_3TO4_MIN = 5.0
AUCTION_4PLUS_MIN = 4.0
AUCTION_4PLUS_MAX = 7.5
AUCTION_3TO4_SWEET_LO = 6.0
AUCTION_3TO4_SWEET_HI = 7.5
AUCTION_4PLUS_SWEET_LO = 5.0
AUCTION_4PLUS_SWEET_HI = 6.0
B1_SWEET_LO = 12.0
B1_SWEET_HI = 15.0
B1_2TO3_HARD_MIN = 8.0
# 2进3 额比：≥0.8 正常；0.74~0.8（差≤0.06）仅可作边缘票 2层
PAR_2TO3_HARD = 0.8
PAR_2TO3_EDGE = 0.74
HIGH_BOARD_MIN = 5
HIGH_BOARD_AUCTION_MAX = 6.0
CONC_UPGRADE_MIN = 40.0
CONC_2TO3_MIN = 30.0
CONCEPT_ZT_2TO3_MIN = 3


def _env_label_from_b1(b1_rate: Optional[float]) -> str:
    if b1_rate is None:
        return "未知"
    if b1_rate > 25:
        return "过热"
    if b1_rate >= 22:
        return "极强"
    if b1_rate >= 18:
        return "偏强"
    if b1_rate >= 15:
        return "常态"
    if b1_rate >= B1_SWEET_LO:
        return "最佳窗口"
    if b1_rate >= 8:
        return "弱势"
    return "极弱"


def _env_level(b1_rate: Optional[float]) -> str:
    lbl = _env_label_from_b1(b1_rate)
    if b1_rate is not None and b1_rate > 25:
        return f"{lbl}·过热"
    return lbl


def _layer_text(pct: float) -> str:
    if pct <= 0:
        return ""
    # 精确档位优先（含减半后的常见值）
    for base, label in (
        (LAYER_4_PCT, "4层"),
        (LAYER_3_PCT, "3层"),
        (LAYER_2_PCT, "2层"),
        (LAYER_1_5_PCT, "1.5层"),
        (LAYER_1_PCT, "1层"),
        (LAYER_4_PCT / 2, "2层"),
        (LAYER_3_PCT / 2, "1.5层"),
        (LAYER_2_PCT / 2, "1层"),
        (LAYER_1_PCT / 2, "0.5层"),
        (7.5, "0.75层"),
    ):
        if abs(pct - base) < 0.55:
            return label
    layers = round(pct / 10.0, 1)
    if abs(layers - round(layers)) < 0.05:
        return f"{int(round(layers))}层"
    return f"{layers:g}层"


def layers_from_position_pct(pct: float) -> str:
    """将 position_pct 转为邮件标题用的短层数文案（如 2层 / 1.5层）。"""
    if pct <= 0:
        return "0层"
    text = _layer_text(pct)
    return text if text else f"{round(pct / 10.0, 1):g}层"


def _qualifies_upgrade_to_4(
    board: int,
    ag: Optional[float],
    b1: Optional[float],
    conc: Optional[float],
) -> bool:
    """3 层 → 4 层（40%）：三组条件满足任一组全部。"""
    if board < 3 or ag is None or b1 is None:
        return False
    sweet_b1 = B1_SWEET_LO <= b1 < B1_SWEET_HI
    if board >= 4 and AUCTION_4PLUS_SWEET_LO <= ag <= AUCTION_4PLUS_SWEET_HI and sweet_b1:
        return True
    if board == 3 and AUCTION_3TO4_SWEET_LO <= ag <= AUCTION_3TO4_SWEET_HI and b1 >= B1_SWEET_LO:
        return True
    if board >= 4 and sweet_b1 and conc is not None and conc >= CONC_UPGRADE_MIN:
        return True
    return False


def _concept_zt_max(hit: dict, concept_zt_stats: list[dict]) -> int:
    if not concept_zt_stats:
        return 0
    top_concepts = list(hit.get("top_concepts") or [])
    if not top_concepts:
        return 0
    best = 0
    for c in concept_zt_stats:
        if c.get("name") in top_concepts:
            cnt = c.get("limit_up_count", c.get("count", 0))
            best = max(best, int(cnt or 0))
    return best


def _float_or_none(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _auction_gain_pct(hit: dict) -> Optional[float]:
    return _float_or_none(hit.get("auction_gain"))


def _apply_global_position_adjust(
    target_pct: float,
    *,
    b1: Optional[float],
    limit_down: Optional[int],
) -> tuple[float, bool, bool]:
    """跌停潮减半（层数折半）；晋级率过热 ×0.7。"""
    halved = False
    overheated = False
    p = float(target_pct)
    if limit_down is not None and limit_down >= 9 and p > 0:
        p = round(p / 2.0, 1)
        halved = True
    if b1 is not None and b1 > 25 and p > 0:
        p = round(p * 0.7, 1)
        overheated = True
    return p, halved, overheated


def _format_position_text(
    pct: float,
    *,
    upgraded: bool,
    halved: bool,
    overheated: bool,
    tier_tag: str = "",
) -> str:
    tags: list[str] = []
    lyr = _layer_text(pct)
    if lyr:
        tags.append(lyr)
    if tier_tag:
        tags.append(tier_tag)
    if upgraded:
        tags.append("升4层")
    if halved:
        tags.append("跌停潮减半")
    if overheated:
        tags.append("晋级率>25%×0.7")
    text = f"{pct:g}%"
    if tags:
        text += "（" + "·".join(tags) + "）"
    return text


def _space_board_gate_pct(
    space_board_today: Optional[dict],
    highest_board_tier_today: Optional[dict],
) -> tuple[Optional[float], str]:
    """3进4 空间板门槛：最高连板档多只取均竞价；单只仍用 prev_space_board。"""
    tier = highest_board_tier_today or {}
    cnt = int(tier.get("count") or 0)
    tp = _float_or_none(tier.get("avg_today_pct"))
    if tp is None:
        tp = _float_or_none(tier.get("today_pct"))
    if tp is not None:
        if cnt > 1:
            yb = tier.get("yesterday_board")
            label = f"昨日{yb}板高标均竞价" if yb else "昨日最高连板档均竞价"
            return tp, label
        if cnt == 1:
            nm = tier.get("name") or "空间板"
            return tp, f"昨日空间板 {nm}"
        yb = tier.get("yesterday_board")
        label = f"昨日{yb}板高标均竞价" if yb else "昨日最高连板档均竞价"
        return tp, label
    if space_board_today:
        tp = _float_or_none(space_board_today.get("today_open_pct"))
        if tp is None:
            tp = _float_or_none(space_board_today.get("today_pct"))
        nm = space_board_today.get("name", "空间板")
        return tp, f"昨日空间板 {nm}"
    return None, ""


def _resolve_space_red(
    market_env: dict,
    space_board_today: Optional[dict],
    highest_board_tier_today: Optional[dict],
) -> Optional[bool]:
    """高标红盘：pct>0 为红；显式 space_red 优先。None=未知。"""
    if "space_red" in market_env and market_env.get("space_red") is not None:
        return bool(market_env["space_red"])
    tp, _ = _space_board_gate_pct(space_board_today, highest_board_tier_today)
    if tp is not None:
        return tp > 0
    return None


def _sector_ok_2to3(conc: Optional[float], concept_zt_max: int) -> bool:
    return (conc is not None and conc >= CONC_2TO3_MIN) or (concept_zt_max >= CONCEPT_ZT_2TO3_MIN)


def _decide_2to3_tier(
    *,
    ag: Optional[float],
    at: Optional[float],
    pt: Optional[float],
    par: Optional[float],
    b1: Optional[float],
    space_red: Optional[bool],
    conc: Optional[float],
    concept_zt_max: int,
    skip_prev_vol: bool,
) -> tuple[Optional[float], str, list[str]]:
    """2进3 分档：返回 (target_pct|None, tier_tag, fails)。

    None + fails → 0 层；否则 1/2/3 层对应 pct。
    """
    fails: list[str] = []

    # ── 硬门槛（任一 → 0 层）──
    if ag is None:
        fails.append("竞价涨幅缺失")
    elif ag < AUCTION_2TO3_MIN or ag > AUCTION_2TO3_MAX:
        fails.append(f"竞价涨幅 {ag:.2f}% 不在 {AUCTION_2TO3_MIN}~{AUCTION_2TO3_MAX}%")

    if at is None:
        fails.append("竞价换手缺失")
    elif at < 0.3:
        fails.append(f"竞价换手 {at:.2f}% < 0.3%")

    if not skip_prev_vol:
        if pt is None:
            fails.append("昨日换手率缺失（需 ≥3%）")
        elif pt < 3:
            fails.append(f"昨日换手率 {pt:.2f}% < 3%")
        if par is None:
            fails.append(f"昨日/前日成交额比缺失（需 ≥{PAR_2TO3_EDGE}）")
        else:
            par = round(float(par), 2)
            if par < PAR_2TO3_EDGE:
                fails.append(
                    f"昨日/前日成交额比 {par:.2f} < {PAR_2TO3_EDGE}（边缘下限）"
                )

    if b1 is None:
        fails.append("昨日1进2晋级率缺失")
    elif b1 < B1_2TO3_HARD_MIN:
        fails.append(f"昨日1进2晋级率 {b1:.1f}% < {B1_2TO3_HARD_MIN:.0f}%")

    # 高标绿盘 且 晋级率 <12% → 双重弱势
    if space_red is False and b1 is not None and b1 < B1_SWEET_LO:
        fails.append(f"高标绿盘且晋级率 {b1:.1f}% < 12%（双重弱势）")

    if fails:
        return None, "", fails

    assert ag is not None and at is not None and b1 is not None
    # 一字豁免时，分档用的昨换手/额比按达标下限看待（不挡升档，也不虚抬）
    pt_e = pt if pt is not None else (5.0 if skip_prev_vol else 0.0)
    par_e = round(float(par), 2) if par is not None else (1.0 if skip_prev_vol else 0.0)
    red = space_red is True
    green = space_red is False
    sector_ok = _sector_ok_2to3(conc, concept_zt_max)
    sweet_b1 = B1_SWEET_LO <= b1 < B1_SWEET_HI
    # 额比 0.74~0.8：仅允许 1 层边缘票，不可升 2/3 层
    edge_par = (
        not skip_prev_vol
        and par is not None
        and PAR_2TO3_EDGE <= par_e < PAR_2TO3_HARD
    )

    # ── 3 层（重点参与）──
    if (
        not edge_par
        and 5.0 <= ag <= 7.5
        and at >= 0.5
        and pt_e >= 5
        and par_e >= 1.0
        and sweet_b1
        and red
        and sector_ok
    ):
        return float(LAYER_3_PCT), "重点参与", []

    # ── 2 层（正常参与）任一组合 ──
    combo2_a = (
        not edge_par
        and 5.0 <= ag <= 7.5 and at >= 0.4 and pt_e >= 4 and par_e >= 0.9 and sweet_b1
    )
    combo2_b = (
        not edge_par
        and 4.5 <= ag <= 7.5
        and at >= 0.5
        and pt_e >= 5
        and par_e >= 1.0
        and b1 >= B1_SWEET_LO
        and red
    )
    combo2_c = (
        not edge_par
        and 5.0 <= ag <= 7.5
        and at >= 0.5
        and pt_e >= 4
        and par_e >= 0.9
        and b1 >= B1_SWEET_HI
        and red
    )
    if combo2_a or combo2_b or combo2_c:
        return float(LAYER_3_PCT), "正常参与", []

    # ── 轻仓试错（2层）任一组合：额比需 ≥0.8 ──
    combo1_a = (
        not edge_par
        and 4.0 <= ag <= 7.5
        and at >= 0.3
        and pt_e >= 3
        and par_e >= PAR_2TO3_HARD
        and 8.0 <= b1 < B1_SWEET_LO
        and red
    )
    combo1_b = (
        not edge_par
        and 4.0 <= ag <= 7.5
        and at >= 0.3
        and pt_e >= 3
        and par_e >= PAR_2TO3_HARD
        and sweet_b1
        and green
    )
    combo1_c = (
        not edge_par
        and 4.5 <= ag <= 7.5
        and at >= 0.3
        and pt_e >= 3
        and par_e >= PAR_2TO3_HARD
        and b1 >= B1_SWEET_HI
        and red
    )
    if combo1_a or combo1_b or combo1_c:
        return float(LAYER_2_PCT), "轻仓试错", []

    # ── 边缘票 2层：额比仅差 ≤0.06，其余硬门槛已过 ──
    if edge_par:
        return float(LAYER_2_PCT), "边缘票", []

    fails.append("未达正常/重点/轻仓任一组合（边缘指标或高标信号不足）")
    return None, "", fails


def build_light_trial_decision(
    hit: dict,
    market_env: Optional[dict] = None,
    *,
    reason_note: str = "",
    tier_tag: str = "轻仓试错",
) -> dict:
    """强制轻仓试错 1层（仅 2进3 单票不达标兜底 / 1进2 零票公式兜底）。"""
    board = int(hit.get("continuous_limit_up", 0) or 0)
    ladder_label = f"{board}进{board + 1}" if board >= 1 else "首板"
    env = market_env or {}
    b1 = _float_or_none(env.get("b1_rate"))
    ld = env.get("market_limit_down")
    try:
        ld_i = int(ld) if ld is not None else None
    except (TypeError, ValueError):
        ld_i = None
    target_pct, halved, overheated = _apply_global_position_adjust(
        float(LAYER_1_PCT), b1=b1, limit_down=ld_i,
    )
    ag = _auction_gain_pct(hit)
    at = _float_or_none(hit.get("auction_turnover"))
    bits = [
        ladder_label,
        tier_tag,
        reason_note or None,
        f"{_env_label_from_b1(b1)}（1进2 {b1:.1f}%）" if b1 is not None else None,
        f"竞价 {ag:.2f}%" if ag is not None else None,
        f"换手 {at:.2f}%" if at is not None else None,
    ]
    return {
        "rules_version": RULES_VERSION,
        "ladder_label": ladder_label,
        "action": "开仓",
        "position_pct": target_pct,
        "position_text": _format_position_text(
            target_pct, upgraded=False, halved=halved, overheated=overheated,
            tier_tag=tier_tag,
        ),
        "reason": " · ".join(b for b in bits if b),
        "can_open": True,
        "veto_reason": None,
        "halved": halved,
        "upgraded_4layer": False,
        "tier_tag": tier_tag,
        "forced_light_trial": True,
    }


def _hit_board_count(hit: dict) -> int:
    try:
        return int(hit.get("continuous_limit_up") or 0)
    except (TypeError, ValueError):
        return 0


def apply_single_hit_light_trial(
    hits: list[dict],
    market_env: Optional[dict] = None,
) -> bool:
    """今日有且仅有 1 只「2进3」且策略不开仓 → 强制轻仓试错 1层。

    3进4+ 不强制改仓，保持原决策（含 0 仓）。
    """
    if len(hits) != 1:
        return False
    h = hits[0]
    if not isinstance(h, dict):
        return False
    if _hit_board_count(h) != 2:
        return False
    psd = h.get("per_stock_decision")
    if isinstance(psd, dict) and psd.get("can_open") is True:
        return False
    prev_reason = ""
    if isinstance(psd, dict):
        prev_reason = str(psd.get("reason") or "")
    note = "单票兜底"
    if prev_reason:
        note = "单票兜底（原条件未达）"
    h["per_stock_decision"] = build_light_trial_decision(
        h, market_env, reason_note=note, tier_tag="轻仓试错",
    )
    return True


def compute_per_stock_decision(
    hit: dict,
    market_env: dict,
    concept_zt_stats: Optional[list[dict]] = None,
    space_board_today: Optional[dict] = None,
    market_highest_board: Optional[int] = None,
    highest_board_tier_today: Optional[dict] = None,
) -> dict:
    """对单只 hit 按 v4.0 梯队规则出决策。"""
    board = int(hit.get("continuous_limit_up", 0) or 0)
    ladder_label = f"{board}进{board + 1}" if board >= 1 else "首板"

    at = _float_or_none(hit.get("auction_turnover"))
    b1 = _float_or_none(market_env.get("b1_rate"))
    conc = _float_or_none(market_env.get("concentration"))
    ld = market_env.get("market_limit_down")
    try:
        ld_i = int(ld) if ld is not None else None
    except (TypeError, ValueError):
        ld_i = None

    ag = _auction_gain_pct(hit)
    concept_zt_max = _concept_zt_max(hit, concept_zt_stats or [])
    env_lbl = _env_label_from_b1(b1)
    space_red = _resolve_space_red(
        market_env, space_board_today, highest_board_tier_today,
    )

    base = {
        "rules_version": RULES_VERSION,
        "ladder_label": ladder_label,
    }

    if board == 1:
        # 零命中后的 1进2 公式兜底（有且仅有一只）
        if hit.get("fallback_1to2"):
            return build_light_trial_decision(
                hit, market_env,
                reason_note="1进2公式兜底",
                tier_tag="轻仓试错",
            )
        return {
            **base,
            "action": "空仓",
            "position_pct": 0,
            "position_text": "0% (空仓)",
            "reason": "1进2 信号不做（v4.0 历史胜率偏低，仅做 2进3+；零命中时可公式兜底）",
            "can_open": False,
            "veto_reason": "no_1to2",
            "halved": False,
        }

    if board < 2:
        return {
            **base,
            "action": "观察",
            "position_pct": 0,
            "position_text": "0% (规则未覆盖)",
            "reason": "首板不在本梯队规则范围",
            "can_open": False,
            "veto_reason": None,
            "halved": False,
        }

    if board >= HIGH_BOARD_MIN and ag is not None and ag > HIGH_BOARD_AUCTION_MAX:
        return _veto(
            ladder_label,
            f"{HIGH_BOARD_MIN}板以上且竞价 {ag:.2f}% > {HIGH_BOARD_AUCTION_MAX:.0f}% 不做",
            "high_board_gate",
        )

    # ── 2进3：三档分位决策 ──
    if board == 2:
        skip_prev = hit.get("prev_day_yizi") is True
        pt = _float_or_none(hit.get("prev_day_turnover"))
        par = _float_or_none(hit.get("prev_amount_ratio"))
        target_pct, tier_tag, fails = _decide_2to3_tier(
            ag=ag, at=at, pt=pt, par=par, b1=b1,
            space_red=space_red, conc=conc, concept_zt_max=concept_zt_max,
            skip_prev_vol=skip_prev,
        )
        if target_pct is None:
            return _veto(
                ladder_label, "条件未达：" + "；".join(fails),
                "auction_fail",
            )
        target_pct, halved, overheated = _apply_global_position_adjust(
            target_pct, b1=b1, limit_down=ld_i,
        )
        pos_text = _format_position_text(
            target_pct, upgraded=False, halved=halved, overheated=overheated,
            tier_tag=tier_tag,
        )
        reason_bits = [
            ladder_label,
            tier_tag or None,
            f"{env_lbl}（1进2 {b1:.1f}%）" if b1 is not None else env_lbl,
            f"竞价 {ag:.2f}%" if ag is not None else None,
            f"换手 {at:.2f}%" if at is not None else None,
            "高标红盘" if space_red is True else ("高标绿盘" if space_red is False else None),
        ]
        return {
            **base,
            "action": "开仓",
            "position_pct": target_pct,
            "position_text": pos_text,
            "reason": " · ".join(b for b in reason_bits if b),
            "can_open": True,
            "veto_reason": None,
            "halved": halved,
            "upgraded_4layer": False,
            "tier_tag": tier_tag,
        }

    fails: list[str] = []

    if board == 3:
        if b1 is None:
            fails.append("昨日1进2晋级率缺失")
        elif b1 < 10:
            fails.append(f"昨日1进2晋级率 {b1:.1f}% < 10%（3进4不做）")
        if ag is None:
            fails.append("竞价涨幅缺失")
        elif ag < AUCTION_3TO4_MIN:
            fails.append(f"竞价涨幅 {ag:.2f}% < {AUCTION_3TO4_MIN}%")
        sec_ok = (conc is not None and conc >= 25) or (concept_zt_max >= 2)
        if not sec_ok:
            fails.append(
                f"板块集中度 {conc:.1f}%<25% 且概念涨停 {concept_zt_max} 只 <2"
                if conc is not None
                else f"板块集中度未知，概念涨停 {concept_zt_max} 只 <2"
            )
        if at is None:
            fails.append("竞价换手缺失")
        elif at <= 0.5:
            fails.append(f"竞价换手 {at:.2f}% ≤ 0.5%")
        tp, gate_label = _space_board_gate_pct(
            space_board_today, highest_board_tier_today,
        )
        if tp is not None and tp <= -9.5:
            fails.append(f"{gate_label} ({tp:.1f}%)")

    else:
        if b1 is None:
            fails.append("昨日1进2晋级率缺失")
        elif b1 < 8:
            fails.append(f"昨日1进2晋级率 {b1:.1f}% < 8%（4进5+不做）")
        if ag is None:
            fails.append("竞价涨幅缺失")
        elif ag < AUCTION_4PLUS_MIN or ag > AUCTION_4PLUS_MAX:
            fails.append(
                f"竞价涨幅 {ag:.2f}% 不在 {AUCTION_4PLUS_MIN}~{AUCTION_4PLUS_MAX}%"
            )
        sec_ok = (conc is not None and conc >= 25) or (concept_zt_max >= 2)
        if not sec_ok:
            fails.append(
                f"板块集中度 {conc:.1f}%<25% 且概念涨停 {concept_zt_max} 只 <2"
                if conc is not None
                else f"板块集中度未知，概念涨停 {concept_zt_max} 只 <2"
            )
        if at is None:
            fails.append("竞价换手缺失")
        elif at <= 0.5:
            fails.append(f"竞价换手 {at:.2f}% ≤ 0.5%")

    if fails:
        return _veto(
            ladder_label, "条件未达：" + "；".join(fails),
            "auction_fail",
        )

    upgraded = _qualifies_upgrade_to_4(board, ag, b1, conc)
    target_pct = float(LAYER_4_PCT if upgraded else LAYER_3_PCT)

    target_pct, halved, overheated = _apply_global_position_adjust(
        target_pct, b1=b1, limit_down=ld_i,
    )

    pos_text = _format_position_text(
        target_pct, upgraded=upgraded, halved=halved, overheated=overheated,
    )

    reason_bits = [
        ladder_label,
        f"{env_lbl}（1进2 {b1:.1f}%）" if b1 is not None else env_lbl,
        f"竞价 {ag:.2f}%" if ag is not None else None,
        f"换手 {at:.2f}%" if at is not None else None,
    ]
    reason = " · ".join(b for b in reason_bits if b)

    return {
        **base,
        "action": "开仓",
        "position_pct": target_pct,
        "position_text": pos_text,
        "reason": reason,
        "can_open": True,
        "veto_reason": None,
        "halved": halved,
        "upgraded_4layer": upgraded,
    }


def _veto(ladder_label: str, reason: str, veto_code: str, halved: bool = False) -> dict:
    return {
        "rules_version": RULES_VERSION,
        "action": "空仓",
        "position_pct": 0,
        "position_text": "0% (空仓)",
        "reason": reason,
        "can_open": False,
        "ladder_label": ladder_label,
        "veto_reason": veto_code,
        "halved": halved,
    }

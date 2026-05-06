"""当日主动攻击个股 + 市场攻击热度阶段评估

用户口径（个股 4 项核验）：
  1. 阳线实体：真阳线 close>open，上影线 < 实体 1/3
  2. 创新高：今日 high > 过去 3 日最高（最优：10 日新高）
  3. 分时均线：80% 时间在均线上方 — 因无分钟级数据，简化为"日 K 全程不破开盘价"
     （low >= open，盘中不破开盘价，间接反映多头主导）
  4. 无危险动作：
       涨停股 → 未炸板 (zbc==0) AND 封板时间 ≤ 14:30
       非涨停股 → 默认通过（无封板/炸板可言）
判定：(1) ∧ (2) ∧ ((3) ∨ (4)) → 当日主动攻击

市场层（top30 整体攻击热度 → 总仓位上限）：
  ≤5 只     冰点期       ≤20%
  6-10 只   启动/修复期  40-50%
  11-19 只  发酵期       60-80%（最佳窗口）
  ≥20 只   高潮/加速期  ≤40%（主动降仓）

三问诊断：
  Q1 翻倍股数量：≥5 只 → 攻击周期已至中后段
  Q2 高低切换：高位崩盘 + 低位（45-60%）承接 → 新一轮启动
  Q3 攻击数趋势：连续 3 日递减 → 退潮
"""
from __future__ import annotations
import json
from typing import Optional

import pandas as pd

from src.config import DATA_DIR


def evaluate_active_attack(
    today_ohlc: dict,
    prev_highs_3d: list[float],
    prev_highs_10d: list[float],
    zt_info: Optional[dict],
) -> dict:
    """单只股票主动攻击核验

    Args:
        today_ohlc: {open, high, low, close} 当日 K 线
        prev_highs_3d: 过去 3 个交易日 high 列表
        prev_highs_10d: 过去 10 个交易日 high 列表（含 3 日，可不传）
        zt_info: 涨停池条目 {lbc, lbt, zbc} 或 None

    Returns:
        {is_attack, score (0-2),
         cond1_yang/_detail, cond2_new_high/_is_10d_high/_detail,
         cond3_intraday/_detail, cond4_safe/_detail}
        当 today_ohlc 不可用时返回 None
    """
    try:
        op = float(today_ohlc.get("open") or 0)
        hi = float(today_ohlc.get("high") or 0)
        lo = float(today_ohlc.get("low") or 0)
        cl = float(today_ohlc.get("close") or 0)
    except (TypeError, ValueError):
        return None
    if op <= 0 or hi <= 0 or cl <= 0:
        return None

    # === Cond 1: 阳线实体 + 上影线 < 1/3 实体 ===
    body = cl - op
    if body > 0:
        upper_shadow = max(0.0, hi - cl)
        ratio = upper_shadow / max(body, 0.001)
        cond1 = ratio < 1 / 3
        cond1_detail = f"实体{body:.2f} 上影{upper_shadow:.2f}({ratio:.0%})"
    else:
        cond1 = False
        cond1_detail = f"非阳线 (close{cl:.2f} ≤ open{op:.2f})"

    # === Cond 2: 创新高 ===
    p3 = max((float(x) for x in prev_highs_3d if x), default=0.0)
    p10 = max((float(x) for x in (prev_highs_10d or prev_highs_3d) if x), default=0.0)
    cond2 = hi > p3 if p3 > 0 else False
    cond2_10d = hi > p10 if p10 > 0 else False
    cond2_detail = f"今高{hi:.2f} vs 3日高{p3:.2f}"
    if cond2_10d:
        cond2_detail += "（10日新高）"

    # === Cond 3: 分时均线（简化：low ≥ open 表示日内不破开盘）===
    cond3 = (lo >= op) if lo > 0 else False
    cond3_detail = (
        f"日内不破开盘 (low{lo:.2f}≥open{op:.2f})"
        if cond3
        else f"日内破开盘 (low{lo:.2f}<open{op:.2f})"
    )

    # === Cond 4: 无危险动作 ===
    if zt_info:
        zbc = int(zt_info.get("zbc", 0) or 0)
        lbt = str(zt_info.get("lbt") or "")
        no_zhaban = (zbc == 0)
        no_late = bool(lbt) and lbt < "14:30:00"
        cond4 = no_zhaban and no_late
        cond4_detail = (
            f"涨停 炸板{zbc}次 封板{lbt or '-'}"
            if not (no_zhaban and no_late)
            else f"涨停 早封板{lbt}"
        )
    else:
        cond4 = True  # 非涨停股无封板/炸板，默认通过
        cond4_detail = "非涨停"

    is_attack = cond1 and cond2 and (cond3 or cond4)
    score = (1 if cond3 else 0) + (1 if cond4 else 0)

    return {
        "is_attack": bool(is_attack),
        "score": score,
        "cond1_yang": bool(cond1), "cond1_detail": cond1_detail,
        "cond2_new_high": bool(cond2), "cond2_is_10d_high": bool(cond2_10d),
        "cond2_detail": cond2_detail,
        "cond3_intraday": bool(cond3), "cond3_detail": cond3_detail,
        "cond4_safe": bool(cond4), "cond4_detail": cond4_detail,
    }


# ── 市场层聚合 ────────────────────────────────────────────────

_HISTORY_FILE = DATA_DIR / "active_attack_history.json"


def _load_history() -> list[dict]:
    if not _HISTORY_FILE.exists():
        return []
    try:
        return json.loads(_HISTORY_FILE.read_text())
    except Exception:
        return []


def _append_history(date_str: str, attack_count: int) -> list[dict]:
    """每日 attack_count 落盘，用于"连续递减"趋势判断"""
    hist = _load_history()
    hist = [h for h in hist if h.get("date") != date_str]
    hist.append({"date": date_str, "attack_count": int(attack_count)})
    hist = sorted(hist, key=lambda h: h["date"])[-90:]
    try:
        _HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=2))
    except Exception:
        pass
    return hist


def aggregate_market_attack_phase(
    ranking_records: list[dict],
    date_str: str = "",
) -> dict:
    """从 top30 ranking_records 聚合攻击热度 + 周期阶段 + 三问诊断

    要求每条 ranking record 包含 active_attack 子对象（由 evaluate_active_attack
    生成）以及 gain_10d / change_pct / continuous_limit_up 字段。
    """
    attack_codes: list[str] = []
    doubler_codes: list[str] = []
    for r in ranking_records:
        aa = r.get("active_attack") or {}
        if aa.get("is_attack"):
            attack_codes.append(str(r.get("code", "")))
        if float(r.get("gain_10d") or 0) >= 100:
            doubler_codes.append(str(r.get("code", "")))

    n_attack = len(attack_codes)
    n_doubler = len(doubler_codes)

    if n_attack <= 5:
        phase, cap_pct, note = "冰点期", 20, "市场做多稀缺，等待启动信号"
    elif n_attack <= 10:
        phase, cap_pct, note = "启动/修复期", 50, "分批试探性加仓，优先低位首板"
    elif n_attack <= 19:
        phase, cap_pct, note = "发酵期", 80, "本策略最佳操作窗口期，加重仓位"
    else:
        phase, cap_pct, note = "高潮/加速期", 40, "高潮之后往往是退潮，分批兑现利润"

    # === 三问诊断 ===
    # Q1: 翻倍股数量
    if n_doubler >= 5:
        q1 = f"⚠️ 翻倍股 {n_doubler} 只，攻击周期已至中后段，溢价空间收窄"
    elif n_doubler >= 2:
        q1 = f"翻倍股 {n_doubler} 只，攻击周期处于中段"
    else:
        q1 = f"翻倍股 {n_doubler} 只，空间充足"

    # Q2: 高低切换 (高位 >=80% gain 今日跌 -3% / 低位 45-60% gain 今日涨 >=5%)
    high_drop = sum(
        1 for r in ranking_records
        if float(r.get("gain_10d") or 0) >= 80
        and float(r.get("change_pct") or 0) <= -3
    )
    low_rise = sum(
        1 for r in ranking_records
        if 45 <= float(r.get("gain_10d") or 0) < 60
        and float(r.get("change_pct") or 0) >= 5
    )
    if high_drop >= 2 and low_rise >= 3:
        q2 = f"⚡ 高低切换：高位 {high_drop} 只崩盘 + 低位 {low_rise} 只承接，新一轮启动信号；聚焦 50% 涨幅附近新标的"
    elif high_drop >= 2:
        q2 = f"高位 {high_drop} 只崩盘，但低位承接不足（{low_rise} 只 ≥5%），谨慎"
    elif low_rise >= 3:
        q2 = f"低位 {low_rise} 只放量启动，但高位未瓦解，仍可参与高位接力"
    else:
        q2 = "无明显高低切换"

    # Q3: 攻击数趋势（需历史）
    today_str = date_str or ""
    if today_str:
        hist = _append_history(today_str, n_attack)
    else:
        hist = _load_history()
    counts = [int(h.get("attack_count", 0) or 0) for h in hist[-3:]]
    if len(counts) >= 3 and counts[-1] < counts[-2] < counts[-3]:
        q3 = f"⚠️ 主动攻击数连续 3 日递减（{counts[-3]}→{counts[-2]}→{counts[-1]}），退潮信号，主动降仓"
    elif len(counts) >= 2:
        delta = counts[-1] - counts[-2]
        sign = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
        q3 = f"主动攻击数 {counts[-2]}→{counts[-1]}（{sign}{abs(delta)}），趋势{'升温' if delta > 0 else ('降温' if delta < 0 else '持平')}"
    else:
        q3 = "无趋势历史数据"

    return {
        "attack_count": n_attack,
        "attack_codes": attack_codes,
        "doubler_count": n_doubler,
        "doubler_codes": doubler_codes,
        "phase": phase,
        "position_cap_pct": cap_pct,
        "phase_note": note,
        "diagnostics": {
            "doubler": q1,
            "switch": q2,
            "trend": q3,
        },
        "history_window": counts,
    }

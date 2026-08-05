"""昨日选股标的 — 次日集合竞价卖出建议（9:27 邮件推送后回填）。"""
from __future__ import annotations

from typing import Any, Optional

from src.config import now_cn


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ladder_label(board: int) -> str:
    b = int(board or 0)
    if b <= 1:
        return "首板"
    if b == 2:
        return "2进3"
    if b == 3:
        return "3进4"
    return "4进5+"


def classify_yesterday_close(record: dict) -> str:
    """涨停 / 炸板 / 收正 / 收负。"""
    if record.get("is_limit_up"):
        return "limit_up"
    if record.get("is_zhaban"):
        return "zhaban"
    dc = _f(record.get("day_change"))
    if dc is not None and dc > 0:
        return "positive"
    return "negative"


_YSTATUS = {
    "limit_up": "昨涨停",
    "zhaban": "昨炸板",
    "positive": "昨收正",
    "negative": "昨收负",
}


def _is_today_one_word_limit(auction_pct: float) -> bool:
    return auction_pct >= 9.8


def _is_today_auction_limit_down(auction_pct: float) -> bool:
    return auction_pct <= -9.5


def _pack(
    ylabel: str,
    pct: float,
    action: str,
    reason: str,
    tone: str,
    *,
    ladder: str = "",
) -> dict:
    sign = "+" if pct >= 0 else ""
    pct_s = f"{sign}{pct:.1f}%"
    summary = f"{ylabel} 今竞价{pct_s} {action} {reason}"
    out: dict[str, Any] = {
        "yesterday_status": ylabel,
        "today_auction_pct": round(pct, 2),
        "action": action,
        "reason": reason,
        "tone": tone,
        "summary": summary,
    }
    if ladder:
        out["ladder_label"] = ladder
    return out


def compute_next_day_sell_advice(
    record: dict,
    *,
    market_limit_down: Optional[int] = None,
    b1_rate: Optional[float] = None,
    today_auction_pct: Optional[float] = None,
) -> Optional[dict]:
    """按次日卖出规则生成建议；缺次日竞价% 时返回 None。"""
    pct = today_auction_pct
    if pct is None:
        pct = _f(record.get("next_day_auction_gain"))
    if pct is None:
        return None

    yst = classify_yesterday_close(record)
    ylabel = _YSTATUS[yst]
    ladder = _ladder_label(int(record.get("continuous_limit_up") or 0))

    ld = market_limit_down
    if ld is not None and int(ld) >= 10:
        return _pack(ylabel, pct, "竞价卖出", "全市场跌停≥10家", "sell", ladder=ladder)

    b1 = _f(b1_rate if b1_rate is not None else record.get("b1_rate"))
    weak_emotion = b1 is not None and b1 < 10.0

    if yst == "limit_up" and _is_today_one_word_limit(pct):
        return _pack(ylabel, pct, "继续持有", "昨涨停今一字板", "hold", ladder=ladder)
    if yst == "zhaban" and _is_today_auction_limit_down(pct):
        return _pack(ylabel, pct, "挂跌停卖出", "昨炸板今竞价跌停", "sell", ladder=ladder)

    if pct <= -2.5:
        advice = _pack(ylabel, pct, "开盘卖出", "竞价≤-2.5%硬止损", "sell", ladder=ladder)
    elif pct >= 9.0:
        advice = _pack(ylabel, pct, "开盘卖出", "竞价≥9%获利了结", "sell", ladder=ladder)
    elif yst == "limit_up":
        if pct > 7.0:
            advice = _pack(ylabel, pct, "部分止盈", "高开过多卖一半", "partial", ladder=ladder)
        elif pct >= 3.0:
            advice = _pack(ylabel, pct, "持有并设回撤止盈", "博弈连板，回落3%卖", "hold", ladder=ladder)
        else:
            advice = _pack(ylabel, pct, "观察5分钟", "竞价偏弱，不翻红则卖", "sell", ladder=ladder)
    elif yst == "zhaban":
        turnover = _f(record.get("auction_turnover"))
        if turnover is not None and turnover > 20:
            advice = _pack(ylabel, pct, "竞价卖出", "炸板日放量>20%", "sell", ladder=ladder)
        else:
            advice = _pack(ylabel, pct, "开盘卖出", "炸板股历史低开概率高", "sell", ladder=ladder)
    elif yst == "positive":
        if pct >= 2.0:
            advice = _pack(ylabel, pct, "观察5分钟", "高开观察能否延续", "hold", ladder=ladder)
        elif pct >= 0:
            advice = _pack(ylabel, pct, "观察5分钟", "平开区，5分钟不翻红则卖", "sell", ladder=ladder)
        else:
            advice = _pack(ylabel, pct, "开盘卖出", "竞价转弱", "sell", ladder=ladder)
    elif pct >= 1.0:
        advice = _pack(ylabel, pct, "观察5分钟", "弱转强观察，不能快速翻红则卖", "hold", ladder=ladder)
    else:
        advice = _pack(ylabel, pct, "开盘卖出", "避免小亏变大亏", "sell", ladder=ladder)

    if weak_emotion and advice.get("tone") != "sell":
        dc = _f(record.get("day_change"))
        profitable = dc is not None and dc > 0
        if profitable and advice.get("tone") in ("hold", "partial"):
            advice = _pack(
                ylabel, pct, "部分止盈", "1进2<10%情绪弱，盈利单减半", "partial", ladder=ladder,
            )
        elif not profitable:
            advice = _pack(
                ylabel, pct, "开盘卖出", "1进2<10%情绪弱，亏损单立卖", "sell", ladder=ladder,
            )
    return advice


def hydrate_yesterday_sell_advice_on_store(
    *,
    market_limit_down: Optional[int] = None,
    b1_rate: Optional[float] = None,
    pick_date: str | None = None,
) -> int:
    """读库后为昨日选股日补全缺失的 next_day_sell_advice 并写回（看板 light 轮询用）。"""
    from src.engine.screener_history import _load, _record_date_str, _save, yesterday_pick_date

    today = now_cn().strftime("%Y-%m-%d")
    target_date = str(pick_date or yesterday_pick_date(today) or "")[:10]
    if not target_date:
        return 0

    records = _load()
    updated = 0
    for r in records:
        if _record_date_str(r) != target_date:
            continue
        if r.get("next_day_auction_gain") is None:
            continue
        existing = r.get("next_day_sell_advice")
        if isinstance(existing, dict) and existing.get("summary"):
            continue
        adv = compute_next_day_sell_advice(
            r,
            market_limit_down=market_limit_down,
            b1_rate=b1_rate,
        )
        if not adv:
            continue
        r["next_day_sell_advice"] = adv
        updated += 1
    if updated:
        _save(records)
        print(f"[次日卖出建议] 读库补全 {updated} 只（选股日 {target_date}）")
    return updated


def backfill_next_day_sell_advice(
    *,
    market_limit_down: Optional[int] = None,
    b1_rate: Optional[float] = None,
    pick_date: str | None = None,
) -> int:
    """为「昨日选股日」且已有次日竞价% 的记录写入 next_day_sell_advice（看板昨日选股表同源）。"""
    from src.engine.screener_history import _load, _record_date_str, _save, yesterday_pick_date

    today = now_cn().strftime("%Y-%m-%d")
    target_date = str(pick_date or yesterday_pick_date(today) or "")[:10]
    if not target_date:
        return 0

    records = _load()
    updated = 0
    for r in records:
        rec_date = _record_date_str(r)
        if rec_date != target_date:
            continue
        if r.get("next_day_auction_gain") is None:
            continue
        adv = compute_next_day_sell_advice(
            r,
            market_limit_down=market_limit_down,
            b1_rate=b1_rate,
        )
        if not adv:
            continue
        if r.get("next_day_sell_advice") != adv:
            r["next_day_sell_advice"] = adv
            updated += 1
    if updated:
        _save(records)
        print(f"[次日卖出建议] 已写入 {updated} 只（选股日 {target_date}）")
    return updated

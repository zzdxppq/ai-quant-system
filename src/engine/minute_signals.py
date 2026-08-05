"""分时做 T 买卖点（通达信 7 格坐标 · 仅价线+均线）

Y 轴：昨收居中，涨停幅约 ±7 格（14 格 × 1.4% ≈ ±9.8%）。

做 T 买点：价在均线下方 ≥4 格 + 15 分钟内放量急跌 + 自低点反弹。
做 T 卖点：价在均线上方 ≥6 格 + 缩量拉升（价升量缩）。
"""
from __future__ import annotations

import re
from typing import Any

# 通达信分时主图：上下各 7 格，每格约 1.4%
GRID_PCT = 1.4
GRID_HALF = 7
BUY_GRIDS_BELOW_MA = 4
SELL_GRIDS_ABOVE_MA = 6
WINDOW_MINUTES = 15
MIN_BARS_BETWEEN = 5
WARMUP_BARS = 3

VOL_SPIKE_RATIO = 1.75
SHARP_DROP_BAR_PCT = -0.35
REBOUND_BAR_PCT = 0.12
SELL_RISE_BARS = 4
SELL_VOL_SHRINK_RATIO = 0.72


def _pct_from_pre(price: float, pre_close: float) -> float:
    if pre_close <= 0 or price <= 0:
        return 0.0
    return (price / pre_close - 1.0) * 100.0


def _hm_to_mins(t: str) -> int | None:
    s = str(t or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})\s*:\s*(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h * 60 + mi
    if re.match(r"^\d{3,4}$", s):
        pad = s if len(s) == 4 else "0" + s
        return int(pad[:2]) * 60 + int(pad[2:4])
    return None


def grid_gap_vs_avg(price: float, avg: float, pre_close: float) -> float:
    """现价相对均线偏离（格数，正=价在均线上方）。"""
    if pre_close <= 0 or price <= 0 or avg <= 0:
        return 0.0
    gap_pct = ((price - avg) / pre_close) * 100.0
    return gap_pct / GRID_PCT


def price_at_grid(pre_close: float, grid: float) -> float:
    return pre_close * (1.0 + grid * GRID_PCT / 100.0)


def _window_indices(bars: list[dict[str, Any]], end_idx: int, minutes: int) -> list[int]:
    end_m = _hm_to_mins(str(bars[end_idx].get("t") or ""))
    if end_m is None:
        lo = max(0, end_idx - minutes + 1)
        return list(range(lo, end_idx + 1))
    out: list[int] = []
    for j in range(end_idx, -1, -1):
        mj = _hm_to_mins(str(bars[j].get("t") or ""))
        if mj is None:
            if end_idx - j <= minutes:
                out.append(j)
            continue
        if end_m - mj <= minutes:
            out.append(j)
        else:
            break
    out.reverse()
    return out if out else [end_idx]


def _avg_vol(bars: list[dict[str, Any]], idx: int, lookback: int = 10) -> float:
    s = 0.0
    n = 0
    for j in range(max(0, idx - lookback), idx):
        v = float(bars[j].get("vol_bar") or 0)
        if v > 0:
            s += v
            n += 1
    return s / n if n else float(bars[idx].get("vol_bar") or 0)


def _detect_t_buy(
    bars: list[dict[str, Any]],
    idx: int,
    *,
    pre: float,
) -> bool:
    b1 = bars[idx]
    p1 = float(b1.get("p") or 0)
    a1 = float(b1.get("avg") or p1)
    if p1 <= 0 or a1 <= 0:
        return False
    if grid_gap_vs_avg(p1, a1, pre) > -BUY_GRIDS_BELOW_MA:
        return False

    win = _window_indices(bars, idx, WINDOW_MINUTES)
    if len(win) < 3:
        return False

    had_spike_drop = False
    dip_price = p1
    dip_idx = idx

    for k in range(1, len(win)):
        j = win[k]
        j0 = win[k - 1]
        p0 = float(bars[j0].get("p") or 0)
        p = float(bars[j].get("p") or 0)
        if p <= 0 or p0 <= 0:
            continue
        v = float(bars[j].get("vol_bar") or 0)
        v_ma = _avg_vol(bars, j, 8)
        bar_chg = (p / p0 - 1.0) * 100.0
        if v > max(v_ma * VOL_SPIKE_RATIO, 1e-6) and bar_chg <= SHARP_DROP_BAR_PCT:
            had_spike_drop = True
            if p < dip_price:
                dip_price = p
                dip_idx = j

    if not had_spike_drop:
        return False
    if p1 <= dip_price * (1.0 + REBOUND_BAR_PCT / 100.0) and idx <= dip_idx:
        return False
    if p1 > dip_price and (p1 / dip_price - 1.0) * 100.0 >= REBOUND_BAR_PCT:
        return True
    return p1 > float(bars[dip_idx].get("p") or 0) and idx > dip_idx


def _detect_t_sell(
    bars: list[dict[str, Any]],
    idx: int,
    *,
    pre: float,
) -> bool:
    b1 = bars[idx]
    p1 = float(b1.get("p") or 0)
    a1 = float(b1.get("avg") or p1)
    if p1 <= 0 or a1 <= 0:
        return False
    if grid_gap_vs_avg(p1, a1, pre) < SELL_GRIDS_ABOVE_MA:
        return False

    n = min(SELL_RISE_BARS, idx)
    if n < 2:
        return False
    prices: list[float] = []
    vols: list[float] = []
    for j in range(idx - n, idx + 1):
        p = float(bars[j].get("p") or 0)
        v = float(bars[j].get("vol_bar") or 0)
        if p > 0:
            prices.append(p)
            vols.append(v)
    if len(prices) < 3:
        return False
    rising = prices[-1] > prices[0] and prices[-1] >= prices[-2]
    if not rising:
        return False
    pos_vols = [v for v in vols if v > 0]
    if len(pos_vols) < 3:
        return False
    early = sum(pos_vols[: max(1, len(pos_vols) // 2)]) / max(1, len(pos_vols) // 2)
    late = sum(pos_vols[len(pos_vols) // 2 :]) / max(1, len(pos_vols) - len(pos_vols) // 2)
    if early <= 0:
        return False
    return late <= early * SELL_VOL_SHRINK_RATIO and pos_vols[-1] <= pos_vols[-2] * 1.05


def compute_minute_signals(
    bars: list[dict[str, Any]],
    *,
    pre_close: float = 0.0,
) -> list[dict[str, Any]]:
    """从分时 bar 计算做 T 买卖点（扫描至最后一根，便于实时刷新标注）。"""
    if not bars or len(bars) < WARMUP_BARS + 2:
        return []

    pre = float(pre_close or 0)
    if pre <= 0:
        return []

    signals: list[dict[str, Any]] = []
    last_buy = -999
    last_sell = -999

    for i in range(WARMUP_BARS, len(bars)):
        b1 = bars[i]
        p1 = float(b1.get("p") or 0)
        if p1 <= 0:
            continue
        t1 = str(b1.get("t") or "")
        pct1 = float(b1.get("pct")) if b1.get("pct") is not None else _pct_from_pre(p1, pre)

        if _detect_t_buy(bars, i, pre=pre) and i - last_buy >= MIN_BARS_BETWEEN:
            signals.append({
                "t": t1,
                "kind": "t_buy",
                "label": "做T买",
                "price": round(p1, 4),
                "pct": round(pct1, 3),
                "bar_idx": i,
            })
            last_buy = i

        if _detect_t_sell(bars, i, pre=pre) and i - last_sell >= MIN_BARS_BETWEEN:
            signals.append({
                "t": t1,
                "kind": "t_sell",
                "label": "做T卖",
                "price": round(p1, 4),
                "pct": round(pct1, 3),
                "bar_idx": i,
            })
            last_sell = i

    return signals


SIGNAL_LEGEND: list[dict[str, Any]] = [
    {"kind": "t_buy", "label": "做T买", "color": "#ef4444", "group": "t", "default_on": True},
    {"kind": "t_sell", "label": "做T卖", "color": "#22c55e", "group": "t", "default_on": True},
]

SIGNAL_GROUPS: list[dict[str, Any]] = [
    {"id": "t", "label": "做T", "default_on": True},
]


def default_signal_filter() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for item in SIGNAL_LEGEND:
        out[str(item["kind"])] = bool(item.get("default_on", True))
    return out

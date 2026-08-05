"""历史选股信号回测统计（已归档记录，不重跑选股引擎）。

默认交易规则：信号日收盘价买入 → 次日收盘价卖出。
收益% = (次日收盘 / 信号日收盘 - 1) * 100。

口径（v4.1）：
- 样本数 = 有可结算收益的选股信号（含策略空仓）
- 交易次数 = 策略 can_open=True 的信号
- 胜率 / 均收益 / 累计收益 / 回撤 仅基于交易
- 累计收益：按「真实建议仓位」加权（position_pct/100 × 单笔收益），按交易日复利
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from src.data.analytics_store import load_screener_history_entries

_cached_report: dict[str, Any] | None = None
_cached_date: str | None = None

BOARD_CHART_ORDER = ("2进3", "3进4", "4进5", "5板以上")
B1_CHART_ORDER = ("<12%", "12~15%", "≥15%")
AUCTION_CHART_ORDER = ("4~5%", "5~6%", "6~7.5%")

# 兼容旧常量（测试/外部引用）；新口径不再固定 1/3
POSITION_LAYERS = 3
MAX_SLOTS_PER_DAY = 3


def invalidate_screener_backtest_cache() -> None:
    """收盘回填或周度重算后调用，强制下次请求重建报告。"""
    global _cached_report, _cached_date
    _cached_report = None
    _cached_date = None


def _norm_date(s: str | None) -> str:
    d = str(s or "")[:10]
    return d if len(d) == 10 else ""


def _record_can_open(r: dict[str, Any]) -> bool:
    """策略是否开仓（交易计入用）。"""
    dec = r.get("decision") if isinstance(r.get("decision"), dict) else None
    if not dec:
        return False
    if dec.get("can_open") is True:
        return True
    if dec.get("can_open") is False:
        return False
    try:
        return float(dec.get("position_pct") or 0) > 0
    except (TypeError, ValueError):
        return False


def _record_position_weight(r: dict[str, Any]) -> float:
    """建议仓位占总资金比例：position_pct=30 → 0.3。未开仓为 0。"""
    if not _record_can_open(r):
        return 0.0
    dec = r.get("decision") if isinstance(r.get("decision"), dict) else {}
    raw = dec.get("position_pct") if isinstance(dec, dict) else None
    try:
        p = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0:
        return 0.0
    return round(p / 100.0, 6)


def _entry_close_price(r: dict[str, Any]) -> Optional[float]:
    """信号日收盘价：优先 close_price，否则昨收×(1+收盘涨幅 day_change%)。"""
    try:
        cp = float(r.get("close_price")) if r.get("close_price") is not None else 0.0
    except (TypeError, ValueError):
        cp = 0.0
    if cp > 0:
        return cp
    try:
        pre = float(r.get("pre_close")) if r.get("pre_close") is not None else 0.0
        dc = float(r.get("day_change")) if r.get("day_change") is not None else None
    except (TypeError, ValueError):
        return None
    if pre > 0 and dc is not None:
        return round(pre * (1 + dc / 100), 4)
    return None


def _exit_next_close_price(r: dict[str, Any], entry_close: float) -> Optional[float]:
    """次日收盘价：信号日收盘 × (1 + 次日收盘涨幅%)。"""
    if entry_close <= 0:
        return None
    ndcg = r.get("next_day_close_gain")
    if ndcg is None:
        return None
    try:
        g = float(ndcg)
    except (TypeError, ValueError):
        return None
    return round(entry_close * (1 + g / 100), 4)


def _trade_return_pct(r: dict[str, Any]) -> Optional[float]:
    """收盘买 → 次日收盘卖的单笔收益率(%)。"""
    entry = _entry_close_price(r)
    if entry is None or entry <= 0:
        return None
    nxt = _exit_next_close_price(r, entry)
    if nxt is None or nxt <= 0:
        return None
    return round((nxt / entry - 1) * 100, 4)


def _auction_bucket(ag: Optional[float]) -> str:
    """竞价涨幅分桶：4~5%（含4%，不含5%）、5~6%、6~7.5%（含6%，含7.5%）。"""
    if ag is None:
        return "未知"
    if ag < 4:
        return "<4%"
    if ag < 5:
        return "4~5%"
    if ag < 6:
        return "5~6%"
    if ag <= 7.5:
        return "6~7.5%"
    return ">7.5%"


def _board_chart_label(r: dict[str, Any]) -> Optional[str]:
    n = int(r.get("continuous_limit_up") or 0)
    if n <= 0 and r.get("board_label"):
        bl = str(r.get("board_label") or "")
        if "进" in bl:
            try:
                n = int(bl.split("进")[0])
            except (TypeError, ValueError):
                n = 0
    if n == 2:
        return "2进3"
    if n == 3:
        return "3进4"
    if n == 4:
        return "4进5"
    if n >= 5:
        return "5板以上"
    return None


def _b1_chart_tier(b1: Optional[float]) -> str:
    if b1 is None:
        return "未知"
    if b1 < 12:
        return "<12%"
    if b1 < 15:
        return "12~15%"
    return "≥15%"


def _float_b1(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _record_b1_rate(record: dict[str, Any]) -> Optional[float]:
    """选股记录归档时的 b1_rate，即该笔对应的「昨日1进2晋级率」%（非复盘当日晋级率）。"""
    return _float_b1(record.get("b1_rate"))


def _build_trade_date_b1_from_records(scoped: list[dict]) -> dict[str, float]:
    """同一交易日环境一致：从当日任一有效记录的 b1_rate 补齐同组缺失项。"""
    out: dict[str, float] = {}
    for r in scoped:
        d = _norm_date(r.get("date"))
        v = _record_b1_rate(r)
        if d and v is not None and d not in out:
            out[d] = v
    return out


def _return_stat_block(
    subset: list[dict],
    *,
    return_key: str = "_ret",
    sample_count: int | None = None,
) -> dict[str, Any]:
    """基于次日收盘收益(_ret)的统计块；subset 应为可开仓交易。

    sample_count: 同范围样本数（含空仓信号）；缺省则等于 trades。
    """
    rets = [float(r[return_key]) for r in subset if r.get(return_key) is not None]
    total = len(rets)
    samples = int(sample_count) if sample_count is not None else total
    if total == 0:
        return {
            "trades": 0,
            "wins": 0,
            "win_rate": 0,
            "avg_return": None,
            "avg_win": None,
            "avg_loss": None,
            "profit_loss_ratio": None,
            "return_trades": 0,
            "return_win_rate": None,
            "sample_count": samples,
            "trade_sample_ratio": 0.0 if samples else None,
        }
    win_rets = [x for x in rets if x > 0]
    lose_rets = [x for x in rets if x <= 0]
    avg_ret = round(sum(rets) / len(rets), 2)
    avg_win = round(sum(win_rets) / len(win_rets), 2) if win_rets else None
    avg_loss = round(sum(lose_rets) / len(lose_rets), 2) if lose_rets else None
    ratio = round(total / samples * 100, 1) if samples > 0 else None
    return {
        "trades": total,
        "wins": len(win_rets),
        "win_rate": round(len(win_rets) / total * 100, 1),
        "return_trades": total,
        "return_win_rate": round(len(win_rets) / total * 100, 1),
        "avg_return": avg_ret,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_loss_ratio": round(avg_win / abs(avg_loss), 2)
        if avg_win is not None and avg_loss is not None and avg_loss != 0
        else None,
        "sample_count": samples,
        "trade_sample_ratio": ratio,
    }


def _daily_portfolio_return_pct(day_rets: list[float]) -> float:
    """旧三分仓口径（兼容测试）：sum(各槽位收益) / 3。"""
    if not day_rets:
        return 0.0
    slots = list(day_rets)[:MAX_SLOTS_PER_DAY]
    return round(sum(slots) / float(POSITION_LAYERS), 4)


def _daily_weighted_portfolio_return_pct(day_rows: list[dict]) -> float:
    """真实建议仓位加权：sum(position_pct/100 * ret)。"""
    total = 0.0
    for r in day_rows:
        w = _record_position_weight(r)
        ret = r.get("_ret")
        if w <= 0 or ret is None:
            continue
        total += w * float(ret)
    return round(total, 4)


def _trades_by_date_from_rows(with_return: list[dict]) -> dict[str, list[float]]:
    """兼容旧接口：按日收集收益列表（不再截断槽位）。"""
    trades_by_date: dict[str, list[float]] = defaultdict(list)
    for r in with_return:
        d = _norm_date(r.get("date"))
        if d and r.get("_ret") is not None:
            trades_by_date[d].append(float(r["_ret"]))
    return dict(trades_by_date)


def _rows_by_date(with_return: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in with_return:
        d = _norm_date(r.get("date"))
        if d and r.get("_ret") is not None:
            out[d].append(r)
    return dict(out)


def _compound_factor_by_day(trades_by_date: dict[str, list[float]]) -> float:
    """旧三分仓复利（兼容测试）。"""
    factor = 1.0
    for d in sorted(trades_by_date.keys()):
        day_r = _daily_portfolio_return_pct(trades_by_date[d])
        factor *= 1 + day_r / 100.0
    return factor


def _compound_factor_by_weighted_rows(rows_by_date: dict[str, list[dict]]) -> float:
    """真实仓位加权按日复利。"""
    factor = 1.0
    for d in sorted(rows_by_date.keys()):
        day_r = _daily_weighted_portfolio_return_pct(rows_by_date[d])
        factor *= 1 + day_r / 100.0
    return factor


def _build_equity_curve(with_return: list[dict]) -> list[dict[str, Any]]:
    """权益曲线：按真实建议仓位加权的当日组合收益复利。"""
    if not with_return:
        return []
    rows_by_date = _rows_by_date(with_return)
    dates = sorted(rows_by_date.keys())
    first_date = datetime.strptime(dates[0], "%Y-%m-%d")
    last_date = datetime.strptime(dates[-1], "%Y-%m-%d")
    factor = 1.0
    equity: list[dict[str, Any]] = []
    cur = first_date
    while cur <= last_date:
        d = cur.strftime("%Y-%m-%d")
        day_list = rows_by_date.get(d)
        day_pnl: Optional[float] = None
        if day_list:
            day_pnl = _daily_weighted_portfolio_return_pct(day_list)
            factor *= 1 + day_pnl / 100.0
        equity.append(
            {
                "date": d,
                "pnl_pct": day_pnl,
                "cumulative_pct": round((factor - 1) * 100, 2),
            }
        )
        cur += timedelta(days=1)
    return equity


def _calc_max_drawdown(equity_curve: list[dict]) -> tuple[float, dict[str, str]]:
    """权益峰谷最大回撤(%)：基于 wealth=1+cumulative_pct/100，(peak-trough)/peak。"""
    if not equity_curve:
        return 0.0, {}
    peak_wealth = 0.0
    peak_date = ""
    max_dd = 0.0
    dd_peak_date = ""
    dd_trough_date = ""
    for entry in equity_curve:
        cum = float(entry.get("cumulative_pct", 0))
        d = str(entry.get("date", ""))
        wealth = 1.0 + cum / 100.0
        if wealth <= 0:
            wealth = 1e-9
        if wealth >= peak_wealth:
            peak_wealth = wealth
            peak_date = d
        if peak_wealth <= 0:
            continue
        dd = (peak_wealth - wealth) / peak_wealth * 100.0
        if dd > max_dd:
            max_dd = dd
            dd_peak_date = peak_date
            dd_trough_date = d
    if max_dd <= 0:
        return 0.0, {}
    return round(max_dd, 2), {"peak_date": dd_peak_date, "trough_date": dd_trough_date}


def _last_friday_iso(today: str) -> str:
    dt = datetime.strptime(today[:10], "%Y-%m-%d")
    # 周五 weekday=4
    days_since_fri = (dt.weekday() - 4) % 7
    fri = dt - timedelta(days=days_since_fri)
    return fri.strftime("%Y-%m-%d")


def _build_trading_guidance(cross: dict[str, dict[str, dict]]) -> dict[str, Any]:
    """根据连板×竞价交叉统计生成开仓指导（表格数据 + 风险文案）。"""

    def _wr(board: str, auction: str) -> Optional[float]:
        blk = (cross.get(board) or {}).get(auction) or {}
        wr = blk.get("win_rate")
        return float(wr) if wr is not None and blk.get("trades") else None

    def _fmt_wr(v: Optional[float], fallback: str) -> str:
        return f"{v:.0f}%" if v is not None else fallback

    rules = [
        {
            "priority": 1,
            "board": "4进5",
            "auction": "5% ~ 6%",
            "win_rate": _fmt_wr(_wr("4进5", "5~6%"), "75%"),
            "position": "满仓（核心）",
            "skip": False,
        },
        {
            "priority": 2,
            "board": "4进5",
            "auction": "4~5% 或 6~7.5%",
            "win_rate": _fmt_wr(
                None,
                "57%",
            ),
            "position": "正常仓",
            "skip": False,
            "note": "两档合并展示",
        },
        {
            "priority": 3,
            "board": "3进4",
            "auction": "6~7.5%",
            "win_rate": _fmt_wr(_wr("3进4", "6~7.5%"), "50%"),
            "position": "半仓",
            "skip": False,
        },
        {
            "priority": 3,
            "board": "3进4",
            "auction": "5~6%",
            "win_rate": _fmt_wr(_wr("3进4", "5~6%"), "45%"),
            "position": "半仓",
            "skip": False,
        },
        {
            "priority": 0,
            "board": "2进3",
            "auction": "任何",
            "win_rate": (
                f"≤{max(v for v in (_wr('2进3', b) for b in AUCTION_CHART_ORDER) if v is not None):.0f}%"
                if any(_wr("2进3", b) is not None for b in AUCTION_CHART_ORDER)
                else "≤35%"
            ),
            "position": "0%",
            "skip": True,
        },
        {
            "priority": 0,
            "board": "5板以上",
            "auction": "6~7.5%",
            "win_rate": _fmt_wr(_wr("5板以上", "6~7.5%"), "33%"),
            "position": "0%",
            "skip": True,
        },
    ]
    # 第二档：取 4~5 与 6~7.5 中较高胜率展示
    w45 = _wr("4进5", "4~5%")
    w675 = _wr("4进5", "6~7.5%")
    parts = [_fmt_wr(v, "") for v in (w45, w675) if v is not None]
    if parts:
        rules[1]["win_rate"] = " / ".join(parts) if len(parts) > 1 else parts[0]

    risk_summary = (
        "放弃所有2进3；5板以上不做高开（6~7.5%）；"
        "4进5只选竞价5~6%为最优，其他竞价可做但仓位减半。"
    )
    mantra = "只做3进4和4进5，4进5中5~6%最佳，放弃2进3和5板高开。"

    return {
        "rules": rules,
        "risk_summary": risk_summary,
        "mantra": mantra,
    }


def _assemble_report(scoped: list[dict]) -> dict[str, Any]:
    scoped = sorted(scoped, key=lambda x: (x.get("date", ""), x.get("code", "")))
    samples = [r for r in scoped if r.get("_ret") is not None]
    trades = [r for r in samples if _record_can_open(r)]
    equity = _build_equity_curve(trades)
    max_dd, dd_range = _calc_max_drawdown(equity)

    summary_blk = _return_stat_block(trades, sample_count=len(samples))
    rows_by_date = _rows_by_date(trades)
    factor = _compound_factor_by_weighted_rows(rows_by_date) if rows_by_date else 1.0
    total_return_pct = round((factor - 1) * 100, 2) if trades else 0.0

    latest_trade_date = ""
    if scoped:
        latest_trade_date = max(_norm_date(r.get("date")) for r in scoped if _norm_date(r.get("date")))

    summary = {
        "rule": "收盘买入 → 次日收盘卖出；仅策略开仓计交易；仓位按个股建议仓位加权、按日复利",
        "position_rule": "真实建议仓位(position_pct)；空仓信号只计入样本数",
        "total_signals": len(scoped),
        "sample_count": len(samples),
        "return_settled": summary_blk["trades"],
        "trade_sample_ratio": summary_blk.get("trade_sample_ratio"),
        "return_win_rate": summary_blk["return_win_rate"],
        "avg_return": summary_blk["avg_return"],
        "total_return_pct": total_return_pct,
        "profit_loss_ratio": summary_blk["profit_loss_ratio"],
        "max_drawdown_pct": max_dd,
        "max_drawdown_range": dd_range,
    }

    trade_date_b1 = _build_trade_date_b1_from_records(scoped)
    by_board_raw: dict[str, list] = defaultdict(list)
    by_board_samples: dict[str, list] = defaultdict(list)
    by_b1_raw: dict[str, list] = defaultdict(list)
    by_b1_samples: dict[str, list] = defaultdict(list)
    cross_raw: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    cross_samples: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    for r in samples:
        bl = _board_chart_label(r)
        if bl:
            by_board_samples[bl].append(r)
            if _record_can_open(r):
                by_board_raw[bl].append(r)
        d = _norm_date(r.get("date"))
        b1 = _record_b1_rate(r)
        if b1 is None and d:
            b1 = trade_date_b1.get(d)
        tier = _b1_chart_tier(b1)
        if tier != "未知":
            by_b1_samples[tier].append(r)
            if _record_can_open(r):
                by_b1_raw[tier].append(r)
        try:
            ag = float(r.get("auction_gain")) if r.get("auction_gain") is not None else None
        except (TypeError, ValueError):
            ag = None
        ab = _auction_bucket(ag)
        if bl and ab in AUCTION_CHART_ORDER:
            cross_samples[bl][ab].append(r)
            if _record_can_open(r):
                cross_raw[bl][ab].append(r)

    by_board = {
        k: _return_stat_block(by_board_raw[k], sample_count=len(by_board_samples[k]))
        for k in BOARD_CHART_ORDER
        if k in by_board_samples
    }
    by_b1_tier = {
        k: _return_stat_block(by_b1_raw.get(k, []), sample_count=len(by_b1_samples.get(k, [])))
        for k in B1_CHART_ORDER
    }
    by_board_cross: dict[str, dict[str, dict]] = {
        board: {
            ab: _return_stat_block(
                cross_raw[board][ab],
                sample_count=len(cross_samples[board][ab]),
            )
            for ab in AUCTION_CHART_ORDER
        }
        for board in BOARD_CHART_ORDER
    }

    from src.config import now_cn

    today = now_cn().strftime("%Y-%m-%d")
    weekly_updated = _last_friday_iso(today)

    return {
        "summary": summary,
        "latest_trade_date": latest_trade_date,
        "equity_curve": equity,
        "by_board": by_board,
        "by_b1_tier": by_b1_tier,
        "by_board_cross_auction": by_board_cross,
        "trading_guidance": _build_trading_guidance(by_board_cross),
        "meta": {
            "weekly_metrics_updated": weekly_updated,
            "daily_metrics_note": "基础指标每个交易日收盘后更新",
            "weekly_metrics_note": "收益曲线、最大回撤、盈亏比每周五收盘后重算（建议仓位加权按日复利）",
        },
    }


def _load_scoped_records() -> list[dict]:
    records = load_screener_history_entries()
    scoped: list[dict] = []
    for r in records:
        d = _norm_date(r.get("date"))
        if not d:
            continue
        row = dict(r)
        row["_ret"] = _trade_return_pct(row)
        scoped.append(row)
    return scoped


def _build_full_report() -> dict[str, Any]:
    return _assemble_report(_load_scoped_records())


def build_screener_backtest_report(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """构建回测统计包（供 /api/screener-backtest）。"""
    global _cached_report, _cached_date

    from src.config import now_cn

    today = now_cn().strftime("%Y-%m-%d")
    if _cached_report is None or _cached_date != today:
        _cached_report = _build_full_report()
        _cached_date = today

    if not date_from and not date_to:
        return _cached_report

    scoped = _load_scoped_records()
    df = _norm_date(date_from) if date_from else ""
    dt = _norm_date(date_to) if date_to else ""
    filtered = [
        r
        for r in scoped
        if (not df or _norm_date(r.get("date")) >= df) and (not dt or _norm_date(r.get("date")) <= dt)
    ]
    return _assemble_report(filtered)

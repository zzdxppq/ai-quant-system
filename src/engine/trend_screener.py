"""趋势选股引擎 — 从 TOP30 中筛选强趋势股（每日盘后执行）

规则（V1）
========

初筛（全部满足）：
  · 10日涨幅 ≥ 45%
  · 今日涨幅 > 0
  · 连板 == 0 OR (连板 == 1 且 今日未涨停)
  · 收盘 > MA5 且 > MA10
  · 换手率 3% ~ 20%（缺失时放宽到 2%~25%）

基础评分（满分 100）：
  · 趋势强度 25：10日 ≥60% → 25 / 45%-60% → 20
  · 近期动能 20：今日 ≥8% → 20 / 5%-8% → 15 / 3%-5% → 10 / <3% → 5
  · 主动攻击 15：active_attack.is_attack → 15
  · 量价健康 20：换手 5%-15% → 20 / 3%-5% 或 15%-20% → 12 / 其他 → 5
  · 市值流动 10：50亿-200亿 → 10 / 200亿-500亿 → 7 / 其他 → 5

板块动量修正（±10）：
  · 今日 top30 中该股 top_concepts 加权占比，对比近 5 日均占比
  · 变化 >+5% → +10 / +2~+5% → +5 / -2~+2 → 0 / -5~-2 → -5 / <-5 → -10

最终：总分 ≥ 70 取前 3 名作为明日观察池
"""
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Optional
from pathlib import Path
import json
from collections import defaultdict

from src.config import DATA_DIR, now_cn


SECTOR_HISTORY_FILE = DATA_DIR / "trend_sector_history.json"
TREND_LATEST_FILE = DATA_DIR / "latest_trend.json"
TREND_HISTORY_FILE = DATA_DIR / "trend_history.json"


@dataclass
class TrendHit:
    code: str
    name: str
    close: float
    gain_10d: float
    today_gain: float
    continuous_limit_up: int
    industry: str
    top_concepts: list[str] = field(default_factory=list)
    is_main_board: bool = True
    market_cap_yi: float = 0
    turnover: Optional[float] = None
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    active_attack: bool = False
    # 评分
    score_trend: int = 0
    score_momentum: int = 0
    score_attack: int = 0
    score_volume: int = 0
    score_market_cap: int = 0
    base_score: int = 0
    sector_bonus: int = 0
    total_score: int = 0
    sector_bonus_detail: str = ""


def _fetch_kline_metrics(code: str, market_cap_yi: Optional[float] = None) -> dict:
    """拉 K 线，算 MA5 / MA10 / 当日 turnover。东财优先，新浪兜底。
    新浪无 turnover → 用 vol * close / 流通市值 估算。
    """
    out = {"ma5": None, "ma10": None, "turnover": None}
    df = None
    try:
        from src.data.eastmoney_api import fetch_kline
        for _ in range(2):
            df = fetch_kline(code, klt=101, fqt=1, limit=15)
            if df is not None and not df.empty:
                break
    except Exception:
        df = None
    used_em = df is not None and not df.empty
    if not used_em:
        try:
            from src.data.sina_kline_api import fetch_kline as sina_fetch
            df = sina_fetch(code, datalen=15)
        except Exception:
            df = None
    if df is None or df.empty or len(df) < 10:
        return out
    closes = df["close"].astype(float).tolist()
    if len(closes) >= 5:
        out["ma5"] = round(sum(closes[-5:]) / 5, 3)
    if len(closes) >= 10:
        out["ma10"] = round(sum(closes[-10:]) / 10, 3)
    # turnover：优先东财字段，否则用 vol*close/流通市值 估算
    if used_em and "turnover" in df.columns:
        try:
            t = float(df.iloc[-1]["turnover"])
            if t > 0:
                out["turnover"] = round(t, 2)
        except (TypeError, ValueError):
            pass
    if out["turnover"] is None and market_cap_yi and market_cap_yi > 0:
        try:
            last_close = float(df.iloc[-1]["close"])
            last_vol = float(df.iloc[-1]["volume"])
            if last_close > 0 and last_vol > 0:
                est = last_vol * last_close / (market_cap_yi * 1e8) * 100
                out["turnover"] = round(est, 2)
        except (TypeError, ValueError, KeyError):
            pass
    return out


def _passes_initial_filter(item: dict, ma5, ma10, turnover) -> tuple[bool, str]:
    """返回 (是否通过, 不通过原因)"""
    try:
        gain_10d = float(item.get("gain_10d") or 0)
    except (TypeError, ValueError):
        gain_10d = 0
    if gain_10d < 45:
        return False, f"10日涨幅 {gain_10d:.1f}% < 45%"

    try:
        today_gain = float(item.get("change_pct") or 0)
    except (TypeError, ValueError):
        today_gain = 0
    if today_gain <= 0:
        return False, f"今日涨幅 {today_gain:.2f}% ≤ 0"

    board = int(item.get("continuous_limit_up") or 0)
    is_main = item.get("is_main_board", True)
    lu_thr = 19.5 if not is_main else 9.8
    today_lu = today_gain >= lu_thr and bool(item.get("last_limit_up_time"))
    if board >= 2:
        return False, f"连板 {board} ≥ 2（已脱离趋势）"
    if board == 1 and today_lu:
        return False, "连板=1 且今日涨停 → 转接力梯队"

    close = float(item.get("close") or 0)
    if ma5 is None or ma10 is None:
        return False, "MA5/MA10 数据缺失"
    if not (close > ma5 and close > ma10):
        return False, f"破均线（close={close:.2f} MA5={ma5:.2f} MA10={ma10:.2f}）"

    # 换手过滤：3-20%（缺失或放宽到 2-25%）
    if turnover is None:
        return False, "换手率数据缺失"
    if not (2 <= turnover <= 25):
        return False, f"换手率 {turnover:.2f}% 越界（放宽 2-25 仍未达）"

    return True, ""


def _compute_base_score(item: dict, turnover: Optional[float]) -> tuple[int, dict]:
    """基础 100 分 → 返回 (总分, 明细字典)"""
    gain_10d = float(item.get("gain_10d") or 0)
    today_gain = float(item.get("change_pct") or 0)
    mc = float(item.get("market_cap_yi") or 0)

    # 趋势强度 25
    score_trend = 25 if gain_10d >= 60 else 20
    # 近期动能 20
    if today_gain >= 8: score_momentum = 20
    elif today_gain >= 5: score_momentum = 15
    elif today_gain >= 3: score_momentum = 10
    else: score_momentum = 5
    # 主动攻击 15
    aa = item.get("active_attack") or {}
    is_attack = bool(aa.get("is_attack"))
    score_attack = 15 if is_attack else 0
    # 量价健康 20
    if turnover is None:
        score_volume = 0
    elif 5 <= turnover <= 15:
        score_volume = 20
    elif (3 <= turnover < 5) or (15 < turnover <= 20):
        score_volume = 12
    else:
        score_volume = 5
    # 市值流动 10
    if 50 <= mc <= 200: score_market_cap = 10
    elif 200 < mc <= 500: score_market_cap = 7
    else: score_market_cap = 5

    total = score_trend + score_momentum + score_attack + score_volume + score_market_cap
    return total, {
        "trend": score_trend, "momentum": score_momentum,
        "attack": score_attack, "volume": score_volume,
        "market_cap": score_market_cap, "is_attack": is_attack,
    }


def _load_sector_history() -> dict:
    if not SECTOR_HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(SECTOR_HISTORY_FILE.read_text())
    except Exception:
        return {}


def _save_sector_history(hist: dict) -> None:
    SECTOR_HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=2))


def _compute_sector_pcts(top30: list[dict]) -> dict[str, float]:
    """统计今日 top30 中每个概念的出现次数及占比（次数/30 * 100）"""
    pcts: dict[str, float] = {}
    counter: dict[str, int] = defaultdict(int)
    total = len(top30) or 1
    for it in top30:
        concepts = it.get("top_concepts") or []
        for c in concepts:
            if c:
                counter[c] += 1
    for name, cnt in counter.items():
        pcts[name] = round(cnt / total * 100, 2)
    return pcts


def _sector_momentum(today_pcts: dict[str, float], history: dict) -> dict[str, int]:
    """对每个概念，计算 (今日占比 - 5日均占比) → 修正分

    history: {date_str: {concept_name: pct}}
    """
    today_str = now_cn().strftime("%Y-%m-%d")
    # 收集近 5 日（不含今日，含今日的 5 日均更稳定，按 spec "过去5日(含今日)的平均"）
    sorted_dates = sorted(history.keys())[-4:]  # 最近 4 个非今日
    all_dates_pcts = [history[d] for d in sorted_dates] + [today_pcts]
    # 每个概念求 5 日平均（若某日无该概念则计 0）
    momentum: dict[str, int] = {}
    for concept, today_v in today_pcts.items():
        avg = sum(d.get(concept, 0) for d in all_dates_pcts) / len(all_dates_pcts)
        delta = today_v - avg
        if delta > 5: bonus = 10
        elif delta > 2: bonus = 5
        elif delta > -2: bonus = 0
        elif delta > -5: bonus = -5
        else: bonus = -10
        momentum[concept] = bonus
    return momentum


def _apply_sector_bonus(hit_concepts: list[str], momentum: dict[str, int]) -> tuple[int, str]:
    """取个股 top_concepts 中绝对值最大的板块动量分作为修正"""
    if not hit_concepts:
        return 0, ""
    best = 0
    best_name = ""
    for c in hit_concepts:
        v = momentum.get(c, 0)
        if abs(v) > abs(best):
            best = v
            best_name = c
    detail = f"{best_name} {best:+d}" if best_name and best != 0 else "—"
    return best, detail


def run_trend_screener() -> dict:
    """跑趋势选股 → 写 latest_trend.json + 滚动 sector_history"""
    from src.config import is_trading_day
    if not is_trading_day():
        print("[趋势选股] 非交易日跳过")
        return {"status": "skipped", "reason": "non-trading day"}

    today = now_cn().strftime("%Y-%m-%d")
    rank_file = DATA_DIR / "latest_ranking.json"
    if not rank_file.exists():
        print("[趋势选股] latest_ranking 不存在")
        return {"status": "error", "reason": "no ranking"}
    rd = json.loads(rank_file.read_text())
    top30 = rd.get("ranking") or []
    if not top30:
        return {"status": "error", "reason": "ranking empty"}

    # 1) 算今日板块占比 + 保存到 sector_history
    today_pcts = _compute_sector_pcts(top30)
    history = _load_sector_history()
    history[today] = today_pcts
    # 仅保留最近 5 个交易日
    keep_dates = sorted(history.keys())[-5:]
    history = {d: history[d] for d in keep_dates}
    _save_sector_history(history)

    # 2) 板块动量分
    # 5 日均：取最近 4 日历史 + 今日
    momentum = _sector_momentum(today_pcts, {d: history[d] for d in keep_dates if d != today})

    # 3) 遍历 top30，初筛 + 评分
    hits: list[TrendHit] = []
    rejected: list[dict] = []
    for item in top30:
        code = str(item.get("code") or "")
        if not code:
            continue
        mc = float(item.get("market_cap_yi") or 0) or None
        km = _fetch_kline_metrics(code, market_cap_yi=mc)
        ma5, ma10, turnover = km["ma5"], km["ma10"], km["turnover"]

        passed, reason = _passes_initial_filter(item, ma5, ma10, turnover)
        if not passed:
            rejected.append({"code": code, "name": item.get("name"), "reason": reason})
            continue

        base, detail = _compute_base_score(item, turnover)
        bonus, bonus_text = _apply_sector_bonus(item.get("top_concepts") or [], momentum)
        total = base + bonus

        hit = TrendHit(
            code=code,
            name=item.get("name", ""),
            close=float(item.get("close") or 0),
            gain_10d=float(item.get("gain_10d") or 0),
            today_gain=float(item.get("change_pct") or 0),
            continuous_limit_up=int(item.get("continuous_limit_up") or 0),
            industry=item.get("industry", "") or "",
            top_concepts=item.get("top_concepts") or [],
            is_main_board=bool(item.get("is_main_board", True)),
            market_cap_yi=float(item.get("market_cap_yi") or 0),
            turnover=turnover,
            ma5=ma5, ma10=ma10,
            active_attack=detail["is_attack"],
            score_trend=detail["trend"],
            score_momentum=detail["momentum"],
            score_attack=detail["attack"],
            score_volume=detail["volume"],
            score_market_cap=detail["market_cap"],
            base_score=base,
            sector_bonus=bonus,
            sector_bonus_detail=bonus_text,
            total_score=total,
        )
        hits.append(hit)

    # 4) ≥70 取前 3
    qualified = sorted([h for h in hits if h.total_score >= 70],
                       key=lambda x: -x.total_score)[:3]

    payload = {
        "date": today,
        "generated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "pool": [asdict(h) for h in qualified],
        "all_scored": [asdict(h) for h in sorted(hits, key=lambda x: -x.total_score)],
        "rejected": rejected,
        "sector_momentum": momentum,
        "sector_today_pcts": today_pcts,
    }
    TREND_LATEST_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    # 5) 写入历史（仅 qualified；若空也存日期标记）
    hist_records = []
    if TREND_HISTORY_FILE.exists():
        try:
            hist_records = json.loads(TREND_HISTORY_FILE.read_text())
        except Exception:
            hist_records = []
    # 去重：当日重跑覆盖
    hist_records = [r for r in hist_records if r.get("date") != today]
    for h in qualified:
        hist_records.append({
            "date": today,
            **asdict(h),
            # 次日表现回填字段
            "next_day_open": None,
            "next_day_auction_gain": None,
            "next_day_close": None,
            "next_day_close_gain": None,
            "is_win": None,
        })
    TREND_HISTORY_FILE.write_text(json.dumps(hist_records, ensure_ascii=False, indent=2))

    print(f"[趋势选股] 完成: top30={len(top30)} 评分={len(hits)} 入池={len(qualified)} 拒绝={len(rejected)}")
    return {
        "status": "ok",
        "date": today,
        "pool_count": len(qualified),
        "scored_count": len(hits),
    }


def backfill_trend_next_day() -> dict:
    """回填趋势池次日表现（盘后调用）"""
    from src.config import is_trading_day
    if not is_trading_day():
        return {"status": "skipped"}
    from src.data.eastmoney_api import fetch_kline
    if not TREND_HISTORY_FILE.exists():
        return {"status": "no_history"}
    records = json.loads(TREND_HISTORY_FILE.read_text())
    today = now_cn().strftime("%Y-%m-%d")
    updated = 0
    for r in records:
        if r.get("date") == today or r.get("next_day_close") is not None:
            continue
        rec_date = r["date"]
        code = r["code"]
        try:
            df = fetch_kline(code, klt=101, fqt=1, limit=10)
            if df is None or df.empty:
                continue
            dates = [str(x)[:10] for x in df["date"].tolist()]
            if rec_date not in dates:
                continue
            idx = dates.index(rec_date)
            if idx + 1 >= len(df):
                continue
            next_row = df.iloc[idx + 1]
            base_close = float(r.get("close") or 0)
            n_open = float(next_row["open"])
            n_close = float(next_row["close"])
            if base_close > 0 and n_open > 0:
                r["next_day_open"] = round(n_open, 2)
                r["next_day_auction_gain"] = round((n_open / base_close - 1) * 100, 2)
                r["next_day_close"] = round(n_close, 2)
                r["next_day_close_gain"] = round((n_close / base_close - 1) * 100, 2)
                r["is_win"] = r["next_day_close_gain"] > 0
                updated += 1
        except Exception:
            continue
    if updated:
        TREND_HISTORY_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2))
    return {"status": "ok", "updated": updated}

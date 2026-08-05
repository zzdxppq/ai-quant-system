"""趋势选股引擎 — 从 TOP30 中筛选强趋势股（每日盘后执行）

规则（V1）
========

初筛（全部满足）：
  · 10日涨幅 ≥ 45%
  · 今日涨幅 > 0
  · 连板 == 0 OR (连板 == 1 且 今日未涨停)
  · 收盘 > MA5 且 > MA10
  · 换手率 3% ~ 20%（缺失时放宽到 2%~25%）

基础评分（满分 100）+ 板块动量修正（±10）见下。

最终：在通过初筛的标的中取 **总分最高 1 只** 写入观察池与历史。

历史回填口径
============
  · D+1：相对**入选日收盘**的次日开盘/收盘涨幅；`is_win` = D+1 收盘涨跌 > 0。
  · 温和开仓：次日竞价相对入选日收盘 ∈ [-3%, +3%]。
  · D+2：假设以 **D+1 收盘价** 买入，记录再下一日开盘/收盘相对买入价的涨幅；
          `is_win_after_entry` = D+2 收盘涨幅 > 0（持仓第二日收红为胜）。
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from typing import Any, Optional

import pandas as pd
from src.config import DATA_DIR, now_cn
from src.data.json_io import dump_json_file, load_json_file


SECTOR_HISTORY_FILE = DATA_DIR / "trend_sector_history.json"
TREND_LATEST_FILE = DATA_DIR / "latest_trend.json"
TREND_HISTORY_FILE = DATA_DIR / "trend_history.json"

# ledger_doc.doc_key：与 trend_history.json 同表，禁止再使用 data/*.json 手工文件
LEDGER_TREND_POOL_MANUAL = "trend_pool_manual.json"
LEDGER_TREND_HISTORY_MANUAL = "trend_history_manual.json"

# 库中尚无补录文档时的内置默认（科翔 2026-05-14）；覆盖请 upsert 同名 ledger 键
_DEFAULT_TREND_POOL_MANUAL_DOC: dict[str, Any] = {
    "all_scored_extra": [
        {
            "code": "300903",
            "name": "科翔股份",
            "close": 79.24,
            "gain_10d": 58.2,
            "today_gain": 10.06,
            "continuous_limit_up": 0,
            "industry": "电子元件",
            "top_concepts": ["专精特新"],
            "is_main_board": False,
            "market_cap_yi": 48,
            "turnover": 12.4,
            "ma5": 73.18,
            "ma10": 68.52,
            "active_attack": False,
            "score_trend": 25,
            "score_momentum": 20,
            "score_attack": 0,
            "score_volume": 20,
            "score_market_cap": 7,
            "base_score": 72,
            "sector_bonus": 10,
            "total_score": 82,
            "sector_bonus_detail": "专精特新 +10",
        }
    ]
}

_DEFAULT_TREND_HISTORY_MANUAL_LIST: list[dict[str, Any]] = [
    {
        "date": "2026-05-14",
        "code": "300903",
        "name": "科翔股份",
        "close": 79.24,
        "gain_10d": 58.2,
        "today_gain": 10.06,
        "continuous_limit_up": 0,
        "industry": "电子元件",
        "top_concepts": ["专精特新"],
        "is_main_board": False,
        "market_cap_yi": 48,
        "turnover": 12.4,
        "ma5": 73.18,
        "ma10": 68.52,
        "active_attack": False,
        "score_trend": 25,
        "score_momentum": 20,
        "score_attack": 0,
        "score_volume": 20,
        "score_market_cap": 7,
        "base_score": 72,
        "sector_bonus": 10,
        "total_score": 82,
        "sector_bonus_detail": "专精特新 +10",
        "next_day_open": None,
        "next_day_auction_gain": 0.96,
        "next_day_close": None,
        "next_day_close_gain": None,
        "is_win": None,
        "trend_open_ok": True,
        "trend_open_reason": "次日竞价 +0.96% ∈ [-3%, +3%]，满足温和试错开仓",
        "entry_close_next_day": None,
        "d2_open": None,
        "d2_auction_gain_pct": None,
        "d2_close": None,
        "d2_close_gain_pct": None,
        "is_win_after_entry": None,
    }
]


def load_trend_pool_manual_extra_hits() -> list[dict[str, Any]]:
    """读 ledger `trend_pool_manual.json`；无记录时用内置默认（不落盘）。

    若库中已存在文档且含键 `all_scored_extra`（可为空列表），则完全以库为准（空列表表示关闭内置补录）。
    """
    from src.data.ledger_doc_store import load_json

    raw = load_json(LEDGER_TREND_POOL_MANUAL)
    if isinstance(raw, dict) and "all_scored_extra" in raw:
        ex = raw.get("all_scored_extra")
        if isinstance(ex, list):
            return [x for x in ex if isinstance(x, dict)]
        return []
    return [dict(x) for x in _DEFAULT_TREND_POOL_MANUAL_DOC["all_scored_extra"]]


def load_trend_history_manual_records() -> list[dict[str, Any]]:
    """读 ledger `trend_history_manual.json`；无记录时用内置默认（不落盘）。

    库中若存在空列表 `[]`，表示不合并任何手工历史（覆盖内置默认）。
    """
    from src.data.ledger_doc_store import load_json

    raw = load_json(LEDGER_TREND_HISTORY_MANUAL)
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return [dict(x) for x in _DEFAULT_TREND_HISTORY_MANUAL_LIST]


def save_trend_pool_manual_doc(doc: dict[str, Any]) -> None:
    """写入 quant：`ledger_doc` 键 trend_pool_manual.json（结构含 all_scored_extra）。"""
    from src.data.ledger_doc_store import upsert_json

    if not isinstance(doc, dict):
        return
    upsert_json(LEDGER_TREND_POOL_MANUAL, doc)


def save_trend_history_manual_records(rows: list[dict[str, Any]]) -> None:
    """写入 quant：`ledger_doc` 键 trend_history_manual.json（list 记录）。"""
    from src.data.ledger_doc_store import upsert_json

    if not isinstance(rows, list):
        return
    upsert_json(LEDGER_TREND_HISTORY_MANUAL, rows)


def _norm_trade_date_ymd(val: Any) -> str:
    s = str(val or "").strip().replace("/", "-")
    return s[:10] if len(s) >= 10 else ""


def _ranking_trade_date_ymd(rd: dict | None) -> str:
    """榜单所属交易日（YYYY-MM-DD）。

    latest_ranking.json 的 `date`/`updated_at` 多为「收盘后落库时刻」，取日期部分；
    避免 5/14 收盘榜在 5/15 凌晨跑 trend-run 却把历史记成 5/15。
    """
    if not isinstance(rd, dict):
        return now_cn().strftime("%Y-%m-%d")
    for key in ("trade_date", "date", "updated_at"):
        d = _norm_trade_date_ymd(rd.get(key))
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            return d
    return now_cn().strftime("%Y-%m-%d")


def _repair_trend_history_date_vs_ranking() -> int:
    """若 latest_trend 的 date 晚于 ranking 交易日，且历史里有同码同错日，则改回 ranking 日（修旧数据）。"""
    rd = load_json_file(DATA_DIR / "latest_ranking.json")
    if not isinstance(rd, dict):
        return 0
    rank_day = _ranking_trade_date_ymd(rd)
    rt = load_json_file(TREND_LATEST_FILE)
    if not isinstance(rt, dict):
        return 0
    trend_lbl = _norm_trade_date_ymd(rt.get("date"))
    pool = rt.get("pool") or []
    if len(trend_lbl) != 10 or not pool or not isinstance(pool[0], dict):
        return 0
    code = str(pool[0].get("code") or "")
    if not code or trend_lbl <= rank_day:
        return 0
    recs = load_json_file(TREND_HISTORY_FILE)
    if not isinstance(recs, list):
        return 0
    changed = 0
    for r in recs:
        if str(r.get("code") or "") != code:
            continue
        d = _norm_trade_date_ymd(r.get("date"))
        if d == trend_lbl and rank_day < d:
            r["date"] = rank_day
            changed += 1
    if changed:
        dump_json_file(TREND_HISTORY_FILE, recs)
        print(f"[趋势选股] 修正历史入选日 {changed} 条（对齐 ranking 交易日 {rank_day}）")
    return changed


def dedupe_trend_history_by_date(records: list[dict]) -> tuple[list[dict], int]:
    """同一自然日只保留一条：取 total_score 最高（同分再比 close、code）。"""
    if not records:
        return [], 0
    buckets: dict[str, list[dict]] = defaultdict(list)
    loose: list[dict] = []
    for r in records:
        d = _norm_trade_date_ymd(r.get("date"))
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            buckets[d].append(r)
        else:
            loose.append(r)
    out: list[dict] = []
    for d in sorted(buckets.keys()):
        rows = buckets[d]
        if len(rows) == 1:
            out.append(rows[0])
            continue
        best = max(
            rows,
            key=lambda x: (
                int(float(x.get("total_score") or 0)),
                float(x.get("close") or 0),
                str(x.get("code") or ""),
            ),
        )
        out.append(best)
    out.extend(loose)
    return out, len(records) - len(out)


def reconcile_trend_history_file() -> list[dict]:
    """去重同日多条 → 落盘；供 API / 定时任务在读前保证一日一笔。"""
    _repair_trend_history_date_vs_ranking()
    raw = load_json_file(TREND_HISTORY_FILE)
    if not isinstance(raw, list):
        return []
    fixed, removed = dedupe_trend_history_by_date(raw)
    if removed > 0:
        dump_json_file(TREND_HISTORY_FILE, fixed)
        print(f"[趋势选股] 历史去重：合并同日重复 {removed} 条，保留每日最高分 1 笔")
    return fixed


def _fetch_daily_kline_for_trend_backfill(code: str) -> Optional[pd.DataFrame]:
    """日 K：与看板日 K 同源取长序列，避免 80 根截断或单源未更新导致 D+1 收盘无法结算。"""
    try:
        from src.data.sina_kline_api import fetch_daily_kline_robust

        df = fetch_daily_kline_robust(code, min_bars=5, datalen=500, skip_cache_read=False)
    except Exception:
        df = None
    if df is None or df.empty or "date" not in df.columns:
        return None
    out = df.copy()
    out["_d"] = out["date"].astype(str).str.strip().str.replace("/", "-").str[:10]
    out = out[out["_d"].str.len() == 10].sort_values("_d").reset_index(drop=True)
    return out if not out.empty else None


def build_trend_history_stats(records: list[dict]) -> dict:
    """与 /api/trend-history 统计口径一致。"""
    settled = [r for r in records if r.get("is_win") is not None]
    wins = sum(1 for r in settled if r.get("is_win"))
    open_judged = [r for r in records if r.get("trend_open_ok") is not None]
    open_ok = [r for r in open_judged if r.get("trend_open_ok") is True]
    open_ok_settled = [r for r in open_ok if r.get("is_win") is not None]

    def _tier_stat(subset: list) -> dict:
        t = len(subset)
        w = sum(1 for r in subset if r.get("is_win"))
        return {
            "trades": t,
            "wins": w,
            "win_rate": round(w / t * 100, 2) if t else None,
        }

    gte85 = [r for r in settled if float(r.get("total_score") or 0) >= 85]
    lt85 = [r for r in settled if float(r.get("total_score") or 0) < 85]

    hold_settled = [r for r in records if r.get("is_win_after_entry") is not None]
    hold_wins = sum(1 for r in hold_settled if r.get("is_win_after_entry"))

    return {
        "total": len(records),
        "settled": len(settled),
        "wins": wins,
        "win_rate": round(wins / len(settled) * 100, 2) if settled else None,
        "avg_next_close_gain": round(
            sum(r.get("next_day_close_gain") or 0 for r in settled) / len(settled), 2
        )
        if settled
        else None,
        "open_ok_judged": len(open_judged),
        "open_ok_count": len(open_ok),
        "open_ok_settled": len(open_ok_settled),
        "open_ok_win_rate": round(
            sum(1 for r in open_ok_settled if r.get("is_win")) / len(open_ok_settled) * 100, 2
        )
        if open_ok_settled
        else None,
        "hold_settled": len(hold_settled),
        "hold_win_rate": round(hold_wins / len(hold_settled) * 100, 2) if hold_settled else None,
        "by_score_tier": {
            "gte_85": _tier_stat(gte85),
            "lt_85": _tier_stat(lt85),
        },
    }


def merge_trend_history_records_with_manual(records: list[dict]) -> list[dict]:
    """合并 ledger `trend_history_manual.json` 补录（按 date 去重：已存在同日记录则跳过）。"""
    manual = load_trend_history_manual_records()
    if not manual:
        return records
    have_dates = {_norm_trade_date_ymd(r.get("date")) for r in records if _norm_trade_date_ymd(r.get("date"))}
    out = list(records)
    for r in manual:
        if not isinstance(r, dict):
            continue
        d = _norm_trade_date_ymd(r.get("date"))
        if not d or d in have_dates:
            continue
        out.append(dict(r))
        have_dates.add(d)
    out.sort(key=lambda x: str(x.get("date") or ""))
    return out


def merge_trend_pool_with_manual(data: dict) -> dict:
    """合并 ledger `trend_pool_manual.json` 的 all_scored_extra，供 K 线弹窗 _findTrendHit 命中补录标的。"""

    def _c6(s: Any) -> str:
        d = "".join(ch for ch in str(s or "") if ch.isdigit())
        return d[-6:].zfill(6) if len(d) >= 6 else ""

    if not isinstance(data, dict):
        return data
    extras = load_trend_pool_manual_extra_hits()
    if not extras:
        return data
    scored = list(data.get("all_scored") or [])
    seen = {_c6(x.get("code")) for x in scored if _c6(x.get("code"))}
    for ex in extras:
        if not isinstance(ex, dict):
            continue
        c = _c6(ex.get("code"))
        if len(c) != 6 or not c.isdigit() or c in seen:
            continue
        scored.append(dict(ex))
        seen.add(c)
    out = dict(data)
    out["all_scored"] = scored
    return out


def get_trend_history_payload(*, light: bool = False) -> dict:
    """读历史 → 同日去重落盘 → 回填 D+1/D+2 → 再读入并算统计（任意自然日可调 K 线）。

    light=True：仅读库+统计，不触发 reconcile/backfill（涨幅榜首屏用，避免并发拉 K 线拖垮进程）。
    """
    if not light:
        reconcile_trend_history_file()
        backfill_trend_next_day()
    raw = load_json_file(TREND_HISTORY_FILE)
    records = raw if isinstance(raw, list) else []
    records = merge_trend_history_records_with_manual(records)
    return {"records": records, "stats": build_trend_history_stats(records)}


def evaluate_trend_open_conditions(auction_gain_pct: Optional[float]) -> tuple[Optional[bool], str]:
    """次日竞价相对入选日收盘价涨幅 → 是否满足「温和试错」开仓（与看板文案：不超 +3% 一致）。"""
    if auction_gain_pct is None:
        return None, ""
    try:
        ag = float(auction_gain_pct)
    except (TypeError, ValueError):
        return None, ""
    if -3 <= ag <= 3:
        return True, f"次日竞价 {ag:+.2f}% ∈ [-3%, +3%]，满足温和试错开仓"
    if ag > 5:
        return False, f"次日竞价 {ag:+.2f}% > +5%，追高，等回踩 MA5"
    if ag > 3:
        return False, f"次日竞价 {ag:+.2f}% 高于 +3%，不心急追价"
    if ag <= -5:
        return False, f"次日竞价 {ag:+.2f}% 低开过猛，观望"
    return False, f"次日竞价 {ag:+.2f}% 偏低开（-5%~-3%），日内确认再接"


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


def _is_main_board_code(code: str) -> bool:
    """复刻 src.data.ranking_scanner._is_main_board 的判定口径，便于 trend_screener 在
    is_main_board 字段缺失/异常时按代码前缀兜底。

    主板 = 沪市 60 开头（68 已是科创板）、深市 00 开头。
    非主板：30x/301x（创业板）、688（科创板）、4/8/92 开头（北交所京A/京E）。
    """
    code = str(code or "")
    if not code:
        return False
    if code.startswith(("300", "301", "688")):
        return False
    if code.startswith(("4", "8", "92")):
        return False
    return True


def _passes_initial_filter(item: dict, ma5, ma10, turnover) -> tuple[bool, str]:
    """返回 (是否通过, 不通过原因)"""
    code = str(item.get("code") or "")
    # 只选主板：沪市/深市主板（排除创业板 300/301、科创板 688、北交所 4/8 开头）
    is_main = item.get("is_main_board", None)
    if is_main is None:
        is_main = _is_main_board_code(code)
    if not is_main:
        return False, "非主板（只选沪/深主板，排除创业板/科创板/北交所京A）"
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
    data = load_json_file(SECTOR_HISTORY_FILE)
    return data if isinstance(data, dict) else {}


def _save_sector_history(hist: dict) -> None:
    dump_json_file(SECTOR_HISTORY_FILE, hist)


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

    rank_file = DATA_DIR / "latest_ranking.json"
    rd = load_json_file(rank_file)
    if rd is None:
        print("[趋势选股] latest_ranking 不存在")
        return {"status": "error", "reason": "no ranking"}
    top30 = rd.get("ranking") or []
    if not top30:
        return {"status": "error", "reason": "ranking empty"}

    trade_date = _ranking_trade_date_ymd(rd)

    # 1) 算今日板块占比 + 保存到 sector_history（键 = 榜单交易日）
    today_pcts = _compute_sector_pcts(top30)
    history = _load_sector_history()
    history[trade_date] = today_pcts
    # 仅保留最近 5 个交易日
    keep_dates = sorted(history.keys())[-5:]
    history = {d: history[d] for d in keep_dates}
    _save_sector_history(history)

    # 2) 板块动量分
    # 5 日均：取最近 4 日历史 + 今日
    momentum = _sector_momentum(today_pcts, {d: history[d] for d in keep_dates if d != trade_date})

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

    # 4) 仅保留评分最高 1 只（在初筛通过的 hits 中取 max total_score）
    best: Optional[TrendHit] = max(hits, key=lambda h: h.total_score) if hits else None

    pool_list = [asdict(best)] if best else []
    all_scored = [asdict(h) for h in sorted(hits, key=lambda x: -x.total_score)]

    payload = {
        "date": trade_date,
        "generated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "pool": pool_list,
        "all_scored": all_scored,
        "rejected": rejected,
        "sector_momentum": momentum,
        "sector_today_pcts": today_pcts,
    }
    dump_json_file(TREND_LATEST_FILE, payload)

    # 5) 历史：每日仅记录该最高分 1 笔
    hist_records = load_json_file(TREND_HISTORY_FILE)
    if not isinstance(hist_records, list):
        hist_records = []
    hist_records = [r for r in hist_records if _norm_trade_date_ymd(r.get("date")) != trade_date]
    if best:
        h = best
        hist_records.append({
            "date": trade_date,
            **asdict(h),
            "next_day_open": None,
            "next_day_auction_gain": None,
            "next_day_close": None,
            "next_day_close_gain": None,
            "is_win": None,
            "trend_open_ok": None,
            "trend_open_reason": "",
            "entry_close_next_day": None,
            "d2_open": None,
            "d2_auction_gain_pct": None,
            "d2_close": None,
            "d2_close_gain_pct": None,
            "is_win_after_entry": None,
        })
    hist_records, _ = dedupe_trend_history_by_date(hist_records)
    dump_json_file(TREND_HISTORY_FILE, hist_records)

    print(f"[趋势选股] 完成: 榜单日={trade_date} top30={len(top30)} 初筛={len(hits)} 入池={1 if best else 0} 拒绝={len(rejected)}")
    return {
        "status": "ok",
        "date": trade_date,
        "pool_count": 1 if best else 0,
        "scored_count": len(hits),
    }


def backfill_trend_next_day() -> dict:
    """回填趋势历史：D+1 相对入选日收盘；D+2 相对「D+1 收盘」假设尾盘买入后的再次日竞价/收盘。

    is_win：入选日视角 D+1 收盘涨跌（相对入选日收盘）>0
    is_win_after_entry：D+2 收盘相对 D+1 收盘 >0（持仓第二日收红为胜）

    任意自然日可执行（周末也会拉 K 线），不因 is_trading_day 跳过。

    含 ledger 中「仅手工、文件无同日」的记录：一并按 K 线回填后写回 `trend_history_manual`。
    """
    file_recs = load_json_file(TREND_HISTORY_FILE)
    if not isinstance(file_recs, list):
        file_recs = []
    manual_list = load_trend_history_manual_records()
    file_dates = {_norm_trade_date_ymd(r.get("date")) for r in file_recs}
    manual_only = [
        dict(mr)
        for mr in manual_list
        if _norm_trade_date_ymd(mr.get("date")) and _norm_trade_date_ymd(mr.get("date")) not in file_dates
    ]
    records = [dict(r) for r in file_recs] + manual_only
    n_file = len(file_recs)
    today = now_cn().strftime("%Y-%m-%d")
    updated = 0
    dirty_file = False
    dirty_manual_extra = False
    for i, r in enumerate(records):
        rec_date = _norm_trade_date_ymd(r.get("date"))
        if not rec_date or rec_date >= today:
            continue
        code = str(r.get("code") or "")
        if not code:
            continue
        need_d1 = r.get("next_day_close") is None
        if not need_d1 and r.get("d2_close_gain_pct") is not None:
            continue
        try:
            df = _fetch_daily_kline_for_trend_backfill(code)
            if df is None or df.empty:
                continue
            dates = df["_d"].tolist()
            if rec_date not in dates:
                continue
            idx = dates.index(rec_date)
            changed = False
            base_close = float(r.get("close") or 0)
            if need_d1 and idx + 1 < len(df) and base_close > 0:
                next_row = df.iloc[idx + 1]
                n_open = float(next_row["open"])
                n_close = float(next_row["close"])
                if n_open > 0 and n_close > 0:
                    r["next_day_open"] = round(n_open, 2)
                    r["next_day_auction_gain"] = round((n_open / base_close - 1) * 100, 2)
                    r["next_day_close"] = round(n_close, 2)
                    r["next_day_close_gain"] = round((n_close / base_close - 1) * 100, 2)
                    r["is_win"] = r["next_day_close_gain"] > 0
                    ok, reason = evaluate_trend_open_conditions(r.get("next_day_auction_gain"))
                    r["trend_open_ok"] = ok
                    r["trend_open_reason"] = reason
                    r["entry_close_next_day"] = round(n_close, 2)
                    changed = True
            entry = float(r.get("next_day_close") or r.get("entry_close_next_day") or 0)
            if r.get("d2_close_gain_pct") is None and entry > 0 and idx + 2 < len(df):
                d2_row = df.iloc[idx + 2]
                d2_open = float(d2_row["open"])
                d2_close = float(d2_row["close"])
                if d2_open > 0 and d2_close > 0:
                    r["d2_open"] = round(d2_open, 2)
                    r["d2_auction_gain_pct"] = round((d2_open / entry - 1) * 100, 2)
                    r["d2_close"] = round(d2_close, 2)
                    r["d2_close_gain_pct"] = round((d2_close / entry - 1) * 100, 2)
                    r["is_win_after_entry"] = r["d2_close_gain_pct"] > 0
                    changed = True
            if changed:
                updated += 1
                if i < n_file:
                    dirty_file = True
                else:
                    dirty_manual_extra = True
        except Exception:
            continue
    if dirty_file:
        dump_json_file(TREND_HISTORY_FILE, records[:n_file])
    if dirty_manual_extra and n_file < len(records):
        by_d = {_norm_trade_date_ymd(r.get("date")): r for r in records[n_file:]}
        new_manual: list[dict[str, Any]] = []
        for mr in manual_list:
            d = _norm_trade_date_ymd(mr.get("date"))
            if d in by_d:
                new_manual.append(dict(by_d[d]))
            else:
                new_manual.append(dict(mr))
        save_trend_history_manual_records(new_manual)
    return {"status": "ok", "updated": updated}


def backfill_trend_morning_auction(spot_df) -> dict:
    """竞价后（如 9:27）用实时开盘价回填趋势历史「次日竞价%」并判定是否满足温和开仓。

    收盘后 `backfill_trend_next_day` 会用日 K 再覆盖为更准的值；此处优先让看板当日可见竞价结论。
    """
    if spot_df is None or getattr(spot_df, "empty", True):
        return {"status": "skipped", "reason": "no spot"}

    records = load_json_file(TREND_HISTORY_FILE)
    if not isinstance(records, list):
        return {"status": "no_history"}

    today = now_cn().strftime("%Y-%m-%d")
    open_map: dict[str, float] = {}
    for _, row in spot_df.iterrows():
        code = str(row.get("code", ""))
        try:
            open_p = float(row.get("open", 0))
        except (TypeError, ValueError):
            continue
        if code and open_p > 0:
            open_map[code] = open_p

    updated = 0
    for r in records:
        if _norm_trade_date_ymd(r.get("date")) >= today:
            continue
        if r.get("next_day_auction_gain") is not None:
            continue
        code = str(r.get("code") or "")
        base_close = float(r.get("close") or 0)
        if not code or base_close <= 0:
            continue
        n_open = open_map.get(code)
        if not n_open or n_open <= 0:
            continue
        r["next_day_open"] = round(n_open, 2)
        r["next_day_auction_gain"] = round((n_open / base_close - 1) * 100, 2)
        ok, reason = evaluate_trend_open_conditions(r.get("next_day_auction_gain"))
        r["trend_open_ok"] = ok
        r["trend_open_reason"] = reason
        updated += 1

    if updated:
        dump_json_file(TREND_HISTORY_FILE, records)
        print(f"[趋势选股] 竞价回填次日开盘 {updated} 笔")
    return {"status": "ok", "updated": updated}

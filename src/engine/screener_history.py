"""选股记录模块

功能：
1. 每日9:27选股后归档当日选股结果
2. 每日15:30收盘后回填当日收盘价+日内表现
3. 次日9:27回填昨日记录的"次日竞价涨幅"
4. 按周/月/年/总统计胜率

数据结构（screener_history.json）：
[
  {
    "date": "2026-04-17",
    "code": "002297",
    "name": "博云新材",
    "continuous_limit_up": 3,
    "open_price": 25.5,         # 竞价开盘价
    "auction_gain": 5.2,        # 竞价涨幅%
    "close_price": null,        # 当日收盘价（15:30回填）
    "close_gain": null,         # 日内涨幅 (close/open - 1)*100（15:30回填）
    "next_day_open": null,      # 次日开盘价（次日9:27回填）
    "next_day_auction_gain": null,  # 次日竞价涨幅 vs 今收（次日9:27回填）
    "is_win": null,             # 胜负（close_gain > 0）
    "status": "pending"         # pending→closed→settled
  }
]
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.config import DATA_DIR, now_cn


HISTORY_FILE = DATA_DIR / "screener_history.json"


def _load() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return []


def _save(records: list[dict]):
    HISTORY_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2))


def archive_today_hits(hits: list[dict]):
    """9:27选股后归档当日结果

    Args:
        hits: ScreenerHit 的 asdict() 列表
    """
    if not hits:
        return

    records = _load()
    today = now_cn().strftime("%Y-%m-%d")

    # 去重：同一天同一只股票不重复写入
    existing = {(r["date"], r["code"]) for r in records}

    new_count = 0
    for h in hits:
        key = (today, h.get("code", ""))
        if key in existing:
            continue
        records.append({
            "date": today,
            "code": h.get("code", ""),
            "name": h.get("name", ""),
            "continuous_limit_up": h.get("continuous_limit_up", 0),
            "open_price": h.get("open_price", 0),
            "auction_gain": h.get("auction_gain", 0),
            "close_price": None,
            "close_gain": None,
            "next_day_open": None,
            "next_day_auction_gain": None,
            "is_win": None,
            "status": "pending",
        })
        existing.add(key)
        new_count += 1

    _save(records)
    print(f"[选股记录] 归档 {new_count} 只 ({today})")


def backfill_close(spot_df):
    """15:30收盘后回填当日收盘价

    Args:
        spot_df: 全市场实时行情 DataFrame（含 code, close 列）
    """
    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    updated = 0

    # 构建 code→close 映射
    if spot_df is not None and not spot_df.empty:
        price_map = dict(zip(spot_df["code"].astype(str), spot_df["close"].astype(float)))
    else:
        price_map = {}

    for r in records:
        if r["date"] == today and r["status"] == "pending":
            code = r["code"]
            close = price_map.get(code)
            if close and close > 0:
                open_p = r.get("open_price", 0)
                r["close_price"] = round(close, 2)
                r["close_gain"] = round((close / open_p - 1) * 100, 2) if open_p > 0 else 0
                r["is_win"] = r["close_gain"] > 0
                r["status"] = "closed"
                updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] 回填收盘价 {updated} 只 ({today})")


def backfill_next_day_auction(spot_df):
    """次日9:27回填昨日记录的次日竞价涨幅

    Args:
        spot_df: 今日实时行情（竞价后）
    """
    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    updated = 0

    if spot_df is not None and not spot_df.empty:
        price_map = {}
        for _, row in spot_df.iterrows():
            code = str(row.get("code", ""))
            open_p = float(row.get("open", 0))
            if code and open_p > 0:
                price_map[code] = open_p
    else:
        price_map = {}

    for r in records:
        # 找 status=closed（昨天已回填收盘但还没填次日竞价）的记录
        if r["status"] == "closed" and r["date"] != today:
            code = r["code"]
            next_open = price_map.get(code)
            close_p = r.get("close_price", 0)
            if next_open and next_open > 0 and close_p and close_p > 0:
                r["next_day_open"] = round(next_open, 2)
                r["next_day_auction_gain"] = round((next_open / close_p - 1) * 100, 2)
                r["status"] = "settled"
                updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] 回填次日竞价 {updated} 只")


def calc_win_stats() -> dict:
    """计算胜率统计

    Returns:
        {
            "total": {"trades": N, "wins": N, "win_rate": X.X},
            "weekly": {"trades": N, "wins": N, "win_rate": X.X},
            "monthly": {"trades": N, "wins": N, "win_rate": X.X},
            "yearly": {"trades": N, "wins": N, "win_rate": X.X},
        }
    """
    records = _load()
    now = now_cn()
    today = now.strftime("%Y-%m-%d")

    # 只统计已有胜负判定的记录
    judged = [r for r in records if r.get("is_win") is not None]

    def _stat(subset):
        total = len(subset)
        wins = sum(1 for r in subset if r["is_win"])
        return {
            "trades": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        }

    # 本周（周一起）
    weekday = now.weekday()
    monday = (now - timedelta(days=weekday)).strftime("%Y-%m-%d")
    weekly = [r for r in judged if r["date"] >= monday]

    # 本月
    month_start = now.strftime("%Y-%m-01")
    monthly = [r for r in judged if r["date"] >= month_start]

    # 本年
    year_start = now.strftime("%Y-01-01")
    yearly = [r for r in judged if r["date"] >= year_start]

    return {
        "total": _stat(judged),
        "weekly": _stat(weekly),
        "monthly": _stat(monthly),
        "yearly": _stat(yearly),
    }


def get_history(limit: int = 200) -> list[dict]:
    """获取选股记录（按日期降序）"""
    records = _load()
    records.sort(key=lambda r: r["date"], reverse=True)
    return records[:limit]

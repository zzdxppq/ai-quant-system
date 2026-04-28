"""集合竞价明细数据获取

数据源优先级（用户机器对 push2.eastmoney.com 偶尔被阻断，故有降级链）:
1. 东方财富 push2 stock/details/get  - 首选，含 9:15-9:25 逐 tick 虚拟成交价
2. 新浪 stock_intraday_sina           - 兜底，9:25 集合竞价单点 + 9:30+ 连续撮合
3. 腾讯 stock_zh_a_tick_tx_js (AkShare) - 二级兜底，分笔历史

数据按 (code, date) 缓存到 data/auction_cache/{date}/{code}.json，
9:25 后竞价已固定，缓存直至下一交易日。

返回结构 (统一为 list of dict):
    {
        "time": "09:15:00",
        "price": 12.24,                # 价格 (元)
        "matched_vol": 12000,          # 已匹配成交量 (手)
        "unmatched_vol": 5000,         # 未匹配挂单量 (手) — push2 才有，其它源为 0
        "direction": "buy"|"sell"|"neutral",  # 买卖方向 (买盘多/卖盘多/平衡)
    }
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from src.config import DATA_DIR, now_cn

CACHE_DIR = DATA_DIR / "auction_cache"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0",
    "Referer": "https://quote.eastmoney.com/",
}


def _market_prefix(code: str) -> str:
    """东方财富 secid 前缀: 沪市 1, 深市 0, 北交所 0"""
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "9")):
        return "1"
    return "0"


def _cache_path(code: str, date: str) -> Path:
    d = CACHE_DIR / date
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{code}.json"


def _load_cache(code: str, date: str) -> Optional[dict]:
    p = _cache_path(code, date)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _save_cache(code: str, date: str, payload: dict) -> None:
    try:
        _cache_path(code, date).write_text(
            json.dumps(payload, ensure_ascii=False)
        )
    except Exception as e:
        print(f"[竞价] 缓存写入失败 {code}: {e}")


# ============================================================
# 数据源 1: 东方财富 push2 — 含 9:15-9:25 逐 tick
# ============================================================

def _fetch_eastmoney_ticks(code: str, retry: int = 2, timeout: int = 15) -> Optional[list[dict]]:
    """从 push2.eastmoney.com 拉竞价 + 盘中 tick 明细

    返回 None 表示连不上（用于触发降级），返回 [] 表示连得上但无数据。
    """
    secid = f"{_market_prefix(code)}.{str(code).zfill(6)}"
    url = (
        "https://push2.eastmoney.com/api/qt/stock/details/get"
        f"?secid={secid}"
        "&fields1=f1,f2,f3,f4"
        "&fields2=f51,f52,f53,f54,f55"
    )

    for attempt in range(retry + 1):
        try:
            with httpx.Client(timeout=timeout, headers=HEADERS, http2=False) as cli:
                r = cli.get(url)
                if r.status_code != 200 or not r.text.strip():
                    raise RuntimeError(f"status={r.status_code}")
                data = r.json().get("data", {}) or {}
                raw = data.get("details", []) or []
                if not raw:
                    return []

                ticks: list[dict] = []
                for line in raw:
                    parts = str(line).split(",")
                    if len(parts) < 5:
                        continue
                    t, price, vol, _kind, direction_code = parts[:5]
                    try:
                        ticks.append({
                            "time": t,
                            "price": float(price),
                            "matched_vol": int(float(vol)),
                            "unmatched_vol": 0,
                            "direction": _direction_label(direction_code),
                        })
                    except (ValueError, TypeError):
                        continue
                return ticks
        except Exception as e:
            if attempt >= retry:
                print(f"[竞价] eastmoney {code} 三次失败: {e}")
                return None
            time.sleep(0.8 * (attempt + 1))
    return None


def _direction_label(code: str) -> str:
    """eastmoney 方向编码: 1=买 2=卖 4=平/集合竞价"""
    code = str(code).strip()
    if code == "1":
        return "buy"
    if code == "2":
        return "sell"
    return "neutral"


# ============================================================
# 数据源 2: 新浪 (兜底) — 仅 9:25 集合竞价 + 9:30+ 分笔
# ============================================================

def _fetch_sina_ticks(code: str) -> Optional[list[dict]]:
    """新浪 stock_intraday_sina 通过 AkShare 调用。

    新浪只给 9:25 集合竞价单点 + 9:30 后连续撮合分笔，无 9:15-9:25 过程。
    """
    try:
        import akshare as ak
        sina_symbol = ("sh" if _market_prefix(code) == "1" else "sz") + str(code).zfill(6)
        today = now_cn().strftime("%Y%m%d")
        df = ak.stock_intraday_sina(symbol=sina_symbol, date=today)
        if df is None or df.empty:
            return []
        # 列：symbol, name, ticktime, price, volume, prev_price, kind
        # kind: U=上涨, D=下跌, E=平盘 (一般竞价撮合时无 buy/sell 区分)
        kind_map = {"U": "buy", "D": "sell", "E": "neutral"}
        out = []
        for _, row in df.iterrows():
            t = str(row.get("ticktime", ""))
            try:
                out.append({
                    "time": t,
                    "price": float(row.get("price", 0)),
                    # 新浪 volume 是股，转手 (1手=100股)
                    "matched_vol": int(float(row.get("volume", 0)) / 100),
                    "unmatched_vol": 0,
                    "direction": kind_map.get(str(row.get("kind", "")), "neutral"),
                })
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f"[竞价] 新浪兜底 {code} 失败: {e}")
        return None


# ============================================================
# 数据源 3: 腾讯 (二级兜底) — AkShare stock_zh_a_tick_tx_js
# ============================================================

def _fetch_tencent_ticks(code: str) -> Optional[list[dict]]:
    try:
        import akshare as ak
        tx_symbol = ("sh" if _market_prefix(code) == "1" else "sz") + str(code).zfill(6)
        df = ak.stock_zh_a_tick_tx_js(symbol=tx_symbol)
        if df is None or df.empty:
            return []
        # 列：成交时间, 成交价格, 价格变动, 成交量, 成交金额, 性质
        kind_map = {"买盘": "buy", "卖盘": "sell"}
        out = []
        for _, row in df.iterrows():
            try:
                out.append({
                    "time": str(row.get("成交时间", "")),
                    "price": float(row.get("成交价格", 0)),
                    "matched_vol": int(float(row.get("成交量", 0))),  # 腾讯单位本身就是手
                    "unmatched_vol": 0,
                    "direction": kind_map.get(str(row.get("性质", "")), "neutral"),
                })
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f"[竞价] 腾讯兜底 {code} 失败: {e}")
        return None


# ============================================================
# 对外主入口
# ============================================================

def fetch_auction_ticks(code: str, force: bool = False) -> dict:
    """获取一只股票的竞价明细（含降级 + 缓存）

    Args:
        code: 6 位代码
        force: True 时绕过缓存重新拉取

    Returns:
        {
            "code": "603318",
            "date": "20260427",
            "source": "eastmoney"|"sina"|"tencent"|"none",
            "ticks": [...],
            "auction_window": [...],   # 9:15-9:25 子集
            "intraday_window": [...],  # 9:25-9:30 子集（连续撮合衔接）
        }
    """
    code = str(code).zfill(6)
    today = now_cn().strftime("%Y%m%d")

    # 1) 缓存
    if not force:
        cached = _load_cache(code, today)
        if cached and cached.get("ticks"):
            return cached

    # 2) 拉数据 — 主路径 → 降级
    source = "none"
    ticks: list[dict] = []

    em_ticks = _fetch_eastmoney_ticks(code)
    if em_ticks:
        ticks = em_ticks
        source = "eastmoney"
    else:
        # 新浪
        sn_ticks = _fetch_sina_ticks(code)
        if sn_ticks:
            ticks = sn_ticks
            source = "sina"
        else:
            tx_ticks = _fetch_tencent_ticks(code)
            if tx_ticks:
                ticks = tx_ticks
                source = "tencent"

    # 3) 切分窗口
    # auction_window = 9:15-9:25:00 (虚拟成交价过程，仅 eastmoney 有)
    # intraday_window = 9:25-9:35  (集合竞价开盘那笔 + 之后 10 分钟，给前端衔接显示)
    auction_window = [t for t in ticks if _in_window(t["time"], "09:15:00", "09:25:01")]
    intraday_window = [t for t in ticks if _in_window(t["time"], "09:25:00", "09:35:00")]

    # 集合竞价开盘单点（任一数据源都有）
    open_tick = next((t for t in ticks if t["time"].startswith("09:25")), None)

    payload = {
        "code": code,
        "date": today,
        "source": source,
        "tick_count": len(ticks),
        "auction_window": auction_window,
        "intraday_window": intraday_window,
        "open_tick": open_tick,
    }

    if source != "none":
        _save_cache(code, today, payload)

    return payload


def _in_window(t: str, start: str, end: str) -> bool:
    """判断 t (HH:MM:SS) 是否在 [start, end) 区间"""
    return start <= t < end

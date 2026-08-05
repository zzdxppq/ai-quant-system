"""腾讯分时数据（与 go-stock GetStockMinutePriceData 同源）

接口: https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh600000
每根: 「HHMM 价 累计量(手) 累计额(元)」——累计额用于算均价(VWAP)。
"""
from __future__ import annotations

from typing import Any

import httpx

from src.config import now_cn
from src.data.kline_file_cache import normalize_code6
from src.data.tencent_api import _to_tencent_code
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
HEADERS = {
    "Host": "web.ifzq.gtimg.cn",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    ),
}


def _resolve_pre_close(code6: str) -> float:
    """昨收：新浪实时 → 多源日 K 推导（与 K 线分析同源）。"""
    from src.data.sina_kline_api import resolve_prev_close

    return resolve_prev_close(code6)


def _parse_time(hhmm: str) -> str:
    s = (hhmm or "").strip()
    if len(s) >= 4 and s.isdigit():
        return f"{s[:2]}:{s[2:4]}"
    if len(s) == 3 and s.isdigit() and s[0] == "9":
        return f"9:{s[1:3]}"
    return s


def fetch_minute_payload(code6: str) -> dict[str, Any]:
    """拉取当日分时并组装为可 JSON 序列化结构（供 API + SQLite）。

    Returns:
        dict: code, symbol, trade_date, pre_close, bars, cached_at, source
        失败时含 error 字段（str）。
    """
    code6 = normalize_code6(code6)
    if len(code6) != 6 or not code6.isdigit():
        return {"error": "invalid code", "code": code6}

    symbol = _to_tencent_code(code6)
    try:
        with httpx.Client(timeout=12, headers=HEADERS) as client:
            resp = client.get(MINUTE_URL, params={"code": symbol})
            root = resp.json()
    except Exception as e:
        return {"error": f"minute http: {e}", "code": code6}

    if not isinstance(root, dict) or int(root.get("code", -1)) != 0:
        return {"error": "minute api code != 0", "code": code6}

    data = root.get("data") or {}
    if not isinstance(data, dict) or symbol not in data:
        return {"error": "minute empty data", "code": code6}

    node = data.get(symbol) or {}
    inner = (node.get("data") or {}) if isinstance(node, dict) else {}
    if not isinstance(inner, dict):
        return {"error": "minute bad shape", "code": code6}

    trade_date = str(inner.get("date") or "").strip()
    raw_list = inner.get("data")
    if not isinstance(raw_list, list) or not raw_list:
        return {"error": "no minute bars", "code": code6}

    pre_close = _resolve_pre_close(code6)

    bars: list[dict[str, Any]] = []
    prev_cum_lot = 0.0
    lot_unit = 100.0  # 1 手 = 100 股

    for item in raw_list:
        if not isinstance(item, str):
            continue
        parts = item.replace("\r\n", " ").split()
        if len(parts) < 3:
            continue
        hhmm = parts[0].strip()
        try:
            price = float(parts[1])
            cum_lot = float(parts[2])
        except (ValueError, TypeError):
            continue
        amount = 0.0
        if len(parts) >= 4:
            try:
                amount = float(parts[3])
            except (ValueError, TypeError):
                amount = 0.0

        vol_bar = cum_lot - prev_cum_lot if prev_cum_lot > 0 else cum_lot
        if vol_bar < 0:
            vol_bar = 0.0
        prev_cum_lot = cum_lot

        shares = cum_lot * lot_unit
        avg = (amount / shares) if shares > 0 else price

        pct = ((price / pre_close) - 1.0) * 100.0 if pre_close > 0 else 0.0
        avg_pct = ((avg / pre_close) - 1.0) * 100.0 if pre_close > 0 else 0.0

        bars.append({
            "t": _parse_time(hhmm),
            "p": round(price, 4),
            "avg": round(avg, 4),
            "vol_bar": round(vol_bar, 2),
            "cum_lot": round(cum_lot, 2),
            "amount": round(amount, 2),
            "pct": round(pct, 3),
            "avg_pct": round(avg_pct, 3),
        })

    if not bars:
        return {"error": "parsed zero bars", "code": code6}

    return {
        "code": code6,
        "symbol": symbol,
        "trade_date": trade_date,
        "pre_close": round(pre_close, 4) if pre_close > 0 else None,
        "cached_at": now_cn().isoformat(),
        "source": "tencent_minute",
        "bars": bars,
    }


def load_latest_minute_store(code6: str) -> dict[str, Any] | None:
    """从 quant 库 minute_kline 读取该股最近一次落盘的分时。"""
    from src.data.structured_store import load_latest_minute_payload

    c = normalize_code6(code6)
    if len(c) != 6:
        return None
    try:
        got = load_latest_minute_payload(c)
    except Exception as e:
        print(f"[minute] load cache failed {c}: {e}")
        try:
            from src.data.quant_db import reset_shared_connection

            reset_shared_connection()
        except Exception:
            pass
        return None
    return got if isinstance(got, dict) and isinstance(got.get("bars"), list) and len(got["bars"]) > 0 else None


def _minute_cache_usable(cached: dict[str, Any] | None) -> bool:
    if not cached or not isinstance(cached.get("bars"), list):
        return False
    return len(cached["bars"]) >= 8


def resolve_minute_payload_cached_then_network(
    code: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """先读库内分时；默认有缓存则立即返回，避免每次打开都等腾讯拉网。

    force_refresh=True 时强制拉腾讯（定时刷新 / 用户点刷新）。
    成功字段：
      - served_from_cache: 本次响应是否来自库内缓存（未等网络或网络失败回退）
      - refresh_error: served_from_cache 时可能记录拉网失败摘要
    """
    code6 = normalize_code6(code)
    if len(code6) != 6 or not code6.isdigit():
        return {"error": "invalid code", "code": code6}

    cached = load_latest_minute_store(code6)

    if not force_refresh and _minute_cache_usable(cached):
        out = dict(cached)
        out["served_from_cache"] = True
        out["refresh_error"] = None
        return out

    net = fetch_minute_payload(code6)
    ok_net = not net.get("error") and isinstance(net.get("bars"), list) and len(net["bars"]) > 0
    if ok_net:
        net["served_from_cache"] = False
        net["refresh_error"] = None
        return net

    if _minute_cache_usable(cached):
        out = dict(cached)
        out["served_from_cache"] = True
        out["refresh_error"] = str(net.get("error") or "") or None
        out["refresh_attempt_at"] = now_cn().isoformat()
        return out

    return net

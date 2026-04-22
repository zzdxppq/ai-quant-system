"""腾讯财经行情接口 qt.gtimg.cn

用途：作为东方财富被 WAF 封禁时，补全新浪 spot 缺失的字段
    - market_cap (流通市值，亿元)
    - total_market_cap (总市值，亿元)
    - turnover (换手率 %)
    - volume_ratio (量比)
    - pe / pb

接口格式：http://qt.gtimg.cn/q=sh600743,sz000534  (返回 v_<code>="..." 的 GBK 文本)
"""
import time
from typing import Iterable

import httpx
import pandas as pd

TENCENT_URL = "http://qt.gtimg.cn/q="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://finance.qq.com/",
}

# Tencent 响应字段下标（以 ~ 分割）
# 参考常见文档与实测
IDX = {
    "name": 1,
    "code": 2,
    "close": 3,          # 当前价
    "pre_close": 4,
    "open": 5,
    "volume_lots": 6,    # 成交量（手）
    "change_amount": 31,
    "change_pct": 32,
    "high": 33,
    "low": 34,
    "volume_lots_2": 36,
    "amount_wan": 37,    # 成交额（万元）
    "turnover": 38,      # 换手率 %
    "pe": 39,
    "amplitude": 43,
    "market_cap": 44,    # 流通市值（亿元）
    "total_cap": 45,     # 总市值（亿元）
    "pb": 46,
    "volume_ratio": 56,  # 量比
}


def _to_tencent_code(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("60", "68", "90", "110", "113")):
        return f"sh{code}"
    if code.startswith("5"):
        return f"sh{code}"
    if code.startswith(("4", "8", "92")):
        return f"bj{code}"
    return f"sz{code}"


def _safe_float(s: str) -> float:
    try:
        return float(s) if s else 0.0
    except (ValueError, TypeError):
        return 0.0


def fetch_stock_details(codes: Iterable[str], batch_size: int = 50) -> pd.DataFrame:
    """批量拉腾讯行情补全字段

    Returns:
        DataFrame 列: code, name, close, pre_close, open, volume, amount,
                      change_pct, high, low, turnover, market_cap, total_cap,
                      volume_ratio, pe, pb
        其中 market_cap/total_cap 单位为「亿元」
    """
    code_list = list(codes)
    if not code_list:
        return pd.DataFrame()

    rows: list[dict] = []
    with httpx.Client(timeout=10, headers=HEADERS) as client:
        for i in range(0, len(code_list), batch_size):
            batch = code_list[i : i + batch_size]
            q = ",".join(_to_tencent_code(c) for c in batch)
            url = TENCENT_URL + q
            try:
                resp = client.get(url)
                resp.encoding = "gbk"
                text = resp.text
            except Exception as e:
                print(f"腾讯接口失败: {e}")
                continue

            for line in text.splitlines():
                line = line.strip().rstrip(";")
                if not line or "=" not in line:
                    continue
                # 形如: v_sh600743="1~华远控股~600743~..."
                try:
                    _, value = line.split("=", 1)
                    value = value.strip().strip('"')
                    parts = value.split("~")
                    if len(parts) < 50:
                        continue
                    raw_code = parts[IDX["code"]]
                    rows.append({
                        "code": raw_code,
                        "name": parts[IDX["name"]],
                        "close": _safe_float(parts[IDX["close"]]),
                        "pre_close": _safe_float(parts[IDX["pre_close"]]),
                        "open": _safe_float(parts[IDX["open"]]),
                        "volume": _safe_float(parts[IDX["volume_lots"]]) * 100,  # 手→股
                        "amount": _safe_float(parts[IDX["amount_wan"]]) * 1e4,  # 万元→元
                        "change_pct": _safe_float(parts[IDX["change_pct"]]),
                        "high": _safe_float(parts[IDX["high"]]),
                        "low": _safe_float(parts[IDX["low"]]),
                        "turnover": _safe_float(parts[IDX["turnover"]]),
                        "market_cap_yi": _safe_float(parts[IDX["market_cap"]]),
                        "total_cap_yi": _safe_float(parts[IDX["total_cap"]]),
                        "volume_ratio": _safe_float(parts[IDX["volume_ratio"]]),
                        "pe": _safe_float(parts[IDX["pe"]]),
                        "pb": _safe_float(parts[IDX["pb"]]),
                    })
                except Exception:
                    continue
            # 轻量节流
            if i + batch_size < len(code_list):
                time.sleep(0.1)

    return pd.DataFrame(rows)


def enrich_screener_hits(hits: list, market_cap_min: float = 20.0,
                         market_cap_max: float = 100.0,
                         volume_ratio_min: float = 0.9) -> list:
    """对 screener hits 调腾讯接口补全字段并做严格二次过滤

    补全：market_cap(亿) / volume_ratio / auction_turnover(%)
    过滤：流通市值 > market_cap_max 亿，或 量比 < volume_ratio_min 的剔除
          （对齐通达信公式 PD4/DYNAINFO(17) 条件）

    注意：由于该接口在盘中返回的是「当前」累计数据（非竞价瞬间），
    在非 9:27 场景下 turnover/volume_ratio 会偏高，但对过滤阈值意义不变

    Args:
        hits: list[ScreenerHit]，会被就地修改
    Returns:
        过滤后的 hits 列表
    """
    if not hits:
        return hits

    codes = [h.code for h in hits]
    df = fetch_stock_details(codes)
    if df.empty:
        print("  腾讯富化失败，保留原 hits")
        return hits

    info = {str(row["code"]).strip(): row for _, row in df.iterrows()}

    filtered = []
    for h in hits:
        row = info.get(str(h.code).strip())
        if row is None:
            # 腾讯查不到，保留但不富化
            filtered.append(h)
            continue

        mc = float(row.get("market_cap_yi", 0))
        vr = float(row.get("volume_ratio", 0))
        tr = float(row.get("turnover", 0))

        # 富化 hit 展示字段
        if mc > 0:
            h.market_cap = round(mc, 2)
        if vr > 0:
            h.volume_ratio = round(vr, 2)
        if tr > 0:
            h.auction_turnover = round(tr, 2)

        # 严格二次过滤（补齐字段后才能执行）
        # PD4: 流通市值 20~100亿
        if mc > 0:
            if mc > market_cap_max:
                print(f"  剔除 {h.code} {h.name}: 流通市值 {mc:.1f}亿 > {market_cap_max}亿")
                continue
            if mc < market_cap_min:
                print(f"  剔除 {h.code} {h.name}: 流通市值 {mc:.1f}亿 < {market_cap_min}亿")
                continue
        if vr > 0 and vr < volume_ratio_min:
            print(f"  剔除 {h.code} {h.name}: 量比 {vr:.2f} < {volume_ratio_min}")
            continue

        filtered.append(h)

    return filtered

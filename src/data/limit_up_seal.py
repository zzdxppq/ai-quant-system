"""A 股涨停封板判定（按涨停价，而非涨幅≥9.8% 近似）。

差一档（如收盘 9.99% 但未触及涨停价）不得计入连板/涨停池。
对齐通达信 ZTPRICE(REF(C,1),0.1) 四舍五入到分。
"""
from __future__ import annotations

from typing import Any, Optional


def limit_up_ratio(code: str) -> float:
    c = "".join(ch for ch in str(code or "") if ch.isdigit())[-6:].zfill(6)
    if c.startswith(("300", "301", "688")):
        return 1.20
    return 1.10


def limit_up_price(code: str, pre_close: float) -> Optional[float]:
    try:
        pre = float(pre_close)
    except (TypeError, ValueError):
        return None
    if pre <= 0:
        return None
    return round(pre * limit_up_ratio(code), 2)


def is_limit_up_sealed(
    code: str,
    pre_close: Any = None,
    close: Any = None,
    *,
    tol: float = 0.005,
) -> bool:
    """收盘价是否封住涨停价（允许半分钱浮点误差）。"""
    try:
        pre = float(pre_close) if pre_close is not None else 0.0
        cl = float(close) if close is not None else 0.0
    except (TypeError, ValueError):
        return False
    lim = limit_up_price(code, pre)
    if lim is None or cl <= 0:
        return False
    return cl + 1e-9 >= lim - tol


def is_limit_up_row(row: Any, *, code_key: str = "code") -> bool:
    """DataFrame/Series/dict 行是否收盘封板。优先 close/pre_close，无价则不认纯涨幅阈值。"""
    try:
        if hasattr(row, "get"):
            code = row.get(code_key) or row.get("stock_code")
            pre = row.get("pre_close")
            close = row.get("close")
        else:
            code = row[code_key] if code_key in getattr(row, "index", []) else None
            pre = row["pre_close"] if "pre_close" in getattr(row, "index", []) else None
            close = row["close"] if "close" in getattr(row, "index", []) else None
    except Exception:
        return False
    if pre is not None and close is not None:
        try:
            if float(pre) > 0 and float(close) > 0:
                return is_limit_up_sealed(str(code or ""), pre, close)
        except (TypeError, ValueError):
            pass
    return False

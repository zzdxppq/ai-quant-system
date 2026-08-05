#!/usr/bin/env python3
"""从 Excel 导入历史选股记录 → screener_history_entry。

列序（与「量化交易数据补全.xlsx」首表一致）：
  日期, 三日1进2涨停数%, 当日标的, 代码, 连板前连板, 开盘价, 竞价涨幅%,
  当日收盘价, 当日收盘涨幅%, 次日竞价涨幅%, 次日收盘涨幅%, 10日涨幅%, 胜负

用法（项目根目录）：
  python scripts/import_screener_history_from_excel.py "d:\\量化交易数据补全.xlsx"
  python scripts/import_screener_history_from_excel.py --min-date 2025-06-20
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _norm_code(raw) -> str:
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not d:
        return ""
    return d[-6:].zfill(6)


def _norm_date(raw) -> str:
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return ""
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, date):
        return raw.isoformat()
    s = str(raw).strip()[:10]
    if len(s) == 10 and s[4] == "-":
        return s
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, 4)
    except (TypeError, ValueError):
        return None


def _parse_win(v) -> bool | None:
    s = str(v or "").strip()
    if s in ("胜", "赢", "盈", "Y", "y", "1", "true", "True"):
        return True
    if s in ("负", "亏", "败", "N", "n", "0", "false", "False"):
        return False
    return None


def _row_to_record(row) -> dict | None:
    trade_date = _norm_date(row.iloc[0])
    code = _norm_code(row.iloc[3])
    if len(trade_date) != 10 or len(code) != 6:
        return None

    b1_rate = _f(row.iloc[1])
    name = str(row.iloc[2] or "").strip()
    board = int(_f(row.iloc[4]) or 0)
    open_p = _f(row.iloc[5])
    auction_gain = _f(row.iloc[6])
    close_price = _f(row.iloc[7])
    day_change = _f(row.iloc[8])
    next_day_auction_gain = _f(row.iloc[9])
    next_day_close_gain = _f(row.iloc[10])
    gain_10d = _f(row.iloc[11])
    is_win = _parse_win(row.iloc[12])

    pre_close = None
    if open_p and auction_gain is not None and auction_gain > -100:
        pre_close = round(open_p / (1 + auction_gain / 100), 4)
    close_gain = None
    if open_p and close_price and open_p > 0:
        close_gain = round((close_price / open_p - 1) * 100, 2)

    limit_th = 19.5 if code.startswith(("300", "301", "688")) else 9.8
    is_limit_up = None
    if day_change is not None:
        is_limit_up = day_change >= limit_th

    board_label = f"{board}进{board + 1}" if board >= 1 else "首板"
    status = "settled" if is_win is not None else "closed"

    return {
        "date": trade_date,
        "code": code,
        "name": name,
        "continuous_limit_up": board,
        "board_label": board_label,
        "open_price": open_p,
        "pre_close": pre_close,
        "auction_gain": auction_gain,
        "close_price": close_price,
        "close_gain": close_gain,
        "day_change": day_change,
        "next_day_auction_gain": next_day_auction_gain,
        "next_day_close_gain": next_day_close_gain,
        "gain_10d": gain_10d,
        "is_win": is_win,
        "is_limit_up": is_limit_up,
        "is_zhaban": False,
        "status": status,
        "b1_rate": b1_rate,
        "sanbanzhu": False,
        "sanbanzhu_detail": "",
        "matched_cycle": False,
        "is_cycle_stock": False,
        "import_source": "excel",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Excel → screener_history_entry")
    ap.add_argument(
        "excel",
        nargs="?",
        default=r"d:\量化交易数据补全.xlsx",
        help="xlsx 路径",
    )
    ap.add_argument("--sheet", default=0, help="工作表名或索引")
    ap.add_argument("--min-date", default="2025-06-20", help="仅导入该日（含）之后")
    args = ap.parse_args()

    import pandas as pd

    path = Path(args.excel)
    if not path.is_file():
        print(f"文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    df = pd.read_excel(path, sheet_name=args.sheet)
    min_d = _norm_date(args.min_date)
    if len(min_d) != 10:
        print("无效 --min-date", file=sys.stderr)
        sys.exit(1)

    from src.data.analytics_store import (
        load_screener_history_entries,
        replace_screener_history_entries,
        backfill_daily_screener_hit_from_history,
    )

    existing = load_screener_history_entries()
    by_key: dict[tuple[str, str], dict] = {}
    for r in existing:
        d = _norm_date(r.get("date"))
        c = _norm_code(r.get("code"))
        if len(d) == 10 and len(c) == 6:
            by_key[(d, c)] = r

    imported = 0
    skipped = 0
    for _, row in df.iterrows():
        rec = _row_to_record(row)
        if not rec:
            skipped += 1
            continue
        if rec["date"] < min_d:
            continue
        by_key[(rec["date"], rec["code"])] = rec
        imported += 1

    merged = sorted(by_key.values(), key=lambda x: (x["date"], x["code"]))
    replace_screener_history_entries(merged)
    n_hit = backfill_daily_screener_hit_from_history(min_iso_date=min_d[:10])

    wins = sum(1 for r in merged if r.get("is_win") is True)
    losses = sum(1 for r in merged if r.get("is_win") is False)
    pending = sum(1 for r in merged if r.get("is_win") is None)
    print(
        f"Excel 解析 {imported} 行（跳过 {skipped} 行无效）；"
        f"库内合计 {len(merged)} 条；胜 {wins} / 负 {losses} / 待定 {pending}；"
        f"daily_screener_hit 回填 {n_hit} 行"
    )


if __name__ == "__main__":
    main()

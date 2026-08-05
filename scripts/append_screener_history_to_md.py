#!/usr/bin/env python3
"""将 DuckDB screener_history_entry 中指定日期之后的记录追加到 量化交易数据补全.md。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLUMNS = [
    "日期",
    "昨日1进2晋级率%",
    "今日标的",
    "代码",
    "开盘前连板",
    "开盘价",
    "竞价涨幅%",
    "当日收盘价",
    "当日收盘涨幅%",
    "次日竞价涨幅%",
    "次日收盘涨幅%",
    "10日涨幅%",
    "胜负",
]


def _fmt_num(v) -> str:
    if v is None:
        return ""
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return ""
    except (TypeError, ValueError):
        return ""
    if abs(x - round(x)) < 1e-6:
        return str(int(round(x)))
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _fmt_code(code: str) -> str:
    c = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not c:
        return ""
    c = c[-6:].zfill(6)
    return str(int(c))


def _fmt_win(is_win) -> str:
    if is_win is True:
        return "胜"
    if is_win is False:
        return "负"
    return ""


def record_to_row(r: dict) -> str:
    cells = [
        str(r.get("date") or "")[:10],
        _fmt_num(r.get("b1_rate")),
        str(r.get("name") or "").strip(),
        _fmt_code(r.get("code")),
        _fmt_num(r.get("continuous_limit_up")),
        _fmt_num(r.get("open_price")),
        _fmt_num(r.get("auction_gain")),
        _fmt_num(r.get("close_price")),
        _fmt_num(r.get("day_change")),
        _fmt_num(r.get("next_day_auction_gain")),
        _fmt_num(r.get("next_day_close_gain")),
        _fmt_num(r.get("gain_10d")),
        _fmt_win(r.get("is_win")),
    ]
    return "| " + " | ".join(cells) + " |"


def append_after_date(md_path: Path, min_exclusive: str = "2026-04-22") -> int:
    from src.data.analytics_store import load_screener_history_entries

    recs = [
        r
        for r in load_screener_history_entries()
        if (r.get("date") or "")[:10] > min_exclusive
    ]
    if not recs:
        print(f"无 date > {min_exclusive} 的选股记录")
        return 0

    text = md_path.read_text(encoding="utf-8")
    marker = "## Sheet2"
    if marker not in text:
        raise SystemExit(f"{md_path} 中未找到 {marker}")

    existing_dates = set()
    for line in text.splitlines():
        if line.startswith("| 20") and line.count("|") >= 12:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) > 2 and parts[1][:4] == "202":
                existing_dates.add((parts[1], parts[4]))  # date, code

    new_lines: list[str] = []
    skipped = 0
    for r in recs:
        key = (str(r.get("date") or "")[:10], _fmt_code(r.get("code")))
        if key in existing_dates:
            skipped += 1
            continue
        new_lines.append(record_to_row(r))

    if not new_lines:
        print(f"共 {len(recs)} 条，均已存在于 {md_path.name}，跳过 {skipped}")
        return 0

    block = "\n".join(new_lines) + "\n"
    head, tail = text.split(marker, 1)
    head = head.rstrip("\n") + "\n"
    new_text = head + block + marker + tail
    md_path.write_text(new_text, encoding="utf-8")
    print(f"已追加 {len(new_lines)} 行 → {md_path}（库内 {len(recs)} 条，跳过已存在 {skipped}）")
    return len(new_lines)


if __name__ == "__main__":
    after = sys.argv[1] if len(sys.argv) > 1 else "2026-04-22"
    targets = [
        ROOT / "data" / "量化交易数据补全.md",
        ROOT / "docs" / "量化交易数据补全.md",
    ]
    for p in targets:
        if p.exists():
            append_after_date(p, after)
        else:
            print(f"跳过（不存在）: {p}")

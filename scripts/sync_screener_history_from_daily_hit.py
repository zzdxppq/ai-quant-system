#!/usr/bin/env python3
"""补写 screener_history_entry（缺日/缺股时执行一次即可）。

数据源优先级:
  1. daily_screener_hit
  2. latest_screener / decision_records → archive_today_hits
  3. daily_auction_scores（决策卡兜底，daily_hit 被 0 命中跑数清空时）

用法（项目根 / 容器内）:
  python scripts/sync_screener_history_from_daily_hit.py --date 20260525
  python scripts/sync_screener_history_from_daily_hit.py --date 20260525 --diagnose
  python scripts/sync_screener_history_from_daily_hit.py --date 20260525 --all-sources

若 DuckDB 报「文件正被占用」，请先 docker stop quant-ai 再执行。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="补写 screener_history_entry")
    ap.add_argument(
        "--date",
        action="append",
        default=[],
        metavar="YYYYMMDD",
        help="交易日（可多次）；支持 20260514 或 2026-05-14",
    )
    ap.add_argument("--diagnose", action="store_true", help="仅打印各表条数，不写库")
    ap.add_argument(
        "--all-sources",
        action="store_true",
        help="daily_hit 为 0 时继续尝试 latest/decision/auction 兜底（默认开启）",
    )
    ap.add_argument(
        "--daily-hit-only",
        action="store_true",
        help="仅 daily_screener_hit → history（旧行为）",
    )
    args = ap.parse_args()
    if not args.date:
        print("请指定 --date，例如: --date 20260525", file=sys.stderr)
        return 1

    from src.data.models import init_db

    init_db()

    from src.data.analytics_store import (
        diagnose_screener_history_sync,
        sync_screener_history_all_sources,
        sync_screener_history_from_daily_hit,
    )

    for d in args.date:
        if args.diagnose:
            diag = diagnose_screener_history_sync(hit_date_yyyymmdd=d)
            print(f"[diagnose {d}] {diag}")
            continue

        if args.daily_hit_only:
            n = sync_screener_history_from_daily_hit(hit_dates_yyyymmdd=[d])
            print(f"[daily_hit {d}] 已补写 {n} 条")
            continue

        use_all = args.all_sources or not args.daily_hit_only
        if use_all:
            diag_before = diagnose_screener_history_sync(hit_date_yyyymmdd=d)
            if diag_before.get("screener_history_entry", 0) > 0:
                print(f"[{d}] history 已有 {diag_before['screener_history_entry']} 条，跳过")
                continue
            if diag_before.get("daily_screener_hit", 0) == 0:
                print(f"[{d}] daily_screener_hit=0，尝试 decision/auction 兜底 …")
            res = sync_screener_history_all_sources(hit_dates_yyyymmdd=[d])
            total = res["daily_hit"] + res["archive"] + res["auction"]
            print(
                f"[{d}] 补写合计 {total} 条 "
                f"(daily_hit={res['daily_hit']} archive={res['archive']} auction={res['auction']})"
            )
            if total == 0:
                print(f"[{d}] 仍无数据，诊断: {diag_before}")
                return 1
        else:
            n = sync_screener_history_from_daily_hit(hit_dates_yyyymmdd=[d])
            print(f"已补写 {n} 条")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""按 limit_up_cache 校正 screener_history 连板数（回测明细 2进3→3进4 等）。

用法:
  python scripts/reconcile_screener_history_board.py --date 2026-05-25
  python scripts/reconcile_screener_history_board.py --date 20260525 --all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="校正选股历史连板数/决策标签")
    ap.add_argument("--date", action="append", default=[], help="YYYY-MM-DD 或 YYYYMMDD，可多次")
    ap.add_argument("--all", action="store_true", help="全量历史（慎用）")
    args = ap.parse_args()

    from src.data.models import init_db
    from src.engine.screener_history import reconcile_history_board_counts

    init_db()
    dates = None if args.all else (args.date or None)
    if not args.all and not dates:
        print("请指定 --date 2026-05-25 或 --all", file=sys.stderr)
        return 1
    n = reconcile_history_board_counts(trade_dates=dates)
    print(f"已校正 {n} 条")
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

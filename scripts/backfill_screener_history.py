#!/usr/bin/env python3
"""将 screener_history.json 导入 `screener_history_entry`，并回填 `daily_screener_hit`（默认 >=2026-04-17）。

用法（在项目根目录）：
  python scripts/backfill_screener_history.py
  python scripts/backfill_screener_history.py --json E:/path/to/screener_history.json
  python scripts/backfill_screener_history.py --min-date 2026-04-17
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DECODE = ("utf-8", "utf-8-sig", "gb18030", "gbk")


def _load_list(path: Path) -> list[dict] | None:
    if not path.is_file():
        return None
    raw = path.read_bytes()
    for enc in _DECODE:
        try:
            obj = json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(obj, list):
            return obj
        return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="screener_history → analytics 表 + daily_screener_hit")
    ap.add_argument("--json", type=Path, default=None, help="指定 screener_history.json；省略则依次尝试 data 与最新 backups")
    ap.add_argument("--min-date", default="2026-04-17", help="回填 daily_screener_hit 的起始日（含）")
    args = ap.parse_args()

    from src.config import DATA_DIR

    candidates: list[Path] = []
    if args.json:
        candidates.append(args.json.resolve())
    else:
        candidates.append((DATA_DIR / "screener_history.json").resolve())
        backups = ROOT / "backups"
        if backups.is_dir():
            subdirs = sorted(backups.glob("data_*"), reverse=True)
            for d in subdirs:
                p = d / "screener_history.json"
                if p.is_file():
                    candidates.append(p.resolve())

    rows: list[dict] | None = None
    src: Path | None = None
    for p in candidates:
        got = _load_list(p)
        if got:
            rows = got
            src = p
            break

    if not rows:
        print("未找到可用的 screener_history.json（非空 list）", file=sys.stderr)
        sys.exit(1)

    print(f"读取 {len(rows)} 条 ← {src}")

    from src.data.analytics_store import (
        backfill_daily_screener_hit_from_history,
        replace_screener_history_entries,
    )

    replace_screener_history_entries(rows)
    n = backfill_daily_screener_hit_from_history(min_iso_date=str(args.min_date))
    print(f"已写入 screener_history_entry；daily_screener_hit 回填/更新 {n} 行（>= {args.min_date}）")


if __name__ == "__main__":
    main()

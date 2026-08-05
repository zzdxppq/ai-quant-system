#!/usr/bin/env python3
"""删除 data 下「带交易日」的过期缓存（仅子目录，不删根目录 latest_*.json 等业务文件）。

覆盖：
  - data/auction_cache/YYYYMMDD/*.json（整目录过期则删目录）
  - data/minute_cache/*_YYYYMMDD.json

默认保留最近 60 个自然日；加 --apply 才真正删除，否则只打印计划。
"""
from __future__ import annotations

import argparse
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

_MINUTE_SUFFIX = re.compile(r"_(\d{8})\.json$", re.I)


def _parse_yyyymmdd(s: str) -> datetime | None:
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="保留最近 N 天（按自然日）")
    ap.add_argument("--apply", action="store_true", help="执行删除；省略则 dry-run")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent / "data"
    if not root.is_dir():
        print("data 目录不存在，退出")
        return

    cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=args.days)
    removed_dirs = 0
    removed_files = 0

    ac = root / "auction_cache"
    if ac.is_dir():
        for child in sorted(ac.iterdir()):
            if not child.is_dir():
                continue
            dt = _parse_yyyymmdd(child.name)
            if dt is None or dt >= cutoff:
                continue
            print(f"{'DEL' if args.apply else 'DRY'} auction_cache/{child.name}/")
            if args.apply:
                shutil.rmtree(child, ignore_errors=True)
            removed_dirs += 1

    mc = root / "minute_cache"
    if mc.is_dir():
        for f in mc.iterdir():
            if not f.is_file() or f.suffix.lower() != ".json":
                continue
            m = _MINUTE_SUFFIX.search(f.name)
            if not m:
                continue
            dt = _parse_yyyymmdd(m.group(1))
            if dt is None or dt >= cutoff:
                continue
            print(f"{'DEL' if args.apply else 'DRY'} minute_cache/{f.name}")
            if args.apply:
                try:
                    f.unlink()
                except OSError as e:
                    print(f"  跳过 {f}: {e}")
            removed_files += 1

    mode = "已删除" if args.apply else "dry-run（加 --apply 执行）"
    print(f"完成 {mode}: 目录 {removed_dirs} 个, 文件 {removed_files} 个（cutoff < {cutoff.date()}）")


if __name__ == "__main__":
    main()

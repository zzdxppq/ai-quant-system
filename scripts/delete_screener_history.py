#!/usr/bin/env python3
"""删除选股历史记录（按日期+代码，或仅按日期批量删除）。

用法（项目根目录，Docker 部署需先停容器）：
  # 删除 2026-05-30 的所有记录
  python -u scripts/delete_screener_history.py --date 2026-05-30

  # 删除指定日期的指定一只
  python -u scripts/delete_screener_history.py --date 2026-05-30 --code 600162

  # 预览模式（不实际删除）
  python -u scripts/delete_screener_history.py --date 2026-05-30 --dry-run

  # Docker 环境（自动停/启容器）
  bash scripts/deploy/server_delete_screener_history.sh --date 2026-05-30
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("MOCK", "0")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="删除选股历史记录")
    p.add_argument(
        "--date",
        required=True,
        metavar="YYYY-MM-DD",
        help="要删除的记录日期（如 2026-05-30）",
    )
    p.add_argument(
        "--code",
        default="",
        metavar="XXXXXX",
        help="可选：仅删除该日期下指定代码的记录",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览，不实际删除",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    target_date = args.date.strip()
    if len(target_date) != 10 or not target_date[:4].isdigit():
        raise SystemExit(f"invalid --date: {target_date!r}，格式应为 YYYY-MM-DD")

    target_code = args.code.strip()
    if target_code and len(target_code) != 6:
        raise SystemExit(f"invalid --code: {target_code!r}，应为 6 位数字")

    from src.data.models import init_db

    init_db()

    from src.engine.screener_history import _load, _save

    records = _load()
    print(f"[preview] 当前共 {len(records)} 条选股记录")

    # 日期过滤：支持 YYYY-MM-DD 和 YYYYMMDD 两种格式
    date_variants = {target_date, target_date.replace("-", "")}
    matched = [r for r in records if str(r.get("date", ""))[:10] in date_variants]
    if not matched:
        print(f"[info] 没有找到日期为 {target_date} 的记录，无需删除")
        return 0

    print(f"[preview] 日期 {target_date} 下共有 {len(matched)} 条记录：")
    for r in matched:
        code = str(r.get("code", "")).strip()
        name = str(r.get("name", "")).strip()
        board = r.get("board_label", "")
        print(f"  {r.get('date')}  {code}  {name}  {board}")

    if target_code:
        matched = [r for r in matched if str(r.get("code", "")).strip()[-6:].zfill(6) == target_code.zfill(6)]
        print(f"\n[preview] 过滤指定代码 {target_code} 后剩余 {len(matched)} 条")

    if not matched:
        print("[info] 过滤后无匹配记录，无需删除")
        return 0

    if args.dry_run:
        print("\n[dry-run] 预览完毕，实际删除请去掉 --dry-run 参数")
        return 0

    confirm = input(f"\n确认删除以上 {len(matched)} 条记录？(y/N): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("[abort] 已取消删除")
        return 1

    # 从记录列表中排除要删除的
    matched_keys = {(str(r.get("date", ""))[:10], str(r.get("code", "")).strip()[-6:].zfill(6)) for r in matched}
    remaining = [r for r in records if (str(r.get("date", ""))[:10], str(r.get("code", "")).strip()[-6:].zfill(6)) not in matched_keys]

    _save(remaining)
    print(f"\n[done] 已删除 {len(matched)} 条，剩余 {len(remaining)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
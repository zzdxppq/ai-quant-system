#!/usr/bin/env python3
"""将 data/ 下 JSON 批量导入 quant 库（`data/quant.duckdb`，DuckDB）。

路由与运行时 `json_io` 一致（见 relational_sqlite.import_json_file）：
  - kline_cache / minute_cache / auction_cache → structured_store
  - latest_* 快照 → structured + analytics 快照逻辑
  - concept_cache / industry_cache → analytics_store
  - review、cycle、report_* 等 ledger 键 → ledger_doc
  - 其余 → relational 的 jdt_* 宽表/行表

步骤：
  1. 备份：将整个 data 目录复制到项目根下 backups/data_YYYYMMDD_HHMMSS/（含 json 与 quant.duckdb）
  2. 建表：relational_sqlite.init_schema()（含 structured / analytics / ledger / registry）
  3. 导入：遍历 data 下 .json（跳过 backups/、json_backup_* 目录）
  4. 可选 --purge-json：导入成功后删除已导入的 .json（运行时恒为 DATA_STORAGE_BACKEND=quant）

用法：
  python scripts/migrate_json_to_sqlite.py
  python scripts/migrate_json_to_sqlite.py --purge-json
  python scripts/migrate_json_to_sqlite.py --backup-only
  python scripts/migrate_json_to_sqlite.py --data-dir E:/claude/ai-quant-system/data

迁移完成后无需改 DATA_STORAGE_BACKEND（固定为 quant，见 src/config.py）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_backup(data_dir: Path, backup_root: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_root / f"data_{ts}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(data_dir, dest, dirs_exist_ok=False)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=None, help="默认: 项目 data/")
    ap.add_argument("--backup-root", type=Path, default=None, help="默认: 项目 backups/")
    ap.add_argument("--backup-only", action="store_true")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    ap.add_argument(
        "--purge-json",
        action="store_true",
        help="导入成功后删除 data 下已导入的 .json（仅在你确认数据已进 quant 库后使用）",
    )
    args = ap.parse_args()

    from src.config import BASE_DIR, DATA_DIR

    data_dir = (args.data_dir or DATA_DIR).resolve()
    backup_root = (args.backup_root or (BASE_DIR / "backups")).resolve()

    if not data_dir.is_dir():
        print(f"[migrate] 数据目录不存在: {data_dir}")
        return 1

    if not args.no_backup:
        print(f"[migrate] 备份整个 data 目录到 {backup_root} …")
        dest = run_backup(data_dir, backup_root)
        print(f"[migrate] 备份完成: {dest}")
        if args.backup_only:
            return 0
    elif args.backup_only:
        print("[migrate] --backup-only 需要执行备份，勿与 --no-backup 同用")
        return 1

    from src.data.data_paths import discover_data_json_files
    from src.data.relational_sqlite import import_json_file, init_schema

    init_schema()
    pairs = discover_data_json_files(data_dir)
    ok, bad = 0, 0
    for rel, p in pairs:
        if import_json_file(rel, p):
            ok += 1
        else:
            print(f"[migrate] 跳过或失败: {rel}")
            bad += 1
    if bad:
        print(f"[migrate] 完成: 导入 {ok} 个 JSON，失败/跳过 {bad}")
        print("[migrate] 请重启服务（DATA_STORAGE_BACKEND 固定为 quant）。")
        return 1
    print(f"[migrate] 完成: 导入 {ok} 个 JSON")
    if args.purge_json:
        removed = 0
        for rel, p in pairs:
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
            except OSError as e:
                print(f"[migrate] 删除失败 {rel}: {e}")
        print(f"[migrate] 已删除 {removed} 个 JSON 文件（--purge-json）")
    print("[migrate] 请重启服务（DATA_STORAGE_BACKEND 固定为 quant）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""以备份目录为真源，将其中 JSON 树强制导入 quant 库（`quant.duckdb`，路由同 import_json_file）。

默认会先复制当前 quant.duckdb 到 backups/ 再导入。可选把同名文件同步到 DATA_DIR（便于人工对账）。

用法（项目根）:
  python scripts/restore_from_json_backup.py --backup backups/data_20260513_201617
  python scripts/restore_from_json_backup.py --backup E:/path/to/data_snapshot --sync-data-dir
  python scripts/restore_from_json_backup.py --backup ... --no-db-backup
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="以备份 JSON 强制覆盖 quant.duckdb")
    ap.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="备份根目录（内含与 data/ 相同的相对路径 JSON 树）",
    )
    ap.add_argument(
        "--no-db-backup",
        action="store_true",
        help="不复制 quant.duckdb（不推荐）",
    )
    ap.add_argument(
        "--sync-data-dir",
        action="store_true",
        help="导入后把备份中的各 JSON 再复制到 DATA_DIR 同相对路径（覆盖磁盘文件）",
    )
    args = ap.parse_args()

    backup = args.backup.resolve()
    if not backup.is_dir():
        print("[restore] 备份目录不存在:", backup)
        return 1

    from src.config import BASE_DIR, DATA_DIR, DB_PATH
    from src.data.data_paths import discover_data_json_files
    from src.data.relational_sqlite import import_json_file, init_schema

    init_schema()

    if not args.no_db_backup and DB_PATH.is_file():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = (BASE_DIR / "backups" / f"quant_before_restore_{ts}.duckdb").resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DB_PATH, dest)
        print("[restore] 已备份当前库到:", dest)

    pairs = discover_data_json_files(backup)
    print(f"[restore] 待导入 JSON 文件数: {len(pairs)}")
    ok, bad = 0, 0
    for i, (rel, p) in enumerate(pairs, 1):
        if import_json_file(rel, p):
            ok += 1
        else:
            print(f"[restore] 失败或跳过: {rel}")
            bad += 1
        if i % 200 == 0:
            print(f"[restore] 进度 {i}/{len(pairs)} …")

    print(f"[restore] 导入完成: 成功 {ok}, 失败/跳过 {bad}")

    if args.sync_data_dir:
        n = 0
        for rel, p in pairs:
            dest = DATA_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            n += 1
        print(f"[restore] 已同步 {n} 个文件到 DATA_DIR")

    print("[restore] 结束。可启动任务拉取今日新数据。")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

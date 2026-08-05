#!/usr/bin/env python3
"""维护 quant 库（SQLite / DuckDB）：清理无 app_json_doc_registry 指向的遗留 jdt_* 表。

迁移或手工改库后可能留下孤立 jdt_*；本脚本比对 registry 后 DROP（SKIP_JSON_DOC_REGISTRY=1 时凡 jdt_* 均视为可清理）。

用法（项目根目录）:
  python scripts/reconcile_sqlite_storage.py --dry-run
  python scripts/reconcile_sqlite_storage.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from src.data.relational_sqlite import drop_orphan_jdt_tables, init_schema, list_orphan_jdt_tables

    init_schema()
    orphans = list_orphan_jdt_tables()
    if not orphans:
        print("[reconcile] 无遗留 jdt_* 表")
        return 0
    print("[reconcile] 遗留表:", ", ".join(orphans))
    if args.dry_run:
        return 0
    dropped = drop_orphan_jdt_tables()
    print("[reconcile] 已 DROP:", ", ".join(dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

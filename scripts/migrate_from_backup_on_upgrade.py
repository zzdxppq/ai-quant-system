#!/usr/bin/env python3
"""数据库升级时：从 `backups/*/` 根级 *.json 按与线上一致的路由灌入 DuckDB（补缺；默认不覆盖已有）。

包含：screener_history、latest_review、ledger_doc（review_history 等）、relational 已注册文档等。

用法:
  python scripts/migrate_from_backup_on_upgrade.py
  python scripts/migrate_from_backup_on_upgrade.py --force-backup   # 用备份覆盖
  python scripts/migrate_from_backup_on_upgrade.py --latest-only    # 仅 latest_advice/sentiment/leader
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from src.data import analytics_store as ast
    from src.data.latest_snapshot_hydrate import (
        hydrate_latest_snapshots_from_backups,
        upgrade_hydrate_all_from_backups,
        upgrade_hydrate_latest_snapshots_from_backups,
    )

    if "--latest-only" in sys.argv:
        if "--force-backup" in sys.argv:
            ast.init_analytics_schema()
            print(hydrate_latest_snapshots_from_backups(force=True))
        else:
            upgrade_hydrate_latest_snapshots_from_backups()
    elif "--force-backup" in sys.argv:
        print(upgrade_hydrate_all_from_backups(force=True))
    else:
        summary = upgrade_hydrate_all_from_backups(force=False)
        n = sum(1 for v in summary.values() if v == "restored")
        print(f"[migrate-from-backup] keys={len(summary)} restored={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

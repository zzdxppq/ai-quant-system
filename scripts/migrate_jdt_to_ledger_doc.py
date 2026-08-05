#!/usr/bin/env python3
"""将 ledger 类 doc_key 从 relational 宽表/行表（jdt_*）迁入 ledger_doc，并 DROP 表、清理 registry/colmap。

请在已部署含 ledger_doc_store 与 json_io 改动的代码后执行；执行前备份 quant.duckdb。

用法:
  python scripts/migrate_jdt_to_ledger_doc.py
  python scripts/migrate_jdt_to_ledger_doc.py --dry-run
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
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的操作，不写库")
    args = ap.parse_args()

    from src.data.ledger_doc_store import is_ledger_doc_key, upsert_json
    from src.data.quant_db import connect, table_exists
    from src.data.relational_sqlite import init_schema, load_document

    init_schema()

    registry = "app_json_doc_registry"
    colmap = "app_json_doc_colmap"

    conn = connect()
    try:
        if not table_exists(conn, registry):
            rows = []
        else:
            cur = conn.execute(f"SELECT doc_key, data_table FROM {registry}")
            rows = cur.fetchall()
    finally:
        conn.close()

    moved = 0
    skipped_none = 0
    for doc_key, data_table in rows:
        dk = str(doc_key)
        tname = str(data_table)
        if not is_ledger_doc_key(dk):
            continue

        obj = load_document(dk)
        if obj is None:
            print(f"[migrate_ledger] 跳过（无法 load_document）: {dk} 表={tname}")
            skipped_none += 1
            continue

        if args.dry_run:
            print(f"[dry-run] upsert {dk!r} DROP {tname!r}")
            moved += 1
            continue

        upsert_json(dk, obj)
        conn2 = connect()
        try:
            conn2.execute(f'DROP TABLE IF EXISTS "{tname}"')
            if table_exists(conn2, colmap):
                conn2.execute(f"DELETE FROM {colmap} WHERE doc_key = ?", (dk,))
            if table_exists(conn2, registry):
                conn2.execute(f"DELETE FROM {registry} WHERE doc_key = ?", (dk,))
        finally:
            conn2.close()
        print(f"[migrate_ledger] 已迁移并 DROP: {dk} -> ledger_doc")
        moved += 1

    print(
        f"[migrate_ledger] 完成: 处理 {moved} 个 ledger 文档，"
        f"无法加载跳过 {skipped_none} 个，registry 中共 {len(rows)} 行"
    )
    if not args.dry_run:
        from src.data.relational_sqlite import drop_orphan_jdt_tables

        dropped = drop_orphan_jdt_tables()
        if dropped:
            print(f"[migrate_ledger] 清理无 registry 的遗留表: {', '.join(dropped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

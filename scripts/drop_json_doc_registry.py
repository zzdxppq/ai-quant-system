#!/usr/bin/env python3
"""删除 app_json_doc_registry / app_json_doc_colmap 及全部 jdt_* 表（拍平 JSON 表路径下线）。

执行前请备份 DB；之后请在 .env 中设 SKIP_JSON_DOC_REGISTRY=1，避免 init_schema 再建 registry。

用法（项目根）:
  python scripts/drop_json_doc_registry.py --dry-run
  python scripts/drop_json_doc_registry.py
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

    from src.config import DB_PATH
    from src.data.quant_db import connect, list_main_tables_prefixed

    if not DB_PATH.is_file():
        print("库文件不存在:", DB_PATH, file=sys.stderr)
        return 1

    conn = connect()
    try:
        jdt = list_main_tables_prefixed(conn, "jdt_")
    finally:
        conn.close()

    drops = [*sorted(jdt), "app_json_doc_colmap", "app_json_doc_registry"]
    print(f"DuckDB 库={DB_PATH}")
    print("将 DROP:", ", ".join(drops))
    if args.dry_run:
        return 0

    conn = connect()
    try:
        for t in drops:
            conn.execute(f'DROP TABLE IF EXISTS "{t}"')
    finally:
        conn.close()
    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

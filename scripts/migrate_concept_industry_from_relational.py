#!/usr/bin/env python3
"""将 relational 中 concept_cache / industry_cache 迁入 analytics 表，并 DROP jdt_*。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from src.data import relational_sqlite as rs
    from src.data.analytics_store import init_analytics_schema, replace_concept_from_doc, replace_industry_map
    from src.data.quant_db import connect, table_exists

    rs.init_schema()
    init_analytics_schema()
    for doc_key in ("concept_cache.json", "industry_cache.json"):
        obj = rs.load_document(doc_key)
        if not isinstance(obj, dict) or not obj:
            print(f"[migrate] 跳过空: {doc_key}")
            continue
        if doc_key == "concept_cache.json":
            replace_concept_from_doc(obj)
        else:
            replace_industry_map(obj)
        conn = connect()
        try:
            if table_exists(conn, "app_json_doc_registry"):
                cur = conn.execute(
                    "SELECT data_table FROM app_json_doc_registry WHERE doc_key = ?",
                    (doc_key,),
                )
                row = cur.fetchone()
                if row:
                    t = str(row[0])
                    if t.startswith("jdt_"):
                        conn.execute(f'DROP TABLE IF EXISTS "{t}"')
                    if table_exists(conn, "app_json_doc_colmap"):
                        conn.execute("DELETE FROM app_json_doc_colmap WHERE doc_key = ?", (doc_key,))
                    conn.execute("DELETE FROM app_json_doc_registry WHERE doc_key = ?", (doc_key,))
        finally:
            conn.close()
        print(f"[migrate] 完成: {doc_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

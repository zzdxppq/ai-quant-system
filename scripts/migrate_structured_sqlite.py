#!/usr/bin/env python3
"""将 quant 库（DuckDB）中 JSON 分表（jdt_kline_cache_*、minute/auction/latest_*）迁入结构化表并 DROP 旧表。

用法（在项目根执行）：
  python scripts/migrate_structured_sqlite.py --migrate
  python scripts/migrate_structured_sqlite.py --import-backup backups/data_20260513_201617
  python scripts/migrate_structured_sqlite.py --migrate --import-backup backups/data_20260513_201617

--import-backup：将备份目录下 JSON 合并入库（kline/minute/auction/latest 走 structured，其余走 relational_sqlite.save_document）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_REG = "app_json_doc_registry"
_COL = "app_json_doc_colmap"


def _drop_jdt(conn: Any, data_table: str) -> None:
    t = str(data_table).strip()
    if not t.startswith("jdt_") or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in t.lower()):
        raise ValueError(f"unsafe table name: {data_table!r}")
    conn.execute(f'DROP TABLE IF EXISTS "{t}"')


def migrate_exploded_tables() -> int:
    from src.data import relational_sqlite as rs
    from src.data.structured_store import (
        init_structured_schema,
        ingest_auction_json_doc,
        ingest_kline_json_doc,
        ingest_minute_json_doc,
        save_daily_snapshot,
    )

    rs.init_schema()
    init_structured_schema()

    globs = (
        "kline_cache/*",
        "minute_cache/*",
        "auction_cache/*",
        "latest_*.json",
    )
    pairs: list[tuple[str, str]] = []
    from src.data.quant_db import connect, table_exists

    conn = connect()
    try:
        if table_exists(conn, _REG):
            for g in globs:
                cur = conn.execute(
                    f"SELECT doc_key, data_table FROM {_REG} WHERE doc_key GLOB ?",
                    (g,),
                )
                pairs.extend((str(a), str(b)) for a, b in cur.fetchall())
    finally:
        conn.close()

    n_ok = 0
    for doc_key, data_table in sorted(pairs, key=lambda x: x[0]):
        obj = rs.load_document(doc_key)
        if obj is None:
            continue
        try:
            if doc_key.startswith("kline_cache/"):
                ingest_kline_json_doc(doc_key, obj)
            elif doc_key.startswith("minute_cache/"):
                ingest_minute_json_doc(doc_key, obj)
            elif doc_key.startswith("auction_cache/"):
                ingest_auction_json_doc(doc_key, obj)
            elif doc_key.startswith("latest_") and doc_key.endswith(".json"):
                save_daily_snapshot(doc_key.split("/")[-1], obj)
            else:
                continue
        except Exception as e:
            print(f"[migrate] skip {doc_key}: {e}")
            continue

        conn2 = connect()
        try:
            conn2.execute("BEGIN")
            _drop_jdt(conn2, data_table)
            if table_exists(conn2, _COL):
                conn2.execute(f"DELETE FROM {_COL} WHERE doc_key = ?", (doc_key,))
            if table_exists(conn2, _REG):
                conn2.execute(f"DELETE FROM {_REG} WHERE doc_key = ?", (doc_key,))
            conn2.execute("COMMIT")
        except Exception as e:
            try:
                conn2.execute("ROLLBACK")
            except Exception:
                pass
            print(f"[migrate] drop registry failed {doc_key}: {e}")
        finally:
            conn2.close()
        n_ok += 1
        if n_ok % 50 == 0:
            print(f"[migrate] … {n_ok} / {len(pairs)}")
    print(f"[migrate] 完成迁移并 DROP: {n_ok} 个 doc_key（共登记 {len(pairs)}）")
    return 0


def import_backup_dir(backup_root: Path) -> int:
    from src.data.data_paths import should_skip_json_path
    from src.data.relational_sqlite import import_json_file, save_document
    from src.data.structured_store import (
        init_structured_schema,
        ingest_auction_json_doc,
        ingest_kline_json_doc,
        ingest_minute_json_doc,
        save_daily_snapshot,
    )
    from src.data import relational_sqlite as rs

    rs.init_schema()
    init_structured_schema()
    root = backup_root.resolve()
    if not root.is_dir():
        print(f"[import] 不是目录: {root}")
        return 1

    ok, bad = 0, 0
    for p in sorted(root.rglob("*.json")):
        if not p.is_file():
            continue
        if should_skip_json_path(p, root):
            continue
        rel = p.relative_to(root).as_posix()
        try:
            raw = p.read_bytes()
            obj = None
            for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "cp936"):
                try:
                    obj = json.loads(raw.decode(enc))
                    break
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            if obj is None:
                bad += 1
                continue
            if rel.startswith("kline_cache/"):
                ingest_kline_json_doc(rel, obj)
            elif rel.startswith("minute_cache/"):
                ingest_minute_json_doc(rel, obj)
            elif rel.startswith("auction_cache/"):
                ingest_auction_json_doc(rel, obj)
            elif rel.startswith("latest_") and rel.endswith(".json"):
                save_daily_snapshot(Path(rel).name, obj)
            else:
                if not import_json_file(rel, p):
                    bad += 1
                    continue
            ok += 1
        except Exception as e:
            print(f"[import] {rel}: {e}")
            bad += 1
    print(f"[import] 完成: 成功 {ok}, 失败 {bad}（备份根 {root}）")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--migrate", action="store_true", help="从 app_json_doc_registry 迁出 kline/minute/auction/latest_* 并 DROP jdt_*")
    ap.add_argument("--import-backup", type=Path, default=None, help="从备份目录导入 JSON")
    args = ap.parse_args()
    if not args.migrate and not args.import_backup:
        ap.print_help()
        return 1
    rc = 0
    if args.import_backup:
        rc = import_backup_dir(args.import_backup)
    if args.migrate:
        migrate_exploded_tables()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

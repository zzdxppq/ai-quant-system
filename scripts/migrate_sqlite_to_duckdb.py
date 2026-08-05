#!/usr/bin/env python3
"""将 SQLite 的 data/quant.db 全表复制到 DuckDB 的 data/quant.duckdb（pandas 逐表）。

完成后请：
  1. 备份并移除或改名旧 quant.db
  2. 重启应用（运行时固定使用 `data/quant.duckdb`）
  3. 重启应用

用法（项目根）:
  python scripts/migrate_sqlite_to_duckdb.py
  python scripts/migrate_sqlite_to_duckdb.py --src E:/path/quant.db --dst E:/path/quant.duckdb
  python scripts/migrate_sqlite_to_duckdb.py --force   # 覆盖已存在的目标库
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    import sqlite3

    import duckdb
    import pandas as pd

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=ROOT / "data" / "quant.db")
    ap.add_argument("--dst", type=Path, default=ROOT / "data" / "quant.duckdb")
    ap.add_argument(
        "--force",
        action="store_true",
        help="若目标库已存在则先删除再写入",
    )
    args = ap.parse_args()
    src = args.src.resolve()
    dst = args.dst.resolve()
    if not src.is_file():
        print("源库不存在:", src, file=sys.stderr)
        return 1
    if dst.exists():
        if not args.force:
            print("目标已存在，请先删除、换 --dst 或使用 --force:", dst, file=sys.stderr)
            return 2
        try:
            dst.unlink()
        except OSError as e:
            print("无法删除目标库:", dst, e, file=sys.stderr)
            return 3

    s = sqlite3.connect(str(src))
    d = duckdb.connect(str(dst))
    try:
        tables = [
            str(r[0])
            for r in s.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for t in tables:
            try:
                df = pd.read_sql_query(f'SELECT * FROM "{t}"', s)
            except Exception as e:
                print("跳过表", t, ":", e)
                continue
            d.register("df_tmp", df)
            d.execute(f'CREATE OR REPLACE TABLE "{t}" AS SELECT * FROM df_tmp')
            d.unregister("df_tmp")
            print("已复制", t, "行数", len(df))
    finally:
        s.close()
        d.close()
    print("完成 →", dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""对比 data 下 JSON 文件与 quant.duckdb 各 JSON 业务表还原后的文档是否一致（深度比较）。

用法（在项目根目录）：
  python scripts/verify_json_sqlite_parity.py
  python scripts/verify_json_sqlite_parity.py --data-dir E:/path/to/data
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=None)
    args = ap.parse_args()

    from src.config import DATA_DIR
    from src.data.data_paths import discover_data_json_files
    from src.data.relational_sqlite import init_schema, load_document

    data_dir = (args.data_dir or DATA_DIR).resolve()
    init_schema()
    mism = 0
    for rel, p in discover_data_json_files(data_dir):
        try:
            file_obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            try:
                file_obj = json.loads(p.read_bytes().decode("utf-8-sig"))
            except Exception:
                print(f"[verify] 无法读文件: {rel}")
                mism += 1
                continue
        db_obj = load_document(rel)
        if db_obj is None:
            print(f"[verify] 库中缺失: {rel}")
            mism += 1
            continue
        if file_obj != db_obj:
            print(f"[verify] 内容不一致: {rel}")
            mism += 1
    if mism:
        print(f"[verify] 共 {mism} 处问题")
        return 1
    print("[verify] 全部一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

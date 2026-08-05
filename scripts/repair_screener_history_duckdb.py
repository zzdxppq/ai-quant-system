#!/usr/bin/env python3
"""修复 screener_history_entry 表 DuckDB 索引损坏 / invalidated。

全表 DELETE 曾触发「Failed to delete all rows from index」并使库进入 invalidated。
本脚本在**服务已停止**时执行：导出 → DROP+CREATE 表 → 写回。

用法（项目根，先 docker stop quant-ai）:
  python scripts/repair_screener_history_duckdb.py
  python scripts/repair_screener_history_duckdb.py --json data/backups/screener_history.json
  python scripts/repair_screener_history_duckdb.py --dry-run
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

from src.config import DATA_DIR


def _find_json_backup() -> Path | None:
    candidates = [
        DATA_DIR / "screener_history.json",
        DATA_DIR / "backups" / "screener_history.json",
    ]
    backups = DATA_DIR / "backups"
    if backups.is_dir():
        for p in sorted(backups.glob("**/screener_history.json"), reverse=True):
            candidates.append(p)
    for p in candidates:
        if p.is_file():
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(obj, list) and obj:
                    return p
            except Exception:
                continue
    return None


def _load_rows(json_path: Path | None) -> list[dict]:
    if json_path and json_path.is_file():
        obj = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(obj, list):
            return [r for r in obj if isinstance(r, dict)]
    from src.data.quant_db import reset_shared_connection

    reset_shared_connection()
    try:
        from src.data.analytics_store import load_screener_history_entries

        return load_screener_history_entries()
    except Exception as exc:
        print(f"从 DuckDB 读取失败: {exc}", file=sys.stderr)
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="重建 screener_history_entry 表")
    ap.add_argument("--json", type=Path, default=None, help="screener_history.json 备份")
    ap.add_argument("--dry-run", action="store_true", help="仅统计行数，不写库")
    args = ap.parse_args()

    from src import config as cfg

    dbp = Path(cfg.DB_PATH).resolve()
    if not dbp.is_file():
        print(f"FAIL: 未找到 {dbp}", file=sys.stderr)
        return 1

    json_path = args.json
    if json_path is None:
        json_path = _find_json_backup()
    rows = _load_rows(json_path)
    if not rows:
        print("FAIL: 无可用 screener_history 数据（JSON 备份或可读 DuckDB）", file=sys.stderr)
        return 1
    print(f"待写回 {len(rows)} 条" + (f"（来源 {json_path}）" if json_path else "（来源 DuckDB）"))

    if args.dry_run:
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = dbp.with_suffix(f".duckdb.bak_{stamp}")
    shutil.copy2(dbp, bak)
    print(f"已备份库 → {bak}")

    from src.data.quant_db import reset_shared_connection

    reset_shared_connection()
    from src.data.analytics_store import (
        _recreate_screener_history_table,
        init_analytics_schema,
        replace_screener_history_entries,
    )
    from src.data.quant_db import connect as quant_connect

    init_analytics_schema()
    conn = quant_connect()
    try:
        conn.execute("BEGIN")
        _recreate_screener_history_table(conn)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()

    reset_shared_connection()
    replace_screener_history_entries(rows)
    print("screener_history_entry 已重建并写回")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

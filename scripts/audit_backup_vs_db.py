"""对比备份目录与当前 quant 库（ledger / daily_screener_hit 等），不写库。

示例:
  python scripts/audit_backup_vs_db.py
  python scripts/audit_backup_vs_db.py --backup E:/path/to/backups/data_xxx
  python scripts/audit_backup_vs_db.py --db E:/path/data/quant.duckdb
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
DEFAULT_BACKUP = ROOT / "backups" / "data_20260513_201617"
CUTOFF = "2026-04-15"


def _open_db_path(db_path: Path) -> Any:
    """打开 DuckDB；若路径非 .duckdb 后缀则尝试 SQLite（仅用于对比审计旧备份库）。"""
    import sqlite3

    p = str(db_path.resolve())
    if db_path.suffix.lower() == ".duckdb":
        import duckdb

        return duckdb.connect(p)
    conn = sqlite3.connect(p, timeout=120, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
    except sqlite3.Error:
        pass
    return conn


def _ledger_body(conn: Any, doc_key: str):
    row = conn.execute("SELECT body_json FROM ledger_doc WHERE doc_key=?", (doc_key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def _list_dates_from_screener_history(obj) -> set[str]:
    if not isinstance(obj, list):
        return set()
    out: set[str] = set()
    for r in obj:
        if not isinstance(r, dict):
            continue
        d = str(r.get("date") or "")[:10]
        if len(d) == 10 and d >= CUTOFF:
            out.add(d)
    return out


def _list_dates_from_screener_table(conn: Any) -> set[str]:
    """与结构化表 screener_history_entry 对齐（ledger 已可能无该 doc）。"""
    cutoff_ymd = CUTOFF.replace("-", "")
    try:
        cur = conn.execute(
            "SELECT DISTINCT trade_date FROM screener_history_entry WHERE length(trade_date)=8 AND trade_date >= ?",
            (cutoff_ymd,),
        )
    except Exception:
        return set()
    out: set[str] = set()
    for (td,) in cur.fetchall():
        s = str(td)
        if len(s) == 8 and s.isdigit():
            out.add(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
    return out


def main() -> int:
    from src.config import DB_PATH

    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()
    backup: Path = args.backup.resolve()
    db_path: Path = args.db.resolve()

    if not db_path.is_file():
        print("DB missing:", db_path)
        return 1

    conn = _open_db_path(db_path)
    try:
        cur = conn.execute("SELECT doc_key, length(body_json) FROM ledger_doc ORDER BY doc_key")
        rows = cur.fetchall()
        print("=== ledger_doc rows:", len(rows))
        for k, ln in rows:
            print(f"  {k}: {ln} bytes")

        bk = _read_json(backup / "screener_history.json")
        ds_b = _list_dates_from_screener_history(bk)
        ds_d = _list_dates_from_screener_table(conn)
        if not ds_d:
            db = _ledger_body(conn, "screener_history.json")
            ds_d = _list_dates_from_screener_history(db)
        only_b = sorted(ds_b - ds_d)
        only_d = sorted(ds_d - ds_b)
        print("=== screener_history.json (>= {})".format(CUTOFF))
        print("  backup dates:", len(ds_b), "db dates:", len(ds_d))
        if only_b:
            print("  ONLY in backup (missing in DB):", only_b[:40])
        if only_d:
            print("  ONLY in DB (not in backup):", only_d[:40])

        def _list_len(p: Path, doc_key: str):
            a = _read_json(p)
            b = _ledger_body(conn, doc_key)
            la = len(a) if isinstance(a, list) else ("dict" if isinstance(a, dict) else type(a).__name__)
            lb = len(b) if isinstance(b, list) else ("dict" if isinstance(b, dict) else type(b).__name__)
            return la, lb, a == b if type(a) == type(b) else None

        print("\n=== list-shaped ledger: backup vs DB ===")
        for name in (
            "review_history.json",
            "sentiment_history.json",
            "decision_records.json",
            "trend_history.json",
        ):
            la, lb, eq = _list_len(backup / name, name)
            print(f"  {name}: backup={la} db={lb} deep_equal={eq}")

        cur = conn.execute(
            "SELECT COUNT(DISTINCT hit_date), MIN(hit_date), MAX(hit_date) FROM daily_screener_hit WHERE hit_date >= ?",
            ("20260415",),
        )
        row = cur.fetchone()
        print("=== daily_screener_hit (hit_date YYYYMMDD)")
        print("  distinct_dates>=20260415:", row[0], "min:", row[1], "max:", row[2])
        cur = conn.execute(
            "SELECT COUNT(*) FROM daily_screener_hit WHERE hit_date >= ?",
            ("20260415",),
        )
        print("  rows:", cur.fetchone()[0])
    finally:
        conn.close()

    print("\n=== backup top-level json ===")
    if backup.is_dir():
        js = sorted(backup.glob("*.json"))
        print("  count:", len(js))
    else:
        print("  backup dir missing:", backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""排查某日选股/决策卡/选股历史在 DuckDB 各表中的落库情况。

用法:
  python scripts/audit_today_screener_db.py
  python scripts/audit_today_screener_db.py --date 20260525
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
    ap.add_argument("--date", default="", help="YYYYMMDD 或 YYYY-MM-DD；默认今天")
    args = ap.parse_args()

    from src.config import DATA_DIR, DB_PATH, now_cn
    from src.data.json_io import load_json_file
    from src.data.models import init_db
    from src.data.quant_db import connect

    init_db()
    ymd = (args.date or now_cn().strftime("%Y%m%d")).replace("-", "")[:8]
    iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"

    db = Path(DB_PATH)
    print(f"quant.duckdb: {db.resolve()}  size={db.stat().st_size if db.is_file() else 0} bytes")

    c = connect()
    try:
        def cnt(sql: str, *p) -> int:
            return int(c.execute(sql, p).fetchone()[0])

        print(f"\n=== 目标日 {iso} ({ymd}) ===")
        print("daily_screener_hit     ", cnt("SELECT COUNT(*) FROM daily_screener_hit WHERE hit_date=?", ymd))
        print("daily_auction_scores   ", cnt("SELECT COUNT(*) FROM daily_auction_scores WHERE score_date=?", ymd))
        print("screener_history_entry ", cnt("SELECT COUNT(*) FROM screener_history_entry WHERE trade_date=?", iso))

        print("\n=== 各表最近日期 ===")
        for label, sql in (
            ("daily_screener_hit", "SELECT MAX(hit_date) FROM daily_screener_hit"),
            ("daily_auction_scores", "SELECT MAX(score_date) FROM daily_auction_scores"),
            ("screener_history", "SELECT MAX(trade_date) FROM screener_history_entry"),
            ("daily_advice", "SELECT MAX(advice_date) FROM daily_advice"),
        ):
            r = c.execute(sql).fetchone()
            print(f"  {label}: {r[0] if r else None}")

        print("\n=== daily_json_blob (latest_*) ===")
        for kind in ("latest_screener", "latest_auction_scores"):
            rows = c.execute(
                """SELECT blob_date, length(payload_json) FROM daily_json_blob
                   WHERE blob_kind=? ORDER BY blob_date DESC LIMIT 3""",
                (kind,),
            ).fetchall()
            if not rows:
                print(f"  {kind}: (空)")
                continue
            for bd, ln in rows:
                nh = "?"
                if bd == ymd:
                    pj = c.execute(
                        "SELECT payload_json FROM daily_json_blob WHERE blob_kind=? AND blob_date=?",
                        (kind, bd),
                    ).fetchone()
                    if pj and pj[0]:
                        try:
                            obj = json.loads(str(pj[0]))
                            if isinstance(obj, list):
                                nh = str(len(obj))
                            elif isinstance(obj, dict):
                                nh = str(len(obj.get("hits") or []))
                        except json.JSONDecodeError:
                            nh = "parse_err"
                print(f"  {kind} blob_date={bd} bytes={ln} hits/items={nh}")

        print("\n=== load_json_file 逻辑路径 ===")
        sc = load_json_file(DATA_DIR / "latest_screener.json") or {}
        ac = load_json_file(DATA_DIR / "latest_auction_scores.json")
        print(f"  latest_screener: date={sc.get('date')!r} hits={len(sc.get('hits') or [])}")
        print(f"  latest_auction_scores: {len(ac) if isinstance(ac, list) else 0} 条")

        dr = load_json_file(DATA_DIR / "decision_records.json")
        if isinstance(dr, list):
            for rec in dr:
                if str(rec.get("date") or "")[:10] == iso:
                    print(
                        f"  decision_records[{iso}]: "
                        f"screener_hits={len(rec.get('screener_hits') or [])} "
                        f"auction_scores={len(rec.get('auction_scores') or [])}"
                    )
                    break
            else:
                print(f"  decision_records: 无 {iso} 记录")
    finally:
        c.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

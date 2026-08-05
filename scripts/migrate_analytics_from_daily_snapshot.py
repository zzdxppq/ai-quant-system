#!/usr/bin/env python3
"""将 daily_snapshot 中已迁移 kind 的 JSON 导入 analytics 结构化表，并可选择删除旧 blob 行。

用法：python scripts/migrate_analytics_from_daily_snapshot.py [--purge-blob]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KINDS = (
    "latest_advice.json",
    "latest_ranking.json",
    "latest_sentiment.json",
    "latest_auction_scores.json",
    "latest_screener.json",
    "latest_leader.json",
    "latest_insight.json",
    "latest_snapshot.json",
    "latest_signals.json",
    "latest_deviation.json",
    "latest_trend.json",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--purge-blob", action="store_true", help="删除 daily_snapshot 中已迁移 kind 的行")
    args = ap.parse_args()

    from src.data.analytics_store import init_analytics_schema, save_from_latest_filename
    from src.data.quant_db import connect

    init_analytics_schema()
    conn = connect()
    try:
        cur = conn.execute(
            "SELECT snapshot_kind, snapshot_date, payload_json FROM daily_snapshot WHERE snapshot_kind IN (%s)"
            % ",".join("?" * len(KINDS)),
            KINDS,
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    n = 0
    for kind, _date, payload in rows:
        try:
            obj = json.loads(str(payload))
        except (json.JSONDecodeError, TypeError):
            continue
        try:
            save_from_latest_filename(str(kind), obj)
            n += 1
        except Exception as e:
            print(f"[migrate] {kind}: {e}")
    print(f"[migrate] 导入 analytics 行数: {n} / {len(rows)}")

    if args.purge_blob:
        conn2 = connect()
        try:
            conn2.execute(
                "DELETE FROM daily_snapshot WHERE snapshot_kind IN (%s)" % ",".join("?" * len(KINDS)),
                KINDS,
            )
            print("[migrate] 已 purge daily_snapshot 中对应 kind")
        finally:
            conn2.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

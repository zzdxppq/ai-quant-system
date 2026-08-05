"""最小盘后补跑：仅刷新涨停缓存(东财池) + 复盘，避免全市场 spot/排行导致崩溃。

单进程、顺序执行；执行前请确保无其他进程占用 quant.duckdb。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("MOCK", "0")


def main() -> int:
    from src.config import now_cn
    from src.data.json_io import load_json_file
    from src.config import DATA_DIR

    today_key = now_cn().strftime("%Y%m%d")
    print(f"[1] today_key={today_key}")

    from src.data.quant_db import reset_shared_connection

    reset_shared_connection()

    import duckdb
    from src.config import DATA_DIR as DD

    db = DD / "quant.duckdb"
    con = duckdb.connect(str(db), read_only=True)
    try:
        dup = con.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT doc_key) FROM ledger_doc"
        ).fetchone()[0]
    finally:
        con.close()
    print(f"[ledger dup_keys]={dup}")
    if dup:
        print("FAIL: ledger_doc 仍有重复 doc_key，请先 dedupe")
        return 2

    from src.data.models import init_db

    init_db()
    print("[2] sync_limit_up_cache_from_zt_pool …")
    from src.data.fetcher import sync_limit_up_cache_from_zt_pool

    n = sync_limit_up_cache_from_zt_pool(today_key)
    print(f"    wrote {n} rows for {today_key}")

    cache = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
    keys = sorted(k for k in cache if str(k).isdigit())
    print(f"[3] cache keys tail: {keys[-3:]}")

    print("[4] run_daily_review …")
    from src.engine.daily_review import run_daily_review

    rev = run_daily_review()
    if rev is None:
        print("FAIL: run_daily_review returned None")
        return 3
    print(f"    review.date={getattr(rev, 'date', None)}")

    from src.data.analytics_store import load_latest_review_document

    doc = load_latest_review_document() or {}
    print(f"[5] latest_review date={doc.get('date')} limit_up={doc.get('limit_up_count')}")

    from src.data.quant_db import reset_shared_connection

    reset_shared_connection()
    con2 = duckdb.connect(str(db), read_only=True)
    try:
        dup2 = con2.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT doc_key) FROM ledger_doc"
        ).fetchone()[0]
    finally:
        con2.close()
    print(f"[ledger dup_keys after]={dup2}")
    ymd = str(doc.get("date") or "")[:10]
    ok = ymd == now_cn().strftime("%Y-%m-%d")
    print("OK" if ok else f"WARN date mismatch expect {now_cn().strftime('%Y-%m-%d')}")
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())

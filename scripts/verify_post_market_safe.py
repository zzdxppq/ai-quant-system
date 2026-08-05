"""安全盘后核对：单进程写库 → 校验复盘/涨停缓存/ledger 健康（不并发、不重复 DDL）。

用法（确保无 main.py / quant-ai 占用 quant.duckdb）:
  python scripts/verify_post_market_safe.py
  python scripts/verify_post_market_safe.py --skip-run   # 仅核对，不执行复盘
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _duckdb_readonly_ok() -> tuple[bool, str]:
    from src.config import DATA_DIR

    db = DATA_DIR / "quant.duckdb"
    if not db.is_file():
        return False, f"缺少 {db}"
    try:
        import duckdb

        con = duckdb.connect(str(db), read_only=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM ledger_doc").fetchone()[0]
            dup = con.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT doc_key) FROM ledger_doc"
            ).fetchone()[0]
            adv = con.execute(
                "SELECT advice_date FROM daily_advice ORDER BY advice_date DESC LIMIT 1"
            ).fetchone()
            msg = f"ledger_doc={n} dup_keys={dup} latest_advice={adv[0] if adv else None}"
            return True, msg
        finally:
            con.close()
    except Exception as e:
        return False, str(e)


def _load_review_summary() -> dict:
    from src.data.analytics_store import load_latest_review_document
    from src.data.json_io import load_json_file
    from src.config import DATA_DIR, now_cn

    doc = load_latest_review_document() or {}
    if not doc:
        raw = load_json_file(DATA_DIR / "latest_review.json")
        doc = raw if isinstance(raw, dict) else {}
    today = now_cn().strftime("%Y-%m-%d")
    ymd = str(doc.get("date") or "")[:10]
    relay = doc.get("relay_env") or {}
    sb = relay.get("space_board") or {}
    return {
        "today": today,
        "review_date": ymd,
        "session_key": str(doc.get("session_date") or doc.get("trade_date") or ""),
        "limit_up_count": doc.get("limit_up_count"),
        "space_board": f"{sb.get('name','')}({sb.get('code','')})",
        "watch_pool_len": len(doc.get("watch_pool") or []),
    }


def _load_limit_up_cache_keys() -> list[str]:
    from src.data.json_io import load_json_file
    from src.config import DATA_DIR

    raw = load_json_file(DATA_DIR / "limit_up_cache.json")
    if not isinstance(raw, dict):
        return []
    return sorted(k for k in raw.keys() if str(k).isdigit() and len(str(k)) == 8)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-run", action="store_true", help="不执行 run_post_market_bundle")
    args = parser.parse_args()

    ok, msg = _duckdb_readonly_ok()
    print(f"[duckdb RO] {'OK' if ok else 'FAIL'}: {msg}")
    if not ok:
        print("库不可读，请先停止占用进程后重试；勿在 invalidated 状态下反复写入。")
        return 2

    if not args.skip_run:
        from src.data.quant_db import reset_shared_connection
        from src.data.models import init_db
        from src.scheduler import run_post_market_bundle

        reset_shared_connection()
        print("[init_db] …")
        init_db()
        print("[post_market] run_post_market_bundle …")
        run_post_market_bundle()

    ok2, msg2 = _duckdb_readonly_ok()
    print(f"[duckdb RO after] {'OK' if ok2 else 'FAIL'}: {msg2}")
    if not ok2:
        return 3

    rev = _load_review_summary()
    lu_keys = _load_limit_up_cache_keys()
    print("[latest_review]", json.dumps(rev, ensure_ascii=False))
    print("[limit_up_cache keys]", lu_keys[-5:] if lu_keys else [])
    if rev["review_date"] and rev["review_date"] != rev["today"]:
        print(
            f"WARN: 复盘 date={rev['review_date']} 与今日 {rev['today']} 不一致 "
            f"(可能非交易日或涨停缓存未刷新)"
        )
    elif rev["review_date"] == rev["today"]:
        print("OK: 复盘日期与今日一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

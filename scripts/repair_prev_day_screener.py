#!/usr/bin/env python3
"""修正上一交易日涨停池 → 同步复盘 → 重跑今日选股（含决策卡）→ 补发邮件。

DuckDB 单写连接：执行前须确保无其他进程占用 quant.duckdb
（Docker 部署请用 scripts/deploy/server_repair_screener.sh 自动停/启 quant-ai）。

用法（项目根，服务已停）:
  python -u scripts/repair_prev_day_screener.py
  python -u scripts/repair_prev_day_screener.py --limit-up-date 20260522 --no-email
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("MOCK", "0")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="修正上一交易日涨停池并重跑今日选股（含决策卡、邮件）",
    )
    p.add_argument(
        "--limit-up-date",
        metavar="YYYYMMDD",
        default="",
        help="要重拉的涨停池日期（默认：上一交易日）",
    )
    p.add_argument(
        "--purge-cache-keys",
        default="",
        help="从 limit_up_cache 删除的键，逗号分隔",
    )
    p.add_argument(
        "--purge-non-trading-keys",
        action="store_true",
        help="自动删除 limit_up_cache 中周六/日的键",
    )
    p.add_argument(
        "--screener-only",
        action="store_true",
        help="仅重跑今日选股+决策卡（跳过涨停池与复盘）",
    )
    p.add_argument(
        "--skip-review",
        action="store_true",
        help="跳过复盘 sync-persist",
    )
    p.add_argument(
        "--skip-screener",
        action="store_true",
        help="跳过选股重跑（仅修涨停池/复盘）",
    )
    p.add_argument(
        "--no-email",
        action="store_true",
        help="重跑选股但不发邮件（默认行为，亦保留兼容）",
    )
    p.add_argument(
        "--send-email",
        action="store_true",
        help="重跑后强制发邮件（仍受 send_guard 时间窗口+当日幂等约束）",
    )
    p.add_argument(
        "--skip-dup-check",
        action="store_true",
        help="跳过 ledger_doc 重复键检查",
    )
    return p.parse_args()


def _check_ledger_dup_keys() -> int:
    from src.data.quant_db import reset_shared_connection
    from src.config import DATA_DIR
    import duckdb

    reset_shared_connection()
    db = DATA_DIR / "quant.duckdb"
    if not db.is_file():
        return 0
    con = duckdb.connect(str(db), read_only=True)
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT doc_key) FROM ledger_doc",
            ).fetchone()[0]
        )
    finally:
        con.close()


def _resolve_limit_up_date(arg: str) -> str:
    key = (arg or "").strip()
    if key:
        if len(key) != 8 or not key.isdigit():
            raise SystemExit(f"invalid --limit-up-date: {key!r}")
        return key
    from src.data.zt_pool_api import prev_trading_date_ymd

    return prev_trading_date_ymd()


def _weekend_cache_keys(cache: dict) -> list[str]:
    from datetime import datetime

    out: list[str] = []
    for k in cache:
        if len(str(k)) != 8 or not str(k).isdigit():
            continue
        try:
            if datetime.strptime(str(k), "%Y%m%d").weekday() >= 5:
                out.append(str(k))
        except ValueError:
            continue
    return sorted(out)


def _apply_cache_purge(cache: dict, explicit: list[str], purge_weekends: bool) -> list[str]:
    removed: list[str] = []
    for k in explicit:
        if k in cache:
            cache.pop(k, None)
            removed.append(k)
    if purge_weekends:
        for k in _weekend_cache_keys(cache):
            if k in cache:
                cache.pop(k, None)
                removed.append(k)
    return removed


def main() -> int:
    args = _parse_args()

    if args.screener_only:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "rerun_screener_today",
            ROOT / "scripts" / "rerun_screener_today.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod.run(
            no_email=(args.no_email or not args.send_email),
            skip_dup_check=args.skip_dup_check,
        )

    dup = _check_ledger_dup_keys()
    print(f"[preflight] ledger dup_keys={dup}")
    if dup and not args.skip_dup_check:
        print("FAIL: ledger_doc 仍有重复 doc_key，请先 dedupe 或加 --skip-dup-check")
        return 2

    ymd = _resolve_limit_up_date(args.limit_up_date)
    print(f"[1/4] sync_limit_up_cache_from_zt_pool date={ymd} …")
    from src.data.models import init_db

    init_db()

    from src.data.fetcher import fetch_limit_up_history, sync_limit_up_cache_from_zt_pool

    n = sync_limit_up_cache_from_zt_pool(ymd)
    print(f"      wrote {n} rows")

    purge_keys = [k.strip() for k in (args.purge_cache_keys or "").split(",") if k.strip()]
    for k in purge_keys:
        if len(k) != 8 or not k.isdigit():
            raise SystemExit(f"invalid purge key: {k!r}")

    if purge_keys or args.purge_non_trading_keys:
        from src.config import DATA_DIR
        from src.data.json_io import dump_json_file, load_json_file

        cache = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
        removed = _apply_cache_purge(cache, purge_keys, args.purge_non_trading_keys)
        if removed:
            dump_json_file(DATA_DIR / "limit_up_cache.json", cache)
            print(f"      purged cache keys: {sorted(set(removed))}")

    hist = fetch_limit_up_history(days=5)
    summary = {}
    for k, v in sorted(hist.items()):
        summary[k] = len(v) if hasattr(v, "__len__") else "?"
    print(f"      limit_up_cache: {summary}")

    if not args.skip_review:
        print("[2/4] sync_review_payload_for_api(persist=True) …")
        from src.engine.daily_review import sync_review_payload_for_api
        from src.engine.screener_market_env import resolve_review_document_for_api

        rd = resolve_review_document_for_api()
        if not rd:
            print("      WARN: 无复盘文档，跳过")
        else:
            out = sync_review_payload_for_api(dict(rd), persist=True)
            print(
                f"      review date={out.get('date')} "
                f"limit_up={out.get('limit_up_count')} "
                f"groups={len(out.get('prev_board_groups') or [])} "
                f"persisted={out.get('_persisted')}"
            )
    else:
        print("[2/4] skip review")

    if not args.skip_screener:
        # 默认不发邮件（安全）；仅在显式 --send-email 且没传 --no-email 时才发
        skip_email = True if (args.no_email or not args.send_email) else False
        print(f"[3/4] run_screener_update(skip_email={skip_email}) …")
        print("      提示: 需全市场竞价 spot 可用（建议 9:27 前后）；接口失败会导致 0 命中")
        from src.scheduler import run_screener_update

        res = run_screener_update(skip_email=skip_email)
        if isinstance(res, dict) and res.get("status") == "skipped":
            print(f"      WARN: screener skipped: {res.get('reason')}")
        else:
            date_s = res.get("date") if isinstance(res, dict) else str(res)
            hits_n = len((res or {}).get("hits", [])) if isinstance(res, dict) else "?"
            print(f"      screener date={date_s} hits={hits_n}")
    else:
        print("[3/4] skip screener")

    print("[4/4] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""仅重跑今日选股链：latest_screener / latest_auction_scores / latest_advice / 邮件。

不含涨停池修正、不含复盘 sync。须独占 quant.duckdb（先 docker stop quant-ai）。

用法（镜像内已含 scripts/，只需挂载 data）:
  docker run --rm --env-file /opt/ai-quant-system/.env -e TZ=Asia/Shanghai \\
    -v /opt/ai-quant-system/data:/app/data \\
    registry.cn-hangzhou.aliyuncs.com/kczj/ai-quant-service:TAG \\
    python -u scripts/rerun_screener_today.py --auction-time
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("MOCK", "0")

# 今日预期命中（2026-05-25 竞价口径，供补跑后校验）
EXPECTED_HIT_CODES = frozenset({"002442", "600303"})


@contextmanager
def _patch_auction_time_927():
    """将 now_cn 固定为今日 09:27:30，使量比/富化走竞价分支。"""
    from src.config import now_cn as real_now_cn

    def fake_now_cn():
        n = real_now_cn()
        return n.replace(hour=9, minute=27, second=30, microsecond=0)

    patches = [
        patch("src.config.now_cn", fake_now_cn),
        patch("src.engine.screener.now_cn", fake_now_cn),
        patch("src.scheduler.now_cn", fake_now_cn),
    ]
    for p in patches:
        p.start()
    try:
        print("      [auction-time] 时钟模拟为今日 09:27:30（竞价口径）")
        yield
    finally:
        for p in patches:
            p.stop()


def _print_hits_summary(hits: list) -> None:
    for h in hits:
        if isinstance(h, dict):
            print(
                f"      · {h.get('code')} {h.get('name')} "
                f"{h.get('continuous_limit_up')}板 竞价{h.get('auction_gain')}%"
            )
        else:
            print(f"      · {h}")


def run(
    *,
    no_email: bool = True,
    send_email: bool = False,
    skip_dup_check: bool = False,
    auction_time: bool = False,
) -> int:
    from src.config import DATA_DIR, now_cn
    from src.data.quant_db import reset_shared_connection
    import duckdb

    reset_shared_connection()
    db = DATA_DIR / "quant.duckdb"
    if db.is_file() and not skip_dup_check:
        con = duckdb.connect(str(db), read_only=True)
        try:
            dup = int(
                con.execute(
                    "SELECT COUNT(*) - COUNT(DISTINCT doc_key) FROM ledger_doc",
                ).fetchone()[0]
            )
        finally:
            con.close()
        print(f"[preflight] ledger dup_keys={dup}")
        if dup:
            print("FAIL: ledger_doc 重复 doc_key")
            return 2

    from src.data.models import init_db

    init_db()

    # 默认不发邮件（安全）；显式 --send-email 且没传 --no-email 时才发。
    # 邮件资格最终由 send_guard 中央守卫判定（2.6 升级）：
    # 非交易时段（凌晨/晚间/午休/周末）+ 当日已发过 都硬拦截。
    skip_email = True if (no_email or not send_email) else False
    print(f"[1/2] run_screener_update(skip_email={skip_email}) @ {now_cn()} …")
    if auction_time:
        print("      使用 --auction-time：量比/富化按 9:27 竞价分支（open/pre_close 仍来自当前 spot）")
    else:
        print("      提示: 补跑建议加 --auction-time；否则午间第三方量比可能误杀")

    from src.engine.screener import LAST_AUCTION_SPOT_STATUS
    from src.scheduler import run_screener_update

    ctx = _patch_auction_time_927() if auction_time else _null_context()
    with ctx:
        res = run_screener_update(skip_email=skip_email)

    if isinstance(res, dict) and res.get("status") == "skipped":
        print(f"FAIL: {res.get('reason')}")
        return 3

    spot_rows = int((res or {}).get("_spot_rows") or 0)
    spot_src = str((res or {}).get("_spot_source") or LAST_AUCTION_SPOT_STATUS)
    print(f"      spot_rows={spot_rows} spot_source={spot_src}")
    if spot_rows <= 0 or spot_src == "empty":
        print("FAIL: 竞价快照全源失败（含腾讯 universe）")
        return 5

    hits = (res or {}).get("hits", []) if isinstance(res, dict) else []
    print(f"      hits={len(hits)} date={res.get('date') if isinstance(res, dict) else res}")
    if hits:
        _print_hits_summary(hits)

    from src.data.json_io import load_json_file

    scores = load_json_file(DATA_DIR / "latest_auction_scores.json")
    n_scores = len(scores) if isinstance(scores, list) else 0
    print(f"[2/2] latest_auction_scores: {n_scores} 条")

    if len(hits) == 0:
        print("WARN: 选股 0 命中")
        return 4

    print("[3/3] ensure_today_archived …")
    from src.engine.screener_history import ensure_today_archived

    n_hist = ensure_today_archived()
    print(f"      screener_history 今日 {n_hist} 条")

    got = {str(h.get("code", "")).zfill(6)[-6:] for h in hits if isinstance(h, dict)}
    if EXPECTED_HIT_CODES and got != EXPECTED_HIT_CODES and not got.issuperset(EXPECTED_HIT_CODES):
        print(f"WARN: 命中 {sorted(got)}，预期至少含 {sorted(EXPECTED_HIT_CODES)}")
    elif got >= EXPECTED_HIT_CODES:
        print(f"OK: 命中含预期 {sorted(EXPECTED_HIT_CODES)}")
    return 0


@contextmanager
def _null_context():
    yield


def main() -> int:
    p = argparse.ArgumentParser(description="重跑今日选股 + 决策卡 + 可选邮件")
    p.add_argument(
        "--auction-time",
        action="store_true",
        help="模拟 09:27:30 时钟（竞价量比/富化口径，补跑推荐）",
    )
    p.add_argument("--no-email", action="store_true", help="不发邮件（默认行为）")
    p.add_argument(
        "--send-email",
        action="store_true",
        help="强制发邮件（仍受 send_guard 时间窗口+当日幂等约束）",
    )
    p.add_argument(
        "--sync-history-only",
        action="store_true",
        help="仅从 latest_screener / daily_screener_hit 补写今日选股记录，不重跑选股",
    )
    p.add_argument("--skip-dup-check", action="store_true")
    args = p.parse_args()
    if args.sync_history_only:
        from src.data.models import init_db
        from src.engine.screener_history import ensure_today_archived

        init_db()
        n = ensure_today_archived()
        print(f"screener_history 今日 {n} 条")
        return 0 if n > 0 else 1
    return run(
        no_email=args.no_email,
        send_email=args.send_email,
        skip_dup_check=args.skip_dup_check,
        auction_time=args.auction_time,
    )


if __name__ == "__main__":
    raise SystemExit(main())

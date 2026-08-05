#!/usr/bin/env python3
"""从损坏前 DuckDB 备份合并历史选股、看板、趋势、排行等到当前 quant.duckdb。

不覆盖 daily_kline / minute_kline（重建后可能正在增量写入）。
默认源库：backups/quant_corrupt_20260519_224431.duckdb

用法（项目根）:
  python scripts/merge_duckdb_from_corrupt_backup.py
  python scripts/merge_duckdb_from_corrupt_backup.py --src backups/quant_corrupt_20260519_224431.duckdb
  python scripts/merge_duckdb_from_corrupt_backup.py --json-only   # 仅从 backups/*.json 强灌 trend/snapshot
  python scripts/merge_duckdb_from_corrupt_backup.py --repair-advice   # 仅补 daily_advice
  python scripts/merge_duckdb_from_corrupt_backup.py --merge-kline     # 合并 K 线（主键去重，保留当前库已有行）
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 源库行数应不少于目标才整表替换；否则跳过（避免用空表覆盖）
_REPLACE_IF_SRC_GE: tuple[str, ...] = (
    "screener_history_entry",
    "daily_screener_hit",
    "daily_ranking",
    "daily_json_blob",
    "ledger_doc",
    "daily_advice",
    "daily_sentiment",
    "concept_info",
    "concept_members",
    "industry_member",
)

_SKIP_TABLES: frozenset[str] = frozenset(
    {
        "daily_kline",
        "minute_kline",
        "kline_series_meta",
        "auction_session",
    }
)

_JSON_FORCE_KEYS: tuple[str, ...] = (
    "latest_trend.json",
    "latest_snapshot.json",
    "trend_history.json",
    "trend_sector_history.json",
)


def _count(conn, schema: str, table: str) -> int:
    try:
        return int(
            conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()[0]
        )
    except Exception:
        return -1


def _replace_table(conn, table: str) -> str:
    n_dst = _count(conn, "main", table)
    n_src = _count(conn, "src", table)
    if n_src < 0:
        return f"skip_src_missing(n_dst={n_dst})"
    if n_src == 0:
        return f"skip_src_empty(n_dst={n_dst})"
    if n_dst >= 0 and n_src < n_dst:
        return f"skip_src_smaller(src={n_src},dst={n_dst})"
    conn.execute(f'DELETE FROM main."{table}"')
    if table == "daily_advice":
        conn.execute(
            """
            INSERT INTO main.daily_advice
            SELECT * FROM (
                SELECT DISTINCT ON (advice_date) *
                FROM src.daily_advice
                ORDER BY advice_date, updated_at DESC
            ) t
            """
        )
    else:
        conn.execute(f'INSERT INTO main."{table}" SELECT * FROM src."{table}"')
    return f"replaced(src={n_src},was_dst={n_dst})"


def _repair_daily_advice(conn) -> str:
    """从 src 灌入 daily_advice（源表按 advice_date 去重）。"""
    n_dst = _count(conn, "main", "daily_advice")
    n_src = _count(conn, "src", "daily_advice")
    if n_src <= 0:
        return f"skip_src_empty(n_dst={n_dst})"
    conn.execute("DELETE FROM main.daily_advice")
    conn.execute(
        """
        INSERT INTO main.daily_advice
        SELECT * FROM (
            SELECT DISTINCT ON (advice_date) *
            FROM src.daily_advice
            ORDER BY advice_date, updated_at DESC
        ) t
        """
    )
    n_after = _count(conn, "main", "daily_advice")
    return f"advice_rows={n_after}(src_distinct_from={n_src},was_dst={n_dst})"


def _merge_daily_kline(conn) -> str:
    """将 src.daily_kline 并入 main：主键冲突时保留 main（新拉），仅补缺。"""
    before = _count(conn, "main", "daily_kline")
    conn.execute(
        """
        INSERT INTO main.daily_kline
        SELECT s.*
        FROM src.daily_kline AS s
        WHERE NOT EXISTS (
            SELECT 1 FROM main.daily_kline AS m
            WHERE m.stock_code = s.stock_code
              AND m.trade_date = s.trade_date
              AND m.scale = s.scale
              AND m.source_datalen = s.source_datalen
        )
        """
    )
    after = _count(conn, "main", "daily_kline")
    added = after - before
    if _count(conn, "src", "kline_series_meta") > 0:
        conn.execute(
            """
            INSERT INTO main.kline_series_meta
            SELECT s.*
            FROM src.kline_series_meta AS s
            WHERE NOT EXISTS (
                SELECT 1 FROM main.kline_series_meta AS m
                WHERE m.stock_code = s.stock_code
                  AND m.scale = s.scale
                  AND m.source_datalen = s.source_datalen
            )
            """
        )
    meta = _count(conn, "main", "kline_series_meta")
    return f"kline_rows={after}(+{added} from src, was {before}), meta={meta}"


def _hydrate_json_keys(force_keys: tuple[str, ...]) -> dict[str, str]:
    from src.config import DATA_DIR
    from src.data.json_io import dump_json_file
    from src.data.latest_snapshot_hydrate import read_newest_backup_json

    out: dict[str, str] = {}
    for doc_key in force_keys:
        raw = read_newest_backup_json(doc_key)
        if raw is None:
            out[doc_key] = "missing_backup"
            continue
        try:
            dump_json_file(DATA_DIR / doc_key, raw)
            out[doc_key] = "restored"
        except Exception as e:
            out[doc_key] = f"error:{e!s}"[:120]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--src",
        type=Path,
        default=ROOT / "backups" / "quant_corrupt_20260519_224431.duckdb",
    )
    ap.add_argument("--no-db-backup", action="store_true")
    ap.add_argument(
        "--json-only",
        action="store_true",
        help="仅从 backups JSON 强灌 trend/snapshot，不合并 DuckDB",
    )
    ap.add_argument(
        "--repair-advice",
        action="store_true",
        help="仅从源库恢复 daily_advice（按 advice_date 去重）",
    )
    ap.add_argument(
        "--merge-kline",
        action="store_true",
        help="将源库 daily_kline 并入当前库（主键去重，冲突保留当前行）",
    )
    args = ap.parse_args()

    from src.config import BASE_DIR, DB_PATH

    if args.json_only:
        summary = _hydrate_json_keys(_JSON_FORCE_KEYS)
        print("[merge] JSON 强灌:", summary)
        return 0

    src = args.src.resolve()

    if args.repair_advice or args.merge_kline:
        if not src.is_file():
            print("[merge] 源库不存在:", src, file=sys.stderr)
            return 1
        dst = Path(DB_PATH).resolve()
        if not dst.is_file():
            print("[merge] 目标库不存在:", dst, file=sys.stderr)
            return 2
        if not args.no_db_backup:
            from src.config import BASE_DIR

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = (BASE_DIR / "backups" / f"quant_before_kline_advice_{ts}.duckdb").resolve()
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bak)
            print("[merge] 已备份当前库 →", bak)
        from src.data.quant_db import connect, reset_shared_connection

        reset_shared_connection()
        conn = connect()
        try:
            conn.execute(f"ATTACH '{src.as_posix()}' AS src (READ_ONLY)")
            if args.repair_advice:
                print("[merge] daily_advice:", _repair_daily_advice(conn))
            if args.merge_kline:
                print("[merge] daily_kline:", _merge_daily_kline(conn))
        finally:
            try:
                conn.execute("DETACH src")
            except Exception:
                pass
            conn.close()
        reset_shared_connection()
        print("[merge] 完成。")
        return 0

    dst = Path(DB_PATH).resolve()
    if not src.is_file():
        print("[merge] 源库不存在:", src, file=sys.stderr)
        return 1
    if not dst.is_file():
        print("[merge] 目标库不存在:", dst, file=sys.stderr)
        return 2

    if not args.no_db_backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = (BASE_DIR / "backups" / f"quant_before_merge_{ts}.duckdb").resolve()
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, bak)
        print("[merge] 已备份当前库 →", bak)

    json_summary = _hydrate_json_keys(_JSON_FORCE_KEYS)
    print("[merge] JSON 强灌:", json_summary)

    from src.data.quant_db import connect, reset_shared_connection

    reset_shared_connection()
    conn = connect()
    try:
        conn.execute(f"ATTACH '{src.as_posix()}' AS src (READ_ONLY)")
        src_tables = {
            str(r[0])
            for r in conn.execute(
                """
                SELECT table_name FROM duckdb_tables()
                WHERE database_name = 'src' AND schema_name = 'main'
                """
            ).fetchall()
        }
        results: dict[str, str] = {}
        for table in _REPLACE_IF_SRC_GE:
            if table in _SKIP_TABLES:
                results[table] = "skip_policy"
                continue
            if table not in src_tables:
                results[table] = "skip_not_in_src"
                continue
            try:
                results[table] = _replace_table(conn, table)
            except Exception as e:
                results[table] = f"error:{e!s}"[:160]
        print("[merge] 表合并结果:")
        for t, msg in results.items():
            print(f"  {t:28s} {msg}")
    finally:
        try:
            conn.execute("DETACH src")
        except Exception:
            pass
        conn.close()

    reset_shared_connection()
    print("[merge] 完成。请重启服务后在前端核对历史选股/看板/趋势。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

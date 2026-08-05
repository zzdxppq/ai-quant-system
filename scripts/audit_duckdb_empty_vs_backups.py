#!/usr/bin/env python3
"""检查本地 DuckDB 各表行数，并对照 backups 下 JSON 是否可补水（只读审计，不改库）。

升级时从备份灌库（补缺或 --force-backup）：
  python scripts/migrate_from_backup_on_upgrade.py
  或：python -m src.data.latest_snapshot_hydrate
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_JSON_TO_TABLES_HINT: dict[str, str] = {
    "latest_advice.json": "daily_advice（save_from_latest_filename）",
    "latest_sentiment.json": "daily_sentiment",
    "latest_leader.json": "daily_json_blob.blob_kind=latest_leader",
    "latest_review.json": "daily_json_blob.blob_kind=latest_review",
    "latest_ranking.json": "daily_ranking",
    "latest_auction_scores.json": "daily_auction_scores（JSON 为 list）",
    "latest_screener.json": "daily_screener_hit",
    "screener_history.json": "screener_history_entry（整表 list）",
    "latest_insight.json": "daily_json_blob.blob_kind=latest_insight",
    "latest_signals.json": "daily_json_blob.blob_kind=latest_signals",
    "latest_deviation.json": "daily_json_blob.blob_kind=latest_deviation",
    "latest_trend.json": "daily_json_blob.blob_kind=latest_trend",
    "latest_snapshot.json": "daily_json_blob.blob_kind=latest_snapshot",
    "review_history.json": "ledger_doc（is_ledger_doc_key）",
    "sentiment_history.json": "ledger_doc",
    "cycle_state.json": "ledger_doc",
    "cycle_history.json": "ledger_doc",
    "decision_records.json": "ledger_doc",
    "limit_up_cache.json": "ledger_doc",
    "industry_cache.json": "industry_member 等",
    "concept_cache.json": "concept_info + concept_members",
}


def _backup_dirs() -> list[Path]:
    b = ROOT / "backups"
    if not b.is_dir():
        return []
    dirs = [p for p in b.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.name, reverse=True)
    return dirs


def _json_files_in_backups() -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for d in _backup_dirs():
        for fp in d.glob("*.json"):
            found[fp.name].append(d.name)
    return dict(found)


def main() -> int:
    from src.config import DB_PATH
    from src.data.latest_snapshot_hydrate import db_needs_hydrate_for_doc_key
    from src.data.quant_db import connect

    dbp = Path(DB_PATH)
    print(f"数据库文件: {dbp.resolve()}")
    conn = None
    if dbp.is_file():
        conn = connect()

    table_counts: list[tuple[str, int]] = []
    blob_by_kind: dict[str, int] = {}
    ledger_rows = -1
    if conn is not None:
        try:
            rows = conn.execute(
                """
                SELECT table_name FROM duckdb_tables()
                WHERE schema_name = 'main'
                  AND lower(table_name) NOT LIKE 'sqlite_%'
                ORDER BY table_name
                """
            ).fetchall()
            for (tname,) in rows:
                tn = str(tname)
                try:
                    n = conn.execute(f'SELECT COUNT(*) FROM "{tn}"').fetchone()[0]
                    n = int(n)
                except Exception as e:
                    n = -1
                    print(f"  [warn] COUNT {tn}: {e}")
                table_counts.append((tn, n))
                if tn == "daily_json_blob":
                    try:
                        cur = conn.execute(
                            "SELECT blob_kind, COUNT(*) FROM daily_json_blob GROUP BY blob_kind ORDER BY blob_kind"
                        )
                        for k, c in cur.fetchall():
                            blob_by_kind[str(k)] = int(c)
                    except Exception as e:
                        print(f"  [warn] daily_json_blob 分组: {e}")
                if tn == "ledger_doc":
                    ledger_rows = n
        finally:
            conn.close()

    print("\n=== 表行数（main 库）===")
    if not table_counts:
        print("  (无连接或无表)")
    else:
        empty = [t for t, c in table_counts if c == 0]
        for t, c in table_counts:
            mark = "  << 空" if c == 0 else ""
            print(f"  {t:40s} {c:>10}{mark}")
        if empty:
            print(f"\n空表共 {len(empty)} 个: {', '.join(empty)}")
        if blob_by_kind:
            print("\n--- daily_json_blob 按 kind ---")
            for k in sorted(blob_by_kind.keys()):
                print(f"  {k:32s} {blob_by_kind[k]:>8}")

    backup_json = _json_files_in_backups()
    print("\n=== backups 下 *.json（按文件名聚合）===")
    if not backup_json:
        print("  (无 backups 子目录或其中无 .json)")
    else:
        for name in sorted(backup_json.keys()):
            hint = _JSON_TO_TABLES_HINT.get(name, "（经 dump_json_file：ledger / snapshot / relational）")
            dirs_preview = ", ".join(backup_json[name][:3])
            if len(backup_json[name]) > 3:
                dirs_preview += ", …"
            print(f"  {name:28s} 出现于: {dirs_preview}")
            print(f"      → {hint}")

    print("\n=== 按「当前库 load 语义」是否仍缺（可补水）===")
    if not backup_json:
        print("  无备份文件可对照")
        return 0
    need = []
    skip = []
    for name in sorted(backup_json.keys()):
        try:
            if db_needs_hydrate_for_doc_key(name):
                need.append(name)
            else:
                skip.append(name)
        except Exception as e:
            need.append(f"{name} (check_error: {e!s})"[:120])

    if need:
        print(f"  仍缺或空列表（建议升级时 migrate）共 {len(need)} 个:")
        for n in need:
            print(f"    - {n}")
    else:
        print("  无：备份中出现的 doc_key 在库中均有有效数据（或无法判定）。")
    print(f"  已有数据跳过: {len(skip)} 个")

    print("\n=== 粗粒度表级提示（空表 + 备份有同名 JSON）===")
    if not table_counts or not backup_json:
        print("  跳过")
        return 0

    def empty_table(name: str) -> bool:
        for t, c in table_counts:
            if t == name:
                return c == 0
        return False

    suggestions: list[str] = []
    if empty_table("screener_history_entry") and "screener_history.json" in backup_json:
        suggestions.append("screener_history_entry 为空 + 备份有 screener_history.json → migrate_from_backup_on_upgrade")
    if (blob_by_kind.get("latest_review", 0) == 0) and "latest_review.json" in backup_json:
        if not empty_table("daily_json_blob") or blob_by_kind.get("latest_review", 0) == 0:
            suggestions.append("daily_json_blob 无 latest_review + 备份有 latest_review.json → 同上全量 migrate")
    if ledger_rows == 0 and any(
        k in backup_json
        for k in (
            "review_history.json",
            "sentiment_history.json",
            "cycle_state.json",
        )
    ):
        suggestions.append("ledger_doc 为空且备份含 history/state → migrate_from_backup_on_upgrade 会灌入 ledger_doc")

    if not suggestions:
        print("  无额外表级组合；详见上一节「按 load 语义」。")
        for t in ("cycle_state", "daily_quote", "gain_ranking"):
            if any(x[0] == t and x[1] == 0 for x in table_counts):
                print(
                    f"  说明：{t} 为空通常需跑周期/行情任务或 ORM，"
                    "不能仅靠 backups 根级 JSON 补水。"
                )
    else:
        for line in suggestions:
            print(f"  - {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

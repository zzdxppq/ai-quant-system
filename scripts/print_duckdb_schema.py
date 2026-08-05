#!/usr/bin/env python3
"""打印 quant.duckdb 中 main 库全部表、字段；列注释来自 duckdb_columns.comment（多为空）。

用法:
  python scripts/print_duckdb_schema.py
  python scripts/print_duckdb_schema.py --out docs/duckdb_schema.txt
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 表级说明（库内列 COMMENT 多为空；与 src/data 分工一致）
_TABLE_NOTES: dict[str, str] = {
    "auction_session": "结构化：单股竞价会话原始/聚合 JSON（structured_store）",
    "concept_info": "分析：概念元数据（analytics_store）",
    "concept_members": "分析：概念-成分股-日期（analytics_store）",
    "cycle_state": "ORM：周期引擎单行状态（SQLAlchemy models，可与 ledger cycle_state.json 并存）",
    "daily_advice": "分析：9:27 决策快照行表（latest_advice 真源）",
    "daily_auction_scores": "分析：竞价打分卡按日按股（latest_auction_scores）",
    "daily_json_blob": "分析：未拆表的 latest_* 整包 JSON（如 latest_review、latest_leader blob）",
    "daily_kline": "结构化：日 K 线序列",
    "daily_quote": "ORM：日线行情样本/回测",
    "daily_ranking": "分析：10 日涨幅榜行表（latest_ranking）",
    "daily_screener_hit": "分析：选股命中按日按股（latest_screener）",
    "daily_sentiment": "分析：梯队情绪池行表（latest_sentiment）",
    "daily_snapshot": "遗留：旧版 snapshot_kind + payload_json（迁移后可空）",
    "gain_ranking": "ORM：涨幅排行",
    "industry_member": "分析：行业-成分股映射",
    "kline_series_meta": "结构化：K 线缓存元数据",
    "ledger_doc": "大块业务 JSON 真源（doc_key → body_json）",
    "minute_kline": "结构化：分时 K 线",
    "screener_history_entry": "分析：选股历史展开行（screener_history.json 主存）",
    "stock_basic": "结构化：全市场代码-名称表",
    "app_json_doc_registry": "relational：doc_key → jdt_* 物理表名",
    "app_json_doc_colmap": "relational：拍平列与 JSON path 映射",
}


def build_schema_report() -> str:
    from src.config import DB_PATH
    from src.data.quant_db import connect

    lines: list[str] = []
    conn = connect()
    try:
        tables = [
            str(r[0])
            for r in conn.execute(
                """
                SELECT table_name FROM duckdb_tables()
                WHERE schema_name = 'main' AND NOT internal
                ORDER BY table_name
                """
            ).fetchall()
        ]

        lines.append("数据库文件: " + str(DB_PATH.resolve()))
        lines.append("schema: main")
        lines.append("共计表数量: " + str(len(tables)))
        lines.append("")

        for tname in tables:
            note = _TABLE_NOTES.get(tname, "（未在脚本字典中标注，见 duckdb_tables / 业务代码）")
            lines.append("--- 表: " + tname + " ---")
            lines.append("    表说明: " + note)
            cols = conn.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default, comment
                FROM duckdb_columns()
                WHERE database_name = current_database()
                  AND schema_name = 'main'
                  AND table_name = ?
                ORDER BY column_index NULLS LAST, column_name
                """,
                (tname,),
            ).fetchall()
            if not cols:
                lines.append("  (duckdb_columns 无结果，尝试 DESCRIBE)")
                safe = tname.replace('"', '""')
                for row in conn.execute(f'DESCRIBE "{safe}"').fetchall():
                    lines.append("  " + repr(row))
                lines.append("")
                continue
            for cn, dt, nul, dft, cmt in cols:
                parts = [str(cn), "类型=" + str(dt)]
                if nul is True or str(nul).upper() == "YES":
                    parts.append("可空")
                if dft is not None and str(dft).strip():
                    parts.append("默认=" + str(dft))
                line = "  " + " | ".join(parts)
                if cmt:
                    line += "\n      列说明: " + str(cmt)
                lines.append(line)
            lines.append("")
    finally:
        conn.close()
    return "\n".join(lines)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="打印 DuckDB main 表结构")
    ap.add_argument(
        "--out",
        type=str,
        default="",
        help="写入 UTF-8 文本路径（可选）",
    )
    args = ap.parse_args()

    text = build_schema_report()
    print(text)
    if args.out.strip():
        outp = Path(args.out.strip())
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(text, encoding="utf-8")
        print("[已写入]", outp.resolve(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

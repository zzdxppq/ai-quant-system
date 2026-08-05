"""剩余 data/*.json 文档的真源：单表 ledger_doc(doc_key, body_json)，替代 jdt_* 宽表/行表。

- 与 SQLAlchemy 的 cycle_state / gain_ranking 等 ORM 表无关；cycle_state.json 仅写入本表 doc_key='cycle_state.json'。
- 读写走本模块（quant / DuckDB 进程内单连接池）。
- 写入用 UPDATE/INSERT 替代 DELETE+INSERT，并移除 updated_at 二级索引，避免 DuckDB ART 删除异常。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional

from src.config import now_cn
from src.data.quant_db import connect as _db_connect
from src.data.quant_db import executescript_compat

_LOCK = threading.RLock()
_SCHEMA_READY = False

_TABLE = "ledger_doc"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    doc_key TEXT NOT NULL PRIMARY KEY,
    body_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _drop_broken_updated_index(conn: Any) -> None:
    """ix_ledger_doc_updated(updated_at) 在部分 DuckDB 版本上会导致按 doc_key DELETE 触发 ART 损坏。"""
    try:
        conn.execute("DROP INDEX IF EXISTS ix_ledger_doc_updated")
    except Exception:
        pass


def _dedupe_ledger_doc_keys(conn: Any) -> None:
    """清理无 PRIMARY KEY 遗留库中的重复 doc_key。"""
    try:
        conn.execute(
            f"""
            DELETE FROM {_TABLE}
            WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM {_TABLE} GROUP BY doc_key
            )
            """
        )
    except Exception:
        pass


def init_ledger_doc_schema() -> None:
    global _SCHEMA_READY
    with _LOCK:
        if _SCHEMA_READY:
            return
        conn = _db_connect()
        try:
            executescript_compat(conn, _DDL)
            _drop_broken_updated_index(conn)
            _dedupe_ledger_doc_keys(conn)
            _SCHEMA_READY = True
        finally:
            conn.close()


# 从 jdt 迁出、不再走 relational save_document 的 doc_key（含 report_YYYY_MM.json）
_LEDGER_EXACT: frozenset[str] = frozenset(
    {
        "review_history.json",
        "sentiment_history.json",
        "cycle_state.json",
        "cycle_history.json",
        "limit_up_cache.json",
        "decision_records.json",
        "my_holdings.json",
        "prev_ranking.json",
        "top30_streak_state.json",
        "trend_history.json",
        "trend_sector_history.json",
        "trend_pool_manual.json",
        "trend_history_manual.json",
        "active_attack_history.json",
        "sanbanzhu_lhb_cache.json",
        "_stock_list_cache.json",
        "concept_cache.json.partial",
    }
)


def is_ledger_doc_key(doc_key: str | None) -> bool:
    if not doc_key:
        return False
    if doc_key in _LEDGER_EXACT:
        return True
    low = doc_key.lower()
    if low.startswith("report_") and low.endswith(".json"):
        return True
    return False


def upsert_json(doc_key: str, obj: Any) -> None:
    """写入/覆盖 ledger 行（UPDATE 优先，避免 DELETE 触发损坏的 updated_at 索引）。"""
    init_ledger_doc_schema()
    body = json.dumps(obj, ensure_ascii=False)
    ts = now_cn().isoformat()
    with _LOCK:
        conn = _db_connect()
        try:
            _drop_broken_updated_index(conn)
            row = conn.execute(
                f"SELECT rowid FROM {_TABLE} WHERE doc_key = ? LIMIT 1",
                (doc_key,),
            ).fetchone()
            if row:
                conn.execute(
                    f"UPDATE {_TABLE} SET body_json = ?, updated_at = ? WHERE rowid = ?",
                    (body, ts, int(row[0])),
                )
            else:
                conn.execute(
                    f"INSERT INTO {_TABLE}(doc_key, body_json, updated_at) VALUES (?,?,?)",
                    (doc_key, body, ts),
                )
        finally:
            conn.close()


def delete_ledger_doc_key(doc_key: str) -> None:
    """删除 ledger 行（按 rowid 逐条删，避开损坏索引上的 doc_key DELETE）。"""
    init_ledger_doc_schema()
    with _LOCK:
        conn = _db_connect()
        try:
            _drop_broken_updated_index(conn)
            rows = conn.execute(
                f"SELECT rowid FROM {_TABLE} WHERE doc_key = ?",
                (doc_key,),
            ).fetchall()
            for (rid,) in rows:
                conn.execute(f"DELETE FROM {_TABLE} WHERE rowid = ?", (int(rid),))
        finally:
            conn.close()


def load_json(doc_key: str) -> Optional[Any]:
    init_ledger_doc_schema()
    with _LOCK:
        conn = _db_connect()
        try:
            cur = conn.execute(
                f"SELECT body_json FROM {_TABLE} WHERE doc_key = ?",
                (doc_key,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        return json.loads(str(row[0]))
    except (json.JSONDecodeError, TypeError):
        return None


def list_ledger_doc_keys_glob(glob_pat: str) -> list[str]:
    """与 app_json_doc_registry 的 GLOB 语义一致，供 data_dir_glob_json 合并列举。"""
    init_ledger_doc_schema()
    pat = glob_pat.replace("\\", "/")
    with _LOCK:
        conn = _db_connect()
        try:
            cur = conn.execute(
                f"SELECT doc_key FROM {_TABLE} WHERE doc_key GLOB ? ORDER BY doc_key DESC",
                (pat,),
            )
            return [str(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()

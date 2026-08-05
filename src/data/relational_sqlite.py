"""文档层（SQLite / DuckDB）：与 quant 库内其它表分工如下。

- **structured_store**：K 线、分时、竞价、latest_* 日快照（规范化/JSON 列）。
- **analytics_store**：概念/行业关系、日级 ranking/sentiment 等。
- **ledger_doc_store**：大块业务 JSON（review/cycle/history/report_* 等，整段 body_json）。
- **本模块**：`app_json_doc_registry` + `jdt_*` 宽表/行表，仅承载上述三者未覆盖、且仍须拍平存库的 doc_key；
  `save_document` / `load_document` 对 ledger 与 concept/industry 键已做委托，避免误建 jdt。
- 若配置 `SKIP_JSON_DOC_REGISTRY=1`（见 `src.config`）：不再创建 registry/colmap；非 ledger 文档一律写入 `ledger_doc`；
  遗留 **jdt_*** 可由 `scripts/drop_json_doc_registry.py` 与 `drop_orphan_jdt_tables()` 清理。

遗留的、无 registry 指向的 **jdt_*** 表可用 `drop_orphan_jdt_tables()` 清理。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Any, Literal, Optional

from src.data.json_flat_store import Typ, flatten, unflatten
from src.data.quant_db import (
    connect as quant_connect,
    executescript_compat,
    is_locked_error,
    list_main_tables_prefixed,
    table_column_names,
    table_exists,
)

_LOCK = threading.RLock()

_REGISTRY = "app_json_doc_registry"
_COLMAP = "app_json_doc_colmap"

# SQLite 默认 SQLITE_MAX_COLUMN=2000，预留 id + 余量
WIDE_MAX_LEAVES = 1900


def _skip_json_doc_registry() -> bool:
    from src import config as _cfg

    return bool(getattr(_cfg, "SKIP_JSON_DOC_REGISTRY", False))

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_REGISTRY} (
    doc_key TEXT NOT NULL PRIMARY KEY,
    data_table TEXT NOT NULL UNIQUE,
    tbl_mode TEXT NOT NULL CHECK (tbl_mode IN ('wide', 'row'))
);
CREATE TABLE IF NOT EXISTS {_COLMAP} (
    doc_key TEXT NOT NULL,
    col_name TEXT NOT NULL,
    json_path TEXT NOT NULL,
    typ TEXT NOT NULL,
    PRIMARY KEY (doc_key, col_name)
);
CREATE INDEX IF NOT EXISTS ix_json_registry_glob ON {_REGISTRY}(doc_key);
"""


def _connect() -> Any:
    return quant_connect()


def _data_table_slug_base(doc_key: str) -> str:
    """由 doc_key 生成可读表名主体：jdt_<路径转 slug>（不含冲突后缀）。"""
    s = doc_key.replace("\\", "/").strip()
    low = s.lower()
    if low.endswith(".json"):
        s = s[: -len(".json")]
    s = s.replace("/", "__")
    s = re.sub(r"[^0-9a-zA-Z_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "doc"
    name = f"jdt_{s.lower()}"
    if len(name) > 180:
        h = hashlib.sha256(doc_key.encode("utf-8")).hexdigest()[:10]
        stem = s[: 160 - len("jdt_") - 1 - len(h)].lower()
        name = f"jdt_{stem}_{h}"
    return name


def _data_table_name(doc_key: str, disambig: int = 0) -> str:
    base = _data_table_slug_base(doc_key)
    if disambig == 0:
        return base
    suf = hashlib.sha256(f"{doc_key}\0{disambig}".encode("utf-8")).hexdigest()[:8]
    tail = f"_{suf}"
    max_base = 200 - len(tail)
    if len(base) > max_base:
        base = base[:max_base].rstrip("_")
    return f"{base}{tail}"


def _col_for_path(path: str) -> str:
    return "c_" + hashlib.sha256(path.encode("utf-8")).hexdigest()


def _sql_type_for_typ(typ: Typ) -> str:
    if typ in ("i", "b"):
        return "INTEGER"
    if typ == "r":
        return "REAL"
    return "TEXT"


def _row_tuple(path: str, typ: Typ, val: Any) -> tuple[str, str, Any, Any, Any, Any]:
    if typ == "n":
        return path, typ, None, None, None, None
    if typ == "b":
        return path, typ, None, None, None, 1 if val else 0
    if typ == "i":
        return path, typ, None, None, int(val), None  # type: ignore[arg-type]
    if typ == "r":
        return path, typ, None, float(val), None, None  # type: ignore[arg-type]
    if typ == "s":
        return path, typ, str(val), None, None, None
    return path, typ, str(val), None, None, None


def init_schema() -> None:
    with _LOCK:
        conn = _connect()
        try:
            if not _skip_json_doc_registry():
                executescript_compat(conn, _DDL)
            conn.execute("DROP TABLE IF EXISTS app_doc_scalar")
            conn.execute("DROP TABLE IF EXISTS app_documents")
        finally:
            conn.close()
    from src.data.structured_store import init_structured_schema
    from src.data.analytics_store import init_analytics_schema
    from src.data.ledger_doc_store import init_ledger_doc_schema

    init_structured_schema()
    init_analytics_schema()
    init_ledger_doc_schema()


def _get_registry(conn: Any, doc_key: str) -> tuple[str, Literal["wide", "row"]] | None:
    cur = conn.execute(
        f"SELECT data_table, tbl_mode FROM {_REGISTRY} WHERE doc_key = ?",
        (doc_key,),
    )
    row = cur.fetchone()
    if not row:
        return None
    m = str(row[1])
    if m not in ("wide", "row"):
        m = "row"
    return str(row[0]), m  # type: ignore[return-value]


def _ensure_registry(
    conn: Any, doc_key: str, mode: Literal["wide", "row"]
) -> str:
    got = _get_registry(conn, doc_key)
    if got:
        tname, existing = got
        if existing == mode and _table_exists(conn, tname):
            return tname
        conn.execute(f"DROP TABLE IF EXISTS {tname}")
        conn.execute(f"DELETE FROM {_COLMAP} WHERE doc_key = ?", (doc_key,))
        conn.execute(f"DELETE FROM {_REGISTRY} WHERE doc_key = ?", (doc_key,))
    disambig = 0
    while True:
        tname = _data_table_name(doc_key, disambig)
        cur = conn.execute(
            f"SELECT 1 FROM {_REGISTRY} WHERE data_table = ?",
            (tname,),
        )
        if cur.fetchone() is None and not _table_exists(conn, tname):
            break
        disambig += 1
    if mode == "wide":
        conn.execute(
            f"CREATE TABLE {tname} (id INTEGER PRIMARY KEY NOT NULL CHECK (id = 1))"
        )
    else:
        conn.execute(
            f"""CREATE TABLE {tname} (
                path TEXT NOT NULL PRIMARY KEY,
                typ TEXT NOT NULL,
                str_v TEXT,
                real_v REAL,
                int_v INTEGER,
                bool_v INTEGER
            )"""
        )
    conn.execute(
        f"INSERT INTO {_REGISTRY}(doc_key, data_table, tbl_mode) VALUES (?,?,?)",
        (doc_key, tname, mode),
    )
    return tname


def _table_columns(conn: Any, tname: str) -> set[str]:
    return table_column_names(conn, tname)


def _save_wide(conn: Any, doc_key: str, tname: str, flat: list[tuple[str, Typ, Any]]) -> None:
    existing_cols = _table_columns(conn, tname)
    col_vals: dict[str, Any] = {}
    for path, typ, val in flat:
        col = _col_for_path(path)
        col_vals[col] = (typ, val)
        if col not in existing_cols:
            sql_t = _sql_type_for_typ(typ)
            conn.execute(f"ALTER TABLE {tname} ADD COLUMN {col} {sql_t}")
            existing_cols.add(col)
            conn.execute(
                f"INSERT INTO {_COLMAP}(doc_key, col_name, json_path, typ) VALUES (?,?,?,?)",
                (doc_key, col, path, typ),
            )
    parts = ["id"]
    placeholders = ["1"]
    vals: list[Any] = []
    for col in sorted(col_vals.keys()):
        typ, val = col_vals[col]
        parts.append(col)
        placeholders.append("?")
        if typ == "n":
            vals.append(None)
        elif typ == "b":
            vals.append(1 if val else 0)
        elif typ == "i":
            vals.append(int(val))  # type: ignore[arg-type]
        elif typ == "r":
            vals.append(float(val))  # type: ignore[arg-type]
        elif typ == "s":
            vals.append(str(val))
        else:
            vals.append(str(val))
    conn.execute(f"DELETE FROM {tname}")
    conn.execute(
        f"INSERT INTO {tname} ({', '.join(parts)}) VALUES ({', '.join(placeholders)})",
        vals,
    )


def _save_row(conn: Any, tname: str, flat: list[tuple[str, Typ, Any]]) -> None:
    conn.execute(f"DELETE FROM {tname}")
    rows = [_row_tuple(p, t, v) for p, t, v in flat]
    conn.executemany(
        f"""INSERT INTO {tname} (path, typ, str_v, real_v, int_v, bool_v)
            VALUES (?,?,?,?,?,?)""",
        rows,
    )


def save_document(doc_key: str, obj: Any) -> None:
    """写入文档表；遇 database is locked 时短暂重试（迁移/IDE 占用库文件时常见）。"""
    init_schema()
    from src.data.ledger_doc_store import is_ledger_doc_key, upsert_json

    if is_ledger_doc_key(doc_key):
        upsert_json(doc_key, obj)
        return
    if _skip_json_doc_registry():
        upsert_json(doc_key, obj)
        return
    flat = flatten(obj)
    mode: Literal["wide", "row"] = "wide" if len(flat) <= WIDE_MAX_LEAVES else "row"
    last_lock: Exception | None = None
    for attempt in range(30):
        with _LOCK:
            conn = _connect()
            try:
                conn.execute("BEGIN")
                tname = _ensure_registry(conn, doc_key, mode)
                reg = _get_registry(conn, doc_key)
                assert reg is not None
                _, eff_mode = reg
                if eff_mode == "wide":
                    _save_wide(conn, doc_key, tname, flat)
                else:
                    _save_row(conn, tname, flat)
                conn.execute("COMMIT")
                return
            except Exception as e:
                conn.execute("ROLLBACK")
                if is_locked_error(e):
                    last_lock = e
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise
            finally:
                conn.close()
    if last_lock:
        raise last_lock


def _table_exists(conn: Any, name: str) -> bool:
    return table_exists(conn, name)


def _load_row(conn: Any, tname: str) -> Optional[Any]:
    cur = conn.execute(
        f"SELECT path, typ, str_v, real_v, int_v, bool_v FROM {tname} ORDER BY path"
    )
    fetched = cur.fetchall()
    if not fetched:
        return None
    out: list[tuple[str, Typ, Any]] = []
    for path, typ, sv, rv, iv, bv in fetched:
        t = str(typ)
        if t == "n":
            out.append((path, "n", None))  # type: ignore[arg-type]
        elif t == "b":
            out.append((path, "b", bool(bv)))
        elif t == "i":
            out.append((path, "i", int(iv)))
        elif t == "r":
            out.append((path, "r", float(rv)))
        elif t == "s":
            out.append((path, "s", sv if sv is not None else ""))
        else:
            out.append((path, "s", str(sv)))
    return unflatten(out)


def _load_wide(conn: Any, doc_key: str, tname: str) -> Optional[Any]:
    cur = conn.execute(
        f"SELECT col_name, json_path, typ FROM {_COLMAP} WHERE doc_key = ?",
        (doc_key,),
    )
    cmap = {str(r[0]): (str(r[1]), str(r[2])) for r in cur.fetchall()}
    if not cmap:
        return None
    cur2 = conn.execute(f"SELECT * FROM {tname} WHERE id = 1")
    desc = cur2.description
    row = cur2.fetchone()
    if not row or not desc:
        return None
    colnames = [d[0] for d in desc]
    pairs: list[tuple[str, Typ, Any]] = []
    for i, cname in enumerate(colnames):
        if cname == "id":
            continue
        if cname not in cmap:
            continue
        path, decl_typ = cmap[cname]
        val = row[i]
        t: Typ = decl_typ if decl_typ in ("n", "b", "i", "r", "s") else "s"  # type: ignore[assignment]
        if val is None:
            pairs.append((path, "n", None))
            continue
        if t == "b":
            pairs.append((path, "b", bool(val)))
        elif t == "i":
            pairs.append((path, "i", int(val)))
        elif t == "r":
            pairs.append((path, "r", float(val)))
        elif t == "s":
            pairs.append((path, "s", str(val)))
        else:
            pairs.append((path, "n", None))
    if not pairs:
        return None
    return unflatten(pairs)


def load_document(doc_key: str) -> Optional[Any]:
    init_schema()
    from src.data.ledger_doc_store import is_ledger_doc_key, load_json as load_ledger_doc_json

    if is_ledger_doc_key(doc_key):
        led = load_ledger_doc_json(doc_key)
        if led is not None:
            return led
    if _skip_json_doc_registry():
        return load_ledger_doc_json(doc_key)
    with _LOCK:
        conn = _connect()
        try:
            reg = _get_registry(conn, doc_key)
            if not reg:
                return None
            tname, eff_mode = reg
            if not _table_exists(conn, tname):
                return None
            if eff_mode == "row":
                return _load_row(conn, tname)
            return _load_wide(conn, doc_key, tname)
        finally:
            conn.close()


def list_doc_keys_glob(glob_pat: str) -> list[str]:
    init_schema()
    from src.data.ledger_doc_store import list_ledger_doc_keys_glob

    pat = glob_pat.replace("\\", "/")
    if _skip_json_doc_registry():
        return sorted(list_ledger_doc_keys_glob(pat), reverse=True)
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                f"SELECT doc_key FROM {_REGISTRY} WHERE doc_key GLOB ? ORDER BY doc_key DESC",
                (pat,),
            )
            rel_keys = [str(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()
    led_keys = list_ledger_doc_keys_glob(pat)
    return sorted(set(rel_keys) | set(led_keys), reverse=True)


def _decode_json_file(path: Path) -> Optional[Any]:
    import json

    if not path.is_file():
        return None
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "cp936"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def list_orphan_jdt_tables() -> list[str]:
    """列出库中存在、但不在 app_json_doc_registry.data_table 中的 jdt_* 表名。

    当 SKIP_JSON_DOC_REGISTRY 或 registry 表不存在时，凡 jdt_* 均视为遗留表（可 DROP）。"""
    init_schema()
    with _LOCK:
        conn = _connect()
        try:
            names = list_main_tables_prefixed(conn, "jdt_")
            if _skip_json_doc_registry() or not table_exists(conn, _REGISTRY):
                return names
            cur2 = conn.execute(f"SELECT data_table FROM {_REGISTRY}")
            reg = {str(r[0]) for r in cur2.fetchall()}
            return [n for n in names if n not in reg]
        finally:
            conn.close()


def drop_orphan_jdt_tables() -> list[str]:
    """DROP 无 registry 指向的 jdt_* 表；返回已删除的表名列表。"""
    drops = list_orphan_jdt_tables()
    if not drops:
        return []
    done: list[str] = []
    with _LOCK:
        conn = _connect()
        try:
            for n in drops:
                conn.execute(f'DROP TABLE IF EXISTS "{n}"')
                done.append(n)
        finally:
            conn.close()
    return done


def import_json_file(doc_key: str, path: Path) -> bool:
    """从磁盘 JSON 导入：按 doc_key 路由到 structured / analytics / ledger / jdt，与 json_io 语义一致。"""
    init_schema()
    obj = _decode_json_file(path)
    if obj is None:
        return False
    dk = doc_key.replace("\\", "/")

    from src.data.structured_store import (
        ingest_auction_json_doc,
        ingest_kline_json_doc,
        ingest_minute_json_doc,
        is_daily_snapshot_filename,
        save_daily_snapshot,
    )

    if dk == "concept_cache.json" and isinstance(obj, dict):
        from src.data.analytics_store import replace_concept_from_doc

        replace_concept_from_doc(obj)
        return True
    if dk == "industry_cache.json" and isinstance(obj, dict):
        from src.data.analytics_store import replace_industry_map

        replace_industry_map(obj)
        return True

    snap_name = Path(dk).name
    if is_daily_snapshot_filename(snap_name):
        save_daily_snapshot(snap_name, obj)
        return True

    if dk.startswith("kline_cache/"):
        if not isinstance(obj, dict):
            return False
        ingest_kline_json_doc(dk, obj)
        return True

    if dk.startswith("minute_cache/"):
        return ingest_minute_json_doc(dk, obj)

    if dk.startswith("auction_cache/"):
        return ingest_auction_json_doc(dk, obj)

    save_document(dk, obj)
    return True

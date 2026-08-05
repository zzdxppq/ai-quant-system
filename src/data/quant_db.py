"""quant 库统一连接：仅 DuckDB（`data/quant.duckdb`）。

进程内通过 SQLAlchemy StaticPool 持有一条写连接，原生 ``connect()`` 与 ORM 共用，
避免 Windows 上多连接打开同一文件失败。
只读脚本在服务已占用库时可用 ``connect(read_only=True)``。
"""
from __future__ import annotations

import re
import threading
from functools import wraps
from typing import Any, Callable

_CONN_LOCK = threading.RLock()
_SA_ENGINE: Any = None
_SA_CONN: Any = None
_POOLED: Any = None

# DuckDB 单连接非线程安全；所有常用调用必须串行（否则 Windows 上易 0xC0000409 崩溃）
_LOCKED_CONN_METHODS = frozenset({
    "execute",
    "executemany",
    "query",
    "fetchdf",
    "fetchone",
    "fetchall",
    "fetchmany",
    "commit",
    "rollback",
    "begin",
    "cursor",
})


class _PooledConnection:
    """包装共享 DuckDB 连接：``close()`` 为 no-op；execute 等走进程内 RLock。"""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name not in _LOCKED_CONN_METHODS:
            return attr

        @wraps(attr)
        def _locked(*args: Any, **kwargs: Any) -> Any:
            with _CONN_LOCK:
                return attr(*args, **kwargs)

        return _locked


def reset_shared_connection() -> None:
    """测试或迁移脚本用：关闭并清空进程内共享连接。"""
    global _SA_ENGINE, _SA_CONN, _POOLED
    with _CONN_LOCK:
        if _SA_CONN is not None:
            try:
                _SA_CONN.close()
            except Exception:
                pass
            _SA_CONN = None
        if _SA_ENGINE is not None:
            try:
                _SA_ENGINE.dispose()
            except Exception:
                pass
            _SA_ENGINE = None
        _POOLED = None


def get_sqlalchemy_engine() -> Any:
    """SQLAlchemy 引擎（StaticPool，单连接）。"""
    global _SA_ENGINE
    with _CONN_LOCK:
        if _SA_ENGINE is None:
            from sqlalchemy import create_engine
            from sqlalchemy.engine import URL
            from sqlalchemy.pool import StaticPool

            from src import config as _cfg

            _SA_ENGINE = create_engine(
                URL.create("duckdb", database=str(_cfg.DB_PATH.resolve())),
                poolclass=StaticPool,
                echo=False,
            )
        return _SA_ENGINE


def _native_inner() -> Any:
    """底层 DuckDBPyConnection（与 ORM 共用）。"""
    global _SA_CONN, _POOLED
    with _CONN_LOCK:
        if _POOLED is None:
            eng = get_sqlalchemy_engine()
            _SA_CONN = eng.connect()
            inner = _SA_CONN.connection
            # duckdb_engine：DBAPI 即 DuckDBPyConnection
            if hasattr(inner, "dbapi_connection"):
                inner = inner.dbapi_connection
            elif hasattr(inner, "driver_connection"):
                inner = inner.driver_connection
            _POOLED = _PooledConnection(inner)
        return _POOLED._inner


def connect(*, read_only: bool = False) -> Any:
    """返回 duckdb 连接（execute / executemany / close）。

    - 默认：进程内单例写连接，``close()`` 不真正断开。
    - ``read_only=True``：独立只读连接（服务运行时外部脚本查库用）。
    """
    from src import config as _cfg

    import duckdb

    dbp = _cfg.DB_PATH
    dbp.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        return duckdb.connect(str(dbp), read_only=True)

    with _CONN_LOCK:
        if _POOLED is None:
            _native_inner()
        return _POOLED


def is_duckdb_conn(conn: Any) -> bool:
    """恒为 True（保留接口供旧代码分支收敛时兼容）。"""
    return True


def executescript_compat(conn: Any, script: str) -> None:
    parts = re.split(r";\s*\n", script)
    for raw in parts:
        stmt = raw.strip()
        if stmt:
            conn.execute(stmt)


def table_column_names(conn: Any, table: str) -> set[str]:
    """返回表列名集合。"""
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'main'
          AND lower(table_name) = lower(?)
        """,
        (table,),
    ).fetchall()
    return {str(r[0]).lower() for r in rows}


def table_exists(conn: Any, name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM duckdb_tables()
        WHERE schema_name = 'main' AND lower(table_name) = lower(?)
        LIMIT 1
        """,
        (name,),
    ).fetchone()
    return row is not None


def list_main_tables_prefixed(conn: Any, prefix: str) -> list[str]:
    like_pat = prefix + "%"
    rows = conn.execute(
        """
        SELECT table_name FROM duckdb_tables()
        WHERE schema_name = 'main' AND table_name LIKE ?
        ORDER BY table_name
        """,
        (like_pat,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def is_duckdb_invalidated(exc: BaseException) -> bool:
    """先前 FatalException 后连接不可再用，需 reset_shared_connection。"""
    msg = str(exc).lower()
    return "database has been invalidated" in msg or "must be restarted prior" in msg


def is_duckdb_index_delete_fatal(exc: BaseException) -> bool:
    """daily_kline 批量 DELETE 触发的 DuckDB ART 索引损坏（见 duckdb#21394）。"""
    msg = str(exc)
    return "failed to delete all rows from index" in msg.lower()


def is_locked_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "database is locked" in msg or "database table is locked" in msg:
        return True
    if "could not set lock" in msg or "conflicting transaction" in msg:
        return True
    if "already open" in msg or "being used by another process" in msg:
        return True
    if "different configuration" in msg:
        return True
    return False

"""结构化表：K 线 / 分时 / 竞价、以及部分 latest_* 日快照（规范化/JSON 列）。

- 与 **ledger_doc**（大块 JSON 整段）、**analytics_store**（概念/行业/日级 ranking 等）、
  **relational jdt_***（其余拍平文档）分工；`import_json_file` / `json_io` 按 doc_key 路由写入。
- 未拆表的 latest_* 整包 JSON 写入 **analytics_store.daily_json_blob**（不再使用 daily_snapshot 表）。
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Optional

import pandas as pd

from src.config import TZ_CN, now_cn
from src.data.quant_db import connect as quant_connect
from src.data.quant_db import executescript_compat, table_column_names, table_exists

_LOCK = threading.RLock()


def _code6(s: str) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) >= 6:
        return d[-6:]
    return ""


_DDL = """
CREATE TABLE IF NOT EXISTS daily_kline (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    scale TEXT NOT NULL,
    source_datalen INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL,
    turnover REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, trade_date, scale, source_datalen)
);

CREATE TABLE IF NOT EXISTS kline_series_meta (
    stock_code TEXT NOT NULL,
    scale TEXT NOT NULL,
    source_datalen INTEGER NOT NULL,
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, scale, source_datalen)
);

CREATE TABLE IF NOT EXISTS minute_kline (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    seq INTEGER NOT NULL,
    bar_time VARCHAR,
    price REAL,
    avg_price REAL,
    vol_bar REAL,
    cum_lot REAL,
    amount REAL,
    pct REAL,
    avg_pct REAL,
    pre_close REAL,
    symbol TEXT,
    source TEXT,
    cached_at TEXT,
    PRIMARY KEY (stock_code, trade_date, seq)
);
CREATE INDEX IF NOT EXISTS ix_minute_kline_lookup
    ON minute_kline(stock_code, trade_date DESC);

CREATE TABLE IF NOT EXISTS auction_session (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    source TEXT,
    tick_count INTEGER,
    bid_open REAL,
    bid_volume REAL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, trade_date)
);
CREATE INDEX IF NOT EXISTS ix_auction_session_date ON auction_session(trade_date DESC);

CREATE TABLE IF NOT EXISTS stock_basic (
    stock_code TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stock_basic_updated ON stock_basic(updated_at DESC);
"""

_MINUTE_DDL_ONLY = """
CREATE TABLE IF NOT EXISTS minute_kline (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    seq INTEGER NOT NULL,
    bar_time VARCHAR,
    price REAL,
    avg_price REAL,
    vol_bar REAL,
    cum_lot REAL,
    amount REAL,
    pct REAL,
    avg_pct REAL,
    pre_close REAL,
    symbol TEXT,
    source TEXT,
    cached_at TEXT,
    PRIMARY KEY (stock_code, trade_date, seq)
);
CREATE INDEX IF NOT EXISTS ix_minute_kline_lookup
    ON minute_kline(stock_code, trade_date DESC);
"""


def _migrate_daily_kline_drop_lookup_index_if_needed(conn: Any) -> None:
    """移除 daily_kline 二级索引：DuckDB 在 CREATE INDEX 后批量 DELETE 可能触发 ART 损坏（#21394）。"""
    if not table_exists(conn, "daily_kline"):
        return
    try:
        conn.execute("DROP INDEX IF EXISTS ix_daily_kline_lookup")
    except Exception:
        pass


def _migrate_minute_kline_drop_bar_time_if_needed(conn: Any) -> None:
    """曾删除 bar_time 的迁移已废弃：分时横轴必须以真实时刻为准，见 _migrate_minute_kline_add_bar_time_if_needed。"""
    return


def _migrate_minute_kline_add_bar_time_if_needed(conn: Any) -> None:
    """为 minute_kline 增加 bar_time（HH:MM），避免仅用 seq 推断导致分时挤在左侧、量能错位。"""
    if not table_exists(conn, "minute_kline"):
        return
    cols = {c.lower() for c in table_column_names(conn, "minute_kline")}
    if "bar_time" in cols:
        return
    # IF NOT EXISTS：并发/重复 init 时不得抛错，否则 DuckDB 连接会进入 pending-error 状态
    conn.execute("ALTER TABLE minute_kline ADD COLUMN IF NOT EXISTS bar_time VARCHAR")


def _connect() -> Any:
    return quant_connect()


def init_structured_schema() -> None:
    with _LOCK:
        conn = _connect()
        try:
            executescript_compat(conn, _DDL)
            _migrate_daily_kline_drop_lookup_index_if_needed(conn)
            _migrate_minute_kline_drop_bar_time_if_needed(conn)
            _migrate_minute_kline_add_bar_time_if_needed(conn)
        except Exception:
            from src.data.quant_db import reset_shared_connection

            reset_shared_connection()
            raise
        finally:
            conn.close()


def _norm_bar_date(s: str) -> str:
    s = (s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _snapshot_date_from_obj(obj: Any) -> str:
    if not isinstance(obj, dict):
        return now_cn().strftime("%Y%m%d")
    for k in ("date", "trade_date", "snapshot_date", "as_of", "generated_date"):
        v = obj.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if re.fullmatch(r"\d{8}", s):
            return s
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s.replace("-", "")
    return now_cn().strftime("%Y%m%d")


_SNAPSHOT_FILENAMES = frozenset(
    {
        "latest_ranking.json",
        "latest_screener.json",
        "latest_signals.json",
        "latest_advice.json",
        "latest_sentiment.json",
        "latest_review.json",
        "latest_leader.json",
        "latest_insight.json",
        "latest_snapshot.json",
        "latest_trend.json",
        "latest_deviation.json",
        "latest_auction_scores.json",
    }
)


def is_daily_snapshot_filename(name: str) -> bool:
    return name in _SNAPSHOT_FILENAMES


def save_daily_snapshot(filename: str, obj: Any) -> None:
    """filename 如 latest_ranking.json：优先写入 analytics 结构化表；其余写入 daily_json_blob。"""
    init_structured_schema()
    from src.data import analytics_store

    kind = filename
    if not is_daily_snapshot_filename(kind):
        return
    if analytics_store.is_migrated_snapshot_filename(kind):
        analytics_store.save_from_latest_filename(kind, obj)
        return
    if not isinstance(obj, dict):
        return
    analytics_store.upsert_daily_json_snapshot(kind, obj)


def load_latest_daily_snapshot(filename: str) -> Optional[Any]:
    """按 snapshot_date 降序取该 kind 最新一条；已迁移 kind 从 analytics 表组装。"""
    init_structured_schema()
    from src.data import analytics_store

    kind = filename
    if not is_daily_snapshot_filename(kind):
        return None
    if analytics_store.is_migrated_snapshot_filename(kind):
        got = analytics_store.load_migrated_snapshot(kind)
        if got is not None:
            return got
    blob = analytics_store.load_latest_daily_json_snapshot(kind)
    if blob is not None:
        return blob
    with _LOCK:
        conn = _connect()
        try:
            if not table_exists(conn, "daily_snapshot"):
                return None
            cur = conn.execute(
                """SELECT payload_json FROM daily_snapshot
                   WHERE snapshot_kind = ? ORDER BY snapshot_date DESC LIMIT 1""",
                (kind,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return json.loads(str(row[0]))
        except (json.JSONDecodeError, TypeError):
            return None
        finally:
            conn.close()


_KLINE_UPSERT_SQL = """INSERT INTO daily_kline(
    stock_code, trade_date, scale, source_datalen,
    open, high, low, close, volume, amount, turnover, updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(stock_code, trade_date, scale, source_datalen) DO UPDATE SET
    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
    volume=excluded.volume, amount=excluded.amount, turnover=excluded.turnover,
    updated_at=excluded.updated_at"""


def _replace_kline_series_tx(
    conn: Any,
    code: str,
    scale: str,
    source_datalen: int,
    rows: list[tuple[Any, ...]],
    ts: str,
) -> None:
    new_dates = {r[1] for r in rows}
    conn.executemany(_KLINE_UPSERT_SQL, rows)
    cur = conn.execute(
        """SELECT trade_date FROM daily_kline
           WHERE stock_code=? AND scale=? AND source_datalen=?""",
        (code, str(scale), int(source_datalen)),
    )
    for (td,) in cur.fetchall():
        if td not in new_dates:
            conn.execute(
                """DELETE FROM daily_kline
                   WHERE stock_code=? AND trade_date=? AND scale=? AND source_datalen=?""",
                (code, td, str(scale), int(source_datalen)),
            )
    conn.execute(
        """INSERT INTO kline_series_meta(stock_code, scale, source_datalen, refreshed_at)
           VALUES(?,?,?,?)
           ON CONFLICT(stock_code, scale, source_datalen) DO UPDATE SET
             refreshed_at=excluded.refreshed_at""",
        (code, str(scale), int(source_datalen), ts),
    )


def replace_kline_series(
    stock_code: str,
    scale: str,
    source_datalen: int,
    df: pd.DataFrame,
    *,
    cached_at_iso: str,
) -> None:
    """整段替换该股该 scale 下该 source_datalen 序列（与 JSON 缓存文件语义一致）。"""
    init_structured_schema()
    code = _code6(str(stock_code))
    if len(code) != 6 or not code.isdigit():
        return
    if df is None or df.empty:
        return
    ts = cached_at_iso or now_cn().isoformat()
    rows: list[tuple[Any, ...]] = []
    for _, row in df.iterrows():
        td = _norm_bar_date(str(row.get("date", "") or ""))
        if not td:
            continue
        try:
            o = float(row.get("open", 0) or 0)
            h = float(row.get("high", 0) or 0)
            l = float(row.get("low", 0) or 0)
            c = float(row.get("close", 0) or 0)
            v = float(row.get("volume", 0) or 0)
        except (TypeError, ValueError):
            continue
        amt = row.get("amount")
        tov = row.get("turnover")
        try:
            amt_f = float(amt) if amt is not None else None
        except (TypeError, ValueError):
            amt_f = None
        try:
            tov_f = float(tov) if tov is not None else None
        except (TypeError, ValueError):
            tov_f = None
        rows.append((code, td, str(scale), int(source_datalen), o, h, l, c, v, amt_f, tov_f, ts))

    if not rows:
        return

    from src.data.quant_db import (
        is_duckdb_index_delete_fatal,
        is_duckdb_invalidated,
        reset_shared_connection,
    )

    last_exc: Optional[BaseException] = None
    for attempt in range(2):
        with _LOCK:
            conn = _connect()
            try:
                conn.execute("BEGIN")
                _replace_kline_series_tx(conn, code, str(scale), int(source_datalen), rows, ts)
                conn.execute("COMMIT")
                return
            except Exception as exc:
                last_exc = exc
                if not is_duckdb_invalidated(exc):
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        reset_shared_connection()
                else:
                    reset_shared_connection()
            finally:
                conn.close()
        if attempt == 0 and last_exc is not None and (
            is_duckdb_index_delete_fatal(last_exc) or is_duckdb_invalidated(last_exc)
        ):
            reset_shared_connection()
            init_structured_schema()
            continue
        break
    if last_exc is not None:
        raise last_exc


def try_read_kline_dataframe(
    stock_code: str,
    scale: str,
    source_datalen: int,
    *,
    allow_stale: bool,
    ttl_seconds: int,
) -> Optional[pd.DataFrame]:
    from src.config import TZ_CN
    from datetime import datetime

    init_structured_schema()
    code = _code6(str(stock_code))
    if len(code) != 6 or not code.isdigit():
        return None

    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT refreshed_at FROM kline_series_meta WHERE stock_code=? AND scale=? AND source_datalen=?",
                (code, str(scale), int(source_datalen)),
            )
            m = cur.fetchone()
            if not m or not m[0]:
                return None
            refreshed = str(m[0])
            try:
                rt = datetime.fromisoformat(refreshed)
                if rt.tzinfo is None:
                    rt = rt.replace(tzinfo=TZ_CN)
                age = (now_cn() - rt).total_seconds()
                if age > ttl_seconds and not allow_stale:
                    return None
            except ValueError:
                if not allow_stale:
                    return None

            cur2 = conn.execute(
                """SELECT trade_date, open, high, low, close, volume, amount, turnover
                   FROM daily_kline
                   WHERE stock_code=? AND scale=? AND source_datalen=?
                   ORDER BY trade_date DESC LIMIT ?""",
                (code, str(scale), int(source_datalen), int(source_datalen)),
            )
            fetched = cur2.fetchall()
        finally:
            conn.close()

    if not fetched:
        return None
    fetched = list(reversed(fetched))
    df = pd.DataFrame(
        {
            "date": [r[0] for r in fetched],
            "open": [float(r[1]) for r in fetched],
            "high": [float(r[2]) for r in fetched],
            "low": [float(r[3]) for r in fetched],
            "close": [float(r[4]) for r in fetched],
            "volume": [float(r[5]) for r in fetched],
        }
    )
    return df


def replace_minute_series(payload: dict[str, Any]) -> None:
    init_structured_schema()
    code = str(payload.get("code") or "").strip()
    if len(code) != 6 or not code.isdigit():
        return
    td = str(payload.get("trade_date") or "").strip() or now_cn().strftime("%Y%m%d")
    bars = payload.get("bars")
    if not isinstance(bars, list) or not bars:
        return
    sym = str(payload.get("symbol") or "")
    src = str(payload.get("source") or "")
    cached = str(payload.get("cached_at") or now_cn().isoformat())
    try:
        pre = float(payload.get("pre_close") or 0)
    except (TypeError, ValueError):
        pre = 0.0

    rows: list[tuple[Any, ...]] = []
    for i, b in enumerate(bars):
        if not isinstance(b, dict):
            continue
        try:
            bt = str(b.get("t") or "").strip()
            if len(bt) > 12:
                bt = bt[:12]
            rows.append(
                (
                    code,
                    td,
                    i,
                    bt or None,
                    float(b.get("p") or 0),
                    float(b.get("avg") or 0),
                    float(b.get("vol_bar") or 0),
                    float(b.get("cum_lot") or 0),
                    float(b.get("amount") or 0),
                    float(b.get("pct") or 0),
                    float(b.get("avg_pct") or 0),
                    pre,
                    sym,
                    src,
                    cached,
                )
            )
        except (TypeError, ValueError):
            continue

    if not rows:
        return

    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM minute_kline WHERE stock_code=? AND trade_date=?", (code, td))
            conn.executemany(
                """INSERT INTO minute_kline(
                    stock_code, trade_date, seq, bar_time, price, avg_price, vol_bar, cum_lot,
                    amount, pct, avg_pct, pre_close, symbol, source, cached_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def load_minute_payload(code6: str, trade_date: str | None = None) -> Optional[dict[str, Any]]:
    """读取分时；trade_date 为 YYYYMMDD，None 时取该股库中最近交易日。"""
    init_structured_schema()
    code = _code6(str(code6))
    if len(code) != 6 or not code.isdigit():
        return None
    with _LOCK:
        conn = _connect()
        try:
            if trade_date:
                td = re.sub(r"\D", "", str(trade_date))
                if len(td) != 8:
                    return None
            else:
                cur = conn.execute(
                    """SELECT trade_date FROM minute_kline WHERE stock_code=?
                       ORDER BY trade_date DESC LIMIT 1""",
                    (code,),
                )
                r0 = cur.fetchone()
                if not r0:
                    return None
                td = str(r0[0])
            cur2 = conn.execute(
                """SELECT seq, bar_time, price, avg_price, vol_bar, cum_lot, amount, pct, avg_pct,
                          pre_close, symbol, source, cached_at
                   FROM minute_kline WHERE stock_code=? AND trade_date=?
                   ORDER BY seq ASC""",
                (code, td),
            )
            fetched = cur2.fetchall()
        finally:
            conn.close()

    if not fetched:
        return None
    row0 = fetched[0]
    pre_close = float(row0[9] or 0) if row0[9] is not None else None
    symbol = str(row0[10] or "")
    source = str(row0[11] or "")
    cached_at = str(row0[12] or "")

    def _seq_to_hhmm(seq: int) -> str:
        """SQLite 仅存 seq；按连续交易分钟还原 HH:MM（上午 121 根含 11:30，下午自 13:00）。"""
        t930 = 9 * 60 + 30
        t1300 = 13 * 60
        t1130 = 11 * 60 + 30
        morning_slots = (t1130 - t930) + 1  # 9:30..11:30 共 121 根
        s = int(seq)
        if s < morning_slots:
            m = t930 + s
        else:
            m = t1300 + (s - morning_slots)
        h, mi = m // 60, m % 60
        return f"{h:d}:{mi:02d}"

    bars: list[dict[str, Any]] = []
    for t in fetched:
        seq = int(t[0] or 0)
        bar_time = str(t[1] or "").strip()
        t_label = bar_time if bar_time else _seq_to_hhmm(seq)
        bars.append(
            {
                "t": t_label,
                "p": round(float(t[2] or 0), 4),
                "avg": round(float(t[3] or 0), 4),
                "vol_bar": round(float(t[4] or 0), 2),
                "cum_lot": round(float(t[5] or 0), 2),
                "amount": round(float(t[6] or 0), 2),
                "pct": round(float(t[7] or 0), 3),
                "avg_pct": round(float(t[8] or 0), 3),
            }
        )
    return {
        "code": code,
        "symbol": symbol,
        "trade_date": td,
        "pre_close": round(pre_close, 4) if pre_close and pre_close > 0 else None,
        "cached_at": cached_at,
        "source": source or "tencent_minute",
        "bars": bars,
    }


def load_latest_minute_payload(code6: str) -> Optional[dict[str, Any]]:
    return load_minute_payload(code6, None)


def list_minute_trade_dates(code6: str, *, limit: int = 30) -> list[str]:
    code = _code6(str(code6))
    if len(code) != 6:
        return []
    init_structured_schema()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """SELECT DISTINCT trade_date FROM minute_kline WHERE stock_code=?
                   ORDER BY trade_date DESC LIMIT ?""",
                (code, int(limit)),
            )
            return [str(x[0]) for x in cur.fetchall()]
        finally:
            conn.close()


def save_auction_session(stock_code: str, trade_date: str, payload: dict[str, Any]) -> None:
    init_structured_schema()
    c = _code6(str(stock_code))
    if len(c) != 6 or not c.isdigit():
        return
    d = re.sub(r"\D", "", str(trade_date or ""))
    if len(d) == 8:
        pass
    elif len(d) == 6:
        d = now_cn().strftime("%Y%m%d")
    else:
        d = str(payload.get("date") or "").strip()
        d = re.sub(r"\D", "", d)
    if len(d) != 8:
        return
    body = json.dumps(payload, ensure_ascii=False)
    src = str(payload.get("source") or "")
    try:
        tc = int(payload.get("tick_count") or 0)
    except (TypeError, ValueError):
        tc = 0
    bid_o: Optional[float] = None
    bid_v: Optional[float] = None
    ot = payload.get("open_tick")
    if isinstance(ot, dict):
        try:
            bid_o = float(ot.get("price") or 0) or None
        except (TypeError, ValueError):
            bid_o = None
        try:
            bid_v = float(ot.get("matched_vol") or 0) or None
        except (TypeError, ValueError):
            bid_v = None
    ts = now_cn().isoformat()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO auction_session(
                    stock_code, trade_date, source, tick_count, bid_open, bid_volume, payload_json, updated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                  source=excluded.source,
                  tick_count=excluded.tick_count,
                  bid_open=excluded.bid_open,
                  bid_volume=excluded.bid_volume,
                  payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at""",
                (c, d, src, tc, bid_o, bid_v, body, ts),
            )
        finally:
            conn.close()


def load_auction_session(stock_code: str, trade_date: str) -> Optional[dict[str, Any]]:
    init_structured_schema()
    c = _code6(str(stock_code))
    d = re.sub(r"\D", "", str(trade_date or ""))
    if len(c) != 6 or len(d) != 8:
        return None
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT payload_json FROM auction_session WHERE stock_code=? AND trade_date=?",
                (c, d),
            )
            row = cur.fetchone()
            if not row:
                return None
            obj = json.loads(str(row[0]))
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
        finally:
            conn.close()


def ingest_kline_json_doc(doc_key: str, obj: Any) -> int:
    """从 kline_cache/*.json 对象写入 daily_kline；返回插入行数。"""
    if not isinstance(obj, dict):
        return 0
    code = str(obj.get("code") or "")
    scale = str(obj.get("scale") or "")
    try:
        dl = int(obj.get("datalen") or 0)
    except (TypeError, ValueError):
        dl = 0
    bars = obj.get("bars")
    if not isinstance(bars, list) or dl <= 0:
        return 0
    cached = str(obj.get("cached_at") or now_cn().isoformat())
    try:
        df = pd.DataFrame(bars)
        for col in ("open", "close", "high", "low", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        if "date" not in df.columns:
            return 0
    except Exception:
        return 0
    replace_kline_series(code, scale, dl, df, cached_at_iso=cached)
    return len(df)


def replace_stock_basic(df: pd.DataFrame) -> int:
    """全量覆盖 A 股代码名称表（持仓/全市场搜索用，与 _stock_list_cache 同源字段）。"""
    init_structured_schema()
    if df is None or df.empty:
        return 0
    if "code" not in df.columns or "name" not in df.columns:
        return 0
    iso = now_cn().replace(microsecond=0).isoformat()
    seen: set[str] = set()
    rows: list[tuple[str, str, str]] = []
    for _, r in df.iterrows():
        c = re.sub(r"\D", "", str(r.get("code") or ""))[-6:].zfill(6)
        nm = str(r.get("name") or "").strip()
        if len(c) != 6 or not c.isdigit() or not nm or c in seen:
            continue
        seen.add(c)
        rows.append((c, nm, iso))
    if not rows:
        return 0
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM stock_basic")
            conn.executemany(
                "INSERT INTO stock_basic(stock_code, name, updated_at) VALUES (?, ?, ?)",
                rows,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    return len(rows)


def stock_basic_stats() -> dict[str, Any]:
    """{count, updated_at} 供看板展示 / 刷新前检查。"""
    init_structured_schema()
    with _LOCK:
        conn = _connect()
        try:
            if not table_exists(conn, "stock_basic"):
                return {"count": 0, "updated_at": None}
            r = conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM stock_basic"
            ).fetchone()
            return {
                "count": int(r[0] or 0) if r else 0,
                "updated_at": str(r[1]) if r and r[1] else None,
            }
        finally:
            conn.close()


def load_stock_basic_df_if_fresh(
    *, min_rows: int = 3000, max_age_sec: float = 86400.0
) -> Optional[pd.DataFrame]:
    """记录数 ≥ min_rows 且更新未过期则返回 code/name DataFrame。"""
    from datetime import datetime

    init_structured_schema()
    cur: list[Any] | None = None
    with _LOCK:
        conn = _connect()
        try:
            if not table_exists(conn, "stock_basic"):
                return None
            r0 = conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM stock_basic"
            ).fetchone()
            if not r0 or int(r0[0] or 0) < min_rows or not r0[1]:
                return None
            mx = str(r0[1])
            try:
                u0 = datetime.fromisoformat(mx.replace("Z", "+00:00"))
                if u0.tzinfo is None:
                    u0 = u0.replace(tzinfo=TZ_CN)
                age = (now_cn() - u0).total_seconds()
            except Exception:
                return None
            if age > max_age_sec:
                return None
            cur = conn.execute(
                "SELECT stock_code, name FROM stock_basic ORDER BY stock_code"
            ).fetchall()
        finally:
            conn.close()
    if not cur:
        return None
    return pd.DataFrame(cur, columns=["code", "name"])


def load_stock_basic_full_df(*, min_rows: int = 2500) -> Optional[pd.DataFrame]:
    """只要行数 ≥ min_rows 即返回全表（不做时间新鲜度过滤，供搜索优先走库）。"""
    init_structured_schema()
    cur: list[Any] | None = None
    with _LOCK:
        conn = _connect()
        try:
            if not table_exists(conn, "stock_basic"):
                return None
            r0 = conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()
            if not r0 or int(r0[0] or 0) < min_rows:
                return None
            cur = conn.execute(
                "SELECT stock_code, name FROM stock_basic ORDER BY stock_code"
            ).fetchall()
        finally:
            conn.close()
    if not cur:
        return None
    return pd.DataFrame(cur, columns=["code", "name"])


def ingest_minute_json_doc(_doc_key: str, obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    replace_minute_series(obj)
    return True


def ingest_auction_json_doc(doc_key: str, obj: Any) -> bool:
    """doc_key 形如 auction_cache/20260509/600130.json"""
    if not isinstance(obj, dict):
        return False
    parts = doc_key.replace("\\", "/").split("/")
    if len(parts) < 3:
        return False
    date_part, code_file = parts[-2], parts[-1]
    code = code_file.replace(".json", "")
    save_auction_session(code, date_part, obj)
    return True

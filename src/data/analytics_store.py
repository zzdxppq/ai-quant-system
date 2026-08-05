"""日级分析结构化表 + 概念/行业关系表（供 SQL 回测）；与旧 daily_snapshot 大 JSON 解耦。

- latest_advice / ranking / sentiment / auction_scores / screener → 规范化行表
- concept_cache / industry_cache → concept_info + concept_members、industry_member
- 其余 latest_* 仍可用 daily_json_blob(kind, date, json) 单表承载（避免再建 jdt_*）
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from typing import Any, Optional

from src.config import now_cn
from src.data.quant_db import connect as quant_connect
from src.data.quant_db import executescript_compat, table_column_names

_LOCK = threading.RLock()

_DDL = """
CREATE TABLE IF NOT EXISTS daily_advice (
    advice_date TEXT PRIMARY KEY,
    advice_text TEXT,
    bucket TEXT,
    reason TEXT,
    suggested_position TEXT,
    suggested_position_short TEXT,
    conclusion TEXT,
    bad_count INTEGER,
    weighted_auction_gain REAL,
    limit_down INTEGER,
    dimensions_json TEXT,
    inputs_json TEXT,
    dashboard_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_ranking (
    rank_date TEXT NOT NULL,
    rank_pos INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    gain_10d REAL,
    market_cap REAL,
    industry TEXT,
    concepts_json TEXT,
    extra_json TEXT,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (rank_date, stock_code)
);
CREATE INDEX IF NOT EXISTS ix_daily_ranking_date_pos ON daily_ranking(rank_date, rank_pos);

CREATE TABLE IF NOT EXISTS daily_sentiment (
    sent_date TEXT PRIMARY KEY,
    pool_size INTEGER,
    avg_auction_gain REAL,
    weighted_bid_avg REAL,
    high_open_count INTEGER,
    flat_open_count INTEGER,
    low_open_count INTEGER,
    limit_down_count INTEGER,
    limit_up_flat INTEGER,
    verdict TEXT,
    reason TEXT,
    total_stocks INTEGER,
    market_json TEXT,
    relay_json TEXT,
    prev_day_weighted REAL,
    raw_extras_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_auction_scores (
    score_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    action TEXT,
    position TEXT,
    reason TEXT,
    score REAL,
    stop_loss REAL,
    stop_loss_pct REAL,
    d1_self REAL,
    d2_leader REAL,
    d3_ranking REAL,
    d4_sector REAL,
    has_veto INTEGER,
    vetoes_json TEXT,
    details_json TEXT,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (score_date, stock_code)
);
CREATE INDEX IF NOT EXISTS ix_daily_auction_scores_date_score ON daily_auction_scores(score_date, score DESC);

CREATE TABLE IF NOT EXISTS daily_screener_hit (
    hit_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    continuous_limit_up INTEGER,
    auction_gain REAL,
    auction_turnover REAL,
    market_cap REAL,
    volume_ratio REAL,
    industry TEXT,
    concepts_json TEXT,
    extra_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (hit_date, stock_code)
);

CREATE TABLE IF NOT EXISTS concept_info (
    concept_code TEXT PRIMARY KEY,
    concept_name TEXT NOT NULL,
    source TEXT DEFAULT 'eastmoney',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_members (
    concept_code TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    PRIMARY KEY (concept_code, stock_code, trade_date)
);
CREATE INDEX IF NOT EXISTS ix_concept_members_stock_date ON concept_members(stock_code, trade_date);
CREATE INDEX IF NOT EXISTS ix_concept_members_code_date ON concept_members(concept_code, trade_date);

CREATE TABLE IF NOT EXISTS industry_member (
    stock_code TEXT NOT NULL,
    industry_name TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, as_of_date)
);
CREATE INDEX IF NOT EXISTS ix_industry_member_name ON industry_member(industry_name, as_of_date);

CREATE TABLE IF NOT EXISTS daily_json_blob (
    blob_kind TEXT NOT NULL,
    blob_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (blob_kind, blob_date)
);
CREATE INDEX IF NOT EXISTS ix_daily_json_blob_kind_date ON daily_json_blob(blob_kind, blob_date DESC);

CREATE TABLE IF NOT EXISTS screener_history_entry (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    continuous_limit_up INTEGER,
    board_label TEXT,
    open_price REAL,
    pre_close REAL,
    auction_gain REAL,
    auction_turnover REAL,
    auction_volume_ratio REAL,
    market_cap REAL,
    gain_10d REAL,
    industry TEXT,
    market_highest_board INTEGER,
    limit_up_time TEXT,
    close_price REAL,
    close_gain REAL,
    day_change REAL,
    next_day_open REAL,
    next_day_auction_gain REAL,
    next_day_close_gain REAL,
    is_win INTEGER,
    is_limit_up INTEGER,
    is_zhaban INTEGER,
    sanbanzhu INTEGER,
    sanbanzhu_detail TEXT,
    matched_cycle INTEGER,
    top30_streak INTEGER,
    status TEXT,
    market_limit_down INTEGER,
    weighted_auction_gain REAL,
    yesterday_lianban_today_avg REAL,
    b1_rate REAL,
    concentration REAL,
    decision_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code)
);
CREATE INDEX IF NOT EXISTS ix_screener_hist_trade_date ON screener_history_entry(trade_date DESC);
CREATE INDEX IF NOT EXISTS ix_daily_advice_advice_date ON daily_advice(advice_date DESC);
"""


def _connect() -> Any:
    return quant_connect()


def _migrate_daily_sentiment_raw_extras_json(conn: Any) -> None:
    """旧库 raw_extras_json 曾被误建成 INTEGER，导致 dashboard_v2 无法写入。"""
    row = conn.execute(
        """
        SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'main' AND lower(table_name) = 'daily_sentiment'
          AND lower(column_name) = 'raw_extras_json'
        """
    ).fetchone()
    if not row:
        return
    dt = str(row[0] or "").upper()
    if dt in ("VARCHAR", "TEXT"):
        return
    conn.execute("ALTER TABLE daily_sentiment ADD COLUMN raw_extras_json_text VARCHAR")
    conn.execute("ALTER TABLE daily_sentiment DROP COLUMN raw_extras_json")
    conn.execute(
        "ALTER TABLE daily_sentiment RENAME COLUMN raw_extras_json_text TO raw_extras_json"
    )


def init_analytics_schema() -> None:
    with _LOCK:
        conn = _connect()
        try:
            executescript_compat(conn, _DDL)
            _migrate_daily_sentiment_raw_extras_json(conn)
            for tbl, col, decl in (
                ("daily_ranking", "updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("daily_auction_scores", "updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("daily_screener_hit", "matched_cycle", "INTEGER"),
                ("daily_screener_hit", "created_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                names = table_column_names(conn, tbl)
                if names and col not in names:
                    decl_d = decl.replace(" NOT NULL", "").split(" DEFAULT ")[0].strip()
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl_d}")
            conn.execute("DROP TABLE IF EXISTS screener_result")
        finally:
            conn.close()


def _to_ymd(date_str: str | None) -> str:
    s = (date_str or "").strip()
    if re.match(r"^\d{8}$", s):
        return s
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    try:
        return now_cn().strftime("%Y%m%d")
    except Exception:
        return datetime.utcnow().strftime("%Y%m%d")


# --- 写入：按 latest_* 文件名分发 ---

def save_from_latest_filename(filename: str, obj: Any) -> None:
    init_analytics_schema()
    if not isinstance(obj, dict) and not isinstance(obj, list):
        return
    if filename == "latest_advice.json" and isinstance(obj, dict):
        _save_advice(obj)
        return
    if filename == "latest_ranking.json" and isinstance(obj, dict):
        _save_ranking(obj)
        return
    if filename == "latest_sentiment.json" and isinstance(obj, dict):
        _save_sentiment(obj)
        return
    if filename == "latest_auction_scores.json" and isinstance(obj, list):
        _save_auction_scores(obj)
        return
    if filename == "latest_screener.json" and isinstance(obj, dict):
        _save_screener(obj)
        _save_json_blob("latest_screener", obj)
        return
    if filename == "latest_review.json" and isinstance(obj, dict):
        _save_json_blob("latest_review", obj)
        return
    if filename in (
        "latest_leader.json",
        "latest_insight.json",
        "latest_snapshot.json",
        "latest_signals.json",
        "latest_deviation.json",
        "latest_trend.json",
    ) and isinstance(obj, dict):
        _save_json_blob(filename.replace(".json", ""), obj)
        return


def _count_daily_screener_hit(hit_date: str) -> int:
    ymd = _to_ymd(hit_date)
    if len(ymd) != 8:
        return 0
    with _LOCK:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT COUNT(*) FROM daily_screener_hit WHERE hit_date = ?",
                (ymd,),
            ).fetchone()
            return int(r[0] or 0)
        finally:
            conn.close()


def _save_advice(obj: dict[str, Any]) -> None:
    g = str(obj.get("generated_at") or "")[:10]
    d = _to_ymd(g or str(obj.get("date") or ""))
    inputs = obj.get("inputs") or {}
    w = inputs.get("weighted_auction_gain")
    ld = inputs.get("limit_down")
    ts = now_cn().isoformat()
    row = (
        d,
        str(obj.get("text") or ""),
        str(obj.get("bucket") or ""),
        str(obj.get("reason") or ""),
        str(obj.get("suggested_position") or obj.get("position") or ""),
        str(obj.get("suggested_position_short") or obj.get("position_short") or ""),
        str(obj.get("conclusion") or ""),
        _i(obj.get("bad_count")),
        float(w) if w is not None else None,
        _i(ld),
        json.dumps(obj.get("dimensions") or {}, ensure_ascii=False),
        json.dumps(inputs, ensure_ascii=False),
        json.dumps(obj.get("dashboard") or {}, ensure_ascii=False),
        ts,
    )
    with _LOCK:
        conn = _connect()
        try:
            # 历史库表可能缺 PRIMARY KEY，DuckDB ON CONFLICT 会失败 → 先删后插
            conn.execute("DELETE FROM daily_advice WHERE advice_date = ?", (d,))
            conn.execute(
                """INSERT INTO daily_advice(
                    advice_date, advice_text, bucket, reason,
                    suggested_position, suggested_position_short, conclusion,
                    bad_count, weighted_auction_gain, limit_down,
                    dimensions_json, inputs_json, dashboard_json, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
        finally:
            conn.close()


def _save_ranking(obj: dict[str, Any]) -> None:
    d = _to_ymd(str(obj.get("date") or obj.get("updated_at") or ""))
    rows = obj.get("ranking") or []
    ts = now_cn().isoformat()
    batch: list[tuple[Any, ...]] = []
    for i, rec in enumerate(rows):
        if not isinstance(rec, dict):
            continue
        code = str(rec.get("code") or "").strip()[-6:].zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        name = str(rec.get("name") or "")
        g = rec.get("gain_10d")
        mc = rec.get("market_cap")
        try:
            gf = float(g) if g is not None else None
        except (TypeError, ValueError):
            gf = None
        try:
            mcf = float(mc) if mc is not None else None
        except (TypeError, ValueError):
            mcf = None
        ind = str(rec.get("industry") or "")
        concepts = rec.get("concepts")
        cj = json.dumps(concepts, ensure_ascii=False) if concepts is not None else None
        extra = {k: v for k, v in rec.items() if k not in ("code", "name", "gain_10d", "market_cap", "industry", "concepts")}
        batch.append(
            (d, i + 1, code, name, gf, mcf, ind, cj, json.dumps(extra, ensure_ascii=False) if extra else None, ts)
        )
    if not batch:
        return
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM daily_ranking WHERE rank_date = ?", (d,))
            conn.executemany(
                """INSERT INTO daily_ranking(
                    rank_date, rank_pos, stock_code, stock_name, gain_10d, market_cap,
                    industry, concepts_json, extra_json, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                batch,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _save_sentiment(obj: dict[str, Any]) -> None:
    d = _to_ymd(str(obj.get("date") or ""))
    ts = now_cn().isoformat()
    mkt = obj.get("market")
    relay = obj.get("relay_sentiment_index")
    try:
        pool_size = int(obj.get("pool_size") or 0)
    except (TypeError, ValueError):
        pool_size = 0
    extras = {
        k: v
        for k, v in obj.items()
        if k
        not in (
            "date",
            "pool_size",
            "avg_auction_gain",
            "weighted_auction_gain",
            "limit_up_flat",
            "high_open",
            "flat_open",
            "low_open",
            "limit_down",
            "verdict",
            "reason",
            "market",
            "relay_sentiment_index",
            "prev_day_weighted_auction_gain",
        )
    }
    try:
        tot = int((mkt or {}).get("total") or 0)
    except (TypeError, ValueError):
        tot = None
    row = (
        d,
        pool_size,
        _f(obj.get("avg_auction_gain")),
        _f(obj.get("weighted_auction_gain")),
        _i(obj.get("high_open")),
        _i(obj.get("flat_open")),
        _i(obj.get("low_open")),
        _i(obj.get("limit_down")),
        _i(obj.get("limit_up_flat")),
        str(obj.get("verdict") or ""),
        str(obj.get("reason") or ""),
        tot,
        json.dumps(mkt, ensure_ascii=False) if mkt else None,
        json.dumps(relay, ensure_ascii=False) if relay else None,
        _f(obj.get("prev_day_weighted_auction_gain")),
        json.dumps(extras, ensure_ascii=False) if extras else None,
        ts,
    )
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM daily_sentiment WHERE sent_date = ?", (d,))
            conn.execute(
                """INSERT INTO daily_sentiment(
                    sent_date, pool_size, avg_auction_gain, weighted_bid_avg,
                    high_open_count, flat_open_count, low_open_count, limit_down_count,
                    limit_up_flat, verdict, reason, total_stocks, market_json, relay_json,
                    prev_day_weighted, raw_extras_json, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
        finally:
            conn.close()


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _bool_flag(v: Any) -> Optional[bool]:
    """INTEGER 0/1 → bool，供前端 `is_win === true/false` 判定。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return None


def _save_auction_scores(rows: list[Any]) -> None:
    if not rows:
        return
    d = None
    for rec in rows:
        if isinstance(rec, dict) and rec.get("date"):
            d = _to_ymd(str(rec.get("date")))
            break
    if d is None:
        d = _to_ymd(now_cn().strftime("%Y-%m-%d"))
    ts = now_cn().isoformat()
    batch: list[tuple[Any, ...]] = []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        code = str(rec.get("code") or "").strip()[-6:].zfill(6)
        if len(code) != 6:
            continue
        details = {k: rec.get(k) for k in ("d1_detail", "d2_detail", "d3_detail", "d4_detail")}
        batch.append(
            (
                d,
                code,
                str(rec.get("name") or ""),
                str(rec.get("action") or ""),
                str(rec.get("position") or ""),
                str(rec.get("reason") or ""),
                _f(rec.get("total_score")),
                _f(rec.get("stop_loss")),
                _f(rec.get("stop_loss_pct")),
                _f(rec.get("d1_self")),
                _f(rec.get("d2_leader")),
                _f(rec.get("d3_ranking")),
                _f(rec.get("d4_sector")),
                1 if rec.get("has_veto") else 0,
                json.dumps(rec.get("vetoes") or [], ensure_ascii=False),
                json.dumps({k: v for k, v in details.items() if v}, ensure_ascii=False),
                ts,
            )
        )
    if not batch:
        return
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM daily_auction_scores WHERE score_date = ?", (d,))
            conn.executemany(
                """INSERT INTO daily_auction_scores(
                    score_date, stock_code, stock_name, action, position, reason, score,
                    stop_loss, stop_loss_pct, d1_self, d2_leader, d3_ranking, d4_sector,
                    has_veto, vetoes_json, details_json, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                batch,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _save_screener(obj: dict[str, Any]) -> None:
    d = _to_ymd(str(obj.get("date") or obj.get("as_of") or ""))
    hits = obj.get("hits") or []
    ts = now_cn().isoformat()
    batch: list[tuple[Any, ...]] = []
    for rec in hits:
        if not isinstance(rec, dict):
            continue
        code = str(rec.get("code") or "").strip()[-6:].zfill(6)
        if len(code) != 6:
            continue
        concepts = rec.get("top_concepts") or rec.get("concepts")
        cj = json.dumps(concepts, ensure_ascii=False) if concepts is not None else None
        extra = {k: v for k, v in rec.items() if k not in ("code", "name", "continuous_limit_up", "auction_gain", "auction_turnover", "market_cap", "volume_ratio", "industry", "top_concepts", "concepts")}
        mc_flag = 1 if (rec.get("matched_cycle") or rec.get("is_cycle_stock")) else 0
        cr_at = str(rec.get("created_at") or ts)
        batch.append(
            (
                d,
                code,
                str(rec.get("name") or ""),
                _i(rec.get("continuous_limit_up")),
                _f(rec.get("auction_gain")),
                _f(rec.get("auction_turnover")),
                _f(rec.get("market_cap")),
                _f(rec.get("volume_ratio")),
                str(rec.get("industry") or ""),
                cj,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                int(mc_flag),
                cr_at,
                ts,
            )
        )
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM daily_screener_hit WHERE hit_date = ?", (d,))
            if not batch:
                conn.execute("COMMIT")
                return
            if batch:
                conn.executemany(
                    """INSERT INTO daily_screener_hit(
                        hit_date, stock_code, stock_name, continuous_limit_up,
                        auction_gain, auction_turnover, market_cap, volume_ratio,
                        industry, concepts_json, extra_json, matched_cycle, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    batch,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _save_json_blob(kind: str, obj: dict[str, Any]) -> None:
    d = _to_ymd(str(obj.get("date") or obj.get("generated_at") or obj.get("as_of") or ""))
    ts = now_cn().isoformat()
    body = json.dumps(obj, ensure_ascii=False)
    with _LOCK:
        conn = _connect()
        try:
            # 用 UPSERT 替代 DELETE+INSERT，避免索引损坏时 DELETE 触发 fatal invalidate
            conn.execute("BEGIN")
            conn.execute(
                """INSERT INTO daily_json_blob(blob_kind, blob_date, payload_json, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT (blob_kind, blob_date) DO UPDATE SET
                     payload_json = excluded.payload_json,
                     updated_at = excluded.updated_at""",
                (kind, d, body, ts),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def upsert_daily_json_snapshot(filename: str, obj: dict[str, Any]) -> None:
    """未拆表的 latest_* 整包 JSON → daily_json_blob（替代旧 daily_snapshot 表）。"""
    if not isinstance(obj, dict):
        return
    fn = str(filename)
    k = fn[:-5] if fn.endswith(".json") else fn
    _save_json_blob(k, obj)


def load_latest_daily_json_snapshot(filename: str) -> Optional[dict[str, Any]]:
    fn = str(filename)
    k = fn[:-5] if fn.endswith(".json") else fn
    return _load_json_blob_latest(k)


def _pack_history_decision_json(r: dict[str, Any]) -> str | None:
    """decision_json 列：兼容旧版纯 decision；新版可附带 next_day_sell_advice。"""
    decision = r.get("decision")
    sell = r.get("next_day_sell_advice")
    if sell is not None:
        return json.dumps(
            {"decision": decision, "next_day_sell_advice": sell},
            ensure_ascii=False,
        )
    if decision is not None:
        return json.dumps(decision, ensure_ascii=False)
    return None


def _unpack_history_decision_json(raw: str | None) -> tuple[Any, Any]:
    if not raw:
        return None, None
    try:
        obj = json.loads(str(raw))
    except json.JSONDecodeError:
        return None, None
    if isinstance(obj, dict) and "next_day_sell_advice" in obj:
        return obj.get("decision"), obj.get("next_day_sell_advice")
    return obj, None


def _history_rec_to_tuple(r: dict[str, Any], trade_date: str, stock_code: str, ts: str) -> tuple[Any, ...]:
    decision_json = _pack_history_decision_json(r)
    matched = 1 if (r.get("matched_cycle") or r.get("is_cycle_stock")) else 0
    cr_at = str(r.get("created_at") or ts)
    return (
        trade_date,
        stock_code,
        str(r.get("name") or ""),
        _i(r.get("continuous_limit_up")),
        str(r.get("board_label") or "") or None,
        _f(r.get("open_price")),
        _f(r.get("pre_close")),
        _f(r.get("auction_gain")),
        _f(r.get("auction_turnover")),
        _f(r.get("auction_volume_ratio")),
        _f(r.get("market_cap")),
        _f(r.get("gain_10d")),
        str(r.get("industry") or "") or None,
        _i(r.get("market_highest_board")),
        str(r.get("limit_up_time") or "") or None,
        _f(r.get("close_price")),
        _f(r.get("close_gain")),
        _f(r.get("day_change")),
        _f(r.get("next_day_open")),
        _f(r.get("next_day_auction_gain")),
        _f(r.get("next_day_close_gain")),
        _i(r.get("is_win")),
        _i(r.get("is_limit_up")),
        _i(r.get("is_zhaban")),
        1 if r.get("sanbanzhu") else 0,
        str(r.get("sanbanzhu_detail") or "") or None,
        int(matched),
        _i(r.get("top30_streak")),
        str(r.get("status") or "pending") or "pending",
        _i(r.get("market_limit_down")),
        _f(r.get("weighted_auction_gain")),
        _f(r.get("yesterday_lianban_today_avg")),
        _f(r.get("b1_rate")),
        _f(r.get("concentration")),
        decision_json,
        cr_at,
        ts,
    )


def _tuple_to_history_rec(tup: tuple[Any, ...]) -> dict[str, Any]:
    (
        trade_date,
        stock_code,
        stock_name,
        continuous_limit_up,
        board_label,
        open_price,
        pre_close,
        auction_gain,
        auction_turnover,
        auction_volume_ratio,
        market_cap,
        gain_10d,
        industry,
        market_highest_board,
        limit_up_time,
        close_price,
        close_gain,
        day_change,
        next_day_open,
        next_day_auction_gain,
        next_day_close_gain,
        is_win,
        is_limit_up,
        is_zhaban,
        sanbanzhu,
        sanbanzhu_detail,
        matched_cycle,
        top30_streak,
        status,
        market_limit_down,
        weighted_auction_gain,
        yesterday_lianban_today_avg,
        b1_rate,
        concentration,
        decision_json,
        created_at,
        _updated_at,
    ) = tup
    td = str(trade_date or "")
    if len(td) == 8 and td.isdigit():
        disp = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
    else:
        disp = td[:10] if td else ""
    rec: dict[str, Any] = {
        "date": disp,
        "code": stock_code,
        "name": stock_name or "",
        "continuous_limit_up": continuous_limit_up,
        "board_label": board_label,
        "open_price": open_price,
        "pre_close": pre_close,
        "auction_gain": auction_gain,
        "auction_turnover": auction_turnover,
        "auction_volume_ratio": auction_volume_ratio,
        "market_cap": market_cap,
        "gain_10d": gain_10d,
        "industry": industry or "",
        "market_highest_board": market_highest_board,
        "limit_up_time": limit_up_time,
        "close_price": close_price,
        "close_gain": close_gain,
        "day_change": day_change,
        "next_day_open": next_day_open,
        "next_day_auction_gain": next_day_auction_gain,
        "next_day_close_gain": next_day_close_gain,
        "is_win": _bool_flag(is_win),
        "is_limit_up": _bool_flag(is_limit_up),
        "is_zhaban": _bool_flag(is_zhaban),
        "sanbanzhu": bool(sanbanzhu),
        "sanbanzhu_detail": sanbanzhu_detail or "",
        "is_cycle_stock": bool(matched_cycle),
        "matched_cycle": bool(matched_cycle),
        "top30_streak": top30_streak,
        "status": status or "pending",
        "market_limit_down": market_limit_down,
        "weighted_auction_gain": weighted_auction_gain,
        "yesterday_lianban_today_avg": yesterday_lianban_today_avg,
        "b1_rate": b1_rate,
        "concentration": concentration,
        "created_at": created_at,
    }
    if decision_json:
        dec, sell = _unpack_history_decision_json(str(decision_json))
        if dec is not None:
            rec["decision"] = dec
        if sell is not None:
            rec["next_day_sell_advice"] = sell
    return rec


_SCREENER_HISTORY_UPSERT_SQL = """
INSERT INTO screener_history_entry(
    trade_date, stock_code, stock_name, continuous_limit_up, board_label,
    open_price, pre_close, auction_gain, auction_turnover, auction_volume_ratio,
    market_cap, gain_10d, industry, market_highest_board, limit_up_time,
    close_price, close_gain, day_change, next_day_open, next_day_auction_gain,
    next_day_close_gain, is_win, is_limit_up, is_zhaban, sanbanzhu, sanbanzhu_detail,
    matched_cycle, top30_streak, status, market_limit_down, weighted_auction_gain,
    yesterday_lianban_today_avg, b1_rate, concentration, decision_json, created_at, updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT (trade_date, stock_code) DO UPDATE SET
    stock_name=excluded.stock_name,
    continuous_limit_up=excluded.continuous_limit_up,
    board_label=excluded.board_label,
    open_price=excluded.open_price,
    pre_close=excluded.pre_close,
    auction_gain=excluded.auction_gain,
    auction_turnover=excluded.auction_turnover,
    auction_volume_ratio=excluded.auction_volume_ratio,
    market_cap=excluded.market_cap,
    gain_10d=excluded.gain_10d,
    industry=excluded.industry,
    market_highest_board=excluded.market_highest_board,
    limit_up_time=excluded.limit_up_time,
    close_price=excluded.close_price,
    close_gain=excluded.close_gain,
    day_change=excluded.day_change,
    next_day_open=excluded.next_day_open,
    next_day_auction_gain=excluded.next_day_auction_gain,
    next_day_close_gain=excluded.next_day_close_gain,
    is_win=excluded.is_win,
    is_limit_up=excluded.is_limit_up,
    is_zhaban=excluded.is_zhaban,
    sanbanzhu=excluded.sanbanzhu,
    sanbanzhu_detail=excluded.sanbanzhu_detail,
    matched_cycle=excluded.matched_cycle,
    top30_streak=excluded.top30_streak,
    status=excluded.status,
    market_limit_down=excluded.market_limit_down,
    weighted_auction_gain=excluded.weighted_auction_gain,
    yesterday_lianban_today_avg=excluded.yesterday_lianban_today_avg,
    b1_rate=excluded.b1_rate,
    concentration=excluded.concentration,
    decision_json=excluded.decision_json,
    created_at=excluded.created_at,
    updated_at=excluded.updated_at
"""

_SCREENER_HISTORY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS screener_history_entry (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    continuous_limit_up INTEGER,
    board_label TEXT,
    open_price REAL,
    pre_close REAL,
    auction_gain REAL,
    auction_turnover REAL,
    auction_volume_ratio REAL,
    market_cap REAL,
    gain_10d REAL,
    industry TEXT,
    market_highest_board INTEGER,
    limit_up_time TEXT,
    close_price REAL,
    close_gain REAL,
    day_change REAL,
    next_day_open REAL,
    next_day_auction_gain REAL,
    next_day_close_gain REAL,
    is_win INTEGER,
    is_limit_up INTEGER,
    is_zhaban INTEGER,
    sanbanzhu INTEGER,
    sanbanzhu_detail TEXT,
    matched_cycle INTEGER,
    top30_streak INTEGER,
    status TEXT,
    market_limit_down INTEGER,
    weighted_auction_gain REAL,
    yesterday_lianban_today_avg REAL,
    b1_rate REAL,
    concentration REAL,
    decision_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code)
);
CREATE INDEX IF NOT EXISTS ix_screener_hist_trade_date ON screener_history_entry(trade_date DESC);
"""


def _recreate_screener_history_table(conn: Any) -> None:
    """DROP+CREATE 绕过 ART 索引上 DELETE 全表触发的 fatal invalidate。"""
    conn.execute("DROP TABLE IF EXISTS screener_history_entry")
    executescript_compat(conn, _SCREENER_HISTORY_TABLE_DDL)


def _replace_screener_history_tx(
    conn: Any,
    rows: list[tuple[Any, ...]],
    new_keys: set[tuple[str, str]],
) -> None:
    """UPSERT + 按主键逐条删孤儿，避免 DELETE FROM screener_history_entry 全表。"""
    if rows:
        conn.executemany(_SCREENER_HISTORY_UPSERT_SQL, rows)
    cur = conn.execute("SELECT trade_date, stock_code FROM screener_history_entry")
    for trade_date, stock_code in cur.fetchall():
        key = (str(trade_date or ""), str(stock_code or "").strip()[-6:].zfill(6))
        if key not in new_keys:
            conn.execute(
                "DELETE FROM screener_history_entry WHERE trade_date=? AND stock_code=?",
                (trade_date, stock_code),
            )


def replace_screener_history_entries(records: list[dict[str, Any]]) -> None:
    """全量替换 screener_history（与原先整文件 JSON 语义一致）。"""
    from src.data.quant_db import (
        is_duckdb_index_delete_fatal,
        is_duckdb_invalidated,
        reset_shared_connection,
    )

    init_analytics_schema()
    ts = now_cn().isoformat()
    rows: list[tuple[Any, ...]] = []
    new_keys: set[tuple[str, str]] = set()
    for r in records:
        if not isinstance(r, dict):
            continue
        td = _to_ymd(str(r.get("date") or "")[:10])
        code = str(r.get("code") or "").strip()[-6:].zfill(6)
        if len(td) != 8 or len(code) != 6:
            continue
        rows.append(_history_rec_to_tuple(r, td, code, ts))
        new_keys.add((td, code))

    last_exc: Optional[BaseException] = None
    for attempt in range(3):
        with _LOCK:
            conn = _connect()
            try:
                conn.execute("BEGIN")
                if attempt >= 2:
                    _recreate_screener_history_table(conn)
                    if rows:
                        conn.executemany(_SCREENER_HISTORY_UPSERT_SQL, rows)
                else:
                    _replace_screener_history_tx(conn, rows, new_keys)
                conn.execute("COMMIT")
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    reset_shared_connection()
                if is_duckdb_invalidated(exc):
                    reset_shared_connection()
            finally:
                conn.close()
        if last_exc is None:
            break
        if attempt < 2 and (
            is_duckdb_index_delete_fatal(last_exc)
            or is_duckdb_invalidated(last_exc)
        ):
            reset_shared_connection()
            continue
        raise last_exc

    try:
        from src.data.ledger_doc_store import delete_ledger_doc_key

        delete_ledger_doc_key("screener_history.json")
    except Exception:
        pass


def load_screener_history_entries() -> list[dict[str, Any]]:
    """返回与 screener_history.json 相同结构的 list[dict]（按日期、代码排序）。"""
    init_analytics_schema()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """SELECT trade_date, stock_code, stock_name, continuous_limit_up, board_label,
                          open_price, pre_close, auction_gain, auction_turnover, auction_volume_ratio,
                          market_cap, gain_10d, industry, market_highest_board, limit_up_time,
                          close_price, close_gain, day_change, next_day_open, next_day_auction_gain,
                          next_day_close_gain, is_win, is_limit_up, is_zhaban, sanbanzhu, sanbanzhu_detail,
                          matched_cycle, top30_streak, status, market_limit_down, weighted_auction_gain,
                          yesterday_lianban_today_avg, b1_rate, concentration, decision_json, created_at, updated_at
                   FROM screener_history_entry ORDER BY trade_date ASC, stock_code ASC"""
            )
            raw = cur.fetchall()
        finally:
            conn.close()
    return [_tuple_to_history_rec(t) for t in raw]


def backfill_daily_screener_hit_from_history(*, min_iso_date: str = "2026-04-17") -> int:
    """将 screener_history_entry 中日期 >= min_iso 的记录写入 daily_screener_hit（不删已有其它日数据）。"""
    init_analytics_schema()
    min_td = _to_ymd(min_iso_date)
    recs = load_screener_history_entries()
    ts = now_cn().isoformat()
    n = 0
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            for r in recs:
                td = _to_ymd(str(r.get("date") or "")[:10])
                if len(td) != 8 or td < min_td:
                    continue
                code = str(r.get("code") or "").strip()[-6:].zfill(6)
                if len(code) != 6:
                    continue
                concepts = r.get("top_concepts") or r.get("concepts")
                cj = json.dumps(concepts, ensure_ascii=False) if concepts is not None else None
                core = {
                    "code",
                    "name",
                    "continuous_limit_up",
                    "auction_gain",
                    "auction_turnover",
                    "market_cap",
                    "volume_ratio",
                    "auction_volume_ratio",
                    "industry",
                    "top_concepts",
                    "concepts",
                    "date",
                    "matched_cycle",
                    "is_cycle_stock",
                    "created_at",
                }
                extra = {k: v for k, v in r.items() if k not in core}
                mc = 1 if (r.get("matched_cycle") or r.get("is_cycle_stock")) else 0
                cr_at = str(r.get("created_at") or ts)
                conn.execute(
                    "DELETE FROM daily_screener_hit WHERE hit_date = ? AND stock_code = ?",
                    (td, code),
                )
                conn.execute(
                    """INSERT INTO daily_screener_hit(
                        hit_date, stock_code, stock_name, continuous_limit_up,
                        auction_gain, auction_turnover, market_cap, volume_ratio,
                        industry, concepts_json, extra_json, matched_cycle, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        td,
                        code,
                        str(r.get("name") or ""),
                        _i(r.get("continuous_limit_up")),
                        _f(r.get("auction_gain")),
                        _f(r.get("auction_turnover")),
                        _f(r.get("market_cap")),
                        _f(r.get("volume_ratio") if r.get("volume_ratio") is not None else r.get("auction_volume_ratio")),
                        str(r.get("industry") or ""),
                        cj,
                        json.dumps(extra, ensure_ascii=False) if extra else None,
                        int(mc),
                        cr_at,
                        ts,
                    ),
                )
                n += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    return n


def sync_screener_history_from_daily_hit(*, hit_dates_yyyymmdd: list[str]) -> int:
    """将 `daily_screener_hit` 已有、`screener_history_entry` 缺失的 (交易日, 代码) 补写入选股历史。

    典型场景：`latest_screener` 已通过 `_save_screener` 写入 `daily_screener_hit`，但
    `archive_today_hits` → `_save` 失败；或事后用备份 JSON `replace_screener_history_entries`
    全量覆盖导致最近交易日丢失。
    """
    if not hit_dates_yyyymmdd:
        return 0
    init_analytics_schema()
    recs = load_screener_history_entries()
    existing: set[tuple[str, str]] = set()
    for r in recs:
        d = str(r.get("date") or "")[:10]
        c = str(r.get("code") or "").strip()[-6:].zfill(6)
        if len(d) == 10 and len(c) == 6:
            existing.add((d, c))

    rows_by_date: dict[str, list[tuple[Any, ...]]] = {}
    with _LOCK:
        conn = _connect()
        try:
            for hd in hit_dates_yyyymmdd:
                ymd = _to_ymd(hd)
                if len(ymd) != 8:
                    continue
                cur = conn.execute(
                    """SELECT stock_code, stock_name, continuous_limit_up, auction_gain,
                              auction_turnover, market_cap, volume_ratio, industry, concepts_json, extra_json,
                              matched_cycle, created_at
                       FROM daily_screener_hit WHERE hit_date = ?""",
                    (ymd,),
                )
                rows_by_date[ymd] = cur.fetchall()
        finally:
            conn.close()

    added = 0
    ts_fallback = now_cn().isoformat()
    for ymd, rows in rows_by_date.items():
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        for tup in rows:
            (
                code_raw,
                name,
                cl,
                ag,
                at,
                mc,
                vr,
                ind,
                cj,
                ej,
                mcy,
                cr_at,
            ) = tup
            code = str(code_raw or "").strip()[-6:].zfill(6)
            if len(code) != 6:
                continue
            if (iso, code) in existing:
                continue
            merged: dict[str, Any] = {}
            if ej:
                try:
                    ex = json.loads(str(ej))
                    if isinstance(ex, dict):
                        merged.update(ex)
                except (json.JSONDecodeError, TypeError):
                    pass
            merged["date"] = iso
            merged["code"] = code
            merged["name"] = str(name or merged.get("name") or "")
            merged["continuous_limit_up"] = int(cl if cl is not None else merged.get("continuous_limit_up") or 0)
            board = int(merged.get("continuous_limit_up") or 0)
            merged["board_label"] = f"{board}进{board + 1}" if board >= 1 else "首板"
            if ag is not None:
                merged["auction_gain"] = float(ag)
            elif merged.get("auction_gain") is None:
                merged["auction_gain"] = 0.0
            if at is not None:
                merged["auction_turnover"] = at
            if mc is not None:
                merged["market_cap"] = mc
            if vr is not None:
                merged["volume_ratio"] = vr
            merged["industry"] = str(ind or merged.get("industry") or "")
            if cj and not merged.get("top_concepts"):
                try:
                    merged["top_concepts"] = json.loads(str(cj))
                except (json.JSONDecodeError, TypeError):
                    pass
            if mcy is not None:
                merged["matched_cycle"] = bool(int(mcy))
                merged["is_cycle_stock"] = merged.get("is_cycle_stock", bool(int(mcy)))
            else:
                merged.setdefault("matched_cycle", bool(merged.get("matched_cycle")))
                merged.setdefault("is_cycle_stock", bool(merged.get("is_cycle_stock")))
            psd = merged.get("per_stock_decision")
            if isinstance(psd, dict):
                merged["decision"] = {
                    "action": psd.get("action"),
                    "position_pct": psd.get("position_pct"),
                    "position_text": psd.get("position_text"),
                    "ladder_label": psd.get("ladder_label"),
                    "can_open": psd.get("can_open"),
                    "reason": psd.get("reason"),
                }
                del merged["per_stock_decision"]
            merged.setdefault("open_price", merged.get("open_price") or 0)
            merged.setdefault("pre_close", merged.get("pre_close") or 0)
            merged.setdefault("auction_volume_ratio", merged.get("auction_volume_ratio"))
            merged.setdefault("gain_10d", merged.get("gain_10d"))
            merged.setdefault("market_highest_board", merged.get("market_highest_board"))
            merged.setdefault("limit_up_time", merged.get("limit_up_time"))
            merged.setdefault("close_price", None)
            merged.setdefault("close_gain", None)
            merged.setdefault("day_change", None)
            merged.setdefault("next_day_open", None)
            merged.setdefault("next_day_auction_gain", None)
            merged.setdefault("next_day_close_gain", None)
            merged.setdefault("is_win", None)
            merged.setdefault("is_limit_up", None)
            merged.setdefault("is_zhaban", None)
            merged.setdefault("sanbanzhu", False)
            merged.setdefault("sanbanzhu_detail", "")
            merged.setdefault("top30_streak", merged.get("top30_streak"))
            merged.setdefault("status", "pending")
            merged.setdefault("market_limit_down", merged.get("market_limit_down"))
            merged.setdefault("weighted_auction_gain", merged.get("weighted_auction_gain"))
            merged.setdefault("yesterday_lianban_today_avg", merged.get("yesterday_lianban_today_avg"))
            merged.setdefault("b1_rate", merged.get("b1_rate"))
            merged.setdefault("concentration", merged.get("concentration"))
            merged["created_at"] = str(cr_at or merged.get("created_at") or ts_fallback)
            recs.append(merged)
            existing.add((iso, code))
            added += 1

    if added:
        recs.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("code") or "")))
        replace_screener_history_entries(recs)
    return added


def diagnose_screener_history_sync(*, hit_date_yyyymmdd: str) -> dict[str, int]:
    """诊断某日选股数据在各表中的条数（补写 0 条时排查用）。"""
    ymd = _to_ymd(hit_date_yyyymmdd)
    iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}" if len(ymd) == 8 else ""
    init_analytics_schema()
    out: dict[str, int] = {
        "daily_screener_hit": 0,
        "screener_history_entry": 0,
        "daily_auction_scores": 0,
        "latest_screener_blob_hits": 0,
        "decision_records_hits": 0,
    }
    if len(ymd) != 8:
        return out
    out["daily_screener_hit"] = _count_daily_screener_hit(ymd)
    for r in load_screener_history_entries():
        if str(r.get("date") or "")[:10] == iso:
            out["screener_history_entry"] += 1
    with _LOCK:
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT COUNT(*) FROM daily_auction_scores WHERE score_date = ?",
                (ymd,),
            ).fetchone()
            out["daily_auction_scores"] = int(r[0] or 0)
            cur = conn.execute(
                "SELECT payload_json FROM daily_json_blob WHERE blob_kind = ? AND blob_date = ?",
                ("latest_screener", ymd),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if row and row[0]:
        try:
            blob = json.loads(str(row[0]))
            if isinstance(blob, dict):
                out["latest_screener_blob_hits"] = len(blob.get("hits") or [])
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        from src.data.json_io import load_json_file
        from src.config import DATA_DIR

        dr = load_json_file(DATA_DIR / "decision_records.json")
        if isinstance(dr, list):
            for rec in dr:
                if str(rec.get("date") or "")[:10] == iso:
                    out["decision_records_hits"] = len(rec.get("screener_hits") or [])
                    break
    except Exception:
        pass
    return out


def _hit_dict_from_auction_score(rec: dict[str, Any]) -> dict[str, Any]:
    import re

    detail = " ".join(
        str(rec.get(k) or "")
        for k in ("d1_detail", "d2_detail", "d3_detail", "d4_detail")
    )
    ag = 0.0
    m = re.search(r"竞价([\d.]+)%", detail)
    if m:
        try:
            ag = float(m.group(1))
        except ValueError:
            pass
    cl = 0
    m2 = re.search(r"(\d+)连板", detail)
    if m2:
        try:
            cl = int(m2.group(1))
        except ValueError:
            pass
    return {
        "code": str(rec.get("code") or "").strip()[-6:].zfill(6),
        "name": str(rec.get("name") or ""),
        "continuous_limit_up": cl,
        "auction_gain": ag,
        "open_price": 0,
        "auction_turnover": 0,
        "market_cap": 0,
        "volume_ratio": 0,
    }


def sync_screener_history_from_auction_scores(*, hit_dates_yyyymmdd: list[str]) -> int:
    """决策卡表 daily_auction_scores → screener_history_entry（daily_screener_hit 被 0 命中覆盖时的兜底）。"""
    if not hit_dates_yyyymmdd:
        return 0
    init_analytics_schema()
    recs = load_screener_history_entries()
    existing: set[tuple[str, str]] = set()
    for r in recs:
        d = str(r.get("date") or "")[:10]
        c = str(r.get("code") or "").strip()[-6:].zfill(6)
        if len(d) == 10 and len(c) == 6:
            existing.add((d, c))

    added = 0
    ts_fallback = now_cn().isoformat()
    for hd in hit_dates_yyyymmdd:
        ymd = _to_ymd(hd)
        if len(ymd) != 8:
            continue
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        scores = _load_auction_scores_for_date(ymd)
        if not scores:
            continue
        for rec in scores:
            code = str(rec.get("code") or "").strip()[-6:].zfill(6)
            if len(code) != 6 or (iso, code) in existing:
                continue
            hit = _hit_dict_from_auction_score(rec)
            merged = dict(hit)
            merged["date"] = iso
            merged["code"] = code
            board = int(merged.get("continuous_limit_up") or 0)
            merged["board_label"] = f"{board}进{board + 1}" if board >= 1 else "首板"
            merged.setdefault("status", "pending")
            merged["created_at"] = ts_fallback
            recs.append(merged)
            existing.add((iso, code))
            added += 1
    if added:
        recs.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("code") or "")))
        replace_screener_history_entries(recs)
    return added


def _load_auction_scores_for_date(ymd: str) -> list[dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """SELECT stock_code, stock_name, action, position, reason, score,
                          stop_loss, stop_loss_pct, d1_self, d2_leader, d3_ranking, d4_sector,
                          has_veto, vetoes_json, details_json
                   FROM daily_auction_scores WHERE score_date = ? ORDER BY score DESC""",
                (ymd,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    for tup in rows:
        (
            code,
            name,
            action,
            position,
            reason,
            score,
            sl,
            slp,
            d1,
            d2,
            d3,
            d4,
            hv,
            vj,
            dj,
        ) = tup
        rec: dict[str, Any] = {
            "code": code,
            "name": name,
            "action": action,
            "position": position,
            "reason": reason,
            "total_score": score,
            "stop_loss": sl,
            "stop_loss_pct": slp,
            "d1_self": d1,
            "d2_leader": d2,
            "d3_ranking": d3,
            "d4_sector": d4,
            "has_veto": bool(hv),
        }
        if vj:
            try:
                rec["vetoes"] = json.loads(vj)
            except json.JSONDecodeError:
                rec["vetoes"] = []
        if dj:
            try:
                det = json.loads(dj)
                if isinstance(det, dict):
                    rec.update(det)
            except json.JSONDecodeError:
                pass
        out.append(rec)
    return out


def sync_screener_history_all_sources(*, hit_dates_yyyymmdd: list[str]) -> dict[str, int]:
    """按优先级补写选股历史：daily_hit → latest/decision → auction_scores。"""
    result = {"daily_hit": 0, "archive": 0, "decision": 0, "auction": 0}
    result["daily_hit"] = sync_screener_history_from_daily_hit(hit_dates_yyyymmdd=hit_dates_yyyymmdd)
    if result["daily_hit"] > 0:
        return result

    from src.config import DATA_DIR, now_cn
    from src.data.json_io import load_json_file
    from src.engine.screener_history import archive_today_hits, _load

    for hd in hit_dates_yyyymmdd:
        ymd = _to_ymd(hd)
        if len(ymd) != 8:
            continue
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        hits: list[dict] = []
        sc = load_json_file(DATA_DIR / "latest_screener.json") or {}
        sc_date = _to_ymd(str(sc.get("date") or ""))
        if sc_date == ymd:
            hits = list(sc.get("hits") or [])
        if not hits:
            dr = load_json_file(DATA_DIR / "decision_records.json")
            if isinstance(dr, list):
                for rec in dr:
                    if str(rec.get("date") or "")[:10] == iso:
                        hits = list(rec.get("screener_hits") or [])
                        break
        if hits:
            before = sum(1 for r in _load() if str(r.get("date") or "")[:10] == iso)
            archive_today_hits(hits, trade_date=iso)
            after = sum(1 for r in _load() if str(r.get("date") or "")[:10] == iso)
            result["archive"] = max(result["archive"], after - before)
            if result["archive"] <= 0:
                result["decision"] = len(hits)

    result["daily_hit"] = sync_screener_history_from_daily_hit(hit_dates_yyyymmdd=hit_dates_yyyymmdd)
    if result["daily_hit"] > 0 or result["archive"] > 0:
        return result

    result["auction"] = sync_screener_history_from_auction_scores(hit_dates_yyyymmdd=hit_dates_yyyymmdd)
    return result


# --- 读取：组装回与原 JSON 尽量一致的 dict（供 load_json_file） ---

def load_migrated_snapshot(filename: str) -> Optional[Any]:
    init_analytics_schema()
    if filename == "latest_advice.json":
        return _load_advice_latest()
    if filename == "latest_ranking.json":
        return _load_ranking_latest()
    if filename == "latest_sentiment.json":
        return _load_sentiment_latest()
    if filename == "latest_auction_scores.json":
        return _load_auction_latest()
    if filename == "latest_screener.json":
        return _load_screener_latest()
    if filename == "latest_review.json":
        got = _load_json_blob_latest("latest_review")
        return got if isinstance(got, dict) else None
    kind = filename.replace(".json", "")
    if kind in (
        "latest_leader",
        "latest_insight",
        "latest_snapshot",
        "latest_signals",
        "latest_deviation",
        "latest_trend",
    ):
        return _load_json_blob_latest(kind)
    return None


def load_latest_review_document() -> dict[str, Any]:
    """最新复盘整包（daily_json_blob.blob_kind=latest_review）。"""
    init_analytics_schema()
    got = _load_json_blob_latest("latest_review")
    return got if isinstance(got, dict) else {}


def load_review_history_document() -> list[dict[str, Any]]:
    """历史复盘列表（ledger_doc.review_history.json）。"""
    from src.data.ledger_doc_store import load_json

    raw = load_json("review_history.json")
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def load_latest_sentiment_document() -> dict[str, Any]:
    """与 latest_sentiment.json 等价的结构化行组装结果。"""
    init_analytics_schema()
    got = _load_sentiment_latest()
    if not isinstance(got, dict):
        return {}
    return _patch_sentiment_for_pre_auction(got)


def load_latest_ranking_document() -> dict[str, Any]:
    """与 latest_ranking.json 等价的结构化行组装结果。"""
    init_analytics_schema()
    got = _load_ranking_latest()
    return got if isinstance(got, dict) else {}


def load_latest_leader_document() -> dict[str, Any]:
    """与 latest_leader.json 等价的 blob（daily_json_blob.latest_leader）。"""
    init_analytics_schema()
    got = _load_json_blob_latest("latest_leader")
    return got if isinstance(got, dict) else {}


def load_sentiment_market_for_ymd(ymd: str) -> dict[str, Any] | None:
    """指定交易日 YYYYMMDD 的 market 块（来自 daily_sentiment.market_json）。"""
    d = re.sub(r"\D", "", str(ymd or ""))[:8]
    if len(d) != 8:
        return None
    init_analytics_schema()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT market_json FROM daily_sentiment WHERE sent_date = ?",
                (d,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if not row or not row[0]:
        return None
    try:
        mkt = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    return mkt if isinstance(mkt, dict) else None


def load_prev_trading_day_sentiment_market() -> tuple[dict[str, Any] | None, str]:
    """上一交易日 9:26 落盘的 market 统计；返回 (market, YYYY-MM-DD)。"""
    from src.engine.screener_market_env import _prev_trading_day_iso

    prev_iso = _prev_trading_day_iso()
    prev_ymd = prev_iso.replace("-", "")
    mkt = load_sentiment_market_for_ymd(prev_ymd)
    if mkt:
        return mkt, prev_iso
    init_analytics_schema()
    today_ymd = now_cn().strftime("%Y%m%d")
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """SELECT sent_date, market_json FROM daily_sentiment
                   WHERE sent_date < ? ORDER BY sent_date DESC LIMIT 1""",
                (today_ymd,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if not row or not row[0]:
        return None, prev_iso
    try:
        mkt = json.loads(row[1])
    except (json.JSONDecodeError, TypeError):
        return None, prev_iso
    if not isinstance(mkt, dict):
        return None, prev_iso
    d = str(row[0])
    iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else prev_iso
    return mkt, iso


def _patch_sentiment_for_pre_auction(doc: dict[str, Any] | None) -> dict[str, Any]:
    """09:15 前：用上一交易日竞价 market 替换当日空/未生成块。"""
    if not isinstance(doc, dict):
        return doc or {}
    from src.market_schedule import is_before_trading_auction_open

    if not is_before_trading_auction_open():
        return doc
    pm, as_of = load_prev_trading_day_sentiment_market()
    if not pm:
        return doc
    out = dict(doc)
    m = dict(pm)
    m["auction_market_as_of"] = as_of
    m["auction_market_source"] = "prev_trading_day"
    out["market"] = m
    return out


def load_prev_day_limit_down_codes() -> list[str]:
    """上一交易日竞价跌停代码（daily_sentiment.market_json.limit_down_list）。"""
    init_analytics_schema()
    today_ymd = now_cn().strftime("%Y%m%d")
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """SELECT market_json FROM daily_sentiment
                   WHERE sent_date < ? ORDER BY sent_date DESC LIMIT 1""",
                (today_ymd,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if not row or not row[0]:
        return []
    try:
        mkt = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(mkt, dict):
        return []
    out: list[str] = []
    for item in mkt.get("limit_down_list") or []:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("code") or "").strip()
        digits = "".join(c for c in raw if c.isdigit())
        if len(digits) >= 6:
            out.append(digits[-6:].zfill(6))
    return out


def _repair_advice_dashboard_for_read(
    dash: dict[str, Any] | None, inputs: dict[str, Any]
) -> dict[str, Any] | None:
    """读侧修补：历史行可能出现 reason 与 v2 格子不一致、或 dashboard 里大量 null。"""
    if not isinstance(dash, dict):
        return dash
    part = dash.get("participate")
    if not isinstance(part, dict):
        part = {}
        dash["participate"] = part
    if isinstance(part, dict):
        if part.get("limit_down_main_board") is None:
            part["limit_down_main_board"] = 0
        if part.get("limit_down_all") is None:
            ld = inputs.get("limit_down")
            if ld is not None:
                try:
                    part["limit_down_all"] = int(ld)
                except (TypeError, ValueError):
                    pass
        if part.get("drop_over_9pct") is None:
            d9 = inputs.get("drop_over_9pct")
            if d9 is not None:
                try:
                    part["drop_over_9pct"] = float(d9)
                except (TypeError, ValueError):
                    pass
        try:
            from src.engine.dashboard_decision import (
                _apply_space_board_to_participate,
                main_board_lianban_space_auction,
                rebuild_dashboard_decision_from_participate,
            )
            from src.engine.screener_market_env import resolve_b1_with_review_date

            b1, rev_d = resolve_b1_with_review_date(hint_date=part.get("b1_review_date"))
            if b1 is not None:
                part["b1_rate"] = b1
                part["b1_review_date"] = rev_d
            try:
                ld_doc = load_latest_leader_document() or {}
                sp = main_board_lianban_space_auction(ld_doc, for_dashboard=True)
                _apply_space_board_to_participate(part, sp)
            except Exception:
                pass
        except Exception:
            pass
        try:
            from src.market_schedule import is_before_trading_auction_open

            refresh_live = not is_before_trading_auction_open()
        except Exception:
            refresh_live = True
        if refresh_live or part.get("relay_decision_index") is None:
            try:
                sf = load_latest_sentiment_document() or {}
                rsi = sf.get("relay_sentiment_index") or {}
                if isinstance(rsi, dict) and rsi.get("index") is not None:
                    part["relay_decision_index"] = float(rsi["index"])
                    part["relay_decision_detail"] = rsi
                    if rsi.get("prev_index") is not None:
                        try:
                            part["relay_decision_prev_index"] = float(rsi["prev_index"])
                        except (TypeError, ValueError):
                            pass
            except Exception:
                pass
        elif part.get("relay_decision_index") is None:
            try:
                sf = load_latest_sentiment_document() or {}
                rsi = sf.get("relay_sentiment_index") or {}
                if isinstance(rsi, dict) and rsi.get("index") is not None:
                    part["relay_decision_index"] = float(rsi["index"])
                    if not part.get("relay_decision_detail"):
                        part["relay_decision_detail"] = rsi
            except Exception:
                pass
        try:
            from src.engine.dashboard_decision import (
                count_main_board_auction_limit_down,
                resolve_auction_market_for_dashboard,
            )

            mkt = resolve_auction_market_for_dashboard(
                (inputs.get("market") if isinstance(inputs.get("market"), dict) else None)
            )
            if mkt:
                ld_mb = count_main_board_auction_limit_down(mkt)
                if ld_mb is not None:
                    part["limit_down_main_board"] = ld_mb
                if mkt.get("drop_over_9pct") is not None:
                    part["drop_over_9pct"] = float(mkt["drop_over_9pct"])
                if mkt.get("limit_down") is not None:
                    part["limit_down_all"] = mkt.get("limit_down")
                if mkt.get("auction_market_as_of"):
                    part["auction_market_as_of"] = mkt["auction_market_as_of"]
        except Exception:
            pass
        try:
            from src.engine.dashboard_decision import count_main_board_auction_limit_down

            sf = load_latest_sentiment_document() or {}
            mkt = sf.get("market") if isinstance(sf.get("market"), dict) else {}
            if mkt.get("limit_down_main_board") is not None:
                part["limit_down_main_board"] = int(mkt["limit_down_main_board"])
            elif mkt.get("limit_down_list"):
                part["limit_down_main_board"] = count_main_board_auction_limit_down(mkt)
            if mkt.get("limit_down") is not None:
                part["limit_down_all"] = mkt.get("limit_down")
            if mkt.get("drop_over_9pct") is not None:
                part["drop_over_9pct"] = float(mkt["drop_over_9pct"])
        except Exception:
            pass
        try:
            from src.engine.dashboard_decision import rebuild_dashboard_decision_from_participate

            dash["decision"] = rebuild_dashboard_decision_from_participate(part)
        except Exception:
            pass
    ref = dash.get("reference")
    if isinstance(ref, dict) and ref.get("pool_weighted_auction_top30") is None:
        w = inputs.get("weighted_auction_gain")
        if w is not None:
            try:
                ref["pool_weighted_auction_top30"] = float(w)
            except (TypeError, ValueError):
                pass
    return dash


def _is_legacy_advice_reason(reason: str | None) -> bool:
    """旧版四维警戒文案（加权竞价<0 + 单只高标水下），与 v2 决策树不一致。"""
    r = str(reason or "")
    if not r:
        return False
    if "四维警戒" in r:
        return True
    if "梯队加权竞价" in r and "偏弱" in r and "加权接力" not in r:
        return True
    if "昨日连板高标" in r and "水下" in r and "加权接力" not in r:
        return True
    return False


def _apply_v2_decision_to_advice_out(out: dict[str, Any], dash: dict[str, Any]) -> None:
    dec = dash.get("decision")
    if not isinstance(dec, dict):
        return
    tl = str(dec.get("tagline") or "").strip()
    if tl:
        out["reason"] = tl
    if dec.get("headline"):
        out["text"] = dec["headline"]
    if dec.get("conclusion"):
        out["conclusion"] = dec["conclusion"]
    if dec.get("position"):
        out["suggested_position"] = str(dec["position"])
    if dec.get("position_short"):
        out["suggested_position_short"] = str(dec["position_short"])
    if dec.get("bucket"):
        out["bucket"] = dec["bucket"]


def _rebuild_advice_dashboard_v2(*, allow_spot: bool = True) -> dict[str, Any] | None:
    """按 sentiment + leader 重算 v2 dashboard。

    allow_spot=True 时拉全市场 spot，用于：
    - 9:27 write_advice_snapshot / POST refresh-advice
    - 加权接力指数、连板高标现价、竞价跌停家数、参考区昨跌停/炸板反馈等

    GET /api/daily-advice 读侧应 allow_spot=False（见 _hydrate_advice_dashboard_on_read）：
    决策卡以 9:27 落盘快照为准，盘内/盘后读库即可，避免 60s 轮询打穿行情源。
    """
    try:
        from src.engine.advice_snapshot_hydrate import merge_spot_market_into_sentiment
        from src.engine.dashboard_decision import calc_daily_advice_v2

        sent = load_latest_sentiment_document() or {}
        leader = load_latest_leader_document() or {}
        spot = None
        if allow_spot:
            try:
                from src.data.fetcher import fetch_realtime_spot

                spot = fetch_realtime_spot()
            except Exception:
                pass
        if spot is not None and not getattr(spot, "empty", True):
            sent = merge_spot_market_into_sentiment(sent, spot)
        v2 = calc_daily_advice_v2(sent, leader, spot)
        dash = v2.get("dashboard")
        return dash if isinstance(dash, dict) else None
    except Exception as e:
        print(f"[决策快照] 读侧 v2 重建失败: {e}")
        return None


def _hydrate_advice_dashboard_on_read(
    out: dict[str, Any],
    dash: dict[str, Any] | None,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """修补 dashboard 并同步 v2 决策。

    读侧策略（GET /api/daily-advice）：
    - 不拉全市场 spot：决策卡 9:27 快照为真源，repair 只补 b1/高标(leader 落盘)/relay(sentiment 落盘)
    - 盘内若要重算参与/参考/决策树 → POST /api/refresh-advice（显式拉 spot）
    - 15:00 后同理，仅读库 + 复盘补 1进2
    """
    if not isinstance(dash, dict):
        dash = _rebuild_advice_dashboard_v2(allow_spot=False) or {}
    if isinstance(dash, dict):
        _repair_advice_dashboard_for_read(dash, inputs if isinstance(inputs, dict) else {})
        out["dashboard"] = dash
        _apply_v2_decision_to_advice_out(out, dash)
    return dash if isinstance(dash, dict) else {}


def _load_advice_latest() -> Optional[dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """SELECT advice_date, advice_text, bucket, reason, suggested_position,
                          suggested_position_short, conclusion, bad_count, weighted_auction_gain,
                          limit_down, dimensions_json, inputs_json, dashboard_json, updated_at
                   FROM daily_advice ORDER BY advice_date DESC LIMIT 1"""
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if not row:
        return None
    dims = json.loads(row[10]) if row[10] else {}
    inputs = json.loads(row[11]) if row[11] else {}
    dash_raw = json.loads(row[12]) if row[12] else None
    dash = dash_raw if isinstance(dash_raw, dict) else None
    ymd = str(row[0])
    disp = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]} 09:27:00"
    out: dict[str, Any] = {
        "generated_at": disp,
        "bucket": row[2],
        "text": row[1],
        "suggested_position": row[4] or "",
        "suggested_position_short": row[5] or "",
        "reason": row[3] or "",
        "conclusion": row[6] or row[1],
        "bad_count": row[7] if row[7] is not None else 0,
        "dimensions": dims,
        "inputs": inputs,
    }
    if isinstance(inputs, dict):
        if row[8] is not None and inputs.get("weighted_auction_gain") is None:
            inputs["weighted_auction_gain"] = row[8]
        if row[9] is not None and inputs.get("limit_down") is None:
            inputs["limit_down"] = row[9]
    out["inputs"] = inputs
    _hydrate_advice_dashboard_on_read(out, dash, inputs if isinstance(inputs, dict) else {})
    return out


def _load_ranking_latest() -> Optional[dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute("SELECT MAX(rank_date) FROM daily_ranking")
            r = cur.fetchone()
            if not r or not r[0]:
                return None
            d = str(r[0])
            cur2 = conn.execute(
                """SELECT rank_pos, stock_code, stock_name, gain_10d, market_cap, industry, concepts_json, extra_json, updated_at
                   FROM daily_ranking WHERE rank_date = ? ORDER BY rank_pos ASC""",
                (d,),
            )
            rows = cur2.fetchall()
            cur3 = conn.execute(
                "SELECT MAX(updated_at) FROM daily_ranking WHERE rank_date = ?",
                (d,),
            )
            ts_row = cur3.fetchone()
        finally:
            conn.close()
    if not rows:
        return None
    ranking: list[dict[str, Any]] = []
    for tup in rows:
        pos, code, name, g, mc, ind, cj, ej, row_ts = tup
        rec: dict[str, Any] = {"code": code, "name": name, "gain_10d": g, "market_cap": mc, "industry": ind}
        if cj:
            try:
                rec["concepts"] = json.loads(cj)
            except json.JSONDecodeError:
                rec["concepts"] = []
        if ej:
            try:
                rec.update(json.loads(ej))
            except json.JSONDecodeError:
                pass
        ranking.append(rec)
    trade_disp = f"{d[:4]}-{d[4:6]}-{d[6:8]} 15:30:00"
    raw_ts = str(ts_row[0] or "").strip() if ts_row and ts_row[0] else ""
    updated_disp = trade_disp
    if raw_ts:
        if "T" in raw_ts:
            updated_disp = raw_ts.replace("T", " ")[:19]
        elif len(raw_ts) >= 19:
            updated_disp = raw_ts[:19]
        else:
            updated_disp = raw_ts
    return {
        "date": trade_disp,
        "updated_at": updated_disp,
        "trade_date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
        "ranking": ranking,
    }


def _load_sentiment_latest() -> Optional[dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """SELECT sent_date, pool_size, avg_auction_gain, weighted_bid_avg,
                          high_open_count, flat_open_count, low_open_count, limit_down_count,
                          limit_up_flat, verdict, reason, total_stocks, market_json, relay_json,
                          prev_day_weighted, raw_extras_json
                   FROM daily_sentiment ORDER BY sent_date DESC LIMIT 1"""
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if not row:
        return None
    d = str(row[0])
    disp = f"{d[:4]}-{d[4:6]}-{d[6:8]} 09:27:00"
    out: dict[str, Any] = {
        "date": disp,
        "pool_size": row[1],
        "avg_auction_gain": row[2],
        "weighted_auction_gain": row[3],
        "high_open": row[4],
        "flat_open": row[5],
        "low_open": row[6],
        "limit_down": row[7],
        "limit_up_flat": row[8],
        "verdict": row[9],
        "reason": row[10],
    }
    if row[12]:
        try:
            out["market"] = json.loads(row[12])
        except json.JSONDecodeError:
            out["market"] = {}
    if row[13]:
        try:
            out["relay_sentiment_index"] = json.loads(row[13])
        except json.JSONDecodeError:
            pass
    if row[14] is not None:
        out["prev_day_weighted_auction_gain"] = row[14]
    if row[11] is not None:
        out["_market_total_stocks"] = row[11]
    if row[15]:
        try:
            extra = json.loads(row[15])
            if isinstance(extra, dict):
                out.update(extra)
        except json.JSONDecodeError:
            pass
    return out


def _load_auction_latest() -> Optional[list[Any]]:
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute("SELECT MAX(score_date) FROM daily_auction_scores")
            r = cur.fetchone()
            if not r or not r[0]:
                return None
            d = str(r[0])
            cur2 = conn.execute(
                """SELECT stock_code, stock_name, action, position, reason, score,
                          stop_loss, stop_loss_pct, d1_self, d2_leader, d3_ranking, d4_sector,
                          has_veto, vetoes_json, details_json
                   FROM daily_auction_scores WHERE score_date = ? ORDER BY score DESC""",
                (d,),
            )
            rows = cur2.fetchall()
        finally:
            conn.close()
    if not rows:
        return None
    out: list[dict[str, Any]] = []
    for tup in rows:
        (
            code,
            name,
            action,
            position,
            reason,
            score,
            sl,
            slp,
            d1,
            d2,
            d3,
            d4,
            hv,
            vj,
            dj,
        ) = tup
        rec = {
            "code": code,
            "name": name,
            "action": action,
            "position": position,
            "reason": reason,
            "total_score": score,
            "stop_loss": sl,
            "stop_loss_pct": slp,
            "d1_self": d1,
            "d2_leader": d2,
            "d3_ranking": d3,
            "d4_sector": d4,
            "has_veto": bool(hv),
        }
        if vj:
            try:
                rec["vetoes"] = json.loads(vj)
            except json.JSONDecodeError:
                rec["vetoes"] = []
        if dj:
            try:
                det = json.loads(dj)
                rec.update(det)
            except json.JSONDecodeError:
                pass
        out.append(rec)
    return out


def _assemble_screener_from_hit_rows(d: str, rows: list) -> dict[str, Any]:
    if not rows:
        disp = f"{d[:4]}-{d[4:6]}-{d[6:8]} 09:27:00"
        return {"date": disp, "hits": [], "status": "ok"}
    hits: list[dict[str, Any]] = []
    for tup in rows:
        code, name, cl, ag, at, mc, vr, ind, cj, ej, mcy, cr_at = tup
        h: dict[str, Any] = {
            "code": code,
            "name": name,
            "continuous_limit_up": cl,
            "auction_gain": ag,
            "auction_turnover": at,
            "market_cap": mc,
            "volume_ratio": vr,
            "industry": ind,
            "matched_cycle": bool(mcy) if mcy is not None else False,
            "created_at": cr_at or "",
        }
        if cj:
            try:
                arr = json.loads(cj)
                h["top_concepts"] = arr
            except json.JSONDecodeError:
                pass
        if ej:
            try:
                h.update(json.loads(ej))
            except json.JSONDecodeError:
                pass
        hits.append(h)
    disp = f"{d[:4]}-{d[4:6]}-{d[6:8]} 09:27:00"
    return {"date": disp, "hits": hits, "status": "ok"}


def _load_screener_latest() -> Optional[dict[str, Any]]:
    """最新选股：优先 daily_json_blob（含 0 命中当日），否则按 daily_screener_hit 组装。"""
    blob = _load_json_blob_latest("latest_screener")
    blob_ymd = _to_ymd(str((blob or {}).get("date") or ""))

    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute("SELECT MAX(hit_date) FROM daily_screener_hit")
            r = cur.fetchone()
            table_ymd = str(r[0]) if r and r[0] else ""
            rows: list = []
            if table_ymd:
                cur2 = conn.execute(
                    """SELECT stock_code, stock_name, continuous_limit_up, auction_gain,
                              auction_turnover, market_cap, volume_ratio, industry, concepts_json, extra_json,
                              matched_cycle, created_at
                       FROM daily_screener_hit WHERE hit_date = ?""",
                    (table_ymd,),
                )
                rows = cur2.fetchall()
        finally:
            conn.close()

    table_doc = _assemble_screener_from_hit_rows(table_ymd, rows) if table_ymd else None

    if blob and blob_ymd:
        if not table_doc or not table_ymd:
            return blob
        if blob_ymd >= table_ymd:
            return blob
        return table_doc
    return table_doc


def _load_json_blob_latest(kind: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT payload_json FROM daily_json_blob WHERE blob_kind = ? ORDER BY blob_date DESC LIMIT 1",
                (kind,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        obj = json.loads(str(row[0]))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# --- 概念 / 行业 ---

def replace_concept_from_doc(obj: dict[str, Any]) -> None:
    """由 concept_cache.json 全量对象写入 concept_info + concept_members。"""
    init_analytics_schema()
    concepts = obj.get("concepts") or {}
    meta = obj.get("_meta") or {}
    trade_date = _to_ymd(str(meta.get("updated_at") or "")[:10] or now_cn().strftime("%Y-%m-%d"))
    ts = now_cn().isoformat()
    info_rows: list[tuple[str, str, str, str]] = []
    member_rows: list[tuple[str, str, str]] = []
    for bk, info in concepts.items():
        if not isinstance(info, dict):
            continue
        code = str(bk).strip()
        name = str(info.get("name") or "").strip()
        if not code or not name:
            continue
        info_rows.append((code, name, "eastmoney", ts))
        for sc in info.get("stocks") or []:
            s = str(sc).strip()[-6:].zfill(6)
            if len(s) == 6 and s.isdigit():
                member_rows.append((code, s, trade_date))
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM concept_members WHERE trade_date = ?", (trade_date,))
            if info_rows:
                conn.executemany(
                    "INSERT INTO concept_info(concept_code, concept_name, source, updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(concept_code) DO UPDATE SET "
                    "concept_name=excluded.concept_name, source=excluded.source, updated_at=excluded.updated_at",
                    info_rows,
                )
            if member_rows:
                conn.executemany(
                    "INSERT INTO concept_members(concept_code, stock_code, trade_date) VALUES (?,?,?) "
                    "ON CONFLICT(concept_code, stock_code, trade_date) DO NOTHING",
                    member_rows,
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def load_concept_full_doc() -> dict[str, Any]:
    """组装与 concept_cache.json 兼容的结构（供下游不改字段）。"""
    init_analytics_schema()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute("SELECT concept_code, concept_name FROM concept_info")
            infos = cur.fetchall()
            cur2 = conn.execute(
                """SELECT concept_code, stock_code FROM concept_members
                   WHERE trade_date = (SELECT MAX(trade_date) FROM concept_members)"""
            )
            mems = cur2.fetchall()
            cur3 = conn.execute("SELECT MAX(updated_at) FROM concept_info")
            m3 = cur3.fetchone()
        finally:
            conn.close()
    concepts: dict[str, Any] = {}
    for bk, name in infos:
        concepts[str(bk)] = {"name": str(name), "stocks": []}
    for bk, sc in mems:
        bk = str(bk)
        if bk in concepts and isinstance(concepts[bk], dict):
            concepts[bk]["stocks"].append(sc)
    stock_to: dict[str, list[str]] = {}
    for bk, info in concepts.items():
        nm = str(info.get("name") or "")
        for sc in info.get("stocks") or []:
            stock_to.setdefault(str(sc), []).append(nm)
    for sc in stock_to:
        stock_to[sc] = sorted(set(stock_to[sc]))
    return {
        "_meta": {"updated_at": (m3[0] if m3 else "") or now_cn().strftime("%Y-%m-%d %H:%M:%S")},
        "concepts": concepts,
        "stock_to_concepts": stock_to,
    }


def replace_industry_map(ind: dict[str, str], as_of: str | None = None) -> None:
    init_analytics_schema()
    d = _to_ymd(as_of or now_cn().strftime("%Y-%m-%d"))
    ts = now_cn().isoformat()
    rows = [(str(k).strip()[-6:].zfill(6), str(v), d, ts) for k, v in ind.items() if k and v]
    rows = [r for r in rows if len(r[0]) == 6 and r[0].isdigit()]
    if not rows:
        return
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM industry_member WHERE as_of_date = ?", (d,))
            conn.executemany(
                "INSERT INTO industry_member(stock_code, industry_name, as_of_date, updated_at) VALUES (?,?,?,?)",
                rows,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def load_industry_map_latest() -> dict[str, str]:
    init_analytics_schema()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute("SELECT MAX(as_of_date) FROM industry_member")
            r = cur.fetchone()
            if not r or not r[0]:
                return {}
            d = str(r[0])
            cur2 = conn.execute(
                "SELECT stock_code, industry_name FROM industry_member WHERE as_of_date = ?",
                (d,),
            )
            return {str(a): str(b) for a, b in cur2.fetchall()}
        finally:
            conn.close()


def is_migrated_snapshot_filename(name: str) -> bool:
    return name in (
        "latest_advice.json",
        "latest_ranking.json",
        "latest_sentiment.json",
        "latest_auction_scores.json",
        "latest_screener.json",
        "latest_leader.json",
        "latest_insight.json",
        "latest_snapshot.json",
        "latest_signals.json",
        "latest_deviation.json",
        "latest_trend.json",
        "latest_review.json",
    )

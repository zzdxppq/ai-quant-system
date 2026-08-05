"""JSON 逻辑路径与 quant 库映射（DATA_STORAGE_BACKEND 恒为 quant）。

所有 `DATA_DIR` 下相对路径的读写均经 ledger / analytics / structured / relational，
逻辑路径与 `data/quant.duckdb`（DuckDB）映射；不读写业务 JSON 磁盘文件。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

_NO_RELATIONAL_PREFIX = ("kline_cache/", "minute_cache/", "auction_cache/")


def _relational_eligible(key: str | None) -> bool:
    if not key:
        return False
    from src.data.ledger_doc_store import is_ledger_doc_key

    if is_ledger_doc_key(key):
        return False
    if key in ("concept_cache.json", "industry_cache.json"):
        return False
    return not key.startswith(_NO_RELATIONAL_PREFIX)


def _snapshot_path(path: Path) -> bool:
    """data 根目录下的 latest_* 快照 JSON（走 daily_snapshot / analytics 路径）。"""
    from src.config import DATA_DIR
    from src.data.structured_store import is_daily_snapshot_filename

    try:
        if path.resolve().parent != DATA_DIR.resolve():
            return False
    except OSError:
        return False
    return is_daily_snapshot_filename(path.name)


def _load_structured_misc(path: Path) -> Optional[Any]:
    """auction_cache / minute_cache 等走 structured_store。"""
    from src.data.data_paths import doc_key_for_path

    key = doc_key_for_path(path)
    if not key or _relational_eligible(key):
        return None
    if key.startswith("auction_cache/"):
        from src.data.structured_store import load_auction_session

        parts = key.replace("\\", "/").split("/")
        if len(parts) >= 3:
            date_part = re.sub(r"\D", "", parts[-2])
            code = parts[-1].replace(".json", "")
            if len(date_part) == 8:
                return load_auction_session(code, date_part)
    if key.startswith("minute_cache/"):
        from src.data.structured_store import load_minute_payload

        stem = Path(key).stem
        m = re.match(r"^(\d{6})_(\d{8})$", stem)
        if m:
            return load_minute_payload(m.group(1), m.group(2))
    return None


def _load_from_db(path: Path) -> Optional[Any]:
    from src.data.data_paths import doc_key_for_path
    from src.data.relational_sqlite import load_document

    key = doc_key_for_path(path)
    if not key or not _relational_eligible(key):
        return None
    return load_document(key)


def _ledger_load(doc_key: str | None) -> Optional[Any]:
    if not doc_key:
        return None
    from src.data.ledger_doc_store import is_ledger_doc_key, load_json

    if not is_ledger_doc_key(doc_key):
        return None
    return load_json(doc_key)


def load_json_file(path: Path) -> Optional[Any]:
    """从 quant 库解析 `path` 对应的 doc_key（无磁盘 JSON 回退）。"""
    from src.data.data_paths import doc_key_for_path

    key = doc_key_for_path(path)
    if key == "concept_cache.json":
        from src.data.analytics_store import load_concept_full_doc

        doc = load_concept_full_doc()
        if doc.get("concepts") or doc.get("stock_to_concepts"):
            return doc
    if key == "industry_cache.json":
        from src.data.analytics_store import load_industry_map_latest

        im = load_industry_map_latest()
        if im:
            return im
    if key == "screener_history.json":
        from src.data.analytics_store import load_screener_history_entries
        from src.data.ledger_doc_store import load_json as _ledger_load_by_key

        rows = load_screener_history_entries()
        if rows:
            return rows
        led = _ledger_load_by_key("screener_history.json")
        if isinstance(led, list) and led:
            return led
    if key:
        led = _ledger_load(key)
        if led is not None:
            return led
    if _snapshot_path(path):
        from src.data.structured_store import load_latest_daily_snapshot

        snap = load_latest_daily_snapshot(path.name)
        if snap is not None:
            return snap
    misc = _load_structured_misc(path)
    if misc is not None:
        return misc
    got = _load_from_db(path)
    if got is not None:
        return got
    return None


def dump_json_file(path: Path, obj: Any, *, indent: int | None = 2) -> None:
    """写入 quant 库；成功后删除 path 若存在（清理历史残留 .json）。"""
    from src.data.data_paths import doc_key_for_path
    from src.data.relational_sqlite import save_document
    from src.data.structured_store import save_daily_snapshot

    key = doc_key_for_path(path)
    snap = bool(key and _snapshot_path(path))
    if key == "concept_cache.json" and isinstance(obj, dict):
        from src.data.analytics_store import replace_concept_from_doc

        replace_concept_from_doc(obj)
    elif key == "industry_cache.json" and isinstance(obj, dict):
        from src.data.analytics_store import replace_industry_map

        replace_industry_map(obj)
    elif key == "screener_history.json" and isinstance(obj, list):
        from src.data.analytics_store import (
            backfill_daily_screener_hit_from_history,
            replace_screener_history_entries,
        )

        replace_screener_history_entries(obj)
        backfill_daily_screener_hit_from_history()
    elif key:
        from src.data.ledger_doc_store import is_ledger_doc_key, upsert_json

        if is_ledger_doc_key(key):
            upsert_json(key, obj)
        elif snap:
            save_daily_snapshot(path.name, obj)
        elif _relational_eligible(key):
            save_document(key, obj)
    elif snap:
        save_daily_snapshot(path.name, obj)
    if key or snap:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass


def data_dir_glob_json(rel_glob: str) -> list[Path]:
    """在 DATA_DIR 下按 glob 列举逻辑 JSON 路径（仅来自库键）。"""
    from src.config import DATA_DIR
    from src.data.relational_sqlite import list_doc_keys_glob

    rel_glob = rel_glob.replace("\\", "/")
    seen: dict[str, Path] = {}
    for k in list_doc_keys_glob(rel_glob):
        seen[k] = DATA_DIR / k
    return sorted(seen.values(), key=lambda p: p.name, reverse=True)

"""从 `backups/*/…/*.json` 灌入 DuckDB（仅升级/一次性迁移）。

与线上一致：优先走 `dump_json_file(DATA_DIR / doc_key, obj)`，自动路由到
analytics / daily_json_blob / ledger_doc / relational(jdt) 等。

**不要在 API 启动时调用**（避免误覆盖新库）。升级后执行：
  python scripts/migrate_from_backup_on_upgrade.py
  或：python -m src.data.latest_snapshot_hydrate
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.config import BASE_DIR, DATA_DIR

# 仍支持「只灌三类 latest_*」的细粒度 API（与旧版兼容）
_SNAPSHOT_FILES: tuple[str, ...] = (
    "latest_advice.json",
    "latest_sentiment.json",
    "latest_leader.json",
)

# 备份根目录下各子目录内的 *.json 灌库时的推荐顺序（其余按名字排序接在后面）
_HYDRATE_PRIORITY: tuple[str, ...] = (
    "concept_cache.json",
    "industry_cache.json",
    "latest_ranking.json",
    "screener_history.json",
    "latest_sentiment.json",
    "latest_leader.json",
    "latest_review.json",
    "latest_advice.json",
    "latest_screener.json",
    "latest_auction_scores.json",
    "latest_insight.json",
    "latest_signals.json",
    "latest_deviation.json",
    "latest_trend.json",
    "latest_snapshot.json",
)


def backups_root() -> Path:
    return BASE_DIR / "backups"


def sorted_backup_data_dirs() -> list[Path]:
    root = backups_root()
    if not root.is_dir():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.name, reverse=True)
    return dirs


def read_newest_backup_json(filename: str) -> Any | None:
    """在 backups 下各子目录中从新到旧查找首个有效 JSON 文件。"""
    for d in sorted_backup_data_dirs():
        fp = d / filename
        if not fp.is_file():
            continue
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
    return None


def iter_doc_keys_union_backups() -> list[str]:
    """所有备份子目录中出现过的根级 *.json 文件名（并集），去重排序。"""
    keys: set[str] = set()
    for d in sorted_backup_data_dirs():
        for fp in d.glob("*.json"):
            keys.add(fp.name)
    return sorted(keys)


def db_needs_hydrate_for_doc_key(doc_key: str) -> bool:
    """与 `load_json_file(DATA_DIR / doc_key)` 语义对齐：无数据或 screener 空列表则视为需补水。"""
    from src.data.json_io import load_json_file

    try:
        data = load_json_file(DATA_DIR / doc_key)
    except Exception:
        return True
    if data is None:
        return True
    if doc_key == "screener_history.json" and isinstance(data, list):
        return len(data) == 0
    return False


def _ordered_hydration_keys(all_keys: list[str]) -> list[str]:
    pri = [k for k in _HYDRATE_PRIORITY if k in set(all_keys)]
    rest = sorted(k for k in all_keys if k not in pri)
    return pri + rest


def upgrade_hydrate_all_from_backups(*, force: bool = False) -> dict[str, str]:
    """遍历备份中出现的 doc_key，对库中仍缺（或 force）的项调用 `dump_json_file` 写入。

    覆盖 screener_history、latest_review、ledger_doc 键、relational 注册文档等。
    """
    from src.data import analytics_store as ast
    from src.data.json_io import dump_json_file
    from src.data.ledger_doc_store import init_ledger_doc_schema
    from src.data.relational_sqlite import init_schema as init_rel_schema
    from src.data.structured_store import init_structured_schema

    try:
        init_structured_schema()
    except Exception as e:
        print(f"[backup-hydrate] structured 表: {e}")
    try:
        ast.init_analytics_schema()
    except Exception as e:
        print(f"[backup-hydrate] analytics 表: {e}")
    try:
        init_ledger_doc_schema()
    except Exception as e:
        print(f"[backup-hydrate] ledger_doc 表: {e}")
    try:
        init_rel_schema()
    except Exception as e:
        print(f"[backup-hydrate] relational 元数据: {e}")

    keys = iter_doc_keys_union_backups()
    ordered = _ordered_hydration_keys(keys)
    out: dict[str, str] = {}
    for doc_key in ordered:
        if not force and not db_needs_hydrate_for_doc_key(doc_key):
            out[doc_key] = "skip_db_nonempty"
            continue
        raw = read_newest_backup_json(doc_key)
        if raw is None:
            out[doc_key] = "missing_backup"
            continue
        try:
            dump_json_file(DATA_DIR / doc_key, raw)
            out[doc_key] = "restored"
        except Exception as e:
            out[doc_key] = f"save_error:{e!s}"[:200]
    return out


def hydrate_latest_snapshots_from_backups(*, force: bool = False) -> dict[str, str]:
    """仅三类 latest_*（不经由全量 doc 循环，与旧测试/脚本兼容）。"""
    from src.data import analytics_store as ast

    ast.init_analytics_schema()
    out: dict[str, str] = {}
    for name in _SNAPSHOT_FILES:
        if not force:
            try:
                if ast.load_migrated_snapshot(name) is not None:
                    out[name] = "skip_db_nonempty"
                    continue
            except Exception:
                pass
        raw = read_newest_backup_json(name)
        if raw is None:
            out[name] = "missing_backup"
            continue
        if not isinstance(raw, dict):
            out[name] = "invalid_json_type"
            continue
        try:
            ast.save_from_latest_filename(name, raw)
            out[name] = "restored"
        except Exception as e:
            out[name] = f"save_error:{e!s}"[:200]
    return out


def upgrade_hydrate_latest_snapshots_from_backups() -> dict[str, str]:
    """兼容旧名：仅建表 + 三类 latest_* 补缺（不跑全量备份键）。"""
    from src.data import analytics_store as ast
    from src.data.structured_store import init_structured_schema

    try:
        init_structured_schema()
    except Exception as e:
        print(f"[latest-snapshots-upgrade] structured 表初始化: {e}")
    try:
        ast.init_analytics_schema()
    except Exception as e:
        print(f"[latest-snapshots-upgrade] analytics 表初始化失败: {e}")
        return {k: f"init_error:{e!s}"[:120] for k in _SNAPSHOT_FILES}
    try:
        summary = hydrate_latest_snapshots_from_backups(force=False)
        print(f"[latest-snapshots-upgrade] 结果: {summary}")
        return summary
    except Exception as e:
        print(f"[latest-snapshots-upgrade] 补水失败: {e}")
        return {k: f"error:{e!s}"[:120] for k in _SNAPSHOT_FILES}


if __name__ == "__main__":
    import sys

    if "--force-backup" in sys.argv and "--latest-only" in sys.argv:
        from src.data import analytics_store as ast

        ast.init_analytics_schema()
        print(hydrate_latest_snapshots_from_backups(force=True))
    elif "--force-backup" in sys.argv:
        print(upgrade_hydrate_all_from_backups(force=True))
    elif "--latest-only" in sys.argv:
        upgrade_hydrate_latest_snapshots_from_backups()
    else:
        summary = upgrade_hydrate_all_from_backups(force=False)
        n_restored = sum(1 for v in summary.values() if v == "restored")
        print(f"[backup-hydrate] 完成 keys={len(summary)} restored={n_restored}")
        if n_restored:
            for k, v in sorted(summary.items()):
                if v != "skip_db_nonempty":
                    print(f"  {k}: {v}")

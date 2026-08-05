"""兼容旧 import：路径工具见 data_paths，结构化存储见 relational_sqlite。"""
from __future__ import annotations

from pathlib import Path

from src.data.data_paths import discover_data_json_files, doc_key_for_path, should_skip_json_path
from src.data.relational_sqlite import init_schema, import_json_file, list_doc_keys_glob, load_document, save_document

__all__ = [
    "discover_data_json_files",
    "doc_key_for_path",
    "should_skip_json_path",
    "init_schema",
    "import_path",
    "list_keys_glob",
    "get_body",
    "upsert_body",
    "export_key_to_path",
]


def import_path(doc_key: str, path: Path) -> bool:
    return import_json_file(doc_key, path)


def list_keys_glob(glob_pat: str) -> list[str]:
    return list_doc_keys_glob(glob_pat)


def get_body(doc_key: str) -> str | None:
    """已废弃：旧 app_documents 接口。返回 None 强制走文件/新逻辑。"""
    return None


def upsert_body(doc_key: str, body: str) -> None:
    raise RuntimeError(
        "app_documents 已移除。请用 json_io.dump_json_file(DATA_DIR 下路径, obj) "
        "或 relational_sqlite.save_document(doc_key, obj)（按 doc_key 自动路由 structured/analytics/ledger/jdt）。"
    )


def export_key_to_path(doc_key: str, dest: Path) -> bool:
    obj = load_document(doc_key)
    if obj is None:
        return False
    import json

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return True

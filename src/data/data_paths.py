"""data 目录下相对路径（doc_key）工具。"""
from __future__ import annotations

from pathlib import Path


def doc_key_for_path(path: Path) -> str | None:
    from src.config import DATA_DIR

    try:
        rp = path.resolve()
        rd = DATA_DIR.resolve()
        rel = rp.relative_to(rd)
        return rel.as_posix()
    except (ValueError, OSError):
        return None


def should_skip_json_path(path: Path, data_dir: Path) -> bool:
    try:
        rel = path.resolve().relative_to(data_dir.resolve())
    except ValueError:
        return True
    if "backups" in rel.parts:
        return True
    return any(p.startswith("json_backup_") for p in rel.parts)


def discover_data_json_files(data_dir: Path) -> list[tuple[str, Path]]:
    data_dir = data_dir.resolve()
    out: list[tuple[str, Path]] = []
    for p in sorted(data_dir.rglob("*.json")):
        if not p.is_file():
            continue
        if should_skip_json_path(p, data_dir):
            continue
        rel = p.resolve().relative_to(data_dir).as_posix()
        out.append((rel, p))
    return out

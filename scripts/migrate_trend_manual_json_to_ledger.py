"""将 data/trend_*_manual.json 迁入 quant `ledger_doc` 后删除磁盘残留（一次性）。"""
from __future__ import annotations

import json
from pathlib import Path

from src.config import DATA_DIR
from src.data.ledger_doc_store import upsert_json


def main() -> None:
    for name in ("trend_pool_manual.json", "trend_history_manual.json"):
        p = DATA_DIR / name
        if not p.is_file():
            continue
        raw = json.loads(p.read_text(encoding="utf-8"))
        upsert_json(name, raw)
        p.unlink()
        print(f"[migrate] {name} → ledger_doc, removed {p}")


if __name__ == "__main__":
    main()

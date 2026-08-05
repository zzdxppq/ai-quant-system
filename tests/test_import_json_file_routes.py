"""import_json_file 与 jdt 孤儿表清理：路由到 analytics / ledger / structured，避免误建 registry。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.config as cfg
import src.data.ledger_doc_store as led
import src.data.relational_sqlite as rs
from src.data.quant_db import connect


def _patch_db(monkeypatch, tmp_path: Path) -> Path:
    db = tmp_path / "quant.duckdb"
    monkeypatch.setattr(cfg, "DB_PATH", db)
    monkeypatch.setattr(cfg, "SKIP_JSON_DOC_REGISTRY", False)
    return db


def test_import_concept_no_jdt_registry(tmp_path: Path, monkeypatch):
    _patch_db(monkeypatch, tmp_path)
    rs.init_schema()
    p = tmp_path / "concept_cache.json"
    p.write_text(
        json.dumps({"concepts": {"600000": {"name": "浦发"}}, "_meta": {}}),
        encoding="utf-8",
    )
    assert rs.import_json_file("concept_cache.json", p)
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM app_json_doc_registry WHERE doc_key=?",
            ("concept_cache.json",),
        ).fetchone()[0]
        assert int(n) == 0
    finally:
        conn.close()


def test_import_review_history_to_ledger(tmp_path: Path, monkeypatch):
    _patch_db(monkeypatch, tmp_path)
    rs.init_schema()
    p = tmp_path / "review_history.json"
    body = [{"d": "2026-05-01"}]
    p.write_text(json.dumps(body), encoding="utf-8")
    assert rs.import_json_file("review_history.json", p)
    assert led.load_json("review_history.json") == body
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM app_json_doc_registry WHERE doc_key=?",
            ("review_history.json",),
        ).fetchone()[0]
        assert int(n) == 0
    finally:
        conn.close()


def test_drop_orphan_jdt(tmp_path: Path, monkeypatch):
    _patch_db(monkeypatch, tmp_path)
    rs.init_schema()
    conn = connect()
    try:
        conn.execute("CREATE TABLE jdt_zombie_test (id INTEGER)")
    finally:
        conn.close()
    assert "jdt_zombie_test" in rs.list_orphan_jdt_tables()
    dropped = rs.drop_orphan_jdt_tables()
    assert "jdt_zombie_test" in dropped
    assert "jdt_zombie_test" not in rs.list_orphan_jdt_tables()

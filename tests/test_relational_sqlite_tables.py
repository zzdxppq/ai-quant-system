"""relational_sqlite：每 doc_key 独立表，宽表 / 行表 roundtrip。"""
import json

import pytest

import src.config as cfg
from src.data import relational_sqlite as rs
from src.data.quant_db import connect, table_exists


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "quant.duckdb"
    monkeypatch.setattr(cfg, "DB_PATH", db)
    monkeypatch.setattr(cfg, "SKIP_JSON_DOC_REGISTRY", False)
    rs.init_schema()
    yield db


def test_table_name_uses_json_slug(isolated_db):
    rs.save_document("latest_ranking.json", {"x": 1})
    conn = connect()
    try:
        cur = conn.execute(
            "SELECT data_table FROM app_json_doc_registry WHERE doc_key = ?",
            ("latest_ranking.json",),
        )
        row = cur.fetchone()
        assert row is not None
        assert str(row[0]) == "jdt_latest_ranking"
    finally:
        conn.close()


def test_save_load_wide_roundtrip(isolated_db):
    obj = {"a": 1, "nested": {"x": True, "y": None}, "z": [1, 2]}
    rs.save_document("latest_ranking.json", obj)
    got = rs.load_document("latest_ranking.json")
    assert json.loads(json.dumps(obj)) == json.loads(json.dumps(got))


def test_list_doc_keys_glob(isolated_db):
    rs.save_document("b.json", {"k": 1})
    rs.save_document("a.json", {"k": 2})
    keys = rs.list_doc_keys_glob("*.json")
    assert set(keys) >= {"a.json", "b.json"}


def test_row_mode_when_many_leaves(isolated_db, monkeypatch):
    monkeypatch.setattr(rs, "WIDE_MAX_LEAVES", 5)
    obj = {"items": [{"i": j, "s": str(j)} for j in range(20)]}
    rs.save_document("big.json", obj)
    got = rs.load_document("big.json")
    assert got == obj


def test_skip_mode_roundtrip_ledger(tmp_path, monkeypatch):
    db = tmp_path / "quant.duckdb"
    monkeypatch.setattr(cfg, "DB_PATH", db)
    monkeypatch.setattr(cfg, "SKIP_JSON_DOC_REGISTRY", True)
    rs.init_schema()
    obj = {"x": 1}
    rs.save_document("adhoc.json", obj)
    assert rs.load_document("adhoc.json") == obj
    conn = connect()
    try:
        assert not table_exists(conn, "app_json_doc_registry")
    finally:
        conn.close()

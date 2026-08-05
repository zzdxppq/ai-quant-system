"""json_io：quant 库路径映射与 UTF-8 往返。"""
from pathlib import Path

from src.data.json_io import dump_json_file, load_json_file


def test_load_json_file_utf8(tmp_path: Path, monkeypatch):
    import src.config as cfg

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    p = tmp_path / "a.json"
    obj = {"x": 1, "name": "测试"}
    dump_json_file(p, obj)
    assert load_json_file(p) == obj


def test_ledger_doc_store_roundtrip(tmp_path: Path, monkeypatch):
    import src.config as cfg

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "quant.duckdb")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    p = tmp_path / "review_history.json"
    sample = {"items": [{"d": "2026-05-01"}]}
    dump_json_file(p, sample)
    assert load_json_file(p) == sample

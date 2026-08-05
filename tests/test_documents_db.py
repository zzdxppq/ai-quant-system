"""documents_db / json_io 与 DATA_DIR 路径键。"""
from pathlib import Path

from src.config import DATA_DIR
from src.data.data_paths import doc_key_for_path, discover_data_json_files


def test_doc_key_under_data_dir():
    p = DATA_DIR / "latest_ranking.json"
    assert doc_key_for_path(p) == "latest_ranking.json"


def test_doc_key_nested():
    p = DATA_DIR / "kline_cache" / "600000_240_500.json"
    assert doc_key_for_path(p) == "kline_cache/600000_240_500.json"


def test_doc_key_outside_returns_none():
    assert doc_key_for_path(Path("C:/Windows/system.ini")) is None


def test_discover_skips_backups(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.json").write_text("{}", encoding="utf-8")
    bdir = data / "backups" / "x"
    bdir.mkdir(parents=True)
    (bdir / "b.json").write_text("{}", encoding="utf-8")
    pairs = discover_data_json_files(data)
    keys = {k for k, _ in pairs}
    assert "a.json" in keys
    assert not any(k.startswith("backups/") for k in keys)

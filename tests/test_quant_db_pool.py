"""DuckDB 进程内连接复用。"""
from src.data import quant_db


def test_connect_returns_pooled_wrapper(tmp_path, monkeypatch):
    dbp = tmp_path / "pool_test.duckdb"
    monkeypatch.setattr("src.config.DB_PATH", dbp)
    quant_db.reset_shared_connection()
    c1 = quant_db.connect()
    c2 = quant_db.connect()
    assert c1 is c2
    c1.close()
    c3 = quant_db.connect()
    assert c3 is c1
    quant_db.reset_shared_connection()

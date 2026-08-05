import pandas as pd
import pytest

from src.data import kline_file_cache as kfc


def test_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(kfc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kfc, "KLINE_CACHE_ENABLED", True)
    monkeypatch.setattr(kfc, "KLINE_CACHE_TTL_SECONDS", 86400)

    df = pd.DataFrame({
        "date": ["2026-05-01", "2026-05-02"],
        "open": [1.0, 2.0],
        "close": [1.1, 2.1],
        "high": [1.2, 2.2],
        "low": [0.9, 1.9],
        "volume": [100.0, 200.0],
    })
    kfc.write_cache("600519", "240", 15, df)

    got = kfc.try_read_cache("600519", "240", 15, allow_stale=False)
    assert got is not None and len(got) == 2
    assert float(got.iloc[-1]["close"]) == pytest.approx(2.1)


def test_cache_path_uses_last_six_digits():
    p = kfc.cache_path("sh600519", "240", 500)
    assert "600519" in p.name and p.name.endswith("_500.json")


def test_ttl_reject_then_allow_stale(tmp_path, monkeypatch):
    import src.config as cfg
    from src.data.structured_store import init_structured_schema, replace_kline_series

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "quant_kline.duckdb")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kfc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kfc, "KLINE_CACHE_ENABLED", True)
    monkeypatch.setattr(kfc, "KLINE_CACHE_TTL_SECONDS", -1)

    init_structured_schema()
    df = pd.DataFrame(
        [{"date": "2020-01-01", "open": 1, "close": 2, "high": 2, "low": 1, "volume": 1}]
    )
    replace_kline_series(
        "000001", "240", 10, df, cached_at_iso="2000-01-01T00:00:00+08:00"
    )

    assert kfc.try_read_cache("000001", "240", 10, allow_stale=False) is None
    st = kfc.try_read_cache("000001", "240", 10, allow_stale=True)
    assert st is not None and len(st) == 1


def test_replace_kline_series_twice_same_key(tmp_path, monkeypatch):
    """复现 600396/240/12 反复写入：不得触发 DuckDB 二级索引批量 DELETE 致命错误。"""
    import src.config as cfg
    from src.data.structured_store import init_structured_schema, replace_kline_series

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "kline_twice.duckdb")
    init_structured_schema()
    dates = [
        "2026-04-29", "2026-04-30", "2026-05-06", "2026-05-07", "2026-05-08",
        "2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15",
        "2026-05-18", "2026-05-19",
    ]
    df = pd.DataFrame({
        "date": dates,
        "open": [1.0] * 12,
        "high": [1.1] * 12,
        "low": [0.9] * 12,
        "close": [1.05] * 12,
        "volume": [100.0] * 12,
    })
    for _ in range(3):
        replace_kline_series("600396", "240", 12, df, cached_at_iso="2026-05-19T12:00:00+08:00")
    df2 = df.iloc[:8].copy()
    replace_kline_series("600396", "240", 12, df2, cached_at_iso="2026-05-19T13:00:00+08:00")
    got = kfc.try_read_cache("600396", "240", 12, allow_stale=True)
    assert got is not None and len(got) == 8

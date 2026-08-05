import pandas as pd

from src.data import stock_search as ss


def test_search_stocks_prefix_code(monkeypatch):
    df = pd.DataFrame({
        "code": ["000001", "000002", "600519"],
        "name": ["平安银行", "万科A", "贵州茅台"],
    })
    monkeypatch.setattr(ss, "get_search_universe", lambda: df)
    r = ss.search_stocks("000", limit=10)
    assert len(r) == 2
    assert r[0]["code"] == "000001"


def test_search_stocks_name(monkeypatch):
    df = pd.DataFrame({"code": ["600519"], "name": ["贵州茅台"]})
    monkeypatch.setattr(ss, "get_search_universe", lambda: df)
    r = ss.search_stocks("茅台", limit=5)
    assert len(r) == 1
    assert r[0]["code"] == "600519"


def test_search_stocks_suffix_without_leading_zeros(monkeypatch):
    df = pd.DataFrame({"code": ["002918", "600519"], "name": ["蒙娜丽莎", "贵州茅台"]})
    monkeypatch.setattr(ss, "get_search_universe", lambda: df)
    r = ss.search_stocks("2918", limit=5)
    assert len(r) == 1
    assert r[0]["code"] == "002918"


def test_search_stocks_prefix_partial(monkeypatch):
    df = pd.DataFrame({"code": ["600519", "600036"], "name": ["贵州茅台", "招商银行"]})
    monkeypatch.setattr(ss, "get_search_universe", lambda: df)
    r = ss.search_stocks("6005", limit=10)
    assert len(r) >= 1
    assert r[0]["code"] == "600519"

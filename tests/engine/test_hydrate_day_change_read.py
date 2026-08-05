"""读侧补 day_change：有收盘价无涨幅时仍能展示收盘涨幅。"""
from src.engine.screener_history import (
    _hydrate_day_change_from_stored_close,
    hydrate_settled_close_on_read,
)


def test_hydrate_from_stored_close():
    r = {
        "date": "2026-05-25",
        "code": "002442",
        "open_price": 8.1,
        "auction_gain": 5.92,
        "pre_close": 0,
        "close_price": 7.85,
        "day_change": None,
        "status": "closed",
    }
    assert _hydrate_day_change_from_stored_close(r)
    assert r["day_change"] is not None
    assert r["pre_close"] > 0


def test_hydrate_on_read_skips_intraday_today(monkeypatch):
    monkeypatch.setattr(
        "src.engine.screener_history._pick_date_market_settled",
        lambda _d: False,
    )
    r = {"date": "2026-05-25", "code": "002442", "close_price": 7.85, "day_change": None}
    hydrate_settled_close_on_read([r], allow_kline_for_today=False)
    assert r["day_change"] is None

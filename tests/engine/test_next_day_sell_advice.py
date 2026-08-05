"""次日卖出建议规则单测。"""
from src.engine.next_day_sell_advice import classify_yesterday_close, compute_next_day_sell_advice


def _rec(**kw):
    base = {
        "continuous_limit_up": 2,
        "day_change": 5.0,
        "is_limit_up": False,
        "is_zhaban": False,
        "auction_turnover": 10,
        "next_day_auction_gain": 0.0,
    }
    base.update(kw)
    return base


def test_hard_stop_auction_down():
    r = _rec(is_limit_up=True, next_day_auction_gain=-3.0)
    a = compute_next_day_sell_advice(r)
    assert a["action"] == "开盘卖出"
    assert "硬止损" in a["reason"]


def test_hard_stop_auction_up_9():
    r = _rec(is_limit_up=True, next_day_auction_gain=9.5)
    a = compute_next_day_sell_advice(r)
    assert a["action"] == "开盘卖出"
    assert "获利" in a["reason"]


def test_limit_up_hold_mid_range():
    r = _rec(is_limit_up=True, day_change=10, next_day_auction_gain=4.5)
    a = compute_next_day_sell_advice(r)
    assert a["action"] == "持有并设回撤止盈"


def test_zhaban_sell():
    r = _rec(is_zhaban=True, is_limit_up=False, next_day_auction_gain=-1.2)
    a = compute_next_day_sell_advice(r)
    assert a["action"] == "开盘卖出"
    assert "炸板" in a["reason"] or "低开" in a["reason"]


def test_global_limit_down():
    r = _rec(next_day_auction_gain=3.0)
    a = compute_next_day_sell_advice(r, market_limit_down=12)
    assert a["action"] == "竞价卖出"


def test_backfill_only_yesterday_pick_date(monkeypatch):
    from src.engine import next_day_sell_advice as mod

    records = [
        {"date": "2026-05-20", "code": "000001", "next_day_auction_gain": 1.0},
        {"date": "2026-05-25", "code": "600001", "next_day_auction_gain": 2.0},
        {"date": "2026-05-25", "code": "600002", "next_day_auction_gain": -1.0},
    ]

    def _fake_load():
        return [dict(x) for x in records]

    saved = {}

    def _fake_save(rows):
        saved["rows"] = rows

    monkeypatch.setattr(mod, "now_cn", lambda: __import__("datetime").datetime(2026, 5, 26, 9, 30))
    monkeypatch.setattr("src.engine.screener_history._load", _fake_load)
    monkeypatch.setattr("src.engine.screener_history._save", _fake_save)
    monkeypatch.setattr(
        "src.engine.screener_history.yesterday_pick_date",
        lambda today=None: "2026-05-25",
    )

    n = mod.backfill_next_day_sell_advice()
    assert n == 2
    out = {r["code"]: r.get("next_day_sell_advice") for r in saved["rows"]}
    assert out["600001"] and out["600002"]
    assert "next_day_sell_advice" not in saved["rows"][0] or not saved["rows"][0].get("next_day_sell_advice")


def test_classify_zhaban():
    assert classify_yesterday_close({"is_zhaban": True, "day_change": 8}) == "zhaban"

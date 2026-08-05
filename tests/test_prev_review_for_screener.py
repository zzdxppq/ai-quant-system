"""上一交易日复盘加载口径：pick=今日，而非把「上一交易日」当 pick。"""
import pytest

from src.engine.screener_market_env import (
    b1_rate_from_review_document,
    load_prev_trading_day_review_document,
    load_review_document_for_pick_date,
    prev_trading_day_iso_before,
    resolve_b1_with_review_date,
)


def test_prev_review_uses_today_as_pick_date(monkeypatch):
    """load_prev 应等价于 load_review(today)，而非 load_review(上一交易日)。"""
    calls: list[str] = []

    def _fake_load(pick_date: str):
        calls.append(pick_date)
        return {"date": prev_trading_day_iso_before(pick_date), "prev_board_groups": []}

    monkeypatch.setattr(
        "src.engine.screener_market_env.load_review_document_for_pick_date",
        _fake_load,
    )
    monkeypatch.setattr(
        "src.engine.screener_market_env.now_cn",
        lambda: __import__("datetime").datetime(2026, 5, 25, 10, 0, 0),
    )
    load_prev_trading_day_review_document()
    assert calls == ["2026-05-25"]


def test_b1_from_prev_board_groups():
    review = {
        "date": "2026-05-22",
        "prev_board_groups": [
            {"prev_board": 1, "promoted": [{}] * 2, "failed": [{}] * 10},
        ],
    }
    assert b1_rate_from_review_document(review) == 16.7


def test_load_review_skips_empty_exact_match(monkeypatch):
    """上一交易日有复盘壳但无 1进2 数据时，应回退到更早的有效记录。"""
    hist = [
        {"date": "2026-05-19", "prev_board_groups": [{"prev_board": 1, "promoted": [{}] * 2, "failed": [{}] * 8}]},
        {"date": "2026-05-22", "scorecard": {"indicators": []}},
    ]
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_review_history_document",
        lambda: hist,
    )
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_latest_review_document",
        lambda: {},
    )
    doc = load_review_document_for_pick_date("2026-05-25")
    assert str(doc.get("date") or "")[:10] == "2026-05-19"
    assert b1_rate_from_review_document(doc) == pytest.approx(20.0, abs=0.1)


def test_resolve_b1_uses_hint_date_from_history(monkeypatch):
    hist = [
        {
            "date": "2026-05-19",
            "prev_board_groups": [{"prev_board": 1, "promoted": [{}] * 3, "failed": [{}] * 7}],
        },
    ]
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_review_history_document",
        lambda: hist,
    )
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_latest_review_document",
        lambda: {},
    )
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_review_document_for_pick_date",
        lambda _pick: {},
    )
    b1, d = resolve_b1_with_review_date(hint_date="2026-05-19")
    assert b1 == pytest.approx(30.0, abs=0.1)
    assert d == "2026-05-19"

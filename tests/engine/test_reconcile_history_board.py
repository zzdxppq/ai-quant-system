"""连板数校正：归档递推错误时用 limit_up_cache lbc 修正 history。"""
from unittest.mock import patch

from src.engine import screener_history


def test_reconcile_history_board_from_limit_up_cache(monkeypatch):
    records = [
        {
            "date": "2026-05-25",
            "code": "002442",
            "name": "龙星科技",
            "continuous_limit_up": 2,
            "board_label": "2进3",
            "auction_gain": 5.92,
            "decision": {"ladder_label": "2进3", "action": "开仓"},
        },
    ]
    cache = {
        "20260522": [
            {"code": "002442", "name": "龙星科技", "continuous_limit_up": 3, "board_count": 3},
        ],
    }

    monkeypatch.setattr(screener_history, "_load", lambda: records)
    saved = []

    def fake_save(recs):
        saved.append(list(recs))

    monkeypatch.setattr(screener_history, "_save", fake_save)
    monkeypatch.setattr(
        screener_history,
        "load_json_file",
        lambda path: cache if "limit_up_cache" in str(path) else None,
    )
    monkeypatch.setattr(screener_history, "_recompute_record_decision", lambda r, d: True)
    monkeypatch.setattr(
        "src.data.analytics_store.backfill_daily_screener_hit_from_history",
        lambda **k: 0,
    )

    with patch("src.engine.daily_review._yesterday_cache_key", return_value="20260522"):
        with patch("src.engine.daily_review._board_count_walk", return_value=3):
            n = screener_history.reconcile_history_board_counts(trade_dates=["2026-05-25"])

    assert n == 1
    assert saved[0][0]["continuous_limit_up"] == 3
    assert saved[0][0]["board_label"] == "3进4"

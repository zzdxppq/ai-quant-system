"""历史选股 decision 列：按选股日 v4 + 同日最高连板档重算。"""
from src.engine.screener_history import (
    _decision_needs_v4_recompute,
    _recompute_record_decision,
    _tier_ctx_by_pick_date,
    recompute_history_decisions_v4,
)


def test_tier_ctx_single_3_board():
    records = [
        {"date": "2026-05-25", "code": "002442", "name": "龙星科技", "continuous_limit_up": 3, "auction_gain": 5.92},
        {"date": "2026-05-25", "code": "600303", "name": "曙光股份", "continuous_limit_up": 2, "auction_gain": 4.44},
    ]
    tier = _tier_ctx_by_pick_date(records)["2026-05-25"]
    assert tier["count"] == 1
    assert tier["avg_today_pct"] == 5.92


def test_stale_v4_with_missing_b1_triggers_recompute():
    r = {
        "continuous_limit_up": 3,
        "decision": {
            "rules_version": "v4.0",
            "ladder_label": "3进4",
            "can_open": False,
            "reason": "条件未达：昨日1进2晋级率缺失；板块集中度未知，概念涨停 0 只 <2",
            "b1_rate": None,
        },
    }
    assert _decision_needs_v4_recompute(r)


def test_recompute_442_opens(monkeypatch):
    review = {
        "date": "2026-05-19",
        "prev_board_groups": [{"prev_board": 1, "promoted": [{}] * 2, "failed": [{}] * 10}],
        "concept_zt_stats": [{"name": "专精特新", "limit_up_count": 5}],
        "highest_board": 4,
    }
    monkeypatch.setattr(
        "src.engine.screener_market_env.load_review_document_for_pick_date",
        lambda _d: review,
    )
    monkeypatch.setattr(
        "src.engine.screener_market_env.review_context_for_pick_date",
        lambda pick_date, tier_ctx=None: {
            "concept_zt_stats": review["concept_zt_stats"],
            "space_board_today": None,
            "highest_board_tier_today": tier_ctx,
            "market_highest_board": 4,
        },
    )
    records = [
        {"date": "2026-05-25", "code": "002442", "name": "龙星科技", "continuous_limit_up": 3,
         "auction_gain": 5.92, "auction_turnover": 7.28, "top_concepts": ["专精特新"]},
        {"date": "2026-05-25", "code": "600303", "name": "曙光股份", "continuous_limit_up": 2,
         "auction_gain": 4.44, "auction_turnover": 21.0},
    ]
    tier = _tier_ctx_by_pick_date(records)["2026-05-25"]
    r = records[0]
    assert _recompute_record_decision(r, "2026-05-25", tier_ctx=tier)
    assert r["decision"]["can_open"] is True
    assert "30%" in (r["decision"]["position_text"] or "")

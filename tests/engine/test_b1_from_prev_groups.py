"""1进2 成功率须与晋级矩阵一致，不能只用落盘 scorecard。"""
import pytest

from src.engine.screener_market_env import b1_rate_from_review_document


def test_b1_prefers_prev_board_groups_over_stale_scorecard():
    doc = {
        "date": "2026-05-19",
        "scorecard": {
            "indicators": [{"label": "1进2成功率", "raw": 26.2, "detail": "old"}],
        },
        "prev_board_groups": [
            {
                "prev_board": 1,
                "promoted": [{"code": f"{i:06d}"} for i in range(14)],
                "failed": [{"code": f"{j:06d}"} for j in range(53)],
            },
        ],
    }
    assert b1_rate_from_review_document(doc) == pytest.approx(20.9, abs=0.1)


def test_b1_falls_back_to_scorecard_when_no_groups():
    doc = {
        "scorecard": {"indicators": [{"label": "1进2成功率", "raw": 16.2}]},
    }
    assert b1_rate_from_review_document(doc) == pytest.approx(16.2)

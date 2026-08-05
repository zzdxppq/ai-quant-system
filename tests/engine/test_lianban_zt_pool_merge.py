"""连板梯队须合并 zt_pool，避免东财涨幅榜筛涨停漏掉高连板空间板。"""
from unittest.mock import patch

from src.engine.daily_review import _get_lianban_ladder


def test_lianban_includes_zt_pool_only_high_board(monkeypatch):
    """cache 仅有 3 板标的时，zt_pool 中的 4 板应进入梯队并成为最高板。"""
    cache = {
        "20260525": [
            {"code": "603661", "name": "恒林股份", "change_pct": 9.99},
        ],
    }
    zt_pool = {
        "603661": {"name": "恒林股份", "lbc": 3, "lbt": "14:55:00"},
        "000518": {"name": "四环生物", "lbc": 4, "lbt": "09:35:00"},
    }

    monkeypatch.setattr(
        "src.engine.daily_review.load_json_file",
        lambda _path: cache if "limit_up" in str(_path) else {},
    )
    monkeypatch.setattr(
        "src.engine.daily_review._build_spot_map_for_codes",
        lambda _codes: {
            "603661": {"code": "603661", "change_pct": 9.99, "close": 10.0},
            "000518": {"code": "000518", "change_pct": 10.0, "close": 5.0},
        },
    )

    with patch(
        "src.data.zt_pool_api.fetch_zt_pool_with_retry",
        return_value=zt_pool,
    ):
        ladder = _get_lianban_ladder(session_today_key="20260525")

    assert ladder, "梯队不应为空"
    top = max(ladder, key=lambda x: x["board_count"])
    assert top["code"] == "000518"
    assert top["board_count"] == 4
    assert top["name"] == "四环生物"

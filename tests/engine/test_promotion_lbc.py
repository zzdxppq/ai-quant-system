"""连板晋级与同花顺口径：涨停池 lbc + 今日连板≥昨日+1。"""
import pytest

from src.engine.daily_review import (
    _build_prev_board_groups,
    _is_promoted_board,
    _merge_today_limit_up_codes,
    _prev_board_for_promotion,
    _today_board_for_promotion,
    _zt_pool_lbc,
)


def test_zt_pool_lbc():
    pool = {"001211": {"lbc": 2, "name": "利仁"}}
    assert _zt_pool_lbc(pool, "001211") == 2


def test_is_promoted_board_strict():
    assert _is_promoted_board(1, 2, in_today_limit_up=True) is True
    assert _is_promoted_board(2, 3, in_today_limit_up=True) is True
    assert _is_promoted_board(1, 1, in_today_limit_up=True) is False
    assert _is_promoted_board(1, 2, in_today_limit_up=False) is False


def test_prev_board_prefers_zt_over_walk():
    cache = {
        "20260518": [{"code": "000001", "continuous_limit_up": 2}],
        "20260519": [],
    }
    yesterday_pool = {"000001": {"code": "000001", "name": "A"}}
    zt_y = {"000001": {"lbc": 1, "name": "A"}}
    assert _prev_board_for_promotion(
        "000001", "20260518", yesterday_pool, cache, zt_y,
    ) == 1


def test_today_board_prefers_zt():
    assert _today_board_for_promotion(
        "000001",
        {"000001": {"board_count": 1}},
        {"000001": {"lbc": 2}},
    ) == 2


def test_merge_today_limit_up_codes_includes_zt_pool():
    cache_rows = [{"code": "000001", "name": "A", "change_pct": 10.0}]
    zt_today = {"000518": {"name": "四环生物", "lbc": 4, "lbt": "14:46:48"}}
    codes = _merge_today_limit_up_codes(cache_rows, zt_today)
    assert "000518" in codes
    assert "000001" in codes


def test_promoted_when_zt_pool_but_missing_from_cache(monkeypatch):
    """spot/cache 漏记 9.93% 时，东财涨停池 lbc=4 仍应 3进4 晋级。"""
    cache = {
        "20260522": [
            {"code": "000518", "name": "四环生物", "continuous_limit_up": 3, "change_pct": 10.0},
        ],
        "20260525": [
            {"code": "000001", "name": "其他", "continuous_limit_up": 1, "change_pct": 10.0},
        ],
    }
    zt_y = {"000518": {"lbc": 3, "name": "四环生物"}}
    zt_t = {"000518": {"lbc": 4, "name": "四环生物"}}

    monkeypatch.setattr(
        "src.engine.daily_review.load_json_file",
        lambda path: cache if "limit_up_cache" in str(path) else {},
    )
    monkeypatch.setattr(
        "src.engine.daily_review._build_spot_map_for_codes",
        lambda codes: {
            "000518": {
                "close": 10.0,
                "pre_close": 9.1,
                "open": 9.5,
                "change_pct": 9.93,
            },
        },
    )

    def _fake_zt(d):
        if d == "20260522":
            return zt_y
        if d == "20260525":
            return zt_t
        return {}

    monkeypatch.setattr(
        "src.data.zt_pool_api.fetch_zt_pool_with_retry",
        _fake_zt,
    )

    groups = _build_prev_board_groups([], [], session_today_key="20260525")
    g3 = next((g for g in groups if g.get("prev_board") == 3), None)
    assert g3 is not None
    prom = g3.get("promoted") or []
    assert any(s.get("code") == "000518" for s in prom)
    assert not any(s.get("code") == "000518" for s in (g3.get("failed") or []))
    assert prom[0].get("today_board") == 4

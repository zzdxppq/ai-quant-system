"""次日关注标的池入选规则（主板 + 流通市值≤100亿 + ≥2连板）。"""
from src.engine.daily_review import build_watch_pool_from_ranking


def test_watch_pool_excludes_first_board_high_gain_in_top30(monkeypatch):
    monkeypatch.setattr("src.data.zt_pool_api.fetch_zt_pool", lambda: {})
    ranking = [
        {
            "code": "600001",
            "name": "首板样例",
            "is_main_board": True,
            "gain_10d": 50.0,
            "continuous_limit_up": 1,
            "market_cap_yi": 50,
            "close": 10,
            "industry": "测试",
        },
    ]
    assert build_watch_pool_from_ranking(ranking, lianban_ladder=None) == []


def test_watch_pool_from_lianban_ladder_main_board_small_cap():
    ladder = [
        {
            "code": "600002",
            "name": "二板小盘",
            "board_count": 2,
            "is_main_board": True,
            "market_cap_yi": 100,
            "close_price": 12.3,
            "industry": "火电",
            "concepts": ["风能"],
            "lbt": "09:35:00",
        },
        {
            "code": "300001",
            "name": "创业板",
            "board_count": 3,
            "is_main_board": False,
            "market_cap_yi": 30,
            "close_price": 20,
        },
        {
            "code": "600003",
            "name": "大盘二板",
            "board_count": 2,
            "is_main_board": True,
            "market_cap_yi": 101,
            "close_price": 8,
        },
    ]
    out = build_watch_pool_from_ranking([], lianban_ladder=ladder)
    codes = [x["code"] for x in out]
    assert codes == ["600002"]
    assert out[0]["board_count"] == 2
    assert out[0]["pool_tag"] == "小盘接力"

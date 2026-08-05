"""main_board_lianban_space_auction：最高连板档多只取竞价均值。"""
import pytest

from src.engine import dashboard_decision as dd


def _stub_review(monkeypatch, review: dict) -> None:
    monkeypatch.setattr(dd, "load_latest_review_document", lambda: review)


def test_max_board_single_leader(monkeypatch):
    monkeypatch.setattr(dd, "load_latest_review_document", lambda: {})
    leader = {
        "main_board_leaders": [
            {
                "leader_code": "603986",
                "leader_name": "大普微",
                "board_count": 5,
                "auction_change_pct": -3.0,
                "signal": "负反馈",
            },
            {
                "leader_code": "601991",
                "leader_name": "大唐发电",
                "board_count": 2,
                "auction_change_pct": 6.2,
                "signal": "正反馈",
            },
        ],
    }
    r = dd.main_board_lianban_space_auction(leader)
    assert r["pct"] == pytest.approx(-3.0)
    assert r["source"] == "main_board_lianban"
    assert "大普" in (r.get("name") or "")


def test_tied_max_board_uses_average(monkeypatch):
    monkeypatch.setattr(dd, "load_latest_review_document", lambda: {})
    leader = {
        "main_board_leaders": [
            {
                "leader_code": "000408",
                "leader_name": "四环生物",
                "board_count": 3,
                "auction_change_pct": -6.3,
                "signal": "负反馈",
            },
            {
                "leader_code": "002442",
                "leader_name": "龙星科技",
                "board_count": 3,
                "auction_change_pct": 5.92,
                "signal": "强正反馈",
            },
            {
                "leader_code": "603569",
                "leader_name": "威龙股份",
                "board_count": 3,
                "auction_change_pct": -10.0,
                "signal": "跌停",
            },
            {
                "leader_code": "600303",
                "leader_name": "曙光股份",
                "board_count": 3,
                "auction_change_pct": 4.44,
                "signal": "正反馈",
            },
        ],
    }
    r = dd.main_board_lianban_space_auction(leader)
    assert r["pct"] == pytest.approx((-6.3 + 5.92 - 10.0 + 4.44) / 4, abs=0.01)
    assert r["source"] == "main_board_lianban_avg"
    assert r.get("name") == "4只3板"
    assert r.get("board_count") == 3


def test_dashboard_board_count_uses_yesterday_board(monkeypatch):
    """看板展示板数仍可用 relay 昨连板数覆盖。"""
    review = {
        "date": "2026-05-20",
        "relay_env": {
            "prev_space_board_today": {
                "code": "001259",
                "name": "利仁科技",
                "yesterday_board": 7,
            },
        },
    }
    from src.engine import screener_market_env as sme

    monkeypatch.setattr(
        sme,
        "load_prev_trading_day_review_document",
        lambda: review,
    )
    leader = {
        "main_board_leaders": [
            {
                "leader_code": "001259",
                "leader_name": "利仁科技",
                "board_count": 6,
                "auction_change_pct": 9.9,
                "signal": "强正反馈",
            },
        ],
    }
    r = dd.main_board_lianban_space_auction(leader, for_dashboard=True)
    assert r["board_count"] == 7


def test_refresh_space_auction_tier_codes_average():
    import pandas as pd

    sp = {
        "name": "4只3板",
        "board_count": 3,
        "pct": -6.3,
        "tier_count": 4,
        "tier_codes": ["000408", "002442", "603569", "600303"],
    }
    spot = pd.DataFrame([
        {"code": "000408", "open": 9.37, "pre_close": 10.0},
        {"code": "002442", "open": 10.59, "pre_close": 10.0},
        {"code": "603569", "open": 9.0, "pre_close": 10.0},
        {"code": "600303", "open": 10.44, "pre_close": 10.0},
    ])
    r = dd.refresh_space_auction_from_spot(sp, spot, prefer_spot=True)
    assert r["pct"] == pytest.approx((-6.3 + 5.9 - 10.0 + 4.4) / 4, abs=0.05)
    assert r.get("name") == "4只3板"
    assert "+4.4%" not in (r.get("label") or "")


def test_refresh_space_auction_overrides_stale_leader_pct():
    import pandas as pd

    sp = {
        "code": "001259",
        "name": "利仁科技",
        "pct": 9.9,
        "label": "强(+9.9%)",
        "board_count": 7,
    }
    spot = pd.DataFrame(
        [{"code": "001259", "open": 10.5, "pre_close": 10.0}],
    )
    r = dd.refresh_space_auction_from_spot(sp, spot, prefer_spot=True)
    assert r["pct"] == pytest.approx(5.0)
    assert "+5.0%" in (r.get("label") or "")


def test_max_board_when_no_review_file(monkeypatch):
    monkeypatch.setattr(dd, "load_latest_review_document", lambda: {})
    leader = {
        "main_board_leaders": [
            {
                "leader_code": "603986",
                "leader_name": "大普微",
                "board_count": 5,
                "auction_change_pct": -3.0,
                "signal": "负反馈",
            },
        ],
    }
    r = dd.main_board_lianban_space_auction(leader)
    assert r["pct"] == pytest.approx(-3.0)

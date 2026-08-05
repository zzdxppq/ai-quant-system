"""复盘 API 同步：scorecard 与 prev_board_groups 一致。"""
import pytest

from src.engine.daily_review import (
    _build_scorecard,
    apply_prev_space_board_patch,
    sync_review_payload_for_api,
)


def test_sync_scorecard_from_groups():
    doc = {
        "date": "2026-05-19",
        "limit_up_count": 80,
        "highest_board": 7,
        "scorecard": {
            "indicators": [{"label": "1进2成功率", "raw": 26.2, "detail": "16/61"}],
        },
        "prev_board_groups": [
            {
                "prev_board": 1,
                "next_board": 2,
                "promoted": [{"code": f"{i:06d}"} for i in range(14)],
                "failed": [{"code": f"{j:06d}"} for j in range(53)],
            },
        ],
        "relay_env": {},
        "sector_zt_stats": [],
        "concept_zt_stats": [],
    }
    out = sync_review_payload_for_api(doc)
    assert out.get("_scorecard_synced") is True
    ind = next(
        i for i in (out["scorecard"].get("indicators") or []) if i.get("label") == "1进2成功率"
    )
    assert ind.get("raw") == pytest.approx(20.9, abs=0.1)
    assert ind.get("detail") == "14/67"


def test_sync_hydrates_limit_up_count_from_cache(monkeypatch):
    """落库 limit_up_count=8 时，API 同步应从 limit_up_cache 刷新为实际条数。"""
    cache_rows = [{"code": f"{i:06d}", "name": f"n{i}", "change_pct": 10.0} for i in range(115)]
    ladder = [{"code": "000001", "name": "A", "board_count": 3, "industry": "X"}]

    monkeypatch.setattr(
        "src.engine.daily_review.load_json_file",
        lambda _path: {"20260522": cache_rows},
    )
    monkeypatch.setattr(
        "src.engine.daily_review._get_lianban_ladder",
        lambda **_: ladder,
    )
    monkeypatch.setattr(
        "src.engine.daily_review.rebuild_prev_board_groups_for_date",
        lambda _date: [{"prev_board": 1, "promoted": [{"code": "000001"}], "failed": []}],
    )
    monkeypatch.setattr(
        "src.engine.daily_review._build_sector_zt_stats",
        lambda _lb: [{"industry": "X", "count": 1}],
    )
    monkeypatch.setattr(
        "src.engine.daily_review._build_concept_zt_stats",
        lambda _lb: [{"name": "题材", "limit_up_count": 3}],
    )

    doc = {
        "date": "2026-05-22",
        "limit_up_count": 8,
        "highest_board": 2,
        "lianban_ladder": [],
        "prev_board_groups": [],
        "relay_env": {},
        "sector_zt_stats": [],
        "concept_zt_stats": [],
    }
    out = sync_review_payload_for_api(doc)
    assert out.get("_limit_up_cache_hydrated") is True
    assert out["limit_up_count"] == 115
    assert out["highest_board"] == 3
    assert out["lianban_ladder"] == ladder


def test_sync_prefers_rebuilt_groups_over_stale_blob(monkeypatch):
    """落库 16/61 时，API 同步应优先用 rebuild 矩阵，而非只重算旧 groups 上的 scorecard。"""
    stale = {
        "prev_board": 1,
        "next_board": 2,
        "promoted": [{"code": f"{i:06d}"} for i in range(16)],
        "failed": [{"code": f"{j:06d}"} for j in range(45)],
    }
    fresh = {
        "prev_board": 1,
        "next_board": 2,
        "promoted": [{"code": f"{i:06d}"} for i in range(14)],
        "failed": [{"code": f"{j:06d}"} for j in range(57)],
    }

    def _fake_rebuild(_date: str):
        return [fresh, {"prev_board": 2, "promoted": [{"code": "000001"}], "failed": [{"code": "000002"}] * 4}]

    monkeypatch.setattr(
        "src.engine.daily_review.rebuild_prev_board_groups_for_date",
        _fake_rebuild,
    )
    doc = {
        "date": "2026-05-19",
        "limit_up_count": 80,
        "highest_board": 7,
        "scorecard": {"indicators": [{"label": "1进2成功率", "raw": 26.2, "detail": "16/61"}]},
        "prev_board_groups": [stale],
        "relay_env": {},
        "sector_zt_stats": [],
        "concept_zt_stats": [],
    }
    out = sync_review_payload_for_api(doc)
    assert out.get("_prev_board_groups_rebuilt") is True
    ind = next(
        i for i in (out["scorecard"].get("indicators") or []) if i.get("label") == "1进2成功率"
    )
    assert ind.get("detail") == "14/71"
    assert ind.get("raw") == pytest.approx(19.7, abs=0.1)


def test_height_breakthrough_uses_yesterday_board_8_when_today_7():
    """昨空间板 8 板、今日最高 7 板（8进9 失败）→ 高度突破为 ↓1 可接受，非 7=昨7。"""
    groups = [
        {
            "prev_board": 8,
            "next_board": 9,
            "promoted": [],
            "failed": [{"code": "001259", "name": "利仁科技", "today_pct": -5.0, "today_close": 10.0}],
        },
    ]
    relay = {
        "prev_space_board_today": {
            "code": "001259",
            "name": "利仁科技",
            "yesterday_board": 7,
            "today_held": False,
            "today_open_pct": 2.0,
        },
    }
    patched = apply_prev_space_board_patch(
        {"relay_env": relay},
        {"yesterday_board": 8, "today_held": False, "today_pct": -5.0},
    )
    relay2 = patched["relay_env"]
    sc = _build_scorecard(groups, relay2, [], 30, highest_board=7)
    height = next(i for i in sc["indicators"] if i["label"] == "高度突破")
    assert "昨8板" in height["today"]
    assert "↓1" in height["today"] or "可接受" in height["today"]
    assert height["score"] == 1.0
    space = next(i for i in sc["indicators"] if i["label"] == "空间板")
    assert space["score"] == 0.0
    assert "断板" in space["today"]

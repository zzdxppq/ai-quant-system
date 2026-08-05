"""0 命中选股应落库并读出空列表，不得回退到上一交易日 daily_screener_hit。"""
from src.data.analytics_store import (
    _save_screener,
    load_migrated_snapshot,
    save_from_latest_filename,
)
from src.data.json_io import dump_json_file, load_json_file
from src.config import DATA_DIR


def _seed_yesterday_hits(ymd: str) -> None:
    save_from_latest_filename(
        "latest_screener.json",
        {
            "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]} 09:27:00",
            "hits": [
                {
                    "code": "002442",
                    "name": "龙星科技",
                    "continuous_limit_up": 3,
                    "auction_gain": 5.92,
                },
            ],
        },
    )


def test_zero_hits_today_overrides_yesterday_table(tmp_path, monkeypatch):
    _seed_yesterday_hits("20260525")
    today = {
        "date": "2026-05-26 10:13:34",
        "hits": [],
        "status": "ok",
    }
    dump_json_file(DATA_DIR / "latest_screener.json", today)

    loaded = load_json_file(DATA_DIR / "latest_screener.json")
    assert loaded is not None
    assert loaded.get("hits") == []
    assert "2026-05-26" in str(loaded.get("date") or "")

    via_migrate = load_migrated_snapshot("latest_screener.json")
    assert via_migrate is not None
    assert via_migrate.get("hits") == []


def test_save_screener_clears_hit_rows_for_empty_day():
    save_from_latest_filename(
        "latest_screener.json",
        {
            "date": "2026-05-26 09:27:00",
            "hits": [{"code": "600303", "name": "曙光股份", "continuous_limit_up": 2}],
        },
    )
    save_from_latest_filename(
        "latest_screener.json",
        {"date": "2026-05-26 10:13:34", "hits": [], "status": "ok"},
    )
    got = load_migrated_snapshot("latest_screener.json")
    assert got is not None
    assert got.get("hits") == []

"""盘后 18:00 单次串行 eod_bundle 行为测试。"""
from __future__ import annotations

import src.scheduler as sched


def test_eod_bundle_skips_non_trading_day(monkeypatch):
    monkeypatch.setattr(
        sched,
        "now_cn",
        lambda: __import__("datetime").datetime(2026, 5, 18, 18, 0, 0),
    )
    monkeypatch.setattr("src.config.is_trading_day", lambda: False)

    out = sched.run_eod_bundle()
    assert out["skipped"] == "non-trading day"
    assert out["ok"] is True
    assert "trade_date" in out and "finished_at" in out

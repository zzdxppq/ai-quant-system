"""决策卡读侧：旧版四维文案 → v2 dashboard 重建。"""
from src.data.analytics_store import (
    _apply_v2_decision_to_advice_out,
    _hydrate_advice_dashboard_on_read,
    _is_legacy_advice_reason,
)


def test_legacy_reason_detect():
    assert _is_legacy_advice_reason("四维警戒中已 2 项触发，避免开仓。")
    assert _is_legacy_advice_reason("梯队加权竞价 -1.1% 偏弱（<0）；昨日连板高标 四环生物(水下-6.3%)")
    assert not _is_legacy_advice_reason("加权接力情绪指数 1.2%（≤0%），触发空仓条件。")


def test_apply_v2_decision_overwrites_legacy():
    out = {
        "bucket": "stop",
        "text": "🛑 今日不操作",
        "reason": "梯队加权竞价 -1.1% 偏弱；四维警戒中已 2 项触发",
    }
    dash = {
        "decision": {
            "bucket": "go",
            "headline": "🟢 正常参与",
            "tagline": "加权接力 1.5%；1进2 16.9% → 3进4+",
            "conclusion": "🟢 正常仓位（3层）",
            "position": "3 层（标准仓位）",
            "position_short": "3层",
        },
        "participate": {"b1_rate": 16.9},
    }
    _apply_v2_decision_to_advice_out(out, dash)
    assert out["bucket"] == "go"
    assert "四维警戒" not in out["reason"]
    assert out["text"] == "🟢 正常参与"


def test_repair_fills_b1_when_db_has_date_only(monkeypatch):
    """落盘 b1_rate=null 但 b1_review_date 有值时，读侧应从 history 补 1进2。"""
    from src.data.analytics_store import _repair_advice_dashboard_for_read

    monkeypatch.setattr(
        "src.engine.screener_market_env.resolve_b1_with_review_date",
        lambda hint_date=None: (18.5, "2026-05-19"),
    )
    dash = {
        "participate": {"b1_rate": None, "b1_review_date": "2026-05-22"},
        "reference": {},
    }
    _repair_advice_dashboard_for_read(dash, {})
    assert dash["participate"]["b1_rate"] == 18.5
    assert dash["participate"]["b1_review_date"] == "2026-05-19"


def test_hydrate_rebuilds_when_no_dashboard(monkeypatch):
    rebuilt = {
        "participate": {"b1_rate": 16.9, "b1_review_date": "2026-05-22"},
        "reference": {},
        "decision": {
            "bucket": "go",
            "headline": "🟢 正常参与",
            "tagline": "v2 tagline",
            "position": "3 层",
            "position_short": "3层",
        },
    }
    monkeypatch.setattr(
        "src.data.analytics_store._rebuild_advice_dashboard_v2",
        lambda **_: rebuilt,
    )
    monkeypatch.setattr(
        "src.data.analytics_store._repair_advice_dashboard_for_read",
        lambda dash, inputs: dash,
    )
    out = {
        "reason": "梯队加权竞价 -1.1% 偏弱；四维警戒",
        "text": "🛑 今日不操作",
        "bucket": "stop",
    }
    _hydrate_advice_dashboard_on_read(out, None, {})
    assert out["dashboard"]["participate"]["b1_rate"] == 16.9
    assert out["bucket"] == "go"
    assert out["reason"] == "v2 tagline"

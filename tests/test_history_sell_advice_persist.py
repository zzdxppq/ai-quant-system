"""screener_history decision_json 持久化 next_day_sell_advice。"""
from src.data.analytics_store import _pack_history_decision_json, _unpack_history_decision_json


def test_pack_unpack_sell_advice_roundtrip():
    raw = _pack_history_decision_json({
        "decision": {"action": "hold"},
        "next_day_sell_advice": {"summary": "昨涨停 今竞价+1.4% 观察5分钟", "tone": "hold"},
    })
    dec, sell = _unpack_history_decision_json(raw)
    assert dec == {"action": "hold"}
    assert sell["tone"] == "hold"


def test_unpack_legacy_decision_only():
    import json

    raw = json.dumps({"action": "sell"}, ensure_ascii=False)
    dec, sell = _unpack_history_decision_json(raw)
    assert dec == {"action": "sell"}
    assert sell is None

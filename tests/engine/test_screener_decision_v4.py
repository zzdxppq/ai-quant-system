"""个股决策 v4.0 规则单测（2进3 三档 + 3/4进 30/40）。"""
from src.engine.screener_decision import (
    RULES_VERSION,
    LAYER_1_PCT,
    LAYER_2_PCT,
    LAYER_3_PCT,
    LAYER_4_PCT,
    compute_per_stock_decision,
    layers_from_position_pct,
)


def _env(b1=13.0, conc=35.0, limit_down=0, space_red=True):
    return {
        "b1_rate": b1,
        "concentration": conc,
        "market_limit_down": limit_down,
        "space_red": space_red,
    }


def _hit(board=2, auction_gain=6.0, auction_turnover=0.8, **kw):
    base = {
        "continuous_limit_up": board,
        "auction_gain": auction_gain,
        "auction_turnover": auction_turnover,
        "prev_day_yizi": True,
    }
    base.update(kw)
    return base


def test_rules_version():
    psd = compute_per_stock_decision(_hit(), _env())
    assert psd["rules_version"] == RULES_VERSION == "v4.0"


def test_1to2_veto():
    psd = compute_per_stock_decision(_hit(board=1), _env())
    assert not psd["can_open"]
    assert psd["veto_reason"] == "no_1to2"


def test_2to3_tier3_focus():
    """最佳窗口 + 高标红盘 + 质量指标 → 3层。"""
    psd = compute_per_stock_decision(
        _hit(
            board=2, auction_gain=6.0, auction_turnover=0.6,
            prev_day_yizi=False, prev_day_turnover=6.0, prev_amount_ratio=1.1,
        ),
        _env(b1=13.0, conc=35.0, space_red=True),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_3_PCT
    assert "3层" in psd["position_text"]
    assert psd.get("tier_tag") == "重点参与"


def test_2to3_tier2_combo_a():
    """晋级12~15 + 中等质量 → 正常参与 3层。"""
    psd = compute_per_stock_decision(
        _hit(
            board=2, auction_gain=5.5, auction_turnover=0.45,
            prev_day_yizi=False, prev_day_turnover=4.5, prev_amount_ratio=0.95,
        ),
        _env(b1=13.0, space_red=False),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_3_PCT
    assert "3层" in psd["position_text"]
    assert psd.get("tier_tag") == "正常参与"


def test_2to3_tier1_edge_green():
    """晋级12~15 + 高标绿盘 + 硬门槛刚过 → 轻仓试错 2层。"""
    psd = compute_per_stock_decision(
        _hit(
            board=2, auction_gain=4.2, auction_turnover=0.35,
            prev_day_yizi=False, prev_day_turnover=3.2, prev_amount_ratio=0.85,
        ),
        _env(b1=13.0, space_red=False),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_2_PCT
    assert "2层" in psd["position_text"]
    assert psd.get("tier_tag") == "轻仓试错"


def test_2to3_edge_ticket_par_074():
    """额比 0.74（差 0.06）+ 其余硬门槛过 → 边缘票 2层。"""
    psd = compute_per_stock_decision(
        _hit(
            board=2, auction_gain=4.87, auction_turnover=1.08,
            prev_day_yizi=False, prev_day_turnover=7.03, prev_amount_ratio=0.74,
        ),
        _env(b1=13.8, conc=48.0, space_red=True),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_2_PCT
    assert psd.get("tier_tag") == "边缘票"
    assert "2层" in psd["position_text"]


def test_2to3_edge_ticket_par_rounds_to_074():
    """展示值 0.74 的浮点残留（0.738）按两位小数仍算边缘票。"""
    psd = compute_per_stock_decision(
        _hit(
            board=2, auction_gain=4.87, auction_turnover=1.08,
            prev_day_yizi=False, prev_day_turnover=7.03, prev_amount_ratio=0.738,
        ),
        _env(b1=13.8, space_red=False),
    )
    assert psd["can_open"]
    assert psd.get("tier_tag") == "边缘票"
    assert psd["position_pct"] == LAYER_2_PCT


def test_2to3_par_below_edge_veto():
    """额比 <0.74 → 仍空仓。"""
    psd = compute_per_stock_decision(
        _hit(
            board=2, auction_gain=4.87, auction_turnover=1.08,
            prev_day_yizi=False, prev_day_turnover=7.03, prev_amount_ratio=0.73,
        ),
        _env(b1=13.8, space_red=False),
    )
    assert not psd["can_open"]
    assert "0.74" in psd["reason"] or "额比" in psd["reason"]


def test_2to3_auction_below_4_veto():
    psd = compute_per_stock_decision(_hit(board=2, auction_gain=3.5), _env(b1=13.0))
    assert not psd["can_open"]
    assert "4.0~7.5" in psd["reason"] or "不在" in psd["reason"]


def test_2to3_b1_below_8_veto():
    psd = compute_per_stock_decision(_hit(board=2), _env(b1=7.0))
    assert not psd["can_open"]


def test_2to3_green_and_b1_below_12_veto():
    psd = compute_per_stock_decision(
        _hit(board=2, auction_gain=5.0, auction_turnover=0.5),
        _env(b1=10.0, space_red=False),
    )
    assert not psd["can_open"]
    assert "双重弱势" in psd["reason"] or "绿盘" in psd["reason"]


def test_2to3_low_turnover_veto():
    psd = compute_per_stock_decision(
        _hit(board=2, auction_turnover=0.2),
        _env(b1=13.0),
    )
    assert not psd["can_open"]
    assert "0.3%" in psd["reason"]


def test_2to3_yizi_skips_prev_vol():
    """一字板豁免昨换手/额比，仍可按竞价+晋级率分档。"""
    psd = compute_per_stock_decision(
        _hit(
            board=2, auction_gain=6.0, auction_turnover=0.6,
            prev_day_yizi=True,
        ),
        _env(b1=13.0, conc=35.0, space_red=True),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_3_PCT


def test_3to4_base_30_layer():
    psd = compute_per_stock_decision(
        _hit(board=3, auction_gain=5.2), _env(b1=11.0, conc=30.0),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_3_PCT
    assert not psd.get("upgraded_4layer")


def test_3to4_upgrade_to_40_layer():
    psd = compute_per_stock_decision(_hit(board=3, auction_gain=6.5), _env(b1=13.0))
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_4_PCT
    assert psd.get("upgraded_4layer")
    assert "升4层" in psd["position_text"]


def test_4to5_upgrade_concentration():
    psd = compute_per_stock_decision(
        _hit(board=4, auction_gain=5.5), _env(b1=13.0, conc=42.0),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_4_PCT
    assert psd.get("upgraded_4layer")


def test_high_board_gate():
    psd = compute_per_stock_decision(_hit(board=5, auction_gain=7.0), _env(b1=16.0))
    assert not psd["can_open"]
    assert psd["veto_reason"] == "high_board_gate"


def test_limit_down_halves_3_layer_to_15():
    psd = compute_per_stock_decision(
        _hit(board=3, auction_gain=5.2), _env(b1=11.0, limit_down=9),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == 15
    assert psd["halved"]


def test_3to4_uses_tier_avg_not_single_space_limit_down():
    """最高 3 板多只：单只跌停不应误杀，均竞价未跌停区则可通过。"""
    tier_avg = (-6.3 + 5.92 - 10.0 + 4.44) / 4
    psd = compute_per_stock_decision(
        _hit(board=3, auction_gain=5.92),
        _env(b1=13.0),
        highest_board_tier_today={
            "yesterday_board": 3,
            "count": 4,
            "avg_today_pct": tier_avg,
        },
        space_board_today={
            "name": "威龙股份",
            "today_pct": -10.0,
        },
    )
    assert psd["can_open"]
    assert "威龙" not in psd.get("reason", "")


def test_1to2_fallback_opens_light_trial():
    psd = compute_per_stock_decision(
        _hit(board=1, auction_gain=5.0, fallback_1to2=True),
        _env(b1=13.0),
    )
    assert psd["can_open"]
    assert psd["position_pct"] == LAYER_2_PCT
    assert psd.get("tier_tag") == "轻仓试错"
    assert psd.get("forced_light_trial")


def test_apply_single_hit_light_trial():
    from src.engine.screener_decision import apply_single_hit_light_trial

    hits = [
        {
            "code": "003001",
            "continuous_limit_up": 2,
            "auction_gain": 4.87,
            "auction_turnover": 1.08,
            "per_stock_decision": {
                "can_open": False,
                "position_pct": 0,
                "reason": "条件未达：额比不足",
            },
        }
    ]
    assert apply_single_hit_light_trial(hits, _env())
    assert hits[0]["per_stock_decision"]["can_open"]
    assert hits[0]["per_stock_decision"]["position_pct"] == LAYER_2_PCT


def test_layers_from_position_pct_avg():
    assert layers_from_position_pct(20) == "2层"
    assert layers_from_position_pct(15) == "1.5层"
    assert layers_from_position_pct(25) == "2.5层"

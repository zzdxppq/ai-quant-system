"""Test for Story email-sync-1.1: 邮件推送内容逐字段对齐首页看板

Test Design: docs/qa/assessments/email-sync-1.1-test-design-20260508.md
Truth source: src/static/index.html:1196-1268 (dailyAdvice) + :505-600 (指标格)

Run:
    pytest tests/notify/test_email_decision_alignment.py -v
"""
import inspect
import re
from pathlib import Path

import pytest

from src.notify import email_sender
from src.notify.email_sender import (
    _build_html,
    _calc_daily_advice,
    send_screener_report,
)


# ============================================================
# Helpers
# ============================================================

def _sent(*, limit_down=None, drop_over_9pct=None, w_avg=None,
          prev_day_limit_down=None, prev_day_w_avg=None,
          market_override=None):
    """Build a sentiment_data dict. Use the keyword args; pass `...` (Ellipsis)
    to omit the field entirely (i.e. test "key missing" behavior)."""
    market = {} if market_override is ... else (market_override or {})
    if limit_down is not ...:
        market["limit_down"] = limit_down
    if drop_over_9pct is not ...:
        market["drop_over_9pct"] = drop_over_9pct
    if prev_day_limit_down is not ...:
        market["prev_day_limit_down"] = prev_day_limit_down
    sent = {"market": market if market_override is not ... else None}
    if w_avg is not ...:
        sent["weighted_auction_gain"] = w_avg
    if prev_day_w_avg is not ...:
        sent["prev_day_weighted_auction_gain"] = prev_day_w_avg
    return sent


def _leader_full(
    avg_change_pct=2.5, sample_count=10, positive_count=6, negative_count=4,
    median_change_pct=1.8, high5_count=3, flat2_count=4, low5_count=2,
    limit_down_count=1, mb_list=None, **overrides,
):
    """Build a leader dict with full yesterday_main_board_avg_auction subfields."""
    y = {
        "avg_change_pct": avg_change_pct,
        "sample_count": sample_count,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "median_change_pct": median_change_pct,
        "high5_count": high5_count,
        "flat2_count": flat2_count,
        "low5_count": low5_count,
        "limit_down_count": limit_down_count,
    }
    y.update(overrides)
    return {
        "main_board_leaders": mb_list or [],
        "yesterday_main_board_avg_auction": y,
        "yesterday_zb_today_auction": {},
        "yesterday_limit_down_today_auction": {},
    }


def _good_sent():
    """All 4 dims green: ld=3, drop=2, w=+1, prev good (ld_prev=4, w_prev=+0.5)."""
    return _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=4, prev_day_w_avg=0.5)


# ============================================================
# AC1: 引入第四维（跌幅>9% 个股数 警戒）
# ============================================================

def test_1_1_unit_001_drop_bad_triggered_above_threshold():
    sent = _sent(limit_down=3, drop_over_9pct=10, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert "市场跌幅>9% 个股 10 只（>9 警戒线）" in advice["reason"]


def test_1_1_unit_002_drop_bad_strict_threshold_boundary():
    # BR-1.2: 严格 `>` 9，不是 `>=`
    sent = _sent(limit_down=3, drop_over_9pct=9, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert "跌幅>9%" not in advice["reason"]
    assert advice["bucket"] == "go"  # 没有警戒触发


def test_1_1_unit_003_drop_bad_independently_increments_bad_count():
    # 仅第四维触发 → bad_count=1 → bucket=warn
    sent = _sent(limit_down=3, drop_over_9pct=10, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["bucket"] == "warn"
    assert advice["position_short"] == "1.5层"


def test_1_1_unit_004_drop_over_9pct_missing_safe_default():
    # BR-1.1: drop_over_9pct=None → drop_bad=False
    sent = _sent(limit_down=3, drop_over_9pct=None, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert "跌幅>9%" not in advice["reason"]
    assert advice["bucket"] == "go"


def test_1_1_unit_005_drop_over_9pct_non_numeric_safe_default():
    # BR-1.1: 类型守护
    sent = _sent(limit_down=3, drop_over_9pct="abc", w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    # 不抛错
    advice = _calc_daily_advice(sent, _leader_full())
    assert "跌幅>9%" not in advice["reason"]


def test_1_1_unit_006_all_four_dims_empty_returns_loading_branch():
    # BR-1.3: has_drop 也参与"全空判定"
    advice = _calc_daily_advice(None, None)
    assert advice["text"] == "— 数据加载中 —"
    assert advice["position"] == "—"
    assert advice["position_short"] == "—"


# ============================================================
# AC2: 连续 2 日情绪好 → 升 4 层
# ============================================================

def test_1_1_unit_007_promote_to_4_layers_when_two_days_good():
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=4, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["position"] == "4 层（连续情绪良好）"
    assert advice["position_short"] == "4层"


def test_1_1_unit_008_fallback_to_3_layers_when_prev_bad():
    # BR-2.2: prev_w_avg=-0.5 → prevGood=False
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=4, prev_day_w_avg=-0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["position"] == "3 层（标准仓位）"
    assert advice["position_short"] == "3层"


def test_1_1_unit_009_promote_boundary_ld_eq_5_w_eq_0():
    # 边界: ld=5 (≤5 真) + w=0 (≥0 真)
    sent = _sent(limit_down=5, drop_over_9pct=2, w_avg=0,
                 prev_day_limit_down=5, prev_day_w_avg=0)
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["position"] == "4 层（连续情绪良好）"


def test_1_1_unit_010_warn_bucket_does_not_evaluate_promotion():
    # BR-2.1: warn 分支不进升仓评估
    # drop=10 触发 drop_bad → bad_count=1 → warn
    sent = _sent(limit_down=3, drop_over_9pct=10, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["bucket"] == "warn"
    assert advice["position"] == "1.5 层（小仓试错）"


def test_1_1_unit_011_prev_day_limit_down_missing_falls_back():
    # BR-2.2: prev_day_limit_down 字段缺失（不在 dict 中）
    market = {"limit_down": 3, "drop_over_9pct": 2}
    sent = {"market": market, "weighted_auction_gain": 1.0,
            "prev_day_weighted_auction_gain": 0.5}
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["position"] == "3 层（标准仓位）"


def test_1_1_unit_012_prev_day_w_none_fallback():
    # prev_day_weighted_auction_gain=None → prevGood=False
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=None)
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["position_short"] == "3层"


def test_1_1_unit_013_prev_good_independent_of_drop_over_9pct():
    # BR-2.3: prevGood 不引用 drop_over_9pct（即使昨日 drop=999，仍按 ld+w 判定）
    # 本 fixture 中没有"昨日 drop_over_9pct"字段，模拟该字段未参与
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    # 由于 prevGood 仅看 ld+w → 升 4 层
    assert advice["position_short"] == "4层"


# ============================================================
# AC3: 谨慎参与文案对齐 → "1.5 层（小仓试错）"
# ============================================================

def test_1_1_unit_014_warn_bucket_position_text():
    # bad_count=1 (仅 ld_bad)
    sent = _sent(limit_down=8, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["position"] == "1.5 层（小仓试错）"
    assert advice["position_short"] == "1.5层"
    # 旧文案不残留
    for v in advice.values():
        if isinstance(v, str):
            assert "1-2 层（小仓试错）" not in v
            assert "1-2层" not in v


def test_1_1_int_001_subject_renders_position_short_15(monkeypatch):
    # BR-3.2: subject 含 "仓位1.5层"
    captured = {}

    def fake_send(subject, html):
        captured["subject"] = subject
        captured["html"] = html
        return True

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@example.com")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    sent = _sent(limit_down=8, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1,
        representative=None, leader=_leader_full(),
        hits=[], signals=[],
        sentiment_data=sent, ranking_data=None,
    )
    assert ok is True
    assert "仓位1.5层" in captured["subject"]


# ============================================================
# AC4: 可参与文案对齐 → "3 层" / "4 层"
# ============================================================

def test_1_1_unit_015_go_bucket_3_layers_default():
    # 升仓条件不满足: prev_w=-0.5
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=-0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["bucket"] == "go"
    assert advice["position"] == "3 层（标准仓位）"
    assert advice["position_short"] == "3层"


def test_1_1_unit_016_go_bucket_4_layers_when_promoted():
    sent = _good_sent()
    advice = _calc_daily_advice(sent, _leader_full())
    assert advice["bucket"] == "go"
    assert advice["position"] == "4 层（连续情绪良好）"
    assert advice["position_short"] == "4层"


def test_1_1_unit_017_old_3_to_6_copy_completely_removed():
    # 任何分支都不应出现 "3-6 层" / "3-6层"
    cases = [
        # bad_count=0 升 4 层
        (_good_sent(), _leader_full()),
        # bad_count=0 不升仓
        (_sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
               prev_day_limit_down=3, prev_day_w_avg=-0.5), _leader_full()),
        # bad_count=1 (warn)
        (_sent(limit_down=8, drop_over_9pct=2, w_avg=1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), _leader_full()),
        # bad_count=2 (stop)
        (_sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), _leader_full()),
        # 数据加载中
        (None, None),
    ]
    for sent, leader in cases:
        advice = _calc_daily_advice(sent, leader)
        for v in advice.values():
            if isinstance(v, str):
                assert "3-6 层" not in v
                assert "3-6层" not in v


# ============================================================
# AC5: reason 维度数文案 → "四维"
# ============================================================

def test_1_1_unit_018_reason_says_four_dims_when_count_2():
    # ld_bad + drop_bad
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert "四维警戒中已 2 项触发，避免开仓。" in advice["reason"]


def test_1_1_unit_019_reason_max_count_4():
    # 4 维全部触发
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=-2.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    leader = _leader_full(mb_list=[
        {"leader_name": "test", "signal": "跌停", "auction_change_pct": -10},
    ])
    advice = _calc_daily_advice(sent, leader)
    assert "四维警戒中已 4 项触发" in advice["reason"]


def test_1_1_unit_020_no_legacy_three_dim_string_in_module():
    # BR-5.1: 整个模块文件不含 "三维"
    p = Path(email_sender.__file__)
    content = p.read_text(encoding="utf-8")
    assert "三维" not in content


# ============================================================
# AC6: 第 1 指标格双数 + 箭头 + 昨日对比
# ============================================================

def _render_html(sent, leader=None, hits=None):
    leader = leader if leader is not None else _leader_full()
    advice = _calc_daily_advice(sent, leader)
    return _build_html(
        cycle_phase="孕育期", cycle_day=1,
        leader=leader, hits=hits or [], signals=[],
        sentiment_data=sent, ranking_data=None, advice=advice,
    )


def test_1_1_unit_021_metric_cell_1_full_render():
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
                 prev_day_limit_down=5, prev_day_w_avg=0.5)
    html = _render_html(sent)
    assert "竞价跌停 (&gt;5⚠) / 跌&gt;9% (&gt;9⚠)" in html
    assert "8 / 12" in html
    assert "↑" in html
    assert "昨日跌停 5（差值+3）" in html


def test_1_1_unit_022_metric_cell_1_arrow_down():
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=5, prev_day_w_avg=0.5)
    html = _render_html(sent)
    assert "↓" in html
    assert "差值-2" in html


def test_1_1_unit_023_metric_cell_1_arrow_flat():
    sent = _sent(limit_down=5, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=5, prev_day_w_avg=0.5)
    html = _render_html(sent)
    assert "→" in html
    # 持平：差值=0
    assert "差值0" in html


def test_1_1_unit_024_metric_cell_1_partial_missing_limit_down():
    sent = _sent(limit_down=None, drop_over_9pct=12, w_avg=1.0,
                 prev_day_limit_down=5, prev_day_w_avg=0.5)
    html = _render_html(sent)
    assert "— / 12" in html
    # limit_down 缺失 → 无箭头
    # 比较前后的箭头出现位置可能比较脆弱，这里我们检查："差值" 不应出现（因为没有 limit_down 不能算差值）
    assert "差值" not in html


def test_1_1_unit_025_metric_cell_1_partial_missing_prev():
    # 主值有 limit_down=8，但 prev_day_limit_down 缺失
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
                 prev_day_limit_down=None, prev_day_w_avg=0.5)
    html = _render_html(sent)
    assert "昨日跌停 —" in html
    assert "差值" not in html


def test_1_1_unit_026_metric_cell_1_all_missing():
    sent = _sent(limit_down=None, drop_over_9pct=None, w_avg=None,
                 prev_day_limit_down=None, prev_day_w_avg=None)
    html = _render_html(sent)
    # 主值 "— / —"
    assert "— / —" in html


# ============================================================
# AC7: 第 4 指标格 "接力情绪" + 细分子项
# ============================================================

def test_1_1_unit_027_metric_cell_4_full_render():
    sent = _good_sent()
    html = _render_html(sent, _leader_full())
    assert "接力情绪" in html
    assert "昨日涨停 10 只 · 高开 6 / 低开 4 / 跌停 1" in html
    assert "中位数 +1.8%" in html
    assert "高开>5%:3" in html
    assert "平开±2%:4" in html
    assert "低开<-5%:2" in html


def test_1_1_unit_028_metric_cell_4_partial_missing_subfields():
    # Rebaselined 2026-05-08 per Story 2.3 BR-2.3 authorization:
    # When median_change_pct is None, the entire sub <div> is omitted (defensive UX)
    # — this supersedes the prior 1.1 BR-7.2 contract that rendered "中位数 —".
    sent = _good_sent()
    leader = _leader_full(median_change_pct=None)
    html = _render_html(sent, leader)
    for token in ("中位数", "高开>5%", "平开±2%", "低开<-5%"):
        assert token not in html, (
            f"Story 2.3 BR-2.3: sub-line token '{token}' must be absent when median is None"
        )


def test_1_1_unit_029_metric_cell_4_zero_sample():
    sent = _good_sent()
    leader = _leader_full(sample_count=0)
    html = _render_html(sent, leader)
    assert "昨日涨停 — 只" in html


def test_1_1_unit_030_legacy_label_4_completely_removed():
    sent = _good_sent()
    html = _render_html(sent)
    assert "昨日涨停溢价" not in html


# ============================================================
# AC8: 第 6 指标格标签
# ============================================================

def test_1_1_unit_031_metric_cell_6_new_label():
    sent = _good_sent()
    html = _render_html(sent)
    assert "昨日跌停平均反馈" in html


def test_1_1_unit_032_legacy_label_6_completely_removed():
    sent = _good_sent()
    html = _render_html(sent)
    assert "昨日跌停今日" not in html


# ============================================================
# AC9: 不引入回归
# ============================================================

def test_1_1_int_002_smtp_user_missing_skips_send(monkeypatch, capsys):
    monkeypatch.setattr(email_sender, "SMTP_USER", "")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1,
        representative=None, leader=_leader_full(),
        hits=[], signals=[],
        sentiment_data=_good_sent(), ranking_data=None,
    )
    assert ok is False
    out = capsys.readouterr().out
    assert "未配置 SMTP_USER" in out


def test_1_1_int_003_all_none_input_returns_loading_branch():
    advice = _calc_daily_advice(None, None)
    assert advice["bucket"] == "go"
    assert advice["text"] == "— 数据加载中 —"
    assert advice["position"] == "—"
    assert advice["reason"] == ""


def test_1_1_int_004_empty_hits_renders_placeholder():
    sent = _good_sent()
    html = _render_html(sent, hits=[])
    assert "无命中标的" in html


def test_1_1_int_005_send_screener_report_signature_unchanged():
    sig = inspect.signature(send_screener_report)
    params = list(sig.parameters.items())
    # 期望参数顺序与默认值
    expected = [
        ("cycle_phase", inspect.Parameter.empty),
        ("cycle_day", inspect.Parameter.empty),
        ("representative", inspect.Parameter.empty),
        ("leader", inspect.Parameter.empty),
        ("hits", inspect.Parameter.empty),
        ("signals", inspect.Parameter.empty),
        ("deviations", None),
        ("sentiment_data", None),
        ("ranking_data", None),
    ]
    assert len(params) == len(expected)
    for (name, default), (exp_name, exp_default) in zip(params, expected):
        assert name == exp_name
        if exp_default is inspect.Parameter.empty:
            assert params[[p[0] for p in params].index(name)][1].default is inspect.Parameter.empty
        else:
            assert sig.parameters[name].default == exp_default
    assert sig.return_annotation is bool


def test_1_1_int_006_dashboard_algorithm_equivalence_4dim_combinations():
    # 覆盖 4 维触发组合 0/1/2/3/4 项
    leader_safe = _leader_full()
    leader_lb = _leader_full(mb_list=[
        {"leader_name": "test", "signal": "跌停", "auction_change_pct": -10},
    ])
    cases = [
        # (sent, leader, expected_bucket, expected_bad_count)
        # 0 项触发
        (_sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), leader_safe, "go", 0),
        # 1 项触发 - ld
        (_sent(limit_down=8, drop_over_9pct=2, w_avg=1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), leader_safe, "warn", 1),
        # 1 项触发 - drop
        (_sent(limit_down=3, drop_over_9pct=12, w_avg=1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), leader_safe, "warn", 1),
        # 1 项触发 - w
        (_sent(limit_down=3, drop_over_9pct=2, w_avg=-1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), leader_safe, "warn", 1),
        # 1 项触发 - lb
        (_sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), leader_lb, "warn", 1),
        # 2 项触发 - ld+drop
        (_sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), leader_safe, "stop", 2),
        # 3 项触发 - ld+drop+w
        (_sent(limit_down=8, drop_over_9pct=12, w_avg=-1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), leader_safe, "stop", 3),
        # 4 项触发
        (_sent(limit_down=8, drop_over_9pct=12, w_avg=-1.0,
               prev_day_limit_down=3, prev_day_w_avg=0.5), leader_lb, "stop", 4),
    ]
    for sent, leader, exp_bucket, exp_count in cases:
        advice = _calc_daily_advice(sent, leader)
        assert advice["bucket"] == exp_bucket, (
            f"sent={sent}, expected={exp_bucket}, got={advice['bucket']}"
        )
        if exp_count >= 2:
            assert f"四维警戒中已 {exp_count} 项触发" in advice["reason"]


def test_1_1_int_007_html_keyword_negative_and_positive_assertion():
    # 三类 bucket 全都跑一遍
    cases = [
        # stop
        _sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
              prev_day_limit_down=3, prev_day_w_avg=0.5),
        # warn
        _sent(limit_down=8, drop_over_9pct=2, w_avg=1.0,
              prev_day_limit_down=3, prev_day_w_avg=0.5),
        # go (升 4 层)
        _good_sent(),
    ]
    for sent in cases:
        html = _render_html(sent)
        # 旧关键词不得出现
        assert "三维" not in html
        assert "3-6 层" not in html and "3-6层" not in html
        assert "昨日涨停溢价" not in html
        assert "昨日跌停今日" not in html
    # 新关键词在合适分支出现
    html_stop = _render_html(cases[0])
    assert "四维" in html_stop
    html_warn = _render_html(cases[1])
    assert "1.5 层（小仓试错）" in html_warn
    html_go = _render_html(cases[2])
    assert "接力情绪" in html_go
    assert "昨日跌停平均反馈" in html_go


# ============================================================
# Blind Spot Scenarios [BLIND-SPOT]
# ============================================================

def test_1_1_blind_boundary_001_drop_over_9pct_zero():
    sent = _sent(limit_down=3, drop_over_9pct=0, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert "跌幅>9%" not in advice["reason"]
    assert advice["bucket"] == "go"


def test_1_1_blind_boundary_002_drop_over_9pct_extreme_large():
    sent = _sent(limit_down=3, drop_over_9pct=9999, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    advice = _calc_daily_advice(sent, _leader_full())
    assert "市场跌幅>9% 个股 9999 只" in advice["reason"]


def test_1_1_blind_boundary_003_all_four_dims_triggered():
    sent = _sent(limit_down=10, drop_over_9pct=15, w_avg=-2.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    leader = _leader_full(mb_list=[
        {"leader_name": "test", "signal": "跌停", "auction_change_pct": -10},
    ])
    advice = _calc_daily_advice(sent, leader)
    assert advice["bucket"] == "stop"
    assert "四维警戒中已 4 项触发" in advice["reason"]


def test_1_1_blind_boundary_004_w_avg_type_mismatch():
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg="abc",
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    # 不应抛错
    advice = _calc_daily_advice(sent, _leader_full())
    # has_w=False → w_bad=False
    assert "梯队加权竞价" not in advice["reason"]


def test_1_1_blind_error_001_yesterday_avg_auction_dict_missing():
    # leader 缺 yesterday_main_board_avg_auction
    leader = {"main_board_leaders": []}
    sent = _good_sent()
    # 不抛 KeyError
    html = _render_html(sent, leader)
    # 第 4 格仍然存在标签 "接力情绪"，子项以 "—" 占位
    assert "接力情绪" in html


def test_1_1_blind_error_002_market_dict_none():
    sent = {"market": None, "weighted_auction_gain": 1.0,
            "prev_day_weighted_auction_gain": 0.5}
    # 不抛 KeyError
    advice = _calc_daily_advice(sent, _leader_full())
    # has_ld=False, has_drop=False → ld_bad/drop_bad=False
    assert "市场竞价跌停" not in advice["reason"]
    assert "跌幅>9%" not in advice["reason"]


def test_1_1_blind_flow_001_loading_state_downstream_render_safe(monkeypatch):
    # 全空数据 → bucket=go, position="—"
    captured = {}
    def fake_send(subject, html):
        captured["subject"] = subject
        captured["html"] = html
        return True

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1,
        representative=None, leader=None,
        hits=[], signals=[],
        sentiment_data=None, ranking_data=None,
    )
    assert ok is True
    # subject 不应出现 "0层"（数据加载中时是 "—"）
    assert "0层" not in captured["subject"]
    assert "仓位—" in captured["subject"]
    # HTML 渲染未抛错
    assert "无命中标的" in captured["html"]

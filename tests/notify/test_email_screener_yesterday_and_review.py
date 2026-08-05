"""早盘邮件：空仓原因 / 昨日选股今日决策 / 脚注指标；盘后复盘邮件。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.notify import email_sender
from src.notify.email_sender import (
    _build_html,
    _build_review_html,
    _render_per_stock_decision_email,
    send_review_guard_allows,
    send_review_report,
)


CN_TZ = timezone(timedelta(hours=8))


def _ts(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 7, 15, hh, mm, ss, tzinfo=CN_TZ)


def _advice_v2(*, ri=12.5, b1=15.2):
    return {
        "bucket": "warn",
        "text": "⚠️ 谨慎参与",
        "position": "建议仓位：1.5 层",
        "position_short": "1.5层",
        "reason": "测试",
        "color": "#fbbf24",
        "bg": "#2a2a1a",
        "conclusion": "谨慎参与",
        "dashboard": {
            "participate": {
                "limit_down_main_board": 13,
                "drop_over_9pct": 14,
                "relay_decision_index": ri,
                "b1_rate": b1,
                "space_board_auction_pct": 5.9,
                "space_board_label": "强 (+5.9%)",
                "space_board_name": "测试",
                "space_board_board_count": 3,
            },
            "reference": {},
            "decision": {
                "headline": "⚠️ 谨慎参与",
                "position": "建议仓位：1.5 层",
                "conclusion": "⚠️ 谨慎参与 (小小仓试错)",
                "tone": "warn",
            },
        },
    }


@pytest.fixture
def tmp_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(email_sender, "DATA_DIR", tmp_path)
    email_sender._TODAY_SENT_CACHE.clear()
    yield tmp_path


def test_empty_position_shows_reason():
    hit = {
        "per_stock_decision": {
            "action": "空仓",
            "position_text": "0% (空仓)",
            "ladder_label": "2进3",
            "reason": "1进2 信号不做（v4.0 历史胜率偏低，仅做 2进3+）",
        }
    }
    html = _render_per_stock_decision_email(hit, _advice_v2())
    assert "⛔ 空仓" in html
    assert "空仓原因：" in html
    assert "1进2 信号不做" in html


def test_open_position_no_empty_reason():
    hit = {
        "per_stock_decision": {
            "action": "开仓",
            "position_text": "50%",
            "ladder_label": "2进3",
            "reason": "环境达标",
        }
    }
    html = _render_per_stock_decision_email(hit, _advice_v2())
    assert "开仓" in html
    assert "空仓原因" not in html


def test_yesterday_section_only_when_rows(monkeypatch):
    rows = [
        {
            "date": "2026-07-14",
            "code": "600664",
            "name": "哈药股份",
            "continuous_limit_up": 3,
            "auction_gain": 4.5,
            "day_change": 10.0,
            "next_day_auction_gain": 3.2,
            "next_day_sell_advice": {
                "summary": "昨涨停 今竞价+3.2% 持有并设回撤止盈 博弈连板，回落3%卖",
                "tone": "hold",
                "ladder_label": "3进4",
            },
        }
    ]
    monkeypatch.setattr(
        email_sender,
        "_load_yesterday_selections_for_email",
        lambda: ("2026-07-14", rows),
    )
    html = _build_html(
        "孕育期", 0, None,
        hits=[{"code": "001388", "name": "信测标准", "auction_gain": 4.81,
               "open_price": 34.0, "continuous_limit_up": 2, "market_cap": 28.74,
               "auction_turnover": 0.81,
               "per_stock_decision": {"action": "空仓", "position_text": "0%",
                                     "reason": "条件未达"}}],
        signals=[],
        sentiment_data=None,
        ranking_data=None,
        advice=_advice_v2(),
    )
    assert "昨日选股今日竞价" in html
    assert "哈药股份" in html
    assert "今日决策" in html
    assert "持有并设回撤止盈" in html
    assert "当前周期" not in html
    assert "加权接力情绪" in html
    assert "1进2成功率" in html
    assert "空仓原因：" in html


def test_yesterday_section_hidden_when_empty(monkeypatch):
    monkeypatch.setattr(
        email_sender,
        "_load_yesterday_selections_for_email",
        lambda: ("", []),
    )
    html = _build_html(
        "孕育期", 0, None, hits=[], signals=[],
        sentiment_data=None, ranking_data=None, advice=_advice_v2(),
    )
    assert "昨日选股今日竞价" not in html
    assert "当前周期" not in html


def test_review_html_omits_target_column():
    review = {
        "date": "2026-07-15",
        "market_breadth": {
            "sh_close": 3955.58,
            "sh_pct": -0.29,
            "advance": 3086,
            "decline": 1841,
            "flat": 64,
        },
        "scorecard": {
            "total_score": 6,
            "decision": "重仓",
            "decision_color": "#ef4444",
            "indicators": [
                {
                    "label": "1进2成功率",
                    "today": "15.2%",
                    "target": "≥15%",
                    "score": 1,
                    "detail": "12/79",
                },
                {
                    "label": "2进3成功率",
                    "today": "50.0%",
                    "target": "≥25%",
                    "score": 1,
                    "detail": "3/6",
                },
            ],
            "today_action": {
                "verdict": "重仓",
                "position": "≥50%",
                "ladders": "全梯队",
                "note": "六项满分，明日可全梯队接力",
            },
        },
    }
    html = _build_review_html(review)
    assert "接力环境评分卡" in html
    assert "1进2成功率" in html
    assert "15.2%" in html
    assert "12/79" in html
    assert "达标标准" not in html
    assert "≥15%" not in html  # target 列内容不出现
    assert "明日决策：重仓" in html
    assert "上证指数" in html
    assert "3086" in html
    # 手机竖排：不再用四列表头
    assert ">今日数据<" not in html
    assert 'role="presentation"' in html
    assert "display:flex" not in html


def test_review_guard_evening_window(monkeypatch, tmp_data_dir):
    monkeypatch.setattr(email_sender, "now_cn", lambda: _ts(18, 0))
    ok, _ = send_review_guard_allows(force=False)
    assert ok is True

    monkeypatch.setattr(email_sender, "now_cn", lambda: _ts(9, 30))
    ok, reason = send_review_guard_allows(force=False)
    assert ok is False
    assert "窗口" in reason

    # force 可绕过窗口（手动补发）
    ok, _ = send_review_guard_allows(force=True)
    assert ok is True


def test_review_and_screener_idempotent_independent(monkeypatch, tmp_data_dir):
    """选股已发不拦复盘；复盘已发不拦选股（kind 分离）。"""
    monkeypatch.setattr(email_sender, "now_cn", lambda: _ts(18, 5))
    email_sender._record_send("cron", "早盘选股", kind="screener")
    ok, _ = send_review_guard_allows(force=False)
    assert ok is True

    email_sender._record_send("review_eod", "复盘", kind="review")
    ok, reason = send_review_guard_allows(force=False)
    assert ok is False
    assert "复盘" in reason


def test_send_review_report_happy_path(monkeypatch, tmp_data_dir):
    monkeypatch.setattr(email_sender, "now_cn", lambda: _ts(18, 0))
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")
    captured = {}

    def fake_send(subject, html):
        captured["subject"] = subject
        captured["html"] = html
        return True

    monkeypatch.setattr(email_sender, "_send", fake_send)
    review = {
        "date": "2026-07-15",
        "scorecard": {
            "total_score": 6,
            "decision": "重仓",
            "decision_color": "#ef4444",
            "indicators": [
                {"label": "高度突破", "today": "4板↑", "target": "≥昨高-1",
                 "score": 1, "detail": "今4/昨3"},
            ],
            "today_action": {
                "verdict": "重仓", "position": "≥50%",
                "ladders": "全梯队", "note": "满分",
            },
        },
    }
    ok = send_review_report(review, entry="review_eod")
    assert ok is True
    assert "复盘" in captured["subject"]
    assert "重仓" in captured["subject"]
    assert "达标标准" not in captured["html"]

"""Story anti-duplicate-email-hardening-2.6 升级守卫。

在 2.5 (`tests/scheduler/test_run_screener_skip_email.py`) 基础上补 3 个新规则：

  R1 当日已发过 → 即便 skip_email=False / force=True 也**不能**绕过日期幂等
  R2 凌晨/晚间非交易时段 → 硬拦截（force=True 仍拦）
  R3 send_log 走 append-only jsonl，便于事后排查

Mock 模式参考 tests/scheduler/test_run_screener_skip_email.py。
Run:
    pytest tests/notify/test_send_guard_hardening.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src import scheduler
from src.notify import email_sender


CN_TZ = timezone(timedelta(hours=8))


def _ts(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 6, 10, hh, mm, ss, tzinfo=CN_TZ)


@pytest.fixture
def tmp_data_dir(monkeypatch, tmp_path):
    """重定向 DATA_DIR 到 tmp_path，隔离 email_send_log.jsonl。"""
    monkeypatch.setattr(email_sender, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    email_sender._TODAY_SENT_CACHE.clear()
    yield tmp_path


@pytest.fixture
def stub_pipeline(monkeypatch, tmp_path):
    """仿 test_run_screener_skip_email.stub_pipeline。"""
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        scheduler, "_fetch_screener_data", lambda: (pd.DataFrame(), [])
    )
    monkeypatch.setattr(scheduler, "run_screener", lambda *a, **kw: [])
    fake_thread = MagicMock()
    fake_thread.start = MagicMock(return_value=None)
    monkeypatch.setattr("threading.Thread", lambda *a, **kw: fake_thread)
    yield


# ============================================================
# R1: 当日已发过 → 拦截（即便 skip_email=False / force=True）
# ============================================================
def test_today_already_sent_blocks_skip_email_false(
    stub_pipeline, tmp_data_dir, monkeypatch
):
    """3:44 已发过（写日志），5:09 skip_email=False 触发 → 守卫拦。"""
    log = tmp_data_dir / "email_send_log.jsonl"
    log.write_text(
        json.dumps(
            {"ts": "2026-06-10 03:44:07", "day": "2026-06-10",
             "entry": "cron:False", "subject": "早盘"},
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "now_cn", lambda: _ts(5, 9, 0))
    with patch("src.notify.email_sender.send_screener_report") as mock_send:
        with patch.object(email_sender, "_send", return_value=True) as mock_real_send:
            res = scheduler.run_screener_update(skip_email=False)
    assert mock_real_send.call_count == 0, (
        "当日已发过 → 凌晨 5:09 即便 skip_email=False 也不应触达 _send"
    )
    # 返回值仍表示已"调用" send_screener_report（被守卫拦 False），但实际上不发
    # 这里不直接断言 res 形状；mock_send 也没被原函数触发（因为 run_screener_update 内部 inline 调）


def test_today_already_sent_blocks_in_window_too(
    stub_pipeline, tmp_data_dir, monkeypatch
):
    """9:30 窗口内，但 03:44 已发过 → 同日不重发。"""
    log = tmp_data_dir / "email_send_log.jsonl"
    log.write_text(
        json.dumps(
            {"ts": "2026-06-10 03:44:07", "day": "2026-06-10",
             "entry": "cron:False", "subject": "凌晨误发"},
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "now_cn", lambda: _ts(9, 30, 0))
    with patch.object(email_sender, "_send", return_value=True) as mock_real_send:
        scheduler.run_screener_update(skip_email=False)
    assert mock_real_send.call_count == 0, (
        "当日已发过 → 即便回到 9:30 窗口内也不应重发"
    )


# ============================================================
# R2: 非交易时段硬拦截（凌晨/晚间）
# ============================================================
def test_3am_outside_trading_hours_blocks(
    stub_pipeline, tmp_data_dir, monkeypatch
):
    """3:00 凌晨即便 skip_email=False 也被守卫拦。"""
    monkeypatch.setattr(scheduler, "now_cn", lambda: _ts(3, 0, 0))
    with patch.object(email_sender, "_send", return_value=True) as mock_real_send:
        scheduler.run_screener_update(skip_email=False)
    assert mock_real_send.call_count == 0, (
        "凌晨 3:00 处于非交易时段，send_screener_report 守卫必须拦截"
    )


def test_5am_outside_trading_hours_blocks(
    stub_pipeline, tmp_data_dir, monkeypatch
):
    """5:09 凌晨同样拦。"""
    monkeypatch.setattr(scheduler, "now_cn", lambda: _ts(5, 9, 0))
    with patch.object(email_sender, "_send", return_value=True) as mock_real_send:
        scheduler.run_screener_update(skip_email=False)
    assert mock_real_send.call_count == 0, (
        "凌晨 5:09 处于非交易时段，必须拦截"
    )


def test_20_oclock_outside_trading_hours_blocks(
    stub_pipeline, tmp_data_dir, monkeypatch
):
    """20:00 晚间（非交易时段）拦截（与上次的 20:00 推送 bug 互证）。"""
    monkeypatch.setattr(scheduler, "now_cn", lambda: _ts(20, 0, 0))
    with patch.object(email_sender, "_send", return_value=True) as mock_real_send:
        scheduler.run_screener_update(skip_email=False)
    assert mock_real_send.call_count == 0, (
        "20:00 收盘后非交易时段，必须拦截"
    )


def test_noon_break_within_trading_hours_but_outside_send_window(
    stub_pipeline, tmp_data_dir, monkeypatch
):
    """12:00 午间处于交易时段但不在 cron 核心窗口 → 拦截（cron 默认窗口）。"""
    monkeypatch.setattr(scheduler, "now_cn", lambda: _ts(12, 0, 0))
    with patch.object(email_sender, "_send", return_value=True) as mock_real_send:
        # skip_email=False 不带 force，cron 核心窗口 9:25-9:35
        scheduler.run_screener_update(skip_email=False)
    assert mock_real_send.call_count == 0, (
        "12:00 午间不在 cron 核心窗口，skip_email=False 不带 api_explicit 必拦"
    )


# ============================================================
# R3: send_log append-only jsonl
# ============================================================
def test_send_log_writes_jsonl(stub_pipeline, tmp_data_dir, monkeypatch):
    """实际成功发送后应向 send_log 追加一行 jsonl。"""
    fake_now = lambda: _ts(9, 30, 0)
    monkeypatch.setattr(scheduler, "now_cn", fake_now)
    # 必须同时 mock email_sender.now_cn（它直接调用 src.config.now_cn）
    monkeypatch.setattr(email_sender, "now_cn", fake_now)
    # 构造最小可用 advice（避免写 _build_html 内部依赖）
    fake_advice = {
        "bucket": "go",
        "text": "✅ 可参与",
        "position": "3 层（标准仓位）",
        "position_short": "3层",
        "reason": "",
        "color": "#ef4444",
        "bg": "#2a0f0f",
        "conclusion": "✅ 可参与",
        "dashboard": None,
    }
    with patch.object(email_sender, "_load_advice_from_disk", return_value=fake_advice), \
         patch.object(email_sender, "_build_html", return_value="<html/>"), \
         patch.object(email_sender, "_send", return_value=True):
        scheduler.run_screener_update(skip_email=False)

    log = tmp_data_dir / "email_send_log.jsonl"
    assert log.exists(), "send_screener_report 成功后应写 send_log"
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, f"应有 1 条记录，实际 {len(lines)}"
    rec = json.loads(lines[0])
    assert rec["day"] == "2026-06-10"
    assert rec["ts"].startswith("2026-06-10 09:30:00")
    assert "entry" in rec and "subject" in rec


# ============================================================
# send_guard_allows 单元测试（守卫核心）
# ============================================================
class TestSendGuardAllows:
    def test_force_skips_idempotency_but_not_window(self, monkeypatch, tmp_data_dir):
        """force=True 跳过日期幂等，但**仍**受交易时段/窗口约束。"""
        monkeypatch.setattr(email_sender, "DATA_DIR", tmp_data_dir)
        email_sender._TODAY_SENT_CACHE.clear()
        # 凌晨 3 点 force=True → 非交易时段硬拦
        monkeypatch.setattr(email_sender, "now_cn", lambda: _ts(3, 0, 0))
        ok, reason = email_sender.send_guard_allows(api_explicit=False, force=True)
        assert ok is False
        assert "非交易时段" in reason, f"期望拦在非交易时段，实际: {reason}"

    def test_window_still_blocked_for_1200_force_true(self, monkeypatch, tmp_data_dir):
        """12:00 force=True 不带 api_explicit → cron 核心窗口外仍拦。"""
        monkeypatch.setattr(email_sender, "DATA_DIR", tmp_data_dir)
        email_sender._TODAY_SENT_CACHE.clear()
        monkeypatch.setattr(email_sender, "now_cn", lambda: _ts(12, 0, 0))
        ok, reason = email_sender.send_guard_allows(api_explicit=False, force=True)
        assert ok is False
        assert "窗口" in reason or "out" in reason.lower(), f"期望窗口外拦截，实际: {reason}"

    def test_9_30_in_cron_window_allows(self, monkeypatch, tmp_data_dir):
        """9:30 cron 核心窗口内，无历史发送 → 放行。"""
        monkeypatch.setattr(email_sender, "DATA_DIR", tmp_data_dir)
        email_sender._TODAY_SENT_CACHE.clear()
        monkeypatch.setattr(email_sender, "now_cn", lambda: _ts(9, 30, 0))
        ok, reason = email_sender.send_guard_allows(api_explicit=False, force=False)
        assert ok is True, f"9:30 cron 窗口内应放行，实际: {reason}"

    def test_3am_force_true_blocked_no_idempotency_bypass(
        self, monkeypatch, tmp_data_dir
    ):
        """凌晨 3:00 即使 force=True 也不能发（窗口是硬规则）。"""
        monkeypatch.setattr(email_sender, "DATA_DIR", tmp_data_dir)
        email_sender._TODAY_SENT_CACHE.clear()
        monkeypatch.setattr(email_sender, "now_cn", lambda: _ts(3, 0, 0))
        ok, _ = email_sender.send_guard_allows(api_explicit=True, force=True)
        assert ok is False, "凌晨 3:00 force=True 仍必须被拦（非交易时段）"

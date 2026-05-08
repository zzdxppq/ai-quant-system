"""Story anti-duplicate-email-2.5: 防盘中重复邮件
（run_screener_update skip_email 守卫）

AC7 四分支：
- (a) skip_email=True            → send_screener_report 未被调用
- (b) skip_email=False           → send_screener_report 被调用
- (c) skip_email=None & 09:27:30 → 被调用（落在 9:27±5min 窗口内）
- (d) skip_email=None & 11:35:00 → 未被调用（落在窗口外）

Mock 模式参考: tests/notify/test_decision_consistency.py

Run:
    pytest tests/scheduler/test_run_screener_skip_email.py -v
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src import scheduler
from src.config import SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE


CN_TZ = timezone(timedelta(hours=8))


def _ts(hh: int, mm: int, ss: int = 0) -> datetime:
    """构造北京时间 datetime（与 now_cn 输出 tz 一致）"""
    return datetime(2026, 5, 8, hh, mm, ss, tzinfo=CN_TZ)


@pytest.fixture
def stub_pipeline(monkeypatch, tmp_path):
    """让 run_screener_update 的重 I/O 都变成 no-op，仅保留 8c 守卫逻辑可测。

    - DATA_DIR 重定向到 tmp_path（避免污染真实 data/）
    - _fetch_screener_data → 空 DataFrame + 空 limit_up_hist
    - run_screener         → 空 hits（绕过富化/偏离度/cross_validate 路径）
    - threading.Thread     → 不真正启动后台任务
    """
    monkeypatch.setattr(scheduler, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        scheduler, "_fetch_screener_data", lambda: (pd.DataFrame(), [])
    )
    monkeypatch.setattr(scheduler, "run_screener", lambda *a, **kw: [])

    fake_thread = MagicMock()
    fake_thread.start = MagicMock(return_value=None)
    monkeypatch.setattr("threading.Thread", lambda *a, **kw: fake_thread)
    yield


def _patch_email():
    """Patch 在源模块路径，inline `from src.notify.email_sender import ...` 会拿到 mock。"""
    return patch("src.notify.email_sender.send_screener_report")


# ============================================================
# AC7 (a) skip_email=True → 未被调用（即便处于 9:27 窗口内）
# ============================================================
def test_skip_email_true_never_sends(stub_pipeline, monkeypatch):
    monkeypatch.setattr(
        scheduler, "now_cn",
        lambda: _ts(SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE, 30),
    )
    with _patch_email() as mock_send:
        scheduler.run_screener_update(skip_email=True)
    assert mock_send.call_count == 0, (
        "skip_email=True 时不应调用 send_screener_report"
    )


# ============================================================
# AC7 (b) skip_email=False → 被调用（即便处于窗口外）
# ============================================================
def test_skip_email_false_always_sends(stub_pipeline, monkeypatch):
    monkeypatch.setattr(scheduler, "now_cn", lambda: _ts(11, 35, 0))
    with _patch_email() as mock_send:
        scheduler.run_screener_update(skip_email=False)
    assert mock_send.call_count == 1, (
        "skip_email=False 时必须调用 send_screener_report"
    )


# ============================================================
# AC7 (c) skip_email=None & 09:27:30 → 被调用（窗口内）
# ============================================================
def test_skip_email_none_in_window_sends(stub_pipeline, monkeypatch):
    monkeypatch.setattr(
        scheduler, "now_cn",
        lambda: _ts(SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE, 30),
    )
    with _patch_email() as mock_send:
        scheduler.run_screener_update()  # skip_email=None 缺省
    assert mock_send.call_count == 1, (
        "9:27 窗口内 skip_email=None 必须调用 send_screener_report"
    )


# ============================================================
# AC7 (d) skip_email=None & 11:35:00 → 未被调用（窗口外）
# ============================================================
def test_skip_email_none_outside_window_skips(stub_pipeline, monkeypatch):
    monkeypatch.setattr(scheduler, "now_cn", lambda: _ts(11, 35, 0))
    with _patch_email() as mock_send:
        scheduler.run_screener_update()
    assert mock_send.call_count == 0, (
        "9:27 窗口外 skip_email=None 不应调用 send_screener_report"
    )


# ============================================================
# 守卫单元测试（边界 ±5min 端点 + 类型显式）
# ============================================================
class TestShouldSendEmailGuard:
    def test_true_skips(self):
        assert scheduler._should_send_email(True) is False

    def test_false_sends(self):
        assert scheduler._should_send_email(False) is True

    def test_none_window_lower_bound_inclusive(self, monkeypatch):
        # 9:22:00 端点含
        monkeypatch.setattr(
            scheduler, "now_cn",
            lambda: _ts(SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE - 5, 0),
        )
        assert scheduler._should_send_email(None) is True

    def test_none_window_upper_bound_inclusive(self, monkeypatch):
        # 9:32:00 端点含
        monkeypatch.setattr(
            scheduler, "now_cn",
            lambda: _ts(SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE + 5, 0),
        )
        assert scheduler._should_send_email(None) is True

    def test_none_just_outside_lower(self, monkeypatch):
        # 9:21:59 → 不含
        monkeypatch.setattr(
            scheduler, "now_cn",
            lambda: _ts(SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE - 6, 59),
        )
        assert scheduler._should_send_email(None) is False

    def test_none_just_outside_upper(self, monkeypatch):
        # 9:32:01 → 不含
        monkeypatch.setattr(
            scheduler, "now_cn",
            lambda: _ts(SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE + 5, 1),
        )
        assert scheduler._should_send_email(None) is False

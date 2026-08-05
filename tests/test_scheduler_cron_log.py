"""APScheduler cron 起止日志（logs/scheduler.log）。"""
from __future__ import annotations

import src.scheduler as sched


def test_wrap_cron_job_writes_start_done(tmp_path, monkeypatch):
    log_file = tmp_path / "scheduler.log"
    monkeypatch.setattr(sched, "SCHEDULER_LOG_PATH", log_file)
    monkeypatch.setattr(sched, "now_cn", lambda: __import__("datetime").datetime(2026, 5, 21, 9, 27, 0))

    def ok_fn():
        return {"n": 1}

    out = sched.wrap_cron_job("screener_update", ok_fn)()
    assert out == {"n": 1}
    text = log_file.read_text(encoding="utf-8")
    assert "[screener_update] START" in text
    assert "[screener_update] DONE ok" in text
    assert text.count("\n") == 2


def test_wrap_cron_job_logs_fail(tmp_path, monkeypatch):
    log_file = tmp_path / "scheduler.log"
    monkeypatch.setattr(sched, "SCHEDULER_LOG_PATH", log_file)

    def bad_fn():
        raise RuntimeError("boom")

    try:
        sched.wrap_cron_job("cycle_update", bad_fn)()
    except RuntimeError:
        pass
    text = log_file.read_text(encoding="utf-8")
    assert "[cycle_update] START" in text
    assert "[cycle_update] DONE FAIL error=boom" in text

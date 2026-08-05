"""东财 K 线节流与退避。"""
from src.data import em_request_guard as guard


def test_backoff_sleep_does_not_block_on_zero(monkeypatch):
    slept = []
    monkeypatch.setattr(guard.time, "sleep", lambda s: slept.append(s))
    guard.backoff_sleep(0)
    assert slept == []


def test_backoff_exponential_seconds(monkeypatch):
    slept = []
    monkeypatch.setattr(guard.time, "sleep", lambda s: slept.append(s))
    guard.backoff_sleep(1)
    guard.backoff_sleep(2)
    guard.backoff_sleep(3)
    assert slept == [1.0, 2.0, 4.0]

"""NOTIFY_TO 多收件人解析。"""
from src.notify import email_sender


def test_notify_recipients_default_two(monkeypatch):
    monkeypatch.delenv("NOTIFY_TO", raising=False)
    got = email_sender.notify_recipients()
    assert got == ["604491810@qq.com", "1124031210@qq.com"]


def test_notify_recipients_comma_separated(monkeypatch):
    monkeypatch.setenv("NOTIFY_TO", " a@x.com , b@y.com ")
    assert email_sender.notify_recipients() == ["a@x.com", "b@y.com"]

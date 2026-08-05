"""send_screener_report：0 命中仍发决策邮件。"""
from unittest.mock import patch

from src.notify.email_sender import send_screener_report


def test_send_screener_report_sends_decision_when_empty_hits(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "u@test.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    with patch("src.notify.email_sender._send", return_value=True) as mock_send:
        ok = send_screener_report(
            cycle_phase="孕育期",
            cycle_day=1,
            representative=None,
            leader=None,
            hits=[],
            signals=[],
        )
    assert ok is True
    mock_send.assert_called_once()
    subject = mock_send.call_args[0][0]
    assert "0只命中" in subject

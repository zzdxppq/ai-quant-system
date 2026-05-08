"""Test for Story decision-consistency-2.1: 9:27 决策快照单一真源
（看板 + 邮件读同一份 latest_advice.json）

Test Design: docs/qa/assessments/decision-consistency-2.1-test-design-20260508.md
Sibling baseline: tests/notify/test_email_decision_alignment.py (email-sync-1.1, 46 cases)

Run:
    pytest tests/notify/test_decision_consistency.py -v
"""
import inspect
import json
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.notify import email_sender
from src.notify.email_sender import (
    _calc_daily_advice,
    _load_advice_from_disk,
    send_screener_report,
    write_advice_snapshot,
)


# ============================================================
# Helpers — 沿用 sibling test_email_decision_alignment.py 风格
# ============================================================

def _sent(*, limit_down=None, drop_over_9pct=None, w_avg=None,
          prev_day_limit_down=None, prev_day_w_avg=None,
          market_override=None):
    """Build a sentiment_data dict. Pass `...` (Ellipsis) to omit a field."""
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


def _leader_min(mb_list=None):
    """Minimal leader fixture (only main_board_leaders matters for advice algo)."""
    return {"main_board_leaders": mb_list or []}


def _good_sent():
    """All 4 dims green (bad_count=0; with prev good → promotes to 4 层)."""
    return _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=4, prev_day_w_avg=0.5)


def _full_advice_payload():
    """A complete latest_advice.json payload — used by AC3 helper tests."""
    return {
        "generated_at": "2026-05-08 09:27:15",
        "bucket": "warn",
        "text": "⚠️ 谨慎参与",
        "suggested_position": "1.5 层（小仓试错）",
        "suggested_position_short": "1.5层",
        "reason": "市场竞价跌停 8 只（>5 警戒线）。仅一项警戒，可小仓试错或观望。",
        "bad_count": 1,
        "dimensions": {"ld_bad": True, "drop_bad": False, "w_bad": False, "lb_bad": False},
        "inputs": {
            "limit_down": 8, "drop_over_9pct": 2, "weighted_auction_gain": 1.0,
            "prev_day_limit_down": 4, "prev_day_weighted_auction_gain": 0.5,
            "main_board_leaders_summary": [],
        },
    }


def _patch_data_dir(monkeypatch, tmp_path):
    """Redirect DATA_DIR in all touched modules to tmp_path."""
    monkeypatch.setattr(email_sender, "DATA_DIR", tmp_path)
    from src.api import app as app_module
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)


def _write_advice_file(tmp_path, payload):
    (tmp_path / "latest_advice.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_HTML = PROJECT_ROOT / "src" / "static" / "index.html"
SCHEDULER_PY = PROJECT_ROOT / "src" / "scheduler.py"
APP_PY = PROJECT_ROOT / "src" / "api" / "app.py"
BASELINE_FIXTURE = Path(__file__).parent / "fixtures" / "index_template_baseline.json"


# ============================================================
# AC1: 9:27 选股完成后写 data/latest_advice.json
# ============================================================

def test_2_1_unit_001_payload_contains_all_required_fields(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    write_advice_snapshot(_good_sent(), _leader_min())
    data = json.loads((tmp_path / "latest_advice.json").read_text(encoding="utf-8"))
    expected_keys = {
        "generated_at", "bucket", "text", "suggested_position",
        "suggested_position_short", "reason", "bad_count", "dimensions", "inputs",
    }
    assert expected_keys.issubset(data.keys())
    assert len(expected_keys & data.keys()) == 9


def test_2_1_unit_002_dimensions_field_structure(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    payload = write_advice_snapshot(_good_sent(), _leader_min())
    dims = payload["dimensions"]
    assert set(dims.keys()) == {"ld_bad", "drop_bad", "w_bad", "lb_bad"}
    for v in dims.values():
        assert isinstance(v, bool)


def test_2_1_unit_003_zero_dim_triggered_all_green(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=4, prev_day_w_avg=0.5)
    payload = write_advice_snapshot(sent, _leader_min())
    assert payload["bucket"] == "go"
    assert payload["bad_count"] == 0
    assert all(v is False for v in payload["dimensions"].values())


def test_2_1_unit_004_one_dim_triggered_warn(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=8, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    payload = write_advice_snapshot(sent, _leader_min())
    assert payload["bad_count"] == 1
    assert payload["bucket"] == "warn"
    assert payload["suggested_position"] == "1.5 层（小仓试错）"
    assert payload["suggested_position_short"] == "1.5层"
    dims = payload["dimensions"]
    assert dims["ld_bad"] is True
    assert dims["drop_bad"] is False
    assert dims["w_bad"] is False
    assert dims["lb_bad"] is False


def test_2_1_unit_005_two_dim_triggered_stop(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    payload = write_advice_snapshot(sent, _leader_min())
    assert payload["bad_count"] == 2
    assert payload["bucket"] == "stop"
    assert payload["suggested_position"] == "0 层（空仓避险）"
    assert payload["suggested_position_short"] == "0层"


def test_2_1_unit_006_three_dim_triggered(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=-0.5,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    payload = write_advice_snapshot(sent, _leader_min())
    assert payload["bad_count"] == 3
    assert payload["reason"].endswith("四维警戒中已 3 项触发，避免开仓。")


def test_2_1_unit_007_four_dim_triggered_max(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=-0.5,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    leader = _leader_min(mb_list=[
        {"leader_name": "X", "signal": "跌停", "auction_change_pct": -10.0,
         "board_count": 4, "leader_gain_10d": 15.2},
    ])
    payload = write_advice_snapshot(sent, leader)
    assert payload["bad_count"] == 4
    assert all(v is True for v in payload["dimensions"].values())
    assert payload["reason"].count("（") >= 1
    for needle in ("跌停", "跌幅", "加权竞价", "连板高标"):
        assert needle in payload["reason"]


def test_2_1_unit_008_field_naming_snake_case(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    payload = write_advice_snapshot(_good_sent(), _leader_min())
    assert "suggested_position" in payload
    assert "suggested_position_short" in payload
    assert "position" not in payload
    assert "position_short" not in payload


def test_2_1_unit_009_inputs_preserves_raw_values(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=-0.3,
                 prev_day_limit_down=5, prev_day_w_avg=0.2)
    payload = write_advice_snapshot(sent, _leader_min())
    inputs = payload["inputs"]
    expected_keys = {
        "limit_down", "drop_over_9pct", "weighted_auction_gain",
        "prev_day_limit_down", "prev_day_weighted_auction_gain",
        "main_board_leaders_summary",
    }
    assert expected_keys.issubset(inputs.keys())
    assert inputs["limit_down"] == 8
    assert inputs["drop_over_9pct"] == 12
    assert inputs["weighted_auction_gain"] == -0.3
    assert inputs["prev_day_limit_down"] == 5
    assert inputs["prev_day_weighted_auction_gain"] == 0.2


def test_2_1_unit_010_main_board_leaders_summary_is_simplified(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    leader = _leader_min(mb_list=[
        {"leader_name": "X", "signal": "跌停", "auction_change_pct": -3.5,
         "board_count": 4, "leader_gain_10d": 15.2,
         "leader_code": "600001", "aggression": "high"},
    ])
    payload = write_advice_snapshot(_good_sent(), leader)
    summary = payload["inputs"]["main_board_leaders_summary"]
    assert len(summary) == 1
    assert summary[0] == {"leader_name": "X", "signal": "跌停", "auction_change_pct": -3.5}
    assert "board_count" not in summary[0]
    assert "leader_gain_10d" not in summary[0]


def test_2_1_unit_011_loading_branch_when_all_inputs_none(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    payload = write_advice_snapshot(None, None)
    assert (tmp_path / "latest_advice.json").exists()
    assert payload["bucket"] == "go"
    assert payload["text"] == "— 数据加载中 —"
    assert payload["suggested_position"] == "—"
    assert payload["bad_count"] == 0


def test_2_1_unit_012_reason_text_equivalence_with_calc(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    leader = _leader_min()
    payload = write_advice_snapshot(sent, leader)
    expected_reason = _calc_daily_advice(sent, leader)["reason"]
    assert payload["reason"] == expected_reason


def test_2_1_unit_013_generated_at_format(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    fixed = datetime(2026, 5, 8, 9, 27, 15)
    monkeypatch.setattr(email_sender, "now_cn", lambda: fixed)
    payload = write_advice_snapshot(_good_sent(), _leader_min())
    assert payload["generated_at"] == "2026-05-08 09:27:15"


def test_2_1_unit_014_utf8_indent_no_ascii_escape(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    write_advice_snapshot(sent, _leader_min())
    raw = (tmp_path / "latest_advice.json").read_bytes()
    assert "今日不操作".encode("utf-8") in raw
    assert b"\\u4eca" not in raw  # 不应被 escape
    text = raw.decode("utf-8")
    assert "\n  " in text  # indent=2


def test_2_1_unit_015_write_failure_silenced(tmp_path, monkeypatch, capsys):
    _patch_data_dir(monkeypatch, tmp_path)
    real_wt = Path.write_text

    def fail_wt(self, *args, **kwargs):
        if self.name == "latest_advice.json":
            raise IOError("disk full")
        return real_wt(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_wt)
    result = write_advice_snapshot(_good_sent(), _leader_min())
    assert result is None
    out = capsys.readouterr().out
    assert "[决策快照] 写入失败" in out


def test_2_1_unit_016_w_avg_zero_boundary_not_bad(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent = _sent(limit_down=3, drop_over_9pct=2, w_avg=0,
                 prev_day_limit_down=3, prev_day_w_avg=0.5)
    payload = write_advice_snapshot(sent, _leader_min())
    assert payload["dimensions"]["w_bad"] is False


# ============================================================
# AC2: 看板 dailyAdvice 改读 /api/daily-advice，不再实时算
# ============================================================

def _api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api.app import app
    _patch_data_dir(monkeypatch, tmp_path)
    return TestClient(app)


def test_2_1_unit_017_api_daily_advice_returns_file_content(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch)
    payload = _full_advice_payload()
    _write_advice_file(tmp_path, payload)
    res = client.get("/api/daily-advice")
    assert res.status_code == 200
    assert res.json() == payload


_PLACEHOLDER = {
    "generated_at": "",
    "bucket": "go",
    "text": "— 数据加载中 —",
    "suggested_position": "—",
    "suggested_position_short": "—",
    "reason": "",
    "bad_count": 0,
    "dimensions": {"ld_bad": False, "drop_bad": False, "w_bad": False, "lb_bad": False},
    "inputs": {},
}


def test_2_1_unit_018_api_daily_advice_placeholder_when_missing(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch)
    assert not (tmp_path / "latest_advice.json").exists()
    res = client.get("/api/daily-advice")
    assert res.status_code == 200
    assert res.json() == _PLACEHOLDER


def test_2_1_unit_019_api_daily_advice_corrupted_json_placeholder(tmp_path, monkeypatch, capsys):
    client = _api_client(tmp_path, monkeypatch)
    (tmp_path / "latest_advice.json").write_text("{ invalid json", encoding="utf-8")
    res = client.get("/api/daily-advice")
    assert res.status_code == 200
    assert res.json() == _PLACEHOLDER
    out = capsys.readouterr().out
    assert "[决策快照] 读取失败" in out


def _index_html_text():
    return INDEX_HTML.read_text(encoding="utf-8")


def _extract_loaddata_block(text):
    m = re.search(r"async function loadData\(\)\s*\{(.*?)^\s{8}\}", text, re.DOTALL | re.MULTILINE)
    assert m, "loadData function not located"
    return m.group(1)


def _extract_dailyadvice_computed_block(text):
    m = re.search(
        r"const dailyAdvice\s*=\s*computed\(\(\)\s*=>\s*\{(.*?)^\s{8}\}\)",
        text, re.DOTALL | re.MULTILINE,
    )
    assert m, "dailyAdvice computed not located"
    return m.group(1)


def test_2_1_int_001_dashboard_loaddata_includes_daily_advice_fetch():
    text = _index_html_text()
    block = _extract_loaddata_block(text)
    assert re.search(r"fetch\(['\"]/api/daily-advice['\"]\)", block)
    promise_all = re.search(r"Promise\.all\(\[(.*?)\]\)", block, re.DOTALL)
    assert promise_all, "Promise.all not found in loadData"
    assert "/api/daily-advice" in promise_all.group(1)


def test_2_1_int_002_dashboard_advice_ref_declared():
    text = _index_html_text()
    assert "const advice = ref(null)" in text
    block = _extract_loaddata_block(text)
    assert re.search(r"advice\.value\s*=", block)


def test_2_1_int_003_daily_advice_computed_does_not_read_market_sentiment_leader():
    text = _index_html_text()
    block = _extract_dailyadvice_computed_block(text)
    assert "market.value" not in block
    assert "sentiment.value" not in block
    assert "leader.value" not in block
    assert "advice.value" in block


def test_2_1_int_004_bucket_to_cls_mapping():
    text = _index_html_text()
    assert "stop: 'advice-stop'" in text or 'stop: "advice-stop"' in text
    assert "warn: 'advice-warn'" in text or 'warn: "advice-warn"' in text
    assert "go: 'advice-go'" in text or 'go: "advice-go"' in text


def _read_baseline():
    return json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))


def _line_range(text, start, end):
    lines = text.split("\n")
    return [lines[i - 1] for i in range(start, end + 1)]


def test_2_1_int_005_template_html_unchanged_in_lines_505_to_666():
    # Note: line numbers rebaselined to 541/631/694 in Story dashboard-hits-table-display-2.4
    # after detecting drift caused by commit ba52314 (操作列+日K图). Asserted CONTENT
    # remains character-equal to the original 2.1 freeze; only the offset within
    # index.html changed because ba52314 added ~36 lines above the hero-banner block.
    text = _index_html_text()
    base = _read_baseline()
    assert _line_range(text, 541, 545) == base["lines_541_545"]
    assert _line_range(text, 631, 632) == base["lines_631_632"]
    assert _line_range(text, 694, 703) == base["lines_694_703"]


def test_2_1_int_006_suggested_position_camel_case_mapping():
    text = _index_html_text()
    assert "{{ dailyAdvice.suggestedPosition }}" in text
    block = _extract_dailyadvice_computed_block(text)
    assert "suggestedPosition" in block
    assert "suggested_position" in block  # 来自 advice.value 的字段


# ============================================================
# AC3: 邮件 _calc_daily_advice 改读 latest_advice.json
# ============================================================

def test_2_1_unit_020_load_advice_from_disk_returns_none_when_missing(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    assert _load_advice_from_disk() is None


def test_2_1_unit_021_load_advice_from_disk_corrupted_json_returns_none(tmp_path, monkeypatch, capsys):
    _patch_data_dir(monkeypatch, tmp_path)
    (tmp_path / "latest_advice.json").write_text("{ invalid json", encoding="utf-8")
    assert _load_advice_from_disk() is None
    out = capsys.readouterr().out
    assert "[邮件] 决策快照解析失败" in out


def test_2_1_unit_022_load_advice_from_disk_partial_fields_returns_none(tmp_path, monkeypatch, capsys):
    _patch_data_dir(monkeypatch, tmp_path)
    (tmp_path / "latest_advice.json").write_text(
        json.dumps({"text": "x"}, ensure_ascii=False), encoding="utf-8",
    )
    assert _load_advice_from_disk() is None
    out = capsys.readouterr().out
    assert "[邮件] 决策快照字段不全" in out


def test_2_1_unit_023_load_advice_renames_snake_case_to_internal(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    _write_advice_file(tmp_path, _full_advice_payload())
    advice = _load_advice_from_disk()
    assert advice is not None
    assert advice["position"] == "1.5 层（小仓试错）"
    assert advice["position_short"] == "1.5层"
    assert advice["bucket"] == "warn"
    assert advice["text"] == "⚠️ 谨慎参与"
    assert advice["reason"].startswith("市场竞价跌停")
    assert "color" in advice and "bg" in advice
    assert "suggested_position" not in advice
    assert "suggested_position_short" not in advice


def test_2_1_unit_024_send_screener_report_skips_calc_when_disk_present(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    _write_advice_file(tmp_path, _full_advice_payload())

    spy = MagicMock(wraps=_calc_daily_advice)
    monkeypatch.setattr(email_sender, "_calc_daily_advice", spy)
    monkeypatch.setattr(email_sender, "_send", lambda subject, html: True)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1, representative=None,
        leader=_leader_min(), hits=[], signals=[],
        sentiment_data=_good_sent(), ranking_data=None,
    )
    assert ok is True
    assert spy.call_count == 0


def test_2_1_unit_025_send_screener_report_falls_back_when_disk_missing(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    assert not (tmp_path / "latest_advice.json").exists()

    spy = MagicMock(wraps=_calc_daily_advice)
    monkeypatch.setattr(email_sender, "_calc_daily_advice", spy)
    monkeypatch.setattr(email_sender, "_send", lambda subject, html: True)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    sent = _good_sent()
    leader = _leader_min()
    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1, representative=None,
        leader=leader, hits=[], signals=[],
        sentiment_data=sent, ranking_data=None,
    )
    assert ok is True
    assert spy.call_count == 1
    args, _ = spy.call_args
    assert args == (sent, leader)


def test_2_1_unit_026_calc_daily_advice_function_body_preserved():
    src = inspect.getsource(_calc_daily_advice)
    assert src.strip()
    assert "warnings" in src
    assert "bad_count" in src


def test_2_1_int_007_send_screener_report_signature_unchanged():
    sig = str(inspect.signature(send_screener_report))
    expected = (
        "(cycle_phase: str, cycle_day: int, representative: dict | None, "
        "leader: dict | None, hits: list[dict], signals: list[dict], "
        "deviations: list[dict] | None = None, sentiment_data: dict | None = None, "
        "ranking_data: dict | None = None) -> bool"
    )
    assert sig == expected


def test_2_1_int_008_email_subject_uses_disk_position_short(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    payload = _full_advice_payload()
    payload["suggested_position_short"] = "1.5层"
    _write_advice_file(tmp_path, payload)

    captured = {}

    def fake_send(subject, html):
        captured["subject"] = subject
        return True

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1, representative=None,
        leader=_leader_min(), hits=[], signals=[],
        sentiment_data=_good_sent(), ranking_data=None,
    )
    assert ok is True
    assert "仓位1.5层" in captured["subject"]


# ============================================================
# AC4: scheduler 流程顺序
# ============================================================

def _read_scheduler_src():
    return SCHEDULER_PY.read_text(encoding="utf-8")


def _line_no(text, needle):
    for i, line in enumerate(text.split("\n"), start=1):
        if needle in line:
            return i
    return -1


def test_2_1_int_009_advice_write_between_signals_and_email():
    src = _read_scheduler_src()
    signals_line = _line_no(src, '"latest_signals.json"')
    advice_line = _line_no(src, "write_advice_snapshot(")
    email_line = _line_no(src, "send_screener_report(")
    assert signals_line > 0 and advice_line > 0 and email_line > 0
    assert signals_line < advice_line < email_line, (
        f"order broken: signals={signals_line}, advice={advice_line}, email={email_line}"
    )


def test_2_1_int_010_advice_write_not_in_background_thread():
    src = _read_scheduler_src()
    advice_line = _line_no(src, "write_advice_snapshot(")
    bg_line = _line_no(src, "def _background_tasks(")
    assert advice_line > 0 and bg_line > 0
    assert advice_line < bg_line, (
        f"write_advice_snapshot must be before _background_tasks "
        f"(advice={advice_line}, _background_tasks={bg_line})"
    )


def test_2_1_int_011_write_failure_does_not_block_email(tmp_path, monkeypatch):
    """Sequence-level integration: scheduler-style sequence (write → send) does
    not abort when write_advice_snapshot's disk write fails."""
    _patch_data_dir(monkeypatch, tmp_path)

    real_wt = Path.write_text

    def fail_wt(self, *args, **kwargs):
        if self.name == "latest_advice.json":
            raise IOError("disk full")
        return real_wt(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_wt)

    send_called = {"v": False}

    def fake_send(subject, html):
        send_called["v"] = True
        return True

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    # 模拟 scheduler 的 7b → 8c 顺序
    write_result = write_advice_snapshot(_good_sent(), _leader_min())
    assert write_result is None  # 写盘失败但不抛
    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1, representative=None,
        leader=_leader_min(), hits=[], signals=[],
        sentiment_data=_good_sent(), ranking_data=None,
    )
    assert ok is True
    assert send_called["v"] is True


# ============================================================
# AC5: 不引入回归（DoD）
# ============================================================

def test_2_1_int_012_smtp_missing_skips_send_but_advice_still_written(tmp_path, monkeypatch, capsys):
    _patch_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(email_sender, "SMTP_USER", "")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "")

    sent = _good_sent()
    leader = _leader_min()
    payload = write_advice_snapshot(sent, leader)
    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1, representative=None,
        leader=leader, hits=[], signals=[],
        sentiment_data=sent, ranking_data=None,
    )
    assert ok is False
    out = capsys.readouterr().out
    assert "[邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送" in out
    assert payload is not None
    assert (tmp_path / "latest_advice.json").exists()


def test_2_1_int_013_all_none_inputs_loading_branch_full_chain(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)

    captured = {}

    def fake_send(subject, html):
        captured["subject"] = subject
        return True

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    write_advice_snapshot(None, None)
    payload_on_disk = json.loads((tmp_path / "latest_advice.json").read_text(encoding="utf-8"))
    assert payload_on_disk["bucket"] == "go"
    assert payload_on_disk["text"] == "— 数据加载中 —"
    assert payload_on_disk["bad_count"] == 0
    assert all(v is False for v in payload_on_disk["dimensions"].values())

    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1, representative=None,
        leader=None, hits=[], signals=[],
        sentiment_data=None, ranking_data=None,
    )
    assert ok is True
    assert "仓位—" in captured["subject"]


def test_2_1_int_014_empty_hits_renders_placeholder(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)

    captured = {}

    def fake_send(subject, html):
        captured["subject"] = subject
        captured["html"] = html
        return True

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    write_advice_snapshot(_good_sent(), _leader_min())
    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1, representative=None,
        leader=_leader_min(), hits=[], signals=[],
        sentiment_data=_good_sent(), ranking_data=None,
    )
    assert ok is True
    assert (tmp_path / "latest_advice.json").exists()
    assert "无命中标的" in captured["html"]


def test_2_1_int_015_dashboard_template_html_unchanged_vs_baseline():
    text = _index_html_text()
    base = _read_baseline()
    # 与 INT-005 重叠，但作为 AC5 回归保护独立断言
    # Line numbers rebaselined in Story dashboard-hits-table-display-2.4 — see INT-005 note
    for key, (s, e) in [
        ("lines_541_545", (541, 545)),
        ("lines_631_632", (631, 632)),
        ("lines_694_703", (694, 703)),
    ]:
        assert _line_range(text, s, e) == base[key], (
            f"Template lines {s}-{e} drifted from baseline (AC5 regression)"
        )


def test_2_1_int_016_refresh_screener_path_also_writes_advice():
    """BLIND-FLOW: refresh-screener path == cron path (run_screener_update)
    → both write latest_advice.json."""
    app_src = APP_PY.read_text(encoding="utf-8")
    sched_src = SCHEDULER_PY.read_text(encoding="utf-8")
    refresh = re.search(
        r"async def refresh_screener\([^)]*\):.*?(?=\n@app\.|\nasync def |\ndef [^_])",
        app_src, re.DOTALL,
    )
    assert refresh, "refresh_screener handler not located"
    assert "run_screener_update" in refresh.group()
    # run_screener_update 内必须含决策快照写入
    rsu = re.search(
        r"def run_screener_update\(\).*?(?=\ndef [^_]|\Z)",
        sched_src, re.DOTALL,
    )
    assert rsu, "run_screener_update not located"
    assert "write_advice_snapshot" in rsu.group()


# ============================================================
# 端到端一致性 (DoD #1：跨 AC1+AC2+AC3)
# ============================================================

def test_2_1_int_017_consistency_file_eq_api_response(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch)
    sent = _sent(limit_down=8, drop_over_9pct=2, w_avg=1.0,
                 prev_day_limit_down=4, prev_day_w_avg=0.5)
    payload = write_advice_snapshot(sent, _leader_min())
    on_disk = json.loads((tmp_path / "latest_advice.json").read_text(encoding="utf-8"))
    res = client.get("/api/daily-advice")
    assert res.status_code == 200
    assert res.json() == on_disk
    assert payload == on_disk


def _fixture_for_bad_count(target):
    """Construct (sent, leader) yielding bad_count == target."""
    if target == 0:
        return _good_sent(), _leader_min()
    if target == 1:
        return (_sent(limit_down=8, drop_over_9pct=2, w_avg=1.0,
                      prev_day_limit_down=3, prev_day_w_avg=0.5),
                _leader_min())
    if target == 2:
        return (_sent(limit_down=8, drop_over_9pct=12, w_avg=1.0,
                      prev_day_limit_down=3, prev_day_w_avg=0.5),
                _leader_min())
    if target == 3:
        return (_sent(limit_down=8, drop_over_9pct=12, w_avg=-0.5,
                      prev_day_limit_down=3, prev_day_w_avg=0.5),
                _leader_min())
    if target == 4:
        return (_sent(limit_down=8, drop_over_9pct=12, w_avg=-0.5,
                      prev_day_limit_down=3, prev_day_w_avg=0.5),
                _leader_min(mb_list=[
                    {"leader_name": "X", "signal": "跌停", "auction_change_pct": -5.0},
                ]))
    raise AssertionError(f"unsupported bad_count target {target}")


@pytest.mark.parametrize("bad_count_target", [0, 1, 2, 3, 4])
def test_2_1_int_018_consistency_email_subject_eq_disk_payload(bad_count_target, tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    sent, leader = _fixture_for_bad_count(bad_count_target)

    payload = write_advice_snapshot(sent, leader)
    assert payload["bad_count"] == bad_count_target

    captured = {}

    def fake_send(subject, html):
        captured["subject"] = subject
        return True

    monkeypatch.setattr(email_sender, "_send", fake_send)
    monkeypatch.setattr(email_sender, "SMTP_USER", "u@x")
    monkeypatch.setattr(email_sender, "SMTP_PASSWORD", "p")

    ok = send_screener_report(
        cycle_phase="孕育期", cycle_day=1, representative=None,
        leader=leader, hits=[], signals=[],
        sentiment_data=sent, ranking_data=None,
    )
    assert ok is True

    on_disk = json.loads((tmp_path / "latest_advice.json").read_text(encoding="utf-8"))
    short = on_disk["suggested_position_short"]
    assert f"仓位{short}" in captured["subject"]

    helper = _load_advice_from_disk()
    assert helper["position_short"] == short

"""Tests for Story dashboard-hits-table-display-2.4.

Implements 47 designed scenarios (16 P0 / 25 P1 / 6 P2; 18 BLIND-SPOT).
Test Design: docs/qa/assessments/dashboard-hits-table-display-2.4-test-design-20260508.md

Run:
    pytest tests/test_screener_display_2_4.py -v
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import subprocess
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
INDEX_HTML = PROJECT_ROOT / "src" / "static" / "index.html"
SCHEDULER_PY = PROJECT_ROOT / "src" / "scheduler.py"
SCREENER_PY = PROJECT_ROOT / "src" / "engine" / "screener.py"
ENRICH_PY = PROJECT_ROOT / "src" / "engine" / "screener_concept_enrich.py"
CROSS_VALIDATOR_PY = PROJECT_ROOT / "src" / "engine" / "cross_validator.py"
BASELINE_FILE = PROJECT_ROOT / "tests" / "fixtures" / "screener_display_baselines.json"


# ============================================================
# Helpers
# ============================================================

def _read_lines(path: Path, start: int, end: int) -> str:
    """Return content of [start, end] inclusive (1-indexed) joined by '\\n'."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[start - 1:end])


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_baseline() -> dict:
    if not BASELINE_FILE.exists():
        pytest.fail(
            f"Baseline file missing: {BASELINE_FILE.relative_to(PROJECT_ROOT)} "
            "(Dev T3 must freeze before AC2/AC4 SHA assertions)"
        )
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


def _build_screener_hit(**overrides):
    """Return a ScreenerHit constructed with sane defaults; pass overrides to mutate."""
    from src.engine.screener import ScreenerHit
    defaults = dict(
        code="600519", name="贵州茅台", continuous_limit_up=2,
        open_price=104.0, auction_gain=4.0, auction_turnover=0.5,
        auction_amount=2000.0, auction_volume_lots=20000.0,
        auction_volume_ratio=2.0, market_cap=25.0,
        volume_ratio=2.0, gain_10d=5.0, matched_cycle=False,
    )
    defaults.update(overrides)
    return ScreenerHit(**defaults)


def _build_realtime_df(**row_overrides):
    """Build a 1-row realtime_df that satisfies every screener filter unless overridden."""
    base = dict(
        code="600519", name="贵州茅台",
        pre_close=100.0, open=104.0, close=104.0,
        volume=1_000_000.0, amount=200_000_000.0,
        turnover=1.0, market_cap=2.5e9,
        volume_ratio=2.0, gain_10d=5.0,
    )
    base.update(row_overrides)
    return pd.DataFrame([base])


def _run_screener_minimal(monkeypatch, *, realtime_df=None,
                          qualified_codes=None, **row_overrides):
    """Run run_screener with continuous-limit-up + kline fetch bypassed."""
    from src.engine import screener as screener_mod
    monkeypatch.setattr(
        screener_mod, "_detect_continuous_limit_up",
        lambda _h: dict(qualified_codes or {"600519": 2}),
    )
    monkeypatch.setattr(screener_mod, "_get_avg_volume_5d", lambda _c: None)
    df = realtime_df if realtime_df is not None else _build_realtime_df(**row_overrides)
    return screener_mod.run_screener(df, {"placeholder": pd.DataFrame()})


def _build_hits_data(*hits_dicts) -> dict:
    """Build the hits_data dict that scheduler writes to latest_screener.json."""
    return {
        "date": "2026-05-08 09:27:00",
        "hits": list(hits_dicts) if hits_dicts else [],
    }


# ============================================================
# AC1: screener.py outputs market_cap=None (no NaN)
# ============================================================


def test_2_4_unit_001_safe_round_normal_value():
    """P0 | _safe_round(2.349) == 2.35 (verifies normal rounding semantics).

    Note: 2.345 in float64 is actually 2.34499..., so round(2.345, 2) == 2.34.
    Use unambiguous values to verify the rounding contract independent of
    IEEE-754 representation surprises.
    """
    from src.engine.screener import _safe_round
    assert _safe_round(25.0) == 25.0
    assert _safe_round(2.349) == 2.35
    assert _safe_round(2.341) == 2.34
    assert _safe_round(2.5e9 / 1e8) == 25.0


def test_2_4_unit_002_safe_round_zero_returns_none():
    """P0 [BLIND-BOUNDARY-002] | _safe_round(0) returns None (BR-1.1 <= 0)."""
    from src.engine.screener import _safe_round
    assert _safe_round(0) is None
    assert _safe_round(0.0) is None


def test_2_4_unit_003_safe_round_negative_returns_none():
    """P0 [BLIND-BOUNDARY-005] | _safe_round(-1) returns None (BR-1.1 negative)."""
    from src.engine.screener import _safe_round
    assert _safe_round(-1.0) is None
    assert _safe_round(-1) is None


def test_2_4_unit_004_safe_round_nan_returns_none():
    """P0 [BLIND-BOUNDARY-004] | _safe_round(NaN) returns None (root cause of '亿' artifact)."""
    from src.engine.screener import _safe_round
    assert _safe_round(float("nan")) is None


def test_2_4_unit_005_safe_round_none_returns_none():
    """P0 [BLIND-BOUNDARY-001] | _safe_round(None) returns None."""
    from src.engine.screener import _safe_round
    assert _safe_round(None) is None


def test_2_4_unit_006_safe_round_inf_returns_none():
    """P1 [BLIND-BOUNDARY-003] | _safe_round(±inf) returns None."""
    from src.engine.screener import _safe_round
    assert _safe_round(float("inf")) is None
    assert _safe_round(float("-inf")) is None


def test_2_4_unit_007_safe_round_string_returns_none():
    """P1 [BLIND-BOUNDARY-005] | _safe_round('25.0') returns None (no implicit coercion)."""
    from src.engine.screener import _safe_round
    assert _safe_round("25.0") is None
    assert _safe_round("") is None


def test_2_4_unit_008_safe_round_dict_returns_none():
    """P1 [BLIND-BOUNDARY-006] | _safe_round(dict) returns None."""
    from src.engine.screener import _safe_round
    assert _safe_round({"a": 1}) is None
    assert _safe_round([1, 2]) is None


def test_2_4_unit_009_safe_round_with_ndigits():
    """P1 | _safe_round honors ndigits parameter (signature accepts non-default)."""
    from src.engine.screener import _safe_round
    assert _safe_round(25.0, ndigits=0) == 25.0
    assert _safe_round(25.345, ndigits=1) == 25.3
    assert _safe_round(2.349, ndigits=1) == 2.3


def test_2_4_unit_010_run_screener_market_cap_nan_to_none(monkeypatch):
    """P0 | run_screener(row.market_cap=NaN) → ScreenerHit.market_cap is None."""
    hits = _run_screener_minimal(monkeypatch, market_cap=float("nan"))
    assert len(hits) >= 1, "Hit should pass screener with NaN market_cap (soft filter)"
    assert hits[0].code == "600519"
    assert hits[0].market_cap is None


def test_2_4_unit_011_run_screener_market_cap_valid_value(monkeypatch):
    """P0 | run_screener(row.market_cap=2.5e9) → ScreenerHit.market_cap == 25.0."""
    hits = _run_screener_minimal(monkeypatch, market_cap=2.5e9)
    assert len(hits) >= 1
    assert hits[0].market_cap == 25.0


def test_2_4_unit_012_json_dumps_market_cap_none_outputs_null():
    """P0 [BLIND-DATA-002] | json.dumps emits 'null' (not NaN) for market_cap=None."""
    hit = _build_screener_hit(market_cap=None)
    s = json.dumps(asdict(hit), ensure_ascii=False)
    assert '"market_cap": null' in s
    assert "NaN" not in s
    parsed = json.loads(s)
    assert parsed["market_cap"] is None


def test_2_4_unit_013_safe_round_module_level_definition():
    """P1 | _safe_round defined at module level in src/engine/screener.py."""
    text = SCREENER_PY.read_text(encoding="utf-8")
    assert re.search(r"^def _safe_round\(", text, re.MULTILINE), \
        "_safe_round must be defined at module level in screener.py"


def test_2_4_unit_014_screener_line_206_uses_safe_round():
    """P1 | screener.py line 206 call site replaced with _safe_round (no leftover round(market_cap_yi,2))."""
    text = SCREENER_PY.read_text(encoding="utf-8")
    assert "_safe_round(market_cap_yi)" in text, \
        "ScreenerHit.market_cap construction must use _safe_round"
    assert "round(market_cap_yi, 2)" not in text, \
        "Leftover `round(market_cap_yi, 2)` indicates incomplete migration"


def test_2_4_unit_015_screener_hit_market_cap_optional_type():
    """P1 | ScreenerHit.market_cap type annotation indicates Optional/None."""
    from src.engine.screener import ScreenerHit
    field_type = ScreenerHit.__dataclass_fields__["market_cap"].type
    type_str = str(field_type) if not isinstance(field_type, str) else field_type
    assert any(token in type_str for token in ("Optional", "None", "|")), \
        f"market_cap type '{type_str}' must indicate Optional/None"


def test_2_4_unit_016_screener_soft_filter_unchanged():
    """P1 | screener.py preserves `if market_cap_yi > 0:` soft-filter (BR-1.4)."""
    text = SCREENER_PY.read_text(encoding="utf-8")
    assert "if market_cap_yi > 0:" in text, \
        "Soft-filter `if market_cap_yi > 0:` must remain (BR-1.4)"


# ============================================================
# AC2: dashboard market_cap column displays "—" for null
# ============================================================


def test_2_4_unit_017_template_line_638_v_if():
    """P0 | index.html market-cap cell contains v-if="hit.market_cap != null"."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    assert 'v-if="hit.market_cap != null"' in text, \
        "Market-cap cell must guard with v-if=\"hit.market_cap != null\""


def test_2_4_unit_018_template_line_638_yi_unit_retained():
    """P0 | market-cap cell still contains '亿' unit on the v-if branch."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    # The v-if branch must render `{{ hit.market_cap }}亿`
    pattern = re.compile(
        r'v-if="hit\.market_cap != null"[^<]*>\s*\{\{\s*hit\.market_cap\s*\}\}亿',
        re.DOTALL,
    )
    assert pattern.search(text), \
        "Market-cap v-if branch must render '{{ hit.market_cap }}亿'"


def test_2_4_unit_019_template_line_638_em_dash_fallback():
    """P0 | market-cap v-else fallback uses '—' (U+2014), aligned with deviation table."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    # The v-else branch must contain U+2014 em-dash
    pattern = re.compile(
        r'v-if="hit\.market_cap != null".*?<template v-else>\s*—\s*</template>',
        re.DOTALL,
    )
    assert pattern.search(text), \
        "Market-cap v-else fallback must render '—' (U+2014 em-dash)"
    # Cross-check: the chosen char must be U+2014 not U+002D hyphen-minus
    assert "—" in text


def test_2_4_unit_020_template_line_638_uses_template_v_else():
    """P1 | market-cap cell uses <template v-else> structure (not v-show / ternary)."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    pattern = re.compile(
        r'v-if="hit\.market_cap != null".*?<template v-else>',
        re.DOTALL,
    )
    assert pattern.search(text), \
        "Market-cap cell must use <template v-else> structure"


def test_2_4_unit_021_table_header_sha256_unchanged():
    """P1 | screener-table header (12 <th> rows) SHA256 unchanged (BR-2.3)."""
    baseline = _load_baseline()
    region = baseline["regions"]["table_header"]
    actual = _sha256(_read_lines(INDEX_HTML, region["start"], region["end"]))
    assert actual == region["sha256"], \
        f"Table header SHA256 mismatch (lines {region['start']}-{region['end']})"


def test_2_4_unit_022_other_9_columns_sha256_unchanged():
    """P1 | screener-table 'other 9 columns' SHA256 unchanged (BR-2.2)."""
    baseline = _load_baseline()
    pre = baseline["regions"]["before_board"]
    post = baseline["regions"]["after_board"]
    actual_pre = _sha256(_read_lines(INDEX_HTML, pre["start"], pre["end"]))
    actual_post = _sha256(_read_lines(INDEX_HTML, post["start"], post["end"]))
    assert actual_pre == pre["sha256"], "Before-board columns SHA256 mismatch"
    assert actual_post == post["sha256"], "After-board columns SHA256 mismatch"


def test_2_4_unit_023_concept_column_template_sha256_unchanged():
    """P2 | board-column template SHA256 unchanged (BR-2.3 + cross-check with AC4)."""
    baseline = _load_baseline()
    region = baseline["regions"]["board_column"]
    actual = _sha256(_read_lines(INDEX_HTML, region["start"], region["end"]))
    assert actual == region["sha256"], \
        f"Board column SHA256 mismatch (lines {region['start']}-{region['end']})"


# ============================================================
# AC3: server-side enrich top_concepts + industry into screener_hits
# ============================================================


def _stub_concept_modules(monkeypatch, *, c_map=None, heats=None,
                           industry_map=None, ranking_loader_raises=False,
                           limit_up_loader_raises=False,
                           filter_concepts_raises=False):
    """Replace the cache loaders consumed by the enrich helper with deterministic stubs."""
    from src.engine import screener_concept_enrich as mod

    if ranking_loader_raises:
        def _raise(*_a, **_kw):
            raise RuntimeError("concept_cache load fail")
        monkeypatch.setattr(mod, "_load_stock_to_concepts_safe", _raise)
    else:
        monkeypatch.setattr(mod, "_load_stock_to_concepts_safe",
                            lambda: dict(c_map or {}))

    if limit_up_loader_raises:
        def _raise(*_a, **_kw):
            raise RuntimeError("limit_up_cache load fail")
        monkeypatch.setattr(mod, "_load_concept_heats_safe", _raise)
    else:
        monkeypatch.setattr(mod, "_load_concept_heats_safe",
                            lambda _cm: list(heats or []))

    monkeypatch.setattr(mod, "_load_industry_cache_safe",
                        lambda: dict(industry_map or {}))

    if filter_concepts_raises:
        def _raise(_lst):
            raise RuntimeError("filter fail")
        monkeypatch.setattr(mod, "_filter_concepts_safe", _raise)


def test_2_4_unit_024_enrich_helper_ranking_priority(monkeypatch):
    """P0 | ranking-data hit → top_concepts/industry pulled directly from ranking row."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts

    _stub_concept_modules(monkeypatch)  # all caches empty (must NOT be needed)
    hits_data = _build_hits_data({"code": "600519", "name": "贵州茅台"})
    ranking = {"ranking": [{
        "code": "600519",
        "top_concepts": ["白酒", "食品饮料"],
        "industry": "白酒",
    }]}
    enrich_screener_hits_with_concepts(hits_data, ranking)
    assert hits_data["hits"][0]["top_concepts"] == ["白酒", "食品饮料"]
    assert hits_data["hits"][0]["industry"] == "白酒"


def test_2_4_unit_025_enrich_helper_cache_fallback(monkeypatch):
    """P0 | ranking miss → top_concepts pulled from concept_cache + limit_up_cache aggregation."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    from src.engine.concept_stats import ConceptHeat

    c_map = {"600519": ["白酒", "食品饮料", "次新股"]}
    heats = [
        ConceptHeat(name="白酒", limit_up_count=8, max_board=4),
        ConceptHeat(name="食品饮料", limit_up_count=5, max_board=2),
    ]
    _stub_concept_modules(monkeypatch, c_map=c_map, heats=heats)

    hits_data = _build_hits_data({"code": "600519", "name": "贵州茅台"})
    ranking = {"ranking": [{"code": "999999", "top_concepts": ["其他"]}]}  # no match
    enrich_screener_hits_with_concepts(hits_data, ranking)

    # Cache-fallback yields top_n=2 ordered by heat
    assert hits_data["hits"][0]["top_concepts"] == ["白酒", "食品饮料"]


def test_2_4_unit_026_enrich_helper_all_fallback_empty(monkeypatch):
    """P0 [BLIND-BOUNDARY-007 / BLIND-FLOW-002] | unknown code + ranking=None + no caches → top_concepts=[] industry=None."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch)

    hits_data = _build_hits_data({"code": "999999", "name": "未知股"})
    enrich_screener_hits_with_concepts(hits_data, None)
    assert hits_data["hits"][0]["top_concepts"] == []
    assert hits_data["hits"][0]["industry"] is None


def test_2_4_unit_027_enrich_helper_industry_cache_fallback(monkeypatch):
    """P0 | industry_cache hit → hit.industry filled (BR-3.3 priority 2)."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch, industry_map={"600519": "白酒"})

    hits_data = _build_hits_data({"code": "600519", "name": "贵州茅台"})
    enrich_screener_hits_with_concepts(hits_data, None)  # ranking absent
    assert hits_data["hits"][0]["industry"] == "白酒"


def test_2_4_unit_028_enrich_helper_meta_concept_filtered(monkeypatch):
    """P0 | top_concepts double-filter excludes meta-tags (BR-3.4)."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    from src.engine.concept_stats import ConceptHeat

    # Heats include meta-tags too
    heats = [
        ConceptHeat(name="白酒", limit_up_count=8),
        ConceptHeat(name="沪股通", limit_up_count=12),     # meta - should be filtered
        ConceptHeat(name="融资融券", limit_up_count=10),    # meta - should be filtered
    ]
    c_map = {"600519": ["沪股通", "白酒", "融资融券"]}
    _stub_concept_modules(monkeypatch, c_map=c_map, heats=heats)

    hits_data = _build_hits_data({"code": "600519", "name": "贵州茅台"})
    enrich_screener_hits_with_concepts(hits_data, None)
    assert hits_data["hits"][0]["top_concepts"] == ["白酒"], \
        "Meta-concepts (沪股通/融资融券) must be filtered out"


def test_2_4_unit_029_enrich_helper_signature():
    """P1 | enrich_screener_hits_with_concepts has signature (hits_data, ranking_data) -> None."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    sig = inspect.signature(enrich_screener_hits_with_concepts)
    params = list(sig.parameters)
    assert params == ["hits_data", "ranking_data"], \
        f"Signature parameters {params} must be ['hits_data', 'ranking_data']"
    # Return annotation should be None (or Optional[None] equivalent)
    ret = sig.return_annotation
    assert ret is None or ret is type(None) or str(ret) in ("None", "<class 'NoneType'>"), \
        f"Return annotation '{ret}' should be None"


def test_2_4_unit_030_enrich_helper_in_place_mutate(monkeypatch):
    """P1 | enrich helper mutates hits_data in place; returns None; same id."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch)

    hits_data = _build_hits_data({"code": "600519", "name": "贵州茅台"})
    before_id = id(hits_data)
    before_hits_id = id(hits_data["hits"])

    result = enrich_screener_hits_with_concepts(hits_data, None)
    assert result is None
    assert id(hits_data) == before_id
    assert id(hits_data["hits"]) == before_hits_id
    # Mutation happened
    assert "top_concepts" in hits_data["hits"][0]
    assert "industry" in hits_data["hits"][0]


def test_2_4_unit_031_enrich_helper_top_concepts_always_list(monkeypatch):
    """P1 | hit.top_concepts is always list[str], even when fallback fails (never None/dict)."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch)

    hits_data = _build_hits_data(
        {"code": "999999"},
        {"code": "888888"},
    )
    enrich_screener_hits_with_concepts(hits_data, None)
    for h in hits_data["hits"]:
        assert isinstance(h["top_concepts"], list)
        # All items must be str
        for c in h["top_concepts"]:
            assert isinstance(c, str)


def test_2_4_unit_032_enrich_helper_industry_str_or_none(monkeypatch):
    """P1 | hit.industry is exactly str or None (never empty str / 0 / dict)."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch, industry_map={"600519": "白酒"})

    hits_data = _build_hits_data(
        {"code": "600519"},
        {"code": "999999"},
    )
    enrich_screener_hits_with_concepts(hits_data, None)
    for h in hits_data["hits"]:
        assert h["industry"] is None or isinstance(h["industry"], str)
        if isinstance(h["industry"], str):
            assert h["industry"] != "", "industry must not be empty string"


def test_2_4_unit_033_screener_hit_dataclass_unchanged_for_concept_industry():
    """P1 | ScreenerHit dataclass NOT extended with top_concepts/industry (BR-3.6)."""
    from src.engine.screener import ScreenerHit
    fields = set(ScreenerHit.__dataclass_fields__.keys())
    assert "top_concepts" not in fields, \
        "ScreenerHit dataclass must NOT contain top_concepts (runtime dict mutate only)"
    assert "industry" not in fields, \
        "ScreenerHit dataclass must NOT contain industry (runtime dict mutate only)"


def test_2_4_int_001_scheduler_writes_enriched_hits(tmp_path, monkeypatch):
    """P0 INT | Mimic scheduler's persist block; verify enrich + write produces top_concepts/industry."""
    from dataclasses import asdict
    from src.engine.screener import ScreenerHit
    from src.engine import screener_concept_enrich as enrich_mod
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts

    # Pre-populate ranking + caches in tmp DATA_DIR
    (tmp_path / "latest_ranking.json").write_text(json.dumps({
        "ranking": [{
            "code": "600519", "name": "贵州茅台",
            "top_concepts": ["白酒", "食品饮料"], "industry": "白酒",
        }],
    }, ensure_ascii=False), encoding="utf-8")

    # Force enrich helper to read from tmp_path (DATA_DIR override at import time)
    monkeypatch.setattr(enrich_mod, "DATA_DIR", tmp_path)

    sample = ScreenerHit(
        code="600519", name="贵州茅台", continuous_limit_up=2,
        open_price=104.0, auction_gain=4.0, auction_turnover=0.5,
        auction_amount=2000.0, auction_volume_lots=20000.0,
        auction_volume_ratio=2.0, market_cap=25.0, volume_ratio=2.0,
        gain_10d=5.0, matched_cycle=False,
    )
    hits_data = {
        "date": "2026-05-08 09:27:00",
        "hits": [asdict(sample)],
    }

    # Replay scheduler block: load ranking → call enrich → write file
    rank_file = tmp_path / "latest_ranking.json"
    ranking_data = json.loads(rank_file.read_text(encoding="utf-8")) if rank_file.exists() else None
    enrich_screener_hits_with_concepts(hits_data, ranking_data)

    out_path = tmp_path / "latest_screener.json"
    out_path.write_text(json.dumps(hits_data, ensure_ascii=False, indent=2), encoding="utf-8")

    out = json.loads(out_path.read_text(encoding="utf-8"))
    assert "hits" in out
    assert len(out["hits"]) == 1
    h = out["hits"][0]
    assert "top_concepts" in h
    assert "industry" in h
    assert h["top_concepts"] == ["白酒", "食品饮料"]
    assert h["industry"] == "白酒"


def test_2_4_int_002_scheduler_calls_enrich_before_write():
    """P1 INT | scheduler.py contains enrich call AND its line-no < latest_screener.json write line."""
    text = SCHEDULER_PY.read_text(encoding="utf-8")
    assert "enrich_screener_hits_with_concepts(" in text, \
        "scheduler.py must call enrich_screener_hits_with_concepts"

    lines = text.splitlines()
    enrich_line = None
    write_line = None
    for i, ln in enumerate(lines, start=1):
        if "enrich_screener_hits_with_concepts(" in ln and enrich_line is None:
            enrich_line = i
        if "latest_screener.json" in ln and "write_text" in ln and write_line is None:
            write_line = i
        if "latest_screener.json" in ln and "dump_json_file" in ln and write_line is None:
            write_line = i
        # Look-ahead: write_text may be on the next line
    # Re-scan for write_text / dump_json_file since call may span lines
    if write_line is None:
        for i, ln in enumerate(lines, start=1):
            if 'latest_screener.json' in ln:
                # check if "write_text" or "dump_json_file" appears on same or next 2 lines
                window = "\n".join(lines[i - 1:i + 2])
                if "write_text" in window or "dump_json_file" in window:
                    write_line = i
                    break
    assert enrich_line is not None
    assert write_line is not None
    assert enrich_line < write_line, \
        f"enrich call (line {enrich_line}) must precede write_text (line {write_line})"


def test_2_4_int_003_scheduler_loads_ranking_data_with_fallback():
    """P1 INT [BLIND-ERROR-006] | scheduler.py loads latest_ranking.json with try/except → None fallback."""
    text = SCHEDULER_PY.read_text(encoding="utf-8")

    # Find a block that:
    #   1. References "latest_ranking.json"
    #   2. References ranking_data (the local var name)
    #   3. Has a try/except around the read
    #   4. Falls back to None on failure
    # Use a flexible regex that allows whitespace/newlines.
    pattern = re.compile(
        r"ranking_data\s*[:=].*?try:\s*"
        r".*?latest_ranking\.json.*?"
        r"except.*?:\s*"
        r".*?ranking_data\s*=\s*None",
        re.DOTALL,
    )
    assert pattern.search(text), \
        "scheduler.py must contain ranking_data load with try/except fallback to None"


# ============================================================
# AC4: dashboard helpers prefer hit field over ranking lookup
# ============================================================


def test_2_4_unit_034_topConceptsOf_uses_hit_first():
    """P0 | topConceptsOf reads hit.top_concepts via screenerHits before falling back to ranking."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    # Locate the topConceptsOf function body
    fn = re.search(
        r"function topConceptsOf\([^)]*\)\s*\{(.*?)^\s*\}",
        text, re.DOTALL | re.MULTILINE,
    )
    assert fn, "topConceptsOf function not found"
    body = fn.group(1)
    assert "screenerHits.value" in body or "hit.top_concepts" in body, \
        "topConceptsOf must consult screenerHits/hit.top_concepts (hit-first lookup)"


def test_2_4_unit_035_topConceptsOf_retains_ranking_fallback():
    """P0 | topConceptsOf still references ranking.value as fallback."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    fn = re.search(
        r"function topConceptsOf\([^)]*\)\s*\{(.*?)^\s*\}",
        text, re.DOTALL | re.MULTILINE,
    )
    assert fn
    body = fn.group(1)
    assert "ranking.value" in body or "r?.top_concepts" in body, \
        "topConceptsOf must retain ranking fallback for compatibility"


def test_2_4_unit_036_industryOf_uses_hit_first():
    """P0 | industryOf reads hit.industry first (hit-first lookup)."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    fn = re.search(
        r"function industryOf\([^)]*\)\s*\{(.*?)^\s*\}",
        text, re.DOTALL | re.MULTILINE,
    )
    assert fn, "industryOf function not found"
    body = fn.group(1)
    assert "hit.industry" in body, \
        "industryOf must reference hit.industry (hit-first)"


def test_2_4_unit_037_industryOf_retains_dash_default():
    """P0 | industryOf still has '-' final fallback."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    fn = re.search(
        r"function industryOf\([^)]*\)\s*\{(.*?)^\s*\}",
        text, re.DOTALL | re.MULTILINE,
    )
    assert fn
    body = fn.group(1)
    assert "'-'" in body, "industryOf must retain '-' final fallback"


def test_2_4_unit_038_concept_column_branch_sha256_unchanged():
    """P1 | Board column template SHA256 unchanged (BR-4.3 — only helper bodies move)."""
    baseline = _load_baseline()
    region = baseline["regions"]["board_column"]
    actual = _sha256(_read_lines(INDEX_HTML, region["start"], region["end"]))
    assert actual == region["sha256"], \
        f"Board column SHA256 mismatch (lines {region['start']}-{region['end']})"


def test_2_4_unit_039_hitLive_industry_fallback_preserved():
    """P1 | hitLive(hit.code).industry fallback chain preserved in board column (BR-4.4)."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    assert "hitLive(hit.code).industry" in text, \
        "hitLive(hit.code).industry chain must remain in board column template"


def test_2_4_unit_040_topConceptsOf_remains_concise():
    """P2 | topConceptsOf body remains <= 8 lines (BR-4.5: no new fetch / state)."""
    text = INDEX_HTML.read_text(encoding="utf-8")
    fn = re.search(
        r"function topConceptsOf\([^)]*\)\s*\{(.*?)^\s*\}",
        text, re.DOTALL | re.MULTILINE,
    )
    assert fn
    body_lines = [ln for ln in fn.group(1).splitlines() if ln.strip()]
    assert len(body_lines) <= 8, \
        f"topConceptsOf body has {len(body_lines)} non-blank lines; must be <= 8"


# ============================================================
# AC5: regression / DoD
# ============================================================


def test_2_4_unit_041_run_screener_empty_hits_unchanged(monkeypatch):
    """P0 | hits=[] → enrich helper preserves {date, hits:[]} structure (no key inflation)."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch)

    hits_data = {"date": "2026-05-08 09:27:00", "hits": []}
    before = deepcopy(hits_data)
    enrich_screener_hits_with_concepts(hits_data, None)
    assert hits_data == before, \
        "Empty hits must remain {date, hits:[]} structure unchanged"
    # Round-trip through JSON unchanged
    s = json.dumps(hits_data, ensure_ascii=False)
    assert json.loads(s) == before


def test_2_4_unit_042_existing_99_tests_green():
    """P0 | tests/notify/* (46+48+5=99) all pass (regression: BR-5.1 + email + decision)."""
    cmd = [
        sys.executable, "-m", "pytest",
        "tests/notify/test_email_decision_alignment.py",
        "tests/notify/test_decision_consistency.py",
        "tests/notify/test_email_fallback_industry_concept.py",
        "-q", "--no-header", "--tb=line", "-p", "no:cacheprovider",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=240,
        cwd=str(PROJECT_ROOT),
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, \
        f"tests/notify/ regression suite failed:\n{out}"
    m = re.search(r"(\d+) passed", out)
    assert m, f"Cannot parse passed count from output:\n{out}"
    passed = int(m.group(1))
    assert passed >= 99, \
        f"Expected ≥99 passed, got {passed}:\n{out}"


def test_2_4_unit_043_send_screener_report_signature_baseline():
    """P1 | send_screener_report signature unchanged (email-sync-1.1 baseline @ eb4e883)."""
    from src.notify.email_sender import send_screener_report
    sig = inspect.signature(send_screener_report)
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
    params = list(sig.parameters.items())
    assert len(params) == len(expected), \
        f"send_screener_report has {len(params)} params; expected {len(expected)}"
    for (name, param), (exp_name, exp_default) in zip(params, expected):
        assert name == exp_name, f"Param name {name} != {exp_name}"
        if exp_default is inspect.Parameter.empty:
            assert param.default is inspect.Parameter.empty
        else:
            assert param.default == exp_default
    assert sig.return_annotation is bool


def test_2_4_unit_044_run_screener_signature_baseline():
    """P1 | run_screener / run_screener_update signatures unchanged (BR-5.1)."""
    from src.engine.screener import run_screener
    from src.scheduler import run_screener_update

    sig_rs = inspect.signature(run_screener)
    rs_params = list(sig_rs.parameters)
    assert rs_params == ["realtime_df", "limit_up_history", "cycle_codes"], \
        f"run_screener signature changed: {rs_params}"
    assert sig_rs.parameters["cycle_codes"].default is None

    sig_ru = inspect.signature(run_screener_update)
    # Story anti-duplicate-email-2.5 AC1: 加 skip_email 参数（缺省 None → 9:27 时间窗口判断）
    # cron job (main.py:37-45) 仍零参调用，AC6 保证字符级行为一致
    ru_params = list(sig_ru.parameters)
    assert ru_params == ["skip_email"], \
        f"run_screener_update signature drift: expected ['skip_email'], got {ru_params}"
    assert sig_ru.parameters["skip_email"].default is None, \
        "skip_email must default to None (per AC1, AC4, AC6)"


def test_2_4_unit_045_latest_screener_json_schema_15_fields(tmp_path, monkeypatch):
    """P1 [BLIND-DATA-001] | enriched hit dict has 13 existing + 2 new = 15 keys (BR-5.5)."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch)

    sample = _build_screener_hit()
    hits_data = _build_hits_data(asdict(sample))
    enrich_screener_hits_with_concepts(hits_data, None)

    expected_existing = {
        "code", "name", "continuous_limit_up", "open_price",
        "auction_gain", "auction_turnover", "auction_amount",
        "auction_volume_lots", "auction_volume_ratio",
        "market_cap", "volume_ratio", "gain_10d", "matched_cycle",
    }
    expected_new = {"top_concepts", "industry"}
    keys = set(hits_data["hits"][0].keys())
    missing_existing = expected_existing - keys
    missing_new = expected_new - keys
    assert not missing_existing, f"Missing existing keys: {missing_existing}"
    assert not missing_new, f"Missing new keys: {missing_new}"
    assert len(keys) == 15, f"Expected 15 keys, got {len(keys)}: {sorted(keys)}"


def test_2_4_unit_046_enrich_helper_idempotent(monkeypatch):
    """P1 [BLIND-FLOW-001] | enrich helper is idempotent: 2nd call yields character-equal result."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    from src.engine.concept_stats import ConceptHeat

    c_map = {"600519": ["白酒", "食品饮料"]}
    heats = [ConceptHeat(name="白酒", limit_up_count=8),
             ConceptHeat(name="食品饮料", limit_up_count=5)]
    _stub_concept_modules(monkeypatch, c_map=c_map, heats=heats,
                          industry_map={"600519": "白酒"})

    hits1 = _build_hits_data({"code": "600519", "name": "贵州茅台"})
    hits2 = deepcopy(hits1)

    enrich_screener_hits_with_concepts(hits1, None)
    enrich_screener_hits_with_concepts(hits2, None)
    enrich_screener_hits_with_concepts(hits2, None)  # 2nd call

    s1 = json.dumps(hits1, ensure_ascii=False, sort_keys=True)
    s2 = json.dumps(hits2, ensure_ascii=False, sort_keys=True)
    assert s1 == s2, "enrich helper is NOT idempotent"


def test_2_4_unit_047_cross_validator_top_concepts_unchanged():
    """P2 | Deviation table 's.top_concepts' path preserved.

    Note: Test design referenced 'src/api/cross_validator.py' (incorrect path).
    Actual `s.top_concepts` literal lives in src/static/index.html (deviation table at
    line ~711-712 of current HEAD), and the data flows from market.limit_up_flat_list
    (assembled by src/engine/cross_validator.py and surfaced via /api/cross_validation
    or similar). This test asserts the index.html literal is unchanged AND the engine
    cross_validator.py file exists.
    """
    text = INDEX_HTML.read_text(encoding="utf-8")
    # The deviation table uses `s.top_concepts` directly (not topConceptsOf)
    assert "(s.top_concepts || []).length" in text, \
        "Deviation table v-if `(s.top_concepts || []).length` literal must remain"
    assert "(s.top_concepts || []).join('/')" in text, \
        "Deviation table top_concepts join expression must remain"
    # Also verify cross_validator.py exists at engine path (sanity)
    assert CROSS_VALIDATOR_PY.exists(), \
        "src/engine/cross_validator.py must exist"


# ============================================================
# Additional [BLIND-SPOT] scenarios
# ============================================================


def test_2_4_blind_error_001_concept_cache_missing(monkeypatch):
    """P1 [BLIND-ERROR-001] | concept_cache absent → top_concepts=[], no exception."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch)  # all empty (simulates missing cache)
    hits_data = _build_hits_data({"code": "600519"})
    enrich_screener_hits_with_concepts(hits_data, None)
    assert hits_data["hits"][0]["top_concepts"] == []


def test_2_4_blind_error_002_concept_cache_corrupted_json(monkeypatch):
    """P1 [BLIND-ERROR-002] | concept_cache loader raises → helper swallows + top_concepts=[]."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch, ranking_loader_raises=True)
    hits_data = _build_hits_data({"code": "600519"})
    # Should not raise
    enrich_screener_hits_with_concepts(hits_data, None)
    assert hits_data["hits"][0]["top_concepts"] == []


def test_2_4_blind_error_003_limit_up_cache_corrupted(monkeypatch):
    """P1 [BLIND-ERROR-003] | limit_up_cache loader raises → heats unavailable; helper survives."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(
        monkeypatch,
        c_map={"600519": ["白酒"]},
        limit_up_loader_raises=True,
    )
    hits_data = _build_hits_data({"code": "600519"})
    enrich_screener_hits_with_concepts(hits_data, None)
    # No heats means top_concepts_for_stock returns [] (no rank_map matches)
    assert hits_data["hits"][0]["top_concepts"] == []


def test_2_4_blind_error_004_industry_cache_missing(monkeypatch):
    """P1 [BLIND-ERROR-004] | industry_cache absent + ranking miss → industry=None."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch)  # industry_map={} simulates missing cache
    hits_data = _build_hits_data({"code": "600519"})
    enrich_screener_hits_with_concepts(hits_data, None)
    assert hits_data["hits"][0]["industry"] is None


def test_2_4_blind_error_005_filter_concepts_raises(monkeypatch):
    """P2 [BLIND-ERROR-005] | filter_concepts raises → that hit gets [], others survive."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    from src.engine.concept_stats import ConceptHeat

    _stub_concept_modules(
        monkeypatch,
        c_map={"600519": ["白酒"], "000001": ["银行"]},
        heats=[ConceptHeat(name="白酒", limit_up_count=8),
               ConceptHeat(name="银行", limit_up_count=5)],
        filter_concepts_raises=True,
    )

    hits_data = _build_hits_data({"code": "600519"}, {"code": "000001"})
    enrich_screener_hits_with_concepts(hits_data, None)
    # Both hits should be present + top_concepts=[] (filter failed silently per BR-3.5)
    for h in hits_data["hits"]:
        assert h["top_concepts"] == []


def test_2_4_blind_flow_003_hits_key_missing(monkeypatch):
    """P2 [BLIND-FLOW-003] | hits_data without 'hits' key → helper does not raise."""
    from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
    _stub_concept_modules(monkeypatch)
    hits_data = {"date": "2026-05-08"}  # no "hits" key
    # Must not raise
    enrich_screener_hits_with_concepts(hits_data, None)

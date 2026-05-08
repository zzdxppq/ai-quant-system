"""Test for Story data-dir-import-fix-1.1: email_sender fallback paths.

Validates AC2/AC3 — the industry_cache.json + limit_up_cache.json fallbacks
in `_build_html` are reachable after restoring `import json` + `from src.config
import DATA_DIR`. Before the fix these blocks raised NameError silently swallowed
by outer `try/except Exception: pass`, so the cache files on disk were never read.

Run:
    pytest tests/notify/test_email_fallback_industry_concept.py -v
"""
import json

from src.notify import email_sender
from src.notify.email_sender import _build_html, _calc_daily_advice


def _minimal_advice():
    """跑真函数得到完整 advice dict（含 bg/text/color 等所有字段）。"""
    return _calc_daily_advice(_minimal_sentiment(), _minimal_leader())


def _minimal_sentiment():
    return {
        "market": {
            "limit_down": 0,
            "drop_over_9pct": 0,
            "prev_day_limit_down": 0,
        },
        "weighted_auction_gain": 0,
        "prev_day_weighted_auction_gain": 0,
    }


def _minimal_leader():
    return {
        "main_board_leaders": [],
        "yesterday_main_board_avg_auction": {},
        "yesterday_zb_today_auction": {},
        "yesterday_limit_down_today_auction": {},
    }


def _hits_one(code="600519", name="贵州茅台"):
    return [{
        "code": code, "name": name,
        "open_price": 1500.0,
        "auction_gain": 2.5,
        "market_cap": 18000.0,
        "continuous_limit_up": 2,
    }]


def _ranking_no_industry(code="600519"):
    return {"ranking": [{"code": code}]}


def _render(tmp_path, monkeypatch, *, ranking=None, hits=None, no_concepts=False):
    """Helper: 把 DATA_DIR 重定向到 tmp_path 后调 _build_html。

    no_concepts=True: 屏蔽 concept_fetcher 返回空 dict，
    用于 AC2 隔离测试（避免概念路径意外输出"白酒"干扰断言）。
    """
    monkeypatch.setattr(email_sender, "DATA_DIR", tmp_path)
    if no_concepts:
        monkeypatch.setattr(
            "src.data.concept_fetcher.load_stock_to_concepts",
            lambda: {},
        )
    return _build_html(
        cycle_phase="孕育期", cycle_day=1,
        leader=_minimal_leader(),
        hits=hits if hits is not None else _hits_one(),
        signals=[],
        sentiment_data=_minimal_sentiment(),
        ranking_data=ranking if ranking is not None else _ranking_no_industry(),
        advice=_minimal_advice(),
    )


# ============================================================
# AC2: industry_cache.json fallback (verifies `from src.config import DATA_DIR`
#      + `import json` actually take effect on the industry block)
# ============================================================

def test_ac2_industry_cache_fallback_renders_real_industry(tmp_path, monkeypatch):
    """主路径：磁盘上有 industry_cache.json，ranking 缺 industry → 板块列显示真实行业名。"""
    cache = tmp_path / "industry_cache.json"
    cache.write_text(json.dumps({"600519": "白酒"}, ensure_ascii=False))

    html = _render(tmp_path, monkeypatch, no_concepts=True)
    assert "白酒" in html, (
        "AC2 fallback failed: industry_cache.json 中的 '白酒' 未出现在 HTML — "
        "说明 industry_map 兜底块未真正激活（NameError 被吞？）"
    )


def test_ac2_industry_cache_missing_degrades_to_dash(tmp_path, monkeypatch):
    """BR-2.1 降级路径：cache 文件不存在 → 不抛错 + 板块列降级 '-'。"""
    # tmp_path 下不写 industry_cache.json
    html = _render(tmp_path, monkeypatch, no_concepts=True)
    # _render_concept_industry_cell 在 top_concepts=[] + industry='-' 时
    # 渲染为 <span style="color:#a0aec0;">-</span>
    assert ">-</span>" in html, (
        "AC2 degrade failed: 板块列应在 cache 缺失时降级为 '-' 占位"
    )
    # 关键：'白酒'/任何业内名不应出现（因没有任何数据源提供它）
    assert "白酒" not in html


def test_ac2_ranking_industry_overrides_cache_fallback(tmp_path, monkeypatch):
    """BR-2.2: ranking 已注入 industry 时优先级高于 fallback（既有逻辑）。"""
    # cache 提供 "白酒"
    cache = tmp_path / "industry_cache.json"
    cache.write_text(json.dumps({"600519": "白酒"}, ensure_ascii=False))
    # ranking 提供 "食品饮料"，应覆盖 cache
    ranking = {"ranking": [{"code": "600519", "industry": "食品饮料"}]}
    html = _render(tmp_path, monkeypatch, ranking=ranking, no_concepts=True)
    assert "食品饮料" in html
    assert "白酒" not in html, "ranking.industry 必须覆盖 cache fallback（fallback 在前注入在后）"


# ============================================================
# AC3: limit_up_cache.json fallback (verifies `import json` works inside
#      the inner try block at lines 446-451)
# ============================================================

def test_ac3_limit_up_cache_fallback_renders_top_concepts(tmp_path, monkeypatch):
    """主路径：磁盘有 limit_up_cache.json + concept_fetcher 提供概念 → 概念列显示真实概念。"""
    lu_file = tmp_path / "limit_up_cache.json"
    lu_file.write_text(json.dumps({
        "2026-05-08": [
            {"code": "600519", "name": "贵州茅台",
             "board_count": 2, "change_pct": 10.0},
        ]
    }, ensure_ascii=False))

    monkeypatch.setattr(email_sender, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        "src.data.concept_fetcher.load_stock_to_concepts",
        lambda: {"600519": ["白酒", "消费"]},
    )

    html = _build_html(
        cycle_phase="孕育期", cycle_day=1,
        leader=_minimal_leader(), hits=_hits_one(), signals=[],
        sentiment_data=_minimal_sentiment(),
        ranking_data=_ranking_no_industry(),
        advice=_minimal_advice(),
    )
    # aggregate_concept_limit_ups 真实跑出 → top_concepts_for_stock 返回 ["白酒"] 或 ["白酒","消费"]
    # 关键：白酒必须出现在概念位（如果 fallback 被吞，c_map 也用不了，concept 列只剩 industry='-'）
    assert "白酒" in html, (
        "AC3 fallback failed: limit_up_cache + concept_fetcher 应让 '白酒' 进入概念列 — "
        "未出现说明 inner try 的 `json.loads(lu_file.read_text())` NameError 仍被吞"
    )


def test_ac3_limit_up_cache_missing_degrades_silently(tmp_path, monkeypatch):
    """BR-3.1 降级路径：limit_up_cache.json 不存在 → heats=[] →
    top_concepts_for_stock 返回 []（因 rank_map 为空），
    最终概念列只剩 industry，不抛错。"""
    monkeypatch.setattr(email_sender, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        "src.data.concept_fetcher.load_stock_to_concepts",
        lambda: {"600519": ["白酒", "消费"]},
    )

    # 不应抛错
    html = _build_html(
        cycle_phase="孕育期", cycle_day=1,
        leader=_minimal_leader(), hits=_hits_one(), signals=[],
        sentiment_data=_minimal_sentiment(),
        ranking_data=_ranking_no_industry(),
        advice=_minimal_advice(),
    )
    # heats 为空 → top_concepts_for_stock 返回 [] → 概念位不出现 "白酒"
    # 板块位降级为 '-'（无 industry_cache.json，ranking 也无 industry）
    assert "白酒" not in html
    assert ">-</span>" in html

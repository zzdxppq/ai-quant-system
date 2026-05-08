"""Tests for Story watch-pool-snapshot-2.2: 次日观察池显示昨日 15:45 冻结快照

Implements 32 designed scenarios (34 collected — UNIT-009 parametrize ×3).
Test Design: docs/qa/assessments/watch-pool-snapshot-2.2-test-design-20260508.md

Run:
    pytest tests/test_review_watch_pool_snapshot.py -v
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TZ_CN = timezone(timedelta(hours=8))


# ============================================================
# Shared fixtures
# ============================================================


@pytest.fixture
def baselines_path():
    """Path to SHA256 baseline JSON (frozen at T1 commit time)."""
    return Path(__file__).parent / "fixtures" / "watch_pool_snapshot_baselines.json"


@pytest.fixture
def fixture_a_yesterday_history():
    """夹具 A：昨日 history entry，watch_pool=[{code:'002081',...}]。

    返回 list（review_history.json 顶层是 list of entries）。
    含 17 个 DailyReview 字段，watch_pool 长度 = 1。
    """
    return [
        {
            "date": "2026-05-07",
            "limit_up_count": 30,
            "main_board_limit_up": 25,
            "sector_groups": {},
            "main_theme": "AI",
            "theme_strength": "强",
            "lianban_ladder": [],
            "highest_board": 5,
            "prev_board_groups": [],
            "sector_zt_stats": [],
            "concept_zt_stats": [],
            "failed_promotion_list": [],
            "watch_pool": [
                {
                    "code": "002081",
                    "name": "金螳螂",
                    "board_count": 2,
                    "industry": "建筑装饰",
                    "close": 5.12,
                    "market_cap_yi": 80.0,
                    "total_gain_pct": 21.0,
                    "reason": "昨日2板",
                    "watch_points": "次日竞价 +3%~+5%",
                    "auction_range": "+3%~+5%",
                    "top_concepts": ["AI", "建筑"],
                    "is_main_board": True,
                    "pool_tag": "高位接力",
                }
            ],
            "market_summary": "情绪偏强",
            "scorecard": {},
            "relay_env": {},
            "promotion_summary": [],
        }
    ]


@pytest.fixture
def fixture_b_today_latest():
    """夹具 B：今日 latest_review.json，watch_pool=[{code:'600519',...}]"""
    return {
        "date": "2026-05-08",
        "limit_up_count": 50,
        "main_board_limit_up": 40,
        "sector_groups": {},
        "main_theme": "新能源",
        "theme_strength": "中",
        "lianban_ladder": [],
        "highest_board": 6,
        "prev_board_groups": [],
        "sector_zt_stats": [],
        "concept_zt_stats": [],
        "failed_promotion_list": [],
        "watch_pool": [
            {
                "code": "600519",
                "name": "贵州茅台",
                "board_count": 3,
                "industry": "白酒",
                "close": 1800.0,
                "market_cap_yi": 22000.0,
                "total_gain_pct": 30.0,
                "reason": "今日3板",
                "watch_points": "次日竞价 +2%~+4%",
                "auction_range": "+2%~+4%",
                "top_concepts": ["白酒"],
                "is_main_board": True,
                "pool_tag": "高位接力",
            }
        ],
        "market_summary": "新能源主线",
        "scorecard": {},
        "relay_env": {},
        "promotion_summary": [],
    }


@pytest.fixture
def fixture_c_ranking_recompute():
    """夹具 C：latest_ranking.json — 若旧代码用此重算，会得 [{code:'000001',...}]。

    Story 2.2 删除 line 336-343 后，该夹具内容**不再**影响 /api/review 的 watch_pool。
    保留以验证"删除生效后 ranking 内容不再造成观察池漂移"。
    """
    return {
        "ranking": [
            {
                "code": "000001",
                "name": "平安银行",
                "gain_10d": 50.0,
                "continuous_limit_up": 3,
                "is_main_board": True,
                "industry": "金融",
                "close": 12.5,
                "market_cap_yi": 200.0,
                "total_gain_pct": 25.0,
            }
        ]
    }


@pytest.fixture
def patched_data_dir(tmp_path, monkeypatch):
    """Replace src.api.app.DATA_DIR with tmp_path。

    返回 tmp_path 供调用方写入 latest_review.json / review_history.json / latest_ranking.json。
    """
    monkeypatch.setattr("src.api.app.DATA_DIR", tmp_path)
    return tmp_path


def _patch_now(monkeypatch, *, year=2026, month=5, day=8, hour=9, minute=30, second=0):
    """Patch src.config.now_cn (NOT src.api.app.now_cn — function-local import 在 get_review 中再次拉取)。"""
    target = datetime(year, month, day, hour, minute, second, tzinfo=TZ_CN)
    monkeypatch.setattr("src.config.now_cn", lambda: target)


@pytest.fixture
def client():
    """TestClient — 每个 test 内部拿到全新实例避免状态泄漏。"""
    from src.api.app import app

    return TestClient(app)


def _write_history(tmp_path: Path, entries):
    (tmp_path / "review_history.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_latest(tmp_path: Path, payload):
    (tmp_path / "latest_review.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_ranking(tmp_path: Path, payload):
    (tmp_path / "latest_ranking.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============================================================
# AC1: review API 不再覆盖 review_data["watch_pool"]
# ============================================================


class TestAC1NoWatchPoolOverride:
    """AC1: 删除 line 336-343 watch_pool 重算块；返回值透传 review_data['watch_pool']"""

    def test_2_2_unit_001_get_review_returns_history_watch_pool_verbatim(
        self,
        client,
        patched_data_dir,
        fixture_a_yesterday_history,
        fixture_c_ranking_recompute,
        monkeypatch,
    ):
        """2.2-UNIT-001 [P0/unit]: 响应 watch_pool == 昨日 history entry 的 watch_pool（不重算）"""
        _write_history(patched_data_dir, fixture_a_yesterday_history)
        _write_ranking(patched_data_dir, fixture_c_ranking_recompute)
        _patch_now(monkeypatch, hour=9, minute=30)

        r = client.get("/api/review")
        assert r.status_code == 200
        wp = r.json()["watch_pool"]
        assert wp == fixture_a_yesterday_history[-1]["watch_pool"]
        # 同时反向断言：watch_pool 不是基于 ranking 重算的（旧代码会取 000001）
        assert all(item["code"] != "000001" for item in wp)

    def test_2_2_unit_002_import_and_override_block_removed(self):
        """2.2-UNIT-002 [P1/unit]: app.py 源码不含 build_watch_pool_from_ranking import 与重算注释"""
        src = (PROJECT_ROOT / "src" / "api" / "app.py").read_text(encoding="utf-8")
        assert "build_watch_pool_from_ranking" not in src, (
            "app.py 仍引用 build_watch_pool_from_ranking — T0/T1 删除未完成"
        )
        assert "始终用最新 ranking 重算" not in src, (
            "app.py 仍含旧重算注释 — T1 删除未完成"
        )

    def test_2_2_unit_003_get_review_docstring_updated(self):
        """2.2-UNIT-003 [P1/unit]: get_review.__doc__ 不含旧语义陈述，含新冻结语义关键字"""
        from src.api.app import get_review

        doc = get_review.__doc__ or ""
        assert "始终用 latest_ranking 重算" not in doc, "docstring 仍声称实时重算"
        assert "15:45" in doc or "review_history" in doc, (
            "docstring 缺少冻结语义关键字（应含 '15:45' 或 'review_history'）"
        )

    def test_2_2_unit_004_strip_meta_concepts_still_applies_to_watch_pool(
        self, client, patched_data_dir, monkeypatch
    ):
        """2.2-UNIT-004 [P1/unit]: _strip_meta_concepts_inplace 对 watch_pool concepts 仍生效

        使用实际黑名单中的元标签（'融资融券' / '沪股通'）验证过滤未被误删。
        """
        history = [
            {
                "date": "2026-05-07",
                "watch_pool": [
                    {
                        "code": "002081",
                        "name": "金螳螂",
                        "concepts": ["融资融券", "半导体"],
                        "top_concepts": ["沪股通", "半导体"],
                    }
                ],
            }
        ]
        _write_history(patched_data_dir, history)
        _patch_now(monkeypatch, hour=9, minute=30)

        r = client.get("/api/review")
        assert r.status_code == 200
        wp = r.json()["watch_pool"]
        assert wp[0]["concepts"] == ["半导体"], "concepts 元标签未被过滤"
        assert wp[0]["top_concepts"] == ["半导体"], "top_concepts 元标签未被过滤"

    def test_2_2_int_001_route_passthrough_watch_pool_from_history(
        self,
        client,
        patched_data_dir,
        fixture_a_yesterday_history,
        fixture_c_ranking_recompute,
        monkeypatch,
    ):
        """2.2-INT-001 [P0/integration]: 完整 route GET /api/review 透传 watch_pool"""
        _write_history(patched_data_dir, fixture_a_yesterday_history)
        _write_ranking(patched_data_dir, fixture_c_ranking_recompute)
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        body = r.json()
        assert body["watch_pool"] == fixture_a_yesterday_history[-1]["watch_pool"]

    def test_2_2_int_002_scorecard_promotion_summary_still_recomputed(
        self, client, patched_data_dir, fixture_a_yesterday_history, monkeypatch
    ):
        """2.2-INT-002 [P0/integration]: scorecard / promotion_summary 仍由当前公式重算（line 339-353 保留）"""
        # 篡改 history 中 scorecard / promotion_summary 为"过期值"
        entry = dict(fixture_a_yesterday_history[-1])
        entry["scorecard"] = {"stale": True}
        entry["promotion_summary"] = [{"stale": True}]
        _write_history(patched_data_dir, [entry])
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        body = r.json()
        # scorecard / promotion_summary 由当前公式重算 → 不会保留 'stale' 键
        assert "stale" not in (body.get("scorecard") or {}), (
            "scorecard 未被重算（仍含 stale 键 = 历史快照原值）"
        )
        # promotion_summary 是列表；列表内任一元素都不应含 'stale' key
        assert all("stale" not in (item or {}) for item in (body.get("promotion_summary") or [])), (
            "promotion_summary 未被重算（含 stale 元素）"
        )


# ============================================================
# AC2: review_history.json 已含 watch_pool 快照（既有行为，本 Story 仅验收）
# ============================================================


class TestAC2HistoryHasWatchPool:
    """AC2: _save_review 既有行为锁定 — 写 latest_review.json + review_history.json 含 watch_pool"""

    def _build_review_with_watch_pool(self, date: str, codes: list[str]):
        """构造 DailyReview 含 watch_pool=[WatchCandidate(...)]"""
        from src.engine.daily_review import DailyReview, WatchCandidate

        watch_pool = [
            WatchCandidate(
                code=c,
                name=f"name_{c}",
                board_count=2,
                industry="测试",
                close=10.0,
                market_cap_yi=50.0,
                total_gain_pct=20.0,
                reason="单测",
                watch_points="单测观察点",
                auction_range="+3%~+5%",
            )
            for c in codes
        ]
        return DailyReview(date=date, watch_pool=watch_pool)

    def test_2_2_unit_005_save_review_writes_latest_with_watch_pool(self, tmp_path, monkeypatch):
        """2.2-UNIT-005 [P1/unit]: _save_review 写 latest_review.json 含 watch_pool 字段"""
        monkeypatch.setattr("src.engine.daily_review.DATA_DIR", tmp_path)
        from src.engine.daily_review import _save_review

        review = self._build_review_with_watch_pool("2026-05-08", ["002081"])
        _save_review(review)

        data = json.loads((tmp_path / "latest_review.json").read_text(encoding="utf-8"))
        assert "watch_pool" in data
        assert len(data["watch_pool"]) == 1
        assert data["watch_pool"][0]["code"] == "002081"

    def test_2_2_unit_006_save_review_appends_history_with_watch_pool(self, tmp_path, monkeypatch):
        """2.2-UNIT-006 [P1/unit]: _save_review 追加 review_history.json 末 entry 含 watch_pool"""
        monkeypatch.setattr("src.engine.daily_review.DATA_DIR", tmp_path)
        from src.engine.daily_review import _save_review

        review = self._build_review_with_watch_pool("2026-05-08", ["002081"])
        _save_review(review)

        history = json.loads((tmp_path / "review_history.json").read_text(encoding="utf-8"))
        assert isinstance(history, list)
        assert len(history) >= 1
        assert "watch_pool" in history[-1]
        assert history[-1]["watch_pool"][0]["code"] == "002081"

    def test_2_2_unit_007_save_review_dedup_same_date(self, tmp_path, monkeypatch):
        """2.2-UNIT-007 [P2/unit]: 同 date 调 _save_review 两次 → history 该 date 仅 1 条且为最后一次"""
        monkeypatch.setattr("src.engine.daily_review.DATA_DIR", tmp_path)
        from src.engine.daily_review import _save_review

        r1 = self._build_review_with_watch_pool("2026-05-08", ["002081"])
        r2 = self._build_review_with_watch_pool("2026-05-08", ["600519"])
        _save_review(r1)
        _save_review(r2)

        history = json.loads((tmp_path / "review_history.json").read_text(encoding="utf-8"))
        same_date = [h for h in history if h["date"] == "2026-05-08"]
        assert len(same_date) == 1, f"同日去重失败：{len(same_date)} 条"
        assert same_date[0]["watch_pool"][0]["code"] == "600519", "应保留最后一次写入"

    def test_2_2_int_003_real_history_last_entry_schema(self):
        """2.2-INT-003 [P2/integration]: 真实 data/review_history.json 末尾 entry watch_pool[0] 含 13 keys"""
        history_path = PROJECT_ROOT / "data" / "review_history.json"
        if not history_path.exists():
            pytest.skip("review_history.json absent in test env")
        history = json.loads(history_path.read_text(encoding="utf-8"))
        if not history:
            pytest.skip("review_history.json is empty")
        last_wp = history[-1].get("watch_pool", [])
        if not last_wp:
            pytest.skip(f"Last entry (date={history[-1].get('date')}) has empty watch_pool")
        expected_keys = {
            "code",
            "name",
            "board_count",
            "industry",
            "close",
            "market_cap_yi",
            "total_gain_pct",
            "reason",
            "watch_points",
            "auction_range",
            "top_concepts",
            "is_main_board",
            "pool_tag",
        }
        actual_keys = set(last_wp[0].keys())
        missing = expected_keys - actual_keys
        assert not missing, f"watch_pool[0] 缺失字段：{missing}"


# ============================================================
# AC3: 复盘页 D 区域 watch_pool 渲染保持冻结快照
# ============================================================


class TestAC3ReviewHtmlFrozen:
    """AC3: review.html 字符级冻结（前端零改动）"""

    def test_2_2_unit_008_review_html_sha256_matches_baseline(self, baselines_path):
        """2.2-UNIT-008 [P1/unit]: review.html SHA256 == baseline['review.html']"""
        actual = hashlib.sha256(
            (PROJECT_ROOT / "src" / "static" / "review.html").read_bytes()
        ).hexdigest()
        expected = json.loads(baselines_path.read_text(encoding="utf-8"))["review.html"]
        assert actual == expected, (
            f"review.html 内容已改动（违反 BR-3.1/3.2/3.3 字符级冻结）\n"
            f"  actual   = {actual}\n  expected = {expected}"
        )

    @pytest.mark.parametrize(
        "substring",
        [
            "watchPool = computed(() => review.value.watch_pool",
            "fetch('/api/review')",
            "<!-- ============ 区域 D",
        ],
    )
    def test_2_2_unit_009_review_html_required_substrings(self, substring):
        """2.2-UNIT-009 [P2/unit]: review.html 含必要子串（SHA256 失败时的诊断辅助）"""
        html = (PROJECT_ROOT / "src" / "static" / "review.html").read_text(encoding="utf-8")
        assert substring in html, f"review.html 缺失关键子串：{substring!r}"


# ============================================================
# AC4: scheduler / cron 流程不变（字符级冻结）
# ============================================================


class TestAC4SchedulerFrozen:
    """AC4: main.py / scheduler.py / daily_review.py 字符级冻结"""

    def test_2_2_unit_010_main_py_sha256_matches_baseline(self, baselines_path):
        """2.2-UNIT-010 [P1/unit]: main.py SHA256 == baseline"""
        actual = hashlib.sha256((PROJECT_ROOT / "main.py").read_bytes()).hexdigest()
        expected = json.loads(baselines_path.read_text(encoding="utf-8"))["main.py"]
        assert actual == expected, f"main.py 已改动\n  actual={actual}\n  expected={expected}"

    def test_2_2_unit_011_scheduler_py_sha256_matches_baseline(self, baselines_path):
        """2.2-UNIT-011 [P1/unit]: src/scheduler.py SHA256 == baseline"""
        actual = hashlib.sha256(
            (PROJECT_ROOT / "src" / "scheduler.py").read_bytes()
        ).hexdigest()
        expected = json.loads(baselines_path.read_text(encoding="utf-8"))["scheduler.py"]
        assert actual == expected, (
            f"scheduler.py 已改动\n  actual={actual}\n  expected={expected}"
        )

    def test_2_2_unit_012_daily_review_py_sha256_matches_baseline(self, baselines_path):
        """2.2-UNIT-012 [P1/unit]: src/engine/daily_review.py SHA256 == baseline"""
        actual = hashlib.sha256(
            (PROJECT_ROOT / "src" / "engine" / "daily_review.py").read_bytes()
        ).hexdigest()
        expected = json.loads(baselines_path.read_text(encoding="utf-8"))["daily_review.py"]
        assert actual == expected, (
            f"daily_review.py 已改动\n  actual={actual}\n  expected={expected}"
        )


# ============================================================
# AC5: 不引入回归（DoD）
# ============================================================


class TestAC5DoDRegression:
    """AC5: 跨 Story 回归保护"""

    def test_2_2_int_004_api_review_top_keys_baseline(
        self,
        client,
        patched_data_dir,
        fixture_a_yesterday_history,
        monkeypatch,
        baselines_path,
    ):
        """2.2-INT-004 [P0/integration]: GET /api/review 顶层字段集合 == baseline pinned set"""
        _write_history(patched_data_dir, fixture_a_yesterday_history)
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        actual_keys = set(r.json().keys())
        expected_keys = set(json.loads(baselines_path.read_text(encoding="utf-8"))["api_review_top_keys"])
        assert actual_keys == expected_keys, (
            f"/api/review 顶层 schema 漂移\n"
            f"  缺失: {expected_keys - actual_keys}\n"
            f"  新增: {actual_keys - expected_keys}"
        )

    def test_2_2_int_005_email_path_unaffected_by_watch_pool_change(self):
        """2.2-INT-005 [P0/integration]: 9:27 邮件 send_screener_report 不依赖 review_data['watch_pool']

        强约束：静态分析 email_sender.py 源码不引用 watch_pool 字段。若不引用，
        Story 2.2 的删除不可能影响邮件链路（结构性反例）。
        """
        src = (PROJECT_ROOT / "src" / "notify" / "email_sender.py").read_text(encoding="utf-8")
        assert "watch_pool" not in src, (
            "src/notify/email_sender.py 引用 watch_pool —— Story 2.2 可能影响邮件链路；"
            "需手工验证 send_screener_report 行为或扩展回归测试"
        )

    def test_2_2_int_006_decision_tracker_reads_latest_review_for_watch_pool(
        self, tmp_path, monkeypatch, fixture_b_today_latest, fixture_c_ranking_recompute
    ):
        """2.2-INT-006 [P1/integration]: decision_tracker.create_premarket_record 写入的 record
        watch_pool 来自调用方（scheduler 从 latest_review.json 读取的字段），不来自 ranking。

        模拟 scheduler.py:625-636 的真实行为：从 latest_review.json 读 watch_pool 后传给
        create_premarket_record。
        """
        from src.engine import decision_tracker

        # patch decision_tracker 的 RECORDS_FILE 至 tmp_path 隔离磁盘
        monkeypatch.setattr(decision_tracker, "RECORDS_FILE", tmp_path / "decisions.json")
        # 模拟 scheduler 路径：latest_review.json 与 latest_ranking.json 都存在但内容不同
        latest_path = tmp_path / "latest_review.json"
        ranking_path = tmp_path / "latest_ranking.json"
        latest_path.write_text(json.dumps(fixture_b_today_latest, ensure_ascii=False), encoding="utf-8")
        ranking_path.write_text(json.dumps(fixture_c_ranking_recompute, ensure_ascii=False), encoding="utf-8")

        # 模拟 scheduler.py:629-633 的读取动作
        review = json.loads(latest_path.read_text(encoding="utf-8"))
        watch_pool = review.get("watch_pool", [])

        record = decision_tracker.create_premarket_record(watch_pool, [], [])
        assert record["watch_pool"][0]["code"] == "600519", (
            "decision record 中 watch_pool 应来自 latest_review.json（fixture B），不来自 ranking（fixture C）"
        )
        # 反向断言：不可能误从 ranking 读
        assert record["watch_pool"][0]["code"] != "000001"

    def test_2_2_unit_013_pytest_collect_count_baseline_guard(self):
        """2.2-UNIT-013 [P2/unit]: pytest --collect-only 总数 == 99 baseline + 本 Story 用例数

        Architect L-3: 防止重名 fixture / module 让 case 数变少而 PASS 仍绿
        """
        out = subprocess.check_output(
            ["pytest", "--collect-only", "-q"],
            text=True,
            cwd=str(PROJECT_ROOT),
            stderr=subprocess.STDOUT,
        )
        m = re.search(r"(\d+)\s+tests?\s+collected", out)
        assert m, f"无法解析 collect 总数；尾部输出：\n{out[-500:]!r}"
        total = int(m.group(1))
        # 99 baseline (email-sync-1.1: 46 + decision-consistency-2.1: 48 + fallback: 5)
        # + 本 Story 34 (32 scenarios; UNIT-009 parametrize ×3)
        EXPECTED_TOTAL = 133
        assert total == EXPECTED_TOTAL, (
            f"测试基线漂移：collect={total}, expected={EXPECTED_TOTAL}\n"
            f"提示：若本 Story 增/减用例，需同步更新 EXPECTED_TOTAL 与 baseline 注释。"
        )


# ============================================================
# T5: 端到端时序一致性集成测试
# ============================================================


class TestT5TimeAxisConsistency:
    """T5: 4 时段 mock now_cn 验证 watch_pool 来源符合冻结语义"""

    def test_2_2_int_007_at_0930_returns_yesterday_history_watch_pool(
        self,
        client,
        patched_data_dir,
        fixture_a_yesterday_history,
        fixture_b_today_latest,
        fixture_c_ranking_recompute,
        monkeypatch,
    ):
        """2.2-INT-007 [P0/integration]: now=09:30 → watch_pool == 夹具 A（昨日 history）"""
        _write_history(patched_data_dir, fixture_a_yesterday_history)
        _write_latest(patched_data_dir, fixture_b_today_latest)
        _write_ranking(patched_data_dir, fixture_c_ranking_recompute)
        _patch_now(monkeypatch, hour=9, minute=30)

        r = client.get("/api/review")
        assert r.status_code == 200
        wp = r.json()["watch_pool"]
        assert wp == fixture_a_yesterday_history[-1]["watch_pool"], (
            "9:30 应读 history 中昨日 entry（fixture A），不读 latest（B）也不读 ranking（C）"
        )
        # 反向断言：不是今日 latest（B）
        assert wp != fixture_b_today_latest["watch_pool"]

    def test_2_2_int_008_at_1430_returns_yesterday_history_watch_pool(
        self,
        client,
        patched_data_dir,
        fixture_a_yesterday_history,
        fixture_b_today_latest,
        fixture_c_ranking_recompute,
        monkeypatch,
    ):
        """2.2-INT-008 [P0/integration]: now=14:30 → watch_pool == 夹具 A（仍 < cutoff）"""
        _write_history(patched_data_dir, fixture_a_yesterday_history)
        _write_latest(patched_data_dir, fixture_b_today_latest)
        _write_ranking(patched_data_dir, fixture_c_ranking_recompute)
        _patch_now(monkeypatch, hour=14, minute=30)

        r = client.get("/api/review")
        assert r.status_code == 200
        wp = r.json()["watch_pool"]
        assert wp == fixture_a_yesterday_history[-1]["watch_pool"]

    def test_2_2_int_009_at_1530_no_today_latest_falls_back_to_history(
        self,
        client,
        patched_data_dir,
        fixture_a_yesterday_history,
        fixture_c_ranking_recompute,
        monkeypatch,
    ):
        """2.2-INT-009 [P0/integration]: now=15:30, latest_review.json 仍是昨日内容 → 显示昨日

        模拟 15:00-15:44 窗口：今日 run_daily_review 未跑（15:45 才触发），
        latest_review.json 仍是昨日 cron 写入的内容（与 fixture A 内容一致）。
        n >= cutoff 时跳过 history 分支直读 latest，恰好等价于昨日 entry。
        """
        # 写 latest_review.json 含昨日内容（即 fixture A 中的 entry）
        yesterdays_latest = fixture_a_yesterday_history[-1]
        _write_latest(patched_data_dir, yesterdays_latest)
        _write_ranking(patched_data_dir, fixture_c_ranking_recompute)
        # 注意：history_file 不写或写但不影响（n >= cutoff 跳过 history 分支）
        _patch_now(monkeypatch, hour=15, minute=30)

        r = client.get("/api/review")
        assert r.status_code == 200
        wp = r.json()["watch_pool"]
        assert wp == yesterdays_latest["watch_pool"], (
            "15:30 latest_review.json 仍为昨日内容时，应直接返回昨日 watch_pool"
        )

    def test_2_2_int_010_at_1550_returns_today_latest_watch_pool(
        self,
        client,
        patched_data_dir,
        fixture_a_yesterday_history,
        fixture_b_today_latest,
        fixture_c_ranking_recompute,
        monkeypatch,
    ):
        """2.2-INT-010 [P0/integration]: now=15:50 → watch_pool == 夹具 B（今日 15:45 新冻结）"""
        _write_history(patched_data_dir, fixture_a_yesterday_history)
        _write_latest(patched_data_dir, fixture_b_today_latest)
        _write_ranking(patched_data_dir, fixture_c_ranking_recompute)
        _patch_now(monkeypatch, hour=15, minute=50)

        r = client.get("/api/review")
        assert r.status_code == 200
        wp = r.json()["watch_pool"]
        assert wp == fixture_b_today_latest["watch_pool"], (
            "15:50 应读今日 latest_review.json（fixture B），不读 history（A）也不读 ranking（C）"
        )


# ============================================================
# Blind Spot Scenarios [BLIND-SPOT]
# ============================================================


class TestBlindSpotBoundary:
    """BLIND-BOUNDARY: 边界条件覆盖"""

    def test_2_2_blind_boundary_001_legacy_history_missing_watch_pool(
        self, client, patched_data_dir, monkeypatch
    ):
        """[BLIND-SPOT] 2.2-BLIND-BOUNDARY-001 [P1/unit]: legacy history entry 缺 watch_pool 字段"""
        legacy_entry = {"date": "2026-05-07", "limit_up_count": 10}  # 故意不含 watch_pool
        _write_history(patched_data_dir, [legacy_entry])
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        # 缺 watch_pool 字段 → 透传后 response 也缺 watch_pool（或为 [] / None — 行为以"不抛错"为准）
        body = r.json()
        wp = body.get("watch_pool", "MISSING")
        # 接受 "MISSING"（缺字段透传）或 [] / None（空容器）；关键是不抛错
        assert wp in ("MISSING", [], None) or isinstance(wp, list), (
            f"legacy entry 缺 watch_pool 应静默透传，实际：{wp!r}"
        )

    def test_2_2_blind_boundary_002_history_empty_list_falls_back_to_latest(
        self, client, patched_data_dir, fixture_b_today_latest, monkeypatch
    ):
        """[BLIND-SPOT] 2.2-BLIND-BOUNDARY-002 [P2/integration]: history.json=[] → 走 latest_review.json"""
        _write_history(patched_data_dir, [])  # 空列表
        _write_latest(patched_data_dir, fixture_b_today_latest)
        _patch_now(monkeypatch, hour=10, minute=0)  # < cutoff，会先尝试 history

        r = client.get("/api/review")
        assert r.status_code == 200
        # history 是空列表 → for 循环不进入 → review_data 仍是 None → 兜底到 latest
        assert r.json()["watch_pool"] == fixture_b_today_latest["watch_pool"]

    def test_2_2_blind_boundary_003_watch_pool_empty_list_returned(
        self, client, patched_data_dir, monkeypatch
    ):
        """[BLIND-SPOT] 2.2-BLIND-BOUNDARY-003 [P2/integration]: review_data['watch_pool']=[]"""
        entry = {"date": "2026-05-07", "watch_pool": []}
        _write_history(patched_data_dir, [entry])
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        assert r.json()["watch_pool"] == [], "空 watch_pool 应原样透传"

    def test_2_2_blind_boundary_004_exact_cutoff_15_00_goes_to_latest_path(
        self,
        client,
        patched_data_dir,
        fixture_a_yesterday_history,
        fixture_b_today_latest,
        monkeypatch,
    ):
        """[BLIND-SPOT] 2.2-BLIND-BOUNDARY-004 [P2/unit]: now == 15:00:00 → 走 latest path

        Code: `if n < cutoff and history_file.exists():` — n == cutoff is False → skip history
        """
        _write_history(patched_data_dir, fixture_a_yesterday_history)
        _write_latest(patched_data_dir, fixture_b_today_latest)
        _patch_now(monkeypatch, hour=15, minute=0, second=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        # 边界 n==cutoff 应走 latest 分支（不走 history）
        assert r.json()["watch_pool"] == fixture_b_today_latest["watch_pool"], (
            "n==cutoff 应走 latest_review.json（不走 history 分支）"
        )


class TestBlindSpotError:
    """BLIND-ERROR: 异常路径覆盖"""

    def test_2_2_blind_error_001_history_corrupt_json_falls_back_to_latest(
        self, client, patched_data_dir, fixture_b_today_latest, monkeypatch
    ):
        """[BLIND-SPOT] 2.2-BLIND-ERROR-001 [P1/integration]: history.json 损坏 → 兜底 latest_review.json"""
        (patched_data_dir / "review_history.json").write_text("{not json", encoding="utf-8")
        _write_latest(patched_data_dir, fixture_b_today_latest)
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        # 异常被既有 line 318-322 的 except 静默吞掉，落到 latest 兜底
        assert r.json()["watch_pool"] == fixture_b_today_latest["watch_pool"]

    def test_2_2_blind_error_002_both_files_missing_returns_empty(
        self, client, patched_data_dir, monkeypatch
    ):
        """[BLIND-SPOT] 2.2-BLIND-ERROR-002 [P1/integration]: 两文件均缺 → 返回 {}"""
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        assert r.json() == {}, "两文件均缺时应返回空 dict（既有 line 331-332 路径）"

    def test_2_2_blind_error_003_ranking_missing_does_not_break_response(
        self, client, patched_data_dir, fixture_a_yesterday_history, monkeypatch
    ):
        """[BLIND-SPOT] 2.2-BLIND-ERROR-003 [P1/integration]: latest_ranking.json 缺失 → 响应正常

        关键回归：旧代码 line 337 `if ranking_file.exists():` 进 try/except；
        新代码彻底不读 ranking_file → 缺失/损坏均不影响 watch_pool 字段
        """
        _write_history(patched_data_dir, fixture_a_yesterday_history)
        # 故意不写 latest_ranking.json
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        assert r.json()["watch_pool"] == fixture_a_yesterday_history[-1]["watch_pool"]


class TestBlindSpotFlow:
    """BLIND-FLOW: 流程多步覆盖"""

    def test_2_2_blind_flow_001_no_caching_reflects_latest_write(
        self, client, patched_data_dir, fixture_b_today_latest, monkeypatch
    ):
        """[BLIND-SPOT] 2.2-BLIND-FLOW-001 [P1/integration]: 无缓存 — 写文件后立即 GET 反映新内容

        模拟 review.html:506-507 手动 *refresh-review → POST /api/review/run → 重写 latest_review.json
        → 立即 GET /api/review 应反映新内容
        """
        # 写 v1
        v1 = dict(fixture_b_today_latest)
        v1["watch_pool"] = [{"code": "AAAA", "name": "v1股"}]
        _write_latest(patched_data_dir, v1)
        _patch_now(monkeypatch, hour=15, minute=50)

        r1 = client.get("/api/review")
        assert r1.status_code == 200
        assert r1.json()["watch_pool"][0]["code"] == "AAAA"

        # 立即覆盖写 v2
        v2 = dict(fixture_b_today_latest)
        v2["watch_pool"] = [{"code": "BBBB", "name": "v2股"}]
        _write_latest(patched_data_dir, v2)

        r2 = client.get("/api/review")
        assert r2.status_code == 200
        assert r2.json()["watch_pool"][0]["code"] == "BBBB", "应立即反映新写入（无内存缓存）"


class TestBlindSpotData:
    """BLIND-DATA: 数据一致性覆盖"""

    def test_2_2_blind_data_001_strip_meta_does_not_mutate_history_on_disk(
        self, client, patched_data_dir, monkeypatch
    ):
        """[BLIND-SPOT] 2.2-BLIND-DATA-001 [P1/integration]: line 334 dict() 防 mutate 磁盘 entry

        Input: history entry watch_pool[0].concepts = ['融资融券', '半导体']
        Expected: GET /api/review 后重新读 review_history.json 文件，
                  磁盘上的 entry 文本仍含 '融资融券'（即响应过滤但磁盘 source 未污染）
        """
        history = [
            {
                "date": "2026-05-07",
                "watch_pool": [
                    {
                        "code": "002081",
                        "name": "金螳螂",
                        "concepts": ["融资融券", "半导体"],
                    }
                ],
            }
        ]
        _write_history(patched_data_dir, history)
        _patch_now(monkeypatch, hour=10, minute=0)

        r = client.get("/api/review")
        assert r.status_code == 200
        # 响应中已被过滤
        assert r.json()["watch_pool"][0]["concepts"] == ["半导体"]

        # 重新读磁盘文件 — 内容应仍含元标签（GET 不可写盘）
        disk_text = (patched_data_dir / "review_history.json").read_text(encoding="utf-8")
        assert "融资融券" in disk_text, (
            "磁盘文件被 mutate（违反只读契约）。GET /api/review 不应写盘。"
        )

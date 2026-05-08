# Dev Log: relay-sentiment-2.3

**Story:** relay-sentiment-2.3 | **Started:** 2026-05-08 | **Agent:** Linus (Opus 4.7, 1M context)

---

## Implementation

### AC1: compute_yesterday_main_board_auction returns 10-field dict (既有 + 测试锁定)

**Test:** `tests/engine/test_leader_feedback_relay.py` (17 cases — UNIT-001..016 + BLIND-BOUNDARY-003)
**Implementation:** None — `src/engine/leader_feedback.py:393-510` 字符级冻结 (BR-1.1)
**Status:** Pending

### AC2: 邮件 cell4 sub 行渲染条件守卫 (defensive UX, 不再"对齐 dashboard")

**Test:** `tests/notify/test_relay_sentiment_render.py` (TestAC2*: 7 cases — UNIT-017..023)
**Implementation:** `src/notify/email_sender.py:458-501` cell4 区块改造（render_sub gate + 删除 line 491 硬编码 fallback + 条件 sub_div）
**Status:** Pending

### AC3: scheduler 数据流不变 (字符级冻结 — 行号已从 395/447 漂移到 423/475)

**Test:** `tests/notify/test_relay_sentiment_render.py::TestAC3SchedulerFreeze` (3 cases — UNIT-024/025 + INT-001)
**Implementation:** None — `src/scheduler.py` 字符级冻结 (BR-3.1)
**Status:** Pending — 行号漂移说明：UNIT-024/025 仅断言子串存在，与行号无关，不受漂移影响

### AC4: dashboard 模板字符级冻结 — **invalidated by post-approval commit 1b50571**

**Test:** `tests/notify/test_relay_sentiment_render.py::TestAC4DashboardFreeze` (UNIT-026/027)
**Implementation:** N/A
**Status:** **SKIP** — 见 "Deviations from SM Design" 节

### AC5: 不引入回归 (DoD)

**Test:** `tests/notify/test_relay_sentiment_render.py::TestAC5Regression` (5 cases — INT-002..006) + TestT5EndToEndIntegration (INT-007/008/009) + TestBlindSpot* (6 cases)
**Implementation:** None — 仅锁定基线
**Status:** Pending

---

## Test Results

**Total at start:** 242 (collect-only) | **Story 2.3 skeleton:** 43 (17 engine + 26 notify) | **Pre-Story baseline:** 199 (含 56 of dashboard-hits-2.4 + 10 of anti-duplicate-email-2.5 + 133 之前) | **Coverage:** TBD

---

## Blockers

None at start.

---

## Feedback to SM

无 — Story 起草质量良好。但请注意 Deviations 节中说明的"post-approval commit 1b50571 dashboard restructure"已使 AC4 literally unimplementable；建议未来 brownfield Story 在 Dev *develop-story 启动时再做一次"代码现状 vs 已批准 scope" 的快速校核（防止 SM/Architect 批准后到 Dev 启动期间的代码漂移）。

---

## Feedback to QA

UNIT-027 (SHA256 baseline) 在 fixture 不存在 key 时"首跑写入、后续比对"的 self-bootstrap 设计，配合 AC4 invalidation 后将 skip — 不需要写入 baseline。

---

## Deviations from SM Design

### Deviation #1: AC4 + UNIT-026 + UNIT-027 → pytest.skip (post-approval drift)

- **Reason**: 用户 2026-05-08 批准 Story 2.3 之后，commit `1b50571` ("feat: 看板新增"加权接力情绪指数" + 原"接力情绪"位置改显"梯队加权竞价"") 重构了 `src/static/index.html`：
  - AC4 / BR-4.1 引用的 `<div class="mb-sub" v-if="ydayAvg && ydayAvg.median_change_pct != null">` 在 line 562 不再存在（grep 0 matches）
  - AC4 引用的 line 556-568 现在是 `<body><div id="app"><div class="header">...` (page header)，不是接力情绪 cell
  - 原"接力情绪" cell 已被替换为 `加权接力情绪指数` (`relayIndex`/`relayIndexInfo` computed at index.html:605-622)
- **User Decision (2026-05-08, path 2)**: AC4 + UNIT-026 + UNIT-027 标记 `pytest.skip(reason='AC4 invalidated by post-approval commit 1b50571 dashboard restructure')`；按 Story 2.3 其余 ACs（1, 2, 3, 5）继续实现。
- **AC2 rationale 调整**: 原"对齐 dashboard v-if"语义因 dashboard 该 v-if 不再存在而失效；本 Story 仍执行 AC2 "邮件 sub 行 缺失数据时不渲染" 行为变更，但定位从"v-if 对齐"改为"defensive UX（数据缺失时不输出半破损占位）"。BR-2.1 / BR-2.2 / BR-2.3 / BR-2.4 / BR-2.6 全部仍适用；BR-2.5 单字段判定 (`_is_num(median_change_pct)`) 仍是合理选择（非"对齐 dashboard" 而是"以 median 作为 4 字段是否齐全的代表"）。
- **Impact**: 43 skeleton tests → 41 PASS + 2 SKIP（UNIT-026 + UNIT-027）；其余 41 tests 全部按 scope 实现。
- **Arch review needed**: 不需要 — 偏离仅减少 scope（不加新行为），不引入架构风险。

---

## Resumption Guide

**Current:** Final Summary written; status flipping to Review at Gate 2
**Next:** Handoff to qa *review relay-sentiment-2.3
**Blockers:** None

---

## Final Summary

**Duration:** ~2 hours | **Completed:** 2026-05-08
**Files Modified:**
- `src/notify/email_sender.py` (cell4 458-501 重构: render_sub gate + 删除 line 491 fallback + 条件 sub_div)
- `tests/engine/test_leader_feedback_relay.py` (skeleton fill, 17 tests)
- `tests/notify/test_relay_sentiment_render.py` (skeleton fill, 26 tests; 2 SKIP per AC4 deviation)
- `tests/notify/test_email_decision_alignment.py` (test_1_1_unit_028 rebaseline per BR-2.3 authorization)

**Tests:** 41/43 PASS + 2 SKIP (AC4) | Story 2.3 own coverage: 100% of in-scope ACs (AC1/2/3/5)
**Full suite:** 235 PASS + 2 SKIP + 5 FAIL (242 collected — matches story-start baseline; 5 failures pre-existing, out-of-scope, documented in Dev Agent Record)

**Self-Review (Gate 1):** PASS — 7 QA dimensions met; 0 critical issues; 0 major issues; 1 minor (out-of-scope pre-existing failures documented).

**Status transition:** Approved → InProgress → Review (after Gate 2).

**Handoff:** qa *review relay-sentiment-2.3

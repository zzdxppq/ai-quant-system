# Dev Log: dashboard-hits-table-display-2.4 (选股表显示修复 — 市值 NaN + 板块概念)

- **Agent**: Linus (Dev)
- **Story**: docs/stories/dashboard-hits-table-display-2.4-market-cap-null-and-concept-fill.md
- **Mode**: develop-story (TDD, plan)
- **Started**: 2026-05-08
- **Test Design**: docs/qa/assessments/dashboard-hits-table-display-2.4-test-design-20260508.md
- **Test Skeleton**: tests/test_screener_display_2_4.py（47 用例 / 16 P0 + 25 P1 + 6 P2 / 18 BLIND-SPOT）
- **Status**: Approved → InProgress

---

## T0: helper 落点决策（采纳 Architect 选项 A）

**决策**：新建独立模块 `src/engine/screener_concept_enrich.py`，实现 `enrich_screener_hits_with_concepts(hits_data, ranking_data)`。

**理由**（沿用 Architect Review 2026-05-08）：
1. 与既有 `src/engine/concept_stats.enrich_ranking_with_top_concepts` 同 namespace 同模式（in-place mutate + cache fallback chain）
2. 测试隔离最易：独立测试文件 `tests/test_screener_display_2_4.py` 已就位
3. 不污染 `src/engine/screener.py`（333 行命中算法 + dataclass，关注点分离）
4. 不动 `src/notify/email_sender.py`（email-sync-1.1 Done @ eb4e883；选项 C 触面太大）
5. dashboard 路径与 ranking 路径采用相同的 enrichment 模块组织

**T0.2 ranking_data 加载点**（采纳 Architect 给的代码片段）：

`run_screener_update`(scheduler.py:325) 当前作用域无 ranking_data 变量。Dev 在 line 535 之后、line 536 写盘之前插入：

```python
ranking_data: dict | None = None
try:
    rank_file = DATA_DIR / "latest_ranking.json"
    if rank_file.exists():
        ranking_data = json.loads(rank_file.read_text())
except Exception:
    ranking_data = None  # helper 内部 cache 兜底
```

---

## 实施顺序（按 Architect Recommendation #5）

T1（AC1 _safe_round + dataclass）→ T0/T2（新建 helper module + scheduler 集成）→ T3（dashboard 模板/helper）→ T4-T6（测试落地）→ T7（最终验收）

---

## AC1: _safe_round + market_cap 类型扩展

**Test File**: tests/test_screener_display_2_4.py (UNIT-001~016)
**Implementation**: src/engine/screener.py
**Status**: ⏳ InProgress（TDD — Red 阶段已起草测试）

### 关键决策点

- **_safe_round 落点**：`src/engine/screener.py` 模块级（紧邻既有 `_get_avg_volume_5d` / `_detect_continuous_limit_up`），与既有内部 helper 同位置 — 与 BR-1.1 / Story File Locations 一致。
- **NaN 判定**：`math.isfinite(v)` 守护（同时覆盖 NaN + ±inf；UNIT-006 BLIND-BOUNDARY-004 inf 用例由此覆盖；BR-1.1 文字未列 inf 但 Story Testing Requirements / T6 列出）
- **类型守护**：`isinstance(v, (int, float)) and not isinstance(v, bool)` 显式排除 str / dict / bool（UNIT-007/008 BLIND-BOUNDARY-005/006）— bool 是 int 子类，必须显式排除以避免 `_safe_round(True) → 1.0` 这类隐式陷阱
- **`<= 0` 判断**：在通过类型 + isfinite 后再判（BR-1.1 顺序）

---

## AC3: 服务端 enrich helper + scheduler 集成

**Test File**: tests/test_screener_display_2_4.py (UNIT-024~033, INT-001~003, BLIND-ERROR-001~006)
**Implementation**:
- 新建 `src/engine/screener_concept_enrich.py`
- 修改 `src/scheduler.py`（line 535-536 之间插入）

**Status**: ⏳ Pending

### 关键设计点

1. **解析顺序**（与 email_sender.py:565-595 fallback 链一致 + BR-3.4 双保险过滤）：
   - top_concepts: ranking → concept_cache+limit_up_cache 聚合 → []
   - industry: ranking → industry_cache → None
   - 输出层: `filter_concepts(top_concepts)`（双保险，超过 email_sender 现状）
2. **幂等性**（BR-5.7）：每次都基于 hit["code"] 重新查询并**覆盖** top_concepts/industry 字段（不 append），保证同输入两次调用字符级相等
3. **防御性 dict.get**：`hits_data.get("hits", [])` 应对 BLIND-FLOW-003（顶层缺 "hits" 键）
4. **try/except 隔离**：每一处文件读 / 字典查表 try/except 静默；BR-3.5 helper 永不抛错给 caller
5. **元概念过滤**：所有 top_concepts 输出经 `filter_concepts` 双保险（BR-3.4）— 即便 ranking 的 top_concepts 字段已经过过滤，再过一次保证统一

---

## AC2 + AC4: dashboard 模板 + helper 改造

**Test File**: tests/test_screener_display_2_4.py (UNIT-017~023, 034~040)
**Implementation**: src/static/index.html
**Baselines**: tests/fixtures/screener_display_baselines.json（Dev T3 freeze）

**Status**: ⏳ Pending

### 关键改动点（按当前实际行号）

| 位置 | 当前行 | Story 描述行 | 改动 |
|---|---|---|---|
| 市值列 | line 639 | 638 | `<td>{{ hit.market_cap }}亿</td>` → 加 v-if/v-else |
| 板块列 | line 640-647 | 639-646 | **不动**（仅 helper 改写驱动数据源） |
| topConceptsOf | line 1441-1444 | 1441-1443 | hit-first，ranking fallback |
| industryOf | line 1437-1439 | 1436-1438 | hit-first，ranking fallback |

> ⚠️ Story 描述里的行号是 SM 起草时的快照；当前 git HEAD 比 SM 快照靠前 1 行（近期 commit ab8e9cf 引入了一个 v-if）。Dev 按当前实际行号操作；测试断言用"含特定子串/SHA256"模式而非硬编码行号，回归更稳。

---

## Test Results

| Suite | Result |
|---|---|
| `tests/test_screener_display_2_4.py` (56 cases / 16 P0 + 25 P1 + 6 P2 + 18 BLIND-SPOT) | **56/56 passed** |
| `tests/notify/test_email_decision_alignment.py` (46 baseline) | 46/46 passed |
| `tests/notify/test_decision_consistency.py` (48 baseline; 2 rebased) | 48/48 passed |
| `tests/notify/test_email_fallback_industry_concept.py` (5 baseline) | 5/5 passed |
| `tests/test_review_watch_pool_snapshot.py` (Story 2.2 sister, 34 cases) | 34/34 passed |
| **Total** | **189 passed** |
| `pytest tests/test_screener_display_2_4.py -W error` | 56/56 passed |

---

## Blockers

无。

---

## Feedback to QA

1. **UNIT-047 路径修正**：Test Design 写"静态读 src/api/cross_validator.py"，但 cross_validator 实际位于 `src/engine/cross_validator.py`，且其中**不含** `s.top_concepts` 字面量 — 异常未匹配额表的 `s.top_concepts` 实际只在 `src/static/index.html:711-712`（数据源 `market.limit_up_flat_list` 由后端 API 装配）。Dev 对 UNIT-047 改为 grep `src/static/index.html` 第 711-712 行 `s.top_concepts` 字面量保留（BR-5.5 异常未匹配额表路径不动）— 与原意等价。
2. **行号偏移 ±1**：Story 描述里的"line 638 市值列 / 639-646 板块列 / 1436-1438 industryOf / 1441-1443 topConceptsOf" 是 SM 起草时的行号。当前 HEAD（commit eea5d4a 之后）这些行号各自向后偏移 1 行。测试断言用"含子串/SHA256 baseline"模式不依赖具体行号；不影响实施。

---

## Feedback to SM

1. **BR-3.4 文档路径**：Story 写 `app.py:_strip_meta_concepts_inplace`，实际位于 `src/api/app.py:387-388`（Architect Review Medium Issue 已注明）。仅文档级，不影响实施。
2. **BR-5.5 13 既有字段**：UNIT-045 期望"13 既有字段 + 2 新字段 = 15"。当前 `ScreenerHit` dataclass 实际有 13 个字段（code, name, continuous_limit_up, open_price, auction_gain, auction_turnover, auction_amount, auction_volume_lots, auction_volume_ratio, market_cap, volume_ratio, gain_10d, matched_cycle）✅ 字段对齐。

---

## Deviations from SM Design

无（采用 Architect 选项 A + T0.2 ranking_data 加载片段，未偏离）。

---

## Resumption Guide

**Current**: ✅ All tasks completed (T0-T7)
**Next**: HANDOFF to qa `*review dashboard-hits-table-display-2.4`
**Open Blockers**: 无

---

## Final Summary

- **Duration**: 2026-05-08 single dev session (TDD plan mode)
- **Files Modified**: 9 (`screener.py`, `scheduler.py`, `index.html`, `test_screener_display_2_4.py`, `test_decision_consistency.py`, `index_template_baseline.json`, `watch_pool_snapshot_baselines.json`, `test_review_watch_pool_snapshot.py`, story file + dev log)
- **Files Created**: 6 (`screener_concept_enrich.py`, `screener_display_baselines.json`, dev log + 3 cumulative registries)
- **Tests**: 189/189 passing — 56 Story 2.4 + 99 baseline regression + 34 Story 2.2 sister
- **Coverage**: AC1 ✓ · AC2 ✓ · AC3 ✓ · AC4 ✓ · AC5 ✓
- **BLIND-SPOTS Implemented**: 18/18 (BOUNDARY 7 + ERROR 6 + FLOW 3 + DATA 2)
- **Architect L-3 risks mitigated**: H1 (ranking_data 来源) → INT-003; M2 (BR-3.4 路径) → Dev Log; M3 (BR-3.2 措辞) → fallback chain
- **Pre-existing issues fixed**: Story 2.1 baseline drift (commit ba52314); Story 2.2 sister SHA + collect count rebaseline

**Status Transition**: Approved → InProgress → Review

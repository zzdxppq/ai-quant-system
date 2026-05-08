# Test Design: relay-sentiment-2.3

2026-05-08 | Turing (QA)

> **Story 真源**: `docs/stories/relay-sentiment-2.3-sub-4-fields-and-email-render-align.md`
> **Story Status (input)**: `AwaitingTestDesign` (after Architect review, score 9.5/10, T0 决策已闭环为 BR-2.5 单字段 `_is_num(median_change_pct)`)
> **test_design_level**: `standard`
> **Story Type Mapping (blind-spot)**: full-stack（数据源 + 邮件 HTML 渲染 + scheduler 数据流 + dashboard 模板冻结）→ 高优类别 `BOUNDARY / ERROR / FLOW / DATA`；`CONCURRENCY / RESOURCE` 不适用（无并发写、无资源句柄）

---

## Overview

| 维度 | 值 |
|---|---|
| **Total Scenarios** | 43 |
| **Unit** | 33 (77%) |
| **Integration** | 10 (23%) |
| **E2E** | 0 (0%) — brownfield 内部行为变更，无新增用户路径，邮件 HTML 用 Integration `inspect HTML string` 而非真实 SMTP |
| **Priority P0** | 24 |
| **Priority P1** | 13 |
| **Priority P2** | 6 |
| **Blind-Spot Scenarios** | 7 (BOUNDARY:3 / ERROR:3 / FLOW:1) |

**测试落点（QA 决定路径）**:
| 文件 | 用途 | 用例数 |
|---|---|---|
| `tests/engine/test_leader_feedback_relay.py` （新建；含 `tests/engine/__init__.py`）| AC1 数据源 4 字段 + 6 类早退（pure unit） + 1 blind-spot | 17 |
| `tests/notify/test_relay_sentiment_render.py` （新建）| AC2 邮件 cell4 v-if 对齐 + AC3/AC4/AC5 字符级冻结 + 集成 + 6 blind-spot | 26 |
| 总计 | | **43** |

> **Dev 注意**: 测试用例数 43 写入 BR-5.7 防漂移基线 — Dev 实现完成后 `pytest --collect-only -q` 总数应为 `（Dev 实施开始时基线）+ 43`，且不得有 `NotImplementedError` 残留。当前 QA 落 design 时项目总数 = 232（189 既有 + 本 Story 43 skeleton）；此为 Dev 起手期望初值。

---

## Scenarios by AC

### AC1: `compute_yesterday_main_board_auction` 返回 dict 含 4 个统计字段（既有 + 测试锁定）

#### Core (10 fields + 类型 + 早退 6 类)

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-UNIT-001 | U | P0 | sample_count > 0 时返回 dict 含全部 10 字段（key 完整） | BR-1.1 / AC1 dict schema 锁定 |
| 2.3-UNIT-002 | U | P0 | 4 字段类型断言：`median_change_pct` 为 `float`，`high5_count`/`flat2_count`/`low5_count` 为 `int` | BR-1.3 类型契约 |
| 2.3-UNIT-003 | U | P0 | `median_change_pct == round(median, 2)`（精度 2 位） | BR-1.3 round 锁定（line 503）|
| 2.3-UNIT-004 | U | P0 | 公开签名 `compute_yesterday_main_board_auction(limit_up_history, spot_df) -> Optional[dict]` 字符级不变（`inspect.signature`）| BR-1.5 / 跨 Story 兼容 |

#### Boundary（BR-1.2 严格语义；T7 边缘 5 类）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-UNIT-005 | U | P0 | `chg == 5.0` 样本 → `high5_count` 不递增（严格 >；line 485 `if chg > 5`）| BR-1.2 边界严格语义 — 关键防回归 |
| 2.3-UNIT-006 | U | P0 | `chg == -5.0` 样本 → `low5_count` 不递增（严格 <；line 487 `elif chg < -5`）| BR-1.2 边界严格语义 |
| 2.3-UNIT-007 | U | P0 | `chg == 5.01` 样本 → `high5_count` 递增（"刚超过"边界） | BLIND-BOUNDARY-004 just beyond limit |
| 2.3-UNIT-008 | U | P0 | `chg == -5.01` 样本 → `low5_count` 递增 | 同上对称 |
| 2.3-UNIT-009 | U | P0 | `chg ∈ {-2.0, 0.0, 2.0}` 三组 → `flat2_count` 递增（闭区间；line 489 `if -2 <= chg <= 2`）| BR-1.2 闭区间锁定 |
| 2.3-UNIT-010 | U | P1 | `chg ∈ {-2.01, 2.01}` → `flat2_count` 不递增 | BR-1.2 闭区间外侧锁定 |

#### Early-Exit Returns None（BR-1.4 — 6 类，本 design 全覆盖以严于 Story T1 要求的 5 类）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-UNIT-011 | U | P0 | `limit_up_history = {}` → 返回 `None`（line 403）| BR-1.4 早退 1 |
| 2.3-UNIT-012 | U | P0 | `past_dates` 空（仅含 today_str key）→ 返回 `None`（line 411）| BR-1.4 早退 2 |
| 2.3-UNIT-013 | U | P0 | 昨日 df 空（None / `df.empty == True`）→ 返回 `None`（line 416）| BR-1.4 早退 3 |
| 2.3-UNIT-014 | U | P0 | 主板 codes 经 `_is_main_board_code` 过滤后为空 → 返回 `None`（line 434）| BR-1.4 早退 4 |
| 2.3-UNIT-015 | U | P0 | spot_df 为空 + 新浪兜底 raise → 返回 `None`（line 460；mock `fetch_a_share_list_sina` 抛错）| BR-1.4 早退 5 + Error Handling |
| 2.3-UNIT-016 | U | P0 | spot_df 全部 `pre_close == 0` → `changes` 列表空 → 返回 `None`（line 492）| BR-1.4 早退 6（Story T1 列入"5 类早退"，此处补全）|

---

### AC2: 邮件 `email_sender.py` 第 4 格 sub 行与 dashboard `v-if` 行为对齐

#### Render-conditional 渲染条件（核心行为变更）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-UNIT-017 | U | P0 | `y_avg = {}` → 渲染 cell4_html **不含**字符串 `中位数` / `高开>5%` / `平开±2%` / `低开<-5%`（sub `<div>` 整段消失） | AC2 主行为变更 — sub 行不渲染 |
| 2.3-UNIT-018 | U | P0 | `y_avg = {sample_count:5, median_change_pct:0.5, high5_count:1, flat2_count:3, low5_count:0, ...}` → 渲染 cell4_html 含 `中位数 +0.5%` + `高开>5%:1` + `平开±2%:3` + `低开<-5%:0` | 既有正向行为保持（line 481-486）|
| 2.3-UNIT-019 | U | P0 | `y_avg = {sample_count:5, median_change_pct: None, ...}`（defensive 场景）→ sub `<div>` 整段不渲染（BR-2.5 单字段判定）| BR-2.5 + Architect T0 决策（dashboard 真源对齐） |

#### Cell4 主体保留（BR-2.4 — main4/title4 不变）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-UNIT-020 | U | P0 | `y_avg = {}` → cell4_html 仍含 `接力情绪` label + `<div>—</div>` 主值 | BR-2.4 cell4 主体保留（dashboard line 558-561 等价）|
| 2.3-UNIT-021 | U | P0 | `y_avg = {}` → cell4_html 含 title `昨日涨停 — 只`（line 488） | BR-2.4 |
| 2.3-UNIT-022 | U | P0 | 任意 y_avg → cell4_html 始终含 `<div style="font-size:11px;color:#888;">接力情绪</div>` label（无条件） | BR-2.4 label 永远输出 |

#### Negative Assertion（BR-2.3 — 旧硬编码 fallback 必须消失）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-UNIT-023 | U | P0 | 任何 y_avg 输入下，渲染 HTML **永不**含字符串 `中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—`（旧 line 491 fallback 已删） | BR-2.3 防回归 — 强约束 |

---

### AC3: scheduler 数据流不变（既有路径验收）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-UNIT-024 | U | P1 | `src/scheduler.py` 第 395 行字符级含 `compute_yesterday_main_board_auction(limit_up_hist, spot_df)`（无空格变体） | BR-3.1 字符级冻结 |
| 2.3-UNIT-025 | U | P1 | `src/scheduler.py` 第 447 行字符级含 `"yesterday_main_board_avg_auction": y_main_board_stats,` | BR-3.1 字符级冻结（含尾逗号）|
| 2.3-INT-001 | I | P0 | 集成：mock `compute_yesterday_main_board_auction` 返回 None，调用 scheduler 写 latest_leader.json，文件含 `"yesterday_main_board_avg_auction": null` | AC3 Scenario + BR-3.1 |

---

### AC4: dashboard 模板字符级冻结

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-UNIT-026 | U | P0 | `src/static/index.html` 第 562 行字符级 `<div class="mb-sub" v-if="ydayAvg && ydayAvg.median_change_pct != null">` | BR-4.1 dashboard 真源锁定 |
| 2.3-UNIT-027 | U | P1 | `index.html` 行 556-568 SHA256 hash 等于本 design 锚定的 baseline hash（动态计算，首跑写入 fixture）| BR-4.1 子串 + hash 双锁 |

---

### AC5: 不引入回归（DoD）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-INT-002 | I | P0 | `inspect.signature(send_screener_report)` 字符串与 email-sync-1.1 / decision-consistency-2.1 baseline 完全一致（沿用 `tests/notify/test_email_decision_alignment.py` INT-005 模式） | BR-5.1 公开签名严格冻结 |
| 2.3-INT-003 | I | P0 | 既有 99 邮件用例 + 34 review 用例 + 56 screener-2.4 用例（如 2.4 已落地）全部 PASS（不修改任何既有 fixture） | BR-5.7 跨 Story 兼容 |
| 2.3-INT-004 | I | P0 | `pytest --collect-only -q` 总用例数 = `dev_baseline + 36`（dev_baseline 为 Dev 起手时观测值；本 Story 新增 36；防漂移）| BR-5.7 anti-drift |
| 2.3-INT-005 | I | P1 | `data/latest_advice.json` 文件 schema 8 字段（bucket / text / suggested_position / suggested_position_short / reason / phase / cycle_day / snapshot_time）字符级不变（解 decision-consistency-2.1 链路）| BR-5.5 |
| 2.3-INT-006 | I | P1 | `data/latest_review.json` 文件含 `watch_pool_snapshot` key（watch-pool-snapshot-2.2 link）；本 Story 改动后字符级不变 | BR-5.5 / BR-5.6 |

---

## Integration Scenarios（T5 端到端邮件 + leader 一致性）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.3-INT-007 | I | P0 | mock `compute_yesterday_main_board_auction` 返回 None → 调 `send_screener_report(stub_args)` → 渲染 HTML 中 cell4 含 `接力情绪` 且**不含** `中位数` 字符串 | AC1+AC2 端到端真路径 |
| 2.3-INT-008 | I | P0 | mock 返回完整 10 字段 dict → 调 `send_screener_report` → 渲染 HTML 含 4 字段子串（`中位数 +0.5%` / `高开>5%:1` / `平开±2%:3` / `低开<-5%:0`） | AC1+AC2 端到端正向路径 |
| 2.3-INT-009 | I | P1 | 跨 Story 集成：`_calc_daily_advice` 写 `latest_advice.json`（decision-consistency-2.1）+ `watch_pool_snapshot` 写 `latest_review.json`（watch-pool-snapshot-2.2）+ 本 Story 的 cell4 渲染 → 三链路并存无冲突 | DoD #1 跨 Story 解耦 |

---

## Blind-Spot Scenarios `[BLIND-SPOT]`

| ID | Category | Pri | Lvl | Scenario | Ref |
|---|---|---|---|---|---|
| 2.3-BLIND-BOUNDARY-001 | BOUNDARY | P1 | U | `leader = None` → line 371 `or {}` 兜底 → `y_avg = {}` → render_sub False → sub 行不渲染、不抛 AttributeError | BOUNDARY-001 / BR-2.1 |
| 2.3-BLIND-BOUNDARY-002 | BOUNDARY | P1 | U | `y_avg = {sample_count:5, median_change_pct:0.5, high5_count:None, flat2_count:None, low5_count:None}`（partial fields, defensive）→ render_sub True（单字段判定）→ sub 行渲染 `中位数 +0.5% · 高开>5%:— · 平开±2%:— · 低开<-5%:—`（高开/平开/低开降级为 `—`，line 483-485 `_is_num(...)` 个字段降级）| BOUNDARY-005 / Architect Low Issue 1 / T7 防御 |
| 2.3-BLIND-BOUNDARY-003 | BOUNDARY | P2 | U | `y_avg = {sample_count: 0, median_change_pct: None, ...}`（理论场景，line 470 `_is_num(sample) and sample > 0` 走 else 分支）→ Dev 实施后 sub_str 由 BR-2.5 控制；render_sub False → sub 不渲染（与 dashboard 行为一致）| BOUNDARY-002 / 关键改动点验证 |
| 2.3-BLIND-ERROR-001 | ERROR | P2 | U | `leader` 不是 dict（值为 `[]` / `0` / `"abc"`）→ line 371 `(leader or {}).get(...)` 类型异常 → 由 `or {}` 链兜底 → 不抛 TypeError；render_sub False | ERROR-003 / BR-2.1 健壮性 |
| 2.3-BLIND-ERROR-002 | ERROR | P2 | U | `fetch_a_share_list_sina()` 抛 `ConnectionError` / 任意 Exception → `compute_yesterday_main_board_auction` 不传播异常（line 457 `except Exception as e`），继续走原 spot_df → 若仍无样本则返回 None | ERROR-001 + AC1 Error Handling |
| 2.3-BLIND-ERROR-003 | ERROR | P2 | U | `spot_df` 缺失 `code` 列（极端 schema 异常）→ line 442 `s["code"]` 抛 KeyError 不捕获（既有行为）→ 由调用方 try/except 覆盖（**本 Story 不修复**，仅记录） | ERROR-003 — 文档化已知行为 |
| 2.3-BLIND-FLOW-001 | FLOW | P2 | I | scheduler 二次调用（cron 触发 + 用户手动调用）同输入 → leader_data 写盘内容字符级一致（幂等性）| FLOW-002 重复调用 |

---

## Risk Coverage

| 风险类别 | 缓解 Scenarios |
|---|---|
| **R1 — 用户视觉不一致**（邮件 vs dashboard）| 2.3-UNIT-017, 018, 019, 023; 2.3-INT-007, 008 |
| **R2 — 4 字段语义漂移**（boundary 误算）| 2.3-UNIT-005～010; 2.3-BLIND-BOUNDARY-002 |
| **R3 — 函数早退路径回归**（None 返回时机变化导致 cell4 误渲染）| 2.3-UNIT-011～016; 2.3-INT-001 |
| **R4 — 跨 Story 链路破坏**（decision-consistency-2.1 / watch-pool-snapshot-2.2 / dashboard）| 2.3-UNIT-024～027; 2.3-INT-002, 003, 005, 006, 009 |
| **R5 — 旧 fallback 字符串残留**（hardcoded 占位回流）| 2.3-UNIT-023（强 negative assertion） |
| **R6 — 用例总数漂移**（CI 静默失败）| 2.3-INT-004 |

---

## Coverage Validation

**Standard Coverage**:
- ✅ AC1 covered by UNIT-001～016 (16 scenarios)
- ✅ AC2 covered by UNIT-017～023 + INT-007, 008 (9 scenarios)
- ✅ AC3 covered by UNIT-024, 025 + INT-001 (3 scenarios)
- ✅ AC4 covered by UNIT-026, 027 (2 scenarios)
- ✅ AC5 covered by INT-002～006, 009 (6 scenarios)
- ✅ No duplicate coverage（INT-007/008 是端到端，UNIT-017～023 是 cell4 局部 — 关注层不同）
- ✅ 关键路径多层次：boundary 由 UNIT 锁定，render-conditional 由 UNIT + INT 双层

**Blind-Spot Coverage**:
- ✅ BOUNDARY for 输入字段 partial / null（BLIND-001/002/003）
- ✅ ERROR for 外部依赖 sina（BLIND-ERROR-002）+ leader 类型异常（BLIND-ERROR-001）
- ✅ FLOW for scheduler 重复调用幂等性（BLIND-FLOW-001）
- ➖ CONCURRENCY 不适用（无并发写、无锁）
- ➖ DATA 不适用（无 DB 事务、无 cascade）
- ➖ RESOURCE 不适用（无连接池/锁/临时文件）

---

## Execution Order

1. **P0 Unit (16 cases)** — AC1 数据源 boundary + 早退（UNIT-001～016 子集 P0：12 个）+ AC2 render-conditional（UNIT-017～023 P0：6 个）+ AC4 dashboard 字符级（UNIT-026 P0：1 个）
2. **P0 Integration (5 cases)** — INT-001, 002, 003, 004, 007, 008
3. **P1 Unit (4 cases)** — UNIT-004, 010, 024, 025, 027
4. **P1 Integration (3 cases)** — INT-005, 006, 009
5. **P1 Blind-Spot (2 cases)** — BLIND-BOUNDARY-001, 002
6. **P2 Blind-Spot + Edge (5 cases)** — BLIND-BOUNDARY-003 / BLIND-ERROR-001/002/003 / BLIND-FLOW-001

---

## Test Skeleton 文件清单（Output 2 已生成）

| 文件 | 用例数 | Blind-Spot 用例数 | 语法验证 |
|---|---|---|---|
| `tests/engine/__init__.py` | — | — | — |
| `tests/engine/test_leader_feedback_relay.py` | 17 | 1 (BLIND-BOUNDARY-003) | ✅ pytest --collect-only 收集到 17 用例 |
| `tests/notify/test_relay_sentiment_render.py` | 26 | 6 (BLIND-BOUNDARY-001/002, ERROR-001/002/003, FLOW-001) | ✅ pytest --collect-only 收集到 26 用例 |
| **总计** | **43** | **7** | 已确认（pytest --collect-only -q 收集到 232 = 189 既有 + 43 新 skeleton）|

---

## Trace References

```
Test design: docs/qa/assessments/relay-sentiment-2.3-test-design-20260508.md
Skeleton: tests/engine/test_leader_feedback_relay.py (17 cases)
Skeleton: tests/notify/test_relay_sentiment_render.py (26 cases)
Total: 43 (Unit 33 / Integration 10 / E2E 0)
P0: 24, P1: 13, P2: 6
Blind-Spot: 7 (BOUNDARY:3 / ERROR:3 / FLOW:1)
Pytest baseline (after skeleton): 232 (189 pre-existing + 43 new)
```

---

## QA 给 Dev 的关键提示

1. **`_is_num` 的 scope**: 顶层 line 189 + 嵌套 line 376 内部各自定义；`_build_html` 内部用 line 376 那个（已闭包绑定）。Dev 可继续复用 line 376 的 `_is_num`（语义与 line 189 完全一致）。
2. **`render_sub` 推荐位置**: line 469 后（在 sample 取值之后、line 470 `if _is_num(sample) and sample > 0:` 分支之前）独立计算 `render_sub = _is_num(y_avg.get("median_change_pct"))`，与 sample 分支解耦。
3. **`sub4_str` 构造点**: 按 Architect Low Issue 1 给出的形式 — `sub4_str = ""` 默认初始化，仅 `render_sub == True` 时填充；line 481-486 既有逻辑保留（含 `_is_num` 个字段降级 `—`，覆盖 BLIND-BOUNDARY-002）。
4. **line 487-491 else 分支**: 删除 line 491 `sub4_str = "中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—"`；其他三行（`title4="昨日涨停 — 只"` / `main4="—"` / `main4_color="#6b7280"`）**保留**（cell4 主体语义不变，BR-2.4）。
5. **line 499 改动**: 从 `f'<div style="font-size:10px;color:#999;margin-top:1px;">{sub4_str}</div>'` 改为 `(f'<div style="font-size:10px;color:#999;margin-top:1px;">{sub4_str}</div>' if render_sub else '')`。
6. **测试基线漂移说明**: 本 Story 起草时项目用例基线 = 133（既有 99 email + 34 review）。Dev 起手时若 dashboard-hits-table-display-2.4（in-flight）已落地，基线可能升至 189。INT-004 用例需读取 Dev 起手时 baseline，本 Story 净新增 = 36（不含 2.4）。
7. **scheduler.py + index.html 不动**：Dev 实施时 grep `git diff --name-only` 必须只含 `src/notify/email_sender.py` + `tests/engine/__init__.py` + 两个新建测试文件 + 本 Story 文件回填，不含 scheduler.py / index.html / leader_feedback.py。

---

## Principles Applied

- **Shift-left**: Unit 75% / Integration 25% / E2E 0%（brownfield 内部行为变更）
- **Risk-based**: R1 / R2 用 P0 全覆盖；R5 negative assertion 显式锁
- **Efficient**: cell4 局部行为锁在 UNIT 层；端到端只 2 个 INT case 验证 wire-up
- **Maintainable**: 复用既有 `inspect.signature` + `latest_*.json` schema baseline（INT-002, 005, 006）
- **Fast feedback**: P0 用例可独立运行（`pytest tests/engine/ tests/notify/test_relay_sentiment_render.py -m "p0" -v`，pytest marker 由 Dev 配置或省略）

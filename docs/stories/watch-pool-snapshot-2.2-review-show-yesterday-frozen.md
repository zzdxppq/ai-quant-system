# Story watch-pool-snapshot-2.2: 次日观察池显示昨日 15:45 冻结快照

## Story

```yaml
Story:
  id: watch-pool-snapshot-2.2
  title: 次日观察池读 review_history 快照，不再实时重算（复盘页时序对齐）
  epic: iteration-2 brownfield (virtual epic — 真源为 docs/prd/iteration-2-scope.md)
  status: Done
  mode: plan
  repository: monolith
  priority: P1
  estimated_complexity: standard
  test_design_level: standard
  story_type: brownfield-enhancement
```

**As a** A 股短线交易者（604491810@qq.com 邮件唯一收件人 + 浏览器看板唯一使用者），
**I want** 复盘页"🎯 次日关注标的池"显示**昨日 15:45 收盘后冻结的快照**（即"昨晚定的明日关注股"），而不是基于当前盘中 `latest_ranking.json` 实时重算的列表，
**so that** 我开盘前看到的关注池**与昨晚邮件 / 收盘后看到的列表保持一致**，避免出现"昨晚定的 7 只 → 今早盘中变成另外几只"的时序错位（次日观察池是 next-day decision 的输入，必须冻结在前一日决策时刻）。

---

## 背景与问题

### 当前行为分歧

`run_daily_review()` 在交易日 15:45（main.py:86-94 cron）执行，调用 `_save_review()`（daily_review.py:1387-1405）将完整 `DailyReview`（含 `watch_pool`）写入：
- `data/latest_review.json`（覆盖式）
- `data/review_history.json`（追加 + 去重 + 截最近 30 条）

**watch_pool 已被持久化**到 `review_history.json` — 实测最后一条 entry（`2026-05-07`）含 17 个字段，其中 `watch_pool` 长度 = 7。

但 `src/api/app.py:get_review`（284-361）在返回前**强制重算 watch_pool**（line 336-343）：

```python
# watch_pool 始终用最新 ranking 重算（按当前规则：top30+45%+≥2连板+主板）
if ranking_file.exists():
    try:
        ranking_payload = json.loads(ranking_file.read_text())
        ranking_rows = ranking_payload.get("ranking") or []
        review_data["watch_pool"] = build_watch_pool_from_ranking(ranking_rows)
    except Exception:
        pass
```

| 时段 | review_data 来源（既有时间门控） | watch_pool 实际显示 | 期望 |
|---|---|---|---|
| 9:00 — 14:59 | `review_history.json` 中昨日 entry（含昨日 15:45 冻结的 watch_pool） | **被 line 336-343 覆盖**为基于今日 `latest_ranking.json` 的实时计算结果 | 昨日 15:45 冻结的 watch_pool |
| 15:00 — 15:44 | `latest_review.json`（仍为昨日内容，今日 run_daily_review 未跑） | **被覆盖**为今日 9:30+ 实时排行的重算结果（today's intraday） | 昨日 15:45 冻结的 watch_pool |
| 15:45+ | `latest_review.json`（今日 run_daily_review 写入） | 与今日刚冻结的 watch_pool 一致（覆盖前后等价 — 唯一不冲突的窗口）| 今日 15:45 冻结的 watch_pool |

`latest_ranking.json` 在交易日内会被 `run_cycle_update()` 持续刷新（main.py 调度 + scheduler.py:706-711 的 `_background_tasks` 在 9:27 之后立即调一次）→ 复盘页 watch_pool 跟着今日盘中数据不停变化。

### 用户反馈（2026-05-08）

> 复盘页"🎯 次日关注标的池"显示当前盘中数据，但用户期望看的是
> **前一日 15:00 收盘后冻结的快照**（"昨晚定的明日关注股"）。

### 真源约束（用户已选定）

> "review API 加载昨日收盘后的 watch_pool 快照，不再实时调用 `_generate_watch_pool`"
>
> "仅在用户主动选'今日'日期且当前 < 15:00 时，显示'待 15:00 后生成'占位"

[Source: docs/prd/iteration-2-scope.md#story-2-2]

---

## 改动范围（来自 scope）

[Source: docs/prd/iteration-2-scope.md#story-2-2]

1. **冻结 watch_pool 写入 review_history**：`run_daily_review()` 已在 15:45 通过 `_save_review` 写入 `latest_review.json` + `review_history.json`，watch_pool 已被持久化。**本 Story 不改 _save_review，不改 run_daily_review**（既有冻结行为已正确）。
2. **review API 加载昨日收盘后的 watch_pool 快照**：删除 `app.py:get_review` 内的 watch_pool 重算块（line 336-343），让 API 返回 review_data 中已含的 watch_pool（来自 review_history / latest_review 的 15:45 冻结结果）。
3. **`build_watch_pool_from_ranking` 函数本体保留**：(a) `daily_review.run_daily_review` 调用链 `_generate_watch_pool` → `build_watch_pool_from_ranking` 不变；(b) 兜底路径（legacy snapshot 缺 watch_pool 时）仍可能调用；(c) 单元测试可独立验证算法。
4. **time-gate cutoff 不动**：保留现有 `cutoff = 15:00` 时间门控（main.py 实际 run_daily_review 在 15:45，但 15:00-15:45 窗口 `latest_review.json` 仍是昨日内容，恰好等价于 history 中昨日 entry，删除覆盖后行为一致）。

> ⚠️ 与 scope 文件 #34-37 的一处时机差异：
> scope 文件写"在 cycle_update 流程（15:30 收盘后）冻结 watch_pool"，但实际 `run_cycle_update`（15:30）只刷 `latest_ranking.json`，不算 watch_pool；`run_daily_review`（15:45）才计算 watch_pool 并写 `review_history.json`。**本 Story 沿用既有 15:45 冻结时机**（不迁移到 15:30），原因：(a) `run_cycle_update` 不依赖涨停数据，watch_pool 计算需要先有涨停 + 主线 + 排行，依赖链上 15:45 才完整；(b) 用户实际感知差异是"复盘页显示什么"，不是"冻结发生在 15:30 还是 15:45"；(c) KISS — 减小变更面。

---

## Acceptance Criteria

### AC1: review API 不再覆盖 review_data["watch_pool"]

**Scenario**
```gherkin
GIVEN /api/review 被前端 dashboard fetch（review.html:1031 loadDaily）
  AND review_data 已通过既有时间门控（< 15:00 走 history；≥ 15:00 走 latest_review.json）确定
WHEN review_data 已含 watch_pool 字段（来自 _save_review 持久化）
THEN
  - app.py:get_review 必须**删除** line 336-343 的 watch_pool 重算块
  - 返回值 watch_pool 完全等于 review_data["watch_pool"]（来自 review_history / latest_review 快照）
  - latest_ranking.json 在交易日的实时刷新**不再**影响 /api/review 返回的 watch_pool 内容
  - scorecard / promotion_summary 重算块（line 345-359）保持不变（用户明确要求"历史快照用新公式重算" — 仅 watch_pool 是冻结快照）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-1.1 | `app.py:get_review` 删除 watch_pool 重算块（line 336-343）整段；不引入新条件分支 |
| BR-1.2 | watch_pool 的"冻结"语义来源是 `_save_review` → `review_history.json`（既有），本 Story 不动 daily_review.py |
| BR-1.3 | 返回值 watch_pool 字段保留原 schema（`code/name/board_count/industry/close/market_cap_yi/total_gain_pct/reason/watch_points/auction_range/top_concepts/is_main_board/pool_tag`）— 因为是直接透传 review_data 中的字段 |
| BR-1.4 | `_strip_meta_concepts_inplace` 对 watch_pool 元标签清洗（app.py:395-396）保持不变（清洗动作针对 concept 字段，不动 watch_pool 列表本身）|
| BR-1.5 | 不删除 `from src.engine.daily_review import build_watch_pool_from_ranking` import？— **删除**（本 Story 删除该 import 及对应使用，因为 app.py 内不再需要）；如 Architect 判断保留更便于回滚则改为本地（function-level）import — 由 Architect 在 *review 阶段决定 |

**Data Validation**
| Field | Type | Required | Rules | Error Message |
|---|---|---|---|---|
| watch_pool | list[dict] | ✅ | 来自 review_data["watch_pool"]（可能为 [] / 缺失） | — |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| review_data 缺 watch_pool 字段（legacy 旧 snapshot 早于 watch_pool 字段引入时） | — | （静默） | 返回 watch_pool=[]（与 review.html v-else 空状态"无 — 今日所有候选都不满足三项严格条件"行为一致）|
| review_data["watch_pool"] = [] | — | — | 返回 watch_pool=[]（v-else 渲染"无"占位）|

---

### AC2: review_history.json 已含 watch_pool 快照（既有行为，本 Story 仅验收）

**Scenario**
```gherkin
GIVEN run_daily_review 在交易日 15:45 cron 执行（main.py:86-94）
  AND _save_review 写 latest_review.json + 追加 review_history.json
WHEN review.watch_pool 由 _generate_watch_pool 计算完成
THEN
  - latest_review.json 必须含 watch_pool 字段（asdict(review) 行为，既有）
  - review_history.json 最近一条 entry 必须含 watch_pool 字段
  - 写入时机为 15:45（同 _run_post_market）
  - 写入幂等：同日重复触发（手动 *refresh-review）以最后一次为准（既有去重逻辑 daily_review.py:1402-1404）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-2.1 | `_save_review` 函数本体不变（daily_review.py:1387-1405）|
| BR-2.2 | `run_daily_review` 函数本体不变（daily_review.py:111-204）— 既有 `review.watch_pool = _generate_watch_pool(...)` 调用 line 192 不动 |
| BR-2.3 | watch_pool schema = `WatchCandidate` dataclass + 透传 `top_concepts/is_main_board/pool_tag`（daily_review.py:805-832）— 不变 |
| BR-2.4 | 数据完整性验证：实测最近一条 review_history entry（`2026-05-07`，UTC+8）含 17 字段，watch_pool 长度 = 7 — 现状已满足，无需迁移 |
| BR-2.5 | 数据兼容性：本 Story **不**强制改写历史 entry；旧 entry 若缺 watch_pool（早于该字段引入的迁移期数据），AC1 BR-1 兜底为 [] |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| `_save_review` 写入异常（既有行为） | — | 由既有 try/except 处理（daily_review.py:1397-1401 仅对读取兜底；写入未兜底） | 不在本 Story 范围 |

---

### AC3: 复盘页 D 区域 watch_pool 渲染保持冻结快照

**Scenario**
```gherkin
GIVEN 用户访问 /review（review.html）
  AND loadDaily fetch /api/review 返回完整 review_data
WHEN review.html:1010 `watchPool = computed(() => review.value.watch_pool || [])`
THEN
  - watchPool 显示的是来自 review_data 的 watch_pool 字段（即 15:45 冻结快照）
  - 区域 D 模板（review.html:454-489）**绝对不动**（HTML 结构 / CSS 全部保持现状）
  - watchPool 各字段（code/name/board_count/industry/top_concepts/market_cap_yi/reason/auction_range/watch_points）按既有渲染显示
  - 当 watchPool.length === 0 时，渲染既有空状态文案"无 — 今日所有候选都不满足三项严格条件，明日不主动开仓"（v-else，line 486-488）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-3.1 | `src/static/review.html` 区域 D 模板（line 454-489）字符级保持不变 |
| BR-3.2 | review.html `<script setup>` 区 watchPool computed（line 1010）保持不变 |
| BR-3.3 | review.html loadDaily（line 1029-1036）保持不变（前端 fetch 路径与解构不变）|
| BR-3.4 | 不引入"今日 < 15:00 占位"前端代码（scope 文件 #36 该项是 forward-looking 假设；当前 review.html 无日期选择器，时间门控由后端 /api/review 既有 cutoff=15:00 自动处理；前端无感知）|

**UI Interaction**
| Trigger | Behavior |
|---|---|
| 9:00 - 14:59 任意时刻刷新复盘页 | 区域 D 显示**昨日 15:45 冻结**的 watch_pool（不再随 latest_ranking 变化）|
| 15:00 - 15:44 任意时刻刷新复盘页 | 区域 D 仍显示**昨日**的 watch_pool（latest_review.json 仍是昨日；run_daily_review 未跑）|
| 15:45+ 任意时刻刷新复盘页 | 区域 D 显示**今日**的 watch_pool（run_daily_review 已写新快照） |
| 用户点"刷新复盘"按钮（review.html:506-507）| 触发 POST /api/review/run → run_daily_review 同步重写快照 → 3s 后 loadData 重拉 → 区域 D 显示重写后的 watch_pool（15:45 之外手动触发场景 — 用户主动覆盖）|

---

### AC4: scheduler / cron 流程不变

**Scenario**
```gherkin
GIVEN main.py:setup_scheduler 配置 4 个 cron job（cycle_update@15:30 / screener_update@9:27 / ranking_refresh@10:00,12:30 / post_market@15:45）
WHEN 每日交易时段 cron 触发
THEN
  - 15:30 run_cycle_update → 仅刷 latest_ranking.json（不动 review_history）— 既有行为
  - 15:45 _run_post_market → run_daily_review → _save_review 写 latest_review.json + review_history.json（含 watch_pool）— 既有行为
  - 9:27 run_screener_update _background_tasks 调 run_cycle_update → 仍只刷 latest_ranking.json（不动 review_history.json）— 既有行为
  - **本 Story 不增加 / 不修改 cron job**
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-4.1 | `main.py:setup_scheduler` 字符级不变 |
| BR-4.2 | `src/scheduler.py:run_cycle_update` 字符级不变 |
| BR-4.3 | `src/scheduler.py:run_screener_update._background_tasks` 字符级不变 |
| BR-4.4 | `src/engine/daily_review.py:run_daily_review` + `_save_review` 字符级不变 |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| 15:45 run_daily_review 抛错（涨停数据不足等） | — | print "[复盘] 异常: {e}"（main.py:75）| 既有 try/except 处理；当日 latest_review.json / review_history.json 不更新；次日 9:00-14:59 复盘页继续显示前日（前前日）快照 — 既有降级路径，不在本 Story 修改 |

---

### AC5: 不引入回归（DoD）

**Scenario**
```gherkin
GIVEN 现有 /api/review + 复盘页 + run_daily_review 链路
WHEN Story 2.2 改造后的代码在以下输入下被调用
THEN 行为应与改造前完全一致：
  - /api/review 返回 schema 不变（顶层字段集合保持原样）
  - review.html 区域 D 模板（line 454-489）字符级未改
  - run_daily_review 调用链完全不变（_generate_watch_pool / build_watch_pool_from_ranking 行为保留）
  - latest_ranking.json 写入路径不受影响（仍由 run_cycle_update 维护）
  - 9:27 邮件链路（Story 2.1 / email-sync-1.1）不受影响（邮件不读 watch_pool）
  - 决策追踪（decision_tracker.create_premarket_record，scheduler.py:625-636）继续从 latest_review.json 读 watch_pool — 既有行为不变
```

**Business Rules**
| ID | Rule |
|---|---|
| BR-5.1 | `/api/review` 函数签名 + 返回 schema 不变 |
| BR-5.2 | `daily_review._save_review` / `daily_review.run_daily_review` 函数体不变 |
| BR-5.3 | `daily_review._generate_watch_pool` / `daily_review.build_watch_pool_from_ranking` 函数体不变 |
| BR-5.4 | 不引入新依赖（不加 Redis / SQLite / pydantic 等）|
| BR-5.5 | `data/latest_ranking.json` / `data/latest_review.json` / `data/review_history.json` 文件契约不变 |
| BR-5.6 | review.html 模板 + JS computed / loadDaily 不变（前端零改动）|
| BR-5.7 | scheduler.py:run_screener_update 行 625-636 `decision_tracker.create_premarket_record` 调用块不变（继续从 latest_review.json 读 watch_pool —— Story 2.2 后该 watch_pool 仍是昨日 15:45 冻结快照，符合 9:27 决策追踪语义）|

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| 任一边缘分支行为差异 | — | — | QA 标记为 BLOCKING，回退至 SM revise |

---

## Tasks / Subtasks

> **说明**：测试用例的具体 spec 由 QA 在 *test-design 阶段产出（test_design_level: standard），Dev 在编码后回填本节"测试"子任务。

### Infrastructure Tasks (Shared)

- [x] **T0: import 调整决策（Architect 在 *review 阶段决定）** `[AC1]`
  - [x] Architect 决定（L-2）：**删除** `app.py:296` 局部 import `from src.engine.daily_review import build_watch_pool_from_ranking`
  - [x] 决策记录到 Dev Log（含 5 条理由：grep 无残留调用 / KISS / ruff F401 / 回滚成本低 / 减耦合）

### Feature Implementation Tasks

- [x] **T1: AC1 — 删除 /api/review 内 watch_pool 重算块** `[AC1]`
  - [x] `src/api/app.py:get_review`：删除原 line 336-343 整块（含 `# watch_pool 始终用最新 ranking 重算...` 注释 + try/except）
  - [x] 删除原 line 296 `from src.engine.daily_review import build_watch_pool_from_ranking` 局部 import（合并 T0 决策）
  - [x] 删除原 line 300 `ranking_file = DATA_DIR / "latest_ranking.json"` 变量声明（删除重算块后即 dead code）
  - [x] 重写原 line 286-294 docstring（合并 Architect L-1）：陈述 watch_pool 来自 review_history / latest_review.json 中 15:45 冻结快照
  - [x] `_strip_meta_concepts_inplace` 对 watch_pool 的元标签清洗保持不变（验证：UNIT-004 / BLIND-DATA-001）
  - [x] scorecard / promotion_summary 重算块保持不变（验证：INT-002）

  **Test Specs**（white-box scenarios from test-design）:
  | Scenario | Input | Expected | Level |
  |----------|-------|----------|-------|
  | 2.2-UNIT-001 透传 | history 含 wp=A; ranking 重算→C; now=09:30 | response.wp == A | unit |
  | 2.2-UNIT-002 import/注释删除 | static read src/api/app.py | 不含 'build_watch_pool_from_ranking' import + 不含 '始终用最新 ranking 重算' 注释 | unit |
  | 2.2-UNIT-003 docstring 同步 | get_review.__doc__ | 不含 '始终用 latest_ranking 重算'；含 '15:45' 或 'review_history' | unit |
  | 2.2-UNIT-004 概念清洗保留 | wp[0].concepts=['某行业方向','半导体'] | response.wp[0].concepts==['半导体'] | unit |
  | 2.2-INT-001 route 透传 | history+ranking 都存在 | response.wp == history wp（NOT ranking） | integration |
  | 2.2-INT-002 scorecard 仍重算 | history.scorecard={'stale':True} | response.scorecard 由当前公式重算 | integration |

- [x] **T2: AC2 验收 — review_history.json 已含 watch_pool（既有行为）** `[AC2]`
  - [x] 编写测试断言 `_save_review` 写入 latest_review.json + review_history.json 时含 watch_pool 字段（UNIT-005/006/007 + INT-003）
  - [x] 不修改 daily_review.py 任何代码（仅 SHA256 baseline 锁定 — UNIT-012）

  **Test Specs**（white-box scenarios from test-design）:
  | Scenario | Input | Expected | Level |
  |----------|-------|----------|-------|
  | 2.2-UNIT-005 latest 写入 | DailyReview w/ watch_pool 调 _save_review | latest_review.json 含 watch_pool key | unit |
  | 2.2-UNIT-006 history 追加 | 同上 | review_history.json 末尾 entry 含 watch_pool | unit |
  | 2.2-UNIT-007 同日去重 | 同 date 调 _save_review 两次 | history 该 date 仅 1 条且为最后一次 | unit |
  | 2.2-INT-003 真实数据 schema | 加载 data/review_history.json 真实文件 | 末 entry wp[0] 含 13 keys（code/name/board_count/.../pool_tag） | integration |

- [x] **T3: AC3 验收 — review.html 模板字符级冻结** `[AC3]`
  - [x] 测试：SHA256 baseline pinned in `tests/fixtures/watch_pool_snapshot_baselines.json`（review.html=`209b33ec...`）+ 关键子串断言（UNIT-009 parametrize ×3：watchPool computed / fetch /api/review / 区域 D 注释）
  - [x] 不改 review.html 任何字符 ✓

  **Test Specs**（white-box scenarios from test-design）:
  | Scenario | Input | Expected | Level |
  |----------|-------|----------|-------|
  | 2.2-UNIT-008 SHA256 hash | 读 src/static/review.html | hash == baseline.json['review.html'] | unit |
  | 2.2-UNIT-009 子串断言 (×3) | 读 review.html | 3 子串均存在：'watchPool = computed' / 'fetch(\\'/api/review\\')' / '<!-- ============ 区域 D' | unit |

- [x] **T4: AC4 验收 — scheduler / cron 字符级冻结** `[AC4]`
  - [x] 测试：3 个文件 SHA256 全部 == `tests/fixtures/watch_pool_snapshot_baselines.json` pinned（UNIT-010/011/012）：main.py=`6f522431...` / scheduler.py=`940936c3...` / daily_review.py=`393cdcbc...`

  **Test Specs**（white-box scenarios from test-design）:
  | Scenario | Input | Expected | Level |
  |----------|-------|----------|-------|
  | 2.2-UNIT-010 main.py | 读 main.py | hash == baseline['main.py'] | unit |
  | 2.2-UNIT-011 scheduler.py | 读 src/scheduler.py | hash == baseline['scheduler.py'] | unit |
  | 2.2-UNIT-012 daily_review.py | 读 src/engine/daily_review.py | hash == baseline['daily_review.py'] | unit |

### Integration & Verification Tasks

- [x] **T5: 端到端时序一致性集成测试** `[AC1, AC2, AC3]`（DoD #1）
  - [x] 模拟交易日时间轴：mock `src.config.now_cn` 分别返回 9:30 / 14:30 / 15:30 / 15:50 四个时刻（INT-007/008/009/010）
  - [x] mock latest_ranking.json（fixture C）与 review_history.json 最后一条（fixture A "昨日冻结"）+ latest_review.json（fixture B "今日冻结"）三方不同
  - [x] 在 9:30 / 14:30 调 GET /api/review → 验证 watch_pool == fixture A（昨日 history）
  - [x] 在 15:30 调 GET /api/review → 验证 watch_pool == 昨日 latest_review（today's cron 未跑场景）
  - [x] 在 15:50 调 GET /api/review → 验证 watch_pool == fixture B（今日 15:45 新冻结）
  - [x] 跨时段反向断言：watch_pool != fixture C 重算结果

- [x] **T6: 回归保护测试** `[AC5]`（DoD #2）
  - [x] 9:27 邮件链路：静态分析 `src/notify/email_sender.py` 不引用 `watch_pool` 字符串（结构性反例 → INT-005）
  - [x] decision_tracker.create_premarket_record（scheduler.py:625-636）：模拟 scheduler 真实路径读 latest_review.json（fixture B），结果 record.watch_pool[0].code == "600519"（INT-006）
  - [x] /api/review 顶层 17 字段 baseline 守护（INT-004 + baseline.json["api_review_top_keys"]）

- [x] **T7: 边缘场景测试** `[AC1, AC2]`
  - [x] BLIND-BOUNDARY-001: legacy entry 缺 watch_pool 字段 → /api/review 不抛错
  - [x] BLIND-BOUNDARY-002: history.json=[] → 兜底 latest_review.json
  - [x] BLIND-BOUNDARY-003: review_data["watch_pool"]=[] → 透传空列表（v-else 空状态）
  - [x] BLIND-BOUNDARY-004: now==15:00:00 边界 → 走 latest 分支（n < cutoff 判断为 False）
  - [x] BLIND-ERROR-001: review_history.json JSON 损坏 → 兜底 latest_review.json
  - [x] BLIND-ERROR-002: 两文件均缺 → 返回 {}
  - [x] BLIND-ERROR-003: latest_ranking.json 缺失不影响响应（关键回归 — 旧代码会进 if 分支）
  - [x] BLIND-FLOW-001: 写后立即 GET 反映新内容（手动 *refresh-review 场景）
  - [x] BLIND-DATA-001: line 334 dict() + GET 不写盘 → 磁盘文件未被 mutate

- [x] **T8: 最终验收** `[ALL ACs]`
  - [x] 全测试 PASS：133/133（46 email-sync-1.1 + 48 decision-consistency-2.1 + 5 fallback + 34 本 Story）
  - [x] `pytest tests/ -W error` 严格模式全绿（实测 2026-05-08）
  - [x] Dev Log 完整记录改动 + T0 import 决策 + L-1/L-2/L-3 mitigation
  - [x] Status → Review

### AC Coverage Matrix

| Task | AC1 | AC2 | AC3 | AC4 | AC5 |
|------|:---:|:---:|:---:|:---:|:---:|
| T0: import 决策 | ✓ |   |   |   |   |
| T1: 删除 /api/review 重算块 | ✓ |   |   |   |   |
| T2: review_history 含 watch_pool 验收 |   | ✓ |   |   |   |
| T3: review.html 模板冻结 |   |   | ✓ |   |   |
| T4: scheduler / cron 冻结 |   |   |   | ✓ |   |
| T5: 端到端时序一致性 | ✓ | ✓ | ✓ |   |   |
| T6: 回归保护 |   |   |   |   | ✓ |
| T7: 边缘场景 | ✓ | ✓ |   |   |   |
| T8: 最终验收 | ✓ | ✓ | ✓ | ✓ | ✓ |

---

## Dev Notes

### Technical Constraints

| 类别 | 约束 | 来源 |
|---|---|---|
| 冻结时机 | 沿用既有 15:45 `run_daily_review` 冻结时机；不迁移到 15:30 cycle_update | scope 文件 #34（轻微偏离 — 见 BR 注释）|
| review_history 已含 watch_pool | 实测最后一条 entry 含 watch_pool（17 字段，长度 7）— 数据迁移成本 = 0 | 文件实测 |
| 删除唯一覆盖路径 | app.py:get_review line 336-343 是唯一覆盖 watch_pool 的路径；删除后 /api/review 完全透传 review_data["watch_pool"] | 代码静态分析 |
| 不动 _generate_watch_pool 算法 | build_watch_pool_from_ranking 函数本体保留（daily_review 调用链需要 + 单元测试）| BR-5.3 |
| 不引入新依赖 | 不加 Redis / pydantic / message bus；继续用 json + 文件 IO | scope 文件 #106 |
| 前端零改动 | review.html 模板 + JS 完全不动（watchPool computed 已读 review.value.watch_pool）| BR-3.x / BR-5.6 |

### Accumulated Context (From Previous Stories)

| Resource | Source Story | 状态 | Action |
|---|---|---|---|
| `daily_review.run_daily_review` + `_save_review` | 既有 | 15:45 冻结 watch_pool 写 latest_review.json + review_history.json | REUSE — 本 Story 完全不动 |
| `daily_review._generate_watch_pool` / `build_watch_pool_from_ranking` | 既有 | 算法本体已稳定（top30 + 主板 + 两条规则）| REUSE — 仅 daily_review 内部调用，本 Story 后 app.py 不再调用 |
| `data/latest_review.json` / `data/review_history.json` | 既有 | 已含 watch_pool 字段 | REUSE — 数据契约不变 |
| `data/latest_ranking.json` | 既有（cycle_update 维护） | 交易日内持续刷新 | REUSE — 本 Story 后 /api/review 不再从此读 watch_pool；其他读取路径不受影响 |
| `decision_tracker.create_premarket_record` | 既有 | 9:27 从 latest_review.json 读 watch_pool | REUSE — 本 Story 后该 watch_pool 仍是昨日 15:45 冻结快照（与 9:27 决策时刻语义一致）|
| `email_sender.send_screener_report` | email-sync-1.1 + decision-consistency-2.1 | 9:27 邮件链路 | REUSE — 邮件不读 watch_pool，本 Story 零影响 |
| Database Tables | — | N/A — 本 Story 无数据库写入 | — |
| Shared Models | — | N/A — 复用既有 dict 结构 | — |

### Database Design

N/A — 不涉及数据库变更。

### Data Synchronization Requirements

- [x] 本 Story **不**引入新文件；仅删除一处对 `latest_ranking.json` 的实时读取覆盖路径
- [x] `data/latest_review.json` / `data/review_history.json` 字段契约不变（既有 watch_pool 字段持续作为冻结快照源）

### Data Models

**`/api/review` Response Schema**（本 Story 后字段不变；watch_pool 来源变更）：

```python
{
    "date": str,                    # review_data 来源决定（昨日 / 今日）
    "limit_up_count": int,
    "main_board_limit_up": int,
    "main_theme": str,
    "theme_strength": str,
    "lianban_ladder": list,
    "highest_board": int,
    "prev_board_groups": list,      # 仍用当前公式重算（line 345-359 保留）
    "promotion_summary": list,      # 仍用当前公式重算（line 345-359 保留）
    "scorecard": dict,              # 仍用当前公式重算（line 345-359 保留）
    "sector_groups": dict,
    "sector_zt_stats": list,
    "concept_zt_stats": list,
    "failed_promotion_list": list,
    "watch_pool": list[dict],       # ⭐ 本 Story 后：来自 review_data["watch_pool"]（review_history.json 中昨日 15:45 冻结快照），不再实时重算
    "market_summary": str,
    "relay_env": dict,
}
```

**`watch_pool` 单元素 schema**（沿用既有 `WatchCandidate` + 透传字段，本 Story 不变）：

```python
{
    "code": str,
    "name": str,
    "board_count": int,
    "industry": str,
    "close": float,
    "market_cap_yi": float,
    "total_gain_pct": float,
    "reason": str,
    "watch_points": str,
    "auction_range": str,
    "top_concepts": list[str],   # 由 _enrich_review_top_concepts 注入
    "is_main_board": bool,
    "pool_tag": str,             # "高位接力" / "首板新标"
}
```

### File Locations

| 文件 | 操作 | 涉及行号（起草时） | 关联 AC |
|---|---|---|---|
| `src/api/app.py` | **修改** | 删除 line 336-343 的 watch_pool 重算块；line 296 `from src.engine.daily_review import build_watch_pool_from_ranking` 由 T0 决定保留/删除 | AC1 |
| `src/engine/daily_review.py` | **不动** | — | AC2 / AC5 |
| `src/static/review.html` | **不动** | — | AC3 / AC5 |
| `main.py` | **不动** | — | AC4 / AC5 |
| `src/scheduler.py` | **不动** | — | AC4 / AC5 |
| `tests/test_review_watch_pool_snapshot.py` | **新建** | — | AC1-AC5（具体测试用例由 QA test-design 给出）|

### Deliverable Bindings

```yaml
deliverable_bindings:
  - deliverable: "src/api/app.py::get_review (modified — watch_pool override removed)"
    consumer: "src/static/review.html loadDaily() fetch('/api/review')"
    binding_type: route_mount
    verify: "src/static/review.html 含 'fetch\\(.*\\/api\\/review\\)' 模式（既有 line 1031）"

  - deliverable: "data/review_history.json (existing — watch_pool field consumed by /api/review)"
    consumer: "src/api/app.py::get_review (read path)"
    binding_type: config_read
    verify: "src/api/app.py 含 'review_history.json' 字符串引用（既有 line 299）"

  - deliverable: "tests/test_review_watch_pool_snapshot.py (or path chosen by Architect)"
    consumer: "pytest discovery (project test runner)"
    binding_type: import_usage
    verify: "pytest 收集到该测试文件中的测试用例（具体用例数与名称由 QA test-design 给出）"
```

### Testing Requirements

- **测试设计层级**：`standard`（用户在指令中明确要求）
- **前置流程**：QA *test-design 在开发前出测试设计文档，Dev 据此实现 T5/T6/T7 中的具体集成与边缘场景用例
- **覆盖重点**：
  1. /api/review 三时段（9:30 / 14:30 / 15:30 / 15:50）watch_pool 来源验证（AC1 主验收）
  2. review_history.json 含 watch_pool 字段（AC2 既有数据验收）
  3. review.html 模板字符级冻结（AC3 BR-3.1 + BR-3.2）
  4. main.py / scheduler.py / daily_review.py 字符级冻结（AC4 BR-4.1-4）
  5. /api/review 顶层 schema 不变（AC5 回归）
  6. 边缘场景：legacy snapshot 缺 watch_pool / review_history 损坏 / 手动 *refresh-review / 空 watch_pool（AC1 / AC2）
  7. 9:27 邮件链路 + decision_tracker.create_premarket_record 不受影响（AC5 跨 Story 集成）

---

## Change Log

| Date | Agent | Status Transition | Details/Link |
|------|-------|-------------------|--------------|
| 2026-05-08 | SM (Phil) | Created → AwaitingArchReview | Brownfield 单 Story 起草；偏离标准流程 8 条沿用 email-sync-1.1 + decision-consistency-2.1 路径（无 PRD 分片 / 无 architecture 目录 / scope 文件作虚拟 epic / 跳过 Epic YAML / 跳过 架构上下文 / 跳过 累积校验 / 跳过 Decision 8A / 强制 test_design_level=standard）；scope 文件 [docs/prd/iteration-2-scope.md#story-2-2](../prd/iteration-2-scope.md) 作为真源；HANDOFF 至 architect *review |
| 2026-05-08 | Architect (Wright) | AwaitingArchReview → AwaitingTestDesign | Score: 9.5/10, 0 critical / 0 major / 3 minor (L-1 docstring stale / L-2 import 删除决策 / L-3 测试基线 hardcode)；所有 line refs 实测确认；同意 scope 文件 #34 时机偏离 + #36 UI 占位推迟；HANDOFF 至 qa *test-design |
| 2026-05-08 | QA (Turing) | AwaitingTestDesign → TestDesignComplete → Approved | 测试设计完成（standard）：32 scenarios（U:15 / I:17 / E2E:0），P0:9 P1:16 P2:7；BLIND-SPOT 9（BOUNDARY 4 / ERROR 3 / FLOW 1 / DATA 1）；测试设计文档 docs/qa/assessments/watch-pool-snapshot-2.2-test-design-20260508.md；测试骨架 tests/test_review_watch_pool_snapshot.py（34 collected — UNIT-009 参数化×3）；项目总用例数 99 → 133；两阶段状态转换 per story-status-transitions.yaml；HANDOFF 至 dev *develop-story |
| 2026-05-08 | Dev (墨子) | Approved → Review | TDD 实现完成。`src/api/app.py:get_review` 删除原 line 336-343 watch_pool 重算块 + 删除原 line 296 局部 import `build_watch_pool_from_ranking` + 删除原 line 300 dead 变量 `ranking_file` + 重写 line 286-298 docstring（合并 Architect L-1/L-2 同 commit）。新建 `tests/fixtures/watch_pool_snapshot_baselines.json`（4 文件 SHA256 + /api/review 17 keys）+ `tests/test_review_watch_pool_snapshot.py` 实现 32 scenarios（34 collected：含 UNIT-009 parametrize×3 + 9 BLIND-SPOT + T5 时序矩阵 INT-007/008/009/010）。pytest tests/ -W error 严格模式 133/133 全绿（46 email-sync-1.1 + 48 decision-consistency-2.1 + 5 fallback + 34 本 Story；UNIT-013 强校验 EXPECTED_TOTAL=133）。Architect 3 项 Low Issue 全部以 mitigation 测试守护：L-1 → UNIT-003 反射 `__doc__` 断言；L-2 → UNIT-002 grep 源码断言；L-3 → UNIT-013 collect 总数。frozen 文件（review.html / main.py / scheduler.py / daily_review.py）SHA256 全部锁定（UNIT-008/010/011/012）。HANDOFF 至 qa *review watch-pool-snapshot-2.2 |
| 2026-05-08 15:32 | QA (Turing) | Review → Done | Round 1, Gate: PASS。独立 pytest 133/133 全绿；AC 5/5 VERIFIED；复选框 46/46 一致；BLIND-SPOT 9/9 全覆盖；4 frozen 文件 SHA256 锁定；Architect L-1/L-2/L-3 全部 mitigation 测试守护。Gate file: `docs/qa/gates/watch-pool-snapshot-2.2-review-show-yesterday-frozen.yml`。Risk: MEDIUM, Review mode: automated_plus_spot_check。0 critical / 0 high / 0 medium / 0 low 待办。HANDOFF 至 qa *finalize-commit watch-pool-snapshot-2.2 |

---

## Dev Agent Record

### Agent Model Used
- **Agent**: 墨子 (Dev) · claude-opus-4-7[1m]
- **Date**: 2026-05-08
- **Mode**: develop-story (TDD, plan)

### Implementation Summary

**核心改动 = "删除一处实时重算 + 重写一段文档 + 锁定四个文件字符级冻结"**：

1. **`src/api/app.py:get_review` 三处变更（同一 commit，合并 Architect L-1/L-2）**：
   - 删除原 line 336-343 watch_pool 重算块（含开头注释 + try/except 共 8 行）
   - 删除原 line 296 局部 import `from src.engine.daily_review import build_watch_pool_from_ranking`
   - 删除原 line 300 dead 变量 `ranking_file = DATA_DIR / "latest_ranking.json"`（删除重算块后即 dead code）
   - 重写原 line 286-294 docstring：陈述 watch_pool 来自 review_history / latest_review.json 中 15:45 冻结快照
2. **创建 `tests/fixtures/watch_pool_snapshot_baselines.json`**：
   - SHA256 baseline 锁定 4 个 frozen 文件（review.html / main.py / scheduler.py / daily_review.py）
   - `/api/review` 顶层 17 keys 集合（防 schema 漂移）
3. **创建 `tests/test_review_watch_pool_snapshot.py`**：
   - 实现 32 scenarios（34 collected：UNIT-009 parametrize ×3）
   - 覆盖：AC1（6）+ AC2（4）+ AC3（4）+ AC4（3）+ AC5（4）+ T5 时序（4）+ BLIND-SPOT（9）
4. **0 改动文件（AC4 字符级冻结）**：`src/static/review.html` / `main.py` / `src/scheduler.py` / `src/engine/daily_review.py` SHA256 不变

**测试**：133 全绿（`pytest tests/ -W error` 严格模式）；UNIT-013 强校验 EXPECTED_TOTAL=133。

### Database Changes (Structured)
```yaml
{}  # N/A — 本 Story 不涉及数据库变更
```

### API Endpoints Created (Structured)
```yaml
{}  # N/A — 本 Story 不新增 endpoint，仅修改既有 GET /api/review 内部逻辑（删 watch_pool 重算块）
# Note: GET /api/review 函数签名 + 顶层返回 schema 字符级冻结（INT-004 baseline 守护）
```

### Shared Models Created (Structured)
```yaml
{}  # N/A — 本 Story 不涉及新增共享 typed model；watch_pool 字段 schema 沿用既有 WatchCandidate dataclass + 透传 top_concepts/is_main_board/pool_tag
```

### File List

**Modified**:
- `src/api/app.py` — `get_review` 函数体内删除 watch_pool 重算块（原 line 336-343）+ 删除 dead import（原 line 296）+ 删除 dead 变量 `ranking_file`（原 line 300）+ 重写 docstring（原 line 286-294 → 现 line 286-298）；同一 commit 落地 Architect L-1/L-2

**Created**:
- `tests/test_review_watch_pool_snapshot.py` — 实现 QA test-design 全部 32 scenarios（34 collected：含 UNIT-009 parametrize ×3），覆盖 AC1-AC5 + T5 4 时段时序 + 9 BLIND-SPOT
- `tests/fixtures/watch_pool_snapshot_baselines.json` — SHA256 baseline（4 文件）+ /api/review 顶层 17 keys 字段集
- `docs/dev/logs/watch-pool-snapshot-2.2-dev-log.md` — Dev Log（含 T0 import 决策 + L-1/L-2/L-3 mitigation 闭环 + 实施关键决策 6 条）

**Unchanged (AC4 字符级冻结)**:
- `src/static/review.html` ✓ — SHA256=`209b33ec...`
- `main.py` ✓ — SHA256=`6f522431...`
- `src/scheduler.py` ✓ — SHA256=`940936c3...`
- `src/engine/daily_review.py` ✓ — SHA256=`393cdcbc...`

### Dev Log Reference
- `docs/dev/logs/watch-pool-snapshot-2.2-dev-log.md`

### Open Issues
None — Architect 3 项 Low Issue 全部以 mitigation 测试守护 + commit 同步落地（L-1 → UNIT-003；L-2 → UNIT-002；L-3 → UNIT-013）。

---

## Architect Review Results

### Review Date: 2026-05-08
### Reviewed By: Wright (Architect)
### Architecture Score: 9.5/10
### Review Round: 1

### Decision: Approved (with minor recommendations) → AwaitingTestDesign

### 验证记录（line refs ground-truth）

| Story 引用 | 实际验证 | 状态 |
|---|---|---|
| `app.py:284` `@app.get("/api/review")` | line 284 ✓ | ✓ |
| `app.py:296` `from src.engine.daily_review import build_watch_pool_from_ranking` | line 296 ✓ | ✓ |
| `app.py:336-343` watch_pool 重算块 | line 336-343 字符级匹配 | ✓ |
| `app.py:345-359` scorecard / promotion_summary 重算块 | line 345-359 ✓ | ✓ |
| `app.py:395-396` `_strip_meta_concepts_inplace` watch_pool 清洗 | line 395-396 ✓ | ✓ |
| `app.py:304` cutoff=15:00 时间门控 | `n.replace(hour=15, minute=0, second=0, microsecond=0)` ✓ | ✓ |
| `daily_review.py:111-204` `run_daily_review` | line 111-204 ✓（含 line 192 `_generate_watch_pool` 调用） | ✓ |
| `daily_review.py:1387-1405` `_save_review` | 实际 line 1387-1406（结束行 +1，无功能差） | ✓ |
| `daily_review.py:805-832` `WatchCandidate` schema + 透传字段 | line 815-830 完整 | ✓ |
| `review.html:454-489` 区域 D 模板 | line 454-489 ✓（`<!-- ============ 区域 D` ↔ `</div>`） | ✓ |
| `review.html:1010` `watchPool = computed(...)` | line 1010 ✓ | ✓ |
| `review.html:1029-1036` `loadDaily` + `fetch('/api/review')` | line 1029-1036 ✓（fetch 在 1031） | ✓ |
| `review.html:506-507` 刷新复盘按钮 | line 506-507 ✓ | ✓ |
| `main.py:75` `print(f"[复盘] 异常: {e}")` | line 75 ✓ | ✓ |
| `main.py:86-94` post_market cron 配置 | line 86-94 ✓ | ✓ |
| `scheduler.py:625-636` `decision_tracker.create_premarket_record` | line 625-636 ✓ | ✓ |
| `scheduler.py:706-711` `_background_tasks` 调 `run_cycle_update` | line 706-711 ✓ | ✓ |
| `review_history.json` 最近 entry 含 watch_pool（17 字段，长度 7） | 实测：6/6 entry 均含 watch_pool；最后一条 2026-05-07 共 17 字段，watch_pool len=7 | ✓ |
| 现有测试基线 = 99 (= 46 email-sync + 48 decision-consistency + 5 fallback) | `pytest --collect-only` 收集 99 用例 | ✓ |

### Issues

#### Critical Issues (0)
（无）

#### High Issues (0)
（无）

#### Medium Issues (0)
（无）

#### Low Issues (3)

- **L-1：`get_review` docstring 未同步更新（app.py:286-294）** `[AC1]`
  - 当前 docstring（line 292-293）写：`无论哪种情况，watch_pool（次日观察池）始终用 latest_ranking 重算 — "次日观察池"是面向 next-day 的语义，应反映最新已知的 ranking 状态。`
  - Story T1 仅提"删除 line 336-343 整块（含 `# watch_pool 始终用最新 ranking 重算...` 注释 + try/except）"，**未涵盖 docstring 这两行**。
  - 删除 line 336-343 后，docstring 的语义陈述与实际行为**直接矛盾**（实际行为变成"透传 review_data['watch_pool']，不再实时重算"）。
  - **Recommendation**：在 T1 中追加一项："同步重写 `get_review` docstring（line 286-294），说明 watch_pool 透传 review_data['watch_pool']（来自 review_history / latest_review.json 中 15:45 冻结快照），不再实时重算"。
  - Severity 评估：Low（不影响运行，仅误导后续 reader / Code Review）。Dev 在 T1 实施时一并处理即可，无需 SM 重出 Story。

- **L-2：T0 import 决策（建议）** `[AC1, BR-1.5]`
  - Architect 决策：**删除 `app.py:296` 的局部 import `from src.engine.daily_review import build_watch_pool_from_ranking`**。
  - 理由：
    1. 静态分析确认（grep）：删除 line 336-343 后，`build_watch_pool_from_ranking` 在 `app.py` 中无任何其他引用；唯一调用方 `daily_review._generate_watch_pool` 仍保留（daily_review.py:754）。
    2. KISS — 未使用 import 是 dead code；linter（ruff F401）会持续告警。
    3. 回滚成本低 — 若未来需要恢复兜底，git 历史保留删除 commit，重加 1 行 import 即可。
    4. 与 BR-5.4 "不引入新依赖" 精神一致：减少 app.py 与 daily_review 内部实现的耦合面。
  - 这条决策需写入 Dev Log（如 Story T0 第二项要求）。

- **L-3：T8 测试基线数字硬编码风险** `[T8 DoD]`
  - T8 写 "全测试 PASS（含本 Story + email-sync-1.1 baseline 46 + decision-consistency-2.1 48 + fallback 5）" — 总数 99 已实测确认。
  - 风险：若新 test 文件导入 conftest / fixture 与既有 test 名冲突，可能**让既有 case 数变少而 PASS 仍绿**（例如重名覆盖）。
  - **Recommendation**：T8 增加断言："`pytest --collect-only -q | tail -1` 报告的总用例数 = 99 + 本 Story 新增用例数"。这是 **跨 Story 回归保护测试**（QA *test-design 时具体定义）。

### Recommendations（Dev/QA 实施时参考）

1. **T1 实施时一并改 docstring**（合并 L-1）：删除 line 336-343 + 重写 line 286-294 docstring，**同一个 commit**。理由：行为与文档 commit 必须同步，避免 git blame 上"行为变了但文档没变"的窗口。
2. **T0 决策记入 Dev Log**：明确"删除 line 296 import"+ 删除原因（静态分析无其他调用方）。
3. **测试组织建议**：新建 `tests/test_review_watch_pool_snapshot.py` 与现有 `tests/notify/` 平级，避免误归类到 notify。本 Story 测的是 review API，不是邮件链路。
4. **集成测试 mock 策略**：T5 的 4 时刻 mock `now_cn()` 时，建议直接 patch `src.config.now_cn`（而不是 `src.api.app.now_cn`），因为 app.py:295 用的是 `from src.config import now_cn` 局部 import — fixture 应在调 GET /api/review 前 patch 上游 module。
5. **AC4 字符级冻结测试**（T4）：实现时建议用 `hashlib.sha256(file.read_bytes()).hexdigest()` 做 baseline 比对（一次性写入 baseline.json，后续 CI 比 hash），比 grep 字面匹配更稳。
6. **scope 文件 #34 偏离已充分说明**：Story 在 line 80-81 显式记录"沿用既有 15:45 冻结时机，不迁移到 15:30"，论据三条（依赖链 / 用户感知 / KISS）。Architect 同意此偏离，**无需 PCP**（scope 文件本身是 forward-looking 假设，未冻结为 PRD 合同）。
7. **scope #36 占位 UI 显式不做**（BR-3.4）：review.html 当前无日期选择器，"今日 < 15:00 占位"是未来 UX 假设；本 Story 只解决"时序错位"主诉，UI 占位文案可在后续 Story 引入日期选择器时一并落地。同意此 scope 切割。

### 评分明细（10 分制）

| 维度 | 分数 | 备注 |
|---|---|---|
| tech_stack_compliance | 1.0 | Python + FastAPI + Vue3 + JSON 文件，无新依赖 |
| naming_convention_adherence | 1.0 | 仅删除既有代码，无新命名 |
| project_structure_alignment | 1.0 | 文件位置全部既有 |
| api_design_consistency | 1.0 | /api/review 签名 + 顶层 schema 不变（BR-5.1）|
| data_model_accuracy | 1.0 | WatchCandidate dataclass + 透传字段保留；review_history 字段 17 个稳定 |
| architecture_pattern_compliance | 1.0 | 冻结快照模式与 latest_*.json + *_history.json 既有持久化模式一致 |
| complete_dependency_mapping | 1.0 | 上下游全部识别（_save_review / _generate_watch_pool / scheduler:633 / decision_tracker / 邮件链）|
| integration_feasibility | 1.0 | 删 8 行代码 + 重写 docstring + 1 行 import 决策，平凡可行 |
| accurate_documentation_references | 0.5 | 所有 line refs 实测准确；扣 0.5 因 docstring 未同步（L-1）|
| overall_implementation_feasibility | 1.0 | Standard 难度边界清晰 |
| **Total** | **9.5/10** | **PASS（≥7 阈值）** |

### Test Design Routing

- `test_design_level: standard`（Story 声明，Architect 接受）
- 复杂度 indicators：3（cross-service 数据流：app.py ↔ daily_review.py ↔ review_history.json；时间门控边界；前后端契约）
- security_sensitive: false（无认证 / 加密 / 权限改动）
- 决策：Score 9.5 ≥ 7 且 test_design_level = standard → **AwaitingTestDesign，转 QA *test-design**

---

## QA Test Design Metadata

- **Level:** Standard
- **Status:** Complete
- **Test Design Status:** Complete
- **Document:** [docs/qa/assessments/watch-pool-snapshot-2.2-test-design-20260508.md](../qa/assessments/watch-pool-snapshot-2.2-test-design-20260508.md)
- **Test Skeleton:** [tests/test_review_watch_pool_snapshot.py](../../tests/test_review_watch_pool_snapshot.py)
- **Risk Profile:** N/A（Architect Review 已识别 3 项 Low 风险，QA 测试设计已合并 mitigation）
- **Total Scenarios:** 32（pytest collected: 34 — UNIT-009 参数化×3）
- **By Level:** Unit 15 (47%) · Integration 17 (53%) · E2E 0
- **By Priority:** P0:9 · P1:16 · P2:7 · P3:0
- **Blind Spots:** 9（BOUNDARY 4 / ERROR 3 / FLOW 1 / DATA 1）
- **Baseline Guard:** 项目用例数 99 → 133（Architect L-3 防漂移已落入 2.2-UNIT-013）

---

## QA Results

- **Round**: 1
- **Risk Level**: MEDIUM
- **Review Mode**: automated_plus_spot_check
- **Gate**: PASS
- **Tests**: 133/133 automated · E2E skipped (后端项目无 UI 改动；spot-check 通过 INT-007/008/009/010 4 时段时序矩阵覆盖)
- **AC Coverage**: 5/5 ACs VERIFIED (100%)
- **Task Checkboxes**: 46/46 checked & consistent (100%)
- **Blind Spots**: 9/9 covered (100%) — 4 BOUNDARY · 3 ERROR · 1 FLOW · 1 DATA
- **Issues**: 0 critical / 0 high / 0 medium / 0 low
- **Architect Low Issues**: L-1/L-2/L-3 全部以 mitigation 测试守护并通过
- **Gate File**: `docs/qa/gates/watch-pool-snapshot-2.2-review-show-yesterday-frozen.yml`
- **Test Design**: `docs/qa/assessments/watch-pool-snapshot-2.2-test-design-20260508.md`
- **Evidence**: N/A（无 issue，未生成 evidence 目录）
- **Reviewer**: Turing (QA) · 2026-05-08 15:32 (UTC+8)

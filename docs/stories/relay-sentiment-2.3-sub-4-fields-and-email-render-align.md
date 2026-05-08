# Story relay-sentiment-2.3: 接力情绪 sub 4 字段（数据源验收 + 邮件 render 与看板 v-if 对齐）

## Story

```yaml
Story:
  id: relay-sentiment-2.3
  title: 接力情绪 sub 4 字段数据源回归锁定 + 邮件 sub 行与 dashboard v-if 行为对齐
  epic: iteration-2 brownfield (virtual epic — 真源为 docs/prd/iteration-2-scope.md)
  status: Approved
  mode: plan
  repository: monolith
  priority: P1
  estimated_complexity: standard
  test_design_level: standard
  story_type: brownfield-enhancement
```

**As a** A 股短线交易者（604491810@qq.com 邮件唯一收件人 + 浏览器看板唯一使用者），
**I want** 邮件第 4 格"接力情绪"sub 行（中位数 / 高开>5% / 平开±2% / 低开<-5%）在数据缺失时**不渲染**（整段隐藏），与浏览器看板 `v-if="ydayAvg && ydayAvg.median_change_pct != null"` 保持一致；当数据可用时，4 个字段必须由 `compute_yesterday_main_board_auction` 正确产出，
**so that** 我在邮件看到的"接力情绪"sub 行**要么显示完整 4 字段，要么整行不出现**，避免出现"中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—"这种半破损占位文案（用户 2026-05-08 实盘反馈：邮件 vs 看板对该行的视觉一致性差）。

---

## 背景与问题

### 当前行为分歧

| 入口 | 文件 / 行号 | 数据缺失（`y_avg` 为空 / `median_change_pct == None`）时行为 |
|---|---|---|
| 邮件 `email_sender.py` 第 4 格 sub 行 | `src/notify/email_sender.py:487-491, 499` | **硬编码**渲染 `"中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—"`（始终输出占位 `<div>`）|
| 看板 `index.html` 第 4 格 sub 行 | `src/static/index.html:562-567` | **整段 `<div class="mb-sub">` 不渲染**（`v-if="ydayAvg && ydayAvg.median_change_pct != null"`）|

**结果**：
- 用户在 dashboard 看到"接力情绪 — / -"（只剩主值，无 sub 行）
- 用户在邮件看到"接力情绪 — / 中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—"（多出半破损 sub 行）

### 实测当日 (2026-05-08) 数据状态

`data/latest_leader.json`：
```json
{
  "yesterday_main_board_avg_auction": null,
  ...
}
```

`compute_yesterday_main_board_auction` 在以下情形会返回 None：
- `limit_up_history` 为空（首次跑 / 历史断档）
- 昨日（`past_dates[0]`）的 df 为空
- 主板代码列表 `codes` 经 `_is_main_board_code` 过滤为空
- spot_df 命中率不足 + 新浪兜底未拉到任何样本
- `changes` 列表为空（spot_df 中无任一昨日涨停股可对齐的 `pre_close/open`）

### 用户反馈（2026-05-08）

> 邮件第 4 格"接力情绪" sub 行显示
> "中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—"。
> dashboard `index.html:562-567` 用 `v-if="median_change_pct != null"` 隐藏整行（看板也没显示）。

### 真源约束（用户 2026-05-08 已选定）

[Source: docs/prd/iteration-2-scope.md#story-2-3]

> **改动范围**：
> 1. `compute_yesterday_main_board_auction` 增加 4 个统计字段（`median_change_pct` / `high5_count` / `flat2_count` / `low5_count`）
> 2. email_sender 第 4 格 sub 行：当 4 个字段任一为 None 时，**整行不渲染**（与 dashboard `v-if` 行为对齐）

### Scope 执行口径（SM 2026-05-08 起草确认）

> ⚠️ **重要事实校核**：scope 文件 #50-52 行声称 `compute_yesterday_main_board_auction` 仅返回
> `{date, sample_count, avg_change_pct, positive_count, negative_count, limit_down_count}` —
> 实际代码核查（`src/engine/leader_feedback.py:499-509`，commit `37af368` "邮件重构") 中
> 4 个字段（`median_change_pct` / `high5_count` / `flat2_count` / `low5_count`）**已存在**。
>
> 用户 2026-05-08 决策：**按原 scope 文字执行（C 选项）** —
> AC1（数据源 4 字段）以"既有实现 + 单元测试锁定（regression-protection）"形式落地；
> AC2（邮件 render 与 dashboard v-if 对齐）为本 Story 的**净新行为变更**。
>
> 本 Story 不涉及"为何 `yesterday_main_board_avg_auction` 在产线为 None"的追根（属数据可用性问题，
> 超出 iteration-2 "体感修复"范围；如需追根另立 Story）。

---

## 改动范围（来自 scope）

[Source: docs/prd/iteration-2-scope.md#story-2-3]

1. **`compute_yesterday_main_board_auction` 增加 4 个统计字段**（既有实现验收 + 单元测试锁定）：
   - `median_change_pct`: 样本竞价涨幅中位数
   - `high5_count`: 高开 > 5% 的数量
   - `flat2_count`: 平开 [-2%, +2%] 的数量
   - `low5_count`: 低开 < -5% 的数量
2. **`email_sender` 第 4 格 sub 行**：当 4 个字段任一为 None 时，**整段 sub `<div>` 不渲染**（与 dashboard `v-if` 行为对齐）

---

## Acceptance Criteria

### AC1: `compute_yesterday_main_board_auction` 返回 dict 含 4 个统计字段（既有 + 测试锁定）

**Scenario**
```gherkin
GIVEN scheduler.run_screener_update 在 9:27 cron 或用户手动调用下执行
  AND limit_up_history 含昨日有效 entry（>= 1 主板涨停股）
  AND spot_df 含该主板涨停股的 pre_close + open 字段（命中率 ≥ 0.7 或新浪兜底成功）
WHEN compute_yesterday_main_board_auction(limit_up_hist, spot_df) 返回非 None
THEN
  - 返回 dict 必须包含**全部** 10 字段：
    {date, sample_count, avg_change_pct, median_change_pct, high5_count, flat2_count, low5_count,
     positive_count, negative_count, limit_down_count}
  - 4 个新增字段语义：
    * median_change_pct: 样本竞价涨幅中位数（保留 2 位小数 round）
    * high5_count: chg > 5 的样本数量（严格大于）
    * flat2_count: -2 <= chg <= 2 的样本数量（闭区间）
    * low5_count: chg < -5 的样本数量（严格小于）
  - 4 字段在 sample_count > 0 时永远为 int 或 float（不为 None）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-1.1 | `compute_yesterday_main_board_auction` 函数体保留**当前**实现（`src/engine/leader_feedback.py:393-509`，commit 37af368）；本 Story 不修改算法 |
| BR-1.2 | 4 字段 boundary 严格语义：`high5_count: chg > 5`（严格 >）；`flat2_count: -2 <= chg <= 2`（闭区间）；`low5_count: chg < -5`（严格 <）。边界值 `chg == 5` / `chg == -5` 不计入 high5/low5；`chg == ±2` 计入 flat2 |
| BR-1.3 | 4 字段类型：`median_change_pct` 为 `float`（round 2）；`high5_count` / `flat2_count` / `low5_count` 为 `int`（≥ 0） |
| BR-1.4 | 函数返回 `None` 的早退条件**不变**：`limit_up_history` 空 / `past_dates` 空 / 昨日 df 空 / 主板 codes 空 / spot_df 与新浪兜底均无样本 / `changes` 列表空（line 403-435, 460-461, 492-493） |
| BR-1.5 | 函数公开签名 `compute_yesterday_main_board_auction(limit_up_history, spot_df) -> Optional[dict]` 字符级不变（`src/engine/leader_feedback.py:393-396`）|
| BR-1.6 | 现有同源消费方（`src/scheduler.py:395-401, 447`：写 `latest_leader.json["yesterday_main_board_avg_auction"]`）保持不变 |

**Data Validation**
| Field | Type | Required | Rules | Error Message |
|---|---|---|---|---|
| date | str | ✅ | "%Y%m%d" | — |
| sample_count | int | ✅ | ≥ 1（否则函数返回 None） | — |
| avg_change_pct | float | ✅ | round(2) | — |
| median_change_pct | float | ✅ | round(2) | — |
| high5_count | int | ✅ | ≥ 0 | — |
| flat2_count | int | ✅ | ≥ 0 | — |
| low5_count | int | ✅ | ≥ 0 | — |
| positive_count | int | ✅ | ≥ 0 | — |
| negative_count | int | ✅ | ≥ 0 | — |
| limit_down_count | int | ✅ | ≥ 0 | — |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| limit_up_history 空 / 昨日 df 空 / 无主板代码 / spot 无命中 | — | （静默） | 函数返回 None（既有行为，不变） |
| spot 命中率 < 0.7 + 新浪兜底拉取失败 | — | print "[昨日主板涨停均价] 新浪全市场拉取失败: {e}"（既有 line 458） | 继续走原 spot_df，最终若仍无命中则返回 None |

---

### AC2: 邮件 `email_sender.py` 第 4 格 sub 行与 dashboard `v-if` 行为对齐

**Scenario**
```gherkin
GIVEN send_screener_report 渲染 6 指标格 cell4_html（src/notify/email_sender.py:493-501）
  AND y_avg = ((leader or {}).get("yesterday_main_board_avg_auction") or {})
WHEN y_avg 缺失 / y_avg 为空 dict / y_avg.median_change_pct 为 None
THEN
  - cell4_html 的 sub `<div style="font-size:10px;color:#999;margin-top:1px;">{sub4_str}</div>`
    （现 line 499）**整段不渲染**（输出空字符串）
  - 主值 `<div>{main4}</div>`、title `<div>{title4}</div>`、label `<div>接力情绪</div>` 保持不变
  - 当 y_avg.median_change_pct 不为 None（即 4 字段都有值）时，sub 行按既有 line 481-486 渲染
    "中位数 {median_str} · 高开>5%:{high5} · 平开±2%:{flat2} · 低开<-5%:{low5}"
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-2.1 | 渲染条件改为：`render_sub = _is_num(y_avg.get("median_change_pct"))`（与 dashboard `v-if="ydayAvg && ydayAvg.median_change_pct != null"` 等价；`_is_num` 已存在 line 189-190，对 None / NaN 严格 False） |
| BR-2.2 | 当 `render_sub = False` 时，`sub4_str = ""` + cell4_html 内 sub `<div>` **整段不输出**（即 `f'<div ...>{sub4_str}</div>' if render_sub else ''`），不输出空 `<div></div>` 残留 |
| BR-2.3 | **删除**既有 line 487-491 的 else 分支硬编码 fallback `"中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—"`（fallback 路径由 BR-2.2 整段不渲染替代）|
| BR-2.4 | 主值 main4 / title4 fallback 行为**保留**（line 487-490）：sample 缺失时 main4="—" / title4="昨日涨停 — 只" / main4_color="#6b7280" — 这些是 cell4 主体，与 dashboard line 558-561 行为一致（dashboard 主值显示 "—"，仅隐藏 sub 行）|
| BR-2.5 | dashboard `v-if` 判断条件**当前**使用 `ydayAvg.median_change_pct != null`（line 562）— 邮件端选 `_is_num(median_change_pct)` 对齐；不引入"4 字段任一 None 检查"复杂判断（KISS — median 是 4 字段中最常变化的代表，与 dashboard 现状一致）|
| BR-2.6 | send_screener_report 公开签名 `(cycle_phase, cycle_day, representative, leader, hits, signals, deviations=None, sentiment_data=None, ranking_data=None) -> bool` 字符级不变（INT-005 inspect.signature baseline 沿用 email-sync-1.1 / decision-consistency-2.1）|

**UI Interaction**
| Trigger | Behavior |
|---|---|
| `y_avg = {}`（产线 2026-05-08 实测场景）| 邮件第 4 格：标签"接力情绪" + title "昨日涨停 — 只" + 主值 "—"（保留）；sub 行**整段消失**（不再显示"中位数 — · ..."占位）|
| `y_avg = {sample_count: 5, median_change_pct: 0.5, high5_count: 1, flat2_count: 3, low5_count: 0, ...}` | 邮件第 4 格：sub 行渲染 "中位数 +0.5% · 高开>5%:1 · 平开±2%:3 · 低开<-5%:0"（既有行为）|
| `y_avg = {sample_count: 5, median_change_pct: None, ...}`（理论场景，BR-1.3 排除但仍 defensive）| sub 行整段不渲染（与 dashboard 等价）|

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| y_avg 不是 dict（极端类型异常） | — | （静默） | line 371 `((leader or {}).get(...) or {})` 已兜底为 `{}`；render_sub 走 False → sub 行不渲染 |

---

### AC3: scheduler 数据流不变（既有路径验收）

**Scenario**
```gherkin
GIVEN scheduler.run_screener_update 调用 compute_yesterday_main_board_auction（src/scheduler.py:395）
  AND y_main_board_stats 由该函数返回
WHEN scheduler 写 latest_leader.json（src/scheduler.py:441-455）
THEN
  - leader_data["yesterday_main_board_avg_auction"] = y_main_board_stats（None 或 10 字段 dict）
  - 该字段位置（line 447）保持不变
  - latest_leader.json 写入路径不变（DATA_DIR / "latest_leader.json"，既有 line 453）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-3.1 | `src/scheduler.py:340-455` 4. 高标龙头竞价反馈块字符级不变 |
| BR-3.2 | scheduler 不在本 Story 修改任何文件（与 watch-pool-snapshot-2.2 同模式：scheduler 完全不动） |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| compute_yesterday_main_board_auction 抛错（不应发生，函数未声明 raise） | — | 由既有 `try/except` 覆盖（src/scheduler.py 整段 4 块未显式 try，依赖外层 main 兜底） | 不在本 Story 范围 |

---

### AC4: dashboard 模板字符级冻结

**Scenario**
```gherkin
GIVEN 用户访问 / 浏览器加载 dashboard
  AND ydayAvg = leader.value?.yesterday_main_board_avg_auction
WHEN 第 4 格"接力情绪" cell 渲染（src/static/index.html:556-568）
THEN
  - 区域 D 第 4 格模板（line 556-568）**字符级保持不变**
  - 既有 v-if 条件 `v-if="ydayAvg && ydayAvg.median_change_pct != null"`（line 562）保持不变
  - dashboard `<script setup>` 区无任何改动（loadData / leader.value 解析路径不变）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-4.1 | `src/static/index.html` line 556-568（第 4 格 cell 模板）字符级保持不变 |
| BR-4.2 | dashboard 不引入新 ref / 不改 fetch 列表 / 不改 computed |
| BR-4.3 | dashboard 现状"sub 行 v-if 隐藏 + 主值显示 —"行为是**真源**；本 Story 邮件端向其对齐（不反向）|

---

### AC5: 不引入回归（DoD）

**Scenario**
```gherkin
GIVEN 现有邮件 + leader_feedback + scheduler 链路
WHEN Story 2.3 改造后的代码在以下输入下被调用
THEN 行为应与改造前完全一致：
  - SMTP_USER 或 SMTP_PASSWORD 缺失 → 打印 "[邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送"，返回 False（line 48-50）
  - sentiment_data + leader 全 None → email 走 fallback；第 4 格 main4="—"，sub 行**不渲染**（AC2）
  - hits 列表为空 → email 渲染 "无命中标的" 占位（与 cell4 sub 行渲染解耦，cell4 行为按 AC2）
  - send_screener_report 公开签名（inspect.signature）与 email-sync-1.1 / decision-consistency-2.1 baseline 完全相同
  - dashboard 模板 HTML（556-568 / 505-509 / 595-596 / 657-666）字符级未改
  - latest_advice.json 写入链路（decision-consistency-2.1）不受影响（_calc_daily_advice / write_advice_snapshot 不读 yesterday_main_board_avg_auction.{4 fields}）
  - latest_review.json / review_history.json watch_pool 链路（watch-pool-snapshot-2.2）不受影响（不读 yesterday_main_board_avg_auction）
  - tests/notify/test_email_decision_alignment.py + test_decision_consistency.py + test_email_fallback_industry_concept.py 既有 99 用例**全部 PASS**（无回归）
  - tests/test_review_watch_pool_snapshot.py 既有 34 用例**全部 PASS**
```

**Business Rules**
| ID | Rule |
|---|---|
| BR-5.1 | 不重命名 send_screener_report 公开签名 |
| BR-5.2 | 不重构 `_build_html` / `_calc_daily_advice` / `write_advice_snapshot` / `_load_advice_from_disk` 内部 |
| BR-5.3 | 不引入新依赖（不加 Redis / pydantic 等）|
| BR-5.4 | dashboard 不改 HTML/CSS（不改 `<script setup>` 区也不改模板）|
| BR-5.5 | `data/latest_leader.json` / `latest_sentiment.json` / `latest_advice.json` 等既有 latest_*.json 文件契约不变 |
| BR-5.6 | `compute_yesterday_main_board_auction` 函数体不变（BR-1.1 重申）|
| BR-5.7 | 项目用例总数 = 133（既有）+ 本 Story 新增用例数 — 全部 PASS（参照 watch-pool-snapshot-2.2 L-3 防漂移做法）|

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| 任一边缘分支行为差异 | — | — | QA 标记为 BLOCKING，回退至 SM revise |

---

## Tasks / Subtasks

> **说明**：测试用例的具体 spec 由 QA 在 *test-design 阶段产出（test_design_level: standard），Dev 在编码后回填本节"测试"子任务。

### Infrastructure Tasks (Shared)

- [ ] **T0: 渲染条件取舍（Architect 在 *review 阶段决定）** `[AC2]`
  - [ ] BR-2.5 给 Architect 确认：邮件 sub 行渲染条件用 `_is_num(median_change_pct)` 单字段判定（与 dashboard `v-if="median_change_pct != null"` 等价）— 是否需要扩展为"4 字段任一 None"双层兜底？
  - [ ] 决策记录到 Dev Log
  - 推荐方向：**保持单字段判定**（KISS + 与 dashboard 严格对齐）；4 字段在 BR-1.3 已保证 `sample_count > 0` 时永远有值，理论上不会出现"median 有值但 high5 为 None"的不一致

### Feature Implementation Tasks

- [ ] **T1: AC1 — `compute_yesterday_main_board_auction` 4 字段单元测试锁定** `[AC1]`

  **Test Specs** (white-box scenarios from test-design — 17 用例落 `tests/engine/test_leader_feedback_relay.py`):

  | Scenario | Input | Expected | Level |
  |----------|-------|----------|-------|
  | UNIT-001 dict 10 字段完整 | valid limit_up_hist + spot_df | dict.keys = {date, sample_count, avg, median, high5, flat2, low5, pos, neg, ld} | unit |
  | UNIT-002 类型断言 | sample_count > 0 | median float, 3 counts int | unit |
  | UNIT-003 round 精度 | changes=[1.234, 1.236, 1.238] | median == 1.24 | unit |
  | UNIT-004 签名冻结 | inspect.signature | `(limit_up_history, spot_df) -> Optional[dict]` | unit |
  | UNIT-005 边界 chg==5.0 | 1 sample chg=5.0 | high5_count == 0 | unit |
  | UNIT-006 边界 chg==-5.0 | 1 sample chg=-5.0 | low5_count == 0 | unit |
  | UNIT-007 just-beyond 5.01 | 1 sample chg=5.01 | high5_count == 1 | unit |
  | UNIT-008 just-beyond -5.01 | 1 sample chg=-5.01 | low5_count == 1 | unit |
  | UNIT-009 闭区间 ±2,0 | chg∈{-2.0,0.0,2.0} | flat2_count == 3 | unit |
  | UNIT-010 区间外 ±2.01 | chg∈{-2.01, 2.01} | flat2_count == 0 | unit |
  | UNIT-011 早退 hist 空 | limit_up_history={} | None | unit |
  | UNIT-012 早退 past 空 | only today key | None | unit |
  | UNIT-013 早退 df 空 | yesterday df empty | None | unit |
  | UNIT-014 早退 codes 空 | only 创业板/北交所 codes | None | unit |
  | UNIT-015 早退 spot+sina 空 | spot empty + sina raise | None | unit |
  | UNIT-016 早退 changes 空 | spot pre_close=0 全部 | None | unit |
  | BLIND-BOUNDARY-003 | sample_count==0 场景 | None (line 492) | unit |

  - [ ] 实现 17 用例 (按 skeleton TODO 逐一替换 `raise NotImplementedError`)
  - [ ] 验证 boundary 严格语义（5.0 / -5.0 不计 high5/low5；±2 / 0 计 flat2）
  - [ ] 验证 6 类早退路径（BR-1.4 — 严于 Story 起草时列的 5 类）
  - [ ] 不修改 `src/engine/leader_feedback.py`（BR-1.1 函数体冻结）

- [ ] **T2: AC2 — 邮件 sub 行 v-if 对齐** `[AC2]`

  **Test Specs** (white-box scenarios from test-design — 7 用例落 `tests/notify/test_relay_sentiment_render.py`):

  | Scenario | Input | Expected | Level |
  |----------|-------|----------|-------|
  | UNIT-017 sub 缺失不渲染 | y_avg={} | HTML 不含 中位数/高开>5%/平开±2%/低开<-5% | unit |
  | UNIT-018 sub 完整渲染 | y_avg=10字段 dict (median=0.5, high5=1, flat2=3, low5=0) | HTML 含 4 子串 | unit |
  | UNIT-019 median None 防御 | y_avg={sample_count:5, median_change_pct:None, ...} | sub `<div>` 整段不渲染 | unit |
  | UNIT-020 main4 保留 | y_avg={} | HTML 含 `接力情绪` + `<div ...>—</div>` 主值 | unit |
  | UNIT-021 title4 保留 | y_avg={} | HTML 含 `昨日涨停 — 只` | unit |
  | UNIT-022 label 无条件 | 任意 y_avg | HTML 始终含 `<div style="font-size:11px;color:#888;">接力情绪</div>` | unit |
  | UNIT-023 旧 fallback 字符串消失（强 negative）| 任意 y_avg | HTML **永不**含 `中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—` | unit |

  **代码改动指引（来自 Architect Low Issue 1）**:
  ```python
  # line 469 后（在 sample 取值之后、line 470 if 之前）独立计算
  render_sub = _is_num(y_avg.get("median_change_pct"))
  sub4_str = ""  # 默认空，仅 render_sub 才填充

  if _is_num(sample) and sample > 0:
      title4 = ...
      main4  = ...
      main4_color = ...
      if render_sub:
          median_str = f"{'+' if median_chg >= 0 else ''}{median_chg}%"
          sub4_str = (
              f"中位数 {median_str} · "
              f"高开>5%:{high5 if _is_num(high5) else '—'} · "
              f"平开±2%:{flat2 if _is_num(flat2) else '—'} · "
              f"低开<-5%:{low5 if _is_num(low5) else '—'}"
          )
  else:
      title4 = "昨日涨停 — 只"
      main4 = "—"
      main4_color = "#6b7280"
      # ⭐ DELETED: line 491 sub4_str = "中位数 — ..." (BR-2.3)

  # cell4_html — line 499 改为条件输出
  sub_div = (f'<div style="font-size:10px;color:#999;margin-top:1px;">{sub4_str}</div>'
             if render_sub else '')
  cell4_html = (
      '<td ...>'
      '<div style="font-size:11px;color:#888;">接力情绪</div>'
      f'<div ...>{title4}</div>'
      f'<div ...>{main4}</div>'
      f'{sub_div}'  # ⭐ 条件插入
      '</td>'
  )
  ```

  - [ ] 修改 `src/notify/email_sender.py` 第 4 格 cell（line 460-501）按上述形式
  - [ ] 删除 line 491 旧硬编码 fallback（BR-2.3）
  - [ ] 实现 7 个 AC2 unit 测试
  - [ ] main4 / title4 / main4_color fallback 行为保留（BR-2.4）

- [ ] **T3: AC3 — scheduler 数据流验收** `[AC3]`

  **Test Specs** (3 用例落 `tests/notify/test_relay_sentiment_render.py`):

  | Scenario | Input | Expected | Level |
  |----------|-------|----------|-------|
  | UNIT-024 line 395 字符级 | Read scheduler.py | 含 `y_main_board_stats = compute_yesterday_main_board_auction(limit_up_hist, spot_df)` 子串 | unit |
  | UNIT-025 line 447 字符级 | Read scheduler.py | 含 `"yesterday_main_board_avg_auction": y_main_board_stats,` 子串（含尾逗号）| unit |
  | INT-001 写盘集成 | mock compute → None；call scheduler write | latest_leader.json 含 `"yesterday_main_board_avg_auction": null` | integration |

  - [ ] 实现 3 个测试（不修改 `src/scheduler.py`）

- [ ] **T4: AC4 — dashboard 模板字符级冻结** `[AC4]`

  **Test Specs** (2 用例落 `tests/notify/test_relay_sentiment_render.py`):

  | Scenario | Input | Expected | Level |
  |----------|-------|----------|-------|
  | UNIT-026 v-if 子串 | Read index.html | 含 `<div class="mb-sub" v-if="ydayAvg && ydayAvg.median_change_pct != null">` | unit |
  | UNIT-027 SHA256 双锁 | index.html 行 556-568 | SHA256 == fixture baseline (`tests/notify/fixtures/index_template_baseline.json` key `lines_556_568_sha256`，首跑写入、后续比对) | unit |

  - [ ] 实现 2 个测试 + 扩展 `index_template_baseline.json` 加 `lines_556_568_sha256` key（不修改 `src/static/index.html`）

### Integration & Verification Tasks

- [ ] **T5: 端到端邮件 + leader 一致性集成测试** `[AC1, AC2, AC3]`（DoD #1）
  - [ ] mock `compute_yesterday_main_board_auction` 返回 None → 调 send_screener_report → 邮件 HTML 中 sub 行**不存在**
  - [ ] mock 返回完整 10 字段 dict → 调 send_screener_report → 邮件 HTML 中 sub 行渲染完整 4 字段
  - [ ] 跨 Story 集成：与 decision-consistency-2.1（_calc_daily_advice）+ watch-pool-snapshot-2.2（/api/review）链路并存验证

- [ ] **T6: 回归保护测试** `[AC5]`（DoD #2）
  - [ ] inspect.signature(send_screener_report) 与 email-sync-1.1 baseline 字符级一致（INT-005 沿用）
  - [ ] dashboard 模板 5 区域字符级未变（556-568 第 4 格 + 既有 505-509 / 595-596 / 657-666）
  - [ ] 既有 99 邮件用例 + 34 review 用例全部 PASS（pytest tests/ -W error 严格模式）
  - [ ] 项目用例总数 = 133（既有）+ 本 Story 新增（具体数由 QA test-design 给）— 通过 `pytest --collect-only -q | tail -1` 防漂移断言

- [ ] **T7: 边缘场景测试** `[AC1, AC2]`
  - [ ] AC1 边界：chg ∈ {5.0, -5.0, -2.0, 0.0, 2.0} 各组样本验证 high5/flat2/low5 boundary
  - [ ] AC2 部分缺失：median 有值但 sample_count 为 0 → sub 行渲染（按 BR-2.5 单字段判定 — 此场景虽不应发生但需 defensive）
  - [ ] AC2 蓝点：leader 对象不是 dict（None / [] / 0 / 字符串）→ line 371 `or {}` 兜底 → sub 行不渲染、不抛错
  - [ ] AC1 蓝点：spot_df 全部 pre_close=0 → changes 为空 → 函数返回 None（既有行为）

- [ ] **T8: 最终验收** `[ALL ACs]`
  - [ ] 全测试 PASS：133（既有）+ 本 Story 新增用例数（pytest tests/ -W error 严格模式）
  - [ ] Dev Log 完整记录改动 + T0 渲染条件决策
  - [ ] Status → Review

### AC Coverage Matrix

| Task | AC1 | AC2 | AC3 | AC4 | AC5 |
|------|:---:|:---:|:---:|:---:|:---:|
| T0: 渲染条件决策 |   | ✓ |   |   |   |
| T1: leader_feedback 4 字段测试锁定 | ✓ |   |   |   |   |
| T2: 邮件 sub 行 v-if 对齐 |   | ✓ |   |   |   |
| T3: scheduler 数据流验收 |   |   | ✓ |   |   |
| T4: dashboard 字符级冻结 |   |   |   | ✓ |   |
| T5: 端到端一致性 | ✓ | ✓ | ✓ |   |   |
| T6: 回归保护 |   |   |   |   | ✓ |
| T7: 边缘场景 | ✓ | ✓ |   |   |   |
| T8: 最终验收 | ✓ | ✓ | ✓ | ✓ | ✓ |

---

## Dev Notes

### Technical Constraints

| 类别 | 约束 | 来源 |
|---|---|---|
| 数据源算法不动 | `compute_yesterday_main_board_auction` 函数体（`src/engine/leader_feedback.py:393-509`）字符级保持不变；4 字段在 commit 37af368 已实现，本 Story 仅做"测试锁定"  | scope #56 → SM 校核 |
| 真源对齐方向 | dashboard `v-if` 是真源；邮件向其对齐（不反向）— 与"看板锁定"原则（decision-consistency-2.1 BR-2.5）一致 | scope #61 |
| 文件结构 | 仅改 email_sender.py 第 4 格 cell（line 460-501）+ 新增测试；不改 dashboard / scheduler / leader_feedback / api/app.py | scope #55-61 |
| 公开签名 | send_screener_report 公开签名严格不变（沿用 email-sync-1.1 / decision-consistency-2.1 baseline） | email-sync-1.1 BR-9.1 |
| 不引入新依赖 | 继续用既有 `_is_num` helper（line 189-190） + 现有渲染 f-string；不加 Jinja2 / template engine | scope #106 |
| 跨 Story 解耦 | 不改 latest_advice.json / latest_review.json 链路；不改 dashboard JS computed | decision-consistency-2.1 / watch-pool-snapshot-2.2 |

### Accumulated Context (From Previous Stories)

| Resource | Source Story | 状态 | Action |
|---|---|---|---|
| `email_sender.send_screener_report` 签名 | email-sync-1.1 (Done) + decision-consistency-2.1 (Done) | INT-005 / INT-007 inspect.signature baseline | REUSE — 字符级冻结 |
| `email_sender._is_num` helper（line 189-190） | email-sync-1.1 (Done) | 严格 isinstance(int, float) 排除 bool | REUSE — AC2 渲染条件直接复用 |
| `email_sender._calc_daily_advice` / `write_advice_snapshot` / `_load_advice_from_disk` | decision-consistency-2.1 (Done) | 决策快照真源链路 | REUSE — 与本 Story 解耦（不读 yesterday_main_board_avg_auction.{4 fields}）|
| `compute_yesterday_main_board_auction` 4 字段实现 | commit 37af368 "邮件重构" | 已落地 line 499-509 | REUSE — 本 Story 仅加测试锁定 |
| `src/scheduler.py:395, 447` 写 latest_leader.json | 既有 | 已 wire `yesterday_main_board_avg_auction` 字段 | REUSE — 字符级冻结 |
| `src/static/index.html:562` v-if 隐藏 sub 行 | 既有 | dashboard 真源行为 | REUSE — 邮件向其对齐 |
| `tests/notify/test_email_decision_alignment.py` (line 52, 67, 613) | 既有 99 用例 | 含 yesterday_main_board_avg_auction 完整 fixture | REUSE — 本 Story 不动 |
| `tests/notify/test_email_fallback_industry_concept.py:37` | 既有 5 用例 | 含 `yesterday_main_board_avg_auction: {}` 空 fixture | REUSE — 本 Story 测试可参考该 fixture 模式 |
| `tests/test_review_watch_pool_snapshot.py` | watch-pool-snapshot-2.2 (Done) 34 用例 | 项目用例基线 133 | REUSE — pytest --collect-only baseline |
| Database Tables | — | N/A — 本 Story 无数据库变更 | — |
| Shared Models | — | N/A — y_avg 为非 typed dict | — |

### Database Design

N/A — 不涉及数据库变更。

### Data Synchronization Requirements

- [ ] 本 Story **不**引入新文件；仅修改 email_sender.py 第 4 格 cell 渲染分支
- [ ] `data/latest_leader.json` 字段契约不变（既有 `yesterday_main_board_avg_auction` 仍可为 None / 10 字段 dict）

### Data Models

**`yesterday_main_board_avg_auction` Schema**（既有，本 Story 测试锁定）：

```python
{
    "date": str,                      # "%Y%m%d"
    "sample_count": int,              # ≥ 1（否则函数返回 None）
    "avg_change_pct": float,          # round(2) 平均竞价涨幅
    "median_change_pct": float,       # round(2) 中位数（4 新字段之一）
    "high5_count": int,               # chg > 5 严格大于（4 新字段之一）
    "flat2_count": int,               # -2 <= chg <= 2 闭区间（4 新字段之一）
    "low5_count": int,                # chg < -5 严格小于（4 新字段之一）
    "positive_count": int,            # chg > 0
    "negative_count": int,            # chg < 0
    "limit_down_count": int,          # chg <= -9.5
} | None  # 函数早退时返回 None
```

**邮件第 4 格 cell HTML 结构（AC2 改造后）**：

```html
<!-- 当 _is_num(y_avg.get("median_change_pct")) == True：sub 行渲染 -->
<td style="background:#f8f9fa;border:1px solid #e5e7eb;padding:6px 10px;border-radius:6px;width:33%;vertical-align:top;">
  <div style="font-size:11px;color:#888;">接力情绪</div>
  <div style="font-size:10px;color:#6b7280;margin-top:1px;">{title4}</div>
  <div style="font-size:16px;font-weight:700;color:{main4_color};margin-top:2px;">{main4}</div>
  <div style="font-size:10px;color:#999;margin-top:1px;">{sub4_str}</div>  <!-- ⭐ AC2 关键：仅当 render_sub==True 时输出该 div -->
</td>

<!-- 当 _is_num(y_avg.get("median_change_pct")) == False：sub 行整段消失 -->
<td style="background:#f8f9fa;border:1px solid #e5e7eb;padding:6px 10px;border-radius:6px;width:33%;vertical-align:top;">
  <div style="font-size:11px;color:#888;">接力情绪</div>
  <div style="font-size:10px;color:#6b7280;margin-top:1px;">{title4}</div>  <!-- "昨日涨停 — 只" -->
  <div style="font-size:16px;font-weight:700;color:{main4_color};margin-top:2px;">{main4}</div>  <!-- "—" -->
</td>
```

### File Locations

| 文件 | 操作 | 涉及行号（起草时） | 关联 AC |
|---|---|---|---|
| `src/notify/email_sender.py` | **修改** | 460-501（第 4 格 cell 渲染：删除 487-491 else 硬编码 fallback；引入 render_sub 条件；line 499 sub `<div>` 改为条件输出） | AC2 |
| `src/engine/leader_feedback.py` | **不动** | 393-509 | AC1 / AC5 |
| `src/scheduler.py` | **不动** | 340-455 | AC3 / AC5 |
| `src/static/index.html` | **不动** | 556-568 | AC4 / AC5 |
| `src/api/app.py` | **不动** | — | AC5 |
| `tests/engine/test_leader_feedback_relay.py`（或 QA 决定路径） | **新建** | — | AC1（4 字段 boundary + 早退场景）|
| `tests/notify/test_relay_sentiment_render.py`（或 QA 决定路径） | **新建** | — | AC2 / AC5（sub 行渲染对齐 + 集成）|

### Deliverable Bindings

```yaml
deliverable_bindings:
  - deliverable: "src/notify/email_sender.py::send_screener_report (modified — cell4 sub 行 v-if 对齐)"
    consumer: "src/scheduler.py::run_screener_update (既有 line 645-655 调用 send_screener_report)"
    binding_type: import_usage
    verify: "src/scheduler.py 含 'from src.notify.email_sender import send_screener_report' 字符级既有 import"

  - deliverable: "compute_yesterday_main_board_auction 4 字段（既有）"
    consumer: "src/notify/email_sender.py::send_screener_report (line 465-468 + 481-486)"
    binding_type: import_usage
    verify: "src/notify/email_sender.py 含 'median_change_pct' / 'high5_count' / 'flat2_count' / 'low5_count' 4 个字符串引用（既有 line 465-468）"

  - deliverable: "tests/engine/test_leader_feedback_relay.py（or path chosen by QA）"
    consumer: "pytest discovery (project test runner)"
    binding_type: import_usage
    verify: "pytest 收集到该测试文件中的测试用例（具体用例数由 QA test-design 给出）"

  - deliverable: "tests/notify/test_relay_sentiment_render.py（or path chosen by QA）"
    consumer: "pytest discovery (project test runner)"
    binding_type: import_usage
    verify: "pytest 收集到该测试文件中的测试用例（具体用例数由 QA test-design 给出）"
```

### Testing Requirements

- **测试设计层级**：`standard`（沿用 iteration-2 brownfield 路径强制 standard）
- **前置流程**：QA *test-design 在开发前出测试设计文档，Dev 据此实现 T5/T7 中的具体集成与边缘场景用例
- **覆盖重点**：
  1. `compute_yesterday_main_board_auction` 4 字段 boundary 严格语义（AC1 BR-1.2 — `chg > 5` / `chg < -5` / `-2 <= chg <= 2`）
  2. 函数返回 None 的 5 类早退场景（AC1 BR-1.4）
  3. 邮件第 4 格 sub 行渲染条件（AC2 BR-2.1）：`_is_num(median_change_pct)` True / False 两态
  4. 邮件 cell4 主体保留行为（AC2 BR-2.4）：sample 缺失时 main4="—" / title4="昨日涨停 — 只"
  5. send_screener_report 签名 + dashboard 模板字符级 diff 回归保护（AC5）
  6. 跨 Story 集成不破：decision-consistency-2.1（latest_advice.json）+ watch-pool-snapshot-2.2（/api/review）链路并存
  7. 项目用例总数防漂移（AC5 BR-5.7：133 + 本 Story 新增）

### Out of Scope (本 Story 明确不做)

- ❌ 调查 `compute_yesterday_main_board_auction` 在产线为何返回 None（数据可用性根因；如需追根另立 Story）
- ❌ 修改 dashboard `v-if` 条件（dashboard 是真源，邮件向其对齐，不反向）
- ❌ 重构 `_build_html` / `_calc_daily_advice` / `write_advice_snapshot`（与 decision-consistency-2.1 解耦）
- ❌ 改 `latest_leader.json` schema（字段保持完整 10 字段或整体为 None 二态）
- ❌ refresh-screener email 重发行为（属 Story 2.5 范围，与本 Story 解耦）

---

## Change Log

| Date | Agent | Status Transition | Details/Link |
|------|-------|-------------------|--------------|
| 2026-05-08 | SM (Phil) | Created → AwaitingArchReview | Brownfield 单 Story 起草；偏离标准流程 8 条沿用 email-sync-1.1 / decision-consistency-2.1 / watch-pool-snapshot-2.2 路径（无 PRD 分片 / 无 architecture 目录 / scope 文件作虚拟 epic / 跳过 Epic YAML / 跳过 架构上下文 / 跳过 累积校验 / 跳过 Decision 8A / 强制 test_design_level=standard）；scope 文件 [docs/prd/iteration-2-scope.md#story-2-3](../prd/iteration-2-scope.md) 作为真源。**重要事实校核**：scope #50-52 声称 4 字段缺失，实测代码 `src/engine/leader_feedback.py:499-509`（commit 37af368）4 字段已存在；用户 2026-05-08 决策 C 选项"按原 scope 文字执行" — AC1 落地为既有实现 + 测试锁定，AC2 为净新行为变更（邮件 sub 行 v-if 对齐 dashboard）。HANDOFF 至 architect *review |
| 2026-05-08 | Architect (Wright) | AwaitingArchReview → AwaitingTestDesign | Score 9.5/10；0 Critical / 0 Major / 0 Medium / 1 Low（T2 sub4_str 构造点需独立于 sample 分支，给出明确重构形式）；T0 渲染条件决策：保持 BR-2.5 单字段 `_is_num(median_change_pct)` 判定（与 dashboard line 562 v-if 字符级等价；KISS）；事实证据链 13 项校核全 PASS（leader_feedback 393-510 / email_sender 189-190, 371, 460-501 / index.html 556-568 / scheduler 395, 447 行号字符级一致）。HANDOFF 至 qa *test-design |
| 2026-05-08 | QA (Turing) | AwaitingTestDesign → TestDesignComplete → Approved | Test design 落地：43 scenarios（Unit 33 / Integration 10 / E2E 0；P0:24 / P1:13 / P2:6；Blind-Spot:7）；Document [docs/qa/assessments/relay-sentiment-2.3-test-design-20260508.md](../qa/assessments/relay-sentiment-2.3-test-design-20260508.md)；Skeleton `tests/engine/test_leader_feedback_relay.py` (17) + `tests/notify/test_relay_sentiment_render.py` (26) + `tests/engine/__init__.py`；pytest --collect-only baseline 232 = 189 既有 + 43 新 skeleton；两阶段状态转换 AwaitingTestDesign → TestDesignComplete（test_done + doc_created）→ Approved（auto-transition）。覆盖 R1～R6 风险矩阵；BR-2.3 旧 fallback 字符串显式 negative assertion；BR-5.7 anti-drift 锁 dev_baseline+43。HANDOFF 至 dev *develop-story relay-sentiment-2.3 |

---

## Dev Agent Record

### Agent Model Used
- **Agent**: TBD (Dev)
- **Date**: TBD
- **Mode**: develop-story (TDD, plan mode)

### Implementation Summary

TBD — 由 Dev 在实现完成后回填。

### Database Changes (Structured)
```yaml
{}  # N/A — 本 Story 不涉及数据库变更
```

### API Endpoints Created (Structured)
```yaml
{}  # N/A — 本 Story 不新增 endpoint，不修改既有 endpoint
```

### Shared Models Created (Structured)
```yaml
{}  # N/A — 本 Story 不涉及新增共享 typed model；y_avg 为非 typed dict
```

### File List

TBD — 由 Dev 在实现完成后回填。

### Dev Log Reference
- TBD — 由 Dev 在实现完成后回填（路径建议 `docs/dev/logs/relay-sentiment-2.3-dev-log.md`）

### Open Issues

TBD — 由 Dev 在实现完成后回填。

---

## Architect Review Results

### Review Date: 2026-05-08
### Reviewed By: Wright (Architect)
### Architecture Score: 9.5/10
### Review Round: 1

### Decision: AwaitingTestDesign（通过审核 → QA *test-design）

### T0 渲染条件决策（Architect *review 强制裁定）

- **决策**：保持 BR-2.5 单字段判定 — `render_sub = _is_num(y_avg.get("median_change_pct"))`
- **理由**：
  1. **真源对齐**：dashboard `src/static/index.html:562` 使用 `v-if="ydayAvg && ydayAvg.median_change_pct != null"`（单字段），邮件向 dashboard 对齐是本 Story 的核心目标（BR-4.3 / scope #61），不引入"4 字段任一 None"的不对称判定
  2. **数据契约保证**：实测代码 `src/engine/leader_feedback.py:465-510` 证明 4 字段在 `sample_count > 0` 时由同一函数同步产出（`high5_count` / `flat2_count` / `low5_count` 初始化为 0，循环内累加；`median_change_pct = round(median, 2)`），不会出现"median 有值但 high5 / flat2 / low5 为 None"的不一致状态 — BR-1.3 已锁定该不变式
  3. **KISS**：单字段判定 vs 4 字段任一 None 检查 → 前者代码 1 行、与 dashboard 行为字符级等价；后者 4 行、引入额外认知负担
- **结论**：T0 关闭，按 BR-2.5 实施，无需扩展为多字段兜底

### Issues

#### Critical Issues (0)
（无）

#### High Issues (0)
（无）

#### Medium Issues (0)
（无）

#### Low Issues (1)
- **T2 重构指引可更明确：sub4_str 构造需独立于 sample 分支**（`Tasks/Subtasks > T2`）：
  当前代码 `src/notify/email_sender.py:481-491` 中 `sub4_str` 仅在 `if _is_num(sample) and sample > 0:` 分支内构造（line 481-486），else 分支提供硬编码 fallback（line 491）。T2 第 4 子项要求"`sub4_str` 仅在 `render_sub == True` 时按 line 481-486 既有逻辑构造"，但未显式说明：当 `render_sub == True` 但 `sample` 为 0/缺失（T7 防御场景）时，sub4_str 必须仍可构造。
  - **Recommendation**：Dev 实施时建议结构为 —
    ```python
    render_sub = _is_num(y_avg.get("median_change_pct"))
    sub4_str = ""  # 默认空，仅 render_sub 才填充
    if render_sub:
        median_str = f"{'+' if median_chg >= 0 else ''}{median_chg}%"
        sub4_str = (
            f"中位数 {median_str} · "
            f"高开>5%:{high5 if _is_num(high5) else '—'} · "
            f"平开±2%:{flat2 if _is_num(flat2) else '—'} · "
            f"低开<-5%:{low5 if _is_num(low5) else '—'}"
        )
    # cell4_html 内：
    sub_div = f'<div style="font-size:10px;color:#999;margin-top:1px;">{sub4_str}</div>' if render_sub else ''
    ```
  - **风险等级**：Low（不影响 AC 通过；仅减少 Dev 误读 NameError 的概率）

### Recommendations

1. **AC1 既有实现已通过事实校核**：`src/engine/leader_feedback.py:393-510`（10 字段 + boundary 严格语义 `chg > 5` / `chg < -5` / `-2 <= chg <= 2`）与 BR-1.1～BR-1.6 字符级一致 → QA test-design 重点写 boundary 锁定测试（chg ∈ {5.0, -5.0, -2.0, 0.0, 2.0} 的 5 类 case + 5 类早退 case）即可
2. **AC2 真源/调用链已校核**：
   - `src/notify/email_sender.py:189-190` `_is_num` helper（排除 bool）✅
   - `src/notify/email_sender.py:371` `y_avg = ((leader or {}).get(...) or {})` 兜底 ✅
   - `src/notify/email_sender.py:481-491, 499` 是本 Story 唯一改动点 ✅
   - dashboard `src/static/index.html:556-568` v-if 真源 ✅
3. **AC3 / AC4 字符级冻结**：scheduler 与 dashboard 模板由 SHA256 / 子串断言 baseline 锁定（QA 决定具体形式）
4. **AC5 回归基线**：133 既有用例（99 email + 34 review）— pytest collect baseline 沿用 watch-pool-snapshot-2.2 / decision-consistency-2.1 防漂移做法
5. **测试设计层级**：保持 `standard`（已由 SM 设定，brownfield-enhancement 多 AC + 集成路径 — 与 watch-pool-snapshot-2.2 / decision-consistency-2.1 一致）

### Tech Stack & Architecture Sub-Scores

| 维度 | 分数 | 备注 |
|---|---|---|
| tech_stack_compliance | 1.0/1 | 无新依赖（沿用 stdlib + pandas + 既有 helper）|
| naming_convention_adherence | 1.0/1 | render_sub / sub4_str / _is_num 均沿用既有命名 |
| project_structure_alignment | 1.0/1 | 改动落点（email_sender.py / 新测试位置）符合既有 src/notify, tests/notify 结构 |
| api_design_consistency | 1.0/1 | send_screener_report 公开签名字符级冻结（INT-005 baseline）|
| data_model_accuracy | 1.0/1 | yesterday_main_board_avg_auction 10 字段 schema 与代码字符级一致 |
| architecture_pattern_compliance | 1.0/1 | dashboard 真源 + 邮件向其对齐 — 与 BR-4.3 / scope #61 一致 |
| complete_dependency_mapping | 1.0/1 | Accumulated Context 表 9 项 REUSE 全部已校核 |
| integration_feasibility | 1.0/1 | 无跨模块/服务影响；与 decision-consistency-2.1 / watch-pool-snapshot-2.2 字段级解耦 |
| accurate_documentation_references | 1.0/1 | Story 引用的全部行号（leader_feedback 393-510 / email_sender 189-190, 371, 460-501 / index.html 556-568 / scheduler 395, 447）均已字符级校核通过 |
| overall_implementation_feasibility | 0.5/1 | 整体可实施，唯 T2 sub4_str 构造点需配合 T7 防御场景（已在 Low Issue 中给出明确重构形式）|

### 数据源 / 行为校核（Brownfield 关键证据链）

| 校核项 | 期望（Story） | 实测（代码） | 结果 |
|---|---|---|---|
| `compute_yesterday_main_board_auction` 返回 dict 含 10 字段 | BR-1.1 | line 499-510 字符级一致 | ✅ |
| boundary `chg > 5` 严格大于 | BR-1.2 | line 485 `if chg > 5:` | ✅ |
| boundary `chg < -5` 严格小于 | BR-1.2 | line 487 `elif chg < -5:` | ✅ |
| boundary `-2 <= chg <= 2` 闭区间 | BR-1.2 | line 489 `if -2 <= chg <= 2:` | ✅ |
| median_change_pct = round(median, 2) | BR-1.3 | line 503 | ✅ |
| 早退 5 类 | BR-1.4 | line 403, 411, 416, 426, 434, 460, 492 | ✅ |
| 公开签名 `(limit_up_history, spot_df) -> Optional[dict]` | BR-1.5 | line 393-396 | ✅ |
| scheduler 写 `latest_leader.json["yesterday_main_board_avg_auction"]` | BR-1.6 / BR-3.1 | line 447 | ✅ |
| `_is_num` helper 排除 bool | BR-2.1 | line 189-190 | ✅ |
| `y_avg = ((leader or {}).get(...) or {})` 兜底 | BR-2.1 / Error Handling | line 371 | ✅ |
| email cell4 line 487-491 else 硬编码 fallback 待删 | BR-2.3 | line 487-491 现存 | ✅ 待 Dev 删 |
| email cell4 line 499 sub `<div>` 待改为条件输出 | BR-2.2 | line 499 现状无条件 | ✅ 待 Dev 改 |
| dashboard `v-if="ydayAvg && ydayAvg.median_change_pct != null"` | BR-2.5 / BR-4.1 | line 562 字符级一致 | ✅ |
| dashboard cell 模板 line 556-568 字符级 | BR-4.1 | 实测一致 | ✅ |

---

## QA Test Design Metadata

- **Level:** Standard
- **Status:** Complete
- **Test Design Status:** Complete
- **Document:** [docs/qa/assessments/relay-sentiment-2.3-test-design-20260508.md](../qa/assessments/relay-sentiment-2.3-test-design-20260508.md)
- **Risk Profile:** N/A — Story 已通过 Architect *review (Score 9.5/10, 0 Critical)，无独立 risk-profile 文档；风险矩阵已写入 test-design 文档 "Risk Coverage" 节
- **Total Scenarios:** 43 (Unit 33 / Integration 10 / E2E 0)
- **Priority Distribution:** P0: 24, P1: 13, P2: 6
- **Blind-Spot Scenarios:** 7 (BOUNDARY: 3, ERROR: 3, FLOW: 1)
- **Test Skeleton Files:**
  - `tests/engine/test_leader_feedback_relay.py` (17 用例 — AC1)
  - `tests/notify/test_relay_sentiment_render.py` (26 用例 — AC2/AC3/AC4/AC5 + 集成 + blind-spot)
  - `tests/engine/__init__.py` (新建空 init)
- **Pytest Baseline (after skeleton):** 232 = 189 既有 + 43 新 skeleton（Dev 起手时观测值）
- **BR-5.7 Anti-drift Check:** Dev 完成后 `pytest --collect-only -q` 总用例数 = `dev_baseline + 43`，且不得有 `NotImplementedError` 残留

---

## QA Results

TBD — 由 QA 在 *review 阶段回填。

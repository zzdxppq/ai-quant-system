# Story decision-consistency-2.1: 9:27 决策快照单一真源

## Story

```yaml
Story:
  id: decision-consistency-2.1
  title: 9:27 决策快照单一真源（看板 + 邮件读同一份 latest_advice.json）
  epic: iteration-2 brownfield (virtual epic — 真源为 docs/prd/iteration-2-scope.md)
  status: Done
  mode: plan
  repository: monolith
  priority: P0
  estimated_complexity: standard
  test_design_level: standard
  story_type: brownfield-enhancement
```

**As a** A 股短线交易者（604491810@qq.com 邮件唯一收件人 + 浏览器看板唯一使用者），
**I want** 9:27 选股完成时一次性算出当日决策（bucket / 仓位 / reason / 4 维状态），冻结写入 `data/latest_advice.json`；之后**邮件**与**看板**始终从该文件读，**不再实时算**，
**so that** 我在 9:27 邮件看到的"0 层 不开仓"与 11:30 看板上看到的建议**永远一致**，避免在邮件与浏览器之间出现"同一只股票相反仓位建议"的实盘困惑（用户 2026-05-08 实盘反馈）。

---

## 背景与问题

### 当前行为分歧

`src/notify/email_sender.py:_calc_daily_advice`（72-172）与 `src/static/index.html:dailyAdvice`（1196-1268）虽然在 email-sync-1.1（commit `eb4e883`）后**算法逻辑已对齐**，但**两路调用时机不同**：

| 入口 | 调用时机 | 输入数据 |
|---|---|---|
| 邮件 `_calc_daily_advice` | 9:27 选股时一次（来自当时的 `sentiment_data` / `leader`） | 9:27 时刻的市场快照（`limit_down` / `weighted_auction_gain` / `main_board_leaders`） |
| 看板 `dailyAdvice` computed | 浏览器每次 `loadData()`（onMount + 每 60s）（`index.html:1622`） | 调用时刻的 `/api/sentiment` + `/api/leader`（盘中持续刷新到 11:30+） |

**结果**：盘中看板会随 `latest_sentiment.json` / `latest_leader.json` 持续更新（`run_cycle_update` 在后台 9:27+ 之后周期性写新值），看板的 `dailyAdvice` 也会跟着重算 → 与邮件中"9:27 那一刻"的判定不一致。

### 用户反馈（2026-05-08）

- **9:27 邮件**：`{bucket: stop, position: "0 层（空仓避险）", reason: "竞价跌停 7 只 + 加权竞价 -0.3% 偏弱"}`
- **11:30 看板**：`{cls: advice-warn, suggestedPosition: "1.5层（小仓试错）", reason: "竞价跌停 4 只"}`（盘中跌停数自然衰减）

同一只股票，邮件 0 层、看板 1.5 层 → 用户**不知道按哪个执行**。

### 真源约束（用户已选定）

> "看板锁定 9:27 快照"为方向（不改邮件入口，让看板回到决策时刻）。

---

## 改动范围（来自 scope）

[Source: docs/prd/iteration-2-scope.md#story-2-1]

1. **9:27 选股完成后**写 `data/latest_advice.json`（含 `dailyAdvice` 完整字段：bucket / text / suggested_position / reason / bad_count / 4 维状态）
2. **看板 `dailyAdvice` computed → 改为读 `/api/daily-advice`**（即 `latest_advice.json`），不再实时算
3. **邮件 `_calc_daily_advice` → 同样从 `latest_advice.json` 读**（保证完全一致）
4. **用户手动 `*refresh-screener` 时也应一起刷新 `latest_advice.json` + 重发邮件（统一）**

> ⚠️ 与 Story 2.5 的关系：Story 2.5 后续会改 `refresh-screener` 默认 `skip_email=True` 不重发邮件。**Story 2.1 不引入 skip_email 参数**，refresh-screener 当前行为（每次都重发邮件）保持不变；Story 2.5 在自己的范围内独立改造。Story 2.1 只确保 refresh-screener 路径**也会刷新 `latest_advice.json`**（这部分 run_screener_update 已默认成立，本 Story 无需特殊处理）。

---

## Acceptance Criteria

### AC1: 9:27 选股完成后写 `data/latest_advice.json`

**Scenario**
```gherkin
GIVEN run_screener_update() 在 9:27 cron 或用户手动调用下执行
  AND latest_sentiment.json + latest_leader.json 已就绪（或部分就绪）
WHEN 选股结果与交叉验证写完（current scheduler.py:536-574 区间）
THEN
  - 在 send_screener_report 之前，scheduler 必须计算当日 advice 并写入 `DATA_DIR / "latest_advice.json"`
  - 该文件必须包含完整字段：generated_at / bucket / text / suggested_position / suggested_position_short / reason / bad_count / dimensions / inputs
  - 文件以 UTF-8 + indent=2 + ensure_ascii=False 写入（与既有 latest_*.json 一致）
  - 计算逻辑与现有 _calc_daily_advice 完全等价（不引入新算法）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-1.1 | latest_advice.json 是**幂等文件**：同一组输入永远算出同一份输出（移植 _calc_daily_advice 不引入随机/时间戳影响逻辑）|
| BR-1.2 | `generated_at` 字段记录写入时刻（now_cn().strftime("%Y-%m-%d %H:%M:%S")），用于看板 / 邮件展示与排错；不参与决策逻辑 |
| BR-1.3 | sentiment / leader 任一缺失或字段不全时，fallback 到与现状一致的"数据加载中"分支输出，并将 `bucket=go / text="— 数据加载中 —"` 写入文件（不抛错、不跳过写）|
| BR-1.4 | 写入前必须保证 `_calc_daily_advice` 完成；scheduler 必须等待该步同步完成（不能放进 `_background_tasks` 异步线程）|
| BR-1.5 | latest_advice.json 文件**不**纳入 git（与其他 `data/latest_*.json` 一致，由 .gitignore 已覆盖；本 Story 不需要改 .gitignore） |

**Data Validation**
| Field | Type | Required | Rules | Error Message |
|---|---|---|---|---|
| generated_at | str | ✅ | now_cn() 格式 "%Y-%m-%d %H:%M:%S" | — |
| bucket | enum["stop","warn","go"] | ✅ | 与 _calc_daily_advice 输出一致 | — |
| text | str | ✅ | 含 emoji + 中文（与 dashboard 一致）| — |
| suggested_position | str | ✅ | "0 层（空仓避险）"/"1.5 层（小仓试错）"/"3 层（标准仓位）"/"4 层（连续情绪良好）"/"—" | — |
| suggested_position_short | str | ✅ | "0层"/"1.5层"/"3层"/"4层"/"—" | — |
| reason | str | ✅ | 可为空字符串（bad_count==0 且非升仓时） | — |
| bad_count | int | ✅ | 0 / 1 / 2 / 3 / 4 | — |
| dimensions | dict | ✅ | `{ld_bad: bool, drop_bad: bool, w_bad: bool, lb_bad: bool}` | — |
| inputs | dict | ✅ | 原始输入快照（参见 BR-1.6）| — |

**BR-1.6** — `inputs` 字段必须保留以下原始值（用于排错与下游显示）：
```yaml
inputs:
  limit_down: int | None
  drop_over_9pct: int | None
  weighted_auction_gain: float | None
  prev_day_limit_down: int | None
  prev_day_weighted_auction_gain: float | None
  main_board_leaders_summary:    # 仅简化结构，不复制全部 leader 详情
    - {leader_name: str, signal: str, auction_change_pct: float | None}
```

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| sentiment_data 为 None | — | （静默） | 写入"数据加载中"分支 advice |
| leader 为 None | — | （静默） | 写入"数据加载中"分支 advice（与 _calc_daily_advice 现状一致） |
| 写入磁盘异常（IOError） | — | print "[决策快照] 写入失败: {e}" | 不抛错给上层；scheduler 继续后续步骤；email 走 fallback 分支（见 AC3 BR-3.3）|

---

### AC2: 看板 `dailyAdvice` 改读 `/api/daily-advice`，不再实时算

**Scenario**
```gherkin
GIVEN 用户访问 / 浏览器加载 dashboard
  AND latest_advice.json 已存在（9:27 后任意时间）
WHEN dashboard loadData() 触发（onMount + 每 60s）
THEN
  - dashboard 必须**新增** fetch('/api/daily-advice')，与既有 8 个 fetch 并入 Promise.all
  - dailyAdvice computed → 改为返回 advice.value（来自 /api/daily-advice 响应），**不再**根据 sentiment / market / leader 实时计算
  - 所有原本 dailyAdvice 字段（cls / text / suggestedPosition / reason）的渲染（505-509 / 595-596 / 657-666）必须无缝切换：cls 由 bucket 映射（stop→advice-stop / warn→advice-warn / go→advice-go）
  - 9:27 之前 latest_advice.json 不存在时，/api/daily-advice 返回 `{bucket:"go", text:"— 数据加载中 —", suggested_position:"—", reason:""}` 占位（与现状"全 4 维都无数据→数据加载中"分支视觉等价）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-2.1 | 后端**新增** API endpoint `GET /api/daily-advice`：读 `DATA_DIR / "latest_advice.json"` → JSONResponse；文件不存在时返回上述"数据加载中"占位 |
| BR-2.2 | dashboard 字段映射：JSON `bucket`(stop/warn/go) → frontend `cls`(advice-stop/advice-warn/advice-go)；JSON `suggested_position`(snake_case) → frontend 仍用 `suggestedPosition`（camelCase 由前端转换或后端字段镜像，由 Architect 选择最小化改动方案）|
| BR-2.3 | dashboard 不再读取 `market.value` / `sentiment.value` / `leader.value` 来计算 dailyAdvice；所有原 computed 内逻辑（1198-1268）由 `advice.value || placeholder` 替换 |
| BR-2.4 | 所有引用 `dailyAdvice.cls` / `dailyAdvice.text` / `dailyAdvice.reason` / `dailyAdvice.suggestedPosition` 的模板片段（505/508/509/595/596/657-666）保持现状，**绝对不**改动模板 HTML 结构 |
| BR-2.5 | dashboard 端**不**在前端做任何 4 维警戒计算（"看板回到决策时刻"约束）|
| BR-2.6 | 60 秒轮询保留：dashboard 每 60s 重新 fetch /api/daily-advice，但因文件 9:27 写一次后不变，UI 视觉无变化（除非用户手动 refresh-screener 触发再写）|

**UI Interaction**
| Trigger | Behavior |
|---|---|
| 9:27 之前刷新 dashboard | dailyAdvice 显示"— 数据加载中 —" + cls=advice-go（绿色边框，与现状一致）|
| 9:27 之后刷新 dashboard | dailyAdvice 显示 9:27 当时计算结果，盘中**不再变化**（直至下一次 refresh-screener）|
| 用户点 refresh-screener 按钮 | refresh-screener 完成后 loadData() 重新拉 /api/daily-advice，dailyAdvice 更新为最新计算结果 |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| /api/daily-advice 返回 5xx | — | console.error("决策卡加载失败:", e) | dailyAdvice 保留前一次值；不刷"数据加载中"以避免视觉跳变 |
| latest_advice.json 损坏（JSON parse 失败）| — | 后端 print "[决策快照] 读取失败: {e}" | API 返回"数据加载中"占位；dashboard 显示加载中 |

---

### AC3: 邮件 `_calc_daily_advice` 改读 `latest_advice.json`，不再实时算

**Scenario**
```gherkin
GIVEN scheduler.run_screener_update 调用 send_screener_report（line 645-655）
  AND data/latest_advice.json 已在 send_screener_report 调用前由 scheduler 写入（AC1）
WHEN send_screener_report 内部确定当日 advice
THEN
  - send_screener_report 必须**优先**从 latest_advice.json 读取已计算 advice（不重算）
  - 邮件 subject / 6 指标格 / hero 大字栏全部使用该 advice 字段
  - 文件不存在时（边缘场景），**fallback** 到原 _calc_daily_advice(sentiment_data, leader) 实时计算（保护 brownfield 既有不可达分支）
  - send_screener_report 公开签名（参数列表 + 返回值类型）保持不变（BR-3.4）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-3.1 | email_sender 新增 helper `_load_advice_from_disk() -> dict | None`，读 `DATA_DIR / "latest_advice.json"`；文件不存在或解析失败返回 None |
| BR-3.2 | send_screener_report 优先序：`advice = _load_advice_from_disk() or _calc_daily_advice(sentiment_data, leader)` |
| BR-3.3 | _calc_daily_advice 函数**保留**（不删除）：(a) AC9 回归保护需要既有签名不变；(b) latest_advice.json 缺失时 fallback；(c) 单元测试可独立验证算法正确性 |
| BR-3.4 | send_screener_report 公开签名 `(cycle_phase, cycle_day, representative, leader, hits, signals, deviations=None, sentiment_data=None, ranking_data=None) -> bool` 字符级不变（INT-005 inspect.signature baseline 沿用 email-sync-1.1）|
| BR-3.5 | 字段差异调和：latest_advice.json 字段 `suggested_position` (snake_case) → 邮件原代码用 `position` (来自 _calc_daily_advice)。Helper 必须把 JSON 反序列化为 _calc_daily_advice 等价 dict（key 重命名 suggested_position→position / suggested_position_short→position_short），保证 _build_html 完全无感知改动 |
| BR-3.6 | 邮件**不**改 _build_html 的任何渲染逻辑；本 AC 仅改 advice 来源，不改 advice 消费 |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| latest_advice.json 不存在 | — | print "[邮件] 决策快照不存在，回退实时计算" | fallback 到 _calc_daily_advice(sentiment_data, leader) |
| latest_advice.json JSON 损坏 | — | print "[邮件] 决策快照解析失败: {e}，回退实时计算" | 同上 fallback |
| advice 字段缺失（key 不全） | — | print "[邮件] 决策快照字段不全，回退实时计算" | 同上 fallback |

---

### AC4: scheduler 流程顺序确保 advice 在 email 之前可用

**Scenario**
```gherkin
GIVEN scheduler.run_screener_update 在 9:27 cron 或 refresh-screener 触发下完整执行
WHEN 流程进行到原 8c 邮件推送步骤（line 616-657）
THEN
  - latest_advice.json 必须已在该步骤之前写入磁盘（AC1）
  - 推荐插入位置：原 line 574（latest_signals.json 写完之后）与原 591（8a 决策卡）之前
  - 该步必须是**同步**操作（不放在 _background_tasks 线程，line 660-689）
  - 写入失败不阻塞下游（包括 email）；email 走 fallback 路径（AC3 BR-3.x）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-4.1 | 写入步骤必须在 send_screener_report 之前；顺序不可与 8a/8b 并行（决策卡与盘前记录无依赖，可保留原顺序）|
| BR-4.2 | 写入步骤的命名建议为新增 `# 7b. 决策快照（看板 + 邮件单一真源）`（具体行号由 Architect 在 *review 阶段定）|
| BR-4.3 | _calc_daily_advice 在 scheduler 中调用一次，结果同时（a）写入 latest_advice.json；（b）（可选）通过 advice 参数传给 send_screener_report 跳过磁盘读 — Architect 选择最小化改动方案 |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| _calc_daily_advice 抛错（不应发生） | — | print "[决策快照] 计算异常: {e}" | scheduler 继续后续步骤；不写文件；email 走 fallback |
| latest_advice.json 写入异常 | — | print "[决策快照] 写入失败: {e}" | scheduler 继续后续步骤；email 走 fallback |

---

### AC5: 不引入回归（DoD）

**Scenario**
```gherkin
GIVEN 现有邮件 + 看板链路的 4 类边缘分支
WHEN Story 2.1 改造后的代码在以下输入下被调用
THEN 行为应与改造前完全一致：
  - SMTP_USER 或 SMTP_PASSWORD 缺失 → 打印 "[邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送"，返回 False
  - sentiment_data + leader 全 None → latest_advice.json 写入"数据加载中"占位 + email/dashboard 显示"— 数据加载中 —"
  - hits 列表为空 → email 渲染 "无命中标的" 占位（latest_advice.json 仍照常写）
  - send_screener_report 公开签名（inspect.signature）与 email-sync-1.1 baseline 完全相同
  - dashboard 模板 HTML（505-509 / 595-596 / 657-666）字符级未改（仅 JS 逻辑改）
  - dashboard 静态文件 src/static/index.html 中 dailyAdvice 模板与文案不变
```

**Business Rules**
| ID | Rule |
|---|---|
| BR-5.1 | 不重命名 send_screener_report 公开签名 |
| BR-5.2 | 不重构 _build_html / _calc_daily_advice 内部（仅在邮件入口处改 advice 来源；算法本体不动）|
| BR-5.3 | 不引入新依赖（不加 Redis / SQLite write / pydantic 等）|
| BR-5.4 | dashboard 不改 HTML/CSS（只改 `<script setup>` 区域的 JS 逻辑）|
| BR-5.5 | latest_sentiment.json / latest_leader.json / latest_market.json 等既有 latest_*.json 文件契约不变 |
| BR-5.6 | _calc_daily_advice 函数体保持不变（搬运到独立模块的方案由 Architect 决定；如果搬运，import 路径必须更新但函数签名 + 行为不变）|

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| 任一边缘分支行为差异 | — | — | QA 标记为 BLOCKING，回退至 SM revise |

---

## Tasks / Subtasks

> **说明**：测试用例的具体 spec 由 QA 在 *test-design 阶段产出（test_design_level: standard），Dev 在编码后回填本节"测试"子任务。

### Infrastructure Tasks (Shared)

- [x] **T0: 算法落点决策（Architect 决定）** `[AC1, AC3]`
  - [x] Architect *review (2026-05-08) 决定：保留 `_calc_daily_advice` 在 `src/notify/email_sender.py:72-172`，**不**搬运到独立模块（KISS + brownfield 最小变更）
  - [x] scheduler 通过 `from src.notify.email_sender import write_advice_snapshot` function-level import（与既有 `from src.notify.email_sender import send_screener_report` 同模式）
  - [x] 决策结果写入 Dev Log

### Feature Implementation Tasks

- [x] **T1: AC1 — 写 latest_advice.json** `[AC1]`
  - [x] 新增 `email_sender.write_advice_snapshot(sent, leader)` (email_sender.py:69-153)：复用 `_calc_daily_advice` 算法本体，独立计算 dimensions/bad_count/inputs（持久化扩展字段）；UTF-8 indent=2 ensure_ascii=False 写入 `DATA_DIR/latest_advice.json`
  - [x] payload 9 字段全部对齐 BR-1.6：generated_at / bucket / text / suggested_position / suggested_position_short / reason / bad_count / dimensions / inputs
  - [x] 写入异常 try/except 捕获，print "[决策快照] 写入失败"，返回 None 不抛错

  **Test Specs** (from QA test-design — `tests/notify/test_decision_consistency.py`)

  | Scenario ID | 输入 | 预期 | Lvl |
  |---|---|---|---|
  | 2.1-UNIT-001 | `_good_sent()` + `_leader_min()` | payload 含 9 个必填字段（含 dimensions / inputs） | unit |
  | 2.1-UNIT-002 | 任意 sent+leader | `dimensions` keys = `{ld_bad,drop_bad,w_bad,lb_bad}`（4 键 bool） | unit |
  | 2.1-UNIT-003..007 | bad_count=0/1/2/3/4 五组 | bucket / suggested_position / suggested_position_short / reason / dimensions 全部正确 | unit |
  | 2.1-UNIT-008 | 任意 | payload 字段名 snake_case（**不**是 `position`） | unit |
  | 2.1-UNIT-009..010 | leader.main_board_leaders 含完整字段 | inputs 5 键齐全 + main_board_leaders_summary 仅 3 键简化 | unit |
  | 2.1-UNIT-011 | sent=None + leader=None | 仍写文件 + bucket=go + text="— 数据加载中 —"（BR-1.3） | unit |
  | 2.1-UNIT-012 | bucket=stop fixture | reason 与 _calc_daily_advice 字符级相等（BR-1.6） | unit |
  | 2.1-UNIT-013 | mock now_cn() | generated_at 格式 "%Y-%m-%d %H:%M:%S" | unit |
  | 2.1-UNIT-014 | bucket=stop（含中文） | UTF-8 indent=2 ensure_ascii=False（中文不 escape） | unit |
  | 2.1-UNIT-015 `[BLIND-ERROR]` | mock write_text 抛 IOError | 静默 print，不抛 | unit |
  | 2.1-UNIT-016 `[BLIND-BOUNDARY]` | weighted_auction_gain=0 | w_bad=False（严格 `<`） | unit |

- [x] **T2: AC2 — 新增 /api/daily-advice + dashboard 改读** `[AC2]`
  - [x] src/api/app.py:94-120 新增 `@app.get("/api/daily-advice")` endpoint：读 `latest_advice.json` → JSONResponse；不存在/损坏 → 占位 dict (`_DAILY_ADVICE_PLACEHOLDER`)
  - [x] src/static/index.html `loadData()` Promise.all 增加 `fetch('/api/daily-advice')` (第 9 个 fetch)
  - [x] 新增 `const advice = ref(null)`（与 leader 同位置声明）；`advice.value = adviceRes`
  - [x] dailyAdvice computed 重写：读 `advice.value` + bucket→cls 映射 (`_BUCKET_TO_CLS`)；不再访问 market.value/sentiment.value/leader.value
  - [x] 模板 HTML 块（505-509 / 595-596 / 657-666）字符级保持不变（INT-005/INT-015 baseline 验证）

  **Test Specs**

  | Scenario ID | 输入 | 预期 | Lvl |
  |---|---|---|---|
  | 2.1-UNIT-017 | 文件存在 + GET /api/daily-advice | 200 + 文件内容逐键相等 | unit |
  | 2.1-UNIT-018 | 文件不存在 | 200 + 占位 dict（BR-2.1） | unit |
  | 2.1-UNIT-019 `[BLIND-ERROR]` | JSON 损坏 | 200 + 占位 + print "读取失败" | unit |
  | 2.1-INT-001 | grep src/static/index.html | `fetch('/api/daily-advice')` 在 Promise.all 内 | integration |
  | 2.1-INT-002 | grep | `const advice = ref(null)` 已声明 | integration |
  | 2.1-INT-003 | grep | dailyAdvice computed 内**无** market.value/sentiment.value/leader.value 直接读（BR-2.3 / BR-2.5） | integration |
  | 2.1-INT-004 | grep | bucket→cls 映射齐全（stop/warn/go） | integration |
  | 2.1-INT-005 | 模板 diff | 行 505-509 / 595-596 / 657-666 字符级未变 | integration |
  | 2.1-INT-006 | grep | suggested_position → suggestedPosition camelCase 镜像 | integration |

- [x] **T3: AC3 — 邮件改读 latest_advice.json** `[AC3]`
  - [x] email_sender 新增 `_load_advice_from_disk()` (email_sender.py:156-188)：缺失/损坏/字段不全 → 返回 None
  - [x] send_screener_report (email_sender.py:52) 优先序：`advice = _load_advice_from_disk() or _calc_daily_advice(sentiment_data, leader)`
  - [x] 字段反序列化（`suggested_position` → `position`；`suggested_position_short` → `position_short`；bucket 派生 color/bg）
  - [x] _calc_daily_advice 函数体保持不变；send_screener_report 签名字符级冻结（INT-007 验证）

  **Test Specs**

  | Scenario ID | 输入 | 预期 | Lvl |
  |---|---|---|---|
  | 2.1-UNIT-020 | 文件不存在 | _load_advice_from_disk() == None | unit |
  | 2.1-UNIT-021 `[BLIND-ERROR]` | JSON 损坏 | None + print "解析失败" | unit |
  | 2.1-UNIT-022 `[BLIND-BOUNDARY]` | 字段缺 bucket | None + print "字段不全" | unit |
  | 2.1-UNIT-023 | 完整 payload | 返回 dict 含 `position` / `position_short`（BR-3.5 反向重命名） | unit |
  | 2.1-UNIT-024 | disk 命中 | _calc_daily_advice spy.call_count == 0 | unit |
  | 2.1-UNIT-025 | disk 缺 | _calc_daily_advice 被调用 1 次（参数 sentiment_data, leader） | unit |
  | 2.1-UNIT-026 | inspect.getsource | _calc_daily_advice 函数体保留（BR-3.3 / BR-5.6） | unit |
  | 2.1-INT-007 | inspect.signature(send_screener_report) | 签名字符串字符级 == email-sync-1.1 INT-005 baseline | integration |
  | 2.1-INT-008 | disk advice.suggested_position_short="1.5层" | email subject 含 "仓位1.5层" | integration |

- [x] **T4: AC4 — scheduler 流程顺序确认** `[AC4]`
  - [x] scheduler.py:576-596 新增 `# 7b. 决策快照` 块，位于 `latest_signals.json` 写之后、`send_screener_report` 调用之前
  - [x] 同步执行（在主线程中 inline 调用，不使用 `_background_tasks` 线程）
  - [x] write_advice_snapshot 内部 try/except + 外层 try/except 双层兜底，写盘异常不阻塞 email（INT-011 验证）

  **Test Specs**

  | Scenario ID | 输入 | 预期 | Lvl |
  |---|---|---|---|
  | 2.1-INT-009 | grep src/scheduler.py | `latest_advice.json` 写入行号 ∈ (latest_signals.json 行号, send_screener_report 调用行号) | integration |
  | 2.1-INT-010 | grep src/scheduler.py | `latest_advice.json` 写入语句**不在** `def _background_tasks()` 函数体内 | integration |
  | 2.1-INT-011 | mock write_text 抛错 | run_screener_update 不 raise，继续推进到 send_screener_report 调用 | integration |

### Integration & Verification Tasks

- [x] **T5: 端到端一致性集成测试** `[AC1, AC2, AC3]`（DoD #1）

  **Test Specs**

  | Scenario ID | 输入 | 预期 | Lvl |
  |---|---|---|---|
  | 2.1-INT-017 | 同一 sent+leader fixture | 文件内容 == /api/daily-advice 响应（key-by-key 相等） | integration |
  | 2.1-INT-018 (×5 parametrize) | bad_count=0/1/2/3/4 五组 fixture | 三方相等：file["suggested_position_short"] ↔ subject "仓位{...}层" ↔ helper.position_short | integration |

- [x] **T6: 回归保护测试** `[AC5]`（DoD #2）

  **Test Specs**

  | Scenario ID | 输入 | 预期 | Lvl |
  |---|---|---|---|
  | 2.1-INT-012 | SMTP_USER="" + valid sent+leader | "未配置 SMTP_USER 或 SMTP_PASSWORD" 提示 + return False；latest_advice.json **仍**写入 | integration |
  | 2.1-INT-013 `[BLIND-FLOW]` | sent=None + leader=None 全程 | latest_advice.json 写入"数据加载中"占位 + email fallback "数据加载中"分支 | integration |
  | 2.1-INT-014 | hits=[] | latest_advice.json 仍正常写入 + email 渲染"无命中标的"占位 | integration |
  | 2.1-INT-015 | src/static/index.html 模板 diff | 行 505-509 / 595-596 / 657-666 字符级未变（与 INT-005 独立断言） | integration |
  | 2.1-INT-016 `[BLIND-FLOW]` | POST /api/refresh-screener | latest_advice.json 也被重写（与 cron 同源） | integration |

- [x] **T7: 边缘场景测试** `[AC2, AC3]`
  - [x] 7 个蓝点场景全部覆盖：UNIT-019 (ERROR JSON 损坏 API) / UNIT-021 (ERROR JSON 损坏 helper) / UNIT-022 (BOUNDARY 字段不全) / UNIT-015 (ERROR write 失败) / UNIT-016 (BOUNDARY w_avg=0) / INT-013 (FLOW all-None) / INT-016 (FLOW refresh-screener 同源)

- [x] **T8: 最终验收** `[ALL ACs]`
  - [x] 99/99 测试通过（48 本 Story + 46 email-sync-1.1 + 5 fallback-industry-concept）
  - [x] `pytest tests/ -W error` 严格模式全绿
  - [x] Dev Log 完整记录决策快照写入位置 + 算法落点决策（T0）
  - [x] Status → Review

### AC Coverage Matrix

| Task | AC1 | AC2 | AC3 | AC4 | AC5 |
|------|:---:|:---:|:---:|:---:|:---:|
| T0: 算法落点决策 | ✓ |   | ✓ |   |   |
| T1: 写 latest_advice.json | ✓ |   |   |   |   |
| T2: /api/daily-advice + 看板 |   | ✓ |   |   |   |
| T3: 邮件改读 |   |   | ✓ |   |   |
| T4: scheduler 流程顺序 |   |   |   | ✓ |   |
| T5: 端到端一致性 | ✓ | ✓ | ✓ |   |   |
| T6: 回归保护 |   |   |   |   | ✓ |
| T7: 边缘场景 |   | ✓ | ✓ |   |   |
| T8: 最终验收 | ✓ | ✓ | ✓ | ✓ | ✓ |

---

## Dev Notes

### Technical Constraints

| 类别 | 约束 | 来源 |
|---|---|---|
| 决策算法不动 | _calc_daily_advice 函数本体（72-172）字符级保持不变；本 Story 仅迁移**调用时机**与**持久化层**，不改算法 | scope 文件 #102 |
| 真源切换 | 看板 dailyAdvice 不再实时算，固定读 latest_advice.json；该文件是 9:27 决策快照，盘中不变（直到 refresh-screener 重写） | scope 文件 #16-21 |
| 文件结构 | 不重构 _build_html；不改 dashboard 模板 HTML / CSS；仅改 dashboard `<script setup>` 区 + 后端 API 新增 endpoint | scope 文件 #102-106 |
| 公开签名 | send_screener_report 公开签名严格不变（与 email-sync-1.1 INT-005 baseline 一致）| email-sync-1.1 BR-9.1 |
| 不引入新依赖 | 不加 Redis / pydantic / 任何 message bus；继续用 json + 文件 IO | scope 文件 #106 |
| Story 2.5 解耦 | 本 Story 不改 refresh-screener email 行为（refresh-screener 仍每次发邮件，由 Story 2.5 后续改）| scope 文件 #87-89 |

### Accumulated Context (From Previous Stories)

| Resource | Source Story | 状态 | Action |
|---|---|---|---|
| email_sender._calc_daily_advice 算法 | email-sync-1.1 (Done) | 已对齐 dashboard 真源 | REUSE — 本 Story 仅迁移调用时机，不动算法 |
| email_sender.send_screener_report 签名 | email-sync-1.1 (Done) | INT-005 inspect.signature baseline | REUSE — 不可变 |
| email_sender.py:419-451 DATA_DIR/json 未 import | email-sync-1.1 (QA observation) | out-of-scope dead code | 不在本 Story 范围（与 2.1 解耦）|
| latest_sentiment.json / latest_leader.json | 既有 | 字段已就绪 | REUSE — 作为 advice 计算输入 |
| /api/sentiment / /api/leader endpoint | 既有 | 已稳定 | REUSE — 本 Story 仅新增 /api/daily-advice，不改既有 |
| Database Tables | — | N/A — 本 Story 无数据库写入 | — |
| Shared Models | — | N/A — 复用既有 dict 结构 | — |

### Database Design

N/A — 不涉及数据库变更。

### Data Synchronization Requirements

- [x] 本 Story 引入新 JSON 文件 `data/latest_advice.json`，与 `latest_sentiment.json` / `latest_leader.json` 同生命周期（9:27 写一次，refresh-screener 触发重写）
- [x] 文件不入库（与既有 latest_*.json 一致）；.gitignore 已覆盖（确认 `data/` 在 .gitignore 内）

### Data Models

**`latest_advice.json` Schema**（本 Story 新建）:
```python
{
    "generated_at": "2026-05-08 09:27:15",  # str, now_cn() 格式
    "bucket": "stop" | "warn" | "go",
    "text": "🛑 今日不操作" | "⚠️ 谨慎参与" | "✅ 可参与" | "— 数据加载中 —",
    "suggested_position": "0 层（空仓避险）" | "1.5 层（小仓试错）" | "3 层（标准仓位）" | "4 层（连续情绪良好）" | "—",
    "suggested_position_short": "0层" | "1.5层" | "3层" | "4层" | "—",
    "reason": "...警戒文案... 或 ''",
    "bad_count": 0 | 1 | 2 | 3 | 4,
    "dimensions": {
        "ld_bad": bool,    # 竞价跌停 > 5
        "drop_bad": bool,  # 跌幅>9% 个股 > 9
        "w_bad": bool,     # 加权竞价 < 0
        "lb_bad": bool,    # 昨日连板高标任一跌停或水下
    },
    "inputs": {
        "limit_down": int | None,
        "drop_over_9pct": int | None,
        "weighted_auction_gain": float | None,
        "prev_day_limit_down": int | None,
        "prev_day_weighted_auction_gain": float | None,
        "main_board_leaders_summary": [
            {"leader_name": str, "signal": str, "auction_change_pct": float | None}
        ]
    }
}
```

**`/api/daily-advice` Response**（本 Story 新建）：
- 文件存在：`JSONResponse(json.loads(latest_advice.read_text()))`
- 文件不存在：
```python
JSONResponse({
    "generated_at": "",
    "bucket": "go",
    "text": "— 数据加载中 —",
    "suggested_position": "—",
    "suggested_position_short": "—",
    "reason": "",
    "bad_count": 0,
    "dimensions": {"ld_bad": False, "drop_bad": False, "w_bad": False, "lb_bad": False},
    "inputs": {}
})
```

### File Locations

| 文件 | 操作 | 涉及行号（起草时） | 关联 AC |
|---|---|---|---|
| `src/scheduler.py` | **修改** | 约 575（新增 7b 决策快照写入步骤）| AC1, AC4 |
| `src/notify/email_sender.py` | **修改** | 36-66（send_screener_report 改读 disk）+ 新增 _load_advice_from_disk helper；72-172 _calc_daily_advice **不动** | AC3, AC5 |
| `src/api/app.py` | **修改** | 新增 `@app.get("/api/daily-advice")` endpoint（建议位置：line 92 附近，紧跟 /api/leader）| AC2 |
| `src/static/index.html` | **修改** | 1198-1268（dailyAdvice computed 改读 advice.value）+ 1571-1580（loadData Promise.all 加 fetch）+ 新增 `const advice = ref(null)`；模板 505-509 / 595-596 / 657-666 **绝对不动** | AC2 |
| `src/engine/daily_advice.py` | **可能新建** | — | AC1, AC3（仅当 T0 决定搬运算法时；Architect 决定）|
| `tests/notify/test_decision_consistency.py` 或 `tests/test_decision_consistency.py` | **新建** | — | AC1-AC5（具体测试用例由 QA test-design 给出）|
| `data/latest_advice.json` | **运行时新建** | — | AC1（运行时由 scheduler 写）|

### Deliverable Bindings

```yaml
deliverable_bindings:
  - deliverable: "data/latest_advice.json"
    consumer: "src/api/app.py (GET /api/daily-advice) + src/notify/email_sender.py (_load_advice_from_disk)"
    binding_type: config_read
    verify: "src/api/app.py 含 'latest_advice.json' 字符串引用 + src/notify/email_sender.py 含 'latest_advice.json' 字符串引用"

  - deliverable: "src/api/app.py::get_daily_advice (GET /api/daily-advice)"
    consumer: "src/static/index.html loadData() Promise.all"
    binding_type: route_mount
    verify: "src/static/index.html 含 'fetch(.*\\/api\\/daily-advice.*)' 模式"

  - deliverable: "tests/notify/test_decision_consistency.py (or path chosen by Architect)"
    consumer: "pytest discovery (project test runner)"
    binding_type: import_usage
    verify: "pytest 收集到 test_decision_consistency 中的测试用例（具体用例数与名称由 QA test-design 给出）"
```

### Testing Requirements

- **测试设计层级**：`standard`（用户在指令中明确要求）
- **前置流程**：QA *test-design 在开发前出测试设计文档，Dev 据此实现 T5/T7 中的具体集成与边缘场景用例
- **覆盖重点**：
  1. latest_advice.json payload 字段完整性（AC1）
  2. 4 维警戒触发组合（0/1/2/3/4 项）一致性（AC1, AC3）
  3. /api/daily-advice 文件存在 + 不存在 + 损坏的三态（AC2）
  4. 邮件 fallback 路径（latest_advice.json 缺失 / 损坏 / 字段不全）（AC3）
  5. send_screener_report 签名 + 边缘分支不变性（AC5）
  6. dashboard 模板 HTML 字符级 diff 检查（AC5 BR-5.4）
  7. scheduler 流程同步保证（写入在 email 之前）（AC4）

---

## QA Test Design Metadata

- **Level**: Standard
- **Status**: Complete
- **Test Design Status**: Complete
- **Document**: docs/qa/assessments/decision-consistency-2.1-test-design-20260508.md
- **Test Skeleton**: tests/notify/test_decision_consistency.py（44 个测试函数；其中 1 个 parametrize → pytest 运行时展开为 48 用例）
- **Risk Profile**: N/A（test_design_level=standard，不强制；design doc § Risk Coverage 已隐性映射 6 类 brownfield 风险）
- **Scenarios 总数**: 44（P0=36 / P1=7 / P2=1）
  - AC1: 16（UNIT 16）
  - AC2: 9（UNIT 3 / INT 6）
  - AC3: 9（UNIT 7 / INT 2）
  - AC4: 3（INT 3）
  - AC5: 5（INT 5）
  - 端到端一致性: 2（INT 2，含 1 parametrize）
  - 蓝点场景: 7 (BOUNDARY:2 / ERROR:3 / FLOW:2)
- **关键集成断言基线**:
  - `send_screener_report(cycle_phase, cycle_day, representative, leader, hits, signals, deviations=None, sentiment_data=None, ranking_data=None) -> bool`（继承 email-sync-1.1 INT-005 baseline；本 Story 2.1-INT-007 独立断言）
  - dashboard HTML 字符级 diff（仅允许 `<script setup>` 区域改动；模板 505-509 / 595-596 / 657-666 不动；本 Story 2.1-INT-005 / 2.1-INT-015 双重断言）
  - scheduler 静态分析：`latest_advice.json` 写入语句行号位于 `latest_signals.json` 写入之后、`send_screener_report(` 调用之前，且**不在** `def _background_tasks()` 内（2.1-INT-009 / 2.1-INT-010）
- **三方一致性矩阵**（用户 2026-05-08 实盘反馈直接缓解项 / RISK-E）:
  - 5 组 fixture（bad_count=0/1/2/3/4）→ 三方相等：`latest_advice.json["suggested_position_short"]` ↔ email subject `"仓位{...}层"` ↔ `_load_advice_from_disk()["position_short"]`（2.1-INT-018）
- **Architect Major Issue #1 收敛**: 测试只覆盖 BR-4.3 路径 (b)（仅写文件，不在 send_screener_report 加 advice 入参）— 2.1-INT-007 公开签名冻结即为结构性反例。

---

## Change Log

| Date | Agent | Status Transition | Details/Link |
|------|-------|-------------------|--------------|
| 2026-05-08 | SM | Created → AwaitingArchReview | Brownfield 单 Story 起草；偏离标准流程 8 条沿用 email-sync-1.1 路径（无 PRD 分片 / 无 architecture 目录 / scope 文件作虚拟 epic / 跳过 Epic YAML / 跳过 架构上下文 / 跳过 累积校验 / 跳过 Decision 8A / 强制 test_design_level=standard）；scope 文件 [docs/prd/iteration-2-scope.md](../prd/iteration-2-scope.md) 作为真源；HANDOFF 至 architect *review |
| 2026-05-08 | Architect (鲁班) | AwaitingArchReview → AwaitingTestDesign | Score: 9.5/10, 0 Critical / 1 Major / 2 Minor; T0 算法落点决策已给出（保留 src/notify/email_sender.py，不搬运）；HANDOFF 至 qa *test-design |
| 2026-05-08 | QA (宋慈) | AwaitingTestDesign → TestDesignComplete → Approved | Test Design Complete (test_design_level=standard)。Doc: docs/qa/assessments/decision-consistency-2.1-test-design-20260508.md。Skeleton: tests/notify/test_decision_consistency.py (44 测试函数 / parametrize 展开 48 用例 / P0=36 / P1=7 / P2=1 / 蓝点=7 BOUNDARY:2 ERROR:3 FLOW:2)。三方一致性矩阵 2.1-INT-018 直接缓解用户 2026-05-08 实盘"邮件 0 层 vs 看板 1.5 层"反馈 (RISK-E)。Architect Major Issue #1 路径 b 通过 2.1-INT-007 公开签名冻结结构性反例。两阶段状态转换：AwaitingTestDesign → TestDesignComplete → Approved（QA two-phase auto-transition）。HANDOFF 至 dev *develop-story decision-consistency-2.1 |
| 2026-05-08 | Dev (墨子) | Approved → Review | TDD 实现完成（按 Architect 推荐顺序 AC1→AC4→AC3→AC2）。新增 email_sender.write_advice_snapshot + _load_advice_from_disk（算法本体冻结，签名字符级冻结）；scheduler.run_screener_update 7b 同步写入块（latest_signals.json 之后、send_screener_report 之前，**非** _background_tasks 线程）；api/app.py 新增 GET /api/daily-advice；static/index.html 新增 advice ref + Promise.all 第 9 个 fetch + dailyAdvice computed 重写（HTML 模板 505-509/595-596/657-666 字符级冻结）。Architect Major Issue #1 按路径 (b) 收敛。48/48 用例 PASS（含 5 parametrize 三方一致性 + 7 蓝点）；pytest tests/ -W error 99/99 全绿。HANDOFF 至 qa *review decision-consistency-2.1 |
| 2026-05-08 | QA (Turing) | Review → Done | Round 1 QA *review (full mode, MEDIUM risk, automated_plus_spot_check)。Gate=PASS。独立执行 pytest tests/ -W error → 99/99 全绿（story 48/48 + email-sync-1.1 baseline 46/46 + fallback 5/5）。AC1-AC5 全部 VERIFIED（5/5 = 100%）；7/7 蓝点覆盖（BOUNDARY:2 / ERROR:3 / FLOW:2）；三方一致性矩阵 5 组 parametrize 全过（直接缓解用户 2026-05-08 实盘"邮件 0 层 vs 看板 1.5 层"反馈）；send_screener_report inspect.signature 字符级冻结 INT-007 PASS；HTML 模板 505-509/595-596/657-666 字符级未变（INT-005/INT-015 双重断言）；scheduler 7b 块顺序断言 INT-009/010 PASS。0 CRITICAL/HIGH/MEDIUM/LOW issues。Architect Major Issue #1 已按路径 (b) 收敛验证。Gate file: docs/qa/gates/decision-consistency-2.1-927-snapshot-single-source.yml |

---

## Dev Agent Record

### Agent Model Used
- **Agent**: 墨子 (Dev) · claude-opus-4-7[1m]
- **Date**: 2026-05-08
- **Mode**: develop-story (TDD, plan mode)

### Implementation Summary

**核心改动 = "调用时机迁移 + 持久化层 + 单一真源切换"**：

1. **email_sender.py 新增两个 helper（算法本体不动）**：
   - `write_advice_snapshot(sent, leader)`：复用 `_calc_daily_advice` 计算 bucket/text/position/position_short/reason，独立计算 dimensions/bad_count/inputs，写入 `DATA_DIR/latest_advice.json`（UTF-8 indent=2 ensure_ascii=False）；写盘异常 print + return None 不抛错
   - `_load_advice_from_disk()`：读 `latest_advice.json` → snake_case 反向重命名（`suggested_position` → `position`）→ 由 bucket+text 重建 color/bg → 返回 _build_html 完全无感知的 dict；缺失/损坏/字段不全 → 返回 None

2. **email_sender.send_screener_report 改一行**：
   ```python
   advice = _load_advice_from_disk() or _calc_daily_advice(sentiment_data, leader)
   ```
   公开签名字符级冻结（INT-007 inspect.signature baseline 沿用 email-sync-1.1 BR-9.1）

3. **scheduler.run_screener_update 新增 7b 决策快照块**（line 576-596）：
   - 位置：`latest_signals.json` 写之后（line 572-574）、`send_screener_report(` 调用之前（line 645-655）
   - 在主线程同步执行（**非** `_background_tasks` 线程内）
   - 双层 try/except 兜底：write_advice_snapshot 内部 + 外层 import/读盘异常

4. **api/app.py 新增 endpoint `GET /api/daily-advice`**（line 94-120）：
   - 文件存在 → JSONResponse(json content)；不存在/损坏 → 占位 dict + print 错误日志

5. **static/index.html 改 JS 不改 HTML 模板**：
   - 新增 `const advice = ref(null)` 与 leader 同位置
   - `loadData` Promise.all 增加第 9 个 fetch `/api/daily-advice`
   - `dailyAdvice` computed 重写：从 ~70 行 4 维警戒计算 → ~12 行 advice.value 映射；不再访问 `market.value` / `sentiment.value` / `leader.value`
   - 模板 HTML 块（505-509 / 595-596 / 657-666）字符级未动（INT-005/INT-015 baseline 锁定）

**测试**：48 个测试用例（44 函数 + 1 parametrize × 5）全部 PASS；`pytest tests/ -W error` 共 99/99 全绿。

### Database Changes (Structured)
```yaml
{}  # N/A — 本 Story 不涉及数据库变更
```

### API Endpoints Created (Structured)
```yaml
- method: GET
  path: /api/daily-advice
  module: src/api/app.py
  reads: data/latest_advice.json
  response_schema_ref: "Dev Notes › Data Models › /api/daily-advice Response"
```

### Shared Models Created (Structured)
```yaml
{}  # N/A — 本 Story 不涉及新增共享 typed model；新增 latest_advice.json 是非 typed dict（schema 定义在 Dev Notes）
```

### File List

**Modified**:
- `src/notify/email_sender.py` — send_screener_report 改 advice 来源（line 52）；新增 `write_advice_snapshot` (line 69-153) + `_load_advice_from_disk` (line 156-188) + `_BUCKET_COLOR` / `_GO_COLOR_*` / `_LOADING_TEXT` / `_REQUIRED_ADVICE_KEYS` / `_is_num` 模块级常量
- `src/scheduler.py` — 新增 `# 7b. 决策快照` 块（line 576-596），同步写入 latest_advice.json
- `src/api/app.py` — 新增 `_DAILY_ADVICE_PLACEHOLDER` (line 94-104) + `GET /api/daily-advice` endpoint (line 107-120)
- `src/static/index.html` — 新增 `const advice = ref(null)` (与 leader 同位置)；`loadData` Promise.all 增加 fetch `/api/daily-advice`；`dailyAdvice` computed 重写为读 advice.value（行号区间整体下移 -58 line / +1 advice ref +1 fetch）

**Created**:
- `tests/notify/test_decision_consistency.py` — 实现 QA test-design 全部 44 测试函数（含 1 parametrize → 48 用例），覆盖 AC1-AC5 + 7 蓝点场景 + 端到端三方一致性矩阵
- `tests/notify/fixtures/index_template_baseline.json` — 模板 HTML 字符级 baseline（lines 505-509 / 595-596 / 657-666）；INT-005/INT-015 引用

**Runtime-created (not in git)**:
- `data/latest_advice.json` — 9:27 决策快照（与 latest_sentiment.json / latest_leader.json 同生命周期；.gitignore 已覆盖）

### Dev Log Reference
- `docs/dev/logs/decision-consistency-2.1-dev-log.md`

### Open Issues
None — 实现阶段无遗留项。Architect Major Issue #1 已按路径 (b) 收敛（仅写文件，**不**为 send_screener_report 加 advice 入参；签名字符级冻结）。

---

## Architect Review Results

### Review Date: 2026-05-08
### Reviewed By: 鲁班 (Architect)
### Architecture Score: 9.5/10
### Review Round: 1

### Decision: APPROVED → AwaitingTestDesign（test_design_level=standard）

### T0 算法落点决策（Architect 在 *review 阶段交付）

**决策**：保留 `_calc_daily_advice` 在 `src/notify/email_sender.py:72-172`，**不**搬运到 `src/engine/daily_advice.py`。

**理由**（KISS + brownfield 最小变更）：
1. **触面最小**：搬运需要改 email_sender.py + scheduler.py + 新建 src/engine/daily_advice.py + 同步迁移既有 `tests/notify/test_email_decision_alignment.py` 中算法相关用例。保留方案只需改 email_sender.py（新增 `_load_advice_from_disk` + `send_screener_report` 优先序两处）+ scheduler.py（新增 7b 决策快照写入步骤一处）。
2. **既有 import 模式成立**：scheduler.py 已大量使用 function-level import（行 585 `from src.engine.screener_history import ...`、595 `from src.engine.auction_scorer import ...`、618 `from src.notify.email_sender import send_screener_report`）。新增 `from src.notify.email_sender import _calc_daily_advice` 完全同模式。
3. **签名/行为冻结一致**：BR-5.6 要求 `_calc_daily_advice` 函数体不变。保留原位最易守住该约束。
4. **测试搬运成本**：`tests/notify/test_email_decision_alignment.py` 已就位。保留则该测试无需移动。
5. **未来若需独立模块**：可在后续迭代中按需重构（非本 Story 范围）。

**Dev / QA 据此确认**：
- T0（Story Tasks）→ 标记为已决策：保留路径
- File Locations 表 `src/engine/daily_advice.py` 行 → 不新建（标记为 "N/A — Architect 决定保留"）
- BR-5.6 "搬运到独立模块的方案由 Architect 决定" → 决定 = 保留

### Issues

#### Critical Issues (0)
（无）

#### High / Major Issues (1)

- **AC4 BR-4.3 路径选择需收敛为单一路径** (位置：Story §AC4 BR-4.3)
  - 描述：BR-4.3 给 Architect 两种 advice 传递路径 — (a) scheduler 写文件 + 通过 advice 参数传给 send_screener_report 跳过磁盘读；(b) 仅写文件，send_screener_report 自行读盘。两种共存会带来"参数 vs 磁盘"潜在不一致。
  - Fix：**收敛为路径 (b) — 仅写文件**。`send_screener_report` 公开签名 BR-3.4 已要求字符级不变，不应新增 advice 参数；scheduler 一次写入磁盘，send_screener_report 内部 `_load_advice_from_disk()` 单一读路径。这与"看板 + 邮件读同一份 latest_advice.json"的单一真源约束完全一致。
  - 影响：Dev 实现 T1/T3/T4 时严格按路径 (b) 执行；不要为了"性能优化"在 send_screener_report 加 advice 入参。

#### Medium Issues (0)
（无）

#### Low / Minor Issues (2)

- **Accumulated Context 表已过期（事实错误）** (位置：Story §Dev Notes › Accumulated Context, line "email_sender.py:419-451 DATA_DIR/json 未 import")
  - 描述：当前 `src/notify/email_sender.py:19` 已 `import json`、`:25` 已 `from src.config import DATA_DIR, now_cn`；`:421/:447` 已正常使用 DATA_DIR。该 Accumulated Context 行的"未 import"判断已不成立。
  - Recommendation：Dev 实现时无需顾虑该备注；可选地由 SM 在下一次 revise 时把该行更新为"DATA_DIR/json 已 import + 复用"或直接删除。本 Story 范围不要求修改。

- **dailyAdvice 行号轻微不一致** (位置：Story §背景 line 29 / §Dev Notes line 78 / §File Locations 写"1196-1268"或"1198-1268"，混用)
  - 描述：实际 `src/static/index.html` 中 dailyAdvice computed 起点为 line 1198（含 1196-1197 两行注释）。Story 引用混用 1196 / 1198 起点。
  - Suggestion：Dev/QA 实现与测试时以 **1198-1268** 为算法定义起点；1196-1197 为注释行；模板渲染区参考点（505-509 / 595-596 / 657-666）准确无误。

### Recommendations
- **Dev**：T0 已决策（保留路径）；按 AC1→AC4→AC3→AC2 顺序实现（先写盘 → 再确认顺序 → 再改邮件读 → 最后改看板读），便于回归隔离
- **Dev**：严格按 Major Issue #1 收敛 BR-4.3 = 仅磁盘单一路径；不要为 send_screener_report 加 advice 入参
- **QA test-design**：基于 standard 层级出具 test design；重点覆盖
  - latest_advice.json 字段完整性 + 4 维警戒 0/1/2/3/4 项触发组合一致性
  - /api/daily-advice 三态（存在 / 不存在 / 损坏）
  - 邮件 fallback 三态（缺失 / 损坏 / 字段不全 → fallback 到 _calc_daily_advice）
  - send_screener_report 签名 + dashboard 模板字符级 diff 回归保护
  - scheduler 同步写入断言（不在 _background_tasks 线程）
- **QA test-design**：建议 30-50 个用例，可参考 email-sync-1.1（46 用例）作为基线
- **测试文件路径**：建议 `tests/notify/test_decision_consistency.py`（与 `tests/notify/test_email_decision_alignment.py` 同 namespace；Story File Locations 已留空 path 决定权给 Architect/QA — 此处确认走该路径）

### 技术合规评分明细（10 维）

| 维度 | 分 | 备注 |
|---|---|---|
| tech_stack_compliance | 1.0 | json + 文件 IO + FastAPI JSONResponse；无新依赖（符合 BR-5.3） |
| naming_convention_adherence | 1.0 | Python snake_case / JS camelCase；BR-3.5 字段重映射策略明确（snake → 内部 dict） |
| project_structure_alignment | 1.0 | 文件位置与 latest_*.json 语义一致（data/ 目录 + .gitignore 已覆盖） |
| api_design_consistency | 1.0 | /api/daily-advice 与既有 /api/leader（85-91）/ /api/screener（76-82）同模式 |
| data_model_accuracy | 1.0 | latest_advice.json schema 完整（generated_at/bucket/text/positions/reason/bad_count/dimensions/inputs）；字段重映射边界明确 |
| architecture_pattern_compliance | 1.0 | 决策快照模式 = 既有 latest_*.json 单一真源模式（无新基础设施） |
| complete_dependency_mapping | 0.8 | T0 已决策；Accumulated Context 表 1 行过期（minor） |
| integration_feasibility | 1.0 | AC4 同步写入 + 流程顺序明确；fallback 路径清晰 |
| accurate_documentation_references | 0.8 | dailyAdvice 行号 1196/1198 轻微不一致；其余引用（INT-005、scope 章节、send_screener_report 签名）准确 |
| overall_implementation_feasibility | 1.0 | 所有 AC 可独立实施 + 验证；测试用例分布明确 |

**总分**：9.6/10 → 取整 **9.5/10** → 通过（≥ 7/10）

---

## QA Results

### QA Review

- **Round**: 1
- **Risk Level**: MEDIUM
- **Review Mode**: automated_plus_spot_check (skip_e2e=true; 项目无 Playwright 环境)
- **Gate**: PASS
- **Tests**: 99/99 passed (story 48/48 + email-sync-1.1 baseline 46/46 + fallback 5/5)
- **Blind Spots**: 7/7 covered (BOUNDARY:2 / ERROR:3 / FLOW:2)
- **AC Coverage**: 5/5 fully verified (100%)
- **Issues**: 0 critical / 0 high / 0 medium / 0 low
- **Three-way Consistency Matrix**: ×5 parametrize 全过 — 用户 2026-05-08 实盘"邮件 0 层 vs 看板 1.5 层"反馈直接缓解
- **Signature Freeze**: send_screener_report inspect.signature 与 email-sync-1.1 baseline 一致（INT-007 PASS）
- **Template Freeze**: src/static/index.html 行 505-509/595-596/657-666 字符级未变（INT-005/INT-015 双重断言）
- **Scheduler Order**: 7b 决策快照写入位于 latest_signals.json @572 之后 / send_screener_report @640+ 之前，且不在 _background_tasks 内（INT-009/010 PASS）
- **Architect Major Issue #1**: 收敛到路径 (b) — 仅写文件，不为 send_screener_report 加 advice 入参（结构性反例 INT-007 验证）
- **Gate File**: `docs/qa/gates/decision-consistency-2.1-927-snapshot-single-source.yml`
- **Evidence**: 无（无问题，Step 6 evidence collection skipped）

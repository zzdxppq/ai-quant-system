# Test Design: decision-consistency-2.1 (9:27 决策快照单一真源)

2026-05-08 | 宋慈 (QA)

## Overview

| Metric | Value |
|---|---|
| 测试设计层级 | Standard |
| 总场景数 | 44（含 1 个 parametrize→5 sub-case，pytest 运行时展开为 48 用例） |
| 单元测试 | 26 (59%) |
| 集成测试 | 18 (41%) |
| E2E | 0 (0%) — 无 UI/跨系统旅程；DoD 全部由 UNIT+INT 验证 |
| 蓝点场景 | 7 (BOUNDARY:2 / ERROR:3 / FLOW:2) |
| P0 | 36 |
| P1 | 7 |
| P2 | 1 |

**说明**

- 测试技术栈：`pytest`（沿用 `tests/notify/test_email_decision_alignment.py` 同 namespace + 同 fixture 风格）。
- 真源对照：`src/static/index.html:1198-1268`（dailyAdvice computed）与 `src/notify/email_sender.py:72-172`（`_calc_daily_advice`）。本 Story 不改算法本体，只改调用时机与持久化层。
- 测试 ID 前缀 `2.1` = decision-consistency-2.1。
- `_calc_daily_advice` 输出 dict key = `bucket / text / position / position_short / reason / color / bg`；`latest_advice.json` 字段 = `bucket / text / suggested_position / suggested_position_short / reason / bad_count / dimensions / inputs / generated_at`（重命名 `position → suggested_position`）。
- `_load_advice_from_disk` 是反向重命名（`suggested_position → position` / `suggested_position_short → position_short`），保 `_build_html` 完全无感知（BR-3.5）。
- E2E 已显式排除：本 Story 不动 SMTP 真实链路；dashboard 渲染由 `_build_html` 已存在的 INT 间接覆盖；Architect *review 阶段已确认 `tests/notify/test_decision_consistency.py` 是测试文件路径。

---

## Scenarios by AC

### AC1: 9:27 选股完成后写 `data/latest_advice.json`

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.1-UNIT-001 | U | P0 | `write_advice_snapshot(sent, leader)` 写入文件后，加载 JSON 含 9 个必填字段：`generated_at / bucket / text / suggested_position / suggested_position_short / reason / bad_count / dimensions / inputs`（BR-1.6） | payload schema 完整性 |
| 2.1-UNIT-002 | U | P0 | `dimensions` 字段结构 = `{"ld_bad": bool, "drop_bad": bool, "w_bad": bool, "lb_bad": bool}`（4 键齐全 + 类型为 bool） | dimensions schema |
| 2.1-UNIT-003 | U | P0 | bad_count=0 + 4 维全绿 → `bucket="go"` + `dimensions` 全 `False` | 0 维触发 |
| 2.1-UNIT-004 | U | P0 | 仅 ld_bad 触发（`limit_down=8`）→ `bad_count=1` + `bucket="warn"` + `suggested_position="1.5 层（小仓试错）"` + `suggested_position_short="1.5层"` + `dimensions.ld_bad=True`（其余 False） | 1 维触发 |
| 2.1-UNIT-005 | U | P0 | ld_bad + drop_bad 触发 → `bad_count=2` + `bucket="stop"` + `suggested_position="0 层（空仓避险）"` + `suggested_position_short="0层"` | 2 维触发 |
| 2.1-UNIT-006 | U | P0 | ld_bad + drop_bad + w_bad → `bad_count=3` + reason 末尾 `"四维警戒中已 3 项触发"` | 3 维触发 |
| 2.1-UNIT-007 | U | P0 | 4 维全部触发 → `bad_count=4` + `dimensions` 全 `True` + reason 含 4 条 warning | 4 维上限 |
| 2.1-UNIT-008 | U | P0 | payload 字段名采用 `suggested_position` / `suggested_position_short` snake_case（**不**是 `position` / `position_short`） | 字段命名（AC2 dashboard / AC3 helper 共同契约） |
| 2.1-UNIT-009 | U | P0 | `inputs` 字段保留：`limit_down / drop_over_9pct / weighted_auction_gain / prev_day_limit_down / prev_day_weighted_auction_gain`（5 键齐全，缺失输入填 None）（BR-1.6） | 排错快照 |
| 2.1-UNIT-010 | U | P1 | `inputs.main_board_leaders_summary` 仅含 `[{leader_name, signal, auction_change_pct}]` 简化结构，**不**复制 `board_count / leader_gain_10d` 等其他字段（BR-1.6） | 简化结构契约 |
| 2.1-UNIT-011 | U | P0 | sent=None + leader=None → `bucket="go"` + `text="— 数据加载中 —"` + `suggested_position="—"` + `bad_count=0` + 文件**仍然写入**（不 skip）（BR-1.3） | "数据加载中" fallback |
| 2.1-UNIT-012 | U | P0 | bucket=stop 时 `reason` 文案与 `_calc_daily_advice(sent, leader)["reason"]` **逐字符相等**（BR-1.6 算法等价） | 算法等价（不引入新算法） |
| 2.1-UNIT-013 | U | P1 | mock `now_cn()` → `2026-05-08 09:27:15` → `generated_at == "2026-05-08 09:27:15"`（BR-1.2） | 时间戳格式 |
| 2.1-UNIT-014 | U | P2 | 文件以 UTF-8 + indent=2 + ensure_ascii=False 写入 → 中文字符（`"今日不操作"`）原样存储，不被 escape 为 `\uXXXX` | 编码契约 |
| 2.1-UNIT-015 | U | P0 | mock `Path.write_text` 抛 `IOError("disk full")` → 函数捕获、`print("[决策快照] 写入失败: ...")`、**不抛错给 caller**（Error Handling）`[BLIND-ERROR-005]` | resource exhaustion 静默 |
| 2.1-UNIT-016 | U | P1 | `weighted_auction_gain=0`（边界）→ `w_bad=False`（严格 `<` 0，沿袭 _calc_daily_advice）`[BLIND-BOUNDARY-002]` | 边界值（继承） |

### AC2: 看板 `dailyAdvice` 改读 `/api/daily-advice`，不再实时算

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.1-UNIT-017 | U | P0 | `GET /api/daily-advice` + 文件存在 → 200 + JSONResponse 字段与磁盘文件**完全相等** | API 主路径 |
| 2.1-UNIT-018 | U | P0 | `GET /api/daily-advice` + 文件不存在 → 200 + `{bucket:"go", text:"— 数据加载中 —", suggested_position:"—", suggested_position_short:"—", reason:"", bad_count:0, dimensions: 全 False, inputs:{}}`（BR-2.1） | 占位响应 |
| 2.1-UNIT-019 | U | P1 | `GET /api/daily-advice` + 文件存在但 JSON 损坏 → 200 + 占位响应 + print "[决策快照] 读取失败"（Error Handling）`[BLIND-ERROR-003]` | invalid response 守护 |
| 2.1-INT-001 | I | P0 | `src/static/index.html` `loadData()` 内 `Promise.all` 含 `fetch('/api/daily-advice')`（grep 断言）（Story T2 #277） | 前端拉取契约 |
| 2.1-INT-002 | I | P0 | `src/static/index.html` 含 `const advice = ref(null)` 声明 + 收 `/api/daily-advice` 响应（grep 断言）（Story T2 #278） | 状态变量契约 |
| 2.1-INT-003 | I | P0 | `dailyAdvice` computed 内**不再**出现 `market.value` / `sentiment.value` / `leader.value` 直接读（仅读 `advice.value`）（BR-2.3 / BR-2.5） | 真源切换断言 |
| 2.1-INT-004 | I | P0 | bucket → cls 映射：`stop→"advice-stop"`, `warn→"advice-warn"`, `go→"advice-go"`（grep mapAdvice 函数体或 ternary 断言）（BR-2.2） | 字段映射 |
| 2.1-INT-005 | I | P0 | dashboard 模板 HTML 行 `505-509 / 595-596 / 657-666` 逐字符未变 — 与 git 中本 Story 起草前 commit 的对应行做 diff（BR-2.4 / BR-5.4 / BR-2.6 隐含） | 模板契约不破 |
| 2.1-INT-006 | I | P1 | dashboard JS：JSON `suggested_position` (snake_case) 在前端被映射为 `suggestedPosition`（camelCase）以保持模板 `{{ dailyAdvice.suggestedPosition }}`（line 509）不动（BR-2.2） | 字段镜像 |

### AC3: 邮件 `_calc_daily_advice` 改读 `latest_advice.json`，不再实时算

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.1-UNIT-020 | U | P0 | `_load_advice_from_disk()` + 文件不存在 → return `None`（BR-3.1） | helper 主路径 |
| 2.1-UNIT-021 | U | P0 | `_load_advice_from_disk()` + 文件 JSON 损坏 → return `None` + print "[邮件] 决策快照解析失败"（Error Handling）`[BLIND-ERROR-003]` | invalid response 守护 |
| 2.1-UNIT-022 | U | P0 | `_load_advice_from_disk()` + 文件 JSON 解析成功但缺关键字段（如缺 `bucket`）→ return `None` + print "[邮件] 决策快照字段不全"（Error Handling）`[BLIND-BOUNDARY-001]` | 字段守护（防半残数据） |
| 2.1-UNIT-023 | U | P0 | `_load_advice_from_disk()` + 字段完整 → 返回 dict 含 `position` / `position_short` 键（**已**反向重命名 from `suggested_position` / `suggested_position_short`）；其他键透传（BR-3.5） | 字段反序列化契约 |
| 2.1-UNIT-024 | U | P0 | `send_screener_report` + `_load_advice_from_disk` 返 dict → `_calc_daily_advice` **不被调用**（spy 验证）（BR-3.2） | disk 优先 |
| 2.1-UNIT-025 | U | P0 | `send_screener_report` + `_load_advice_from_disk` 返 None → `_calc_daily_advice(sentiment_data, leader)` 被调用 1 次（spy）（BR-3.2） | fallback 路径 |
| 2.1-UNIT-026 | U | P0 | `_calc_daily_advice` 函数定义仍存在于 `src/notify/email_sender.py`（inspect.getsource 不为空 + 函数体非空）（BR-3.3 / BR-5.6） | 算法本体保留 |
| 2.1-INT-007 | I | P0 | `inspect.signature(send_screener_report)` 字符串与 email-sync-1.1 INT-005 baseline 完全一致（参数顺序 + 默认值 + 注解 + 返回类型 ≡ `(cycle_phase, cycle_day, representative, leader, hits, signals, deviations=None, sentiment_data=None, ranking_data=None) -> bool`）（BR-3.4） | 公开签名冻结 |
| 2.1-INT-008 | I | P0 | `send_screener_report` + 磁盘文件 advice 含 `suggested_position_short="1.5层"` → 邮件 subject 含 `"仓位1.5层"`（与磁盘字段保持一致，无丢失） | 端到端字段对齐 |

### AC4: scheduler 流程顺序确保 advice 在 email 之前可用

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.1-INT-009 | I | P0 | 静态分析 `src/scheduler.py`：`latest_advice.json` 写入语句出现在 `latest_signals.json` 写入语句**之后** + `send_screener_report(...)` 调用**之前**（grep + line-number assertion）（BR-4.1 / BR-4.2 / Architect Major Issue #1 收敛路径 b） | 流程顺序契约 |
| 2.1-INT-010 | I | P0 | 静态分析 `src/scheduler.py`：`latest_advice.json` 写入语句**不在** `def _background_tasks()` 函数体范围内（同步执行保证；不放线程）（BR-4.3 / Story T4 #292） | 同步保证 |
| 2.1-INT-011 | I | P1 | mock `Path.write_text` 抛错 → `run_screener_update` **继续推进**到 `send_screener_report` 调用（不 raise）（Error Handling Table） | 写入失败不阻塞下游 |

### AC5: 不引入回归（DoD）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.1-INT-012 | I | P0 | `SMTP_USER=""` → `send_screener_report` 输出 `"[邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送"` + return `False`（即便磁盘 advice 已就位）；且本测试**单独**验证 latest_advice.json 写入步骤已先行完成（不被 SMTP 缺失影响） | DoD 边缘分支 1 |
| 2.1-INT-013 | I | P0 | `sentiment_data=None + leader=None` 全程 → `latest_advice.json` 写入 "数据加载中" 占位（含 `bad_count=0 / dimensions all False / inputs={}`）+ email fallback 走 `_calc_daily_advice` "数据加载中" 分支（subject 含 `仓位—`）`[BLIND-FLOW-003]` | DoD 边缘分支 2 |
| 2.1-INT-014 | I | P0 | `hits=[]` → `latest_advice.json` 仍按 sentiment+leader 正常写入 + email HTML 渲染 `"无命中标的"` 占位（既有行为不变） | DoD 边缘分支 3 |
| 2.1-INT-015 | I | P0 | `src/static/index.html` 行 `505-509 / 595-596 / 657-666` 与 git HEAD 起草前 commit 字符级 `diff -u` 输出为空（仅允许 `<script setup>` 区域改动）（BR-5.4） | 模板回归断言 |
| 2.1-INT-016 | I | P1 | `POST /api/refresh-screener` 触发 `run_screener_update` → `latest_advice.json` 也被重写（与 9:27 cron 路径同源；refresh-screener 同步写一次）`[BLIND-FLOW-002]` | refresh-screener 路径不漏 |

### 端到端一致性（DoD #1：跨 AC1+AC2+AC3）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.1-INT-017 | I | P0 | 一致性核心：构造同一组 `(sentiment_data, leader)` fixture → 调用 `run_screener_update`（mock SMTP）→ 加载 `latest_advice.json` 与 `GET /api/daily-advice` 响应字段**逐键相等**（AC1 ↔ AC2 一致） | 看板与文件一致 |
| 2.1-INT-018 | I | P0 | 一致性 5 组矩阵（parametrize bad_count=0,1,2,3,4）：每组断言 `latest_advice.json["suggested_position_short"]` ↔ `email subject 中 "仓位{...}层" 子串` ↔ `_load_advice_from_disk()["position_short"]` 三方相等（AC1 ↔ AC3 一致 + 4 维警戒触发组合覆盖） | 邮件与文件一致 + 警戒矩阵 |

---

## Blind Spot Scenarios `[BLIND-SPOT]`

| ID | Category | Pri | Scenario | Ref |
|---|---|---|---|---|
| 2.1-UNIT-015 | ERROR | P0 | `write_text` 抛 IOError → 静默 print，不抛错 | ERROR-005 / ERROR-001 |
| 2.1-UNIT-016 | BOUNDARY | P1 | `weighted_auction_gain=0` 边界 → w_bad=False | BOUNDARY-002 |
| 2.1-UNIT-019 | ERROR | P1 | `/api/daily-advice` 文件 JSON 损坏 → 占位响应 | ERROR-003 |
| 2.1-UNIT-021 | ERROR | P0 | `_load_advice_from_disk` JSON 损坏 → None + 邮件 fallback | ERROR-003 |
| 2.1-UNIT-022 | BOUNDARY | P0 | `_load_advice_from_disk` 字段不全 → None + 邮件 fallback | BOUNDARY-001 |
| 2.1-INT-013 | FLOW | P0 | sent+leader 全 None → 全链路降级到 "数据加载中"（不抛错、不跳过文件写） | FLOW-003 |
| 2.1-INT-016 | FLOW | P1 | refresh-screener 路径也重写 latest_advice.json（与 cron 一致） | FLOW-002 |

**显式 SKIP 蓝点**（不强制）

| Category | Reason |
|---|---|
| CONCURRENCY | scheduler 写文件 vs API 读文件 — 由 `Path.write_text` POSIX `close-to-replace` 语义保证原子性；测试不强求 |
| RESOURCE | 本 Story 不引入连接池/锁/temp file；nothing to clean |
| DATA / Cross-Service Sync | 本 Story 单进程内文件 IO；无跨服务一致性需求 |
| Cascade Effects | latest_advice.json 是 **derived** 文件，与 latest_sentiment.json / latest_leader.json 单向依赖；删除 latest_advice.json 不需要级联清理 |

---

## Risk Coverage

本 Story `test_design_level=standard` 不强制 risk-profile，但本 design 隐性覆盖以下 brownfield 风险：

| 风险 | 缓解 scenario |
|---|---|
| RISK-A：`_calc_daily_advice` 行为漂移（算法被意外改动） | 2.1-UNIT-012 / 2.1-UNIT-026（算法等价 + 函数体保留） |
| RISK-B：`send_screener_report` 公开签名漂移破坏 brownfield 调用方 | 2.1-INT-007（继承 email-sync-1.1 INT-005 baseline） |
| RISK-C：dashboard 模板被改动破坏视觉契约 | 2.1-INT-005 / 2.1-INT-015（双重模板字符级 diff） |
| RISK-D：磁盘读写故障导致选股链路崩溃 | 2.1-UNIT-015 / 2.1-INT-011（写入静默 + 不阻塞下游） |
| RISK-E：邮件与看板"同一只股票相反仓位建议"用户实盘困惑（用户 2026-05-08 反馈） | 2.1-INT-017 / 2.1-INT-018（端到端三方一致性矩阵） |
| RISK-F：refresh-screener 路径漏写 latest_advice.json 导致用户手动触发后看板未更新 | 2.1-INT-016 |

---

## Execution Order

1. **P0 Unit**（21 用例中 P0 部分）— 算法与 helper 逻辑（最快反馈）
2. **P0 Integration**（dashboard JS grep / scheduler 静态分析 / `inspect.signature` 等纯静态断言）
3. **P0 Integration**（运行时：`run_screener_update` mock SMTP + 文件 IO）
4. **端到端一致性 INT-017 / INT-018**（跨 AC，最后跑 — 任何先序破坏都会先在前面失败）
5. **P1**（蓝点 + 边界 + refresh-screener）
6. **P2**（编码契约 — UNIT-014）

---

## Quality Checklist

**Standard Coverage**
- [x] AC1（5 BR + 3 错误处理）→ 16 用例 ✓
- [x] AC2（6 BR + 2 错误处理）→ 9 用例 ✓
- [x] AC3（6 BR + 3 错误处理）→ 9 用例 ✓
- [x] AC4（3 BR + 2 错误处理）→ 3 用例 ✓
- [x] AC5（6 BR + 1 错误处理）→ 5 用例 ✓
- [x] 无重复（无 unit/int 同断言双覆盖；INT 只覆盖跨函数/grep/静态分析无法 unit 化的场景）
- [x] 关键路径多层覆盖（_calc_daily_advice 已有 email-sync-1.1 46 用例兜底；本 design 仅在落点切换层补充）
- [x] ID 遵循 `2.1-{LEVEL}-{SEQ}` 命名

**Blind Spot Coverage**
- [x] 每个文件 IO 输入有 BOUNDARY（UNIT-022 字段不全 / UNIT-016 数值边界）
- [x] 每个外部依赖（磁盘）有 ERROR（UNIT-015 写入 / UNIT-019 / UNIT-021 读取）
- [x] 多步流程有 FLOW（INT-013 全 None / INT-016 refresh-screener）
- [x] 显式记录 SKIP 蓝点理由（CONCURRENCY/RESOURCE/DATA/Cascade）

---

## Trace References

```
Test design: docs/qa/assessments/decision-consistency-2.1-test-design-20260508.md
Test skeleton: tests/notify/test_decision_consistency.py
Sibling baseline: tests/notify/test_email_decision_alignment.py (email-sync-1.1, 46 cases)
P0: 36 / Total: 44 (parametrize 展开 48)
```

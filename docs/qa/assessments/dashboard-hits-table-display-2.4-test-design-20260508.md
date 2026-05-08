# Test Design: dashboard-hits-table-display-2.4 — 选股表市值 NaN 安全化 + 板块概念服务端注入

**Date**: 2026-05-08 | **Author**: Turing (QA) | **Story**: dashboard-hits-table-display-2.4 | **Level**: standard

---

## Overview

| Metric | Value |
|---|---|
| **Total scenarios** | 56 |
| **By level** | Unit: 53 (95%) · Integration: 3 (5%) · E2E: 0 |
| **By priority** | P0: 23 · P1: 28 · P2: 5 · P3: 0 |
| **Blind-spot scenarios** | 18 (BOUNDARY 7 / ERROR 6 / FLOW 3 / DATA 2) |
| **Coverage** | AC1 ✓ · AC2 ✓ · AC3 ✓ · AC4 ✓ · AC5 ✓ |
| **Story type** | brownfield-enhancement (full-stack: Python helper + scheduler 集成 + Vue inline 模板) |
| **Applicable blind-spot categories** | BOUNDARY · ERROR · FLOW · DATA (CONCURRENCY/RESOURCE 不适用 — 本 Story 不引入并发原语 / 不持有资源 / 仅同步 in-place mutate dict) |

---

## Test Strategy 摘要

本 Story 是一次**最小化、隔离良好的 brownfield 增强**，包含 4 个相互低耦合的实施面：
1. `src/engine/screener.py` 模块级新增 `_safe_round` + `ScreenerHit.market_cap` 类型扩展（纯函数，无 I/O）
2. `src/engine/screener_concept_enrich.py` 新模块（in-place mutate dict + cache fallback chain，与 `concept_stats.enrich_ranking_with_top_concepts` 同模式）
3. `src/scheduler.py` 在 line 532-537 之间插入 4 行（读 `latest_ranking.json` + 调用 enrich）
4. `src/static/index.html` 三处微改（line 638 v-if + line 1436-1438 industryOf + line 1441-1443 topConceptsOf）

测试金字塔向 **unit** 显著倾斜（91%），少量 **integration**（9%），**不写 E2E**：

- **Unit (91%)**：`_safe_round` 全输入空间（AC1）+ enrich helper 9 状态矩阵（AC3）+ 模板/JS helper 静态文本断言（AC2/AC4，沿用 watch-pool-2.2 的 SHA256 baseline + grep 模式）+ 回归基线 freeze（AC5）
- **Integration (9%)**：`scheduler.run_screener_update` mock-DATA_DIR fixture，验证 `latest_screener.json` 落盘后包含 `top_concepts` + `industry` 字段（AC3 端到端）+ 跨 Story 回归（email-sync-1.1 / decision-consistency-2.1 baseline 99 测试不破）
- **E2E**：N/A — 无 UI 浏览器交互改动（区域 D 模板分支 v-if/v-else 字符级冻结，仅市值列单元格内部 + helper 函数体内部变化）

**Mock 边界**（采纳 Architect Review T0.2 + Recommendation #4）：
- `_safe_round` 是纯函数 → 直接 import 调用，不需 mock
- enrich helper 单测：用 `monkeypatch.setattr` 替换模块级 `load_stock_to_concepts` / `aggregate_concept_limit_ups` / `top_concepts_for_stock` / `filter_concepts` 引用；不依赖真实 cache 文件
- scheduler 集成测试：`monkeypatch.setattr("src.scheduler.DATA_DIR", tmp_path)`，配合 fixture 写入 `latest_ranking.json` / `concept_cache.json` / `limit_up_cache.json` / `industry_cache.json`，patch `src.engine.fetcher.*` 行情拉取层
- 模板静态测试：直接 `Path("src/static/index.html").read_text()` + 行号切片 + SHA256/grep（无浏览器依赖）

**Hash baseline 策略**（沿用 watch-pool-2.2 Recommendation #5）：
- 新建 `tests/fixtures/screener_display_baselines.json` 存放 `src/static/index.html` 关键区域的 SHA256：
  - 表头（line 615-624）
  - 板块列模板分支（line 639-646）
  - 其他 9 列单元格（line 627-637 + line 647-656）
- baseline 由 Dev 在 T3 commit 前 freeze（一次性写入），CI 单测比对 hash；视觉等价但代码层任何字符级变动都会触发 fail。
- 模板第 638 行（市值列）**不**纳入 SHA256（本 Story 唯一允许变更点），改为 substring 断言：含 `v-if="hit.market_cap != null"` + `亿` + `—` (U+2014)。

**回归基线锚点**（Architect Recommendation #4）：
- `inspect.signature(send_screener_report)` 与 email-sync-1.1 commit eb4e883 baseline 一致
- `inspect.signature(run_screener)` / `inspect.signature(run_screener_update)` baseline 一致
- `tests/notify/test_email_decision_alignment.py` (46 测试) + `tests/notify/test_decision_consistency.py` (48 测试) + `tests/notify/test_email_fallback_industry_concept.py` (5 测试) = 99 测试全绿

---

## Scenarios by AC

### AC1: `screener.py` 输出 `market_cap` 为 `None`（不再产生 NaN）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.4-UNIT-001 | U | P0 | `_safe_round(2.345)` → `2.35` | 主路径：正常浮点 round 行为不变 |
| 2.4-UNIT-002 | U | P0 | `_safe_round(0)` → `None` | BR-1.1：`<=0` 边界；零值含义为缺失 |
| 2.4-UNIT-003 | U | P0 | `_safe_round(-1.0)` → `None` | BR-1.1：负值边界；异常输入收口 |
| 2.4-UNIT-004 | U | P0 | `_safe_round(float("nan"))` → `None` | 主验收：用户实盘看到的"亿"残缺单位由 NaN 引起，必须收口 |
| 2.4-UNIT-005 | U | P0 | `_safe_round(None)` → `None` | BR-1.1：None 输入透传 |
| 2.4-UNIT-006 | U | P1 | `_safe_round(float("inf"))` → `None` | T6 BLIND-BOUNDARY：异常浮点（非 NaN 但非有限）收口；建议 Dev 用 `math.isfinite(v)` 守护 |
| 2.4-UNIT-007 | U | P1 | `_safe_round("25.0")` → `None`（TypeError 被 except 捕获） | Error Handling row：非数值类型走 None 分支（不抛错给 caller） |
| 2.4-UNIT-008 | U | P1 | `_safe_round({"a": 1})` → `None` | Error Handling row：dict 输入 → math.isnan 抛 TypeError → None 分支 |
| 2.4-UNIT-009 | U | P1 | `_safe_round(25.0, ndigits=0)` → `25.0`（带 ndigits 参数） | BR-1.1 签名：`_safe_round(v, ndigits=2)` 默认 2 位但接受其他位数 |
| 2.4-UNIT-010 | U | P0 | run_screener mock realtime_df 含 `row.market_cap=NaN` → `ScreenerHit.market_cap == None` | BR-1.2 集成：line 206 改造在调用链生效 |
| 2.4-UNIT-011 | U | P0 | run_screener mock 含 `row.market_cap=2.5e9` → `ScreenerHit.market_cap == 25.0` | 回归：有效输入路径不变（25 亿元 → 25.0 亿） |
| 2.4-UNIT-012 | U | P0 | `json.dumps(asdict(hit_with_None))` 含 `'"market_cap": null'`，**不**含字面量 `NaN` | AC1 Scenario 末行：JSON 序列化契约（json.dumps 默认 `allow_nan=True` 会输出 `NaN` — 需要 None 而非 NaN 来满足契约） |
| 2.4-UNIT-013 | U | P1 | 静态读 `src/engine/screener.py` → 含模块级 `def _safe_round(` 定义 | BR-1.1 落点验证（防 commit 部分回退） |
| 2.4-UNIT-014 | U | P1 | 静态读 `src/engine/screener.py:206` → 包含 `_safe_round(market_cap_yi)`，**不**包含 `round(market_cap_yi, 2)` | BR-1.2 调用点替换验证 |
| 2.4-UNIT-015 | U | P1 | 反射 `ScreenerHit.__dataclass_fields__["market_cap"].type` 含 `Optional` 或 `None` 字符串 | BR-1.3 类型注解扩展验证 |
| 2.4-UNIT-016 | U | P1 | 静态读 `src/engine/screener.py` line 156-158 → `if market_cap_yi > 0:` 守护字符级不变 | BR-1.4 软过滤行为不变（防误改） |

### AC2: 选股表模板"市值"列空值显示 "—"

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.4-UNIT-017 | U | P0 | 静态读 `src/static/index.html` line 638 → 含 `v-if="hit.market_cap != null"` | BR-2.1 模板改造主验收 |
| 2.4-UNIT-018 | U | P0 | 静态读 line 638 → 含 `亿`（v-if 分支保留单位） | BR-2.1：正常值仍渲染"{val}亿"格式 |
| 2.4-UNIT-019 | U | P0 | 静态读 line 638 → 含 `—`（U+2014 em-dash），**不**含 `-`（U+002D hyphen-minus）作为空值兜底 | BR-2.4：与异常未匹配额表 line 712 `'—'` 字符级一致 |
| 2.4-UNIT-020 | U | P1 | 静态读 line 638 → 含 `<template v-else>`（双 template 标签结构） | BR-2.1：v-if/v-else 结构（避免 Dev 错用 v-show 或三元表达式） |
| 2.4-UNIT-021 | U | P1 | SHA256(line 615-624 表头区) == baseline | BR-2.3：表头不动 |
| 2.4-UNIT-022 | U | P1 | SHA256(line 627-637 + line 647-656 其他 9 列) == baseline | BR-2.2：其他 9 列字符级不变 |
| 2.4-UNIT-023 | U | P2 | 静态读 line 639-646 → SHA256 == baseline（与 AC4 重叠但锚点不同） | BR-2.3：板块列分支 HTML 结构由 AC4 helper 改写驱动，不应由 AC2 触动 |

### AC3: 服务端为 `screener_hits` 注入 `top_concepts` 与 `industry`

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.4-UNIT-024 | U | P0 | `enrich_screener_hits_with_concepts({"hits":[{"code":"600519",...}]}, {"ranking":[{"code":"600519","top_concepts":["白酒","食品饮料"],"industry":"白酒"}]})` → hits[0]["top_concepts"]=["白酒","食品饮料"]; hits[0]["industry"]="白酒" | BR-3.2 优先级 (1)：ranking 命中路径 |
| 2.4-UNIT-025 | U | P0 | hit.code 不在 ranking + concept_cache + limit_up_cache 可读 → top_concepts 来自 cache 聚合（top_n=2） | BR-3.2 优先级 (2)(3)(4)：cache fallback chain |
| 2.4-UNIT-026 | U | P0 | hit.code 不在 ranking + concept_cache 缺 + limit_up_cache 缺 + industry_cache 缺 → top_concepts=[] + industry=None | BR-3.5：兜底契约（hits 永远可写盘） |
| 2.4-UNIT-027 | U | P0 | hit.code 不在 ranking + industry_cache.json 含 `{"600519":"白酒"}` → industry="白酒" | BR-3.3 优先级 (2)：industry_cache 兜底 |
| 2.4-UNIT-028 | U | P0 | c_map 含元概念 `["沪股通","白酒"]` → top_concepts 仅含 `["白酒"]`（元概念被 `filter_concepts` 过滤） | BR-3.4：元概念双保险过滤 |
| 2.4-UNIT-029 | U | P1 | helper signature: `def enrich_screener_hits_with_concepts(hits_data: dict, ranking_data: dict \| None) -> None`（inspect.signature 验证） | BR-3.1 签名锚点 |
| 2.4-UNIT-030 | U | P1 | helper 修改 hits_data 后返回 None；hits_data 是同一对象（id() 不变） | BR-3.1 in-place mutate 契约 |
| 2.4-UNIT-031 | U | P1 | hit["top_concepts"] 始终 `list[str]`（即便 fallback 全失败也是 [] 而非 None） | Data Validation row：top_concepts 类型不变 |
| 2.4-UNIT-032 | U | P1 | hit["industry"] 类型 `str \| None`（缺失时为 None，**不**为空 str / 0） | Data Validation row：industry 类型 |
| 2.4-UNIT-033 | U | P1 | 静态读 `src/engine/screener.py` → **不**含 `top_concepts` / `industry` 字段在 `ScreenerHit` dataclass | BR-3.6：dataclass 不扩展（运行时 dict mutate） |
| 2.4-INT-001  | I | P0 | `src/scheduler.py` mock DATA_DIR + run_screener_update → `latest_screener.json` 写盘后每条 hit 含 `top_concepts` + `industry` 键 | AC3 主验收：scheduler 集成端到端 |
| 2.4-INT-002  | I | P1 | 静态读 `src/scheduler.py` → 含 `enrich_screener_hits_with_concepts(` 调用，调用行号 < `(DATA_DIR / "latest_screener.json").write_text` 行号 | BR-3.7：调用顺序约束 |
| 2.4-INT-003  | I | P1 | 静态读 `src/scheduler.py` → 含 `latest_ranking.json` 读取代码 + try/except 兜底 → None | T0.2 集成约束（Architect 给的 ranking_data 加载片段） |

### AC4: dashboard 选股表 helper 优先读 `hit` 自身字段

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.4-UNIT-034 | U | P0 | 静态读 `src/static/index.html` line 1441-1443 区域 → `topConceptsOf` 函数体含 `screenerHits.value` 或 `hit.top_concepts` 字面量 | BR-4.1 hit-first 实现 |
| 2.4-UNIT-035 | U | P0 | 静态读 line 1441-1443 区域 → 仍含 `ranking.value` 或 `r?.top_concepts` | BR-4.1 ranking 兜底保留 |
| 2.4-UNIT-036 | U | P0 | 静态读 line 1436-1438 区域 → `industryOf` 函数体含 `hit.industry` 字面量 | BR-4.2 hit-first |
| 2.4-UNIT-037 | U | P0 | 静态读 line 1436-1438 区域 → 仍含 `'-'` 默认兜底 | BR-4.2：'-' 最终 fallback 保留 |
| 2.4-UNIT-038 | U | P1 | SHA256(line 639-646 板块列模板分支) == baseline | BR-4.3：模板 HTML 结构字符级不变（仅 helper 内部逻辑变） |
| 2.4-UNIT-039 | U | P1 | 静态读 line 642 / 645 → 仍含 `hitLive(hit.code).industry` | BR-4.4：hitLive 兜底链不动 |
| 2.4-UNIT-040 | U | P2 | 静态读 line 1441-1443 → 函数行数 ≤ 8（短小 + 不引入 fetch / 新 ref） | BR-4.5：dashboard 不新增 state |

### AC5: 不引入回归（DoD）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.4-UNIT-041 | U | P0 | run_screener(hits=[]) → 返回 []；scheduler 后 latest_screener.json `{date,hits:[]}` 仍正常 | AC5 Scenario row 1：空选股回归 |
| 2.4-UNIT-042 | U | P0 | 全 99 测试基线全绿（pytest tests/notify/ -v） | AC5：邮件 + 决策一致性 + fallback 不破 |
| 2.4-UNIT-043 | U | P1 | `inspect.signature(send_screener_report)` == email-sync-1.1 baseline（已有 baseline 在 `tests/notify/test_email_decision_alignment.py`，本 Story 引用） | BR-5.1：公开签名不变 |
| 2.4-UNIT-044 | U | P1 | `inspect.signature(run_screener)` 参数名/默认值与 baseline 一致 | BR-5.1：公开签名不变 |
| 2.4-UNIT-045 | U | P1 | `latest_screener.json` 写盘后每条 hit 含 13 个既有字段（code, name, continuous_limit_up, open_price, auction_gain, auction_turnover, auction_amount, auction_volume_lots, auction_volume_ratio, market_cap, volume_ratio, gain_10d, matched_cycle）+ 2 个新字段（top_concepts, industry）= 15 字段 | BR-5.5：schema 契约 |
| 2.4-UNIT-046 | U | P1 | enrich helper 幂等性：deep_copy(hits) 后再调一次 → 字符级相等（`json.dumps` 比对） | BR-5.7：幂等契约 |
| 2.4-UNIT-047 | U | P2 | 静态读 `src/api/cross_validator.py` → `s.top_concepts` 路径未变（grep 字面量） | AC5 Scenario row 5：异常未匹配额表路径不动 |

---

## Blind-Spot Scenarios [BLIND-SPOT]

| ID | Category | Pri | Mapped Core ID | Scenario | Ref |
|---|---|---|---|---|---|
| 2.4-BLIND-BOUNDARY-001 | BOUNDARY | P0 | UNIT-005 | `_safe_round(None)` → None | BOUNDARY-001 |
| 2.4-BLIND-BOUNDARY-002 | BOUNDARY | P0 | UNIT-002 | `_safe_round(0)` → None（最小边界值） | BOUNDARY-002 |
| 2.4-BLIND-BOUNDARY-003 | BOUNDARY | P0 | UNIT-004 | `_safe_round(NaN)` → None（"恰超出"有限实数边界） | BOUNDARY-004 |
| 2.4-BLIND-BOUNDARY-004 | BOUNDARY | P1 | UNIT-006 | `_safe_round(inf)` → None（极大值） | BOUNDARY-003 |
| 2.4-BLIND-BOUNDARY-005 | BOUNDARY | P1 | UNIT-007 | `_safe_round("25.0")` → None（类型不匹配） | BOUNDARY-005 |
| 2.4-BLIND-BOUNDARY-006 | BOUNDARY | P2 | UNIT-008 | `_safe_round({"a":1})` → None（dict 输入） | BOUNDARY-005 |
| 2.4-BLIND-BOUNDARY-007 | BOUNDARY | P1 | UNIT-026 | hit.code 在所有 cache 都查不到（新股 / 退市股 / 非主板）→ top_concepts=[] + industry=None | BOUNDARY-001 |
| 2.4-BLIND-ERROR-001 | ERROR | P1 | (新增) | concept_cache.json 文件不存在 → enrich 静默兜底，hit.top_concepts=[] | ERROR-002 |
| 2.4-BLIND-ERROR-002 | ERROR | P1 | (新增) | concept_cache.json JSON 损坏（语法错误）→ 反序列化抛错被捕获 → top_concepts=[] | ERROR-003 |
| 2.4-BLIND-ERROR-003 | ERROR | P1 | (新增) | limit_up_cache.json 不存在 → 概念热度聚合跳过，仍可走 c_map.get(code) → top_concepts=[]（无热度排序）| ERROR-002 |
| 2.4-BLIND-ERROR-004 | ERROR | P1 | (新增) | industry_cache.json 不存在 → industry=None（仅走 ranking）| ERROR-002 |
| 2.4-BLIND-ERROR-005 | ERROR | P2 | (新增) | `filter_concepts` 抛 RuntimeError → 该 hit `top_concepts=[]`（其他 hit 不受影响）| ERROR-003 |
| 2.4-BLIND-ERROR-006 | ERROR | P1 | INT-003 | `latest_ranking.json` 不存在（早期启动 / 容器首次拉起）→ scheduler 加载段 try/except → ranking_data=None → helper 走纯 cache 兜底链 | ERROR-002 |
| 2.4-BLIND-FLOW-001 | FLOW | P1 | UNIT-046 | enrich helper 幂等：同输入两次调用 → 输出字符级相等（in-place mutate 不引入 list duplication）| FLOW-002 |
| 2.4-BLIND-FLOW-002 | FLOW | P1 | UNIT-026 | ranking_data=None + 全 cache 缺 → 100 条 hit 全部得 top_concepts=[] + industry=None（无任何 hit 抛错或被跳过）| FLOW-003 |
| 2.4-BLIND-FLOW-003 | FLOW | P2 | (新增) | hits_data 顶层结构 `{"date":...,"hits":[...]}` 中 `hits` 字段缺失 → helper 不抛错（防御性 dict.get）| FLOW-003 |
| 2.4-BLIND-DATA-001 | DATA | P1 | UNIT-045 | latest_screener.json schema 契约：13 既有字段 type 不变（用 jsonschema-like 断言）+ 2 新字段类型符合 BR-3 Data Validation 表 | DATA-002 |
| 2.4-BLIND-DATA-002 | DATA | P1 | UNIT-012 | json.dumps 对 None 输出 `null`（不开 `allow_nan=True` 兜底输出 `NaN`）| DATA-002 |

**最低覆盖检查**（test-design.md Step 4.5.3）：

- [x] BOUNDARY 每个输入字段 ≥1 — `_safe_round` 7 项 + enrich helper 1 项（hit.code 缺失） ✅
- [x] ERROR 每个外部依赖 ≥1 — concept_cache / limit_up_cache / industry_cache / latest_ranking / filter_concepts = 5 项 + 损坏 1 项 ✅
- [x] FLOW 每个多步过程 ≥1 — enrich 流水（ranking 优先 → cache fallback）3 项 ✅
- [x] CONCURRENCY — N/A（无并发原语；cron 是单线程串行）
- [x] DATA — schema 2 项（既有契约 + JSON 序列化契约） ✅

---

## Risk Coverage

> 本 Story 未单独跑 `*risk-profile`（test_design_level=standard，未触发 securitySensitive）。复用 Architect Review 已识别的 1 High / 2 Medium / 3 Low：

| Risk | 影响 AC | 缓解 Test ID |
|---|---|---|
| **AC3 ranking_data 来源未在 Story 显式说明（Architect High Issue）** | AC3 | INT-003（静态校验 scheduler 加载段）+ BLIND-ERROR-006（latest_ranking.json 缺失）|
| **BR-3.4 文档路径不完整（app.py vs src/api/app.py）（Medium）** | AC3 | UNIT-028（元概念过滤行为）— 路径错填不影响实施，仅文档层 |
| **BR-3.2 "字符级等同"措辞不严谨（Medium）** | AC3 | UNIT-028 + UNIT-024/025/026（fallback 链验证）|
| **新测试目录需 `__init__.py`（Low）** | 测试基础设施 | Dev T4-T6 同步加（不入测试用例）|
| **`from __future__ import annotations` 描述偏冗余（Low）** | AC1 | 不影响实施；UNIT-015 验证 type annotation 即可 |
| **架构文档目录缺失（Low）** | 文档层 | 不影响实施 |

---

## Execution Order

1. **P0 Unit (16)** — 主验收 + 类型/签名锚点（UNIT-001~005, 010~012, 017~019, 024~027, 041~042）
2. **P0 Integration (1)** — INT-001 scheduler 端到端
3. **P1 Unit (24)** — 边缘 + 静态契约 + helper 内部细节（UNIT-006~009, 013~016, 020~022, 028~033, 038~040, 043~046）+ 全 BLIND-BOUNDARY-004~005, BLIND-ERROR-001~004/006, BLIND-FLOW-001~002, BLIND-DATA-001~002
4. **P1 Integration (3)** — INT-002, INT-003 + 跨 Story 回归 99 测试
5. **P2 (6)** — UNIT-023, 040, 047 + BLIND-BOUNDARY-006, BLIND-ERROR-005, BLIND-FLOW-003

---

## Coverage Validation

**Standard Coverage**:
- [x] 每个 AC ≥1 测试 — AC1: 16, AC2: 7, AC3: 13, AC4: 7, AC5: 7 ✅
- [x] 无重复覆盖 — 静态文本断言 vs 行为断言关注点不同（不重叠） ✅
- [x] 关键路径多层 — `_safe_round` (unit) → run_screener (unit) → scheduler INT (int) ✅
- [x] 风险已对接 — Architect 1H/2M/3L 全部映射 ✅

**Blind-Spot Coverage**:
- [x] BOUNDARY 输入字段（_safe_round 6 子类型 + hit.code 1）✅
- [x] ERROR 外部依赖（4 cache 文件 + 1 ranking 文件 + 1 concept filter = 6）✅
- [x] FLOW 多步过程（ranking→cache 链 / 幂等 / hits 字段缺失 = 3）✅
- [x] DATA 跨模块数据完整性（schema + JSON 契约 = 2）✅
- [x] 全 [BLIND-SPOT] 标签正确 ✅

---

## Outputs

### Test Skeleton File

**Path**: `tests/test_screener_display_2_4.py`（沿用 watch-pool-snapshot-2.2 单文件大集合模式；Python 包名规范用 `_`；epic.story 标识符 `2_4` 避免与 dot 分隔冲突）

**Cases**: 47 test functions（每个 scenario 一个 `def test_*`），含 18 个 `[BLIND-SPOT]` 标签

**Validation**：Dev 必须将 `raise NotImplementedError("...")` 全部替换为真实测试逻辑；保留所有用例（可改为 `@pytest.mark.skip(reason=...)` 但不可删）。

### Gate YAML Block

```yaml
test_design:
  scenarios_total: 56
  by_level: {unit: 53, integration: 3, e2e: 0}
  by_priority: {p0: 23, p1: 28, p2: 5}
  blind_spot_scenarios:
    total: 18
    by_category: {BOUNDARY: 7, ERROR: 6, FLOW: 3, CONCURRENCY: 0, DATA: 2, RESOURCE: 0}
  coverage_gaps: []
```

### Trace References

```text
Test design: docs/qa/assessments/dashboard-hits-table-display-2.4-test-design-20260508.md
Test skeleton: tests/test_screener_display_2_4.py
Baselines: tests/fixtures/screener_display_baselines.json (Dev T3 freeze)
P0: 23 (22 unit + 1 integration)
```

---

## Principles Applied

- **Shift left**: 91% unit 优先（pure function + 静态断言 + dict 操作）— `_safe_round` / enrich helper 物理隔离便于隔离测试
- **Risk-based**: 用户实盘反馈"市值=亿"残缺 → AC1 NaN 路径 P0；其余 P1/P2
- **Efficient**: 每个 scenario 仅在最低适用层级测一次（UNIT-024~027 vs INT-001：unit 验证 helper 函数行为，int 验证落盘契约 — 关注点不同，非重复）
- **Maintainable**: SHA256 baseline 存 fixture 文件 → CI 失败提示一目了然，无 ad-hoc 字符串拼接
- **Fast feedback**: P0 16 个 unit 测试预期 < 5s 完成

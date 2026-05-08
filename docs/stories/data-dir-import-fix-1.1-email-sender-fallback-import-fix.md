# Story data-dir-import-fix-1.1: email_sender.py fallback 路径 import 修复

## Story

```yaml
Story:
  id: data-dir-import-fix-1.1
  title: email_sender.py fallback 路径 import 修复（DATA_DIR / json）
  epic: data-dir-import-fix (brownfield, virtual epic — 真源为 docs/prd/iteration-data-dir-import-fix-scope.md)
  status: Done
  mode: quick
  repository: monolith
  priority: P2
  estimated_complexity: trivial
  test_design_level: skip   # quick mode：跳过 Architect *review 与 QA *test-design 前置流程
  story_type: brownfield-bugfix
  derived_from: email-sync-1.1 (QA review 发现的范围外 dead-code observation)
```

**As a** A 股短线交易者（邮件推送的唯一收件人 604491810@qq.com），
**I want** 9:27 选股决策邮件中的"板块列 / 概念列" 在 `ranking_data` 缺字段时能走通 `industry_cache.json` / `limit_up_cache.json` 兜底路径，
**so that** 当盘前缓存未刷新或 ranking API 返回不全时，命中股的板块与概念在邮件里**显示磁盘上已有的真实数据**，而不是被 `NameError` 静默吞掉、最终降级为 "-" 占位。

---

## 背景与问题

`src/notify/email_sender.py` 的 `_build_html` 函数在渲染选股 hits 表时设有**两段** fallback 路径：

| 行号（起草时） | fallback 块 | 数据源 | 触发条件 |
|---|---|---|---|
| 419-424 | `industry_map` 兜底 | `DATA_DIR / "industry_cache.json"` | `ranking_data.ranking[*].industry` 缺失 |
| 438-461（内层 445-453） | `top_concepts_map` 兜底 | `DATA_DIR / "limit_up_cache.json"` 聚合 | `ranking_data.ranking[*].top_concepts` 缺失 |

但**该 2 段 fallback 自引入起即为死代码**：

- 文件第 19-24 行的 import 列表**未导入** `DATA_DIR` 和 `json`：
  ```python
  import os
  import smtplib
  from email.mime.text import MIMEText
  from email.mime.multipart import MIMEMultipart

  from src.config import now_cn   # ← 缺 DATA_DIR
  # ← 缺 import json
  ```
- fallback 块外层包了 `try/except Exception: pass`，把 `NameError: name 'DATA_DIR' is not defined` / `NameError: name 'json' is not defined` 静默吞掉
- 路径**永远**走不到，磁盘上 industry_cache.json / limit_up_cache.json 即使有数据也读不到

### 真源不变性确认

scope 文件 `docs/prd/iteration-data-dir-import-fix-scope.md` 创建时引用 `bbe8c16` 为 bug 引入 commit。**起草本 Story 时复核 git 历史**（root cause analysis）发现：

| Commit | 日期 | 引入内容 | 是否含 import 修复 |
|---|---|---|---|
| `45baa67` | 2026-04-30 | **首次**引入 `DATA_DIR / "industry_cache.json"` + `json.loads(...)` 用于 industry_map 兜底 | ❌（首次留下 import 缺失） |
| `bbe8c16` | 2026-05-06 | 扩展引入第二段 `DATA_DIR / "limit_up_cache.json"` + `json.loads(...)` 用于 top_concepts 兜底 | ❌（沿用 45baa67 的 import 缺失） |
| `eb4e883` | 2026-05-08 | email-sync-1.1 重构 `_calc_daily_advice` + 6 指标格 | 范围外，未触及 fallback |

**Root cause**：scope 文件归因 `bbe8c16` 部分正确（它扩展了 dead-code 模式），但**首次引入**是 `45baa67`。两个 commit 都未补 import，bug 在 8 天的 dead-code 窗口内**一直存在**。本 Story 单 1 行 import 修复**同时**激活两段 fallback 路径，根因一次性根除。

> 引用：QA email-sync-1.1 review gate 文件已识别该 observation：
> "preexisting `DATA_DIR`/json 未 import in `email_sender.py:419-451`, commit 45baa67 引入"
> 见 `docs/qa/gates/email-sync-1.1-email-content-align-dashboard.yml`

---

## Acceptance Criteria

### AC1: Import 完整性

**Scenario**
```gherkin
GIVEN 当前 src/notify/email_sender.py 第 19-24 行 import 列表
WHEN 在 import 区域追加 `import json` 与 `from src.config import DATA_DIR`
THEN
  - `python3 -c "from src.notify.email_sender import _build_html"` 不抛 ImportError
  - `python3 -c "from src.notify.email_sender import DATA_DIR, json"` 能取到模块级名字
  - `grep -nE "DATA_DIR|json\." src/notify/email_sender.py` 中所有引用都在模块级 import 范围内可解析
  - 既有 import（os / smtplib / email.mime.* / src.config.now_cn）保持不变，顺序约定按 PEP 8（标准库 → 第三方 → 项目内）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-1.1 | 仅追加 import，**不**删 / **不**改既有 import 行 |
| BR-1.2 | `from src.config import DATA_DIR` 与现有 `from src.config import now_cn` 合并为同一行（`from src.config import DATA_DIR, now_cn`），避免 PEP 8 多次同源 import 风格警告 |
| BR-1.3 | `import json` 放标准库分组（与 `import os` 同组），按字母序插入 |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| `src.config` 未 export `DATA_DIR` | ImportError | （由 Python 抛出） | **HALT — 升级到 Architect**（验证：起草时 `grep -n DATA_DIR src/config.py` 显示第 38 行 `DATA_DIR = BASE_DIR / "data"` 已 export，本 risk 实际不存在） |

---

### AC2: Fallback 路径走通（industry_cache.json）

**Scenario**
```gherkin
GIVEN 一组 hits 含 code "600519"
  AND ranking_data = {"ranking": [{"code": "600519"}]}  # industry 字段缺失
  AND 磁盘上 DATA_DIR / "industry_cache.json" 含 {"600519": "白酒"}
WHEN 调用 _build_html(..., hits, ..., ranking_data, ...)
THEN
  - 渲染出的 HTML 中 "600519" 行的板块列应显示 "白酒"（来自 industry_cache.json 兜底）
  - **不**显示 "-"（即 fallback 真的被命中，不再被 NameError 吞掉）
  - 与 dashboard 端 `src/static/index.html` 在同样数据缺失时的 fallback 行为方向一致（dashboard 端读 `/api/...`，此处读磁盘缓存，路径不同但目标"显示真实板块名"等价）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-2.1 | `industry_cache.json` 文件不存在或解析失败时，外层 `try/except Exception: pass` 维持原静默行为，板块列降级为 "-"（与修复前一致）—— 仅在文件存在且 json 合法时才注入 |
| BR-2.2 | `ranking_data.ranking[*].industry` 存在时**优先**使用 ranking 字段（既有逻辑：fallback 在前，ranking 注入在后，覆盖兜底值），fallback 仅在 ranking 缺字段时填补 |
| BR-2.3 | 不修改 `industry_cache.json` 的文件格式约定（key=股票代码 str, value=行业名 str） |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| industry_cache.json 文件不存在 | — | （静默） | industry_map 保持空 dict，最终板块列降级为 "-" |
| industry_cache.json json 解析失败 | — | （静默） | 同上 |
| 磁盘 IO 异常（权限 / FS 错误） | — | （静默） | 同上 |

---

### AC3: Fallback 路径走通（limit_up_cache.json → top_concepts）

**Scenario**
```gherkin
GIVEN 一组 hits 含 code "600519"
  AND ranking_data = {"ranking": [{"code": "600519"}]}  # top_concepts 字段缺失
  AND 磁盘上 DATA_DIR / "limit_up_cache.json" 含某交易日的涨停股列表
  AND src.data.concept_fetcher.load_stock_to_concepts() 能返回 600519 → ["白酒", "消费"]
WHEN 调用 _build_html(..., hits, ..., ranking_data, ...)
THEN
  - 渲染出的 HTML 中 "600519" 行的概念列应显示 `top_concepts_for_stock` 的输出（基于 heats 排序选 top 2）
  - 即 fallback 块第 446-451 行（`lu_file = DATA_DIR / "limit_up_cache.json"` → `aggregate_concept_limit_ups`）真的被命中
  - **不**因 `NameError: name 'json' is not defined` 在内层 try 静默退出 → heats 退化为 []
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-3.1 | `limit_up_cache.json` 不存在或解析失败时，内层 `try/except Exception: pass` 维持原静默行为，heats 降级为 []，`top_concepts_for_stock` 在 heats=[] 时返回 c_map 原序前 2 个（既有逻辑） |
| BR-3.2 | 该 fallback 路径依赖 `src.data.concept_fetcher` + `src.engine.concept_stats` 两个外部模块（既有 import，**本 Story 不动**），仅修复 DATA_DIR/json 的 NameError |
| BR-3.3 | 与 dashboard 端 `src/static/index.html` 的概念列渲染**目标**对齐（双层显示 / top_concepts），但**实现路径不同**（dashboard 走 `/api/concept-stats`，此处走磁盘聚合）—— 不要求字符级等价 |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| limit_up_cache.json 不存在 | — | （静默） | heats=[], top_concepts 降级为 c_map 原序前 2 |
| concept_fetcher.load_stock_to_concepts 抛错 | — | （静默） | 外层 try 捕获，top_concepts_map 保持空 dict |

---

### AC4: 不引入回归（scope DoD #4）

**Scenario**
```gherkin
GIVEN 现有 tests/notify/test_email_decision_alignment.py 的 46 个测试用例（email-sync-1.1 已交付，全绿基线）
WHEN 应用本 Story 的 import 修复
THEN
  - `pytest tests/notify/test_email_decision_alignment.py -W error` 仍 46/46 pass
  - 任意现有测试断言、任意现有渲染输出**字符级**保持不变（import 变更纯加法，对既有路径零行为影响）
  - send_screener_report 公开签名零变更
  - SMTP_USER 缺失分支、空 hits 分支、全 None 数据 "数据加载中" 分支行为不变
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-4.1 | 本 Story 是 import 纯加法变更，**不应**触发任何既有测试用例的断言变化；如有任一既有测试 fail，**必须 HALT** 并由 SM/Architect 评估是否撤销修复 |
| BR-4.2 | 不重构 `_build_html` 函数；不优化外层 `try/except Exception: pass`（scope 范围外约束） |
| BR-4.3 | 不改 `industry_cache.json` / `limit_up_cache.json` 文件格式（scope 范围外约束） |
| BR-4.4 | 不改 ranking_data 字段注入来源（scope 范围外约束 — 这是另一 Story） |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| 任一既有 46 测试 fail | — | — | QA 标记为 BLOCKING，回退至 SM revise；触发 BR-4.1 升级条件 |

---

## Tasks / Subtasks

> **说明**：本 Story 走 **quick mode**，跳过 Architect *review 与 QA *test-design 前置流程。
> Dev 直接进入 *quick-develop；QA 在 *quick-verify 阶段一次性出 gate。
> 测试用例由 Dev 自行设计（trivial 1 行 import 修复，新增覆盖 fallback 的测试**不要求**前置 test-design 文档）。

### Implementation Tasks

- [x] **T1: Import 修复** `[AC1]`
  - [x] 在 `src/notify/email_sender.py` 第 19-24 行 import 区域**仅做以下加法**：
    1. 在 `import os` 后追加新行 `import json`（标准库分组，字母序）
    2. 把 `from src.config import now_cn` 改为 `from src.config import DATA_DIR, now_cn`（合并同源，字母序）
  - [x] 运行 `python3 -c "from src.notify.email_sender import _build_html, DATA_DIR, json"` 验证 import 成功
  - [x] 运行 `python3 -m py_compile src/notify/email_sender.py` 验证语法通过
  - [x] 运行 `grep -nE "^import |^from " src/notify/email_sender.py | head -20` 确认 import 区域整洁

- [x] **T2: Fallback 路径覆盖测试** `[AC2, AC3]`
  - [x] 选择测试文件位置（选 **A**）：新建 `tests/notify/test_email_fallback_industry_concept.py`
  - [x] 编写 AC2 用例（`test_ac2_industry_cache_fallback_renders_real_industry`）：mock `DATA_DIR`=tmp_path、写入 `industry_cache.json` 含 `{"600519": "白酒"}` → 调 `_build_html` → 断言 HTML 含 "白酒"
  - [x] 编写 AC3 用例（`test_ac3_limit_up_cache_fallback_renders_top_concepts`）：mock `DATA_DIR` + 写入合法 `limit_up_cache.json` + mock `load_stock_to_concepts` 返回概念 → 断言概念列含 "白酒"
  - [x] 编写 AC2/AC3 降级用例（`test_ac2_industry_cache_missing_degrades_to_dash` + `test_ac3_limit_up_cache_missing_degrades_silently`）：tmp_path 无 cache 文件 → 不抛错 + 列降级为 "-"
  - [x] 额外补 BR-2.2 用例（`test_ac2_ranking_industry_overrides_cache_fallback`）：ranking 已注入 industry 时优先级高于 fallback，cache 值不应出现
  - [x] 关键负断言已用 `monkeypatch.setattr(email_sender, "DATA_DIR", tmp_path)` 实现：修复前同样测试用例必 fail（NameError 吞掉路径），修复后 5/5 全绿

- [x] **T3: 回归保护** `[AC4]`
  - [x] 运行 `pytest tests/notify/test_email_decision_alignment.py -W error` 验证 46/46 仍全绿（实测 46 passed in 0.03s）
  - [x] 运行 `pytest tests/notify/ -W error` 验证整个 notify 测试目录无 warning（实测 51 passed in 0.07s，0 warning）
  - [x] 运行 `python3 -c "import inspect; from src.notify.email_sender import send_screener_report; print(inspect.signature(send_screener_report))"` 与 email-sync-1.1 基线对比，签名零变更（cycle_phase, cycle_day, representative, leader, hits, signals, deviations=None, sentiment_data=None, ranking_data=None）

### AC Coverage Matrix

| Task | AC1 | AC2 | AC3 | AC4 |
|------|:---:|:---:|:---:|:---:|
| T1: Import 修复 | ✓ |   |   |   |
| T2: Fallback 测试 |   | ✓ | ✓ |   |
| T3: 回归保护 |   |   |   | ✓ |

---

## Dev Notes

### Technical Constraints

| 类别 | 约束 | 来源 |
|---|---|---|
| 修复粒度 | **仅** 1 行新增 `import json` + 1 行修改 `from src.config import DATA_DIR, now_cn`；不动函数体 | scope 文件 第 30-37 行 |
| 函数结构 | 不重构 `_build_html`；不优化外层 `try/except Exception: pass`（除非 SM/Architect 评估认为有必要） | scope 范围外约束 |
| 模板引擎 | **不**引入新依赖（既有 f-string 内联）| 沿用 email-sync-1.1 约束 |
| API 契约 | `send_screener_report` 公开签名零变更 | 沿用 email-sync-1.1 约束 |
| Dashboard 端 | **绝对不**修改 `src/static/index.html`（真源约束） | 沿用 email-sync-1.1 约束 |
| 测试设计 | quick mode：Dev 自行设计 fallback 覆盖用例，不前置 QA *test-design | 用户指令 + scope 第 65 行 |

### Accumulated Context (From Previous Stories)

| Resource Type | Name | Source Story | Action | Key Info |
|---|---|---|---|---|
| Code Symbol | `DATA_DIR` | (既有) `src/config.py:38` | REUSE | `BASE_DIR / "data"`，已 export，无需新建 |
| Code Symbol | `json` (stdlib) | (Python 标准库) | REUSE | 标准库直接导入 |
| File | `src/notify/email_sender.py` | email-sync-1.1 (Done) | EXTEND | 仅扩展 import 列表，函数体不动 |
| File | `tests/notify/test_email_decision_alignment.py` | email-sync-1.1 (Done) | REUSE-AS-BASELINE | 46 测试全绿基线，本 Story 不允许任一变红 |
| File | `tests/notify/test_email_fallback_industry_concept.py` | (本 Story) | CREATE (可选 — 见 T2 选项 A) | 仅在 Dev 选 A 时新建 |
| File | `industry_cache.json` / `limit_up_cache.json` | (运行时缓存) `data/` 目录 | REUSE-AS-INPUT | 测试用 tmp_path mock，不动生产文件 |

> 备注：本 Story 是 email-sync-1.1 的衍生 Story（同 brownfield 偏离链路），延续"邮件渲染单一真源 + 不动 dashboard"约定。

### Database Design

N/A — 不涉及数据库变更。

### Data Models

N/A — 仅 import 修复，不动数据结构。

### File Locations

| 文件 | 操作 | 涉及行号（起草时） | 关联 AC |
|---|---|---|---|
| `src/notify/email_sender.py` | **修改 import 区域** | 19-24（import 区域）；419-451（fallback 路径，由 import 修复**间接**激活，不动函数体） | AC1, AC2, AC3 |
| `tests/notify/test_email_fallback_industry_concept.py` | **新建（选项 A）** 或 | — | AC2, AC3 |
| `tests/notify/test_email_decision_alignment.py` | **末尾追加（选项 B）** | 在现有 46 测试之后追加 | AC2, AC3 |
| `src/config.py` | **绝对不动** | 38（DATA_DIR 已 export）| （前置依赖确认） |
| `src/static/index.html` | **绝对不动** | — | （真源约束） |

### Deliverable Bindings

```yaml
deliverable_bindings:
  - deliverable: "src/notify/email_sender.py (modified imports)"
    consumer: "src/notify/email_sender.py:419-451 (fallback path)"
    binding_type: import_usage
    verify: "grep -nE 'DATA_DIR|json\\.' src/notify/email_sender.py 中所有引用都对应模块级 import；python3 -c 'from src.notify.email_sender import _build_html' 不抛错"

  - deliverable: "tests/notify/test_email_fallback_industry_concept.py (or appended cases)"
    consumer: "pytest discovery (project test runner)"
    binding_type: import_usage
    verify: "pytest 收集到 fallback 覆盖的 ≥3 个测试用例（AC2 主路径 + AC3 主路径 + 降级回退）"
```

> 备注：本 Story 不创建新生产代码模块（仅修改 import 列表），新增的测试文件通过 pytest 自动发现机制绑定到 CI 流程。

### Testing Requirements

- **测试设计层级**：`skip`（quick mode，用户指令明确跳过 QA *test-design 前置流程）
- **Dev 自行设计的覆盖重点**：
  1. AC2/AC3 主路径：mock DATA_DIR + 写入合法 cache → 渲染 HTML 含真实板块/概念名（验证 import 修复**实际**激活了 fallback）
  2. BR-2.1 / BR-3.1 降级路径：cache 文件不存在 → 不抛错 + 降级为 "-"（验证既有 `try/except Exception: pass` 行为不变）
  3. AC4 回归基线：现有 46 测试 100% 通过 + 模块 import 不抛错 + send_screener_report 签名不变
- **覆盖最低要求**：≥3 测试用例（主路径 ×2 + 降级 ×1）

### Out-of-Scope（明确不做，沿用 scope 第 56-60 行）

- 不重构 `_build_html` 函数结构
- 不改 `industry_cache.json` / `limit_up_cache.json` 文件格式
- 不改 ranking_data 来源（这是另一个问题）
- 不优化外层 `try/except Exception: pass`（除非 SM/Architect 评估认为有必要）
- 不改 dashboard JS（dashboard 是真源）

---

## Brownfield Deviations from Standard Workflow

> 沿用 email-sync-1.1 已与用户确认的 8 条偏差，本 Story 全部继承：

1. **跳过 Epic YAML 分片**：scope 文件 `docs/prd/iteration-data-dir-import-fix-scope.md` 作为虚拟 epic 真源（无 `docs/prd/epic-*.yaml`）
2. **跳过 architecture 分片加载**：项目无 `docs/architecture/*coding-standards.md` / `tech-stack.md` / `source-tree.md` 等架构文档
3. **跳过 cumulative context 校验**：项目无 database/api/models registry 文件
4. **跳过 user journey precondition traceability**：本 Story 是后台 import 修复，无用户流
5. **跳过 frontend spec UI/UX 引用**：本 Story 无 UI 变更（邮件 HTML 由 fallback 间接受影响，但渲染逻辑 0 变更）
6. **跳过 Architect *review**：用户指令 + scope 第 65 行明确 quick mode；trivial 1 行 import 修复，无架构决策点
7. **跳过 QA *test-design 前置流程**：quick mode；Dev 自行设计 fallback 覆盖用例
8. **状态直接跳到 Approved**：跳过 AwaitingArchReview / AwaitingTestDesign / TestDesignComplete 中间态

---

## Change Log

| Date | Agent | Status Transition | Details/Link |
|------|-------|-------------------|--------------|
| 2026-05-08 | SM | (new) → Approved | Brownfield 单 Story 起草（quick mode）；衍生自 email-sync-1.1 QA review observation；root cause analysis 修正 scope 归因（首次引入是 `45baa67`，非 `bbe8c16`）；scope 文件 [docs/prd/iteration-data-dir-import-fix-scope.md](../prd/iteration-data-dir-import-fix-scope.md) 作为真源；偏离标准流程 8 条沿用 email-sync-1.1 已确认偏差；test_design_level=skip（用户指令） |
| 2026-05-08 | Dev | Approved → InProgress | Quick development started（Linus / Opus 4.7 1M） |
| 2026-05-08 | Dev | InProgress → Review | T1 import 修复（`import json` + `from src.config import DATA_DIR, now_cn`）+ T2 新增 5 个 fallback 测试（test_email_fallback_industry_concept.py）+ T3 回归 51/51 全绿（46 baseline + 5 new，-W error 0 warning）+ send_screener_report 签名零变更 |
| 2026-05-08 | QA | Review → Done | Quick-verify 通过：AC1 静态核验（py_compile + 模块 import smoke）+ AC2/AC3/AC4 联合测试 51/51 PASSED -W error 0 warning + send_screener_report 签名字符级保持。Gate=PASS（LOW risk, automated_only），见 [docs/qa/gates/data-dir-import-fix-1.1-email-sender-fallback-import-fix.yml](../qa/gates/data-dir-import-fix-1.1-email-sender-fallback-import-fix.yml) |

---

## Quick Record

| Field | Value |
|-------|-------|
| Dev | Linus (Claude Opus 4.7, 1M context) |
| Files | `src/notify/email_sender.py` (imports +2 lines), `tests/notify/test_email_fallback_industry_concept.py` (new, 5 tests) |
| Tests | Pass (51/51 in `tests/notify/`, `-W error` clean) |
| QA | Verified 2026-05-08 by Turing (gate=PASS, LOW risk, automated_only) |
| Commit | 8ab7abf (Dev) + (pending QA finalize-commit) |

---

## AC Traceability Matrix

> **Dev**: Fill this section BEFORE self-review. Every AC needs evidence.
> **QA**: Verify each entry during *quick-verify.

### AC1: Import 完整性

```yaml
ac_id: AC1
code_locations:
  - "src/notify/email_sender.py:19-25 (实施后行号: import json @ L19, from src.config import DATA_DIR, now_cn @ L25)"
test_locations:
  - "python3 -c 'from src.notify.email_sender import _build_html, DATA_DIR, json' → 通过"
  - "python3 -m py_compile src/notify/email_sender.py → 通过"
  - "tests/notify/test_email_fallback_industry_concept.py: 模块顶部 from src.notify.email_sender import _build_html 隐式覆盖 import 完整性"
verification_type: static_check
aspects_covered:
  main_scenario: verified
  business_rules: verified
  data_validation: verified
  error_handling: verified
notes: "BR-1.1 仅追加（diff 显示 +import json / 改 1 行 from src.config）+ BR-1.2 合并同源（已合并为 DATA_DIR, now_cn）+ BR-1.3 字母序（json < os, DATA_DIR < now_cn）"
```

### AC2: Fallback 路径走通（industry_cache.json）

```yaml
ac_id: AC2
code_locations:
  - "src/notify/email_sender.py:420-425 (industry_map 兜底块；行号在 import 加 1 行后下移)"
test_locations:
  - "tests/notify/test_email_fallback_industry_concept.py::test_ac2_industry_cache_fallback_renders_real_industry (主路径)"
  - "tests/notify/test_email_fallback_industry_concept.py::test_ac2_industry_cache_missing_degrades_to_dash (BR-2.1 降级)"
  - "tests/notify/test_email_fallback_industry_concept.py::test_ac2_ranking_industry_overrides_cache_fallback (BR-2.2 ranking 优先)"
verification_type: unit_test
aspects_covered:
  main_scenario: verified
  business_rules: verified
  data_validation: verified
  error_handling: verified
notes: "BR-2.1 文件缺失降级 → '-' 占位（test_ac2_industry_cache_missing_degrades_to_dash 断言 '>-</span>' 出现）; BR-2.2 ranking 优先（test_ac2_ranking_industry_overrides_cache_fallback 断言 '食品饮料' 覆盖 '白酒'）; BR-2.3 文件格式不动（测试用例使用 {code: industry_name} 既有约定）"
```

### AC3: Fallback 路径走通（limit_up_cache.json → top_concepts）

```yaml
ac_id: AC3
code_locations:
  - "src/notify/email_sender.py:439-462 (top_concepts_map 兜底块；内层 447-454 的 limit_up cache 由 import 修复间接激活；行号在 import 加 1 行后下移)"
test_locations:
  - "tests/notify/test_email_fallback_industry_concept.py::test_ac3_limit_up_cache_fallback_renders_top_concepts (主路径)"
  - "tests/notify/test_email_fallback_industry_concept.py::test_ac3_limit_up_cache_missing_degrades_silently (BR-3.1 降级)"
verification_type: unit_test
aspects_covered:
  main_scenario: verified
  business_rules: verified
  data_validation: verified
  error_handling: verified
notes: "BR-3.1 heats 降级（test_ac3_limit_up_cache_missing_degrades_silently 验证: 无 cache → 不抛错 + '白酒' 不出现 + 概念位降级 '-'; 实际行为是 top_concepts_for_stock 在 heats=[] 时 rank_map 为空返回 [], 与 Story 起草时假设的"返回 c_map 原序前 2"略有出入但更安全, 已校准 BR-3.1 实现行为）; BR-3.2 不动 concept_fetcher（测试只 monkeypatch load_stock_to_concepts, 未改源码）; BR-3.3 与 dashboard 路径不同但目标一致"
```

### AC4: 不引入回归

```yaml
ac_id: AC4
code_locations:
  - "(zero modification to function bodies; only import additions @ L19 + L25)"
test_locations:
  - "tests/notify/test_email_decision_alignment.py (46 baseline tests, 实测 46 passed in 0.03s with -W error)"
  - "tests/notify/ 整体目录 (51 passed in 0.07s with -W error, 0 warning)"
  - "inspect.signature(send_screener_report) 与 email-sync-1.1 基线字符级一致"
verification_type: regression_baseline
aspects_covered:
  main_scenario: verified
  business_rules: verified
  data_validation: verified
  error_handling: verified
notes: "BR-4.1 全 46 绿，无 BLOCKING; BR-4.2 _build_html 函数体未改; BR-4.3 cache 文件格式未改; BR-4.4 ranking_data 注入路径未改"
```

---

### Traceability Summary

| AC | Code Location | Test Location | Type | Status |
|----|---------------|---------------|------|--------|
| AC1 | `src/notify/email_sender.py:19-25` | py_compile + import smoke | static check | ✅ |
| AC2 | `src/notify/email_sender.py:420-425` | `test_email_fallback_industry_concept.py` (3 tests: main + degrade + ranking-overrides) | unit | ✅ |
| AC3 | `src/notify/email_sender.py:439-462` | `test_email_fallback_industry_concept.py` (2 tests: main + degrade) | unit | ✅ |
| AC4 | (zero diff to bodies) | `test_email_decision_alignment.py` 46/46 + signature parity | regression | ✅ |

**Legend**: ✅ Verified | ⏳ Pending | ❌ Missing

**Coverage Achieved**: 5 new tests (AC2 main + AC2 degrade + AC2 BR-2.2 + AC3 main + AC3 degrade) + 46 existing tests = 51 passed, 0 warning, send_screener_report signature 字符级保持。

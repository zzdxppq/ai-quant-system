# Story 11.1: 手动录入开仓（Position 创建 API）

> **SM**: 萧何 | **创建日期**: 2026-06-03 | **状态**: AwaitingArchReview

## Story

```yaml
Story:
  id: position-create-11.1
  title: 手动录入开仓（Position 创建 API + 持仓存储）
  epic: Epic 11 - 持仓管理 + 实时盈亏
  tier: standard
  status: AwaitingArchReview
  mode: full
  repository: monolith
  priority: P0
  estimated_complexity: medium
  story_type: greenfield-in-monolith
```

**作为** A 股短线交易者，
**我希望** 在看到 9:27 强信号后，能通过 API 录入实际买入的开仓（代码、买入价、股数、关联信号），
**以便** 系统跟踪这笔持仓的盈亏、信号对照、止盈止损。

---

## 验收标准

### AC1: 成功录入一笔开仓

**场景**
```gherkin
假设 用户当日已收到 strong 信号（标的 600123，价格 10.50）
当 用户调用 POST /api/positions {code, buy_price, shares, signal_source, planned_position_layers}
那么
  - 创建 Position 记录，status=open
  - 记录关联当日 screener run 的 signal_source
  - 返回 201 + 完整 Position 对象
```

**业务规则**
| ID | 规则 |
|----|------|
| BR-1.1 | 同一 (code, trade_date) 只能有一笔 open 持仓（防重复录入） |
| BR-1.2 | buy_price > 0，shares > 0 且为 100 的整数倍（A 股交易单位） |
| BR-1.3 | signal_source 可空（允许纯手动开仓），但若非空需存在于 screener_history |

**数据验证**
| 字段 | 类型 | 必填 | 规则 | 错误信息 |
|------|------|------|------|----------|
| code | string | 是 | 6 位数字 A 股代码 | 代码格式错误 |
| buy_price | number | 是 | > 0, 精度 0.01 | 买入价必须 > 0 |
| shares | number | 是 | >= 100, 100 的倍数 | 股数必须 ≥ 100 且为 100 倍数 |
| planned_position_layers | number | 是 | 1-9 整数 | 计划仓位层数须在 1-9 之间 |
| signal_source | string | 否 | 可空; 非空时需存在于 screener_history | signal_source 无效 |

**错误处理**
| 场景 | 错误码 | 信息 | 处理方式 |
|------|--------|------|----------|
| 同一 code 当日已有 open 持仓 | 409 | 该代码今日已有未平仓记录 | 返回现有持仓，提示平仓后再开新仓 |
| signal_source 不在 screener_history | 400 | signal_source 无效 | 允许继续（用户可能事后补录）但标记 signal_mismatch=true |
| trade_date 落在非交易日 | 400 | trade_date 非交易日 | 拒绝（节假日不开仓） |
| shares < 100 | 422 | 股数必须 ≥ 100 | 字段级验证 |
| 行情接口失败无法补 name | 200 | name 字段为 null | price_stale=true; name 由 refresh_pnl 任务补齐 |

**示例**
- **输入**:
  ```json
  POST /api/positions
  {"code":"600123","buy_price":10.50,"shares":1000,
   "signal_source":"screener_2026-06-03_927","planned_position_layers":6}
  ```
- **预期**:
  ```json
  201 Created
  {"id":42,"code":"600123","name":"XX 科技","status":"open",
   "buy_price":10.50,"shares":1000,"remaining_shares":1000,
   "planned_position_layers":6,"current_price":10.55,
   "current_pnl":50.0,"current_pnl_pct":0.476,
   "alert_level":"none","price_stale":false,
   "created_at":"2026-06-03T09:35:12+08:00"}
  ```
- **输入**: 重复开仓
  ```json
  POST /api/positions
  {"code":"600123","buy_price":10.50,"shares":1000}  # 当日已有 open
  ```
- **预期**:
  ```json
  409 Conflict
  {"error":"该代码今日已有未平仓记录","existing_position_id":38}
  ```

---

### AC2: 录入后立即计算成本与初始盈亏

**场景**
```gherkin
假设 成功创建 Position 后
当 系统从最新行情快照读取当前价
那么
  - Position 包含 current_price, current_pnl, current_pnl_pct
  - 若行情不可用则标记 price_stale=true（不抛错）
```

**业务规则**
| ID | 规则 |
|----|------|
| BR-2.1 | 盈亏 = (current_price - buy_price) * shares |
| BR-2.2 | 盈亏% = (current_price / buy_price - 1) * 100 |
| BR-2.3 | 初始 current_price 默认取当日 open（若开盘后录入则取最新价） |
| BR-2.4 | 行情失败时 price_stale=true，但 Position 仍创建成功 |

**错误处理**
| 场景 | 错误码 | 信息 | 处理方式 |
|------|--------|------|----------|
| 当前价不可用（停牌 / 数据延迟） | 200 | 持仓已创建，但价格未更新 | price_stale=true; 下次 refresh_pnl 任务补齐 |

---

## 任务 / 子任务

## 基础设施任务 (共享)

- [ ] **T0: 数据库 & 基础配置** `[ALL ACs]`
  - [ ] 创建 migration: `data/migrations/20260603_create_position_tables.sql`
    - `position` 表 + `position_seq` 序列
    - `position_history` 表 + `position_history_seq` 序列
    - 索引: `idx_position_status`, `idx_position_code`, `idx_position_date`
  - [ ] `src/data/models.py` 追加 `Position` / `PositionHistory` dataclass
  - [ ] `src/data/position_store.py` 新建（CREATE）
    - `create(position: Position) -> Position`
    - `get_by_code_today(code, trade_date) -> Position | None`
    - `update_pnl(id, current_price, current_pnl, current_pnl_pct) -> None`
    - `mark_price_stale(id) -> None`
  - [ ] `src/config.py` 追加 `POSITION_CONFIG` 块 + `POSITION_MODULE_ENABLED` 默认 "0"
  - [ ] 验证 migration 幂等（`CREATE TABLE IF NOT EXISTS`）

## 功能实现任务

### AC1: 成功录入开仓

- [ ] **T1: 实现 POST /api/positions 路由** `[AC1]`
  - [ ] 编写单元测试: `tests/api/test_positions_create.py::test_create_open_position`
    - 覆盖 BR-1.1 (code+date 唯一), BR-1.2 (股数 100 倍数), BR-1.3 (signal_source 校验)
  - [ ] `src/api/app.py` 追加 `POST /api/positions`
    - 参数验证: code 6 位 / buy_price > 0 / shares % 100 == 0
    - 业务规则: 查 (code, trade_date) 是否已有 open → 409
    - 调 `position_store.create()` 持久化
    - 同步调 `sina_spot_api.get_spot_one(code)` 补 name + current_price
    - 失败时 price_stale=true, name=null
  - [ ] 实现 `src/engine/position.py::PositionEngine.create_position()`
  - [ ] 验证测试通过

- [ ] **T2: 实现行情快照补齐** `[AC1, AC2]`
  - [ ] 编写单元测试: `tests/engine/test_position.py::test_fill_market_data`
  - [ ] 复用 `src/data/sina_spot_api.py::get_spot_one()` 拉取
  - [ ] 计算 current_pnl / current_pnl_pct
  - [ ] 调 `position_store.update_pnl()` 落库
  - [ ] 验证测试通过

### AC2: 初始盈亏

- [ ] **T3: 处理 price_stale 状态** `[AC2]`
  - [ ] 编写测试: 模拟 sina_spot_api 抛异常 → 验证 Position 仍创建 + price_stale=true
  - [ ] 不抛错, 仅 `logger.warning` + 标记 stale

## 集成 & 验证任务

- [ ] **T4: 集成测试** `[ALL ACs]`
  - [ ] `tests/integration/test_position_workflow.py`
    - 完整流程: 创建 → 行情拉取 → 盈亏计算 → DB 持久化
  - [ ] 边界: 重复开仓 409 / 非交易日 400 / shares=50 422
  - [ ] Mock 模式: `MOCK=1` 时应正常工作（sina 接口走 mock）

- [ ] **T5: 最终验证** `[ALL ACs]`
  - [ ] 单元测试 + 集成测试全通过
  - [ ] `POSITION_MODULE_ENABLED=0` 时路由 503（模块关闭）
  - [ ] `POSITION_MODULE_ENABLED=1` 时路由正常
  - [ ] 无 lint 错误
  - [ ] 状态 → Review

## AC 覆盖矩阵

| 任务 | AC1 | AC2 |
|------|:---:|:---:|
| T0: 基础设施 | ✓ | ✓ |
| T1: POST 路由 | ✓ | |
| T2: 行情补齐 | ✓ | ✓ |
| T3: price_stale | | ✓ |
| T4: 集成 | ✓ | ✓ |
| T5: 最终 | ✓ | ✓ |

---

## 开发说明

### 技术约束

- **强制** 使用 `now_cn()`（TZ_CN）作为时间字段；不依赖 `datetime.now()`（避免时区错位）
- **强制** 使用项目 DuckDB 文档表模式（`ledger_doc_store` 抽象），不新建 SQLite/JSON 文件
- **强制** 模块默认关闭（`POSITION_MODULE_ENABLED=0`），未启用时路由返回 503
- 性能目标: 单次 POST < 500ms（含行情拉取）
- 严禁自动平仓——仅做记录与提醒

### 累积上下文 (来自之前 Story)

> 本 Epic 11 是新建模块，无前置 Story 依赖。以下是项目级复用资源：

| 资源类型 | 名称 | 来源 | 操作 | 关键信息 |
|----------|------|------|------|----------|
| 数据 API | `sina_spot_api.get_spot_one(code)` | v1.2 | REUSE | 单只股票实时快照 |
| ORM 模式 | `Position` dataclass | 新建 | CREATE | 字段见 PRD §6 Epic 11.1 |
| 存储抽象 | `ledger_doc_store.save(doc, table)` | v1.2 | REUSE | 文档表 JSON 落库 |
| 时间工具 | `now_cn()` / `is_trading_day()` | `src/config.py` | REUSE | TZ_CN 强制 |
| 调度模式 | `is_trading_time()` 判断 | v1.2 scheduler | REUSE | 9:30-11:30 / 13:00-15:00 |
| 错误响应格式 | `{error, code, ...}` | v1.2 | REUSE | 与现有 /api/* 路由一致 |

### 数据库设计

详见 [Source: docs/architecture/position-module.md §3]

新表 `position` / `position_history` 均为 DuckDB 文档表（JSON 字段 + 提取列）。`Position` dataclass 中所有字段都序列化到 `doc` JSON，提取常用查询字段（code/status/trade_date/updated_at）到独立列以支持 O(1) 索引。

迁移文件：`data/migrations/20260603_create_position_tables.sql`（幂等）

### 数据同步要求

- [x] **写操作清单**:
  - `position` 表: INSERT (create_position), UPDATE (update_pnl, mark_price_stale)
  - 无跨表同步需求（本 Story 范围内）
- [x] **状态/过期字段检查**: `status` / `updated_at` 已在 model 中
- [x] **关联字段同步分析**: 无（Position 是独立表，不与其他 v1.2 表级联）
- [x] 无未覆盖的同步需求

### 数据模型

详见 [Source: docs/architecture/position-module.md §3.1-3.2]

`Position` dataclass 关键字段：
- `id` (PK, auto increment)
- `code` / `trade_date` / `status`（核心查询字段）
- `buy_price` / `shares` / `remaining_shares`（持仓核心）
- `signal_source` / `signal_level` / `cycle_phase`（信号快照）
- `current_price` / `current_pnl` / `current_pnl_pct`（实时字段，由 T2/T3 写入）
- `alert_level` / `price_stale`（提醒状态）

### 文件位置

**新建**:
- `src/engine/position.py`（~80 行，AC1 范围）
- `src/data/position_store.py`（~100 行）
- `data/migrations/20260603_create_position_tables.sql`（~40 行）
- `tests/api/test_positions_create.py`（~150 行）
- `tests/engine/test_position.py`（~80 行）

**修改**:
- `src/data/models.py`（追加 `Position` / `PositionHistory`）
- `src/api/app.py`（追加 POST /api/positions 路由 + 503 守卫）
- `src/config.py`（追加 `POSITION_CONFIG` + `POSITION_MODULE_ENABLED`）

### 交付物绑定

```yaml
deliverable_bindings:
  - deliverable: "src/engine/position.py::PositionEngine.create_position"
    consumer: "src/api/app.py::POST /api/positions"
    binding_type: import_usage
    verify: "from src.engine.position import.*[Pp]osition"

  - deliverable: "src/data/position_store.py::create"
    consumer: "src/engine/position.py"
    binding_type: import_usage
    verify: "from src.data.position_store import"

  - deliverable: "src/data/sina_spot_api.py::get_spot_one"
    consumer: "src/engine/position.py::PositionEngine.create_position"
    binding_type: import_usage
    verify: "sina_spot_api\\.get_spot_one"

  - deliverable: "data/migrations/20260603_create_position_tables.sql"
    consumer: "main.py (启动时自动跑迁移)"
    binding_type: schema_applied
    verify: "ledger_doc_store.*position"

  - deliverable: "POST /api/positions"
    consumer: "src/static/index.html (持仓 Tab 录入按钮 - Story 11.4)"
    binding_type: route_mount
    verify: "@app\\.post\\(.api/positions.\\)"
```

**绑定状态**: ⏳ 待 Dev 验证

### 测试要求

- **单元测试**:
  - `test_create_open_position_happy_path` (AC1)
  - `test_duplicate_position_returns_409` (BR-1.1)
  - `test_shares_must_be_100_multiple` (BR-1.2)
  - `test_signal_source_validation` (BR-1.3)
  - `test_non_trading_date_rejected` (节假日守卫)
  - `test_price_stale_on_market_data_failure` (AC2)
  - `test_module_disabled_returns_503` (NFR10)
- **集成测试**:
  - 完整流程: POST → DB → GET 列表能看到
  - Mock 模式: `MOCK=1` 环境变量下行情拉取走 mock
- **手动验证**:
  - 用 `curl` 在本地启服务后调用 POST 端点
  - DuckDB 客户端查 `position` 表确认数据落库

---

## 变更日志

| 日期 | Agent | 状态转换 | 详情/链接 |
|------|-------|----------|-----------|
| 2026-06-03 | SM (萧何) | Created → AwaitingArchReview | Story 创建，基于 [PRD §6 Epic 11.1](docs/PRD.md#epic-11) + [架构文档 §3-4](docs/architecture/position-module.md) |

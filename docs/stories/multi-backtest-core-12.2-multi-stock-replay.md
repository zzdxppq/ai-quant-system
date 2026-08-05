# Story 12.2: 多标的逐日回放（核心回测引擎）

> **SM**: 萧何 | **创建日期**: 2026-06-03 | **状态**: AwaitingArchReview

## Story

```yaml
Story:
  id: multi-backtest-core-12.2
  title: 多标的逐日回放（MultiBacktestEngine.run 核心算法）
  epic: Epic 12 - 多标的回测扩展
  tier: comprehensive
  status: AwaitingArchReview
  mode: full
  repository: monolith
  priority: P0
  estimated_complexity: high
  story_type: greenfield-module
```

**作为** A 股短线交易者（系统使用者），
**我希望** 在 `screener_history` 完整的前提下，触发多标回测并得到真实胜率/盈亏比/分组统计，
**以便** 验证"严格按系统信号开仓"的真实历史表现。

---

## 验收标准

### AC1: 完整回放 60 天所有历史命中标的

**场景**
```gherkin
假设 screener_history 完整（142 条记录，每条 1-5 只标的）
当 调用 POST /api/backtest/multi-stock {window_days: 60, initial_capital: 1000000}
那么
  - 逐日模拟：当日 screener 命中 → 次日开盘按信号开仓 → 持有至止盈/止损/3 天
  - 输出：总交易数、胜率、盈亏比、平均盈利%、平均亏损%、最大回撤、最终权益
```

**业务规则**
| ID | 规则 |
|----|------|
| BR-1.1 | 开仓价 = 次日 open（来自 `backfill_next_day_auction` 或日 K） |
| BR-1.2 | 平仓优先级：止盈（>+10%）> 止损（<-5%）> 持有满 3 天强制平仓 |
| BR-1.3 | 仓位公式：`position_size = capital * (planned_position_layers / 30)` — 6 层 = 20% 资金 |
| BR-1.4 | 同一天最多持仓 5 只（满仓后新信号跳过） |
| BR-1.5 | 同一 code 不重复开仓（已有持仓未平仓时跳过） |
| BR-1.6 | 资金 < 1 手（100 股）所需时跳过该信号 |
| BR-1.7 | 回测窗口：默认 60 交易日，可参数化（30-120） |
| BR-1.8 | 回测结束强制平仓剩余持仓（避免遗留"假持仓"） |

**数据验证**
| 字段 | 类型 | 必填 | 规则 | 错误信息 |
|------|------|------|------|----------|
| window_days | number | 否 | 30-120 整数，默认 60 | 窗口需在 30-120 之间 |
| initial_capital | number | 否 | > 0, 默认 1,000,000 | 初始资金必须 > 0 |

**错误处理**
| 场景 | 错误码 | 信息 | 处理方式 |
|------|--------|------|----------|
| 数据缺失 > 10% | 412 | 数据缺失过多，请先执行补缺 | 返回缺失明细, 不执行回测 |
| 回测超时（> 60s） | 408 | 回测超时，建议缩短时间窗口 | 返回部分结果 + 进度百分比 |
| window_days 越界 | 422 | 窗口需在 30-120 之间 | 字段级验证 |
| screener_history 为空 | 422 | 历史选股数据为空 | 提示先运行至少一次选股 |

**示例**
- **输入**:
  ```json
  POST /api/backtest/multi-stock
  {"window_days": 60, "initial_capital": 1000000}
  ```
- **预期**:
  ```json
  200 OK
  {
    "id": 7,
    "run_at": "2026-06-03T19:40:00+08:00",
    "duration_seconds": 23.4,
    "summary": {
      "total_trades": 87,
      "win_count": 49,
      "loss_count": 38,
      "win_rate": 0.563,
      "profit_factor": 1.42,
      "avg_pnl_pct": 0.024,
      "avg_win_pct": 0.082,
      "avg_loss_pct": -0.043,
      "max_consecutive_wins": 5,
      "max_consecutive_losses": 3,
      "max_drawdown": -0.082,
      "final_equity": 1187200,
      "total_return_pct": 0.187
    },
    "by_cycle_phase": {
      "完整周期": {"trades": 42, "win_rate": 0.62, "profit_factor": 1.85},
      "小周期完成": {"trades": 28, "win_rate": 0.54, "profit_factor": 1.32},
      "退潮期": {"trades": 5, "win_rate": null, "note": "样本不足"}
    },
    "by_signal_level": {
      "strong": {"trades": 35, "win_rate": 0.66},
      "normal": {"trades": 40, "win_rate": 0.55},
      "watch": {"trades": 12, "win_rate": 0.42}
    },
    "by_continuous_limit_up": {
      "2": {"trades": 30, "win_rate": 0.50},
      "3": {"trades": 28, "win_rate": 0.61},
      "4": {"trades": 18, "win_rate": 0.67},
      "5+": {"trades": 11, "win_rate": 0.73}
    },
    "skipped_count": 3,
    "trades_csv_url": "/api/backtest/multi-stock/export?id=7"
  }
  ```

---

## 任务 / 子任务

## 基础设施任务 (共享)

- [ ] **T0: 数据库 & 基础配置** `[ALL ACs]`
  - [ ] 创建 migration: `data/migrations/20260603_create_multi_backtest_tables.sql`
    - `multi_backtest_result` 表 + `multi_backtest_seq` 序列
  - [ ] `src/data/models.py` 追加 `MultiBacktestResult` / `Trade` / `GroupStats` dataclass
  - [ ] `src/data/multi_backtest_store.py` 新建（CREATE）
    - `save_result(result) -> result_id`
    - `get_latest_result() -> MultiBacktestResult | None`
  - [ ] `src/config.py` 追加 `BACKTEST_CONFIG` 块 + `MULTI_BACKTEST_ENABLED` 默认 "0"
  - [ ] 验证 migration 幂等

## 功能实现任务

### AC1: 核心回测

- [ ] **T1: 实现信号加载 (`_load_signals`)** `[AC1]`
  - [ ] 编写单元测试: `tests/engine/test_multi_backtest.py::test_load_signals`
  - [ ] `src/engine/multi_backtest.py::MultiBacktestEngine._load_signals(window_days)`
    - 从 `screener_history_entry` DuckDB 表查最近 N 天
    - 检查 `next_day_open` / `close_price` 完整性
    - 返回 `list[SignalRecord]`
  - [ ] 缺数据时自动调 `repair_missing_close_prices` 补缺

- [ ] **T2: 实现开仓逻辑 (`_open_position`)** `[AC1]`
  - [ ] 编写测试: `test_open_position_*` 覆盖 BR-1.3~1.6
  - [ ] `_open_position(signal, capital, open_positions)`
    - 校验：满仓（>= 5）跳过 / 同 code 已持仓跳过 / 资金不足跳过
    - 计算 `position_size = capital * (layers / 30)`
    - `shares = (position_size / next_day_open_price) // 100 * 100`
    - 从 capital 扣除 buy_price * shares
  - [ ] 记录 trade 信号快照

- [ ] **T3: 实现平仓逻辑 (`_close_position`)** `[AC1]`
  - [ ] 编写测试: `test_close_position_take_profit` / `test_stop_loss` / `test_max_hold`
  - [ ] `_close_position(pos, close_price, date, exit_reason)`
    - 按优先级: 止盈 > 止损 > max_hold
    - 计算 `pnl` / `pnl_pct` / `hold_days`
    - 生成 `Trade` 对象
  - [ ] 资金回流 `capital += close_price * shares`

- [ ] **T4: 实现主循环 (`run`)** `[AC1]`
  - [ ] 编写测试: `test_run_full_window_60_days` (用 mock screener_history)
  - [ ] `MultiBacktestEngine.run(window_days, initial_capital)`
    - 遍历交易日历
    - 每日：先平仓 → 再开仓
    - 强制平仓：回测结束时 `final_close`
  - [ ] 性能预算: 60 天 < 30s（不含数据拉取）

- [ ] **T5: 实现统计输出 (`_build_result`)** `[AC1]`
  - [ ] 编写测试: `test_grouping_by_*` 覆盖分组统计
  - [ ] `_build_result(trades, capital, skipped)` → `MultiBacktestResult`
    - 总体统计: win_rate, profit_factor, max_drawdown, equity curve
    - 分组: by_cycle_phase / by_signal_level / by_continuous_limit_up
    - 样本 < 5 标 `win_rate=null` + `note="样本不足"`
  - [ ] 持久化到 `multi_backtest_result` 表

- [ ] **T6: 实现 POST /api/backtest/multi-stock 路由** `[AC1]`
  - [ ] 编写测试: `tests/api/test_multi_backtest_run.py`
  - [ ] `src/api/app.py` 追加路由
    - 调 `engine.run()` 同步执行
    - 超时保护: 60s 后返回 408 + 已完成部分
    - 模块关闭时返回 503
  - [ ] 验证测试通过

## 集成 & 验证任务

- [ ] **T7: 集成测试** `[ALL ACs]`
  - [ ] `tests/integration/test_multi_backtest_workflow.py`
    - 端到端: 准备数据 → 跑回测 → 查结果
  - [ ] 边界: 数据缺失 → 412 / 超时 → 408
  - [ ] 对比 v1.0 `backtest.py` 跑同窗口，确认 v2.0 性能合理

- [ ] **T8: 性能验证** `[ALL ACs]`
  - [ ] 60 天 × 5 只/日 < 30s
  - [ ] 90 天 × 5 只/日 < 60s
  - [ ] 120 天 × 5 只/日 < 90s

- [ ] **T9: 最终验证** `[ALL ACs]`
  - [ ] 单元 + 集成 + 性能测试全通过
  - [ ] `MULTI_BACKTEST_ENABLED=0` 时路由 503
  - [ ] `MULTI_BACKTEST_ENABLED=1` 时路由正常
  - [ ] 无 lint 错误
  - [ ] 状态 → Review

## AC 覆盖矩阵

| 任务 | AC1 |
|------|:---:|
| T0: 基础设施 | ✓ |
| T1: 信号加载 | ✓ |
| T2: 开仓 | ✓ |
| T3: 平仓 | ✓ |
| T4: 主循环 | ✓ |
| T5: 统计 | ✓ |
| T6: 路由 | ✓ |
| T7: 集成 | ✓ |
| T8: 性能 | ✓ |
| T9: 最终 | ✓ |

---

## 开发说明

### 技术约束

- **强制** 复用 v1.0 `backtest.py` 的止盈/止损/持有天数配置（共享 `BACKTEST_CONFIG`）
- **强制** 与 v1.0 回测**并行**而非替换——`src/engine/multi_backtest.py` 是新文件
- **强制** 模块默认关闭（`MULTI_BACKTEST_ENABLED=0`），未启用时路由 503
- 性能预算: 60 天 60s 内（PRD NFR4 衍生）
- 仓位公式固定（6 层 = 20% 资金），不实现凯利公式（避免过拟合陷阱）
- 严禁反向优化/未来函数——所有"已知信息"必须来自 screener_history 归档

### 累积上下文 (来自之前 Story)

| 资源类型 | 名称 | 来源 | 操作 | 关键信息 |
|----------|------|------|------|----------|
| 引擎参考 | `src/engine/backtest.py` (429 行) | v1.0 | REUSE | dataclass + 逐日循环风格 |
| 选股归档 | `screener_history_entry` DuckDB 表 | v1.0 | REUSE | 字段: code, name, trade_date, signal_level, cycle_phase, continuous_limit_up, next_day_open, close_price |
| 补缺链路 | `repair_missing_close_prices()` | v1.0 | REUSE | 幂等按日 K 补 close |
| 交易日历 | `_build_trade_calendar` | v1.0 backtest | REUSE | 60 天交易日历 |
| 配置块 | `BACKTEST_CONFIG` | v1.0 | EXTEND | 复用 take_profit/stop_loss/max_hold_days |
| 日 K 接口 | `sina_kline_api.get_daily_close()` | v1.0 | REUSE | 单只单日 |
| 文档表模式 | `multi_backtest_result` | 新建 | CREATE | 详见 [架构文档 §3.3](docs/architecture/multi-backtest-module.md) |

### 数据库设计

详见 [Source: docs/architecture/multi-backtest-module.md §3.3]

新表 `multi_backtest_result` 文档表，主键 + run_at 索引。`MultiBacktestResult.doc` 包含完整 trades 数组（导出 CSV 用）+ 嵌套分组统计。

### 数据同步要求

- [x] **写操作清单**:
  - `multi_backtest_result` 表: INSERT (save_result)
  - 无跨表同步（独立表）
- [x] **状态/过期字段**: `run_at` 时间戳由 `now_cn()` 生成
- [x] **关联字段同步**: 无
- [x] 无未覆盖同步需求

### 数据模型

详见 [Source: docs/architecture/multi-backtest-module.md §3.1-3.2]

- `MultiBacktestResult`: 含 summary 字段 + 3 个分组 stats + trades 列表
- `Trade`: 单笔交易完整明细（开仓日/买入日/平仓日/价格/股数/盈亏/exit_reason）
- `GroupStats`: 分组统计（trades/win_rate/profit_factor/sample_size）

### 文件位置

**新建**:
- `src/engine/multi_backtest.py`（~350 行，核心）
- `src/data/multi_backtest_store.py`（~120 行）
- `data/migrations/20260603_create_multi_backtest_tables.sql`（~25 行）
- `tests/engine/test_multi_backtest.py`（~250 行）
- `tests/api/test_multi_backtest_run.py`（~100 行）
- `tests/integration/test_multi_backtest_workflow.py`（~120 行）

**修改**:
- `src/data/models.py`（追加 `MultiBacktestResult` / `Trade` / `GroupStats`）
- `src/api/app.py`（追加 POST 路由 + 503 守卫）
- `src/config.py`（追加 `BACKTEST_CONFIG` 块 + `MULTI_BACKTEST_ENABLED`）

### 交付物绑定

```yaml
deliverable_bindings:
  - deliverable: "src/engine/multi_backtest.py::MultiBacktestEngine.run"
    consumer: "src/api/app.py::POST /api/backtest/multi-stock"
    binding_type: import_usage
    verify: "from src.engine.multi_backtest import"

  - deliverable: "src/data/multi_backtest_store.py::save_result"
    consumer: "src/engine/multi_backtest.py"
    binding_type: import_usage
    verify: "from src.data.multi_backtest_store import"

  - deliverable: "src/data/multi_backtest_store.py::get_latest_result"
    consumer: "src/api/app.py::GET /api/backtest/multi-stock/result (Story 12.3)"
    binding_type: import_usage
    verify: "multi_backtest_store\\.get_latest_result"

  - deliverable: "data/migrations/20260603_create_multi_backtest_tables.sql"
    consumer: "main.py (启动时自动跑迁移)"
    binding_type: schema_applied
    verify: "ledger_doc_store.*multi_backtest"

  - deliverable: "POST /api/backtest/multi-stock"
    consumer: "src/static/index.html (回测 Tab 多标按钮 - Story 12.3)"
    binding_type: route_mount
    verify: "@app\\.post\\(.api/backtest/multi-stock.\\)"

  - deliverable: "src/engine/backtest.py (v1.0)"
    consumer: "src/engine/multi_backtest.py"
    binding_type: import_usage
    verify: "from src.engine.backtest import.*[Bb]acktest"
```

**绑定状态**: ⏳ 待 Dev 验证

### 测试要求

- **单元测试** (250 行):
  - `test_load_signals_complete` (AC1 数据加载)
  - `test_load_signals_with_missing_data` (AC1 缺数据补缺)
  - `test_open_position_capacity_limit` (BR-1.4)
  - `test_open_position_duplicate_code` (BR-1.5)
  - `test_open_position_insufficient_capital` (BR-1.6)
  - `test_close_position_take_profit_priority` (BR-1.2)
  - `test_close_position_stop_loss_priority` (BR-1.2)
  - `test_close_position_max_hold_days` (BR-1.2)
  - `test_run_full_window` (AC1 主循环)
  - `test_run_force_close_at_end` (BR-1.8)
  - `test_grouping_sample_size_threshold` (样本 < 5)
- **集成测试** (120 行):
  - 端到端: 准备 mock screener_history → 跑回测 → 验证 trades 数量
  - 边界: 缺数据 → 412 / 超时 → 408
- **性能测试** (T8):
  - 60/90/120 天窗口的耗时基准
- **手动验证**:
  - 本地启服务 → 跑 60 天回测 → 查 DuckDB 表确认持久化

### 关键算法参考

```python
# src/engine/multi_backtest.py 核心伪代码
def run(self, window_days: int = 60, initial_capital: float = 1_000_000) -> MultiBacktestResult:
    capital = initial_capital
    open_positions: dict[str, Position] = {}
    trades: list[Trade] = []
    skipped = 0

    # 1. 加载信号
    signals = self._load_signals(window_days)  # 含补缺
    trade_calendar = self._build_trade_calendar(window_days)

    # 2. 逐日推动
    for date in trade_calendar:
        # 2a. 先平仓（按当日 close）
        for code, pos in list(open_positions.items()):
            close_price = self._get_daily_close(code, date)
            if close_price is None:
                continue
            pnl_pct = (close_price - pos.buy_price) / pos.buy_price * 100
            exit_reason = self._eval_exit(pnl_pct, pos, date)
            if exit_reason:
                trades.append(self._close_position(pos, close_price, date, exit_reason))
                capital += close_price * pos.shares
                del open_positions[code]

        # 2b. 再开仓（按当日信号）
        day_signals = [s for s in signals if s.trade_date == date]
        for sig in day_signals:
            if not sig.has_next_day_open:
                skipped += 1
                continue
            pos = self._open_position(sig, capital, open_positions)
            if pos:
                open_positions[sig.code] = pos
                capital -= pos.buy_price * pos.shares

    # 3. 强制平仓剩余
    final_close = self._get_last_close(trade_calendar[-1])
    for code, pos in list(open_positions.items()):
        trades.append(self._close_position(pos, final_close, trade_calendar[-1], 'force_close'))
        capital += final_close * pos.shares

    # 4. 统计
    return self._build_result(trades, initial_capital, capital, skipped)
```

---

## 变更日志

| 日期 | Agent | 状态转换 | 详情/链接 |
|------|-------|----------|-----------|
| 2026-06-03 | SM (萧何) | Created → AwaitingArchReview | Story 创建，基于 [PRD §6 Epic 12.2](docs/PRD.md#epic-12) + [架构文档 §4](docs/architecture/multi-backtest-module.md) |

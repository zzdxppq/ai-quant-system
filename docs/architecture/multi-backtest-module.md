# 多标的回测扩展 — 实施架构（v2.0）

> **架构师**：鲁班 | **日期**：2026-06-03 | **状态**：Epic 12 设计就绪，待 SM 拆 Story
>
> **输入**：docs/PRD.md v2.0 §6 Epic 12（多标回测 4 个 Story）
>
> **核心原则**：**与 v1.0 `backtest.py` 并行而非替换**。零迁移成本，零数据格式变更，可独立开关。

---

## 1. 设计决策摘要

| 维度 | 决策 | 置信度 | 理由 |
|------|------|--------|------|
| 与 v1.0 回测关系 | **并行模块**（不替换 `backtest.py`） | 高 | 用户可对比"仅代表股 vs 多标的"，是核心价值 |
| 引擎位置 | 新建 `src/engine/multi_backtest.py` | 高 | 单文件 < 400 行；类比 `backtest.py` 风格 |
| 数据源 | `data/screener_history.json` + DuckDB `screener_history_entry` 表 | 高 | 项目已有完整补缺链路 |
| 时间窗口 | 默认 60 交易日（PRD），可参数化 | 高 | 短于 30 样本不足；长于 90 计算 > 5 分钟 |
| 回测精度 | T+1 开仓 / T+2..T+4 持仓 / 强制 T+5 平仓 | 高 | 1+3=4 日，与 PRD 止盈止损 + 持有 ≤3 天一致 |
| 仓位模型 | 按 `planned_position_layers` 等权分配 | 中 | 简单可解释；不实现凯利公式（避免过度优化陷阱） |
| 同日持仓上限 | 5 只 | 高 | PRD 已规定；超限新信号跳过 |
| 统计维度 | 周期阶段 / 信号等级 / 连板数 | 高 | PRD 规定；样本 < 5 标记"样本不足" |
| 性能预算 | 60 天 × 5 只/日 = 300 笔交易，< 60s | 中 | Python 纯循环可达；若 > 5 分钟再考虑 numba |
| API 路由 | 追加到 `src/api/app.py` | 高 | 与 Epic 11 同模式 |
| 模块开关 | `MULTI_BACKTEST_ENABLED=1` 默认关闭 | 高 | NFR10 一致性 |
| CSV 导出 | 后端 `csv` 库 + FastAPI `StreamingResponse` | 高 | 流式输出, 避免大文件内存压力 |

---

## 2. 组件视图

```
┌─────────────────────────────────────────────────────────────────┐
│             src/api/app.py 追加 4 路由                          │
│  GET  /api/backtest/multi-stock/prepare     准备+校验           │
│  POST /api/backtest/multi-stock             执行多标回测         │
│  GET  /api/backtest/multi-stock/result      获取最近结果         │
│  GET  /api/backtest/multi-stock/export      CSV 明细导出         │
└──────────┬──────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│   src/engine/multi_backtest.py  （新建 ~350 行）                 │
│  · MultiBacktestEngine                                          │
│    - run(window_days, initial_capital) -> BacktestResult        │
│    - _load_signals(window_days) -> list[SignalRecord]            │
│    - _simulate_trade(signal, capital) -> Trade                  │
│    - _apply_exit_rules(trade, day_prices) -> exit_event         │
│  · Trade / BacktestResult / GroupStats dataclasses              │
└──────────┬─────────────────┬───────────────────┬────────────────┘
           │                 │                   │
           ▼                 ▼                   ▼
┌────────────────────┐ ┌──────────────┐ ┌──────────────────────┐
│ screener_history   │ │ 现有日K补缺  │ │ 现有止盈止损参数     │
│  + repair_close    │ │ 链路          │ │  src/config.py       │
│  + backfill_next   │ │              │ │  BACKTEST_CONFIG     │
│  _day_auction      │ │              │ │  (新增块)            │
└────────────────────┘ └──────────────┘ └──────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│   src/data/multi_backtest_store.py  （新建 ~120 行）             │
│  · save_result(result) -> result_id                             │
│  · get_latest_result() -> BacktestResult | None                  │
│  · get_result_detail(result_id) -> BacktestResult               │
│  · DuckDB 文档表: multi_backtest_result                          │
└─────────────────────────────────────────────────────────────────┘
```

**新文件**：
- `src/engine/multi_backtest.py`（~350 行）
- `src/data/multi_backtest_store.py`（~120 行）

**修改文件**：
- `src/data/models.py`（追加 `MultiBacktestResult` ORM）
- `src/api/app.py`（追加 4 路由）
- `src/config.py`（追加 `BACKTEST_CONFIG` 块 + `MULTI_BACKTEST_ENABLED`）

---

## 3. 数据模型

### 3.1 `MultiBacktestResult`

```python
@dataclass
class MultiBacktestResult:
    id: int
    run_at: datetime                       # 跑批时间
    window_days: int                       # 回测窗口
    initial_capital: float
    final_equity: float
    total_return_pct: float
    total_trades: int
    win_count: int
    loss_count: int
    win_rate: float
    profit_factor: float                   # 总盈利 / 总亏损
    avg_pnl_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    max_drawdown: float                    # 最大回撤（负数）

    # 分组统计（嵌套结构）
    by_cycle_phase: dict[str, GroupStats]  # 7 阶段
    by_signal_level: dict[str, GroupStats]  # strong/normal/watch
    by_continuous_limit_up: dict[str, GroupStats]  # 2/3/4/5+

    # 交易明细（导出用）
    trades: list[Trade]

    # 元数据
    config_snapshot: dict                  # 跑批时的配置快照
    skipped_count: int                     # 因数据缺失跳过的记录数
    duration_seconds: float
```

### 3.2 `Trade` (嵌套在 Result)

```python
@dataclass
class Trade:
    trade_date_open: str                   # 入选日
    trade_date_buy: str                    # 实际买入日 (T+1)
    trade_date_close: str                  # 平仓日
    code: str
    name: str
    signal_source: str
    signal_level: str
    cycle_phase_open: str
    cycle_phase_close: str
    continuous_limit_up: int
    buy_price: float
    sell_price: float
    shares: int
    position_layers: int
    hold_days: int
    pnl: float
    pnl_pct: float
    exit_reason: str                       # take_profit / stop_loss / max_hold / manual
```

### 3.3 SQL 表

```sql
CREATE TABLE IF NOT EXISTS multi_backtest_result (
  id            BIGINT PRIMARY KEY DEFAULT nextval('multi_backtest_seq'),
  doc           JSON,
  run_at        TIMESTAMP,
  window_days   INT
);
CREATE INDEX IF NOT EXISTS idx_multi_backtest_run_at ON multi_backtest_result(run_at);

CREATE SEQUENCE IF NOT EXISTS multi_backtest_seq START 1;
```

---

## 4. 回测核心算法

```python
# src/engine/multi_backtest.py （伪代码骨架）

def run(window_days: int = 60, initial_capital: float = 1_000_000) -> MultiBacktestResult:
    """主入口"""
    capital = initial_capital
    open_positions: dict[str, Position] = {}  # code -> Position
    trades: list[Trade] = []
    skipped = 0

    # 1. 加载信号池（含补缺校验）
    signals = _load_signals(window_days)  # 内部调用 prepare
    for sig in signals:
        if not sig.has_next_day_open:    # 数据缺失
            skipped += 1
            continue

        # 2. 撮合：当日入选 + 已有持仓未平仓
        if len(open_positions) >= MAX_OPEN:
            continue  # 满仓跳过

        if sig.code in open_positions:
            continue  # 同一 code 已有持仓，跳过（避免重复开仓）

        # 3. 计算可分配仓位
        layers = sig.planned_position_layers or 3
        position_size = capital * (layers / 30)  # 6 层 ≈ 20% 资金
        shares = int(position_size / sig.next_day_open_price / 100) * 100

        if shares < 100:
            continue  # 资金不足以开 1 手

        # 4. 开仓
        pos = Position(
            code=sig.code, buy_price=sig.next_day_open_price,
            shares=shares, open_date=sig.trade_date,
            signal_level=sig.signal_level,
            cycle_phase=sig.cycle_phase,
            planned_take_profit=BACKTEST_CONFIG['take_profit_pct'],
            planned_stop_loss=BACKTEST_CONFIG['stop_loss_pct'],
            max_hold_days=BACKTEST_CONFIG['max_hold_days'],
        )
        open_positions[sig.code] = pos
        capital -= pos.buy_price * pos.shares

    # 5. 逐日推动：检查止盈止损 + 持有天数
    trade_calendar = _build_trade_calendar(window_days)  # 60 个交易日
    for date in trade_calendar:
        # 先平仓
        for code, pos in list(open_positions.items()):
            close_price = _get_daily_close(code, date)
            if close_price is None:
                continue  # 数据缺失保留持仓

            pnl_pct = (close_price - pos.buy_price) / pos.buy_price * 100
            exit_reason = None
            if pnl_pct >= pos.planned_take_profit:
                exit_reason = 'take_profit'
            elif pnl_pct <= pos.planned_stop_loss:
                exit_reason = 'stop_loss'
            elif (date - pos.open_date).days >= pos.max_hold_days:
                exit_reason = 'max_hold'

            if exit_reason:
                trade = _close_position(pos, close_price, date, exit_reason)
                trades.append(trade)
                capital += close_price * pos.shares
                del open_positions[code]

        # 再开仓（按信号顺序）
        new_signals = [s for s in signals if s.trade_date == date]
        # ... 重复 2-4 步骤

    # 6. 强制平仓剩余持仓（回测结束时）
    final_close = _get_last_close(trade_calendar[-1])
    for code, pos in list(open_positions.items()):
        trade = _close_position(pos, final_close, trade_calendar[-1], 'force_close')
        trades.append(trade)
        capital += final_close * pos.shares

    # 7. 统计
    return _build_result(trades, initial_capital, capital, skipped)
```

**关键算法说明**：
- 仓位公式：`position_size = capital * (layers / 30)` — 6 层 = 20% 资金（与 PRD 仓位建议一致）
- 同一 code 不重复开仓：避免"加仓摊薄成本"歧义
- 满仓跳过：保持 5 只上限，避免分散到 10+ 只
- 资金为零仍可继续：满仓后只能卖出等资金回流

---

## 5. 性能评估

| 场景 | 交易笔数 | 预计耗时 |
|------|----------|----------|
| 60 天 × 1 只/日 | 60 笔 | < 5s |
| 60 天 × 3 只/日 | 180 笔 | < 15s |
| 60 天 × 5 只/日 | 300 笔 | < 30s |
| 90 天 × 5 只/日 | 450 笔 | < 60s |
| 120 天 × 5 只/日 | 600 笔 | ~ 90s（接近边界） |

**性能瓶颈**：`_get_daily_close()` 内部走日 K 接口（新浪/东财），单只单日 ~ 200ms。**优化方向**：
- **L1**：批量拉取（同日所有持仓一次性拉日 K）— 计划中
- **L2**：本地缓存（`data/kline_cache/{code}.json`，TTL 7 天）— 计划中
- **L3**：numba/Cython — 暂不需要

**MVP 不做 L1/L2**，先解决功能。Story 12.2 实施时若实测 > 60s 再优化。

---

## 6. API 契约

### 6.1 `GET /api/backtest/multi-stock/prepare`

**Response 200**:
```json
{
  "ready": true,
  "window_days": 60,
  "total_signals": 142,
  "missing_close": 3,
  "missing_next_day": 5,
  "auto_filled": 5,
  "ready_to_run": true
}
```

**Response 412** (数据缺失过多):
```json
{
  "ready": false,
  "missing_pct": 0.18,
  "missing_detail": [
    { "trade_date": "2026-05-15", "code": "600123", "missing": "next_day_open" }
  ],
  "suggested_action": "执行 GET /api/screener-history 触发自动补缺"
}
```

### 6.2 `POST /api/backtest/multi-stock`

**Request**:
```json
{ "window_days": 60, "initial_capital": 1000000 }
```

**Response 200**（同步执行，< 60s）:
```json
{
  "id": 7,
  "run_at": "2026-06-03T19:40:00+08:00",
  "duration_seconds": 23.4,
  "summary": {
    "total_trades": 87,
    "win_rate": 0.563,
    "profit_factor": 1.42,
    "final_equity": 1187200,
    "total_return_pct": 0.187,
    "max_drawdown": -0.082
  },
  "by_cycle_phase": {
    "完整周期": { "trades": 42, "win_rate": 0.62, "profit_factor": 1.85 },
    "退潮期": { "trades": 5, "win_rate": null, "note": "样本不足" }
  }
}
```

**Response 408** (超时):
```json
{ "error": "回测超时（> 60s），建议缩短时间窗口", "partial": {...} }
```

### 6.3 `GET /api/backtest/multi-stock/export?id=7`

**Response 200**: `Content-Type: text/csv` + `Content-Disposition: attachment; filename=multi_backtest_20260401_20260603.csv`

CSV 列：`trade_date_open,trade_date_buy,trade_date_close,code,name,signal_source,signal_level,cycle_phase_open,cycle_phase_close,continuous_limit_up,buy_price,sell_price,shares,position_layers,hold_days,pnl,pnl_pct,exit_reason`

---

## 7. 配置扩展

```python
# src/config.py 追加
BACKTEST_CONFIG = {
    "take_profit_pct": 10.0,
    "stop_loss_pct": -5.0,
    "max_hold_days": 3,
    "max_open_positions": 5,
    "default_window_days": 60,
    "default_initial_capital": 1_000_000,
    "timeout_seconds": 60,
    "min_sample_for_grouping": 5,         # 样本 < 5 标"样本不足"
}

MULTI_BACKTEST_ENABLED = os.getenv("MULTI_BACKTEST_ENABLED", "0") == "1"
```

---

## 8. 与 v1.0 backtest.py 的对比

| 维度 | v1.0 backtest.py | v2.0 multi_backtest.py |
|------|------------------|-------------------------|
| 持仓池 | 仅代表股（1 只/日） | 全部命中标的（1-5 只/日） |
| 仓位 | 固定 6 层 | 按 `planned_position_layers` |
| 评估价值 | 验证"按信号开仓"理念 | 评估**真实**多标的表现 |
| 状态 | ✅ v1.0 已实现 | 🆕 v2.0 新增 |
| 并行运行 | — | ✅ 两个模块独立保存结果 |
| UI 展示 | 已有回测 Tab | 同一 Tab 内增加对比视图 |
| 回测时长 | < 5s | 20-60s（受日 K 拉取限制） |
| 文件 | `src/engine/backtest.py` (429 行) | `src/engine/multi_backtest.py` (~350 行) |

**关键**：两个模块**共享** `BACKTEST_CONFIG` 中的止盈止损/持有天数（`src/config.py` 集中配置）。改一处两边生效。

---

## 9. 实施风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 日 K 数据缺失导致回测偏差 | 中 | 中 | `prepare` 校验 + `skipped_count` 透明展示 |
| 回测 > 60s 用户体验差 | 中 | 中 | 进度条（前端轮询 `/api/backtest/multi-stock/result`） |
| 仓位公式被反向优化（过拟合） | 中 | 高 | 文档明示"回测结果仅供参考，不预测未来" |
| CSV 导出大文件（> 10MB） | 低 | 中 | StreamingResponse 流式输出 |
| 节假日错位 | 低 | 低 | 使用 `is_trading_day` + 交易日历 |

---

## 10. 实施 Story 拆分建议

| Story ID | 标题 | 关键文件 |
|----------|------|----------|
| **12.1** | 历史选股池准备（screener_history 校验 + 补缺） | `multi_backtest.py::_load_signals` + GET prepare 路由 |
| **12.2** | 多标的逐日回放（核心） | `multi_backtest.py::run` + POST 路由 |
| **12.3** | 分组统计 + 对比曲线 | `multi_backtest.py::_build_result` + UI 扩展 |
| **12.4** | 单标交易明细导出（CSV） | `app.py` GET export 路由 + StreamingResponse |

---

## 11. 变更日志

| 日期 | 版本 | 描述 | 作者 |
|------|------|------|------|
| 2026-06-03 | 1.0.0 | 初版：Epic 12 多标回测模块实施架构 | 鲁班 (Architect Agent) |

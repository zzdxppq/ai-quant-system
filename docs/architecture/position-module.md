# 持仓管理 + 实时盈亏 — 实施架构（v2.0）

> **架构师**：鲁班 | **日期**：2026-06-03 | **状态**：Epic 11 设计就绪，待 SM 拆 Story
>
> **输入**：docs/PRD.md v2.0 §6 Epic 11（持仓管理 4 个 Story） + docs/design/variants/variant-1-jizhi.html（UX 视觉契约）
>
> **核心原则**：**复用 v1.2 全部数据层 / 调度 / 配置抽象，零新依赖、零新存储后端、零新调度器**。

---

## 1. 设计决策摘要

| 维度 | 决策 | 置信度 | 理由 |
|------|------|--------|------|
| 存储后端 | **复用 DuckDB `quant.duckdb`** | 高 | 项目已统一；`structured_store.py` / `ledger_doc_store.py` 抽象已成熟 |
| 持仓模型 | 新建 `Position` ORM model（追加到 `src/data/models.py`） | 高 | 与现有 `DailyQuote` / `ScreenerResult` 同模式 |
| 实时盈亏 | **复用** `src/data/sina_spot_api.py` 全市场快照链路 | 高 | 9:30-15:00 跑全市场快照本就有；持仓 1-5 只的成本可忽略 |
| 调度 | 追加到 `main.py` APScheduler，**不**新建调度器 | 高 | 单进程原则；NFR10 要求"未启用时不破坏 v1.2" |
| 盈亏刷新频率 | 60s（PRD NFR4），落地为 60s cron | 高 | UX 实时感足够；避免对新浪接口造成压力 |
| API 路由 | 追加到 `src/api/app.py` 末尾，**不**新建 router 文件 | 中 | 路由 < 10 个，统一文件更易读；如未来 > 15 再拆分 |
| 自动平仓 | **不实现** | 高 | 范蠡哲学：AI 是心智的自行车，绝不替用户操作券商 |
| 信号对照 | 复用 `src/engine/cross_validator.py` 输出 | 高 | 4 级信号已是单一事实源 |
| 持仓是否进 v1.2 数据流 | **可选**——`POSITION_MODULE_ENABLED=1` 开关 | 高 | NFR10 强制：未启用时不影响原流程 |
| 时间戳时区 | `TZ_CN`（`src/config.py` 已有） | 高 | 跨日对账唯一正确方式 |

---

## 2. 组件视图

```
┌────────────────────────────────────────────────────────────────────┐
│                  src/api/app.py （追加 ~6 路由）                    │
│  POST /api/positions        创建开仓                                │
│  GET  /api/positions        持仓列表（含 open/closed）              │
│  GET  /api/positions/{id}   单笔详情                                │
│  POST /api/positions/{id}/close  平仓                               │
│  GET  /api/positions/summary  持仓状态条聚合（总市值/总盈亏/预警）   │
│  GET  /api/positions/{id}/signal-validity  信号对照                  │
└──────────┬─────────────────────────────────────────────┬───────────┘
           │                                             │
           ▼                                             ▼
┌─────────────────────────┐               ┌───────────────────────────┐
│ src/engine/position.py  │               │ src/data/position_store.py│
│  · PositionEngine       │◀──────────────│  · create() / list() /    │
│  · refresh_pnl()        │               │    get() / close()        │
│  · check_signal_valid() │               │  · DuckDB ledger_doc      │
│  · evaluate_alert()     │               │    持久化                 │
└──────┬───────────┬──────┘               └─────────┬─────────────────┘
       │           │                                │
       │           │                                ▼
       │           │                  ┌──────────────────────────────┐
       │           │                  │ data/quant.duckdb            │
       │           │                  │  · position 文档表 (新)      │
       │           │                  │  · position_history (新)     │
       │           │                  │  · 全部 v1.2 表 (复用)        │
       │           │                  └──────────────────────────────┘
       │           │
       │           ▼
       │  ┌─────────────────────────┐    ┌──────────────────────────────┐
       │  │ src/data/sina_spot_api   │    │ src/engine/cross_validator.py│
       │  │  · 全市场实时快照        │    │  · 4 级信号                  │
       │  │  · 复用现有 get_spot()   │    │  · 复用 evaluate()            │
       │  └─────────────────────────┘    └──────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────┐
│ main.py  (APScheduler 追加 1 个 job)   │
│  · refresh_positions_pnl               │
│    触发: 交易日 9:30-15:00 每 60s       │
│    幂等: 同一持仓 60s 内不重刷          │
│    失败: 连续 3 次后降级为不刷新         │
└────────────────────────────────────────┘
```

**新文件清单**：
- `src/engine/position.py`（~250 行，类比 `screener.py` 风格）
- `src/data/position_store.py`（~150 行，类比 `screener_history.py`）

**修改文件**：
- `src/data/models.py`（追加 `Position` / `PositionHistory` ORM）
- `src/api/app.py`（追加 6 路由 + 启动时注册 Pydantic model）
- `main.py`（追加 1 个 APScheduler job + 1 个开关）
- `src/config.py`（追加 `POSITION_CONFIG` 块 + `POSITION_MODULE_ENABLED`）

---

## 3. 数据模型

### 3.1 `Position` （持仓，document 表）

DuckDB 文档表（沿用项目 `ledger_doc_store` 模式），schema：

```python
# src/data/models.py
@dataclass
class Position:
    id: int                          # 自增 PK
    code: str                        # 6 位 A 股代码
    name: str                        # 中文名（拉取时填入）
    trade_date: str                  # 开仓所属交易日 YYYY-MM-DD
    buy_price: float                 # 买入均价
    shares: int                      # 持有股数（100 倍数）
    planned_position_layers: int     # 计划仓位层数 1-9
    planned_take_profit_pct: float   # 止盈%, 默认 10.0
    planned_stop_loss_pct: float     # 止损%, 默认 -5.0
    signal_source: str | None        # 关联 screener run id, 可空
    signal_level: str | None         # strong/normal/watch/avoid, 录入时快照
    cycle_phase: str | None          # 录入时周期阶段快照
    status: str                      # open / partial_close / closed
    realized_pnl: float = 0.0        # 已实现盈亏（部分平仓累加）
    remaining_shares: int = 0         # 剩余股数（= shares - 已平仓）
    current_price: float | None      # 最新价（由 refresh_pnl 填）
    current_pnl: float | None        # 当前浮动盈亏
    current_pnl_pct: float | None    # 当前盈亏%
    alert_level: str = "none"        # none / yellow / red
    price_stale: bool = True         # 行情是否过期
    signal_still_valid: bool | None  # 信号对照结果
    created_at: datetime             # 创建时间（TZ_CN）
    updated_at: datetime             # 更新时间
    closed_at: datetime | None       # 全平时间
```

### 3.2 `PositionHistory` （已平仓归档）

```python
@dataclass
class PositionHistory:
    id: int
    position_id: int                 # 关联原 Position.id
    code: str
    name: str
    trade_date: str                  # 开仓日
    close_date: str                  # 平仓日
    buy_price: float
    sell_price: float                # 加权平均卖出价（部分平仓时）
    shares_closed: int
    realized_pnl: float              # 真实盈亏 = (sell-buy) * shares
    realized_pnl_pct: float
    hold_days: int                   # 持仓天数
    exit_reason: str                 # manual / take_profit / stop_loss / signal_expired
    signal_source: str | None
    cycle_phase_open: str | None
    cycle_phase_close: str | None
    created_at: datetime
```

### 3.3 存储表（SQL）

```sql
-- 持仓主表（DuckDB 文档表，由 ledger_doc_store 管理）
CREATE TABLE IF NOT EXISTS position (
  id            BIGINT PRIMARY KEY DEFAULT nextval('position_seq'),
  doc           JSON,                -- 整个 Position 对象序列化
  -- 提取常用查询字段到列（避免每次 JSON 解析）
  code          VARCHAR,
  trade_date    DATE,
  status        VARCHAR,
  updated_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_position_status ON position(status);
CREATE INDEX IF NOT EXISTS idx_position_code    ON position(code);
CREATE INDEX IF NOT EXISTS idx_position_date    ON position(trade_date);

-- 持仓历史归档
CREATE TABLE IF NOT EXISTS position_history (
  id            BIGINT PRIMARY KEY DEFAULT nextval('position_history_seq'),
  doc           JSON,
  code          VARCHAR,
  trade_date    DATE,
  close_date    DATE,
  created_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_position_history_code ON position_history(code);
CREATE INDEX IF NOT EXISTS idx_position_history_close_date ON position_history(close_date);

-- 序列
CREATE SEQUENCE IF NOT EXISTS position_seq START 1;
CREATE SEQUENCE IF NOT EXISTS position_history_seq START 1;
```

**为什么用文档表 + 提取列**：与 `screener_history_entry`、`decision_records` 等项目已有模式完全一致；提取列保证按 `status` / `code` 过滤的 O(1) 索引。

---

## 4. API 契约（OpenAPI 3.0 摘要）

> 完整 OpenAPI 由 `*extract-api-contracts` 在实施后生成；此处只定义契约。

### 4.1 `POST /api/positions`

**Request**:
```json
{
  "code": "600123",
  "buy_price": 10.50,
  "shares": 1000,
  "trade_date": "2026-06-03",          // 可选, 默认今日
  "planned_position_layers": 6,
  "planned_take_profit_pct": 10.0,     // 可选, 默认 10
  "planned_stop_loss_pct": -5.0,       // 可选, 默认 -5
  "signal_source": "screener_2026-06-03_927"  // 可选
}
```

**Response 201**:
```json
{
  "id": 42,
  "code": "600123",
  "name": "XX 科技",                    // 后端从行情快照补
  "status": "open",
  "buy_price": 10.50,
  "shares": 1000,
  "remaining_shares": 1000,
  "planned_position_layers": 6,
  "current_price": 10.55,                // 当日开盘价/最新价
  "current_pnl": 50.0,
  "current_pnl_pct": 0.476,
  "alert_level": "none",
  "price_stale": false,
  "created_at": "2026-06-03T09:35:12+08:00"
}
```

**Error 409**: `{ "error": "该代码今日已有未平仓记录", "existing_position_id": 38 }`
**Error 400**: `{ "error": "买入价必须 > 0" }` (字段级)

### 4.2 `GET /api/positions?status=open`

**Response 200**:
```json
{
  "positions": [...],
  "summary": {
    "total_market_value": 247800.0,
    "total_pnl": -3420.0,
    "total_pnl_pct": -1.36,
    "open_count": 3,
    "alert_count": 1
  }
}
```

### 4.3 `POST /api/positions/{id}/close`

**Request**:
```json
{ "sell_price": 11.00, "sell_shares": 1000 }
```

**Response 200**:
```json
{
  "id": 42,
  "status": "closed",
  "realized_pnl": 500.0,
  "realized_pnl_pct": 4.76,
  "closed_at": "2026-06-04T10:15:33+08:00"
}
```

**Error 422**: `{ "error": "卖出股数超过剩余股数" }`

### 4.4 `GET /api/positions/summary`

供首页"持仓状态条"用。**1 次调用 = 1 个聚合对象**，避免 N+1。

**Response 200**:
```json
{
  "has_position": true,
  "open_count": 3,
  "total_market_value": 247800.0,
  "total_pnl": -3420.0,
  "total_pnl_pct": -1.36,
  "alerts": [
    { "id": 38, "code": "600456", "alert_level": "red", "reason": "stop_loss" }
  ],
  "as_of": "2026-06-03T14:35:00+08:00"
}
```

### 4.5 `GET /api/positions/{id}/signal-validity`

**Response 200**:
```json
{
  "position_id": 38,
  "code": "600456",
  "signal_still_valid": false,
  "current_signal_level": "avoid",
  "check_date": "2026-06-03",
  "reason": "今日 9:27 选股未命中 600456"
}
```

---

## 5. 调度集成

### 5.1 `main.py` 追加 job

```python
# main.py （追加片段，伪代码）
from src.engine.position import refresh_positions_pnl_job
from src.config import POSITION_MODULE_ENABLED

if POSITION_MODULE_ENABLED:
    scheduler.add_job(
        refresh_positions_pnl_job,
        'cron',
        second='*/60',                           # 每 60s
        hour='9-14',                             # 9:30-15:00 实际（hour=9 实际覆盖 9:00-9:59，依赖 is_trading_time() 二次判断）
        minute='*',
        id='refresh_positions_pnl',
        max_instances=1,                          # 防重叠
        coalesce=True,                           # 漏跑合并
    )
```

**注意**：`is_trading_time()` 二次判断含：
- `is_trading_day(now_cn())` → 排除周末节假日
- 9:30 ≤ time ≤ 15:00 → 排除开盘前/收盘后
- 11:30 ≤ time ≤ 13:00 → 排除午休

### 5.2 幂等性 / 防抖

```python
# src/engine/position.py
_LAST_REFRESH_TS: dict[int, float] = {}  # position_id -> ts

def refresh_positions_pnl_job():
    now = now_cn()
    if not is_trading_time(now):
        return
    for p in position_store.list_open():
        last = _LAST_REFRESH_TS.get(p.id, 0)
        if now.timestamp() - last < 60:
            continue  # 60s 防抖
        try:
            new_price = sina_spot_api.get_spot_one(p.code)  # 单只快照
            position_store.update_pnl(p.id, new_price, evaluate_alert(p, new_price))
            _LAST_REFRESH_TS[p.id] = now.timestamp()
        except Exception as e:
            logger.warning(f"持仓 {p.id} 盈亏刷新失败: {e}")
            position_store.mark_price_stale(p.id)
```

### 5.3 故障降级

连续 3 次失败 → `position_store.mark_untrackable(p.id)` → 该持仓停止刷新，但看板仍显示**最后一次成功价 + `price_stale=true`**，绝不阻塞。

---

## 6. 配置扩展

```python
# src/config.py 追加
POSITION_CONFIG = {
    "take_profit_default_pct": 10.0,
    "stop_loss_default_pct": -5.0,
    "max_open_positions": 5,           # 硬上限, 超出 409
    "refresh_interval_sec": 60,
    "max_consecutive_failure": 3,      # 超过后停止刷新该持仓
    "max_hold_days_warn": 3,           # 持仓 > 3 天提示
}

POSITION_MODULE_ENABLED = os.getenv("POSITION_MODULE_ENABLED", "0") == "1"
```

**默认关闭**——NFR10：未启用时完全不影响 v1.2 流程。`.env` 加 `POSITION_MODULE_ENABLED=1` 启动。

---

## 7. 与 v1.2 的契合度验证

| v1.2 模块 | v2.0 是否复用 | 复用方式 |
|-----------|---------------|----------|
| `sina_spot_api` 全市场快照 | ✅ 复用 | `get_spot_one(code)` 已有, 单只拉取 |
| `cross_validator` 4 级信号 | ✅ 复用 | 信号对照直接调 `evaluate()` |
| `screener_history` 数据 | ✅ 复用 | 持仓 `signal_source` 关联 |
| DuckDB `quant.duckdb` | ✅ 复用 | 新增 2 表, 不改 v1.2 表 |
| APScheduler `main.py` | ✅ 复用 | 追加 1 job, 不改现有 cron |
| `src/config.py` 配置模式 | ✅ 复用 | 追加 `POSITION_CONFIG` 块 |
| `is_trading_day` / `now_cn` | ✅ 复用 | 调度时段判断 |
| `json_io` / `documents_db` | ✅ 复用 | 文档表读写 |
| **不**复用 | — | Web 看板（`src/static/index.html`）需追加持仓 Tab，**由 UX 设计稿驱动** |
| **不**实现 | — | 券商交易 API 自动下单（范蠡红线） |

**架构契合度评分**：9/10——几乎全复用，仅 1 个 UI 改动。Epic 11 增量风险：**低**。

---

## 8. 实施风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 新浪快照限流（60s × 5 只持仓 = 5 req/min） | 中 | 中 | 单只接口 `get_spot_one()` 走独立连接池；超出走 `tencent_api` 备选 |
| 持仓时间晚于 9:27 选股 → 无法关联 `signal_source` | 高 | 低 | 允许 signal_source 为空，标记 `signal_mismatch=false` |
| 同 code 当日重复开仓（用户手抖） | 中 | 中 | 业务规则 BR-1.1 强制 (code, trade_date) 唯一 |
| 节假日开仓（is_trading_day 失效） | 低 | 中 | 录入时校验 trade_date, 周末/节假日返回 400 |
| 用户预期系统自动平仓 | 中 | 高 | UX 反复强调"系统不替你操作"；平仓按钮文案"卖出"非"止损执行" |

---

## 9. 待 SM 拆解的 Story 锚点

> 这些是 Epic 11 的 Story 边界，本架构文档不重复 Story 细节，PRD 已有 AC。SM 拆 Story 时按此架构实施。

| Story ID | 标题 | 关键文件 |
|----------|------|----------|
| **11.1** | 手动录入开仓 | `position.py` + `position_store.py` + `app.py` POST 路由 |
| **11.2** | 实时盈亏刷新任务 | `position.py::refresh_pnl` + `main.py` cron |
| **11.3** | 信号对照 + 状态机 | `position.py::check_signal_valid` + POST close 路由 |
| **11.4** | 持仓 UI（状态条 + Tab） | `index.html` 追加组件（UX 视觉契约 variant-1-jizhi.html） |

**Story 11.4 必须等 UX variant-1-jizhi.html 验收后**才实施——避免 UI 与代码不同步。

---

## 10. 变更日志

| 日期 | 版本 | 描述 | 作者 |
|------|------|------|------|
| 2026-06-03 | 1.0.0 | 初版：Epic 11 持仓管理模块实施架构 | 鲁班 (Architect Agent) |

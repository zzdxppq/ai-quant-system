# AI 量化周期看板 — 系统架构文档

> 版本：1.0.0 | 日期：2026-04-08

---

## 1. 系统总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                         用户浏览器                                    │
│                   Vue.js 3 SPA 暗色仪表盘                             │
│              (src/static/index.html, 550 行)                         │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ HTTP (REST API)
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      FastAPI 应用层                                   │
│                    (src/api/app.py)                                   │
│          11 个 REST 端点 · 静态文件托管 · Uvicorn ASGI               │
└────────┬────────────────────┬───────────────────────┬────────────────┘
         │                    │                       │
         ▼                    ▼                       ▼
┌─────────────┐   ┌──────────────────┐   ┌──────────────────────────┐
│  定时调度器   │   │   手动触发接口    │   │     JSON 数据文件读取      │
│ APScheduler  │   │ POST /api/refresh │   │   GET /api/* → data/*.json│
│ (main.py)    │   │ (app.py)         │   │                          │
└──────┬───────┘   └────────┬─────────┘   └──────────────────────────┘
       │                    │
       ▼                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      调度编排层                                       │
│                  (src/scheduler.py)                                   │
│                                                                      │
│  run_cycle_update()          run_screener_update()                   │
│  ┌───────────────────┐       ┌────────────────────────────────────┐  │
│  │ 1. 拉取涨幅排行    │       │ 1. 拉取竞价快照 + 涨停历史         │  │
│  │ 2. 更新周期状态机  │       │ 2. 加载周期快照                    │  │
│  │ 3. 保存快照+排行   │       │ 3. 龙头竞价反馈评估                │  │
│  │ 4. 追加历史时间线  │       │ 4. 执行 7 层选股筛选               │  │
│  └───────────────────┘       │ 5. 240 周线偏离度过滤              │  │
│                              │ 6. 三层交叉验证                    │  │
│                              │ 7. 保存选股结果+信号               │  │
│                              └────────────────────────────────────┘  │
└──────┬───────────────────────────────┬───────────────────────────────┘
       │                               │
       ▼                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        引擎层 (src/engine/)                          │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐  │
│  │ cycle.py     │  │ screener.py  │  │ cross_validator.py        │  │
│  │ 周期状态机    │  │ 选股引擎     │  │ 三层交叉验证               │  │
│  │ (477 行)     │  │ (220 行)     │  │ (147 行)                  │  │
│  │              │  │              │  │                           │  │
│  │ · CyclePhase │  │ · 7层筛选    │  │ · 周期 × 龙头 × 选股     │  │
│  │ · 状态转换   │  │ · 连板检测   │  │ · 4级信号输出             │  │
│  │ · JSON持久化 │  │ · 排除规则   │  │ · 仓位建议               │  │
│  └──────────────┘  └──────────────┘  └───────────────────────────┘  │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ leader_feedback.py│  │ ma_deviation │  │ backtest.py            │ │
│  │ 龙头竞价反馈      │  │ 240周线偏离  │  │ 历史回测               │ │
│  │ (199 行)         │  │ (163 行)     │  │ (429 行)               │ │
│  │                  │  │              │  │                        │ │
│  │ · 5级信号分类    │  │ · 偏离度计算 │  │ · 逐日回放              │ │
│  │ · 一票否决机制   │  │ · 过热标记   │  │ · 止盈止损              │ │
│  │ · 激进度调节     │  │ · 批量检测   │  │ · 按阶段统计            │ │
│  └──────────────────┘  └──────────────┘  └────────────────────────┘ │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       数据层 (src/data/)                             │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────────┐  │
│  │ fetcher.py       │  │ models.py        │  │ mock_data.py      │  │
│  │ AKShare 数据拉取  │  │ SQLAlchemy ORM   │  │ 模拟数据生成       │  │
│  │ (247 行)         │  │ (98 行)          │  │ (162 行)          │  │
│  │                  │  │                  │  │                   │  │
│  │ · 实时快照       │  │ · DailyQuote     │  │ · 6种周期场景     │  │
│  │ · 涨停板池       │  │ · GainRanking    │  │ · 模拟快照        │  │
│  │ · 个股历史       │  │ · CycleState     │  │ · 模拟涨停历史    │  │
│  │ · 交易日历       │  │ · ScreenerResult │  │                   │  │
│  └────────┬─────────┘  └────────┬─────────┘  └───────────────────┘  │
│           │                     │                                    │
│           ▼                     ▼                                    │
│  ┌──────────────────┐  ┌──────────────────┐                         │
│  │ AKShare API      │  │ SQLite           │                         │
│  │ (东方财富数据)    │  │ data/quant.db    │                         │
│  └──────────────────┘  └──────────────────┘                         │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 目录结构

```
ai-quant-system/
├── main.py                  # 应用入口（初始化DB、启动调度器、启动Web服务）
├── requirements.txt         # Python依赖（9个包）
├── README.md                # 项目说明
│
├── src/
│   ├── __init__.py
│   ├── config.py            # 全局配置（阈值参数、API端口、调度时间）
│   ├── scheduler.py         # 调度编排（run_cycle_update / run_screener_update）
│   │
│   ├── engine/              # 核心引擎
│   │   ├── cycle.py         # 周期状态机（CycleEngine / CyclePhase / TrackedStock）
│   │   ├── screener.py      # 选股引擎（7层筛选规则）
│   │   ├── cross_validator.py # 交叉验证（3层 → 4级信号）
│   │   ├── leader_feedback.py # 龙头竞价反馈（5级信号 + 一票否决）
│   │   ├── ma_deviation.py  # 240周线偏离度过滤
│   │   └── backtest.py      # 历史回测引擎
│   │
│   ├── data/                # 数据层
│   │   ├── models.py        # ORM模型（4张表）
│   │   ├── fetcher.py       # AKShare数据拉取
│   │   └── mock_data.py     # 模拟数据生成（6种场景）
│   │
│   ├── api/
│   │   └── app.py           # FastAPI后端（11个REST端点）
│   │
│   └── static/
│       └── index.html       # Vue.js 3 SPA前端
│
├── data/                    # 运行时数据（自动创建）
│   ├── quant.db             # SQLite数据库
│   ├── cycle_state.json     # 周期状态机持久化
│   ├── latest_snapshot.json # 最新周期快照
│   ├── latest_ranking.json  # 最新涨幅排行
│   ├── latest_screener.json # 最新选股结果
│   ├── latest_signals.json  # 最新交叉验证信号
│   ├── latest_leader.json   # 最新龙头反馈
│   ├── latest_deviation.json# 最新偏离度数据
│   └── cycle_history.json   # 60天周期历史时间线
│
└── docs/                    # 文档
    ├── PRD.md               # 产品需求文档
    ├── ARCHITECTURE.md      # 本文件
    ├── 我的交易系统.md       # 交易体系说明
    ├── 选股公式.md           # 选股公式详解
    ├── 周期运行真相.md       # 周期理论
    └── 龙与妖.md            # 龙妖股心理学
```

---

## 3. 核心数据流

### 3.1 收盘后周期更新（15:30）

```
┌─────────────────┐
│ APScheduler触发  │
│ 或 --update 参数 │
└────────┬────────┘
         ▼
┌─────────────────┐     失败降级     ┌─────────────────┐
│ fetcher.py      │ ───────────────→ │ mock_data.py    │
│ 拉取全市场快照   │                  │ 模拟涨幅排行     │
└────────┬────────┘                  └────────┬────────┘
         │                                    │
         └──────────────┬─────────────────────┘
                        ▼
              ┌─────────────────┐
              │ 计算10日涨幅排行 │
              │ Top 100         │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │ CycleEngine     │
              │ .update()       │
              │ 状态机转换       │
              └────────┬────────┘
                       ▼
              ┌─────────────────────────────────┐
              │ 输出                              │
              │ ├── latest_snapshot.json          │
              │ ├── latest_ranking.json (Top 50)  │
              │ ├── cycle_state.json (状态持久化)  │
              │ └── cycle_history.json (追加)      │
              └─────────────────────────────────┘
```

### 3.2 早盘选股流程（9:27）

```
┌─────────────────┐
│ APScheduler触发  │
│ 或 --screen 参数 │
└────────┬────────┘
         ▼
┌─────────────────┐     ┌─────────────────┐
│ 拉取竞价快照     │     │ 拉取涨停池历史   │
│ stock_zh_a_spot  │     │ 最近5天          │
└────────┬────────┘     └────────┬────────┘
         │                       │
         ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│ 加载上一轮       │     │ 连板检测         │
│ cycle_snapshot   │     │ 统计连续涨停天数 │
└────────┬────────┘     └────────┬────────┘
         │                       │
         ▼                       │
┌─────────────────┐              │
│ 龙头竞价反馈     │              │
│ evaluate_leader  │              │
│ → 5级信号        │              │
│ → 一票否决?      │              │
└────────┬────────┘              │
         │                       │
         └──────────┬────────────┘
                    ▼
           ┌─────────────────┐
           │ run_screener()  │
           │ 7层过滤筛选      │
           └────────┬────────┘
                    ▼
           ┌─────────────────┐
           │ 240周线偏离度    │
           │ batch_check     │
           │ 标记过热标的     │
           └────────┬────────┘
                    ▼
           ┌─────────────────┐
           │ cross_validate  │
           │ 3层交叉验证      │
           │ → 4级信号输出    │
           └────────┬────────┘
                    ▼
           ┌──────────────────────────────┐
           │ 输出                          │
           │ ├── latest_screener.json      │
           │ ├── latest_leader.json        │
           │ ├── latest_deviation.json     │
           │ └── latest_signals.json       │
           └──────────────────────────────┘
```

---

## 4. 数据模型

### 4.1 数据库表（SQLite）

```
┌─────────────────────────────────────────────┐
│ daily_quote (日线行情)                       │
├─────────────────────────────────────────────┤
│ id          INTEGER PK AUTO                  │
│ code        VARCHAR(10) NOT NULL  [IDX]      │
│ name        VARCHAR(20)                      │
│ date        DATE NOT NULL         [IDX]      │
│ open        FLOAT                            │
│ high        FLOAT                            │
│ low         FLOAT                            │
│ close       FLOAT                            │
│ pre_close   FLOAT                            │
│ volume      FLOAT (手)                       │
│ amount      FLOAT (元)                       │
│ turnover    FLOAT (%)                        │
│ market_cap  FLOAT (元)                       │
│ is_limit_up BOOLEAN                          │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ gain_ranking (10日涨幅排行)                  │
├─────────────────────────────────────────────┤
│ id          INTEGER PK AUTO                  │
│ date        DATE NOT NULL         [IDX]      │
│ code        VARCHAR(10) NOT NULL             │
│ name        VARCHAR(20)                      │
│ gain_10d    FLOAT (%)                        │
│ rank        INTEGER                          │
│ sustain_days INTEGER DEFAULT 0               │
│ is_top      BOOLEAN DEFAULT FALSE            │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ cycle_state (周期状态记录)                    │
├─────────────────────────────────────────────┤
│ id                    INTEGER PK AUTO        │
│ date                  DATE NOT NULL  [IDX]   │
│ state                 VARCHAR(20) NOT NULL   │
│ representative_code   VARCHAR(10)            │
│ representative_name   VARCHAR(20)            │
│ representative_gain   FLOAT                  │
│ representative_top_days INTEGER DEFAULT 0    │
│ cycle_day             INTEGER DEFAULT 0      │
│ prev_cycle_code       VARCHAR(10)            │
│ prev_cycle_peak       FLOAT                  │
│ notes                 VARCHAR(200)           │
│ updated_at            DATETIME               │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│ screener_result (选股结果)                    │
├─────────────────────────────────────────────┤
│ id                INTEGER PK AUTO            │
│ date              DATE NOT NULL     [IDX]    │
│ code              VARCHAR(10) NOT NULL       │
│ name              VARCHAR(20)                │
│ continuous_limit_up INTEGER                  │
│ auction_gain      FLOAT (%)                  │
│ auction_turnover  FLOAT (%)                  │
│ auction_amount    FLOAT (万元)               │
│ market_cap        FLOAT (亿)                 │
│ volume_ratio      FLOAT                      │
│ matched_cycle     BOOLEAN DEFAULT FALSE      │
│ created_at        DATETIME                   │
└─────────────────────────────────────────────┘
```

### 4.2 核心数据结构

```python
# 周期阶段枚举
class CyclePhase(str, Enum):
    BREEDING         = "孕育期"
    SMALL_CYCLE_START = "小周期启动"
    CHAOS            = "混沌期"
    SMALL_CYCLE_DONE = "小周期完成"
    FULL_CYCLE       = "完整周期"
    AFTERGLOW        = "余温期"
    EBB              = "退潮期"

# 龙头反馈信号枚举
class LeaderSignal(str, Enum):
    STRONG_POSITIVE = "强正反馈"    # > +3%
    POSITIVE        = "正反馈"      # 0% ~ +3%
    NEUTRAL         = "中性"        # -3% ~ 0%
    NEGATIVE        = "负反馈"      # < -3%
    LIMIT_DOWN      = "跌停"        # 跌停价附近

# 交叉验证信号等级
Signal.level: "strong" | "normal" | "watch" | "avoid"
```

---

## 5. 模块依赖关系

```
main.py
  ├── src/config.py              ← 全局配置（被所有模块引用）
  ├── src/data/models.py         ← 数据库初始化
  ├── src/scheduler.py           ← 调度编排
  │     ├── src/engine/cycle.py
  │     ├── src/engine/screener.py
  │     ├── src/engine/cross_validator.py
  │     │     ├── src/engine/cycle.py        (CycleSnapshot, CyclePhase)
  │     │     ├── src/engine/screener.py     (ScreenerHit)
  │     │     └── src/engine/leader_feedback.py (LeaderFeedback, LeaderSignal)
  │     ├── src/engine/leader_feedback.py
  │     ├── src/engine/ma_deviation.py
  │     ├── src/data/fetcher.py
  │     └── src/data/mock_data.py
  └── src/api/app.py             ← Web层
        ├── src/engine/cycle.py
        ├── src/engine/screener.py
        ├── src/engine/cross_validator.py
        └── src/engine/backtest.py
```

**依赖方向**：`API/调度层 → 引擎层 → 数据层 → 外部（AKShare / SQLite）`

引擎层之间的依赖：
- `cross_validator` → `cycle` + `screener` + `leader_feedback`（聚合三层信号）
- `backtest` → `cycle`（回测中运行状态机）
- 其他引擎模块相互独立

---

## 6. 技术栈

| 层 | 技术 | 版本要求 |
|----|------|----------|
| Web 框架 | FastAPI | >= 0.115.0 |
| ASGI 服务器 | Uvicorn | >= 0.30.0 |
| 前端 | Vue.js 3 (CDN) | 3.x |
| 数据处理 | Pandas + NumPy | >= 2.0 / >= 1.24 |
| 金融数据 | AKShare | >= 1.14.0 |
| 定时调度 | APScheduler | >= 3.10.0 |
| ORM | SQLAlchemy | >= 2.0.0 |
| 数据库 | SQLite (aiosqlite) | >= 0.20.0 |
| HTTP 客户端 | httpx | >= 0.27.0 |

---

## 7. 部署架构

```
┌───────────────────────────────────────────────┐
│                 单机部署                        │
│                                               │
│   python main.py                              │
│     │                                         │
│     ├── Uvicorn (0.0.0.0:8000)                │
│     │     └── FastAPI app                     │
│     │           ├── REST API (11 endpoints)   │
│     │           └── Static files (Vue.js)     │
│     │                                         │
│     └── APScheduler (BackgroundScheduler)     │
│           ├── cycle_update  (cron 周一~五 15:30)│
│           └── screener_update (cron 周一~五 9:27)│
│                                               │
│   data/                                       │
│     ├── quant.db (SQLite)                     │
│     └── *.json (运行时状态, 7个文件)            │
└───────────────────────────────────────────────┘
```

**启动命令**：

```bash
# 正常模式（连接东方财富数据）
python main.py

# Mock 模式（无网络环境测试）
MOCK=1 python main.py

# 启动并立即执行一次周期更新
python main.py --update

# 启动并立即执行一次选股
python main.py --screen

# 指定 Mock 场景
MOCK=1 MOCK_SCENARIO=full_cycle python main.py
```

**环境变量**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` | `0.0.0.0` | Web 服务监听地址 |
| `API_PORT` | `8000` | Web 服务端口 |
| `MOCK` | `0` | 是否强制 Mock 模式 |
| `MOCK_SCENARIO` | `small_cycle_start` | Mock 场景（6 种可选） |

---

## 8. 降级与容错

```
网络正常            网络异常 / MOCK=1
    │                      │
    ▼                      ▼
 AKShare API         mock_data.py
    │                      │
    ▼                      ▼
 真实市场数据         模拟数据（6种场景）
    │                      │
    └──────────┬───────────┘
               ▼
          引擎层正常运行
          （逻辑完全一致）
```

- **自动降级**：`scheduler.py` 中所有数据拉取函数均包含 try/except，失败时自动切换到 Mock
- **手动降级**：`MOCK=1` 环境变量强制使用模拟数据
- **状态恢复**：`CycleEngine` 启动时从 `cycle_state.json` 恢复上次状态
- **历史截断**：`cycle_history.json` 只保留最近 60 天，防止文件无限增长

---

## 9. 代码量统计

| 模块 | 文件 | 行数 | 职责 |
|------|------|------|------|
| 入口 | main.py | 72 | 初始化、调度、启动 |
| 配置 | config.py | 47 | 全局参数 |
| 调度 | scheduler.py | 400 | 编排两大定时任务 |
| 周期引擎 | cycle.py | 478 | 7阶段状态机 |
| 选股引擎 | screener.py | 220 | 7层筛选规则 |
| 交叉验证 | cross_validator.py | 147 | 3层 → 4级信号 |
| 龙头反馈 | leader_feedback.py | 199 | 5级情绪信号 |
| 偏离度 | ma_deviation.py | 163 | 240周线过热检测 |
| 回测 | backtest.py | 429 | 历史策略验证 |
| 数据拉取 | fetcher.py | 247 | AKShare 接口封装 |
| ORM | models.py | 98 | 4张数据库表 |
| 模拟数据 | mock_data.py | 162 | 6种场景 |
| API | app.py | 138 | 11个REST端点 |
| 前端 | index.html | 550 | Vue.js SPA |
| **合计** | **14 文件** | **~3,350 行** | |

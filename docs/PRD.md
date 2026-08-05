# AI 量化周期看板 — 产品需求文档 (PRD)

> **版本：2.0.0** | 日期：2026-06-03 | 状态：v1.2.0 已实现，v2.0 启动新增能力（持仓管理 + 实时盈亏 / 回测扩展多标的）
>
> **架构**：单体仓库 (Monorepo) · 单服务架构 · 单用户本地部署 · FastAPI + APScheduler + DuckDB (`quant.duckdb`, 单一后端) + Vue 3 静态看板

---

## 0. 产品 DNA（灵魂）

> 范蠡注：DNA 先于功能。先讲清楚它"是什么 / 不是什么"，再讲它做什么。

### 0.1 它是什么

**一个 9:27 准时出现的"克制看板"。**

每天早上 9:27 竞价结束那刻，它只说三句话：

1. **今天该不该开仓**（周期 + 龙头反馈）
2. **如果开，开哪几只、几层仓**（选股 + 交叉验证）
3. **如果不开，就是不开**（明确"不操作"是合法结果）

它把交易者从"9:30 开盘后看 K 线手指一抖就追"的本能里拉出来，用**事前已写好的纪律**替代**临场的人性博弈**。

### 0.2 它不是什么

| 不是什么 | 为什么不是 |
|----------|------------|
| 不是"实时盯盘"工具 | 盯盘是焦虑放大器，与克制哲学背道而驰 |
| 不是"全功能量化平台" | 越多功能越稀释纪律性。「做多源于不自信」 |
| 不是"信号更多 = 赚更多"的工具 | 恰恰相反：信号越少越克制，越克制越值钱 |
| 不是"替你做决定"的 AI | AI 是心智的自行车——它放大你的判断，绝不替你判断 |
| 不是"用算法操纵用户"的工具 | 今日不推荐，符合今日的"不操作"才是对用户最大的善良 |
| 不是"多用户 SaaS" | 多用户埋包袱会污染单用户体验。当前阶段坚守单用户 + 极简 |

### 0.3 核心隐喻

**「物理法则」而非「规章制度」。**

不写"早上 9 点必须看盘"这种规则——那是规章，会被绕过。
而是让看板在 9:27 自然出现，信号就是当下，不存在"绕过"。**涌现行为 > 强制约束。**

### 0.4 傻瓜测试

让一位 60 岁的奶奶打开 9:27 看板：

- 看到 3 个色块：**红（强信号，开仓）**、**黄（观察）**、**灰（今天不操作）**
- 如果是灰，她能**安心关掉**——知道今天不亏就是赢
- 如果是红，她能看到 1-3 个标的 + 仓位层数，**不需要再问任何人**

如果做不到这个测试，就还太复杂。

---

## 1. 目标与背景

### 1.1 目标

如果 v2.0 成功，它将交付以下 7 个"想要的结果"：

- **G1**：交易者在 9:30 开盘前**已**知道今日是否有信号、信号是什么
- **G2**：开仓决策**完全**基于系统信号，**无人工临场判断**
- **G3**：持仓过程中的盈亏**实时**对照信号纠偏（v2.0 新增）
- **G4**：所有历史选股的真实胜率、盈亏比可量化（v2.0 新增）
- **G5**：系统故障 / 异常情况下**降级而非崩溃**（Mock 模式、自动重试）
- **G6**：单用户本地部署，**零运维负担**（一次启动，天天可用）
- **G7**：所有阈值参数集中配置，**改参数不改代码**

### 1.2 背景

A股短线交易的核心矛盾是：

> **纪律需要 24 小时在场，但人只有 8 小时理智。**

盘中情绪、K 线异动、朋友推荐、新闻推送——所有这些都是"破坏纪律"的源头。市面上的量化工具要么太复杂（学习成本高）、要么太开放（信号太多反而焦虑）、要么是黑盒（不可信）。

本产品是**给"懂一点、但管不住手"的交易者**造的：把已经验证的交易规则（周期 + 选股 + 龙头反馈 + 仓位）固化成代码，用一个克制到极简的看板在 9:27 那一刻把决策**提前做完**，让用户在 9:30 开盘时只剩一个动作——**按信号执行，或不执行**。

v1.0–v1.2 已交付：周期状态机、7 层选股、龙头反馈、交叉验证、240 周线偏离度、回测、趋势选股、Web 看板、调度系统、**DuckDB `quant.duckdb` 单一后端存储**（业务数据 + 分析表 + 文档表）。

v2.0 补完最后的闭环：**持仓执行追踪 + 真实多标的历史回测**，让"信号 → 持仓 → 复盘"形成完整数据链。

### 1.3 变更日志

| 日期 | 版本 | 描述 | 作者 |
|------|------|------|------|
| 2026-05-26 | 1.2.0 | 调度时间校正/API扩展/三板组/午间排行/趋势回填链路 | — |
| 2026-06-03 | 2.0.0 | 整页重写：DNA 锚定 / Epic 路线图 / 持仓管理 Epic / 回测扩展 Epic / UX 愿景 | 范蠡 (PM Agent) |
| 2026-06-03 | 2.0.0 | 备份原 v1.2.0 → `docs/PRD.v1.2.0.md` | — |
| 2026-06-03 | 2.0.0.1 | 修正：v1.2 描述中残留的"JSON 双写"统一改为"DuckDB 单一后端"（与 `src/config.py` `DATA_STORAGE_BACKEND=quant` 一致）。感谢用户指正 | 范蠡 (PM Agent) |

---

## 2. 需求

### 2.1 功能性需求 (FR)

#### 已实现（v1.2 继承，参考备份文档 `docs/PRD.v1.2.0.md`）

- **FR1**：周期状态机（7 阶段：孕育/小周期启动/小周期完成/完整周期/余温/退潮/混沌）
- **FR2**：9:27 选股引擎（连板检测 + 7 层过滤 + 定向补齐 + 软过滤）
- **FR3**：高标龙头竞价反馈（5 级 + 一票否决）
- **FR4**：三层交叉验证（周期 × 龙头 × 选股 → 4 级信号 strong/normal/watch/avoid）
- **FR5**：240 周线偏离度过滤
- **FR6**：历史回测引擎（仅代表股开仓）
- **FR7**：数据层（httpx 直连东方财富/新浪/腾讯，**DuckDB `quant.duckdb` 单一后端**——`DATA_STORAGE_BACKEND=quant` 强制，6 场景 Mock 降级）
- **FR8**：APScheduler 调度（15:30 周期更新 + 9:27 选股）
- **FR9**：Web 看板（Vue 3 暗色 SPA，FastAPI REST，趋势 Tab，回测统计面板）
- **FR10**：趋势选股（明日观察池，每日 1 只，D+1/D+2 胜率回填）

#### v2.0 新增

- **FR11**：**持仓管理**（手动录入 + 自动对账 + 实时盈亏更新）
  - FR11.1 手动录入开仓（代码、买入价、买入时间、计划仓位、信号来源）
  - FR11.2 实时盈亏计算（基于最新行情，按分钟刷新）
  - FR11.3 持仓状态机（建仓 → 持有 → 止盈 / 止损 / 主动卖出 / 信号失效）
  - FR11.4 信号对照（持仓标的是否仍符合"今日信号"，触发"信号失效警告"）
  - FR11.5 止盈止损提醒（>+10% 黄色，<-5% 红色，临近阈值闪烁）
  - FR11.6 持仓历史归档 + 真实盈亏统计
- **FR12**：**多标的回测扩展**
  - FR12.1 从 `screener_history` 加载所有历史选股命中标的（含次新补缺）
  - FR12.2 逐日模拟"按信号开仓 + 真实止盈止损 + 持有 ≤3 天"
  - FR12.3 按周期阶段、信号等级、连板数分组的胜率/盈亏比
  - FR12.4 真实多标的 vs 仅代表股的对比曲线
  - FR12.5 单标的完整交易明细导出（CSV）

### 2.2 非功能性需求 (NFR)

| 编号 | 需求 |
|------|------|
| NFR1 | **网络断开时自动降级为 Mock 模式**，看板仍可读，不影响决策节奏 |
| NFR2 | 周期更新 < 30s；选股筛选 < 60s（含数据拉取） |
| NFR3 | 看板首次渲染 < 2s（本地静态资源 + 一次聚合 API） |
| NFR4 | **持仓盈亏刷新延迟 ≤ 60s**（v2.0 新增） |
| NFR5 | 所有阈值集中于 `src/config.py` 或 `data/config_overrides.json`，改配置不改代码 |
| NFR6 | 历史快照保留 ≥ 60 天，趋势/选股历史回填幂等 |
| NFR7 | **DuckDB 单写者约束**——避免多进程同时打开 `quant.duckdb` 库文件 |
| NFR8 | **零外部账户依赖**——不接微信/推送/云服务（v2.0 暂不开放小圈子） |
| NFR9 | 单用户本地部署，CPU/内存占用可控（峰值 < 500MB） |
| NFR10 | 持仓与回测模块**完全可选**——未启用时不影响原有 v1.2 流程 |

---

## 3. 用户界面设计目标

### 3.1 整体 UX 愿景

> **少即是克制。克制即是赚钱。**

视觉设计原则：

- **3 种颜色讲完所有事**：
  - 灰色 = 不操作（背景态）
  - 黄色 = 观察（信号弱、仓位小）
  - 红色 = 强信号（开仓窗口）
  - 绿色**只**用于"已盈利"标记，**绝不**用于"信号"
- **数字不超过 7 个 / 屏**（开盘前人脑容量有限）
- **零弹窗广告 / 零推送 / 零提示"今日已上涨 X% 赶紧买"**（反操纵）
- **明确的"今天不操作"是合法结果**——不写"暂无信号"，直接写"今日不开仓"
- **历史只回溯最近 5 个交易日**——再老的让用户主动点"查看历史"

### 3.2 核心交互范式

| 范式 | 说明 |
|------|------|
| **9:27 主动出现** | 看板在 9:27 自动刷新（不需要用户点刷新按钮） |
| **一眼决策** | 进入看板 ≤ 3 秒看清"今天怎么办" |
| **零层级** | 首页 = 决策页，不存在"先点哪个 tab"的问题 |
| **历史可下钻** | 点击任一标的 → 弹出 K 线 / 分时 / 历史选股记录（不跳页） |
| **持仓常驻** | 已开仓后，顶部多一条"持仓状态条"，避免被新信号干扰 |

### 3.3 核心页面与视图

| 页面 | 描述 | 优先级 |
|------|------|--------|
| **首页看板** (`/`) | 9:27 决策卡：周期状态 + 龙头反馈 + 今日选股 + 信号 | P0 |
| **持仓面板** (`/position`) | v2.0 新增：实时盈亏、止盈止损线、信号对照 | P0 |
| **趋势选股 Tab** (`/?tab=trend`) | 已有：明日观察池 + 历史胜率 | P1 |
| **回测统计** (`/?tab=backtest`) | v2.0 扩展：多标的 vs 代表股对比 | P1 |
| **决策历史** (`/?tab=history`) | 最近 5 日决策时间线 | P2 |
| **K 线弹窗** | 已有：点击标的 → 弹窗看日 K / 分时 / 历史选股 | P0 |

### 3.4 无障碍

**无**（内部自用工具，无需 WCAG 适配；保持屏幕阅读器可读即可）

### 3.5 目标设备与平台

**仅桌面端**（14 寸以上显示器，本地浏览器访问 `http://localhost:PORT`）

---

## 4. 技术假设

### 4.1 仓库结构

**Monorepo（单体仓库）**——本项目即为典型代表。

### 4.2 服务架构

**单体架构**——FastAPI 单进程，APScheduler 内嵌调度，零微服务复杂度。

### 4.3 测试要求

| 层级 | 范围 |
|------|------|
| 单元测试 | 引擎层核心函数（cycle/screener/cross_validator/leader_feedback） |
| 集成测试 | 选股端到端（Mock 数据 → 选股 → 交叉验证 → DuckDB 落库） |
| 手动验证 | 每日 9:27 / 15:30 看板目视确认 |
| 回归 | 选股历史补缺 / 趋势历史回填幂等性 |

### 4.4 其他技术假设

- **语言/框架**：Python 3.11+ · FastAPI · httpx · APScheduler · DuckDB（替代旧 SQLite）· Vue 3（CDN 单页）
- **数据源**：东方财富（httpx）/ 新浪（httpx）/ 腾讯分时（httpx），**不**依赖 AKShare（v1.0 已部分淘汰）
- **前端**：保持 Vue 3 CDN 单页架构，**不引入** Webpack/Vite（增加部署复杂度）
- **持久化**：业务数据 / 文档表 / 分析表统一写入 `quant.duckdb`（单一后端，由 `src/config.py` 强制 `DATA_STORAGE_BACKEND=quant`）；分时按 `seq` 落库
- **持仓实时性**：v2.0 持仓盈亏复用现有 `realtime_df` 拉取链路，**不引入新数据源**
- **回测性能**：多标的历史回测预计 1-3 分钟可接受；超过 5 分钟需分块或缓存
- **零云依赖**：v2.0 不接推送、不接云端账号

---

## 5. Epic 列表

> 范蠡注：Epic 必须逻辑顺序、每条独立可交付。新增 Epic（11、12）即 v2.0 重心。Epic 1-10 是 v1.2 现状资产（详见备份 PRD 与 docs/ARCHITECTURE.md）。

| # | Epic 标题 | 状态 | 一句话目标 |
|---|-----------|------|-----------|
| 1 | 周期状态机 | ✅ v1.0 | 7 阶段状态机，15:30 滚动识别 |
| 2 | 9:27 选股引擎 | ✅ v1.0 | 连板 + 7 层 + 软过滤 + 定向补齐 |
| 3 | 龙头反馈 | ✅ v1.0 | 5 级 + 一票否决 |
| 4 | 交叉验证 | ✅ v1.0 | 4 级信号 + 仓位建议 |
| 5 | 240 周线偏离度 | ✅ v1.0 | 过热过滤 |
| 6 | 历史回测（代表股） | ✅ v1.0 | 已实现（仅代表股开仓） |
| 7 | 数据层 | ✅ v1.0 | httpx 直连 + DuckDB + Mock 降级 |
| 8 | 调度系统 | ✅ v1.0 | APScheduler + 手动触发 |
| 9 | Web 看板 | ✅ v1.0 | Vue 3 SPA + 趋势 Tab + 决策卡 |
| 10 | 趋势选股 | ✅ v1.1 | 明日观察池 + D+1/D+2 胜率回填 |
| **11** | **持仓管理 + 实时盈亏** | 🆕 v2.0 | **闭环：从信号 → 持仓 → 真实盈亏** |
| **12** | **多标的回测扩展** | 🆕 v2.0 | **真实胜率：所有历史选股命中的多标的全量回测** |

---

## 6. Epics（YAML 详细规格）

> Epic 1-10 详见 `docs/PRD.v1.2.0.md` 备份文档与 `docs/ARCHITECTURE.md`，其实现与代码位置一一对应。下面只详写 v2.0 的 **Epic 11 与 Epic 12**（这是 PRD 的核心增量），其余 Epic 1-10 通过 reuse_analysis 引用现有实现。

---

## Epic 11: 持仓管理 + 实时盈亏

**Epic 概述:** 把"信号 → 持仓 → 真实盈亏"形成完整数据闭环。用户按信号开仓后，系统自动跟踪盈亏、信号失效、止盈止损。**不接管交易执行**——只做"记录 + 提醒 + 复盘"，保留用户最终决策权。

**目标仓库:** monolith（前后端同仓）

```yaml
epic_id: 11
title: "持仓管理 + 实时盈亏"
description: |
  闭环交易系统的最后一段：信号 → 持仓记录 → 实时盈亏 → 归档复盘。
  用户在 9:27 看到信号后，可手动录入开仓；系统按分钟级拉取最新行情计算盈亏；
  持仓标的若不再符合"今日信号"，触发"信号失效警告"；
  触发止盈/止损阈值时高亮提醒；收盘后归档为历史持仓并贡献真实盈亏统计。
  
  关键设计：不接管券商交易 API（避免合规风险 + 复杂度），
  只做"用户的交易笔记本 + 智能提醒器"。

reuse_analysis:
  directly_reusable:
    - component: "实时行情拉取链路"
      location: "src/data/fetcher.py + src/data/sina_spot_api.py"
      capability: "全市场实时快照，包含最新价、开盘价、涨跌"
      usage: "持仓盈亏刷新直接复用此链路（≤60s 间隔）"
    - component: "选股引擎输出 ScreenerHit"
      location: "src/engine/screener.py"
      capability: "命中标的的代码、名称、连板数、竞价指标"
      usage: "录入开仓时记录 signal_source，关联到具体 screener run"
    - component: "信号等级 4 级（strong/normal/watch/avoid）"
      location: "src/engine/cross_validator.py"
      capability: "持仓标的对照当日信号"
      usage: "signal_still_valid 判定"
    - component: "DuckDB 文档表（单一后端）"
      location: "src/data/models.py + src/ledger_doc_store.py"
      capability: "持久化模式（业务数据 + 文档表 + 分析表统一 quant 库）"
      usage: "持仓表、持仓历史表按相同模式"

  requires_extension:
    - component: "Web 看板首页"
      location: "src/static/index.html"
      current_capability: "决策卡 + 选股表 + 趋势 Tab"
      extension_needed: "顶部增加'持仓状态条'（有持仓时显示）+ 持仓 Tab/页面"
      affected_stories: ["11.1", "11.4"]
    - component: "调度器 main.py"
      location: "main.py"
      current_capability: "周期更新 15:30 + 选股 9:27"
      extension_needed: "持仓盈亏刷新任务（交易时段每 60s 一次）"
      affected_stories: ["11.2"]

  conflicts:
    - component: "9:27 选股一次性输出"
      location: "src/engine/screener.py"
      conflict: "持仓开仓时间可能晚于 9:27（如盘中买入）"
      resolution: "持仓模块不依赖 9:27 选股，单独维护持仓表；可手动录入任何时点的开仓"
      affected_stories: ["11.1", "11.3"]

  new_implementations:
    - feature: "Position 引擎"
      suggested_location: "src/engine/position.py"
      pattern_reference: "src/engine/screener.py（同样的 dataclass + 纯函数风格）"
      affected_stories: ["11.1", "11.2", "11.3"]
    - feature: "持仓 REST API"
      suggested_location: "src/api/app.py（追加路由）"
      pattern_reference: "已有 /api/screener-history 模式"
      affected_stories: ["11.4"]
    - feature: "持仓 UI 组件"
      suggested_location: "src/static/index.html（持仓 Tab）"
      pattern_reference: "已有 Tab 切换模式（趋势 Tab）"
      affected_stories: ["11.4"]

stories:
  - id: "11.1"
    title: "手动录入开仓（Position 创建 API）"
    repository_type: monolith
    estimated_complexity: medium
    priority: P0
    acceptance_criteria:
      - id: AC1
        title: "成功录入一笔开仓"
        scenario:
          given: "用户当日已收到 strong 信号（标的 600123，价格 10.50）"
          when: "用户调用 POST /api/positions {code, buy_price, shares, signal_source, planned_position_layers}"
          then:
            - "创建 Position 记录，status=open"
            - "记录关联当日 screener run 的 signal_source"
            - "返回 201 + 完整 Position 对象"
        business_rules:
          - id: "BR-1.1"
            rule: "同一 (code, trade_date) 只能有一笔 open 持仓（防重复录入）"
          - id: "BR-1.2"
            rule: "buy_price > 0，shares > 0 且为 100 的整数倍（A 股交易单位）"
          - id: "BR-1.3"
            rule: "signal_source 可空（允许纯手动开仓），但若非空需存在于 screener_history"
        data_validation:
          - field: "code"
            type: "string"
            required: true
            rules: "6 位数字 A 股代码"
            error_message: "代码格式错误"
          - field: "buy_price"
            type: "number"
            required: true
            rules: "> 0, 精度 0.01"
            error_message: "买入价必须 > 0"
          - field: "shares"
            type: "number"
            required: true
            rules: ">= 100, 100 的倍数"
            error_message: "股数必须 ≥ 100 且为 100 倍数"
          - field: "planned_position_layers"
            type: "number"
            required: true
            rules: "1-9 整数"
            error_message: "计划仓位层数须在 1-9 之间"
        error_handling:
          - scenario: "同一 code 当日已有 open 持仓"
            code: "409"
            message: "该代码今日已有未平仓记录"
            action: "返回现有持仓，提示平仓后再开新仓"
          - scenario: "signal_source 不在 screener_history"
            code: "400"
            message: "signal_source 无效"
            action: "允许继续（用户可能事后补录）但标记 signal_mismatch=true"
        examples:
          - input: |
              POST /api/positions
              {"code":"600123","buy_price":10.50,"shares":1000,
               "signal_source":"screener_2026-06-03_927","planned_position_layers":6}
            expected: |
              201 Created
              {"id":42,"code":"600123","status":"open",
               "buy_price":10.50,"shares":1000,"signal_source":"screener_2026-06-03_927",
               "planned_position_layers":6,"created_at":"2026-06-03T09:35:12"}
          - input: |
              POST /api/positions
              {"code":"600123","buy_price":10.50,"shares":1000}  # 当日已有 open
            expected: |
              409 Conflict
              {"error":"该代码今日已有未平仓记录","existing_position_id":38}

      - id: AC2
        title: "录入后立即计算成本与初始盈亏"
        scenario:
          given: "成功创建 Position 后"
          when: "系统从最新行情快照读取当前价"
          then:
            - "Position 包含 current_price, current_pnl, current_pnl_pct"
            - "若行情不可用则标记 price_stale=true（不抛错）"
        business_rules:
          - id: "BR-2.1"
            rule: "盈亏 = (current_price - buy_price) * shares"
          - id: "BR-2.2"
            rule: "盈亏% = (current_price / buy_price - 1) * 100"
        error_handling:
          - scenario: "当前价不可用（停牌 / 数据延迟）"
            code: "200"
            message: "持仓已创建，但价格未更新"
            action: "price_stale=true，下一次刷新任务补齐"
    provides_apis:
      - "POST /api/positions"
      - "GET /api/positions"
    consumes_apis: []
    dependencies: []
    sm_hints:
      front_end_spec: null
      architecture:
        files:
          - "src/engine/position.py（新建）"
          - "src/api/app.py（追加路由）"
          - "src/data/position_store.py（新建）"

  - id: "11.2"
    title: "实时盈亏刷新任务"
    repository_type: monolith
    estimated_complexity: medium
    priority: P0
    acceptance_criteria:
      - id: AC1
        title: "交易时段每 60s 刷新一次所有 open 持仓的当前价与盈亏"
        scenario:
          given: "当前有 3 笔 open 持仓"
          when: "调度器触发 refresh_positions_pnl 任务（每个交易日 9:30-15:00 每 60s）"
          then:
            - "每笔持仓的 current_price / current_pnl / current_pnl_pct 被更新"
            - "刷新耗时 < 5s（仅更新持仓对应的少量股票）"
            - "更新失败时不抛错，标记 price_stale=true"
        business_rules:
          - id: "BR-1.1"
            rule: "仅在交易日 9:30-15:00 运行；非交易时段不刷新"
          - id: "BR-1.2"
            rule: "同一持仓 60s 内不重复刷新（防抖）"
          - id: "BR-1.3"
            rule: "止盈/止损阈值触发时标记 alert_level=yellow/red"
        data_validation: []
        error_handling:
          - scenario: "行情 API 失败"
            code: "LOG_WARN"
            message: "持仓盈亏刷新失败，标记 price_stale"
            action: "下一周期重试；连续 3 次失败后降级为不刷新（不阻塞看板）"
        interaction: []
        examples:
          - input: "持仓 600123，buy_price=10.50，shares=1000"
            expected: "current_price=10.85 → pnl=+350, pnl_pct=+3.33%, alert_level=none"
          - input: "持仓 600456，buy_price=20.00，shares=500"
            expected: "current_price=21.50 → pnl_pct=+7.5% → alert_level=yellow（接近止盈 10%）"
    provides_apis: []
    consumes_apis:
      - "GET /api/positions（内部使用）"
    dependencies:
      - "11.1"
    sm_hints:
      front_end_spec: null
      architecture:
        files:
          - "src/engine/position.py（追加 refresh_pnl 函数）"
          - "main.py（追加 APScheduler 任务）"

  - id: "11.3"
    title: "信号对照 + 状态机（持有/平仓/失效）"
    repository_type: monolith
    estimated_complexity: high
    priority: P0
    acceptance_criteria:
      - id: AC1
        title: "持仓标的是否仍符合当日信号"
        scenario:
          given: "用户持有 600123（昨日 strong 信号买入），今日 9:27 选股 600123 不再命中"
          when: "调度器或用户主动触发 check_position_signal_validity"
          then:
            - "该持仓被标记 signal_still_valid=false"
            - "生成警告：'持仓已偏离信号，建议审视'"
        business_rules:
          - id: "BR-1.1"
            rule: "信号对照基于当日 screener run + cross_validator 输出"
          - id: "BR-1.2"
            rule: "avoid 等级持仓默认提示立即审视；watch 提示观察"
          - id: "BR-1.3"
            rule: "信号对照仅在交易日 9:27 后执行"
        error_handling:
          - scenario: "当日 screener 未运行（非交易日 / 失败）"
            code: "SKIP"
            message: "今日无信号数据，跳过对照"
            action: "保持 signal_still_valid=unknown"

      - id: AC2
        title: "平仓 API（手动标记卖出）"
        scenario:
          given: "持仓 600123 当前盈利 +5%"
          when: "用户调用 POST /api/positions/{id}/close {sell_price, sell_shares}"
          then:
            - "持仓 status=closed，记录 realized_pnl"
            - "持仓历史归档（保留近 60 天）"
        business_rules:
          - id: "BR-2.1"
            rule: "可部分平仓（sell_shares < shares → status=partial_close）"
          - id: "BR-2.2"
            rule: "realized_pnl = (sell_price - buy_price) * sell_shares - 手续费（暂按 0 算）"
          - id: "BR-2.3"
            rule: "止盈止损自动平仓：current_price 触发阈值时建议平仓，但需用户确认（系统不自动执行）"
        data_validation:
          - field: "sell_price"
            type: "number"
            required: true
            rules: "> 0, 精度 0.01"
            error_message: "卖出价必须 > 0"
          - field: "sell_shares"
            type: "number"
            required: true
            rules: "1-shares 之间, 100 的倍数"
            error_message: "卖出股数无效"
        examples:
          - input: "POST /api/positions/42/close {sell_price: 11.00, sell_shares: 1000}"
            expected: |
              200 OK
              {"id":42,"status":"closed","realized_pnl":+500,"closed_at":"2026-06-04T10:15:33"}

      - id: AC3
        title: "止盈止损阈值提醒（不自动平仓）"
        scenario:
          given: "持仓 600123 buy_price=10.00, planned_take_profit=+10%, planned_stop_loss=-5%"
          when: "current_price 触及 11.00（止盈）或 9.50（止损）"
          then:
            - "alert_level 升级为 red"
            - "看板持仓状态条闪烁/高亮"
            - "建议文案：'已达止盈阈值，请考虑平仓'"
        business_rules:
          - id: "BR-3.1"
            rule: "止盈止损阈值默认 +10% / -5%（可在 Position 创建时自定义）"
          - id: "BR-3.2"
            rule: "系统绝不自动平仓——只提醒，由用户决定"
    provides_apis:
      - "POST /api/positions/{id}/close"
      - "GET /api/positions/{id}/signal-validity"
    consumes_apis:
      - "GET /api/screener"
      - "GET /api/signals"
    dependencies:
      - "11.1", "11.2"
    sm_hints:
      front_end_spec: null
      architecture:
        files:
          - "src/engine/position.py（追加状态机 + 信号对照）"

  - id: "11.4"
    title: "持仓 UI（首页状态条 + 持仓 Tab）"
    repository_type: monolith
    estimated_complexity: medium
    priority: P0
    acceptance_criteria:
      - id: AC1
        title: "首页持仓状态条（始终可见）"
        scenario:
          given: "用户有 2 笔 open 持仓"
          when: "打开首页 / 任何 Tab 切换时"
          then:
            - "顶部多出持仓状态条：总市值 / 总盈亏 / 持仓数 / 最近预警"
            - "点击状态条 → 跳转到持仓 Tab 详情"
        business_rules:
          - id: "BR-1.1"
            rule: "无持仓时状态条折叠为'当前无持仓'"
          - id: "BR-1.2"
            rule: "总盈亏为负时状态条变红（克制设计：避免绿色暗示'赚钱就好'）"
        interaction:
          - trigger: "持仓盈亏更新"
            behavior: "数字滚动动画（不刷新整页）"
          - trigger: "触发止盈止损"
            behavior: "状态条对应持仓行闪烁 + 角标"

      - id: AC2
        title: "持仓 Tab 详情页"
        scenario:
          given: "进入持仓 Tab"
          when: "查看持仓列表"
          then:
            - "列出所有 open / recent-closed 持仓"
            - "每行显示：代码/名称/买入价/当前价/盈亏/状态/信号对照/止盈止损线"
            - "支持点击 K 线弹窗（复用现有弹窗）"
            - "支持'平仓'按钮 → 弹窗录入卖出价"
        examples:
          - input: "持仓 Tab 渲染"
            expected: |
              | 代码  | 名称 | 买入价 | 当前价 | 盈亏%   | 状态 | 信号对照 | 止盈止损    |
              | 600123 | XX  | 10.50  | 10.85  | +3.33% | 持有 | 仍有效   | +10% / -5%  |
              | 600456 | YY  | 20.00  | 19.20  | -4.00% | 持有 | 已失效   | +10% / -5% ⚠ |
    provides_apis: []
    consumes_apis:
      - "GET /api/positions"
      - "POST /api/positions/{id}/close"
    dependencies:
      - "11.1", "11.2", "11.3"
    sm_hints:
      front_end_spec:
        file: "docs/ux/position-ui-spec.md"
        sections:
          - "持仓状态条组件"
          - "持仓 Tab 列表"
          - "平仓弹窗"
      architecture:
        files:
          - "src/static/index.html（追加持仓 Tab + 状态条）"
```

---

## Epic 12: 多标的回测扩展

**Epic 概述:** 把回测从"仅代表股开仓"扩展为"所有历史选股命中标的的多标的全量回测"，输出真实胜率、盈亏比、按周期阶段/信号等级/连板数分组统计、单标的明细导出。**不重写现有回测引擎**——在 v1.0 backtest.py 旁并行 v2 多标的模块。

**目标仓库:** monolith

```yaml
epic_id: 12
title: "多标的回测扩展"
description: |
  把历史选股命中标的（screener_history）作为真实交易池，按 v1.0 交易规则
  （信号 → 开仓 → 止盈/止损/持有≤3天）逐日回放，输出真实多标的的胜率、盈亏比。
  
  关键差异 vs v1.0：
  - v1.0 仅回测"代表股"（cycle 代表股，1 只）
  - v2.0 回测 screener_history 中所有命中标的（典型每天 1-10 只）
  
  真实多标的回测将回答一个核心问题：
  「如果用户严格按系统信号开仓，过去 60 天的真实收益是多少？」

reuse_analysis:
  directly_reusable:
    - component: "现有 backtest.py 回测引擎"
      location: "src/engine/backtest.py（429 行）"
      capability: "逐日回放、止盈止损、权益曲线、按周期阶段统计"
      usage: "v2.0 多标的模块调用其核心函数，传入不同的持仓池"
    - component: "screener_history 归档数据"
      location: "data/screener_history.json + DuckDB screener_history_entry 表"
      capability: "每日选股命中标的 + 收盘价 + 次日开盘/收盘"
      usage: "作为回测的'历史信号池'"
    - component: "交易规则参数"
      location: "src/config.py（止盈+10%, 止损-5%, 持有≤3天）"
      capability: "回测规则定义"
      usage: "v2.0 沿用相同参数"
    - component: "回测统计输出"
      location: "src/engine/backtest.py + src/static/index.html（回测 Tab）"
      capability: "胜率、盈亏比、最大回撤等指标展示"
      usage: "v2.0 在同一面板展示 v1.0 + v2.0 对比"

  requires_extension:
    - component: "回测面板 UI"
      location: "src/static/index.html（回测 Tab）"
      current_capability: "显示 v1.0 仅代表股回测结果"
      extension_needed: "增加'多标的 vs 代表股'对比视图 + 分组切换 + 明细导出"
      affected_stories: ["12.3", "12.4"]
    - component: "screener_history 补缺逻辑"
      location: "src/engine/screener_history.py"
      current_capability: "按日 K 幂等补 close_price / next_day_open"
      extension_needed: "回测前批量校验：缺失 > 5% 则提示先跑补缺"
      affected_stories: ["12.1"]

  conflicts:
    - component: "回测起始资金 / 仓位分配"
      location: "src/engine/backtest.py"
      conflict: "v1.0 代表股 6 层仓位 vs v2.0 多标的每只仓位如何分？"
      resolution: "v2.0 沿用 planned_position_layers 字段（screener 命中时已记录）；如无则默认 3 层"
      affected_stories: ["12.2"]

  new_implementations:
    - feature: "Multi-stock backtest 引擎"
      suggested_location: "src/engine/multi_backtest.py"
      pattern_reference: "src/engine/backtest.py（同样 dataclass + 逐日循环）"
      affected_stories: ["12.2"]
    - feature: "明细导出 API"
      suggested_location: "src/api/app.py（追加 /api/backtest/multi-stock/export）"
      pattern_reference: "已有 /api/backtest 模式"
      affected_stories: ["12.4"]
    - feature: "对比曲线 UI"
      suggested_location: "src/static/index.html（回测 Tab 新增对比图）"
      pattern_reference: "已有 Chart.js 用法（如有）或简单 SVG 折线"
      affected_stories: ["12.3"]

stories:
  - id: "12.1"
    title: "历史选股池准备（screener_history 校验 + 补缺）"
    repository_type: monolith
    estimated_complexity: low
    priority: P0
    acceptance_criteria:
      - id: AC1
        title: "校验 screener_history 完整性"
        scenario:
          given: "用户触发多标回测"
          when: "后端加载最近 60 天 screener_history"
          then:
            - "统计缺失 close_price / next_day_open 的记录数"
            - "若缺失 > 5% 自动调用 repair_missing_close_prices 补缺"
            - "若补缺后仍缺失 > 5% 提示用户先手动跑补缺"
        business_rules:
          - id: "BR-1.1"
            rule: "回测时间窗口默认最近 60 个交易日（可在请求中指定）"
          - id: "BR-1.2"
            rule: "缺失 close_price 的记录在回测中标记为 skipped"
        error_handling:
          - scenario: "缺失 > 10%"
            code: "PRECONDITION_FAILED"
            message: "数据缺失过多，请先执行补缺"
            action: "返回 412 + 缺失明细"
        examples:
          - input: "GET /api/backtest/multi-stock/prepare"
            expected: |
              {"total_records": 120, "missing_close": 3, "missing_next_day": 5,
               "ready": true, "auto_filled": 5}
    provides_apis:
      - "GET /api/backtest/multi-stock/prepare"
    consumes_apis: []
    dependencies: []
    sm_hints:
      front_end_spec: null
      architecture:
        files:
          - "src/engine/multi_backtest.py（新建）"

  - id: "12.2"
    title: "多标的逐日回放（核心回测）"
    repository_type: monolith
    estimated_complexity: high
    priority: P0
    acceptance_criteria:
      - id: AC1
        title: "完整回放 60 天所有历史命中标的"
        scenario:
          given: "screener_history 完整（120 条记录，每条 1-5 只标的）"
          when: "调用 POST /api/backtest/multi-stock {window_days: 60, initial_capital: 1000000}"
          then:
            - "逐日模拟：当日 screener 命中 → 次日开盘按信号开仓 → 持有至止盈/止损/3 天"
            - "输出：总交易数、胜率、盈亏比、平均盈利%、平均亏损%、最大回撤、最终权益"
        business_rules:
          - id: "BR-1.1"
            rule: "开仓价 = 次日 open（来自 backfill_next_day_auction 或日 K）"
          - id: "BR-1.2"
            rule: "平仓优先级：止盈（>+10%）> 止损（<-5%）> 持有满 3 天强制平仓"
          - id: "BR-1.3"
            rule: "仓位：每只标的按 planned_position_layers * 1000 元底仓，剩余等权"
          - id: "BR-1.4"
            rule: "同一天最多持仓 5 只（满仓后新信号跳过，避免过度分散）"
        data_validation: []
        error_handling:
          - scenario: "回测超时（> 5 分钟）"
            code: "TIMEOUT"
            message: "回测超时，建议缩短时间窗口"
            action: "返回部分结果 + 进度百分比"
        examples:
          - input: "POST /api/backtest/multi-stock {window_days: 60}"
            expected: |
              {
                "summary": {
                  "total_trades": 87,
                  "win_rate": 0.563,
                  "profit_factor": 1.42,
                  "avg_pnl_pct": 0.024,
                  "max_drawdown": -0.082,
                  "final_equity": 1187200,
                  "total_return_pct": 0.187
                },
                "by_cycle_phase": {...},
                "by_signal_level": {...},
                "by_continuous_limit_up": {...}
              }
    provides_apis:
      - "POST /api/backtest/multi-stock"
    consumes_apis: []
    dependencies:
      - "12.1"
    sm_hints:
      front_end_spec: null
      architecture:
        files:
          - "src/engine/multi_backtest.py（核心）"
          - "src/api/app.py（追加路由）"

  - id: "12.3"
    title: "分组统计 + 对比曲线"
    repository_type: monolith
    estimated_complexity: medium
    priority: P0
    acceptance_criteria:
      - id: AC1
        title: "按多维度分组统计"
        scenario:
          given: "多标回测已跑完"
          when: "看板回测 Tab 加载多标回测结果"
          then:
            - "按周期阶段分组（7 阶段 × 胜率/盈亏比）"
            - "按信号等级分组（strong/normal/watch）"
            - "按连板数分组（2/3/4/5+）"
            - "v1.0 仅代表股 vs v2.0 多标的对比表"
        business_rules:
          - id: "BR-1.1"
            rule: "样本数 < 5 的分组标记为'样本不足'不显示具体数字"
        interaction: []
        examples:
          - input: "按周期阶段分组"
            expected: |
              | 阶段       | 交易数 | 胜率 | 盈亏比 |
              | 完整周期    | 42     | 0.62 | 1.85   |
              | 小周期完成  | 28     | 0.54 | 1.32   |
              | 余温期      | 12     | 0.42 | 0.91   |
              | 退潮期      | 5      | 样本不足 | - |

      - id: AC2
        title: "v1.0 vs v2.0 对比曲线"
        scenario:
          given: "v1.0 代表股回测 + v2.0 多标回测均存在"
          when: "用户在回测 Tab 切换对比模式"
          then:
            - "同一图表上画两条权益曲线（v1.0 蓝 / v2.0 红）"
            - "显示关键节点：最大回撤点、最高权益点"
        examples:
          - input: "对比模式开启"
            expected: "曲线图：v1.0 终值 105.2 万 / v2.0 终值 118.7 万，v2.0 优势 +13.5%"
    provides_apis: []
    consumes_apis:
      - "GET /api/backtest"
      - "GET /api/backtest/multi-stock"
    dependencies:
      - "12.2"
    sm_hints:
      front_end_spec:
        file: "docs/ux/multi-backtest-spec.md"
        sections:
          - "分组统计表"
          - "对比曲线组件"
      architecture:
        files:
          - "src/static/index.html（回测 Tab 扩展）"

  - id: "12.4"
    title: "单标交易明细导出（CSV）"
    repository_type: monolith
    estimated_complexity: low
    priority: P1
    acceptance_criteria:
      - id: AC1
        title: "导出完整交易明细为 CSV"
        scenario:
          given: "多标回测完成"
          when: "用户点击'导出明细'按钮"
          then:
            - "下载 CSV：trade_date, code, name, signal_source, buy_price, sell_price, hold_days, pnl, pnl_pct, exit_reason, cycle_phase"
        business_rules:
          - id: "BR-1.1"
            rule: "文件名格式：multi_backtest_YYYYMMDD_YYYYMMDD.csv"
          - id: "BR-1.2"
            rule: "包含全部交易记录，无遗漏"
        examples:
          - input: "点击'导出明细'"
            expected: |
              trade_date,code,name,signal_source,buy_price,sell_price,hold_days,pnl,pnl_pct,exit_reason,cycle_phase
              2026-04-01,600123,XX,screen_2026-04-01,10.50,11.05,2,550,5.24,take_profit,完整周期
              2026-04-01,600456,YY,screen_2026-04-01,20.00,19.00,1,-1000,-5.00,stop_loss,完整周期
    provides_apis:
      - "GET /api/backtest/multi-stock/export"
    consumes_apis: []
    dependencies:
      - "12.2"
    sm_hints:
      front_end_spec: null
      architecture:
        files:
          - "src/api/app.py（追加 export 路由）"
```

---

## 7. 检查清单结果报告

> 待 v2.0 实施完成后由 `pm-checklist` 工作流填写。当前为占位。

| 检查项 | 状态 |
|--------|------|
| DNA 锚定（产品是生命体，不是功能清单） | ✅ v2.0 文档第 0 章 |
| Goals 与 Background Context | ✅ 第 1.1 / 1.2 节 |
| FR / NFR 完整且可验收 | ✅ 第 2.1 / 2.2 节 |
| UX 愿景（核心页面 + 交互范式） | ✅ 第 3 章 |
| 技术假设（含测试要求） | ✅ 第 4 章 |
| Epic 列表顺序合理 | ✅ 第 5 章 |
| Epic 故事卡含 GIVEN/WHEN/THEN + 业务规则 + 错误处理 | ✅ 第 6 章（Epic 11、12） |
| reuse_analysis 复用现有实现 | ✅ Epic 11、12 头部分析 |
| 旧版本备份 | ✅ `docs/PRD.v1.2.0.md` |

---

## 8. 下一步

### 8.1 UX Expert 提示

> 若涉及 UI 改动（v2.0 涉及：持仓状态条、持仓 Tab、回测对比曲线），建议拉入 UX Expert：
> `/o ux-expert --lang=zh`，输入：基于 `docs/PRD.md` v2.0 的 Epic 11.4 与 12.3 故事卡，输出 `docs/ux/position-ui-spec.md` 与 `docs/ux/multi-backtest-spec.md` 详细前端规格。

### 8.2 Architect 提示

> 拉入架构师审阅 v2.0 新增的 `src/engine/position.py`、`src/engine/multi_backtest.py`、`src/api/app.py` 新增路由，确保不破坏 v1.2 现有数据流：
> `/o architect --lang=zh`，输入：基于 `docs/PRD.md` v2.0 的 Epic 11、12，输出 `docs/architecture/position-module.md` 与 `docs/architecture/multi-backtest-module.md` 实施设计。

# Iteration Scope: 邮件推送内容对齐首页看板

## 背景

实盘 9:27 选股决策推送邮件（QQ 邮箱 604491810@qq.com）由 `src/notify/email_sender.py` 渲染。
首页看板 `src/static/index.html` 已迭代到"四维警戒 + 连续好情绪升仓 4 层"逻辑，
但邮件停留在"三维警戒 + 固定 3-6 层"旧版，导致用户在邮箱看到的决策与浏览器看板**不一致**。

**目标**: 邮件推送的决策算法与指标格内容**逐字段对齐 dashboard**，作为唯一真源避免分叉。

## 范围（共 8 项不一致，全部对齐）

### 🔴 Class A — 决策算法核心（必须对齐，影响实盘判断）

| # | 项目 | Dashboard 现状（真源） | Email 当前 | 修复方向 |
|---|---|---|---|---|
| A1 | **第四维：跌幅>9%个股数** | `index.html:1218-1220` 计算 `dropOver9 = market.drop_over_9pct`，`dropBad = dropOver9 > 9` | `email_sender.py:_calc_daily_advice` 完全没算这维度 | 在 `_calc_daily_advice` 中加 `drop_bad` 判定 + 警戒文案 |
| A2 | **连续 2 日情绪好 → 升 4 层** | `index.html:1252-1267`：当 `todayGood && prevGood`（跌停≤5 且 加权竞价≥0，今昨皆是）→ 仓位 "4层（连续情绪良好）" | 邮件 go 分支固定返回 "3-6 层（标准仓位）" | 引入 `prev_day_*` 数据，输出 "4层" 或 "3层" |
| A3 | **谨慎参与文案** | "1.5层（小仓试错）" | "1-2 层（小仓试错）" | 改文案 |
| A4 | **可参与文案** | "3层（标准仓位）" 或 "4层（连续情绪良好）" | "3-6 层（标准仓位）" | 改文案 |
| A5 | **reason 中维度数** | "四维警戒中已 N 项触发，避免开仓。" | "三维警戒中已 N 项触发，避免开仓。" | 改文案 |

### 🟡 Class B — 指标格 UI / 数据维度（次要，与首页阅读体验对齐）

| # | 项目 | Dashboard 现状 | Email 当前 | 修复方向 |
|---|---|---|---|---|
| B6 | **第 1 格双数显示** | `index.html:513-530`：标签 "竞价跌停 (>5⚠) / 跌>9% (>9⚠)"，主值 `limit_down / drop_over_9pct`，附 ↑↓→ 箭头与"昨日跌停 N（差值）" | 仅显示 `limit_down` 单数 | 改为双数 + 箭头 + 昨日对比 |
| B7 | **第 4 格名/细分** | dashboard 名为 **"接力情绪"**（非"昨日涨停溢价"），title 含 "昨日涨停 N 只 · 高开 N / 低开 N / 跌停 N"，sub 显示 "中位数 ±N% · 高开>5%:N · 平开±2%:N · 低开<-5%:N" | 名 "昨日涨停溢价"，仅显示均值 + sample/positive/negative 三项 | 改名 + 加细分子项 |
| B8 | **第 6 格名** | "昨日跌停**平均反馈**" | "昨日跌停今日" | 改名 |

## 关键参考文件

| 用途 | 路径 | 行号 |
|---|---|---|
| 邮件渲染主体 | `src/notify/email_sender.py` | 1-463 |
| 决策算法（待对齐） | `src/notify/email_sender.py` | `_calc_daily_advice` 67-141 |
| 指标格构建 | `src/notify/email_sender.py` | `_build_html` 193-280 |
| Dashboard 决策算法（真源） | `src/static/index.html` | 1196-1268 |
| Dashboard 指标格（真源） | `src/static/index.html` | 505-600 |
| sentiment / market 数据结构 | `src/static/index.html` 取自 `/api/cycle` 或 `/api/sentiment` | 见 `src/api/app.py` |

## 数据可用性确认

以下字段在调用 `send_screener_report` 时**已可获取**（无需新增 API）：

- ✅ `sentiment_data["market"]["limit_down"]` — 已用
- ✅ `sentiment_data["market"]["drop_over_9pct"]` — 待用（A1 / B6）
- ✅ `sentiment_data["market"]["prev_day_limit_down"]` — 待用（B6）
- ✅ `sentiment_data["weighted_auction_gain"]` — 已用
- ✅ `sentiment_data["prev_day_weighted_auction_gain"]` — 待用（A2）
- ✅ `leader["yesterday_main_board_avg_auction"]` — 已用，需补 `median_change_pct / high5_count / flat2_count / low5_count / limit_down_count`（B7）

> **如果发现某字段在 sentiment_data / leader 中实际为空**，需向上追溯到 `src/engine/market_insight.py` / `src/engine/daily_review.py` 等数据生成模块确认字段是否真的被写入；如未写入则需先补数据再做邮件渲染。这部分由 Architect *review 阶段确认。

## 验收标准（DoD）

1. **算法一致性**：手工构造同一 sentiment_data + leader 输入，分别渲染 dashboard（JS）和 email（Python），决策结果（bucket / position / reason）逐字段相等。
2. **金丝雀路径**：四维警戒任意组合（0/1/2/3/4 项触发）的边界用例都能正确分类。
3. **连续好升仓**：今日 + 昨日皆 "跌停≤5 + 加权竞价≥0" 时，邮件输出 "4层（连续情绪良好）" 而非 "3层（标准仓位）"。
4. **文案对齐**：邮件中所有出现"三维"的文字改为"四维"。
5. **指标格数量**：邮件保留 6 格布局，但第 1/4/6 格按 Class B 要求展示对应字段。
6. **不引入回归**：现有"无数据加载中"分支、SMTP_USER 缺失跳过分支、空 hits 分支保持不变。
7. **测试**：QA 阶段补 `tests/notify/test_email_decision_alignment.py`，对每个不一致点写一个用例（构造输入 → 断言输出 HTML 含/不含特定关键词）。

## 范围外（明确不做）

- 不重构整个 `_build_html`（保持单文件函数式结构）。
- 不引入 Jinja2 / 模板引擎（保持 f-string 内联）。
- 不改 dashboard JS（dashboard 是真源）。
- 不改 SMTP 配置 / 邮件发送链路。
- 不重命名 `send_screener_report` 公开签名。

## Story 类型

**Brownfield 单功能增强 · 全栈式（决策算法 + UI 同步）·  Standard 复杂度**
建议 SM 设置 `test_design_level: standard`，触发 QA *test-design 前置流程。

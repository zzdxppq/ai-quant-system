# Iteration 2 Scope: email + dashboard 体感问题修复

## 背景

email-sync-1.1 (commit eb4e883) 上线后用户实盘反馈 3 类体感问题：
邮件渲染不全 / 决策不一致 / 复盘页时序错位。共 5 个独立 Story。

## Story 列表（按优先级排序）

### 🔴 Story 2.1 — 决策一致性（high · 实盘困惑）

**问题**：邮件 9:27 一次性算决策（"0 层 不开仓"），看板 11:30+ 实时算决策（"1.5 层 谨慎参与"）。
同一只股票，邮件和看板给出**矛盾的仓位建议**，用户不知道按哪个执行。

**真源约束**：邮件应该等同于"9:27 决策快照"，看板该锁定那个快照不再实时变。
（用户已选定"看板锁定 9:27 快照"为方向）

**改动范围**：
- 9:27 选股完成后写 `data/latest_advice.json`（含 dailyAdvice 完整字段：bucket / text / suggested_position / reason / bad_count / 4 维状态）
- 看板 dailyAdvice computed → 改为读 `latest_advice.json`，不再实时算
- 邮件 _calc_daily_advice → 同样从 latest_advice.json 读（保证完全一致）
- 用户手动 `*refresh-screener` 时也应一起刷新 latest_advice.json + 重发邮件（统一）

**复杂度**：standard（涉及决策算法迁移到独立计算 + 持久化）

---

### 🔴 Story 2.2 — 次日观察池显示快照（high · 时序错位）

**问题**：复盘页"🎯 次日关注标的池"显示当前盘中数据，但用户期望看的是
**前一日 15:00 收盘后冻结的快照**（"昨晚定的明日关注股"）。

**改动范围**：
- 在 cycle_update 流程（15:30 收盘后）冻结 watch_pool 写入 review_history
- review API 加载昨日收盘后的 watch_pool 快照，不再实时调用 `_generate_watch_pool`
- 仅在用户主动选"今日"日期且当前 < 15:00 时，显示"待 15:00 后生成"占位

**复杂度**：standard

---

### 🟡 Story 2.3 — 接力情绪 sub 4 字段（medium · 显示空）

**问题**：邮件第 4 格"接力情绪" sub 行显示
"中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—"。
dashboard `index.html:562-567` 用 `v-if="median_change_pct != null"` 隐藏整行（看板也没显示）。

根因：`leader_feedback.compute_yesterday_main_board_auction` 仅返回
`{date, sample_count, avg_change_pct, positive_count, negative_count, limit_down_count}` —
**没有 median_change_pct / high5_count / flat2_count / low5_count 这 4 个字段**。

email-sync-1.1 的 B7 实施时假设了数据源会算这些字段，但实际数据源没补。
dashboard 用 v-if 隐藏所以"装看不见"；email 用 fallback "—" 暴露了空。

**改动范围**：
- `compute_yesterday_main_board_auction` 增加 4 个统计字段：
  - `median_change_pct`: 样本竞价涨幅中位数
  - `high5_count`: 高开 > 5% 的数量
  - `flat2_count`: 平开 [-2%, +2%] 的数量
  - `low5_count`: 低开 < -5% 的数量
- email_sender 第 4 格 sub 行：当 4 个字段任一为 None 时，**整行不渲染**（与 dashboard `v-if` 行为对齐）

**复杂度**：standard

---

### 🟡 Story 2.4 — 看板选股表显示（medium · 显示空 + 概念缺失）

**问题**：
- (a) 选股表"市值"列显示空（NaN/null）— `screener.py round(NaN)` 仍是 NaN，json 序列化后模板渲染 "亿"
- (b) 选股表"板块"列只显示行业名，应该是"概念A/概念B (行业)" 格式（与异常未匹配额表一致）— `topConceptsOf(hit.code)` 当前返回空

**改动范围**：
- (a) `screener.py` 包 `_safe_round(v)` 处理 NaN → None；模板加 v-if 隐藏 "—"
- (b) 排查 concept 注入链路：`topConceptsOf` 实现在哪、hit.code 是否进入 concept 缓存查询

**复杂度**：standard（两个 sub-issue 合一）

---

### 🟢 Story 2.5 — 邮件防误触发（low · 副作用预防）

**问题**：用户/Yuri 在盘中手动 `*refresh-screener` 或 `run_screener_update()` 会**重复发送邮件**。
今早至少出现 1 次（Yuri 11:35 重跑发了第二封）。

**改动范围**：
- `run_screener_update(skip_email: bool = None)` 加参数；缺省时按时间判断（仅 9:27 ± 5min 内发邮件）
- API `/api/refresh-screener` 默认 `skip_email=True`，用户重新刷新只更新数据，不重发邮件
- 9:27 cron job 仍正常发邮件

**复杂度**：quick

---

## 数据可用性 / 真源约束

- `data/latest_sentiment.json`、`data/latest_leader.json`、`data/review_history.json` 已存在
- `data/latest_advice.json` 是 Story 2.1 新建文件
- dashboard `index.html` 与 email_sender.py 各自独立，但**决策算法必须共用一份持久化数据**

## 范围外（明确不做）

- 不重构 dailyAdvice 算法本身（仅迁移持久化层）
- 不改 4 维警戒阈值（5/9/0/0）
- 不改 SMTP 推送链路
- 不引入消息队列 / Redis / 等基础设施

## Story 类型

5 个均为 **brownfield 单 Story**，沿用 email-sync-1.1 的 8 条偏差路线（无 PRD 分片 / 无 architecture 目录 / scope 文件作虚拟 epic）。

建议执行顺序：**2.1 → 2.4 → 2.3 → 2.5 → 2.2**
- 2.1 决策一致性是最痛点（实盘困惑），优先
- 2.4 选股表显示影响每天阅读体验
- 2.3 邮件细分次之
- 2.5 quick 顺手
- 2.2 次日池快照需要等到下一个 15:30 才能看到效果，最不紧急

每个 Story SM `*draft` 完后由 watchdog 自动 HANDOFF Architect → QA test-design → Dev → QA review → SM next。

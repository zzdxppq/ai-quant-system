# Story email-sync-1.1: 邮件推送内容逐字段对齐首页看板

## Story

```yaml
Story:
  id: email-sync-1.1
  title: 邮件推送内容逐字段对齐首页看板
  epic: email-dashboard-alignment (brownfield, virtual epic — 真源为 docs/prd/iteration-email-sync-scope.md)
  status: Done
  mode: plan
  repository: monolith
  priority: P1
  estimated_complexity: standard
  test_design_level: standard
  story_type: brownfield-enhancement
```

**As a** A 股短线交易者（邮件推送的唯一收件人 604491810@qq.com），
**I want** 9:27 选股决策邮件中的"决策算法 + 指标格"与首页看板 `src/static/index.html` 的内容**逐字段保持一致**，
**so that** 我在 QQ 邮箱看到的建议（bucket / position / reason / 指标格读数）与浏览器看板**永不分叉**，避免在不同入口看到相互矛盾的实盘判断。

---

## 背景与问题

`src/notify/email_sender.py` 的 `_calc_daily_advice` 与 `_build_html` 已落后于 `src/static/index.html` 的最新版本：

- **Dashboard 真源**已迭代到"**四维**警戒（限跌停 / 加权竞价 / 连板高标 / **跌幅>9% 个股数**）+ **连续 2 日情绪好 → 升 4 层**"逻辑（见 `index.html:1196-1268`）。
- **Email 当前**仍停留在"三维警戒 + 固定 3-6 层"旧版（见 `email_sender.py:67-141`）。
- 用户在 9:27 收到的邮件文案、仓位建议、reason 维度数与浏览器看板**不匹配**，构成实盘信号分歧风险。

scope 文件 `docs/prd/iteration-email-sync-scope.md` 已枚举 8 项不一致：
- 🔴 Class A（5 项，决策算法核心）：A1 第四维 / A2 连续升 4 层 / A3 谨慎文案 / A4 可参与文案 / A5 维度数文案
- 🟡 Class B（3 项，指标格 UI）：B6 第 1 格双数 / B7 第 4 格名+细分 / B8 第 6 格名

**真源不变性已确认**：scope 创建后至本 Story 起草共 22 条 commit，dashboard 端 `index.html:1196-1268` 与 `index.html:505-600` 均**未被修改**，scope 中引用的真源行号与字段全部仍然有效（commit `bbe8c16` 在 email_sender 加过概念格属已对齐字段，不在本 Story 8 项范围内）。

---

## Acceptance Criteria

### AC1: 引入第四维（跌幅>9% 个股数 警戒）

**Scenario**
```gherkin
GIVEN sentiment_data["market"]["drop_over_9pct"] = N
WHEN 调用 _calc_daily_advice(sentiment_data, leader)
THEN
  - 当 N > 9 时，将 "drop_bad" 视为已触发，warnings 列表追加 "市场跌幅>9% 个股 {N} 只（>9 警戒线）"
  - 该维度计入 bad_count，与既有三维（ld_bad / w_bad / lb_bad）共同决定 bucket
  - 与 dashboard `index.html:1218-1220` 的 dropBad = dropOver9 > 9 阈值与归类完全一致
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-1.1 | drop_over_9pct 缺失 / 非数值 / None 时，drop_bad = False（与 has_ld / has_w 同样的"数据有效性守护"模式） |
| BR-1.2 | 阈值严格使用 `>` 9（不是 `>=`），与 dashboard `dropOver9 > 9` 完全相等 |
| BR-1.3 | 该维度的 has_* 守护变量（has_drop）需参与 "全部三维都没数据→数据加载中"分支判定，避免新增维度后出现误归类 |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| sentiment_data 为 None / market 缺失 / drop_over_9pct 非数值 | — | （静默） | drop_bad = False，不进入 warnings |
| 全部 4 维都无有效数据 | — | "— 数据加载中 —" | 维持原 "数据加载中" 分支返回不变 |

---

### AC2: 连续 2 日情绪好 → 升 4 层

**Scenario**
```gherkin
GIVEN sentiment_data["weighted_auction_gain"] = w_today
  AND sentiment_data["prev_day_weighted_auction_gain"] = w_prev
  AND sentiment_data["market"]["limit_down"] = ld_today
  AND sentiment_data["market"]["prev_day_limit_down"] = ld_prev
  AND 4 维警戒全部未触发（bucket = go）
WHEN 调用 _calc_daily_advice
THEN
  - 当 todayGood = (ld_today ≤ 5 AND w_today ≥ 0) 且 prevGood = (ld_prev ≤ 5 AND w_prev ≥ 0) 都成立
  - position 应为 "4 层（连续情绪良好）"，position_short 为 "4层"
  - 否则 position 应为 "3 层（标准仓位）"，position_short 为 "3层"（不再是 "3-6 层"）
  - 判定逻辑与 dashboard `index.html:1252-1267` 完全等价
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-2.1 | 仅在 bucket = go（4 维都未触发）的分支中评估升仓；warn / stop 分支不引入升仓逻辑 |
| BR-2.2 | prev_day 字段任一缺失 / 非数值时，prevGood = False，回退 "3层（标准仓位）"，不抛错 |
| BR-2.3 | 升仓判定仅看"竞价跌停≤5 + 加权竞价≥0"两个原始指标的今昨双日成立，不引入第四维 drop_over_9pct 进入 prevGood 判定（与 dashboard 一致） |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| prev_day_* 字段缺失 | — | （静默） | 退化为 "3 层（标准仓位）" |
| 任一 prevGood 字段为 None | — | （静默） | prevGood = False |

---

### AC3: 谨慎参与文案对齐 → "1.5 层（小仓试错）"

**Scenario**
```gherkin
GIVEN 4 维警戒中**仅 1 项**触发（bad_count == 1）
WHEN 调用 _calc_daily_advice
THEN
  - 返回 position = "1.5 层（小仓试错）"
  - 返回 position_short = "1.5层"
  - 与 dashboard 文案完全一致（替代旧 "1-2 层（小仓试错）"）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-3.1 | 仅替换文案，不改 bucket 判定逻辑（仍是 bad_count == 1 → warn） |
| BR-3.2 | 邮件 subject 中 `position_short` 同步显示 "1.5层"（影响 send_screener_report 第 54 行的 subject 拼装） |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| — | — | — | 文案改造 AC，无新增错误路径 |

---

### AC4: 可参与文案对齐 → "3 层" / "4 层（连续情绪良好）"

**Scenario**
```gherkin
GIVEN 4 维警戒全部未触发（bad_count == 0）
WHEN 调用 _calc_daily_advice
THEN
  - 满足 AC2 升仓条件 → position = "4 层（连续情绪良好）", position_short = "4层"
  - 否则 → position = "3 层（标准仓位）", position_short = "3层"
  - **不再返回 "3-6 层（标准仓位）"** 这一旧文案
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-4.1 | 与 AC2 共享同一 prevGood / todayGood 判定，避免双份实现 |
| BR-4.2 | position 单位均为"层"，与 dashboard 一致（不写"成"或"档"） |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| — | — | — | 文案改造 AC，错误路径已在 AC2 中覆盖 |

---

### AC5: reason 维度数文案 → "四维"

**Scenario**
```gherkin
GIVEN bucket = stop（bad_count >= 2）
WHEN 调用 _calc_daily_advice
THEN
  - reason 字符串末尾应为 "四维警戒中已 N 项触发，避免开仓。"
  - 不再出现 "三维警戒中已 N 项触发" 的旧文案
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-5.1 | 文件内**所有**出现"三维"的字符串均替换为"四维"（包括函数 docstring 67 行附近） |
| BR-5.2 | reason 中维度数 N 的最大可能值由 3 升为 4（4 维全部触发的极端情况） |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| — | — | — | 文案改造 AC，无新增错误路径 |

---

### AC6: 第 1 指标格双数 + 箭头 + 昨日对比

**Scenario**
```gherkin
GIVEN sentiment_data["market"] 中含 limit_down / drop_over_9pct / prev_day_limit_down
WHEN 渲染邮件 HTML 第 1 指标格（_build_html 第 242 行附近）
THEN 该格内容应：
  - **标签**：「竞价跌停 (>5⚠) / 跌>9% (>9⚠)」
  - **主值**：以 "limit_down / drop_over_9pct" 形式同行展示（双数）
  - **箭头**：相比昨日跌停（prev_day_limit_down）变化用 ↑ / ↓ / → 表示
  - **副文本**：「昨日跌停 N（差值±M）」
  - 三段内容与 dashboard `index.html:513-530` 中字段、阈值、箭头规则完全一致
```

**UI Interaction**
| Trigger | Behavior |
|---------|----------|
| limit_down 缺失但 drop_over_9pct 有值 | 主值显示 "— / N"，箭头不渲染 |
| prev_day_limit_down 缺失 | 副文本退化为 "昨日跌停 —"，无差值 |
| 全部缺失 | 维持现有 "—" 占位行为 |

**Business Rules**
| ID | Rule |
|----|------|
| BR-6.1 | 主值颜色规则与现有 num_color(threshold_high_is_bad=True) 保持兼容；可在原函数基础上扩展双值场景 |
| BR-6.2 | 不引入新模板引擎，继续使用 f-string 内联（scope 范围外约束） |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| 输入字段全为 None | — | — | 单元格显示 "—"，与现状一致 |

---

### AC7: 第 4 指标格改名 "接力情绪" + 加细分子项

**Scenario**
```gherkin
GIVEN leader["yesterday_main_board_avg_auction"] 含 avg_change_pct / sample_count / positive_count / negative_count
  AND 同字典含 median_change_pct / high5_count / flat2_count / low5_count / limit_down_count（待 Architect 确认上游是否已写入）
WHEN 渲染邮件 HTML 第 4 指标格（_build_html 第 247 行附近）
THEN 该格应：
  - **标签**：「接力情绪」（替代 "昨日涨停溢价"）
  - **title 行**：「昨日涨停 N 只 · 高开 N / 低开 N / 跌停 N」
  - **主值**：avg_change_pct（与现状一致）
  - **副文本（sub）**：「中位数 ±M% · 高开>5%:N · 平开±2%:N · 低开<-5%:N」
  - 与 dashboard `index.html:505-600` 第 4 格的标签、title、sub 字段完全一致
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-7.1 | **数据可用性前置确认**：median_change_pct / high5_count / flat2_count / low5_count / limit_down_count 字段在调用 send_screener_report 时是否已经在 leader["yesterday_main_board_avg_auction"] 中可用，由 Architect 在 *review 阶段追溯 src/engine/market_insight.py / src/engine/daily_review.py 等数据生成模块确认；如未写入需先补数据再做邮件渲染（scope 文件第 53 行约定） |
| BR-7.2 | 任一细分字段缺失时，该子项以 "—" 占位渲染，不抛错、不省略其他子项 |
| BR-7.3 | 仅改第 4 格名与子项，不改第 4 格在 6 格布局中的位置（保持现有 2 行 3 列） |

**UI Interaction**
| Trigger | Behavior |
|---------|----------|
| sample_count = 0 或 None | title 行显示 "昨日涨停 — 只"，主值与子项均显示 "—" |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| 字段缺失 | — | — | 单字段降级为 "—"，整格不消失 |
| 字段类型异常 | — | — | num_color 沿用现有 try/except 安全转换路径 |

---

### AC8: 第 6 指标格改名 "昨日跌停平均反馈"

**Scenario**
```gherkin
GIVEN leader["yesterday_limit_down_today_auction"] 现有字段不变
WHEN 渲染邮件 HTML 第 6 指标格（_build_html 第 251 行附近）
THEN 该格标签应为 "昨日跌停平均反馈"（替代 "昨日跌停今日"）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-8.1 | 仅改标签字符串，主值 / 副文本 / 颜色规则不变 |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| — | — | — | 单纯文案 AC |

---

### AC9: 不引入回归（scope DoD #6）

**Scenario**
```gherkin
GIVEN 现有邮件链路的三类边缘分支
WHEN 重构后的代码在以下输入下被调用
THEN 行为应与重构前完全一致：
  - SMTP_USER 或 SMTP_PASSWORD 缺失 → 打印 "[邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送"，返回 False
  - sentiment_data + leader 全 None → _calc_daily_advice 返回 "— 数据加载中 —" 分支
  - hits 列表为空 → hits_html 渲染 "无命中标的" 占位
  - send_screener_report 公开签名（参数列表 + 返回值类型）保持不变
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-9.1 | 不重命名 send_screener_report 公开签名，调用方 src/main.py 等不需要改 import |
| BR-9.2 | 不重构整个 _build_html（保持单文件函数式结构，scope 范围外约束） |
| BR-9.3 | 不引入 Jinja2 / 模板引擎，继续 f-string 内联 |
| BR-9.4 | 不改 SMTP 配置 / 邮件发送链路（_send 函数不动） |
| BR-9.5 | 不改 dashboard JS（dashboard 是真源） |

**Error Handling**
| Scenario | Code | Message | Action |
|----------|------|---------|--------|
| 任一边缘分支行为差异 | — | — | QA 标记为 BLOCKING，回退至 SM revise |

---

## Tasks / Subtasks

> **说明**：测试用例的具体 spec 由 QA 在 *test-design 阶段产出（test_design_level: standard），Dev 在编码后回填本节"测试"子任务。

### Test Specs Quick Reference (QA 回填，2026-05-08)

> 完整设计文档见 [docs/qa/assessments/email-sync-1.1-test-design-20260508.md](../qa/assessments/email-sync-1.1-test-design-20260508.md)；
> 测试骨架见 [tests/notify/test_email_decision_alignment.py](../../tests/notify/test_email_decision_alignment.py)（46 个 `pytest.fail` 占位用例）。
> Dev 必须把每个占位实现为真实断言；不允许删除占位（不适用场景改为 `pytest.skip(reason=...)`）。

| T-Task | AC | 关键 Test IDs (P0 优先) | 测试要点 |
|---|---|---|---|
| T1 | AC1 | 1.1-UNIT-001/002/003/004/006 | drop_bad 主路径；阈值 `>9` 严格边界；缺失/类型守护；has_drop 纳入空数据判定 |
| T2 | AC2 | 1.1-UNIT-007/008/010/011 (+ 009/012 P1) | 升 4 层主路径；prevGood=F 回退 3 层；warn 分支不评估升仓；prev_day 字段缺失降级 |
| T3 | AC3+AC4+AC5 | 1.1-UNIT-014/015/016/017/018/019/020 | "1.5 层"/"3 层"/"4 层" 文案对齐；旧 "3-6 层"/"三维" 全文清除（负断言） |
| T4 | AC6 | 1.1-UNIT-021/022/030 (+ 023/024/025 P1, 026 P2) | 第 1 格双数 + ↑↓→ 箭头 + "昨日跌停 N（差值±M）"；字段缺失降级 |
| T5 | AC7 | 1.1-UNIT-027/030 (+ 028/029 P1) | "接力情绪" + title + 4 个细分子项；单字段降级；旧 "昨日涨停溢价" 清除 |
| T6 | AC8 | 1.1-UNIT-031/032 | "昨日跌停平均反馈" 标签；旧 "昨日跌停今日" 清除 |
| T7 | AC1-AC5 | 1.1-INT-006 | dashboard JS dailyAdvice 4 维触发组合 0/1/2/3/4 项算法等价 |
| T8 | AC9 | 1.1-INT-002/003/004/005 | SMTP 未配置；全空数据；空 hits；签名不变 |
| T9 | AC1-AC9 | 1.1-INT-007 + UNIT-017/020/030/032 | HTML 关键词正负断言（新词必含 / 旧词必无） |
| Blind | AC1/AC7 | 1.1-BLIND-BOUNDARY-001/003, ERROR-001/002 (P1) + BOUNDARY-002/004, FLOW-001 (P2) | 阈值 0/极大值；4 维全触发；类型异常；缺失子字典；加载中状态下游渲染 |
| T10 | ALL | 全部上述 + 集成验收 | 所有占位均不再为 `pytest.fail`；`pytest tests/notify/` 全绿且无 warning |

### Infrastructure Tasks (Shared)

- [x] **T0: 数据可用性前置探查** `[AC1, AC2, AC7]`
  - [x] 在 `src/engine/market_insight.py` / `src/engine/daily_review.py` / `src/engine/sentiment.py` 中定位 sentiment_data 与 leader 字典构造点
  - [x] 确认 `prev_day_weighted_auction_gain` / `prev_day_limit_down` / `drop_over_9pct` 在 9:27 调用点是否已被写入
  - [x] 确认 `leader["yesterday_main_board_avg_auction"]["median_change_pct"]` 等 5 项 B7 子字段是否已被写入
  - [x] 缺失字段清单写入 Dev Log；若有缺失则 **HALT 并升级到 Architect**（不在邮件渲染层硬编码 fallback）
  - [x] T0 通过后再进入 T1+ — **结论：全部就绪，无需升级**

### Feature Implementation Tasks

- [x] **T1: AC1 — 引入第四维 drop_bad** `[AC1]`
  - [x] 在 `_calc_daily_advice` 中读取 `market.get("drop_over_9pct")` + has_drop 守护
  - [x] 加 `drop_bad = has_drop and drop_over_9pct > 9` 与对应 warnings 文案
  - [x] 修改 "全维度无数据→数据加载中"分支条件，把 has_drop 纳入"全空"判定
  - [x] 单元测试覆盖 BR-1.1 / BR-1.2 / BR-1.3 (1.1-UNIT-001~006 + BLIND-BOUNDARY-001/002, BLIND-ERROR-002)

- [x] **T2: AC2 — 连续好升 4 层** `[AC2]`
  - [x] 引入 prev_day_* 读取逻辑（沿用 has_* 守护模式）
  - [x] 在 bucket=go 分支中实现 todayGood + prevGood 双判定
  - [x] position / position_short 输出 "4 层" / "3 层"
  - [x] 单元测试覆盖 BR-2.1 / BR-2.2 / BR-2.3 (1.1-UNIT-007~013)

- [x] **T3: AC3+AC4+AC5 — 文案三连改** `[AC3, AC4, AC5]`
  - [x] 替换"1-2 层（小仓试错）"→ "1.5 层（小仓试错）"
  - [x] 替换"3-6 层（标准仓位）"→ 由 AC2/AC4 动态产出
  - [x] 替换文件内全部"三维"→ "四维"（含 docstring 67 行附近）
  - [x] 单元测试覆盖 BR-3.x / BR-4.x / BR-5.x (1.1-UNIT-014~020)

- [x] **T4: AC6 — 第 1 指标格双数 + 箭头 + 昨日对比** `[AC6]`
  - [x] 内联 cell1_html（保持文件函数式结构，不引入新模板）
  - [x] 渲染 "limit_down / drop_over_9pct" 同行
  - [x] 加 ↑↓→ 箭头与"昨日跌停 N（差值）"副文本
  - [x] 字段缺失降级单元测试 (1.1-UNIT-021~026)

- [x] **T5: AC7 — 第 4 格改名 + 细分子项** `[AC7]`
  - [x] 改标签 "昨日涨停溢价"→ "接力情绪"
  - [x] 加 title 行 "昨日涨停 N 只 · 高开 N / 低开 N / 跌停 N"
  - [x] 加 sub 子项 "中位数 ±M% · 高开>5%:N · 平开±2%:N · 低开<-5%:N"
  - [x] 字段缺失降级单元测试 (1.1-UNIT-027~030 + BLIND-ERROR-001)

- [x] **T6: AC8 — 第 6 格标签改名** `[AC8]`
  - [x] "昨日跌停今日"→ "昨日跌停平均反馈"

### Integration & Verification Tasks

- [x] **T7: 算法一致性集成测试** `[AC1-AC5]`（DoD #1）
  - [x] 构造同一组 sentiment_data + leader fixture
  - [x] 调用 `_calc_daily_advice`，断言 bucket / position / position_short / reason 与 dashboard JS 逻辑完全等价
  - [x] 4 维警戒触发组合（0/1/2/3/4 项）边界用例（DoD #2，1.1-INT-006）

- [x] **T8: 回归保护测试** `[AC9]`（DoD #6）
  - [x] SMTP_USER 缺失分支 (1.1-INT-002)
  - [x] 全 None 输入 "数据加载中" 分支 (1.1-INT-003)
  - [x] 空 hits 分支 (1.1-INT-004)
  - [x] send_screener_report 签名不变断言（inspect.signature, 1.1-INT-005）

- [x] **T9: HTML 关键词断言测试** `[AC1-AC9]`（DoD #7）
  - [x] 创建 `tests/notify/test_email_decision_alignment.py`
  - [x] 渲染后 HTML 含新关键词、不含旧关键词 (1.1-INT-007)
  - [x] 测试用例的具体 spec 由 QA 在 *test-design 阶段产出，Dev 已实现 46/46

- [x] **T10: 最终验收** `[ALL ACs]`
  - [x] 所有单元测试 + 集成测试通过 — 46 passed in 0.03s
  - [x] `pytest tests/notify/` 无 lint / 无 warning（`-W error` 严格模式通过）
  - [x] Dev Log 完整记录数据可用性探查结果与实现摘要 — `docs/dev/logs/email-sync-1.1-dev-log.md`
  - [x] Status → Review

### AC Coverage Matrix

| Task | AC1 | AC2 | AC3 | AC4 | AC5 | AC6 | AC7 | AC8 | AC9 |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| T0: 数据探查 | ✓ | ✓ |   |   |   |   | ✓ |   |   |
| T1: 第四维 | ✓ |   |   |   |   |   |   |   |   |
| T2: 升 4 层 |   | ✓ |   |   |   |   |   |   |   |
| T3: 文案三连 |   |   | ✓ | ✓ | ✓ |   |   |   |   |
| T4: 第 1 格双数 |   |   |   |   |   | ✓ |   |   |   |
| T5: 第 4 格细分 |   |   |   |   |   |   | ✓ |   |   |
| T6: 第 6 格标签 |   |   |   |   |   |   |   | ✓ |   |
| T7: 算法一致性 | ✓ | ✓ | ✓ | ✓ | ✓ |   |   |   |   |
| T8: 回归保护 |   |   |   |   |   |   |   |   | ✓ |
| T9: HTML 断言 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |   |
| T10: 最终验收 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

---

## Dev Notes

### Technical Constraints

| 类别 | 约束 | 来源 |
|---|---|---|
| 算法真源 | dashboard JS `dailyAdvice` 是唯一真源；任何分歧由 Python 端对齐 JS，**不得反向** | scope 文件 第 9 行 + 第 70 行 |
| 文件结构 | 不重构 `_build_html` 整体；保持单文件函数式 + f-string 内联 | scope 文件 范围外约束 |
| 模板引擎 | **不**引入 Jinja2 / Mako / 任何模板引擎 | scope 文件 范围外约束 |
| API 契约 | `send_screener_report` 公开签名不变（参数列表 + 返回值类型）| scope 文件 范围外约束 |
| SMTP 链路 | `_send` 函数不动，SMTP_HOST/PORT/USER/PASSWORD 配置不动 | scope 文件 范围外约束 |
| Dashboard 端 | **绝对不**修改 `src/static/index.html` 的 `dailyAdvice` 与第 1/4/6 格 HTML | scope 文件 范围外约束 |
| 数据兜底原则 | 若上游字段缺失，**先升级到 Architect** 评估补数据，**不**在邮件渲染层硬编码 fallback 数值 | scope 文件 第 53 行 |

### Accumulated Context (From Previous Stories)

| Resource | 状态 |
|---|---|
| Database Tables | N/A — 本 Story 无数据库写入 |
| API Endpoints | N/A — 不新增 / 不改动 API |
| Shared Models | N/A — 复用既有 `sentiment_data` / `leader` 字典结构（非 typed model） |

> 备注：本 Story 是 brownfield 单 Story 增强，且为 docs/stories/ 目录下首个 Story，无前置累积上下文。后续 Story 可从本 Story 继承"邮件渲染单一真源"约定。

### Database Design

N/A — 不涉及数据库变更。

### Data Synchronization Requirements

- [x] 经分析，本 Story 无跨表数据同步需求（无任何数据库写操作）。

### Data Models（既有结构契约，仅供 Dev 读用）

**`sentiment_data` 字典关键字段**（来源：`src/engine/sentiment.py` / `src/engine/market_insight.py`，待 T0 确认）：
```python
{
    "weighted_auction_gain": float | None,
    "prev_day_weighted_auction_gain": float | None,  # AC2 新用
    "market": {
        "limit_down": int | None,
        "drop_over_9pct": int | None,                  # AC1, AC6 新用
        "prev_day_limit_down": int | None,             # AC6 新用
    }
}
```

**`leader` 字典关键字段**（来源：`src/engine/daily_review.py` / `src/engine/leader_signal.py`，待 T0 确认）：
```python
{
    "main_board_leaders": [...],
    "yesterday_main_board_avg_auction": {
        "avg_change_pct": float | None,
        "sample_count": int | None,
        "positive_count": int | None,
        "negative_count": int | None,
        "median_change_pct": float | None,             # AC7 新用 — T0 确认
        "high5_count": int | None,                     # AC7 新用 — T0 确认
        "flat2_count": int | None,                     # AC7 新用 — T0 确认
        "low5_count": int | None,                      # AC7 新用 — T0 确认
        "limit_down_count": int | None,                # AC7 新用 — T0 确认
    },
    "yesterday_zb_today_auction": {...},
    "yesterday_limit_down_today_auction": {...},  # AC8 仅改标签
}
```

### File Locations

| 文件 | 操作 | 涉及行号（起草时） | 关联 AC |
|---|---|---|---|
| `src/notify/email_sender.py` | **修改** | `_calc_daily_advice` 67-141；`_build_html` 193-280 | AC1-AC9 |
| `tests/notify/test_email_decision_alignment.py` | **新建** | — | AC1-AC9（具体测试用例由 QA test-design 给出） |
| `src/engine/market_insight.py` 或 `src/engine/daily_review.py` 等 | **可能修改** | 待 T0 探查后由 Architect 决定 | AC1, AC2, AC7（仅当上游缺字段时） |
| `src/static/index.html` | **绝对不动** | 1196-1268 / 505-600 | （真源约束） |

### Deliverable Bindings

```yaml
deliverable_bindings:
  - deliverable: "tests/notify/test_email_decision_alignment.py"
    consumer: "pytest discovery (project test runner)"
    binding_type: import_usage
    verify: "pytest 收集到 test_email_decision_alignment 中的测试用例（具体用例数与名称由 QA test-design 给出）"
```

> 备注：本 Story 主要是**修改**既有 `email_sender.py`，未创建新生产代码模块。新建的测试文件通过 pytest 自动发现机制绑定到 CI 流程。

### Testing Requirements

- **测试设计层级**：`standard`（用户在指令中明确要求）
- **前置流程**：QA *test-design 在开发前出测试设计文档，Dev 据此实现 T9 中的具体用例
- **覆盖重点**：
  1. 4 维警戒触发组合（0/1/2/3/4 项）的 bucket 边界（AC1）
  2. 连续好升仓的真值表（todayGood × prevGood × prev_day 字段缺失）（AC2）
  3. 文案完全替换（不留旧字符串）的负断言（AC3-AC5）
  4. 字段缺失降级在 6 个指标格中的覆盖（AC6-AC8）
  5. 三类边缘分支不变性（AC9）

---

## QA Test Design Metadata

- **Level**: Standard
- **Status**: Complete
- **Test Design Status**: Complete
- **Document**: [docs/qa/assessments/email-sync-1.1-test-design-20260508.md](../qa/assessments/email-sync-1.1-test-design-20260508.md)
- **Test Skeleton**: [tests/notify/test_email_decision_alignment.py](../../tests/notify/test_email_decision_alignment.py) (46 个测试函数，全部默认 `pytest.fail` 未实现态)
- **Risk Profile**: N/A（test_design_level=standard，不强制）
- **统计**: 46 scenarios = UNIT 32 + INT 7 + BLIND 7 (BOUNDARY 4 / ERROR 2 / FLOW 1) ; P0 29 / P1 12 / P2 5
- **Dev 入口**: 直接编辑 `tests/notify/test_email_decision_alignment.py`，逐个把 `pytest.fail("Test not implemented: …")` 替换为真实断言；不应新增任何与设计文档无对应的测试函数；如有场景不再适用，改为 `pytest.skip(reason="…")` 并在 Implementation Summary 中说明
- **关键集成断言基线** (1.1-INT-005): `send_screener_report(cycle_phase, cycle_day, representative, leader, hits, signals, deviations=None, sentiment_data=None, ranking_data=None) -> bool`

---

## Change Log

| Date | Agent | Status Transition | Details/Link |
|------|-------|-------------------|--------------|
| 2026-05-08 | SM | Created → AwaitingTestDesign | Brownfield 单 Story 起草；偏离标准流程 8 条已与用户确认（跳过 Epic YAML / 架构上下文 / 累积校验 / Architect 评审；强制 test_design_level=standard；scope 文件 [docs/prd/iteration-email-sync-scope.md](../prd/iteration-email-sync-scope.md) 作为真源） |
| 2026-05-08 | QA | AwaitingTestDesign → TestDesignComplete → Approved | Test Design Doc: [docs/qa/assessments/email-sync-1.1-test-design-20260508.md](../qa/assessments/email-sync-1.1-test-design-20260508.md)；46 scenarios (UNIT 32 / INT 7 / BLIND 7；P0 29 / P1 12 / P2 5)；测试骨架 [tests/notify/test_email_decision_alignment.py](../../tests/notify/test_email_decision_alignment.py)；QA 两阶段状态转换；Tasks 区已追加 Test Specs Quick Reference；Ready for Dev implementation |
| 2026-05-08 | Dev | Approved → InProgress → Review | T0 探查：上游字段全部就绪（sentiment_pool/leader_feedback 已写入 prev_day_* / drop_over_9pct / median 等 5 项细分子字段），无需升级 Architect。实现：`src/notify/email_sender.py` 重构 `_calc_daily_advice`（四维 + 升 4 层 + 文案三连）+ `_build_html` 第 1/4/6 格改造（双数+箭头+昨日对比 / 接力情绪 + 细分子项 / 标签改名）。测试：46/46 pass（UNIT 32 + INT 7 + BLIND 7），`-W error` 严格模式通过。Dev Log: [docs/dev/logs/email-sync-1.1-dev-log.md](../dev/logs/email-sync-1.1-dev-log.md) |
| 2026-05-08 | QA | Review → Done | Round 1, Gate: PASS, Tests: 100% (46/46). 9/9 ACs verified, 7/7 blind spots covered, dashboard 真源未动。Gate file: [docs/qa/gates/email-sync-1.1-email-content-align-dashboard.yml](../qa/gates/email-sync-1.1-email-content-align-dashboard.yml). 1 LOW (line-number drift, non-blocking) + 1 out-of-scope observation (preexisting `DATA_DIR`/json 未 import in `email_sender.py:419-451`, commit 45baa67 引入). |

---

## AC Traceability Matrix

> **Dev**: Fill this section BEFORE self-review. Every AC needs evidence.
> **QA**: Verify each entry during AC Coverage Verification.

### AC1: 引入第四维（跌幅>9% 个股数 警戒）

```yaml
ac_id: AC1
code_locations:
  - "src/notify/email_sender.py:75-77 (read drop_over_9pct + has_drop)"
  - "src/notify/email_sender.py:96 (全空判定加入 has_drop)"
  - "src/notify/email_sender.py:101 (drop_bad 计算)"
  - "src/notify/email_sender.py:111 (warnings 文案)"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_001~006"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_blind_boundary_001"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_blind_boundary_002"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_blind_error_002"
verification_type: unit_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "BR-1.1 类型守护 + BR-1.2 严格 >9 + BR-1.3 has_drop 纳入空数据判定全部覆盖。dashboard 真源 src/static/index.html:1219-1220"
```

### AC2: 连续 2 日情绪好 → 升 4 层

```yaml
ac_id: AC2
code_locations:
  - "src/notify/email_sender.py:78-79 (read prev_day_*)"
  - "src/notify/email_sender.py:131-141 (today_good + prev_good 双判定 + 升仓分支)"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_007~013"
verification_type: unit_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "BR-2.1 warn 不评估升仓 + BR-2.2 缺失字段降级 + BR-2.3 prevGood 不依赖 drop_over_9pct 全部覆盖。dashboard 真源 :1252-1267"
```

### AC3: 谨慎参与文案对齐

```yaml
ac_id: AC3
code_locations:
  - "src/notify/email_sender.py:124-126 (warn bucket position '1.5 层（小仓试错）' / position_short '1.5层')"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_014"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_int_001"
verification_type: unit_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "BR-3.2 subject 'position_short' 集成断言 (INT-001)。旧文案 '1-2 层（小仓试错）' 与 '1-2层' 已通过负断言确认全部清除"
```

### AC4: 可参与文案对齐

```yaml
ac_id: AC4
code_locations:
  - "src/notify/email_sender.py:135-141 (4 层 / 3 层 dynamic position)"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_015"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_016"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_017"
verification_type: unit_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "BR-4.x 与 AC2 共享 prevGood/todayGood 判定 + 旧 '3-6 层' 在所有分支负断言全部覆盖"
```

### AC5: reason 维度数文案

```yaml
ac_id: AC5
code_locations:
  - "src/notify/email_sender.py:67-72 (docstring '四维警戒')"
  - "src/notify/email_sender.py:117 (reason 末尾 '四维警戒中已 N 项触发')"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_018"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_019"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_020"
verification_type: unit_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "BR-5.1 全文负断言 (UNIT-020 读模块文件全文检查)；BR-5.2 4 维全触发 N=4 极值 (UNIT-019)"
```

### AC6: 第 1 指标格双数 + 箭头 + 昨日对比

```yaml
ac_id: AC6
code_locations:
  - "src/notify/email_sender.py:213-261 (cell1_html inline 渲染：label/双数/箭头/副文本)"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_021~026"
verification_type: unit_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "↑↓→ 三向箭头 + ld/drop 双值 + 单字段降级 + 全空降级。dashboard 真源 :513-530"
```

### AC7: 第 4 格改名 + 细分子项

```yaml
ac_id: AC7
code_locations:
  - "src/notify/email_sender.py:263-307 (cell4_html inline 渲染：label/title/主值/sub)"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_027~030"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_blind_error_001"
verification_type: unit_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "BR-7.1 数据可用性 T0 已确认全部就绪 + BR-7.2 子字段缺失逐项降级 + BR-7.3 不动其他格位置。dashboard 真源 :556-568"
```

### AC8: 第 6 指标格标签改名

```yaml
ac_id: AC8
code_locations:
  - "src/notify/email_sender.py:319 (_metric_cell '昨日跌停平均反馈')"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_031"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_unit_032"
verification_type: unit_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "BR-8.1 仅改标签，主值/副文本/颜色规则保持不变。dashboard 真源 :580"
```

### AC9: 不引入回归

```yaml
ac_id: AC9
code_locations:
  - "src/notify/email_sender.py:31-45 (send_screener_report 公开签名零变更)"
  - "src/notify/email_sender.py:43-45 (SMTP_USER/PASSWORD 缺失分支保留)"
  - "src/notify/email_sender.py:421-424 (空 hits → '无命中标的' 分支保留)"
  - "src/notify/email_sender.py:96 (全空数据 → '— 数据加载中 —' 分支保留)"
test_locations:
  - "tests/notify/test_email_decision_alignment.py::test_1_1_int_002~007"
  - "tests/notify/test_email_decision_alignment.py::test_1_1_blind_flow_001"
verification_type: integration_test
aspects_covered:
  main_scenario: true
  business_rules: true
  data_validation: true
  error_handling: true
notes: "BR-9.1 inspect.signature baseline 严格比对 (INT-005) + BR-9.2~9.5 范围外约束（无新模板引擎、_send 不动、dashboard 不动）通过 1.1-INT-002~007 全部回归覆盖"
```

---

### Traceability Summary

| AC | Code Location | Test Location | Type | Status |
|----|---------------|---------------|------|--------|
| AC1 | `src/notify/email_sender.py:75-111` | `test_1_1_unit_001~006` + BLIND-BOUNDARY-001/002, BLIND-ERROR-002 | unit | ✅ |
| AC2 | `src/notify/email_sender.py:78-141` | `test_1_1_unit_007~013` | unit | ✅ |
| AC3 | `src/notify/email_sender.py:124-126` | `test_1_1_unit_014` + `test_1_1_int_001` | unit + integration | ✅ |
| AC4 | `src/notify/email_sender.py:135-141` | `test_1_1_unit_015~017` | unit | ✅ |
| AC5 | `src/notify/email_sender.py:67-117` | `test_1_1_unit_018~020` | unit | ✅ |
| AC6 | `src/notify/email_sender.py:213-261` | `test_1_1_unit_021~026` | unit | ✅ |
| AC7 | `src/notify/email_sender.py:263-307` | `test_1_1_unit_027~030` + BLIND-ERROR-001 | unit | ✅ |
| AC8 | `src/notify/email_sender.py:319` | `test_1_1_unit_031~032` | unit | ✅ |
| AC9 | `src/notify/email_sender.py:31-45,96,421` | `test_1_1_int_002~007` + BLIND-FLOW-001 | integration | ✅ |

**Legend**: ✅ Verified | ⏳ Pending | ❌ Missing

**Coverage Statistics**: 46/46 tests pass · 32 UNIT + 7 INT + 7 BLIND · P0 29 + P1 12 + P2 5 全部通过 · 0 warning (`-W error` 严格模式)

---

## Dev Agent Record

### Agent Model Used
- **Agent**: Orchestrix Dev (墨子) — `claude-opus-4-7[1m]`
- **Date**: 2026-05-08
- **Mode**: TDD (Red → Green → Refactor，2 轮迭代)

### Implementation Summary

**Scope**：邮件 9:27 决策推送的算法 + 6 指标格逐字段对齐 dashboard 真源（`src/static/index.html:1196-1268` 与 `:505-600`）。

**Approach (TDD)**：
1. **T0 探查**：grep `src/engine/` 确认 6 项新字段全部就绪（sentiment_pool 注入 prev_day_* / drop_over_9pct，leader_feedback 注入 5 项细分子字段），无需升级 Architect。
2. **Red**：把 QA 测试骨架的 46 个 `pytest.fail` 占位全部替换为真实断言 → `pytest` 跑出 34 fail / 12 pass 基线。
3. **Green Round 1**（_calc_daily_advice）：四维 + 升 4 层 + 文案三连 → 32 pass / 14 fail。
4. **Green Round 2**（_build_html 第 1/4/6 格）：cell1_html / cell4_html inline 渲染 + 第 6 格标签改名 → 43 pass / 3 fail。
5. **Green Round 3**（cell1 文本拼接微调）：把双数主值合并到同一 span，让 "8 / 12" 字面在 HTML 中可被 `in` 匹配 → 46/46 pass。
6. **Refactor + 终验**：`-W error` 严格模式无 warning；模块文件全文扫描，旧字串 "三维" / "3-6 层" / "1-2 层" / "昨日涨停溢价" / "昨日跌停今日" 全部清除。

**Key Decisions**：
- `_is_num` 谓词使用 `not isinstance(v, bool)` 防 `True/False` 被识别为 `int`（更严守 Python 数值守护）。
- 第 1 格主值 "ld / drop" 用同一颜色 span（任一维触发警戒整体红，否则绿），保留视觉对齐 dashboard 的整体语义。
- 升 4 层 reason 与 dashboard 等价："连续2日情绪良好（跌停≤5+加权竞价≥0），建议加至4层"（之前 bad_count=0 时 reason="" 行为被改写）。
- 第 4 格 sample_count=0 整格降级（title="昨日涨停 — 只" + 主值/sub 全 "—"），与 dashboard hover-tooltip 行为视觉等价。
- 不引入 Jinja2 / 任何模板引擎，cell1_html / cell4_html 内联 f-string（scope 范围外约束）。
- `send_screener_report` 公开签名零变更（INT-005 inspect.signature 严格比对保护）。

**Test Result**：`46 passed in 0.03s` · 0 warning · 32 UNIT + 7 INT + 7 BLIND · P0 29 + P1 12 + P2 5 全过。

### Database Changes (Structured)
```yaml
{}  # N/A — 本 Story 不涉及数据库变更
```

### API Endpoints Created (Structured)
```yaml
[]  # N/A — 本 Story 不涉及 API 变更
```

### Shared Models Created (Structured)
```yaml
{}  # N/A — 本 Story 不涉及新增共享模型；仅复用既有字典结构
```

### File List

**Modified**:
- `src/notify/email_sender.py` — 模块顶层 docstring 更新；`_calc_daily_advice` 全量重写（四维 + 升 4 层 + 文案三连）；`_build_html` 第 1/4/6 格改造（cell1_html inline + cell4_html inline + 第 6 格标签改名）

**Created**:
- `tests/notify/__init__.py` — empty package marker (existed)
- `tests/notify/test_email_decision_alignment.py` — 46 个测试用例从 `pytest.fail` 占位填实为真实断言 + 4 个 helper（`_sent` / `_leader_full` / `_render_html` / `_good_sent`）
- `docs/dev/logs/email-sync-1.1-dev-log.md` — Dev Log

**Untouched** (scope 外约束):
- `src/static/index.html` — dashboard 真源，全程不改
- `src/notify/email_sender.py::_send` — SMTP 链路保留
- `src/engine/sentiment_pool.py` / `src/engine/leader_feedback.py` — T0 探查确认上游字段已就绪，无需修改

### Dev Log Reference
`docs/dev/logs/email-sync-1.1-dev-log.md`

### Open Issues

无。所有 9 项 AC 已完整实现并通过测试，准备移交 QA *review。

---

## QA Results

## QA Review

- **Round**: 1
- **Risk Level**: MEDIUM
- **Review Mode**: automated_plus_spot_check
- **Gate**: PASS
- **Tests**: 46/46 automated (UNIT 32 + INT 7 + BLIND 7), E2E skipped (skip_e2e=true; INT tests cover same surface)
- **AC Coverage**: 9/9 fully verified (100%)
- **Blind Spots**: 7/7 covered (BOUNDARY 4 / ERROR 2 / FLOW 1)
- **Issues**: 0 critical / 0 high / 0 medium / 1 low (line-number drift, non-blocking)
- **Out-of-scope observation**: `DATA_DIR` + `json` 在 `src/notify/email_sender.py:419-451` 被引用但顶部未 import（commit `45baa67` 引入，predates this Story）；`try/except` 静默吞没 NameError，industry/heat 缓存 fallback 路径目前是死代码。建议另开 Story 修复。
- **Gate File**: `docs/qa/gates/email-sync-1.1-email-content-align-dashboard.yml`
- **Evidence**: 无（无任何 issue 触发证据收集）
- **Negative-assertion grep on module file**: `三维` / `3-6 层` / `1-2 层` / `昨日涨停溢价` / `昨日跌停今日` 全部 0 occurrence，BR-5.1/AC4/AC3/AC7/AC8 文案清理完成。
- **Dashboard 真源未动**: `src/static/index.html:1196-1268` (dailyAdvice) 与 `:505-600` (hero-metrics) 在本 Story commit 范围外；`send_screener_report` 公开签名零变更（INT-005 inspect.signature baseline）。

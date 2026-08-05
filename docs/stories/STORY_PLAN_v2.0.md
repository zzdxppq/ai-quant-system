# Story 创建计划：Epic 11 + Epic 12 (v2.0)

> **SM**: 萧何 | **日期**: 2026-06-03
>
> 本计划汇总 8 个 Story 的拆分逻辑、状态、依赖关系。
> **完整样例**：Story 11.1 + Story 12.2 已落盘（参考标准）。
> **精简版**：其余 6 个 Story 的纲要 + 关键 AC + 关键文件清单，Dev 可按需展开。

---

## Epic 11: 持仓管理 + 实时盈亏（4 Story）

| Story | 标题 | 状态 | 优先级 | 复杂度 | 依赖 |
|-------|------|------|--------|--------|------|
| **11.1** | 手动录入开仓（POST /api/positions） | ✅ 完整样例已落盘 | P0 | medium | — |
| **11.2** | 实时盈亏刷新任务（refresh_pnl_job） | 📋 纲要 | P0 | medium | 11.1 |
| **11.3** | 信号对照 + 状态机（close API） | 📋 纲要 | P0 | high | 11.1, 11.2 |
| **11.4** | 持仓 UI（首页状态条 + 持仓 Tab） | 📋 纲要（**等 UX 验收**） | P0 | medium | 11.1, 11.2, 11.3 |

### Story 11.2 纲要

**核心目标**：APScheduler 追加 1 个 job，60s 刷新 open 持仓的 current_price

**关键文件**：
- 新建：`src/engine/position.py::refresh_pnl_job()`（~80 行）
- 修改：`main.py` 追加 cron 配置
- 新建：`tests/engine/test_position_refresh.py`（~100 行）

**关键 AC**：
- AC1: 交易日 9:30-15:00 每 60s 触发（排除午休 11:30-13:00）
- AC2: 同持仓 60s 内防抖
- AC3: 行情失败 → price_stale=true + 不抛错
- AC4: 连续 3 次失败 → mark_untrackable → 停止刷新该持仓
- AC5: 触发止盈/止损阈值 → alert_level 升级

**业务规则**（PRD §6 Epic 11.2 AC1 BR-1.1~1.3）

**累积上下文**：依赖 Story 11.1 的 `Position` model + `position_store.update_pnl()`

### Story 11.3 纲要

**核心目标**：信号对照 + 平仓 API + 状态机

**关键文件**：
- 新建：`src/engine/position.py::check_signal_validity()`（~50 行）
- 新建：`src/engine/position.py::close_position()`（~60 行）
- 新建：`src/data/position_store.py::update_status()` / `append_history()`（~50 行）
- 修改：`src/api/app.py` 追加 POST /api/positions/{id}/close + GET signal-validity
- 新建：`tests/engine/test_position_close.py`（~150 行）

**关键 AC**：
- AC1: 当日 screener 不命中 → signal_still_valid=false
- AC2: 平仓 API（支持部分平仓 sell_shares < shares → status=partial_close）
- AC3: 止盈止损阈值提醒（不自动平仓，只标记 alert_level=red）

**业务规则**（PRD §6 Epic 11.3 AC1-3 BR-1.1~3.2）

**状态机**：`open` → `partial_close` → `closed` / `force_closed`

**累积上下文**：复用 `cross_validator.evaluate()` + `screener_history` 关联

### Story 11.4 纲要

**核心目标**：UI 落地（UX 视觉契约：`docs/design/variants/variant-1-jizhi.html`）

**关键文件**：
- 修改：`src/static/index.html` 追加 持仓状态条 + 持仓 Tab + 平仓弹窗
- 新建：`docs/ux/position-ui-spec.md`（**等 UX Agent 输出**，基于 variant-1-jizhi.html 派生）

**关键 AC**：
- AC1: 首页持仓状态条（始终可见，总市值/总盈亏/持仓数/预警数）
- AC2: 持仓 Tab 详情页（open + recent-closed 列表）
- AC3: 平仓弹窗（录入 sell_price + sell_shares）
- AC4: 数字滚动动画（不刷新整页）
- AC5: reduced-motion 回退

**业务规则**（PRD §6 Epic 11.4 AC1 BR-1.1~1.2）

**重要阻塞**：UX 视觉契约验收后才开始此 Story。

---

## Epic 12: 多标的回测扩展（4 Story）

| Story | 标题 | 状态 | 优先级 | 复杂度 | 依赖 |
|-------|------|------|--------|--------|------|
| **12.1** | 历史选股池准备（screener_history 校验） | 📋 纲要 | P0 | low | — |
| **12.2** | 多标的逐日回放（核心引擎） | ✅ 完整样例已落盘 | P0 | high | 12.1 |
| **12.3** | 分组统计 + 对比曲线 | 📋 纲要 | P0 | medium | 12.2 |
| **12.4** | 单标交易明细导出（CSV） | 📋 纲要 | P1 | low | 12.2 |

### Story 12.1 纲要

**核心目标**：回测前的数据完整性校验 + 自动补缺

**关键文件**：
- 新建：`src/engine/multi_backtest.py::_load_signals()`（~80 行）
- 新建：`tests/engine/test_multi_backtest_prepare.py`（~80 行）
- 修改：`src/api/app.py` 追加 GET /api/backtest/multi-stock/prepare

**关键 AC**：
- AC1: 校验 screener_history 缺失 close_price / next_day_open 数量
- AC2: 缺失 > 5% 自动调 `repair_missing_close_prices`
- AC3: 补缺后仍缺失 > 5% → 412 + 提示先跑补缺
- AC4: 缺失 > 10% → 拒绝执行

### Story 12.3 纲要

**核心目标**：分组统计 + v1.0 vs v2.0 对比曲线

**关键文件**：
- 新建：`src/engine/multi_backtest.py::_build_result()`（分组统计逻辑，~100 行）
- 修改：`src/static/index.html` 回测 Tab 追加对比视图（Chart.js 或 SVG 折线）
- 新建：`tests/engine/test_multi_backtest_grouping.py`（~100 行）

**关键 AC**：
- AC1: 按周期阶段分组（7 阶段）
- AC2: 按信号等级分组（strong/normal/watch）
- AC3: 按连板数分组（2/3/4/5+）
- AC4: 样本 < 5 标 `note="样本不足"`
- AC5: v1.0 vs v2.0 同一图表对比两条权益曲线

### Story 12.4 纲要

**核心目标**：导出完整交易明细为 CSV

**关键文件**：
- 修改：`src/api/app.py` 追加 GET /api/backtest/multi-stock/export
- 新建：`tests/api/test_multi_backtest_export.py`（~60 行）

**关键 AC**：
- AC1: 流式 CSV 输出（避免大文件内存压力）
- AC2: 文件名格式 `multi_backtest_YYYYMMDD_YYYYMMDD.csv`
- AC3: 含全部 trade 字段（18 列）

---

## 实施顺序建议

```
[独立] 11.1 + 12.1 (可并行)
   ↓
[独立] 11.2 (依赖 11.1) + 12.2 (依赖 12.1)  ← 可并行
   ↓
[独立] 11.3 (依赖 11.1, 11.2) + 12.3 (依赖 12.2) + 12.4 (依赖 12.2) ← 可并行
   ↓
[最后] 11.4 (依赖 11.1, 11.2, 11.3 + UX 验收)
```

**关键路径**：11.1 → 11.2 → 11.3 → 11.4

**MVP 推荐**（如时间紧）：11.1 + 11.2 + 11.3 + 12.2 + 12.4（核心闭环）

---

## 待 Dev 的 8 件事

1. 拉取 `docs/architecture/position-module.md` 与 `docs/architecture/multi-backtest-module.md` 通读
2. 拉取 `docs/PRD.md` v2.0 §6 Epic 11/12 AC 原文
3. 按 Story 11.1 / 12.2 完整样例展开其余 6 个 Story
4. 11.4 必须等 UX variant-1-jizhi.html 验收
5. 所有 PR 提交时引用 Story ID
6. 每个 Story 完成后填写 AC 追溯矩阵
7. 完成后状态 Review（等 QA）
8. QA 通过后 Done

---

## 变更日志

| 日期 | Agent | 状态转换 | 详情/链接 |
|------|-------|----------|-----------|
| 2026-06-03 | SM (萧何) | Created | 8 Story 拆分计划 + 2 完整样例（11.1, 12.2） |

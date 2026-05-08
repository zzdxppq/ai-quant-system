# Dev Log: email-sync-1.1

## Phase Tracker

| Phase | Status | Round | Notes |
|---|---|---|---|
| T0: 上游数据探查 | ✅ Done | 1 | 全字段就绪，无需升级 Architect |
| T1: AC1 第四维 drop_bad | ✅ Done | 1 | _calc_daily_advice 重构 |
| T2: AC2 升 4 层 | ✅ Done | 1 | prevGood 双判定 |
| T3: AC3+4+5 文案三连 | ✅ Done | 1 | 1.5/3/4 层 + 四维 |
| T4: AC6 第 1 格双数+箭头 | ✅ Done | 1 | inline cell1_html |
| T5: AC7 第 4 格接力情绪 | ✅ Done | 1 | inline cell4_html + 子项降级 |
| T6: AC8 第 6 格改名 | ✅ Done | 1 | _metric_cell 调用参数改 |
| T7-T9: 集成/回归/HTML 关键词 | ✅ Done | 1 | 7 INT + 7 BLIND 全过 |
| T10: 终验 + Self-Review | ⏳ In Progress | 1 | 46/46 pass，待 Self-Review Gate |

## T0 — 上游数据可用性探查

**结论**：全部就绪，**无需升级 Architect**。

| 字段 | 来源 | 行号 |
|---|---|---|
| `sentiment_data["weighted_auction_gain"]` | `src/engine/sentiment_pool.py` PoolSentiment dataclass | dataclass field |
| `sentiment_data["prev_day_weighted_auction_gain"]` | `src/engine/sentiment_pool.py:406` | save_sentiment 注入 |
| `sentiment_data["market"]["limit_down"]` | `src/engine/sentiment_pool.py` MarketAuctionStats | dataclass field |
| `sentiment_data["market"]["drop_over_9pct"]` | `src/engine/sentiment_pool.py:34, 369, 436` | MarketAuctionStats + history |
| `sentiment_data["market"]["prev_day_limit_down"]` | `src/engine/sentiment_pool.py:38, 398` | save_sentiment 注入 |
| `leader["yesterday_main_board_avg_auction"][avg_change_pct/sample_count/positive_count/negative_count]` | `src/engine/leader_feedback.py:501-509` | compute_yesterday_main_board_auction |
| `leader["yesterday_main_board_avg_auction"][median_change_pct/high5_count/flat2_count/low5_count/limit_down_count]` | `src/engine/leader_feedback.py:503-509` | 同函数已写入 |

注入路径：`compute_yesterday_main_board_auction` → `leader_data["yesterday_main_board_avg_auction"]` (`src/scheduler.py:447`) → 写盘 `latest_leader.json` → email_sender 读盘 (`src/scheduler.py:621-622`) → 传入 `send_screener_report(..., leader=...)`。

## Implementation Summary

### `src/notify/email_sender.py` — 主要改动

**1. 模块顶层 docstring**
- 旧：4 指标格描述
- 新：6 指标格 + 真源约束声明 (dashboard JS dailyAdvice + hero-metrics 是唯一真源)

**2. `_calc_daily_advice` 重构 (line 67~152)**
- docstring "三维警戒" → "四维警戒"
- 新增字段读取：`drop_over_9pct`、`prev_day_limit_down`、`prev_day_weighted_auction_gain`
- 新增 `_is_num` 辅助谓词（拒绝 `bool`，避免 `True`/`False` 算成 `int`）
- `has_drop` 守护变量加入"全空 → 数据加载中"判定 (BR-1.3)
- 新增 `drop_bad = has_drop and drop_over_9pct > 9` 严格阈值 (BR-1.2)
- warnings 顺序与 dashboard JS 一致：`ld → drop → w → lb`
- bucket=stop reason "三维" → "四维"
- bucket=warn position "1-2 层（小仓试错）" → "1.5 层（小仓试错）"，position_short "1-2层" → "1.5层"
- bucket=go 增加 `today_good && prev_good` 升仓判定 → "4 层（连续情绪良好）"，否则 "3 层（标准仓位）"
- 升仓 reason 与 dashboard 一致："连续2日情绪良好（跌停≤5+加权竞价≥0），建议加至4层"

**3. `_build_html` 指标格区段 (line ~205-300)**
- 第 1 格 (`cell1_html`)：自定义 inline HTML，label "竞价跌停 (>5⚠) / 跌>9% (>9⚠)"，主值 "ld / drop" 同 span（同色），箭头 ↑↓→（两边都有数才显示），副文本 "昨日跌停 N（差值±M）"。
- 第 4 格 (`cell4_html`)：自定义 inline HTML，label "接力情绪"，title 行 "昨日涨停 N 只 · 高开 N / 低开 N / 跌停 N"，主值 avg_change_pct，sub 行 "中位数 ±M% · 高开>5%:N · 平开±2%:N · 低开<-5%:N"。子字段缺失逐项以 "—" 占位 (BR-7.2)；sample_count=0 整格降级为 "—"。
- 第 6 格：`_metric_cell('昨日跌停今日', ...)` → `_metric_cell('昨日跌停平均反馈', ...)`。
- 其他格 (第 2/3/5 格)：保持原 `_metric_cell` 调用，不变。

### `tests/notify/test_email_decision_alignment.py`

- 46 个 `pytest.fail` 占位全部替换为真实断言，无 skip。
- 新增 `_sent` / `_leader_full` / `_render_html` / `_good_sent` 4 个 helper 减少重复。
- 1.1-INT-005 签名 baseline 写死在测试中，参数顺序+默认值精确比对。

### 数据契约影响

无破坏：
- `send_screener_report` 公开签名零变更（INT-005 保护）
- `_calc_daily_advice`、`_build_html` 是模块内部 helper，仅本模块调用
- `_send` SMTP 链路完全未触

## Resumption Guide

如中断后恢复：所有 9 任务已完成，仅 T10 终验剩 Self-Review Gate。直接执行：

```
python3 -m pytest tests/notify/ -v
```

确认 46/46 pass 后进入 *self-review。

## Final Test Run

```text
============================== 46 passed in 0.03s ==============================
```

- 单元测试 32 ✅（含 BLIND 7）
- 集成测试 7 ✅
- P0 / P1 / P2 全过；零 warning（`-W error` 严格模式）
- 模块文件全文已扫描："三维"、"3-6 层"、"1-2 层"、"昨日涨停溢价"、"昨日跌停今日" 全部清除

## Open Issues

无。

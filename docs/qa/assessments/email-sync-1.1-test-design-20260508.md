# Test Design: email-sync-1.1 (邮件推送内容逐字段对齐首页看板)

2026-05-08 | 宋慈 (QA)

## Overview

| Metric | Value |
|---|---|
| 测试设计层级 | Standard |
| 总场景数 | 46 |
| 单元测试 | 32 (70%) |
| 集成测试 | 7 (15%) |
| 蓝点场景 | 7 (15%) — BOUNDARY:4 / ERROR:2 / FLOW:1 |
| E2E | 0 (0%) — 无 UI/跨系统旅程 |
| P0 | 29 |
| P1 | 12 |
| P2 | 5 |

**说明**
- 测试技术栈：`pytest`（项目当前无 pytest 配置，新建测试目录 `tests/notify/`）。
- 真源对照：所有断言以 `src/static/index.html:1196-1268` 与 `:505-600` 为唯一真源（详见 scope 文件 `docs/prd/iteration-email-sync-scope.md`）。
- 测试 ID 前缀 `1.1` = email-sync-1.1。
- AC9 跨函数 + 公开签名校验 → 归类为 INT；其他 AC 都是 `_calc_daily_advice` / `_build_html` 内的纯逻辑/字符串 → UNIT。
- E2E（端到端）已显式排除：scope 范围外不动 `_send`/SMTP，无 UI 旅程，无跨系统集成；DoD 条款全部通过 UNIT+INT 即可验证。

---

## Scenarios by AC

### AC1: 引入第四维（跌幅>9% 个股数 警戒）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-UNIT-001 | U | P0 | drop_over_9pct=10 → drop_bad=True，warnings 含 "市场跌幅>9% 个股 10 只（>9 警戒线）" | 主路径触发 |
| 1.1-UNIT-002 | U | P0 | drop_over_9pct=9（边界）→ drop_bad=False（严格 `>` 而非 `>=`，BR-1.2） | 阈值边界负例 |
| 1.1-UNIT-003 | U | P0 | drop_over_9pct=10 + 其他 3 维全部正常 → bad_count=1 → bucket="warn" | 第四维独立纳入 bad_count |
| 1.1-UNIT-004 | U | P0 | drop_over_9pct=None（缺失）→ drop_bad=False，warnings 不含跌幅>9% 文案（BR-1.1） | 数据守护 — 缺失 |
| 1.1-UNIT-005 | U | P1 | drop_over_9pct="abc"（非数值）→ drop_bad=False（BR-1.1） | 数据守护 — 类型 |
| 1.1-UNIT-006 | U | P0 | sentiment_data 与 leader 全空，且 has_drop=False → 返回 "— 数据加载中 —" 分支（BR-1.3） | has_drop 纳入空数据判定 |

### AC2: 连续 2 日情绪好 → 升 4 层

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-UNIT-007 | U | P0 | bucket=go + todayGood=T(ld=3,w=+1) + prevGood=T(ld_prev=4,w_prev=+0.5) → position="4 层（连续情绪良好）"，position_short="4层" | 升仓主路径 |
| 1.1-UNIT-008 | U | P0 | bucket=go + todayGood=T + prevGood=F(w_prev=-0.5) → position="3 层（标准仓位）"，position_short="3层" | prevGood 失败回退（BR-2.2） |
| 1.1-UNIT-009 | U | P1 | bucket=go + todayGood=T + 边界 ld_prev=5,w_prev=0 → prevGood=T → position="4 层" | 升仓边界（BR-2.x） |
| 1.1-UNIT-010 | U | P0 | bucket=warn (drop_bad=T) + todayGood=T + prevGood=T → 不评估升仓，position="1.5 层（小仓试错）"（BR-2.1） | warn 分支不引入升仓 |
| 1.1-UNIT-011 | U | P0 | prev_day_limit_down 字段缺失 → prevGood=F → 退化 "3 层（标准仓位）"，不抛错（BR-2.2） | 缺失字段降级 |
| 1.1-UNIT-012 | U | P1 | prev_day_weighted_auction_gain=None → prevGood=F | None 字段降级 |
| 1.1-UNIT-013 | U | P2 | prevGood 判定不依赖 drop_over_9pct（即使昨日 drop_over_9pct=999，prevGood 仍仅看 ld+w）（BR-2.3） | 不污染升仓判定 |

### AC3: 谨慎参与文案对齐 → "1.5 层（小仓试错）"

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-UNIT-014 | U | P0 | bad_count=1 → position="1.5 层（小仓试错）"，position_short="1.5层" | 文案对齐 |
| 1.1-INT-001  | I | P0 | send_screener_report subject 在 bucket=warn 时含 "仓位1.5层"（BR-3.2） | subject 拼装级集成 |

### AC4: 可参与文案对齐 → "3 层" / "4 层（连续情绪良好）"

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-UNIT-015 | U | P0 | bad_count=0 + 升仓条件不满足 → position="3 层（标准仓位）"，position_short="3层" | 标准文案 |
| 1.1-UNIT-016 | U | P0 | bad_count=0 + 升仓条件满足 → position="4 层（连续情绪良好）"，position_short="4层" | 升仓文案 |
| 1.1-UNIT-017 | U | P0 | _calc_daily_advice 输出在 bad_count=0 分支不再含 "3-6 层"/"3-6层" 旧文案（负断言，BR-4.x） | 旧文案完全清除 |

### AC5: reason 维度数文案 → "四维"

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-UNIT-018 | U | P0 | bad_count=2 → reason 末尾含 "四维警戒中已 2 项触发，避免开仓。" | 文案对齐 |
| 1.1-UNIT-019 | U | P0 | bad_count=4（4 维全部触发）→ reason 含 "四维警戒中已 4 项触发"（BR-5.2） | 维度上限 |
| 1.1-UNIT-020 | U | P0 | `src/notify/email_sender.py` 全文（含 docstring）不再出现 "三维" 字串（BR-5.1） | 全文替换负断言 |

### AC6: 第 1 指标格双数 + 箭头 + 昨日对比

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-UNIT-021 | U | P0 | limit_down=8, drop_over_9pct=12, prev_day_limit_down=5 → HTML 第 1 格 label 含 "竞价跌停 (>5⚠) / 跌>9% (>9⚠)"，主值显示 "8 / 12"，箭头 "↑"，副文本 "昨日跌停 5（差值+3）" | 主路径 |
| 1.1-UNIT-022 | U | P0 | limit_down=3 < prev_day_limit_down=5 → 箭头 "↓"，副文本 "（差值-2）" | 箭头方向负向 |
| 1.1-UNIT-023 | U | P1 | limit_down=5 == prev_day_limit_down=5 → 箭头 "→"，副文本 "（差值±0 / 差值0）" | 箭头方向持平 |
| 1.1-UNIT-024 | U | P1 | limit_down=None, drop_over_9pct=12 → 主值 "— / 12"，无箭头 | UI 缺失降级 — 单字段 |
| 1.1-UNIT-025 | U | P1 | prev_day_limit_down=None → 副文本 "昨日跌停 —"，无差值 | UI 缺失降级 — 副文本 |
| 1.1-UNIT-026 | U | P2 | 三字段全 None → 第 1 格主值 "—"，与现状降级一致 | 完全降级 |

### AC7: 第 4 指标格改名 "接力情绪" + 加细分子项

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-UNIT-027 | U | P0 | leader["yesterday_main_board_avg_auction"] 全字段（含 median_change_pct/high5_count/flat2_count/low5_count/limit_down_count）齐全 → HTML 第 4 格 label="接力情绪"，title 行 "昨日涨停 N 只 · 高开 N / 低开 N / 跌停 N"，sub 含 "中位数 ±M% · 高开>5%:N · 平开±2%:N · 低开<-5%:N" | 主路径 |
| 1.1-UNIT-028 | U | P1 | median_change_pct=None → sub 中 "中位数 —" 占位，其他子项不消失（BR-7.2） | 单字段降级 |
| 1.1-UNIT-029 | U | P1 | sample_count=0 → title="昨日涨停 — 只"，主值/sub 子项均 "—" | UI Interaction 边界 |
| 1.1-UNIT-030 | U | P0 | HTML 全文不再出现旧标签 "昨日涨停溢价"（负断言） | 旧标签完全清除 |

### AC8: 第 6 指标格改名 "昨日跌停平均反馈"

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-UNIT-031 | U | P0 | HTML 第 6 格 label="昨日跌停平均反馈" | 文案对齐 |
| 1.1-UNIT-032 | U | P0 | HTML 不再出现旧标签 "昨日跌停今日"（负断言） | 旧标签完全清除 |

### AC9: 不引入回归

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 1.1-INT-002 | I | P0 | 暂时清空 SMTP_USER（monkeypatch）→ send_screener_report 返回 False，stdout 含 "未配置 SMTP_USER" | 边缘 1：SMTP 未配置 |
| 1.1-INT-003 | I | P0 | sentiment_data=None + leader=None → _calc_daily_advice 返回 "— 数据加载中 —" 分支（bucket="go"，position="—"，reason=""） | 边缘 2：全空数据 |
| 1.1-INT-004 | I | P0 | hits=[] → 完整渲染 HTML 含 "无命中标的" 占位 | 边缘 3：空命中 |
| 1.1-INT-005 | I | P0 | inspect.signature(send_screener_report).parameters 与 baseline 完全一致（参数名+顺序+默认值+返回类型 `bool`）（BR-9.1） | 公开签名不变 |
| 1.1-INT-006 | I | P0 | 同一组 sentiment_data + leader fixture，覆盖 4 维触发组合 0/1/2/3/4 项 → 断言 bucket/position/position_short/reason 与 dashboard JS dailyAdvice 等价（DoD #1, #2，T7） | 算法一致性集成 |
| 1.1-INT-007 | I | P0 | 完整渲染 HTML 后断言：含新关键词（"四维"、"1.5 层（小仓试错）"、"接力情绪"、"昨日跌停平均反馈"）且不含旧关键词（"三维"、"3-6 层"、"昨日涨停溢价"、"昨日跌停今日"）（DoD #7，T9） | HTML 关键词集成 |

---

## Blind Spot Scenarios [BLIND-SPOT]

| ID | Category | Pri | Scenario | Ref |
|---|---|---|---|---|
| 1.1-BLIND-BOUNDARY-001 | BOUNDARY | P1 | drop_over_9pct=0（最小有效值）→ drop_bad=False，warnings 不含跌幅>9% | BOUNDARY-002 |
| 1.1-BLIND-BOUNDARY-002 | BOUNDARY | P2 | drop_over_9pct=9999（极大值）→ drop_bad=True，warnings 文案能正确格式化 | BOUNDARY-003 |
| 1.1-BLIND-BOUNDARY-003 | BOUNDARY | P1 | bad_count=4（4 维同时触发）→ bucket=stop，warnings 列表含 4 条 | BOUNDARY-004 |
| 1.1-BLIND-BOUNDARY-004 | BOUNDARY | P2 | w_avg="abc"（类型异常）→ has_w=False，w_bad=False，不抛错 | BOUNDARY-005 |
| 1.1-BLIND-ERROR-001  | ERROR    | P1 | leader["yesterday_main_board_avg_auction"] 整体缺失 → 第 4 格全部子项 "—"，不抛 KeyError | ERROR-003 |
| 1.1-BLIND-ERROR-002  | ERROR    | P1 | sentiment_data["market"]=None → has_ld=False, has_drop=False, ld_bad=False, drop_bad=False；不进入 KeyError | ERROR-003 |
| 1.1-BLIND-FLOW-001   | FLOW     | P2 | 数据加载中 → bucket=go but text="— 数据加载中 —" → send_screener_report subject 不应包含 "0层" 等异常拼装；hits 表正常渲染（FLOW: 异常状态下的渲染流程） | FLOW-003 |

**蓝点最低覆盖度自检**
- ✅ 每个输入字段有 BOUNDARY 场景（drop_over_9pct, w_avg, prev_day_*, sample_count）
- ✅ 每个外部依赖有 ERROR 场景（leader 子字典 / sentiment_data["market"]）
- ✅ FLOW 场景覆盖"数据加载中"非正常状态下的下游渲染（避免出现 "0 层" 异常拼装）
- N/A CONCURRENCY（单线程渲染）
- N/A DATA（无数据库写）
- N/A RESOURCE（_send 函数 scope 外约束，不动 SMTP 链路）

---

## Risk Coverage

本 Story 未生成 risk-profile.md（test_design_level=standard 不强制）。从 scope 文件 DoD 反推的 4 个隐性风险及其映射：

| Risk | 描述 | 缓解场景 |
|---|---|---|
| RISK-DASHBOARD-DRIFT | dashboard JS 与 Python 算法分歧 | 1.1-INT-006（4 维触发组合一致性） |
| RISK-LEGACY-COPY | 旧文案残留 | 1.1-UNIT-017、1.1-UNIT-020、1.1-UNIT-030、1.1-UNIT-032、1.1-INT-007 |
| RISK-MISSING-UPSTREAM | T0 上游字段未写入导致渲染异常 | 1.1-UNIT-024～026, 1.1-UNIT-028～029, 1.1-BLIND-ERROR-001/002 |
| RISK-API-BREAKAGE | send_screener_report 签名意外被改 | 1.1-INT-005（inspect.signature） |

---

## Execution Order

1. **P0 UNIT** (1.1-UNIT-001/002/003/004/006/007/008/010/011/014/015/016/017/018/019/020/021/022/027/030/031/032)
2. **P0 INT** (1.1-INT-001/002/003/004/005/006/007)
3. **P1 UNIT** (1.1-UNIT-005/009/012/023/024/025/028/029)
4. **P1 BLIND-SPOT** (1.1-BLIND-BOUNDARY-001/003, 1.1-BLIND-ERROR-001/002)
5. **P2 UNIT** (1.1-UNIT-013/026)
6. **P2 BLIND-SPOT** (1.1-BLIND-BOUNDARY-002/004, 1.1-BLIND-FLOW-001)

---

## Gate YAML Block

```yaml
test_design:
  scenarios_total: 46
  by_level:
    unit: 32
    integration: 7
    e2e: 0
    blind_spot: 7   # 已含在 unit/integration 之外单独计数（蓝点单元测试）
  by_priority:
    p0: 29
    p1: 12
    p2: 5
  blind_spot_scenarios:
    total: 7
    by_category:
      BOUNDARY: 4
      ERROR: 2
      FLOW: 1
      CONCURRENCY: 0
      DATA: 0
      RESOURCE: 0
  coverage_gaps: []
  notes:
    - E2E 显式为 0 — scope 范围外不改 SMTP 链路，无 UI 旅程
    - 1.1-UNIT-027 依赖 T0 上游字段确认；若 T0 暴露字段缺失，BR-7.1 要求先升级到 Architect 补上游数据
    - 1.1-INT-005 baseline signature: send_screener_report(cycle_phase, cycle_day, representative, leader, hits, signals, deviations=None, sentiment_data=None, ranking_data=None) -> bool
```

---

## Trace References

```text
Test design: docs/qa/assessments/email-sync-1.1-test-design-20260508.md
P0: 24
Test skeleton: tests/notify/test_email_decision_alignment.py
```

---

## Quality Checklist

**Standard Coverage**
- [x] 每个 AC 至少有 1 个测试
- [x] 测试层级合理（UNIT 优先；跨函数与签名校验归 INT；无 E2E 浪费）
- [x] 无重复覆盖
- [x] 优先级与风险对齐
- [x] ID 遵循 `1.1-{LEVEL}-{SEQ}` 约定
- [x] 场景原子化

**Blind Spot Coverage**
- [x] BOUNDARY: 每个新增输入字段（drop_over_9pct/prev_day_*/sample_count）覆盖
- [x] ERROR: 每个外部依赖（leader 子字典 / sentiment_data["market"]）覆盖
- [x] FLOW: "数据加载中"异常状态的下游渲染流程覆盖
- [x] [BLIND-SPOT] 标签全部正确

---

## Principles 实践纪要

- **Shift left**: 30/37 在 UNIT 层，仅 7 个真正跨函数/签名校验在 INT
- **Risk-based**: 全部 P0 都对应 DoD 条款（算法一致性 / 旧文案清除 / 公开签名不变 / 边缘分支不变）
- **Efficient**: 不重复——AC1 的"4 维全触发"由 1.1-UNIT-019 (reason 文案) + 1.1-BLIND-BOUNDARY-003 (bucket+warnings 数量) 从两个不同角度断言
- **Maintainable**: 测试骨架已生成 (`tests/notify/test_email_decision_alignment.py`)，Dev 直接填充实现，每个 case 默认 `pytest.fail` 防止漏实现

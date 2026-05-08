# Dev Log: decision-consistency-2.1 (9:27 决策快照单一真源)

- **Agent**: 墨子 (Dev)
- **Story**: docs/stories/decision-consistency-2.1-927-snapshot-single-source.md
- **Mode**: develop-story (TDD)
- **Started**: 2026-05-08
- **Completed**: 2026-05-08
- **Status**: Approved → Review

---

## T0: 算法落点决策（Architect 已交付）

**决策**：保留 `_calc_daily_advice` 在 `src/notify/email_sender.py:72-172`，**不**搬运到 `src/engine/daily_advice.py`。

**理由（沿用 Architect *review 2026-05-08）**：
1. 触面最小（不需要新建 src/engine/daily_advice.py + 同步搬运 tests/notify/test_email_decision_alignment.py）
2. scheduler 已大量使用 function-level import 同模式
3. BR-5.6 算法本体冻结，保留原位最易守住该约束

**Dev 落实**：scheduler 通过 `from src.notify.email_sender import write_advice_snapshot` 在 7b 步骤 lazy import（与既有 `send_screener_report` 完全同模式）。

---

## 实现顺序（按 Architect Recommendation）

> AC1 → AC4 → AC3 → AC2（先写盘 → 再确认顺序 → 再改邮件读 → 最后改看板读，便于回归隔离）

### AC1: write_advice_snapshot

**位置**：`src/notify/email_sender.py:69-153`

**核心思路**（满足 BR-1.6 持久化字段 + BR-5.6 算法本体不动）：

1. 调用 `_calc_daily_advice(sent, leader)` 拿到 bucket / text / position / position_short / reason（**不**修改算法）
2. **独立**重新计算 dimensions（ld_bad/drop_bad/w_bad/lb_bad）+ bad_count，保证持久化扩展字段与 `_calc_daily_advice` 内部分支严格等价（UNIT-002/004/007 + UNIT-016 边界 w_avg=0）
3. 构造 `inputs` 快照：5 主输入 + `main_board_leaders_summary` 仅保留 3 键（leader_name / signal / auction_change_pct，UNIT-010 简化结构契约）
4. UTF-8 + indent=2 + ensure_ascii=False 写入；写盘异常 print + return None 不抛错（UNIT-014 + UNIT-015 BLIND-ERROR）

**关键决策点**：
- dimensions 重算 vs 改 _calc_daily_advice 增字段 → 选**重算**（守住 BR-5.6 函数体不变；代价是双份预测维度逻辑，但用 UNIT-002/003/004/005/007/016 锁定）
- generated_at 字段使用 `now_cn().strftime("%Y-%m-%d %H:%M:%S")`（UNIT-013 用 monkeypatch.setattr(email_sender, "now_cn", ...) 验证）

### AC4: scheduler 流程顺序

**位置**：`src/scheduler.py:576-596`

插入 `# 7b. 决策快照（看板 + 邮件单一真源, decision-consistency-2.1）` 块：
- 位于 latest_signals.json 写之后（line 572-574）
- 位于 send_screener_report 调用之前（line 645-655）
- **非** `def _background_tasks()` 函数体内（确保 email 一定能读到当次写入，BR-4.3）

**双层兜底**：
- 内层：`write_advice_snapshot` 自带 try/except 静默写盘异常
- 外层：scheduler 包一层 try/except 防 import 失败 / 文件读异常等（与既有 8a/8b/8c 块同风格）

**INT-009/INT-010 静态分析断言**通过 `_line_no()` helper 验证：`signals_line < advice_line < bg_line`。

### AC3: email_sender 改读 latest_advice.json

**位置**：
- `src/notify/email_sender.py:52` send_screener_report 改一行
- `src/notify/email_sender.py:156-188` 新增 `_load_advice_from_disk()`

**字段反向重命名（BR-3.5）**：
| 磁盘字段 | 内部字段 |
|---|---|
| suggested_position | position |
| suggested_position_short | position_short |
| bucket / text / reason | （pass-through） |
| — | color / bg（由 bucket+text 重建） |

**color/bg 重建表**：
- bucket="stop" → ("#10b981", "#0a2a0a")
- bucket="warn" → ("#fbbf24", "#2a2a0a")
- bucket="go" + text=="— 数据加载中 —" → ("#6b7280", "#0d1220")
- bucket="go" 其他 → ("#ef4444", "#2a0f0f")

**字段完整性校验**：必须 5 必填字段（bucket / text / suggested_position / suggested_position_short / reason）齐全，否则 print "[邮件] 决策快照字段不全" + return None（UNIT-022 BLIND-BOUNDARY）。

**send_screener_report 公开签名字符级冻结**（INT-007 inspect.signature == email-sync-1.1 INT-005 baseline）：
```
(cycle_phase: str, cycle_day: int, representative: dict | None, leader: dict | None, hits: list[dict], signals: list[dict], deviations: list[dict] | None = None, sentiment_data: dict | None = None, ranking_data: dict | None = None) -> bool
```

### AC2: /api/daily-advice + 看板改读

**后端 endpoint**：`src/api/app.py:107-120`，紧跟 /api/leader 之后插入。占位响应模块级常量 `_DAILY_ADVICE_PLACEHOLDER` 与 BR-2.1 字段精确对齐（UNIT-018 严格相等）。

**前端 JS 改动（仅在 `<script setup>` 区域，不动模板）**：

1. 新增 `const advice = ref(null)` 与 `const leader = ref({})` 同位置（line 968 area）
2. `loadData` Promise.all 第 9 个 fetch：
   ```javascript
   fetch('/api/daily-advice').then(r => r.json())
   ```
   解构变量 `adviceRes` → `advice.value = adviceRes`
3. `dailyAdvice` computed 重写（从 ~70 行四维警戒计算 → ~12 行 advice.value 映射）：
   ```javascript
   const _BUCKET_TO_CLS = { stop: 'advice-stop', warn: 'advice-warn', go: 'advice-go' }
   const dailyAdvice = computed(() => {
       const a = advice.value
       if (!a || !a.bucket) {
           return { cls: 'advice-go', text: '— 数据加载中 —', reason: '', suggestedPosition: '—' }
       }
       return {
           cls: _BUCKET_TO_CLS[a.bucket] || 'advice-go',
           text: a.text || '— 数据加载中 —',
           suggestedPosition: a.suggested_position || '—',
           reason: a.reason || ''
       }
   })
   ```
4. **模板 HTML 字符级冻结**（INT-005/INT-015 双重断言）：
   - 行 505-509：hero-banner / ha-text / ha-position（dailyAdvice.cls / .text / .suggestedPosition）
   - 行 595-596：hero-reason（dailyAdvice.reason）
   - 行 657-666：decision-cell 内 dailyAdvice.cls / .text + advice-go / advice-warn 分支

模板字符级 baseline 冻结在 `tests/notify/fixtures/index_template_baseline.json`，由 INT-005 断言（AC2 不改模板）+ INT-015 独立断言（AC5 回归保护范畴）双重锁定。

---

## 测试矩阵

**总览**：48 用例（44 函数 + 1 parametrize × 5）/ 全部 PASS / 0.4s

| AC | UNIT | INT | 总数 | 蓝点 |
|---|---|---|---|---|
| AC1 | 16 | — | 16 | UNIT-015 ERROR / UNIT-016 BOUNDARY |
| AC2 | 3 | 6 | 9 | UNIT-019 ERROR |
| AC3 | 7 | 2 | 9 | UNIT-021 ERROR / UNIT-022 BOUNDARY |
| AC4 | — | 3 | 3 | — |
| AC5 | — | 5 | 5 | INT-013 FLOW / INT-016 FLOW |
| 端到端 | — | 2 (×5) | 2 | — |
| **合计** | 26 | 18 | 44 | 7 |

**关键基线**：
- INT-007 send_screener_report 签名 == email-sync-1.1 INT-005 baseline（字符级）
- INT-005 / INT-015 模板 HTML diff vs baseline.json（字符级）
- UNIT-026 `_calc_daily_advice` 函数体保留（含 "warnings" + "bad_count" tokens）
- INT-018 三方一致性矩阵（5 parametrize 覆盖 bad_count=0/1/2/3/4）：file["suggested_position_short"] ↔ subject "仓位{...}层" ↔ helper.position_short

**回归保护**（DoD #2）：
- 完整 pytest tests/ -W error → 99/99 PASS
- email-sync-1.1 既有 46 用例 zero regression（_calc_daily_advice 算法本体未动；send_screener_report 签名未动）

---

## Architect Issues 收敛

- **Major Issue #1（BR-4.3 路径选择）**：按 Architect 收敛意见走**路径 (b)** — 仅写文件，send_screener_report **不**新增 advice 入参；签名字符级冻结由 INT-007 锁定。
- **Minor Issue #1（Accumulated Context 表已过期）**：本 Story 范围不要求修改；data-dir-import-fix-1.1 已在 8ab7abf 修复 DATA_DIR/json import，本 Story 仅复用既有 import。
- **Minor Issue #2（dailyAdvice 行号 1196 vs 1198）**：以 1198 为准；Story 文档保留旧引用未更新（不阻塞实现）。

---

## Dev Self-Review (Step 9)

| 7 维度 | 结果 | 说明 |
|---|---|---|
| Requirement Consistency | ✅ | AC1-AC5 全部覆盖；Architect Major Issue #1 路径 (b) 收敛；不引入 advice 入参 |
| Security | ✅ | 仅文件 IO；无注入面；JSON 解析 try/except；写盘失败静默不外泄路径 |
| Testability | ✅ | 单元测试可隔离运行（DATA_DIR 通过 monkeypatch.setattr 模块级注入）；TestClient 覆盖 API；spy 覆盖 fallback 路径 |
| Maintainability | ✅ | 模块级常量分组（_BUCKET_COLOR / _GO_COLOR_* / _LOADING_TEXT / _REQUIRED_ADVICE_KEYS）；helper 单一职责；不引入新模块 / 新依赖 |
| Compatibility | ✅ | send_screener_report 签名字符级冻结；_calc_daily_advice 函数体冻结；模板 HTML 字符级冻结；既有 latest_*.json 契约不变 |
| Data Integrity | N/A | 本 Story 无 DB 变更；新文件 latest_advice.json 已在 .gitignore 覆盖（与 latest_sentiment.json 同 namespace）|
| Automation | ✅ | 测试纯 pytest，无外部依赖；`pytest tests/ -W error` 通过 CI 标准 |

**未削弱测试**（dev customization 第 4 条）：48 用例严格按 QA test-design 实现，所有 raise NotImplementedError 已替换为真实断言；零 skip / 零 xfail。

---

## File List（同 Story Dev Agent Record）

详见 docs/stories/decision-consistency-2.1-927-snapshot-single-source.md › Dev Agent Record › File List。

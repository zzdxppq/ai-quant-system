# Dev Log: watch-pool-snapshot-2.2 (次日观察池显示昨日 15:45 冻结快照)

- **Agent**: 墨子 (Dev)
- **Story**: docs/stories/watch-pool-snapshot-2.2-review-show-yesterday-frozen.md
- **Mode**: develop-story (TDD, plan)
- **Started**: 2026-05-08
- **Status**: Approved → InProgress

---

## T0: import 删除决策（Architect L-2 已交付）

**决策**：**删除** `src/api/app.py:296` 局部 import `from src.engine.daily_review import build_watch_pool_from_ranking`。

**理由**（沿用 Architect *review 2026-05-08）：
1. 静态分析（grep）：删除 line 336-343 重算块后，`build_watch_pool_from_ranking` 在 `app.py` 中无任何其他引用。
2. 唯一调用方 `daily_review._generate_watch_pool` 仍保留（daily_review.py:754），算法本体不动。
3. KISS — 未使用 import 是 dead code；ruff F401 会持续告警。
4. 回滚成本低 — git 历史保留删除 commit；若未来需要恢复兜底，重加 1 行 import 即可。
5. 与 BR-5.4 "不引入新依赖" 精神一致：减少 app.py 与 daily_review 内部实现的耦合面。

**Dev 落实**：在 T1 同一 commit 中删除 line 296 的 import + line 336-343 的重算块 + 重写 line 286-294 的 docstring（合并 L-1）。

---

## 实现顺序（按 Architect Recommendation #1）

> AC1 → 创建 SHA256 baseline → AC2/AC3/AC4 测试 → AC5 跨 Story 回归 → BLIND-SPOT → T5 时序

1. **T1**：改 `src/api/app.py`（删 line 336-343 + line 296 + 重写 line 286-294 docstring）—— 同一 commit
2. **T1.5**：冻结 `review.html` / `main.py` / `scheduler.py` / `daily_review.py` SHA256 + /api/review 顶层 keys 写入 `tests/fixtures/watch_pool_snapshot_baselines.json`
3. **T2-T7**：替换 `tests/test_review_watch_pool_snapshot.py` 全部 NotImplementedError 为真实实现
4. **T8**：`pytest tests/ -W error` 全绿（baseline 99 + 本 Story 34 = 133）

---

## T1: src/api/app.py 改动

**位置**：`src/api/app.py:284-361`（修改前）

**3 处变更**（同一 commit）：

### (a) 删除 line 336-343 的 watch_pool 重算块
原代码：
```python
# watch_pool 始终用最新 ranking 重算（按当前规则：top30+45%+≥2连板+主板）
if ranking_file.exists():
    try:
        ranking_payload = json.loads(ranking_file.read_text())
        ranking_rows = ranking_payload.get("ranking") or []
        review_data["watch_pool"] = build_watch_pool_from_ranking(ranking_rows)
    except Exception:
        pass
```
→ **整块删除**（含开头注释行 + try/except）。删除后 `review_data["watch_pool"]` 完全等于 review_history / latest_review.json 中的冻结字段。

### (b) 删除 line 296 的局部 import
```python
from src.engine.daily_review import build_watch_pool_from_ranking
```
→ **删除**（T0 决策）。删除后 ranking_file 变量在函数内不再被任何代码使用，但保留 `ranking_file = DATA_DIR / "latest_ranking.json"` 行不动（避免误伤其他可能引用 — 实际全删后也不会有引用，但分两步谨慎）。**审慎重读后**：决定一并删除 `ranking_file = DATA_DIR / "latest_ranking.json"`，因为删除重算块后该变量不再被使用，保留即 dead code（同 ruff F841 unused variable 告警风险）。

### (c) 重写 line 286-294 docstring（合并 L-1）
原 docstring 误述"watch_pool 始终用 latest_ranking 重算"。新 docstring 反映冻结快照语义（参考 Story §AC1 + §背景）：
```
"""获取复盘数据。

时间门控（用户要求）：当天 15:00 前显示昨日复盘，15:00 后显示今日。
实现：
  · now < 15:00：从 review_history.json 取最近一条 date != today 的记录
  · now >= 15:00：返回 latest_review.json（今日 cron 在 15:45 跑完）

watch_pool（次日观察池）= review_history / latest_review.json 中
15:45 冻结快照（_save_review 已持久化）；不再实时重算 — 看板需与
"昨晚定的明日关注股" 保持一致（watch-pool-snapshot-2.2）。

scorecard / promotion_summary 仍用当前公式重算（用户要求"历史快照
也用新公式"），仅 watch_pool 是冻结快照。
"""
```

---

## T1.5: SHA256 Baseline 文件

**位置**：`tests/fixtures/watch_pool_snapshot_baselines.json`

冻结于 T1 commit 前的 4 个文件 SHA256 + /api/review 顶层 17 keys：
| 文件 | SHA256 |
|---|---|
| src/static/review.html | `209b33ec...` |
| main.py | `6f522431...` |
| src/scheduler.py | `940936c3...` |
| src/engine/daily_review.py | `393cdcbc...` |

`api_review_top_keys` 含 17 字段：concept_zt_stats / date / failed_promotion_list / highest_board / lianban_ladder / limit_up_count / main_board_limit_up / main_theme / market_summary / prev_board_groups / promotion_summary / relay_env / scorecard / sector_groups / sector_zt_stats / theme_strength / watch_pool。

---

## T2-T7: 测试实现

**文件**：`tests/test_review_watch_pool_snapshot.py`（替换 skeleton 全部 NotImplementedError）

**总数**：32 scenarios → 34 collected（UNIT-009 parametrize × 3）

### 实施关键决策

1. **Mock 边界（Architect Recommendation #4 落地）**：
   - `_patch_now(monkeypatch, ...)` 使用 `monkeypatch.setattr("src.config.now_cn", lambda: target)`，**不**直接 patch `src.api.app.now_cn`。原因：`get_review` 函数体内做 `from src.config import now_cn`（line 300），每次调用重新查 `src.config.now_cn`，patch 上游即可拦截。
   - `patched_data_dir` 用 `monkeypatch.setattr("src.api.app.DATA_DIR", tmp_path)` 隔离磁盘。

2. **黑名单元标签选择**：
   - 初版用了"某行业方向"作为元标签 fixture → 实际 `concept_blacklist.META_CONCEPT_BLACKLIST` 不包含此字符串 → UNIT-004 / BLIND-DATA-001 失败。
   - 修正：改用真实黑名单成员"融资融券" / "沪股通"（`META_CONCEPT_BLACKLIST` 行 14）。
   - 教训：Mock fixture 应使用真实的领域常量，不能凭印象造词。

3. **AC5 INT-005（邮件路径不受影响）实施收敛**：
   - skeleton TODO 写"调 send_screener_report 后断言 result.body 不含 watch_pool"。
   - 实际改为**静态分析**：grep `src/notify/email_sender.py` 不含 `watch_pool` 字符串 — 源码不引用即结构性反例（更强 + 更易维护，无需 mock SMTP / fixture 邮件 IO）。

4. **AC5 INT-006（decision_tracker）实施**：
   - skeleton TODO 模糊指出"调用 create_premarket_record"。
   - 实际：模拟 scheduler.py:625-636 的真实路径 — 写 `latest_review.json`（fixture B）+ `latest_ranking.json`（fixture C）→ 自己读 `latest_review.json` 的 watch_pool 传给 `create_premarket_record` → 断言记录中 watch_pool[0].code == "600519"（来自 latest_review）！= "000001"（ranking）。证明 scheduler 路径继续从 `latest_review.json` 读，不受 Story 2.2 影响。

5. **T5-INT-009（15:30 边界）路径确认**：
   - Story 测试设计原文 + skeleton TODO 在此 case 有歧义（NO latest_review.json vs 写昨日 latest）。
   - 我选**写昨日 latest** 路径（即 fixture A 内容写到 `latest_review.json`）— 因为 `n=15:30 >= cutoff=15:00`，代码跳过 history 分支直读 latest；现实场景下 latest 仍是昨日内容（今日 15:45 cron 还没跑）。这正确反映了"15:00-15:44 窗口看昨日"的语义。

6. **UNIT-013（pytest collect 基线）**：
   - 锁死 `EXPECTED_TOTAL = 133`（99 baseline + 34 本 Story）。
   - 实测 `pytest --collect-only -q` 末行 = `133 tests collected` ✓。
   - Architect L-3 风险已收敛（重名 fixture 让 case 数变少而 PASS 仍绿 → collect 总数会立即触发警报）。

### Test 分布（34 collected）

| 类 | 用例 | 累计 |
|---|---|---|
| TestAC1NoWatchPoolOverride | UNIT-001/002/003/004 + INT-001/002 | 6 |
| TestAC2HistoryHasWatchPool | UNIT-005/006/007 + INT-003 | 4 |
| TestAC3ReviewHtmlFrozen | UNIT-008 + UNIT-009×3（parametrize） | 4 |
| TestAC4SchedulerFrozen | UNIT-010/011/012 | 3 |
| TestAC5DoDRegression | INT-004/005/006 + UNIT-013 | 4 |
| TestT5TimeAxisConsistency | INT-007/008/009/010 | 4 |
| TestBlindSpot{Boundary,Error,Flow,Data} | BLIND-BOUNDARY-001/002/003/004 + BLIND-ERROR-001/002/003 + BLIND-FLOW-001 + BLIND-DATA-001 | 9 |
| **总计** | | **34** ✓ |

---

## T8: 最终验收

**`pytest tests/ -W error` 实测**（2026-05-08）：
```
============================== 133 passed in 0.82s ==============================
```

| 来源 | Count |
|---|---|
| email-sync-1.1 baseline | 46 |
| decision-consistency-2.1 | 48 |
| fallback-industry-concept | 5 |
| watch-pool-snapshot-2.2（本 Story） | **34** |
| **Total** | **133** ✓ |

### Architect 3 项 Low Issue 关闭

| Issue | Mitigation | 状态 |
|---|---|---|
| L-1：docstring 与代码语义矛盾 | T1 同 commit 重写 line 286-298 docstring；UNIT-003 反射 `__doc__` 断言守护 | ✅ 关闭 |
| L-2：未删除的 dead import | T1 同 commit 删除 line 296 import + line 300 `ranking_file` 变量；UNIT-002 grep 断言守护 | ✅ 关闭 |
| L-3：测试基线数字硬编码漂移 | UNIT-013 通过 `pytest --collect-only` 总数 == 133 强校验 | ✅ 关闭 |

### BLIND-SPOT 9/9 覆盖

- **BOUNDARY** (4): legacy 缺字段 / history=[] 兜底 / wp=[] 透传 / n==15:00 边界 → latest 分支
- **ERROR** (3): history JSON 损坏 → latest 兜底 / 两文件均缺 → 返回 {} / latest_ranking.json 缺失不影响响应
- **FLOW** (1): 无缓存 — 写后立即 GET 反映新内容（手动 *refresh-review 场景）
- **DATA** (1): line 334 dict() 隔离 — 响应过滤元标签后磁盘文件未被 mutate

### 三方一致性（用户 2026-05-08 反馈直接缓解）

T5 INT-007/008/009/010 跨 4 时段验证 watch_pool 来源：
- 9:30 / 14:30：来自 history 中昨日 entry（fixture A）✓
- 15:30：来自 latest_review.json（昨日内容，今日 cron 未跑）✓
- 15:50：来自 latest_review.json（今日 15:45 新冻结，fixture B）✓

跨时段断言 `wp != ranking 重算结果`（fixture C），证明删除 line 336-343 后 latest_ranking.json 已**完全无影响**。

---

## Open Issues

无。Architect 3 项 Low Issue 全部以 mitigation 测试守护 + commit 同步落地。

---

## 本次 Story 文件改动汇总

**Modified**:
- `src/api/app.py` — 删除 line 296 `from src.engine.daily_review import build_watch_pool_from_ranking` import；删除 line 300 `ranking_file = DATA_DIR / "latest_ranking.json"` 变量；删除 line 336-343 watch_pool 重算块；重写 line 286-298 docstring（共 3 处变更，1 commit）

**Created**:
- `tests/test_review_watch_pool_snapshot.py` — 实现 32 scenarios（34 collected）覆盖 AC1-AC5 + 9 BLIND-SPOT + T5 时序矩阵
- `tests/fixtures/watch_pool_snapshot_baselines.json` — SHA256 baselines + /api/review 顶层 keys（4 frozen files + 17 keys）
- `docs/dev/logs/watch-pool-snapshot-2.2-dev-log.md` — 本文件

**Unchanged (字符级冻结，AC4)**:
- `src/static/review.html` ✓
- `main.py` ✓
- `src/scheduler.py` ✓
- `src/engine/daily_review.py` ✓

---

## 状态转换

`Approved → Review`（待 QA *review）。HANDOFF 至 qa *review watch-pool-snapshot-2.2。


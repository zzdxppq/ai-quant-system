# Test Design: watch-pool-snapshot-2.2 — 次日观察池显示昨日 15:45 冻结快照

**Date**: 2026-05-08 | **Author**: Turing (QA) | **Story**: watch-pool-snapshot-2.2 | **Level**: standard

---

## Overview

| Metric | Value |
|---|---|
| **Total scenarios** | 32 |
| **By level** | Unit: 15 (47%) · Integration: 17 (53%) · E2E: 0 |
| **By priority** | P0: 9 · P1: 16 · P2: 7 · P3: 0 |
| **Blind-spot scenarios** | 9 (BOUNDARY 4 / ERROR 3 / FLOW 1 / DATA 1) |
| **Coverage** | AC1 ✓ · AC2 ✓ · AC3 ✓ · AC4 ✓ · AC5 ✓ |
| **Story type** | brownfield-enhancement (full-stack delete + contract-freeze) |
| **Applicable blind-spot categories** | BOUNDARY · ERROR · FLOW · DATA (CONCURRENCY/RESOURCE 不适用 — 本 Story 仅删除一处同步读取覆盖块，无新并发/资源持有) |

---

## Test Strategy 摘要

本 Story 是一次**接近纯删除**的改造（`src/api/app.py:get_review` 删除 line 336-343 的 watch_pool 实时重算块 + 删 line 296 import + 重写 line 286-294 docstring），**外加 4 个 "字符级冻结" 契约**（review.html / main.py / scheduler.py / daily_review.py）。

测试金字塔向 **unit + integration** 倾斜，**不写 E2E**：

- **Unit (47%)**：契约冻结（SHA256 baseline）+ 函数级行为锁定（`_save_review`、`get_review` mock DATA_DIR）+ 源码静态断言（import / docstring 删除验证）
- **Integration (53%)**：完整 `/api/review` route 在多时段 mock `now_cn()` 下的 watch_pool 来源验证（T5 主验收）+ 跨 Story 回归（9:27 邮件链路 + decision_tracker）+ 边缘场景（缺文件 / 损坏 JSON / 空快照）
- **E2E**：N/A — 本 Story 无 UI 交互改动（review.html 字符级冻结，区域 D 模板不动），无需浏览器侧验证

**Mock 边界**（采纳 Architect Recommendation #4）：
- 时序测试 patch `src.config.now_cn`（**不是** `src.api.app.now_cn`），因为 `app.py:295` 用的是 function-local `from src.config import now_cn` import。模块顶 line 11 的 `now_cn` 在 `get_review` 内被本地 import 覆盖，patch 上游 `src.config.now_cn` 即可拦截两个引用点。
- DATA_DIR 用 `monkeypatch.setattr("src.api.app.DATA_DIR", tmp_path)`，配合 fixtures 写入 `latest_review.json` / `review_history.json` / `latest_ranking.json` 的"昨日 vs 今日不同"夹具。

**Hash baseline 策略**（采纳 Architect Recommendation #5）：
- 新建 `tests/fixtures/watch_pool_snapshot_baselines.json` 存放 `review.html` / `main.py` / `scheduler.py` / `daily_review.py` 的 SHA256（一次性写入，由 Dev 在 T1 commit 时刻 freeze；CI 比对 hash），比 grep 字面匹配更稳。BR-3.1 / BR-3.2 / BR-3.3 / BR-4.1-4.4 全部转为单一 SHA256 断言。
- review.html 区域 D 字符级特征同步保留 substring 断言（`watchPool = computed(...)` / `fetch('/api/review')` / `<!-- ============ 区域 D`）作为 SHA256 失败时的诊断辅助。

---

## Scenarios by AC

### AC1: review API 不再覆盖 review_data["watch_pool"]

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.2-UNIT-001 | U | P0 | `get_review` 内 mock DATA_DIR + 写入 `review_history.json`（含已知 watch_pool）→ 调用后返回 dict 的 `watch_pool` 字段 == history 中昨日 entry 的 `watch_pool`（不重算）| 主验收：删除 line 336-343 后唯一行为变化 |
| 2.2-UNIT-002 | U | P1 | 静态读取 `src/api/app.py` 文本 → 断言 `from src.engine.daily_review import build_watch_pool_from_ranking` 已不存在；同时 `# watch_pool 始终用最新 ranking 重算` 注释行已不存在 | T0 + T1 删除决策落地（防 commit 部分回退）|
| 2.2-UNIT-003 | U | P1 | 反射读 `src.api.app.get_review.__doc__` → 不含 `始终用 latest_ranking 重算`；含 `15:45 冻结` 或 `review_history` 关键字 | L-1 docstring 同步（防文档腐化）|
| 2.2-UNIT-004 | U | P1 | 构造 `review_data['watch_pool']` 含 `concepts: ['某行业方向', '半导体']`（前者元标签）→ /api/review 返回 watch_pool[0].concepts 仅含 `半导体`（其余被 `_strip_meta_concepts_inplace` 过滤）| BR-1.4 概念清洗保留（防误删 line 395-396）|
| 2.2-INT-001 | I | P0 | 完整路由调用 `GET /api/review`（TestClient + mock DATA_DIR + 写两个文件 review_history.json + latest_ranking.json）→ 响应 JSON 的 `watch_pool` 等于 history 中昨日 entry 的 watch_pool（**不**等于 `build_watch_pool_from_ranking(latest_ranking.ranking)`）| AC1 端到端主验收：route 层透传 |
| 2.2-INT-002 | I | P0 | 完整路由 + history entry 的 `scorecard` / `promotion_summary` 故意写"过期值"（如 `scorecard={}`）→ 响应 JSON 的 `scorecard` / `promotion_summary` 是当前公式重算的**新值**（≠ history 原值）| BR-1（保留 line 345-359）：仅 watch_pool 冻结，scorecard 仍重算 |

### AC2: review_history.json 已含 watch_pool 快照（既有行为，本 Story 仅验收）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.2-UNIT-005 | U | P1 | 构造 `DailyReview` dataclass + watch_pool（≥1 个 WatchCandidate）→ 调 `_save_review(review)` → 读 `latest_review.json` 含 `watch_pool` key，长度匹配 | BR-2.1 既有 _save_review 写入路径不变 |
| 2.2-UNIT-006 | U | P1 | 同上 → 读 `review_history.json` 末尾 entry 含 `watch_pool` key + 与输入一致 | BR-2.4 history 含 watch_pool 字段 |
| 2.2-UNIT-007 | U | P2 | 同 date 调 `_save_review` 两次（不同 watch_pool）→ history 中该 date 仅 1 条且为最后一次（既有去重 line 1402-1404）| BR-2.5 幂等：手动 *refresh-review 不产生重复 |
| 2.2-INT-003 | I | P2 | 加载真实 `data/review_history.json`（项目内 6 条 entry）→ 末尾 entry watch_pool 中第一个元素含 13 个键：`code/name/board_count/industry/close/market_cap_yi/total_gain_pct/reason/watch_points/auction_range/top_concepts/is_main_board/pool_tag`| BR-2.3 schema 兼容性 + 数据契约验证 |

### AC3: 复盘页 D 区域 watch_pool 渲染保持冻结快照

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.2-UNIT-008 | U | P1 | `hashlib.sha256(Path('src/static/review.html').read_bytes()).hexdigest()` == `tests/fixtures/watch_pool_snapshot_baselines.json['review.html']` | BR-3.1 / BR-3.2 / BR-3.3 字符级冻结（前端零改动）|
| 2.2-UNIT-009 | U | P2 | review.html 文本断言（参数化 3 项）：(a) `watchPool = computed(() => review.value.watch_pool` 子串存在；(b) `fetch('/api/review')` 子串存在；(c) `<!-- ============ 区域 D` 子串存在 | SHA256 失败时的诊断辅助；明确告知"哪一处改了"|

### AC4: scheduler / cron 流程不变

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.2-UNIT-010 | U | P1 | `hashlib.sha256(Path('main.py').read_bytes()).hexdigest()` == baseline | BR-4.1 setup_scheduler 字符级冻结 |
| 2.2-UNIT-011 | U | P1 | `hashlib.sha256(Path('src/scheduler.py').read_bytes()).hexdigest()` == baseline | BR-4.2 / BR-4.3 cycle_update + screener_update 字符级冻结 |
| 2.2-UNIT-012 | U | P1 | `hashlib.sha256(Path('src/engine/daily_review.py').read_bytes()).hexdigest()` == baseline | BR-4.4 run_daily_review + _save_review 字符级冻结 |

### AC5: 不引入回归（DoD）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.2-INT-004 | I | P0 | `set(GET /api/review .json().keys())` == 18 字段冻结 baseline（date / limit_up_count / main_board_limit_up / sector_groups / main_theme / theme_strength / lianban_ladder / highest_board / prev_board_groups / sector_zt_stats / concept_zt_stats / failed_promotion_list / watch_pool / market_summary / scorecard / relay_env / promotion_summary 共 17 + 可能的可选 1 项 — 以实测 baseline 为准）| BR-5.1 顶层 schema 不变 |
| 2.2-INT-005 | I | P0 | 沿用 sibling fixture（test_email_decision_alignment.py 风格）调 `email_sender.send_screener_report(...)` → 邮件不读取 review_data['watch_pool']（mock IO，断言不打开 review_history.json / latest_review.json 内的 watch_pool 字段路径）| BR-5.7 9:27 邮件链路 0 影响 |
| 2.2-INT-006 | I | P1 | mock `latest_review.json`（含 watch_pool）→ 调 `decision_tracker.create_premarket_record(...)`（scheduler.py:625-636）→ 写入的 premarket record 中 watch_pool 来自 latest_review.json（**不**来自 latest_ranking.json）| BR-5.7 9:27 决策追踪语义保持（仍是昨日 15:45 冻结快照）|
| 2.2-UNIT-013 | U | P2 | `subprocess.check_output(['pytest', '--collect-only', '-q'])` 末行解析得到的总用例数 == 99（baseline）+ 本 Story 新增数（动态读取本测试文件的用例数）| L-3 测试基线漂移防护 |

### T5: 端到端时序一致性集成测试 [跨 AC1/AC2/AC3]

固定夹具（每个 case 共享）：
- `review_history.json`：含一条昨日（2026-05-07）entry，`watch_pool=[{code:'002081', ...}]`（夹具 A）
- `latest_review.json`：含一条今日（2026-05-08）entry，`watch_pool=[{code:'600519', ...}]`（夹具 B，仅 INT-009/010 时存在 / 不同）
- `latest_ranking.json`：含 30 条 ranking 行，使得 `build_watch_pool_from_ranking()` 会返回 `watch_pool=[{code:'000001', ...}]`（夹具 C，与 A、B 完全不同）

| ID | Lvl | Pri | Test | Why |
|---|---|---|---|---|
| 2.2-INT-007 | I | P0 | patch `src.config.now_cn` → 2026-05-08 09:30；GET /api/review → 响应 watch_pool == 夹具 A（昨日 history 冻结，**不等于** B 也不等于 C）| 9:30 时段：盘中早段不应受 latest_ranking 影响 |
| 2.2-INT-008 | I | P0 | patch now_cn → 2026-05-08 14:30；GET /api/review → 响应 watch_pool == 夹具 A | 14:30 时段：盘中晚段同样冻结 |
| 2.2-INT-009 | I | P0 | patch now_cn → 2026-05-08 15:30；移除 latest_review.json（模拟 today_run_daily_review 未跑，但 history 仍有昨日 entry）；GET /api/review → 响应 watch_pool == 夹具 A | 15:00-15:44 窗口：仍读昨日（latest 不存在 → 走 history 路径）|
| 2.2-INT-010 | I | P0 | patch now_cn → 2026-05-08 15:50；写入今日 latest_review.json（模拟 15:45 cron 已写）；GET /api/review → 响应 watch_pool == 夹具 B（今日新冻结） | 15:45+ 时段：今日新快照接管 |

---

## Blind Spot Scenarios [BLIND-SPOT]

| ID | Category | Lvl | Pri | Scenario | Ref | 关联 AC |
|---|---|---|---|---|---|---|
| 2.2-BLIND-BOUNDARY-001 | BOUNDARY | U | P1 | review_history entry **缺** watch_pool 字段（legacy snapshot 早于该字段引入）→ /api/review 返回 watch_pool=[]（不抛错）| BOUNDARY-001 | AC1 ErrorHandling.legacy snapshot |
| 2.2-BLIND-BOUNDARY-002 | BOUNDARY | I | P2 | review_history.json = `[]` 空数组 → 跳过 history 分支，落到 latest_review.json 兜底；若 latest 也不存在 → 返回 `{}` | BOUNDARY-001 | AC1 fallback chain |
| 2.2-BLIND-BOUNDARY-003 | BOUNDARY | I | P2 | review_data['watch_pool'] = `[]` 空列表 → /api/review 返回 watch_pool=[]（review.html v-else 渲染"无"占位）| BOUNDARY-001 | AC1 ErrorHandling.empty pool |
| 2.2-BLIND-BOUNDARY-004 | BOUNDARY | U | P2 | now == 15:00:00.000（cutoff 边界）→ `n < cutoff` 为 False → 走 latest_review.json 路径（不走 history 路径）| BOUNDARY-001 | AC1 时间门控边界 |
| 2.2-BLIND-ERROR-001 | ERROR | I | P1 | review_history.json 内容为非法 JSON `"{not json"` → 异常被 line 318-319 静默吞掉 → 落到 latest_review.json 兜底；不向客户端泄漏异常 | ERROR-003 | AC1 ErrorHandling 既有兜底 |
| 2.2-BLIND-ERROR-002 | ERROR | I | P1 | review_history.json + latest_review.json **都**不存在 → 返回 `JSONResponse({})`（line 328-329 既有路径） | ERROR-002 | AC1 ErrorHandling.both missing |
| 2.2-BLIND-ERROR-003 | ERROR | I | P1 | latest_ranking.json 不存在 → /api/review 仍能正常返回 review_data（含 watch_pool 来自 history）；**关键回归**：旧代码 line 337 `if ranking_file.exists():` 判断 + 异常静默；新代码彻底不读该文件 | ERROR-002 | AC1 删除后行为：不再依赖 ranking |
| 2.2-BLIND-FLOW-001 | FLOW | I | P1 | 写 latest_review.json（fixture B）→ GET /api/review（now=15:50）→ 覆盖写入新 latest_review.json（fixture B'，不同 watch_pool）→ 立即第二次 GET → 第二次响应反映 B'（无缓存）| FLOW-002 | AC3 UI Interaction：手动 *refresh-review 后立即可见 |
| 2.2-BLIND-DATA-001 | DATA | I | P1 | review_history.json 中昨日 entry 的 watch_pool[0].concepts = `['某行业方向', '半导体']`（含元标签）→ GET /api/review → 响应 wp[0].concepts 已过滤；**且**重新读 review_history.json 时，磁盘上的 entry **未被** mutate（line 331 `review_data = dict(review_data)` 隔离） | DATA-002 | AC1 BR-1.4 + 防快照源数据污染 |

---

## Risk Coverage

> Story 未生成独立 risk-profile，本节直接列 Architect Review 中识别的 3 项 Low 风险与对应测试 mitigation。

| Risk | Severity | Mitigated by |
|---|---|---|
| L-1: docstring 与代码语义矛盾（误导后续 reader）| Low | 2.2-UNIT-003（反射 `__doc__` 断言） |
| L-2: 未删除的 dead import（ruff F401 长期告警 + 隐藏调用方迹象）| Low | 2.2-UNIT-002（源码 grep 断言） |
| L-3: 测试基线数字硬编码漂移（重名 fixture 让 case 数变少而 PASS 仍绿）| Low | 2.2-UNIT-013（pytest --collect-only 总数动态校验） |
| 隐性风险：review_history 中昨日 entry 字段不全（17 字段中部分缺失）| Low | 2.2-INT-003（真实 history 末尾 entry 13 keys 校验） |
| 隐性风险：line 331 `review_data = dict(...)` 浅拷贝是否够（watch_pool 是嵌套 list[dict]，concepts 清洗会就地改 dict）| Medium | 2.2-BLIND-DATA-001（验证磁盘端不被 mutate） |

> **注**：BLIND-DATA-001 标记为 Medium 隐性风险 — 若 line 331 确实只做浅拷贝，`_strip_meta_concepts_inplace` 仍会通过共享引用 mutate 历史 entry 中的 watch_pool[i].concepts 对象。本测试在写入磁盘后 reload 文件做对比，能捕获该污染（如发现污染应升级为 Blocking 由 Dev 改为深拷贝）。

---

## Execution Order

1. **P0 Unit**（2.2-UNIT-001）— 最快反馈：核心删除是否生效
2. **P0 Integration**（2.2-INT-001/002/004/005, 2.2-INT-007/008/009/010）— 端到端主验收 + 时序覆盖
3. **P0 Cross-Story**（2.2-INT-005/006）— 9:27 邮件 + 决策追踪不受影响
4. **P1**（包含全部 BLIND 中 P1 项）— 契约冻结 + 边缘场景兜底
5. **P2**（含 dedup / 集合容量校验 / 边界 cutoff / pytest collect 总数）

---

## Coverage Validation

### Standard Coverage ✅
- [x] 每个 AC 至少 1 个测试（AC1: 6, AC2: 4, AC3: 2, AC4: 3, AC5: 4）
- [x] 无重复覆盖（unit 层验证逻辑 / integration 层验证 route 装配；T5 时序参数化避免重复编写）
- [x] 关键路径多层覆盖（删除主路径有 unit + integration + 时序三层验证）
- [x] 风险有对应 mitigation（L-1/L-2/L-3 全部 cover）

### Blind Spot Coverage ✅
- [x] 每个外部依赖（review_history.json / latest_review.json / latest_ranking.json）有 ERROR 场景
- [x] 输入字段（watch_pool 列表 / cutoff 时间）有 BOUNDARY 场景
- [x] 多步流程（前端 fetch → 手动 refresh → 再次 fetch）有 FLOW 场景
- [x] 跨源数据一致性（history vs latest_review）有 DATA 场景
- [x] CONCURRENCY / RESOURCE 显式不适用（仅删除一处同步读取，无并发或资源新引入）

---

## Notes for Dev (实施提示)

1. **Test 文件路径**：`tests/test_review_watch_pool_snapshot.py`（与 `tests/notify/` 平级；本 Story 测的是 review API，不归类到 notify）。
2. **TestClient**：用 `from fastapi.testclient import TestClient` + `from src.api.app import app`；fixtures 在 `conftest.py` 中或本文件顶部。
3. **Hash baseline 文件**：新建 `tests/fixtures/watch_pool_snapshot_baselines.json`，结构：
   ```json
   {
     "review.html": "<sha256-hex>",
     "main.py": "<sha256-hex>",
     "scheduler.py": "<sha256-hex>",
     "daily_review.py": "<sha256-hex>",
     "api_review_top_keys": ["date", "limit_up_count", "..."]
   }
   ```
   Dev 在 T1 commit 时刻一次性写入；后续 CI 直接比对。
4. **patch 范围**：`monkeypatch.setattr("src.config.now_cn", lambda: datetime(2026, 5, 8, 9, 30, tzinfo=CN_TZ))`，**不要** patch `src.api.app.now_cn`（function-local import 无法拦截）。
5. **DATA_DIR 隔离**：`monkeypatch.setattr("src.api.app.DATA_DIR", tmp_path)` + 显式写入 `tmp_path / "review_history.json"` 等。**不要**修改项目内真实 `data/` 目录。
6. **现有测试基线 = 99**（pytest --collect-only 实测；本 Story 完成后预期 99 + 32 = 131 用例）。

---

## References

- Story: [docs/stories/watch-pool-snapshot-2.2-review-show-yesterday-frozen.md](../../stories/watch-pool-snapshot-2.2-review-show-yesterday-frozen.md)
- Scope (虚拟 epic 真源): [docs/prd/iteration-2-scope.md#story-2-2](../../prd/iteration-2-scope.md)
- Sibling baseline (regression fixture 风格): [tests/notify/test_decision_consistency.py](../../../tests/notify/test_decision_consistency.py)
- Architect Review: 9.5/10, 0 critical / 0 major / 3 minor (合并入 risk coverage)
- Test file (skeleton 由本 *test-design 同步生成): tests/test_review_watch_pool_snapshot.py

P0 count: 9

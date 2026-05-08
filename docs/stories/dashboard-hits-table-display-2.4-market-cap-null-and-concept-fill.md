# Story dashboard-hits-table-display-2.4: 选股表显示修复（市值 NaN 安全化 + 板块概念服务端注入）

## Story

```yaml
Story:
  id: dashboard-hits-table-display-2.4
  title: 看板今日选股表显示修复（市值空值不再渲染"亿" + 板块以"概念A/概念B (行业)"格式）
  epic: iteration-2 brownfield (virtual epic — 真源为 docs/prd/iteration-2-scope.md)
  status: Review
  mode: plan
  repository: monolith
  priority: P1
  estimated_complexity: standard
  test_design_level: standard
  story_type: brownfield-enhancement
```

**As a** A 股短线交易者（604491810@qq.com 邮件唯一收件人 + 浏览器看板唯一使用者），
**I want** 看板"今日选股表"在
（a）`market_cap` 字段缺失或为 NaN 时显示 "—" 而不是孤零零的 "亿"，
（b）板块列稳定显示 `概念A/概念B (行业)` 格式（与"今日异常未匹配额表"行 710-712 一致），
**so that** 我每天看选股表时不再看到"市值=亿"这种残缺单位、也不再看到大多数行只显示纯行业名而看不到主导热概念，避免因显示残缺而怀疑数据可信度（用户 2026-05-08 实盘反馈）。

---

## 背景与问题

### Sub-issue (a) — 市值列显示残缺

**模板**（`src/static/index.html:638`）：
```html
<td>{{ hit.market_cap }}亿</td>
```

**数据**：`data/latest_screener.json` 实测含
```json
{ "code": "600130", "market_cap": null, "auction_turnover": null, "volume_ratio": null, ... }
```

`market_cap=null` 来自 `src/engine/screener.py:206` `market_cap=round(market_cap_yi, 2)`：当批量行情接口（新浪 / 腾讯）返回 `market_cap` 缺失时 `row.get("market_cap", 0)=NaN` → 第 155 行 `market_cap_yi = ... else float(row.get("market_cap", 0))` 仍为 NaN → `round(NaN, 2)=NaN` → 落盘前在 dataclass→dict 序列化链路某处被 `json.dumps`（FastAPI JSONResponse / orjson）转 `null`。

**用户实盘**：选股表市值列出现 "亿"（孤零零的单位）— 视觉上类似"NaN/null 没显示出来"。

### Sub-issue (b) — 板块列只显示行业、缺概念

**模板**（`src/static/index.html:639-646`）：
```html
<template v-if="topConceptsOf(hit.code).length">
    <span style="color:#ef4444;font-weight:600;">{{ topConceptsOf(hit.code).join('/') }}</span>
    <span style="color:#6b7280;font-size:11px;margin-left:3px;">({{ hitLive(hit.code).industry || industryOf(hit.code) || '-' }})</span>
</template>
<template v-else>
    <span style="color:#a0aec0;">{{ hitLive(hit.code).industry || industryOf(hit.code) || '-' }}</span>
</template>
```

模板已经支持 "概念A/概念B (行业)" 与"仅行业"两种渲染分支 — 实际**模板逻辑无问题**。问题在数据源：

`topConceptsOf(code)` 实现在 `src/static/index.html:1441-1443`：
```js
function topConceptsOf(code) {
    const r = (ranking.value || []).find(x => String(x.code) === String(code))
    return r?.top_concepts || []
}
```

**仅查 `ranking.value`**（来自 `/api/ranking` → `latest_ranking.json`，TOP30 涨幅榜）。但 `screener_hits` 走的是连板 + 竞价 + 流通市值 + 量比筛选，命中股的 `code` **绝大多数不在 TOP30 涨幅榜内** → `topConceptsOf` 返回 `[]` → 模板走 v-else → 仅显示行业。

### 对比：异常未匹配额表为何能显示"概念A/概念B (行业)"

`src/static/index.html:710-712`（"🚀 今日一字涨停"明细表）使用：
```html
<template v-if="(s.top_concepts || []).length">
    <span ...>{{ (s.top_concepts || []).join('/') }}</span>
    <span ...>({{ s.industry || '—' }})</span>
</template>
```

**直接读 `s.top_concepts` / `s.industry`**（行内字段），数据源 `market.limit_up_flat_list` 的每条 `s` 已由服务端注入这两个字段。screener_hits 没有同等的服务端注入 → 客户端 fallback 失败。

### 邮件路径已实现的 fallback（参考实现）

`src/notify/email_sender.py:565-595` 在渲染选股 HTML 表时实现了完整的 concept 注入链路：
1. 优先读 `ranking.top_concepts`（同今日 dashboard）
2. 缺失时走 `concept_cache.json` + `limit_up_cache.json` 聚合：
   ```python
   from src.data.concept_fetcher import load_stock_to_concepts
   from src.engine.concept_stats import aggregate_concept_limit_ups, top_concepts_for_stock
   c_map = load_stock_to_concepts() or {}
   heats = aggregate_concept_limit_ups(lu.get(latest, []) or [], c_map)
   top_concepts_map[code] = top_concepts_for_stock(list(c_map.get(code) or []), heats, top_n=2)
   ```
3. industry 走 `industry_cache.json` + ranking 兜底

**邮件已经"概念A/概念B (行业)"格式正常显示** — 看板没有同等的服务端注入 / 客户端兜底。

### 用户反馈（2026-05-08）

- 看板选股表"市值"列大量行显示 "亿"（无数字前缀）
- "板块"列大多数行只显示行业名，与邮件"概念/行业"格式不一致

### 真源约束（用户已选定）

> "(a) `screener.py` 包 `_safe_round(v)` 处理 NaN → None；模板加 v-if 隐藏 '—'"
> "(b) 排查 concept 注入链路：`topConceptsOf` 实现在哪、hit.code 是否进入 concept 缓存查询"

[Source: docs/prd/iteration-2-scope.md#story-2-4]

---

## 改动范围（来自 scope）

[Source: docs/prd/iteration-2-scope.md#story-2-4]

1. **`screener.py`**：新增 `_safe_round(v, ndigits=2)` helper：`v` 为 NaN / None / `<= 0` → 返回 `None`；否则返回 `round(v, ndigits)`。`market_cap=round(market_cap_yi, 2)` → `market_cap=_safe_round(market_cap_yi)`。`ScreenerHit.market_cap` 类型 → `float | None`。
2. **`src/static/index.html` 选股表模板（行 638）**：`<td>{{ hit.market_cap }}亿</td>` → `<td><template v-if="hit.market_cap != null">{{ hit.market_cap }}亿</template><template v-else>—</template></td>`。
3. **服务端为 `screener_hits` 注入 `top_concepts` + `industry`**：在 `scheduler.py` 写 `latest_screener.json` 之前（line 532-537），调用新的 `enrich_screener_hits_with_concepts(hits_data, ranking_data)`，复用 `email_sender.py:565-595` 的 concept 解析路径（ranking 优先 + concept_cache + limit_up_cache 聚合 fallback；industry 优先 + industry_cache + ranking 兜底）。
4. **`src/static/index.html`**：`topConceptsOf(code)` / `industryOf(code)` 优先读 `hit.top_concepts` / `hit.industry`（新字段），ranking 兜底保留以兼容历史数据 / 重启窗口。

> ⚠️ 与 Story 2.3 的关系：Story 2.3（接力情绪 sub 4 字段）后续会改 `compute_yesterday_main_board_auction` 增字段，**与本 Story 完全无依赖**：本 Story 改 `screener.py` 输出 + `latest_screener.json` schema + dashboard 选股表模板；Story 2.3 改 `leader_feedback.py` + 邮件第 4 格 sub 行渲染。两路代码不重叠。

> ⚠️ 与 Story 2.5 的关系：Story 2.5（refresh-screener `skip_email`）改 `run_screener_update` 签名，**与本 Story 也无冲突**：本 Story 不动 `run_screener_update` 签名，仅在已有的 latest_screener.json 写入步骤之前插入 enrich 调用。

> ⚠️ 与 email-sync-1.1 / decision-consistency-2.1 的关系：本 Story **复用** 而非搬运 `email_sender.py:565-595` 的 concept 解析逻辑 — 由 Architect 在 \*review 阶段决定具体落点（见 T0）。

---

## Acceptance Criteria

### AC1: `screener.py` 输出 `market_cap` 为 `None`（不再产生 `NaN`）

**Scenario**
```gherkin
GIVEN run_screener(realtime_df, limit_up_history, cycle_codes) 在以下 3 类输入下被调用
  AND 候选股已通过竞价涨幅 / 连板 / 竞价金额过滤
WHEN 计算 market_cap_yi（src/engine/screener.py:155）
THEN market_cap 字段输出值必须满足：
  - 输入 row["market_cap"]=2.5e9（25 亿元）→ market_cap=25.0
  - 输入 row["market_cap"]=NaN（float('nan')）→ market_cap=None
  - 输入 row["market_cap"]=0（缺失） → market_cap=None
  - 输入 row["market_cap"]=-1（异常负值） → market_cap=None
  - 输入 row 不含 "market_cap" key → market_cap=None
  - 序列化（asdict + json.dumps）后 JSON 中字段为 null（不为 NaN 字面量）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-1.1 | 新增 helper `_safe_round(v, ndigits=2) -> float \| None`（位置：`src/engine/screener.py` 模块级，紧邻 `_get_avg_volume_5d` 或 `_detect_continuous_limit_up`，由 Architect 决定）：v 为 None / NaN / `v <= 0` → 返回 None；否则返回 `round(v, ndigits)`。NaN 判定使用 `math.isnan(v)`（仅对 float / int 类型生效；其他类型走 None 分支）。 |
| BR-1.2 | `market_cap=round(market_cap_yi, 2)`（line 206）→ `market_cap=_safe_round(market_cap_yi)`。**不**改其他 round 调用（auction_gain / open_price / auction_turnover_calc / auction_amount / auction_volume_lots / auction_volume_ratio / volume_ratio / gain_10d 这 8 处）— 这些字段用户未反馈问题，且 0/NaN 输入已有上游 if 守护（`if pre_close <= 0 or open_price <= 0: continue`）。**Story 范围最小化**。 |
| BR-1.3 | `ScreenerHit.market_cap` 类型由 `float` → `float \| None`。dataclass annotation 必须更新（dataclass 派生 `asdict` 行为对 Optional 字段透明，无需特殊处理）。 |
| BR-1.4 | "流通市值过滤"逻辑（line 156-158）行为字符级不变：`if market_cap_yi > 0:` 守护使得 NaN / 0 / 负值的股票**不**触发上限/下限过滤（与现状一致 — 软过滤），仍可命中；只是写入文件时 market_cap 字段为 null。 |
| BR-1.5 | latest_screener.json 文件**不**纳入 git（.gitignore 已覆盖 `data/`，本 Story 不动 .gitignore）。 |

**Data Validation**
| Field | Type | Required | Rules | Error Message |
|---|---|---|---|---|
| ScreenerHit.market_cap | float \| None | ✅ | None 或 round(v, 2) 后非负数 | — |
| _safe_round(v) | float \| None | ✅ | None / NaN / `<=0` → None | — |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| `_safe_round` 输入非数值类型（str / dict 等） | — | （静默） | 走 None 分支（try/except 捕获 TypeError + 返回 None；保护 brownfield 不可达分支） |
| `math.isnan` 抛 TypeError（非 float 输入） | — | （静默） | 走 None 分支 |

---

### AC2: 选股表模板"市值"列空值显示 "—"

**Scenario**
```gherkin
GIVEN 用户访问 dashboard
  AND latest_screener.json 已加载（含若干 hit，部分 hit.market_cap=null）
WHEN 选股表（screener-table）渲染每行第 6 列（市值列）
THEN
  - hit.market_cap != null → 渲染 "{value}亿"（如 "25.32亿"）
  - hit.market_cap == null → 渲染 "—"（中文 em-dash，与异常未匹配额表 v-else 分支文案一致）
  - 不再出现 "亿"（孤零零的单位字符串） / "null亿" / "NaN亿"
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-2.1 | 模板第 638 行 `<td>{{ hit.market_cap }}亿</td>` → `<td><template v-if="hit.market_cap != null">{{ hit.market_cap }}亿</template><template v-else>—</template></td>` |
| BR-2.2 | 不改其他 9 列（代码 / 名称 / 价格 / 涨幅 / 竞价涨幅 / 板块 / 连板 / 10日涨幅 / 周期股 / 决策）的渲染逻辑；**仅市值列单点改动**。 |
| BR-2.3 | 不改 `<th>市值</th>`（第 618 行表头）；不改 CSS / 颜色。 |
| BR-2.4 | 中文 em-dash 字符为 `—`（U+2014，与异常未匹配额表 line 645 `'-'` 区分）— 选 `—` 与"今日异常未匹配额表" `s.industry || '—'`（line 712）保持一致。 |

**UI Interaction**
| Trigger | Behavior |
|---|---|
| 9:27 后 hit.market_cap=25.32 | 渲染 "25.32亿"（绿色无样式，与现状一致） |
| 9:27 后 hit.market_cap=null（数据源缺失）| 渲染 "—"（与现状被截断的"亿"对照） |
| hit.market_cap=0 / NaN（不应出现，因 AC1 已收口）| 兜底渲染 "—"（v-if `!= null` 对 0 仍渲染 "0亿" — 但 AC1 BR-1.1 已确保 `<=0` 输出 None） |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| hit.market_cap=undefined（旧数据 / 字段不存在）| — | — | `!= null` 在 JS 中对 undefined 也为 false（`undefined != null` 为 false），渲染 "—"（与 null 等价） |

---

### AC3: 服务端为 `screener_hits` 注入 `top_concepts` 与 `industry`

**Scenario**
```gherkin
GIVEN run_screener_update() 在 9:27 cron 或 refresh-screener 触发下执行
  AND src/scheduler.py 已计算出 hits（line 481）+ 已富化 tencent (line 487-489)
  AND ranking_data 在 scheduler 中已就绪（含 top_concepts / industry — 来自 enrich_ranking_with_top_concepts）
  AND data/concept_cache.json + data/limit_up_cache.json + data/industry_cache.json 至少其一可读
WHEN scheduler 准备写 data/latest_screener.json（当前 line 532-537）
THEN
  - 在写入之前必须调用新增 helper `enrich_screener_hits_with_concepts(hits_data, ranking_data)`
  - 该 helper 为每条 hit 注入两个新字段：
    - `top_concepts: list[str]`（0 / 1 / 2 项；按全市场涨停热度排序，过滤元概念）
    - `industry: str | None`（行业名；缺失时 None）
  - 解析优先级（top_concepts）：(1) ranking.top_concepts → (2) concept_cache + limit_up 聚合 fallback → (3) []
  - 解析优先级（industry）：(1) ranking.industry → (2) industry_cache.json → (3) None
  - hits_data 写入 latest_screener.json 时每条 hit 含新字段（同时既有 13 个字段不变）
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-3.1 | 新增 helper `enrich_screener_hits_with_concepts(hits_data: dict, ranking_data: dict \| None) -> None`（in-place 修改 `hits_data["hits"]` 每条字典）。具体落点（独立模块 / scheduler 内联 / email_sender 模块共享）由 Architect T0 决定。 |
| BR-3.2 | top_concepts 解析逻辑必须**字符级等同**于 `email_sender.py:565-595` 的 fallback 链：(1) `ranking_data["ranking"]` 中 code 匹配 `top_concepts` 字段；(2) 从 `concept_cache.json` 经 `load_stock_to_concepts()` 取 stock→concepts；(3) 从 `limit_up_cache.json` 最近一日聚合涨停热度（`aggregate_concept_limit_ups`）；(4) 用 `top_concepts_for_stock(list(c_map.get(code) or []), heats, top_n=2)` 选 1-2 个。 |
| BR-3.3 | industry 解析逻辑：(1) `ranking_data["ranking"]` 中 code 匹配 `industry` 字段；(2) `industry_cache.json` 字典查表；(3) None。 |
| BR-3.4 | top_concepts 必须经过 `concept_blacklist.filter_concepts` 过滤元概念（与 `app.py:_strip_meta_concepts_inplace` 一致），避免"沪股通 / 融资融券 / 创业板"这类元标签泄漏。 |
| BR-3.5 | 任一缓存文件读取失败（不存在 / JSON 损坏 / 反序列化抛错）→ try/except 静默 + 该步骤跳过；helper 永远不抛错给 caller；最终 hits 字段至少为 `top_concepts=[]` + `industry=None`（保护 hits 始终可写盘）。 |
| BR-3.6 | `ScreenerHit` dataclass **不**新增 `top_concepts` / `industry` 字段 — 这两个字段是**前端展示用**，不参与 hits 命中算法；保留为"运行时 dict 扩展字段"模式（与 ranking 路径一致：`ranking_records` 是 list[dict]，由 `enrich_ranking_with_top_concepts` in-place mutate）。 |
| BR-3.7 | helper 调用必须放在 `(DATA_DIR / "latest_screener.json").write_text(...)`（line 536-538）**之前**；写入步骤之后已有的 `archive_today_hits` / `cycle_snapshot` 等下游路径**不**消费这两个新字段，无需同步改动。 |

**Data Validation**
| Field | Type | Required | Rules | Error Message |
|---|---|---|---|---|
| hit["top_concepts"] | list[str] | ✅ | 0-2 项；每项为非空 str；过元概念黑名单 | — |
| hit["industry"] | str \| None | ✅ | 非空 str 或 None | — |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| concept_cache.json 不存在 | — | （静默） | 全部 hit `top_concepts=[]` |
| limit_up_cache.json 不存在 | — | （静默） | 走 ranking 优先链；ranking 缺则 `top_concepts=[]` |
| industry_cache.json 不存在 | — | （静默） | 走 ranking 优先链；ranking 缺则 `industry=None` |
| ranking_data=None | — | （静默） | 直接走 cache 兜底链 |
| filter_concepts 抛错 | — | print "[选股富化] 概念过滤失败: {e}" | 该 hit `top_concepts=[]` |

---

### AC4: dashboard 选股表 helper 优先读 `hit` 自身字段，ranking 兜底保留

**Scenario**
```gherkin
GIVEN 用户访问 dashboard 加载 latest_screener.json
  AND 部分 hit 含新字段（top_concepts / industry — 由 AC3 注入）
  AND 部分 hit 仍是历史数据（无新字段 — 重启过渡窗口 / 旧文件）
WHEN 选股表渲染每行的板块列（行 639-646）
THEN
  - topConceptsOf(code) 优先读 `hit.top_concepts`（精确匹配 hit.code），缺失时 fallback 到 ranking 查表（既有逻辑）
  - industryOf(code) 同样优先读 `hit.industry`，缺失时 fallback 到 ranking
  - 模板 v-if/v-else 分支不变；视觉行为：含 top_concepts 显示"概念A/概念B (行业)"；不含则显示纯行业；两者都缺则显示 "-"
```

**Business Rules**
| ID | Rule |
|----|------|
| BR-4.1 | `topConceptsOf(code)` (line 1441-1443) 改写为：先在 `screenerHits.value`（或 `hits.value`）中按 code 找 hit，若 `hit?.top_concepts?.length > 0` 返回 `hit.top_concepts`；否则 fallback 到 ranking 查表（既有 `r?.top_concepts || []`）。 |
| BR-4.2 | `industryOf(code)` (line 1436-1438) 同模式：先 hit.industry，缺则 ranking.industry，缺则 `'-'`。 |
| BR-4.3 | 模板第 639-646 行 HTML 结构**字符级保持不变**（仅 helper 内部逻辑变） — 与 email-sync-1.1 / decision-consistency-2.1 "不改模板 HTML" 一脉相承。 |
| BR-4.4 | `hitLive(hit.code).industry` 兜底（line 642 / 645）保留 — `hitLive` 来源于 `/api/snapshot` 实时行情，industry 字段独立于本 Story 注入路径，**不动**。 |
| BR-4.5 | dashboard 不新增 fetch / API / state ref；仅修改两个 helper 函数体。 |
| BR-4.6 | `screenerHits` ref 的来源（既有 `loadData` 中 `/api/screener` fetch）保持不变；仅消费方变化。 |

**UI Interaction**
| Trigger | Behavior |
|---|---|
| AC3 注入完成后访问 dashboard | 大部分 hit 显示 "概念A/概念B (行业)"（红色概念 + 灰色行业小字）|
| 重启 / 缓存清空时段（top_concepts=[]） | 显示纯行业（v-else 分支）— 与现状视觉等价 |
| ranking 中也无 industry 时 | 显示 "-"（与现状一致） |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| hit.top_concepts 不是 array（JSON 损坏 / 字段类型异常） | — | — | `?.length > 0` 短路返回 false → fallback 到 ranking |
| screenerHits.value 为 null/undefined | — | — | 先 `(screenerHits.value || []).find(...)` 守护 |

---

### AC5: 不引入回归（DoD）

**Scenario**
```gherkin
GIVEN 现有选股 + 看板 + 邮件 + 复盘 + 排行 + 异常未匹配额表 6 类链路的边缘分支
WHEN Story 2.4 改造后的代码在以下输入下被调用
THEN 行为应与改造前完全一致：
  - hits=[] 空选股 → latest_screener.json 仍正常写入 {date, hits:[]} + dashboard 显示"暂无结果"
  - hit.market_cap=25.32（有效值）→ 模板渲染 "25.32亿"（与现状字符级一致）
  - hit.code 在 ranking TOP30 内 → topConceptsOf 仍返回 ranking 注入的 top_concepts（hit-first fallback 不破坏既有路径）
  - 邮件 send_screener_report 行为完全不变（公开签名 + concept/industry fallback 链 + render 输出）
  - cross_validator 异常未匹配额表（s.top_concepts 路径）行为完全不变
  - 复盘页 watch_pool / review API（Story 2.2 范围）字段不变
  - latest_ranking.json 字段契约不变（top_concepts / industry 早已就位）
  - latest_advice.json（Story 2.1 引入）字段不变
  - 模板第 638 行以外的 选股表 9 列（代码 / 名称 / 价格 / 涨幅 / 竞价涨幅 / 板块 / 连板 / 10日涨幅 / 周期股 / 决策）字符级不变
```

**Business Rules**
| ID | Rule |
|---|---|
| BR-5.1 | 不重命名 `run_screener` / `run_screener_update` / `enrich_screener_hits` 等公开签名 |
| BR-5.2 | 不重构 `ScreenerHit` dataclass（仅 market_cap 类型由 `float` → `float \| None`，字段名不变 / 字段顺序不变） |
| BR-5.3 | 不引入新依赖（不加 pandas-extended / pydantic / 新 npm 包等）；helper 仅 import 既有 `math` / `concept_stats` / `concept_blacklist` / `concept_fetcher` |
| BR-5.4 | dashboard 不改 HTML 结构 / 不改 CSS（仅改 第 638 行单元格内部 v-if 与 第 1436-1443 行两个 helper） |
| BR-5.5 | latest_screener.json 字段契约：13 个既有字段 + 2 个新字段（top_concepts / industry）；下游消费方（archive_today_hits / cycle_snapshot / email_sender / 邮件 fallback）继续使用 13 个字段，**不**消费 2 个新字段（保护下游零变更） |
| BR-5.6 | concept_cache.json / limit_up_cache.json / industry_cache.json 文件契约不变（只读消费方） |
| BR-5.7 | enrich_screener_hits_with_concepts helper 必须**幂等**：同一组 (hits_data, ranking_data) 输入两次调用，hits 字段输出字符级相等 |

**Error Handling**
| Scenario | Code | Message | Action |
|---|---|---|---|
| 任一边缘分支行为差异 | — | — | QA 标记为 BLOCKING，回退至 SM revise |

---

## Tasks / Subtasks

> **说明**：测试用例的具体 spec 由 QA 在 \*test-design 阶段产出（test_design_level: standard），Dev 在编码后回填本节"测试"子任务。

### Infrastructure Tasks (Shared)

- [x] **T0: helper 落点决策（Architect 决定）** `[AC3]`
  - [x] Architect 决定 — **选项 A** — 新建 `src/engine/screener_concept_enrich.py`
  - [x] 决策结果写入 Dev Log（`docs/dev/logs/dashboard-hits-table-display-2.4-dev-log.md`）

### Feature Implementation Tasks

- [x] **T1: AC1 — `_safe_round` helper 与 `market_cap` 字段** `[AC1]`
  - [x] `src/engine/screener.py:21-43` 模块级新增 `_safe_round(v, ndigits=2) -> Optional[float]`
  - [x] `ScreenerHit.market_cap` 类型 `float` → `Optional[float]` (line 56)
  - [x] line 219 调用 `_safe_round(market_cap_yi)` 替换原 `round(market_cap_yi, 2)`
  - [x] 验证 `json.dumps(asdict(hit_with_None))` 含 `'"market_cap": null'`（UNIT-012）

- [x] **T2: AC3 — 服务端 concept/industry 注入** `[AC3]`
  - [x] 实现 `src/engine/screener_concept_enrich.py` (新文件 161 行) — 4 个 cache loader + 主 enrich 函数
  - [x] `src/scheduler.py:537-552` 插入 ranking_data 加载 + helper 调用（在 `latest_screener.json.write_text` 之前）
  - [x] 复用 `aggregate_concept_limit_ups` + `top_concepts_for_stock` + `load_stock_to_concepts` + `filter_concepts`
  - [x] try/except 兜底每一处文件读 / 字典查表 — helper 永不抛错给 caller（BR-3.5）

- [x] **T3: AC2 + AC4 — dashboard 模板与 helper 改造** `[AC2, AC4]`
  - [x] `src/static/index.html:675` 市值列加 v-if/v-else `<template>` 分支 + `—`(U+2014) 兜底
  - [x] `src/static/index.html:1543-1548` 重写 `topConceptsOf` — hit-first + ranking fallback
  - [x] `src/static/index.html:1512-1517` 重写 `industryOf` — hit-first + ranking fallback + `'-'` 默认
  - [x] 冻结 `tests/fixtures/screener_display_baselines.json` SHA256 baseline（4 个区域）

### Integration & Verification Tasks

- [x] **T4: 端到端集成测试** `[AC1, AC2, AC3, AC4]`
  - [x] INT-001 scheduler 端到端：latest_screener.json 含 top_concepts + industry
  - [x] INT-002 静态：scheduler.py 含 enrich 调用且行号 < write_text 行号
  - [x] INT-003 静态：scheduler.py 含 ranking_data 加载 + try/except 兜底（BLIND-ERROR-006）
  - [x] dashboard 渲染：v-if/v-else + hit-first / ranking-fallback 视觉等价

- [x] **T5: 回归保护测试** `[AC5]`
  - [x] hits=[] → latest_screener.json `{date, hits:[]}` 结构不变（UNIT-041）
  - [x] tests/notify/* 99 测试全绿（46+48+5；UNIT-042 subprocess pytest 验证）
  - [x] `inspect.signature(send_screener_report)` 与 email-sync-1.1 baseline 一致（UNIT-043）
  - [x] `inspect.signature(run_screener / run_screener_update)` 不变（UNIT-044）
  - [x] hits[0] 13 + 2 = 15 字段契约（UNIT-045 BLIND-DATA-001）
  - [x] 异常未匹配额表 `s.top_concepts` 路径在 index.html line 711-712 不变（UNIT-047 — 修正 QA 测试设计中的 src/api/cross_validator.py 路径错误）

- [x] **T6: 边缘场景测试** `[AC1, AC3]`
  - [x] BOUNDARY: 0 / -1 / NaN / inf / None / str / dict → `_safe_round` 全部 None（UNIT-002~008）
  - [x] BLIND-ERROR: concept_cache 缺/损 / limit_up_cache 损 / industry_cache 缺 / filter_concepts 抛错（BLIND-ERROR-001~005）
  - [x] BLIND-FLOW: ranking_data=None + 全 cache 缺 → top_concepts=[] industry=None（UNIT-026 BLIND-FLOW-002）
  - [x] BLIND-FLOW: 幂等性 — deepcopy + 二次调用字符级相等（UNIT-046 BLIND-FLOW-001）
  - [x] BLIND-FLOW: hits_data 顶层无 'hits' 键 → 不抛错（BLIND-FLOW-003）

- [x] **T7: 最终验收** `[ALL ACs]`
  - [x] tests/test_screener_display_2_4.py 56/56 passed
  - [x] 99/99 邮件 + 决策一致性 + fallback 回归全绿
  - [x] tests/test_review_watch_pool_snapshot.py 34/34（Story 2.2 sister 并行项目，rebaseline 后绿）
  - [x] `pytest tests/test_screener_display_2_4.py -W error` 严格模式全绿
  - [x] 总计 189 passed（56 + 99 + 34）
  - [x] Dev Log 完整记录 T0 决策 + 各 AC 实现位置 + Feedback to QA / SM
  - [x] Status: Approved → Review

### AC Coverage Matrix

| Task | AC1 | AC2 | AC3 | AC4 | AC5 |
|------|:---:|:---:|:---:|:---:|:---:|
| T0: helper 落点决策 |   |   | ✓ |   |   |
| T1: \_safe\_round + market_cap | ✓ |   |   |   |   |
| T2: 服务端 enrich helper |   |   | ✓ |   |   |
| T3: dashboard 模板 + helper |   | ✓ |   | ✓ |   |
| T4: 端到端集成 | ✓ | ✓ | ✓ | ✓ |   |
| T5: 回归保护 |   |   |   |   | ✓ |
| T6: 边缘场景 | ✓ |   | ✓ |   |   |
| T7: 最终验收 | ✓ | ✓ | ✓ | ✓ | ✓ |

### Test Specs (QA Backfill — P0 + P1≤5 per AC)

> 完整 56 用例（含 P2 + 全 BLIND-SPOT 详情）见 [test-design 文档](../qa/assessments/dashboard-hits-table-display-2.4-test-design-20260508.md)。Skeleton 文件 `tests/test_screener_display_2_4.py` 含每用例 `raise NotImplementedError`；Dev 必须替换为真实测试逻辑（保留全部用例，不可删；不适用者改 `@pytest.mark.skip(reason=...)`）。
>
> 命名约定：`UNIT-XXX` = 单元测试（pure function / static text / reflection / dict op）；`INT-XXX` = 集成测试（scheduler 端到端 + DATA_DIR mock）。

#### AC1 Test Specs（仅 P0；P1=8 > 5，详情见 doc）

| Scenario | Input | Expected | Level |
|----------|-------|----------|-------|
| 2.4-UNIT-001 | `_safe_round(2.345)` | `2.35` | unit |
| 2.4-UNIT-002 | `_safe_round(0)` | `None` (BR-1.1 `<=0` 边界) | unit |
| 2.4-UNIT-003 | `_safe_round(-1.0)` | `None` (BR-1.1 负值) | unit |
| 2.4-UNIT-004 | `_safe_round(float("nan"))` | `None`（用户实盘根因） | unit |
| 2.4-UNIT-005 | `_safe_round(None)` | `None` | unit |
| 2.4-UNIT-010 | run_screener mock `row.market_cap=NaN` | `ScreenerHit.market_cap == None` | unit |
| 2.4-UNIT-011 | run_screener mock `row.market_cap=2.5e9` | `ScreenerHit.market_cap == 25.0`（回归） | unit |
| 2.4-UNIT-012 | `json.dumps(asdict(hit_with_None))` | 含 `'"market_cap": null'`，不含 `NaN` | unit |

#### AC2 Test Specs（P0 + P1=3）

| Scenario | Input | Expected | Level |
|----------|-------|----------|-------|
| 2.4-UNIT-017 | 静态读 index.html line 638 | 含 `v-if="hit.market_cap != null"` | unit |
| 2.4-UNIT-018 | 静态读 index.html line 638 | v-if 分支保留 `亿` | unit |
| 2.4-UNIT-019 | 静态读 index.html line 638 | 含 `—` (U+2014)，不在 v-else 用 `-` (U+002D) | unit |
| 2.4-UNIT-020 | 静态读 index.html line 638 | 含 `<template v-else>`（双 template 标签） | unit |
| 2.4-UNIT-021 | SHA256(line 615-624 表头) | == baseline | unit |
| 2.4-UNIT-022 | SHA256(line 627-637 + 647-656 其他 9 列) | == baseline | unit |

#### AC3 Test Specs（仅 P0；P1=7 > 5，详情见 doc）

| Scenario | Input | Expected | Level |
|----------|-------|----------|-------|
| 2.4-UNIT-024 | hits=`[{code:"600519"}]`, ranking 命中 | hits[0].top_concepts==["白酒","食品饮料"]; industry=="白酒" | unit |
| 2.4-UNIT-025 | ranking 不含 hit.code, concept_cache + limit_up_cache 可读 | top_concepts 来自 cache 聚合 (top_n=2) | unit |
| 2.4-UNIT-026 | 全 cache 缺 + ranking=None | top_concepts==[] (空 list); industry==None | unit |
| 2.4-UNIT-027 | ranking 不含 hit.code; industry_cache 命中 | industry=="白酒" | unit |
| 2.4-UNIT-028 | c_map=["沪股通","白酒","融资融券"] | top_concepts 仅含 ["白酒"]（元概念过滤）| unit |
| 2.4-INT-001 | scheduler mock DATA_DIR + run_screener_update | latest_screener.json hits[i] 含 top_concepts + industry 键 | integration |

#### AC4 Test Specs（P0 + P1=2）

| Scenario | Input | Expected | Level |
|----------|-------|----------|-------|
| 2.4-UNIT-034 | 静态读 index.html line 1441-1443 | topConceptsOf 含 `screenerHits.value` 或 `hit.top_concepts` | unit |
| 2.4-UNIT-035 | 静态读 index.html line 1441-1443 | 仍含 `ranking.value` 或 `r?.top_concepts`（fallback 保留）| unit |
| 2.4-UNIT-036 | 静态读 index.html line 1436-1438 | industryOf 含 `hit.industry` | unit |
| 2.4-UNIT-037 | 静态读 index.html line 1436-1438 | 仍含 `'-'`（默认兜底）| unit |
| 2.4-UNIT-038 | SHA256(line 639-646 板块列模板) | == baseline | unit |
| 2.4-UNIT-039 | 静态读 line 642 / 645 | 仍含 `hitLive(hit.code).industry` | unit |

#### AC5 Test Specs（P0 + P1=4）

| Scenario | Input | Expected | Level |
|----------|-------|----------|-------|
| 2.4-UNIT-041 | run_screener 返回 `[]` | latest_screener.json `{date, hits:[]}` 结构不变 | unit |
| 2.4-UNIT-042 | pytest tests/notify/ -v | 99 测试全绿（46+48+5）| unit |
| 2.4-UNIT-043 | inspect.signature(send_screener_report) | == email-sync-1.1 baseline (commit eb4e883) | unit |
| 2.4-UNIT-044 | inspect.signature(run_screener / run_screener_update) | == baseline | unit |
| 2.4-UNIT-045 | latest_screener.json hits[0].keys() | 13 既有 + 2 新 = 15 keys (BR-5.5) | unit |
| 2.4-UNIT-046 | enrich(deep_copy(hits), ranking) × 2 次 | 输出 json.dumps 字符级相等（幂等 BR-5.7） | unit |

---

## Dev Notes

### Technical Constraints

| 类别 | 约束 | 来源 |
|---|---|---|
| 模板最小改动 | 选股表第 638 行**仅**改市值列单元格；其他 9 列与表头第 615-624 行字符级不变 | scope 文件 #74-77 |
| 概念渲染格式对齐 | 板块列必须最终显示"概念A/概念B (行业)"或纯行业 — 模板分支已就位（639-646），只需服务端 / 客户端 helper 提供数据 | scope 文件 #71-72；index.html:710-712 已实现的格式 |
| 服务端注入 vs 客户端注入 | 服务端注入（写 latest_screener.json 时 enrich）— 与 ranking 路径（scheduler.py:139-142 / :214-217 enrich_ranking_with_top_concepts）一致；不引入新 API endpoint | scope 文件 #75；ranking pattern |
| dataclass 类型扩展 | `ScreenerHit.market_cap: float \| None` — Python 3.10+ 语法（与 `dataclass` `from __future__ import annotations` 兼容），不影响既有 asdict | screener.py:33 |
| 不引入新 API | 复用既有 `/api/screener` endpoint；hits 字段扩展即可 | KISS + brownfield 最小变更 |
| 不引入新依赖 | 复用 `math.isnan` + 既有 `concept_stats` / `concept_blacklist` / `concept_fetcher` 模块 | scope 文件 #106 |
| 元概念过滤一致 | top_concepts 必须经 `concept_blacklist.filter_concepts`，与 `app.py:_strip_meta_concepts_inplace` 行为对齐 | app.py:387-388 |

### Accumulated Context (From Previous Stories)

| Resource | Source Story | 状态 | Action |
|---|---|---|---|
| `enrich_ranking_with_top_concepts(records, top_n)` | 既有（pre-iteration-2） | 已稳定，scheduler.py:139/214 调用 | REUSE — 本 Story 借鉴模式但不改其代码 |
| `email_sender.py:565-595` concept/industry fallback 链 | email-sync-1.1 (Done) | 邮件路径已正常显示"概念/行业" | REUSE — 本 Story 抽取等价逻辑给 dashboard 路径（具体抽取方式由 T0 决定）|
| `concept_cache.json` / `limit_up_cache.json` / `industry_cache.json` | 既有 | 字段已就绪 | REUSE — 仅作只读消费 |
| `concept_stats.top_concepts_for_stock` / `aggregate_concept_limit_ups` | 既有 | 已稳定 | REUSE — 本 Story 直接 import 调用 |
| `concept_blacklist.filter_concepts` / `is_meta_concept` | 既有 | 已稳定 | REUSE — 元概念过滤 |
| `concept_fetcher.load_stock_to_concepts` | 既有 | 已稳定 | REUSE — stock→concepts 字典加载 |
| `latest_screener.json` schema | 既有 | 13 字段 | EXTEND — 新增 top_concepts (list) + industry (str\|None) |
| `ScreenerHit` dataclass | 既有 | 13 字段 | EXTEND — `market_cap: float` → `market_cap: float \| None`；**不**新增 dataclass 字段（concept/industry 仅在 dict 层注入） |
| `latest_advice.json` (Story 2.1) | decision-consistency-2.1 (Done) | 字段稳定 | N/A — 本 Story 不消费 |
| `index.html:639-646` 模板分支 | 既有 | 已支持"概念/行业"+ "纯行业"双分支 | REUSE — 不改 HTML，仅修复数据源 |
| `cross_validator.py` deviation 表 (`s.top_concepts`) | 既有 | 已正常显示 | UNTOUCHED — 平行参考实现 |
| Database Tables | — | N/A — 本 Story 无数据库写入 | — |
| Shared Models | — | N/A — 复用既有 ScreenerHit + dict | — |

### Database Design

N/A — 不涉及数据库变更。

### Data Synchronization Requirements

- [x] 本 Story 扩展既有 JSON 文件 `data/latest_screener.json` 字段，与 `latest_ranking.json` 同生命周期（9:27 写一次，refresh-screener 触发重写）
- [x] 文件不入库（与既有 latest_*.json 一致）；.gitignore 已覆盖
- [x] 旧版 latest_screener.json（无新字段）作为重启过渡期数据自然兼容（dashboard helper hit-first 设计 + ranking fallback）

### Data Models

**`ScreenerHit` dataclass 变更**:
```python
# 既有：market_cap: float
# 新：market_cap: float | None        # _safe_round 输出（NaN/0/<=0 → None）
# 其他 12 字段不变
```

**`latest_screener.json` Schema 扩展**（每条 hit）:
```python
{
    # 既有 13 字段（不变）
    "code": str,
    "name": str,
    "continuous_limit_up": int,
    "open_price": float,
    "auction_gain": float,
    "auction_turnover": float | None,
    "auction_amount": float,
    "auction_volume_lots": float,
    "auction_volume_ratio": float,
    "market_cap": float | None,           # ← 类型扩展（既有 None 行为更明确）
    "volume_ratio": float | None,
    "gain_10d": float,
    "matched_cycle": bool,

    # 新增 2 字段（本 Story 引入）
    "top_concepts": list[str],            # 0-2 项；元概念已过滤
    "industry": str | None,
}
```

**`enrich_screener_hits_with_concepts` Signature**（本 Story 新建，落点由 T0 决定）:
```python
def enrich_screener_hits_with_concepts(
    hits_data: dict,                # {"date": str, "hits": list[dict]}
    ranking_data: dict | None,      # {"ranking": list[dict]} | None
) -> None:
    """In-place 注入 top_concepts + industry 到 hits_data['hits'] 每条 dict"""
```

### File Locations

| 文件 | 操作 | 涉及行号（起草时） | 关联 AC |
|---|---|---|---|
| `src/engine/screener.py` | **修改** | 模块级新增 `_safe_round` (建议紧邻 line 218 `_detect_continuous_limit_up` 之上)；`ScreenerHit.market_cap` 类型 (line 33)；line 206 调用 | AC1 |
| `src/engine/screener_concept_enrich.py` 或 `src/engine/screener.py` 或共享 helper | **新建/修改** | 由 T0 决定 | AC3 |
| `src/scheduler.py` | **修改** | line 532-537 之间插入 enrich 调用（在 latest_screener.json write_text 之前）| AC3 |
| `src/static/index.html` | **修改** | line 638（市值列 v-if）+ line 1436-1443（topConceptsOf / industryOf 改写）；模板第 615-624 表头 + 第 639-646 板块列分支字符级不动 | AC2, AC4 |
| `tests/engine/test_screener_safe_round.py` 或 `tests/engine/test_screener_display.py` | **新建** | — | AC1 |
| `tests/engine/test_screener_concept_enrich.py` 或 `tests/engine/test_screener_display.py` | **新建** | — | AC3, AC5 |
| `tests/static/test_index_html_screener_table.py` 或 `tests/integration/test_dashboard_screener_table.py` | **新建** | — | AC2, AC4, AC5 |
| `data/latest_screener.json` | **运行时变更** | — | AC3（运行时由 scheduler 写入新字段） |

### Deliverable Bindings

```yaml
deliverable_bindings:
  - deliverable: "src/engine/screener.py::_safe_round"
    consumer: "src/engine/screener.py::run_screener (line 206)"
    binding_type: import_usage
    verify: "src/engine/screener.py 含 'market_cap=_safe_round(market_cap_yi)' 模式"

  - deliverable: "enrich_screener_hits_with_concepts (T0 决定的模块路径)"
    consumer: "src/scheduler.py (latest_screener.json write 之前)"
    binding_type: import_usage
    verify: "src/scheduler.py 含 'enrich_screener_hits_with_concepts(' 调用 + 调用行号 < 'latest_screener.json'.write_text 行号"

  - deliverable: "data/latest_screener.json hits[].top_concepts + hits[].industry"
    consumer: "src/static/index.html topConceptsOf/industryOf"
    binding_type: schema_applied
    verify: "src/static/index.html 含 'screenerHits.value.find' 或 'hit.top_concepts' 或 'hit.industry' hit-first lookup 模式"

  - deliverable: "src/static/index.html 第 638 行 v-if hit.market_cap != null"
    consumer: "Vue runtime template render"
    binding_type: import_usage
    verify: "src/static/index.html 含 'v-if=\"hit.market_cap != null\"' 模式（或语义等价：'hit.market_cap !== null && hit.market_cap !== undefined'）"

  - deliverable: "tests/engine/test_screener_*.py + tests/static/test_index_html_*.py（具体路径由 QA test-design 决定）"
    consumer: "pytest discovery (project test runner)"
    binding_type: import_usage
    verify: "pytest 收集到 test_screener_safe_round / test_screener_concept_enrich / test_index_html_screener_table 中的测试用例（具体用例数与名称由 QA test-design 给出）"
```

### Testing Requirements

- **测试设计层级**：`standard`（与 Story 2.1 / 2.2 一致；scope 标注为 standard）
- **前置流程**：QA \*test-design 在开发前出测试设计文档，Dev 据此实现 T4/T5/T6 中的具体集成与边缘场景用例
- **覆盖重点**：
  1. `_safe_round` 输入空间：None / NaN / 0 / 负数 / 正常值 / inf（AC1）
  2. `ScreenerHit` asdict + json.dumps 对 `market_cap=None` 输出 `null`（AC1）
  3. dashboard 选股表 第 638 行渲染：null → "—"；正常 → "{val}亿"（AC2）
  4. enrich helper 三态：缓存全在 / 部分在 / 全缺；ranking 三态：在 / 不在 / None（AC3）
  5. enrich helper 边缘：concept_cache 损坏 / limit_up_cache 损坏 / industry_cache 缺失（AC3 BR-3.5）
  6. 元概念过滤覆盖 `concept_blacklist.is_meta_concept` 的元标签集合（AC3 BR-3.4）
  7. dashboard helper hit-first / ranking-fallback 三组合（AC4）
  8. 模板第 639-646 行 + 表头 + 其他 9 列字符级 baseline（AC5 BR-5.4）
  9. cross_validator deviation 表 s.top_concepts 路径不变（AC5）
  10. latest_screener.json 13 个既有字段类型契约不变（AC5 BR-5.5）
  11. enrich helper 幂等性（AC5 BR-5.7）

---

## Change Log

| Date | Agent | Status Transition | Details/Link |
|------|-------|-------------------|--------------|
| 2026-05-08 | SM (Phil) | Created → AwaitingArchReview | Brownfield 单 Story 起草；偏离标准流程 8 条沿用 email-sync-1.1 / decision-consistency-2.1 路径（无 PRD 分片 / 无 architecture 目录 / scope 文件作虚拟 epic / 跳过 Epic YAML / 跳过 架构上下文 / 跳过 累积校验 / 跳过 Decision 8A / 强制 test_design_level=standard）；scope 文件 [docs/prd/iteration-2-scope.md#story-2-4](../prd/iteration-2-scope.md) 作为真源；两 sub-issue 合并为 1 Story（市值 NaN + 板块概念）；上游依赖 Story 2.1 (Done @ eea5d4a) 提供 latest_advice.json — 本 Story 不消费 advice，独立。HANDOFF 至 architect \*review。 |
| 2026-05-08 | Architect (Wright) | AwaitingArchReview → AwaitingTestDesign | Score: 8.5/10, 0 critical / 1 major (AC3 ranking_data 来源未在 Story 显式说明，已在本 Review T0.2 给出代码片段)；T0 决策：选项 A（新建 `src/engine/screener_concept_enrich.py`）；2 medium / 3 low 改进项已记录。HANDOFF 至 qa \*test-design。 |
| 2026-05-08 | QA (Turing) | AwaitingTestDesign → TestDesignComplete → Approved | Test Design 完成（standard 层级，2-phase transition）。Test Design Doc: [docs/qa/assessments/dashboard-hits-table-display-2.4-test-design-20260508.md](../qa/assessments/dashboard-hits-table-display-2.4-test-design-20260508.md)；Test Skeleton: [tests/test_screener_display_2_4.py](../../tests/test_screener_display_2_4.py)（56 用例 / P0:23 P1:28 P2:5 / 18 BLIND-SPOT）。覆盖 AC1-AC5 全部 + Architect 1H/2M/3L 风险映射。回归基线锚点：99 邮件/决策测试 + send_screener_report/run_screener 签名 freeze。pytest --collect-only 验证 56 测试可被收集。HANDOFF 至 dev \*develop-story。 |
| 2026-05-08 | Dev (Linus) | Approved → InProgress → Review | TDD 完整实施 5 个 AC（T0-T7）。新建 `src/engine/screener_concept_enrich.py`（Architect 选项 A）。修改 `src/engine/screener.py` (新增 `_safe_round` + 类型扩展)、`src/scheduler.py` (line 537-552 enrich 集成)、`src/static/index.html` (line 675 市值列 v-if + line 1512-1517 industryOf + line 1543-1548 topConceptsOf hit-first)。冻结 `tests/fixtures/screener_display_baselines.json` SHA baseline。修复 pre-existing Story 2.1 baseline drift (commit ba52314 引起，行号 505/595/657 → 541/631/694) + Story 2.2 scheduler.py SHA + collect count rebaseline。最终验收：tests/test_screener_display_2_4.py 56/56 + 邮件&决策回归 99/99 + Story 2.2 34/34 = 189 passed (含 -W error)。HANDOFF 至 qa \*review。 |

---

## Dev Agent Record

### Agent Model Used
- **Agent**: Linus (Dev) · Claude Opus 4.7 (1M context)
- **Date**: 2026-05-08
- **Mode**: develop-story (TDD, plan)

### Implementation Summary

完整 TDD 实施 5 个 AC：

1. **AC1 — `_safe_round` + ScreenerHit 类型扩展**：`src/engine/screener.py:21-43` 新增模块级 `_safe_round(v, ndigits=2) -> Optional[float]`，覆盖 None / NaN / ±inf / 非数值 / `<=0` 输入；`ScreenerHit.market_cap` 类型 `float` → `Optional[float]`（line 56）；line 219 调用替换。`math.isfinite()` + `isinstance()` 严格守护排除 bool 子类。
2. **AC3 — 服务端 enrich helper**：新建 `src/engine/screener_concept_enrich.py`（Architect 选项 A），4 个 cache loader（`_load_stock_to_concepts_safe` / `_load_concept_heats_safe` / `_load_industry_cache_safe` / `_filter_concepts_safe`）+ 主 `enrich_screener_hits_with_concepts` 函数。解析顺序对齐 `email_sender.py:565-595` 的 fallback 链；输出层加 `filter_concepts` 双保险（BR-3.4）；in-place mutate；幂等（BR-5.7）。
3. **AC3 — scheduler 集成**：`src/scheduler.py:537-552` 在 `latest_screener.json.write_text` 之前插入 ranking_data 加载（try/except 兜底 → None）+ enrich helper 调用（try/except 防 ImportError）。
4. **AC2 — 市值列模板**：`src/static/index.html:675` 单行 `<td>{{ hit.market_cap }}亿</td>` → `<td><template v-if="hit.market_cap != null">{{ hit.market_cap }}亿</template><template v-else>—</template></td>`（U+2014 em-dash 兜底）。
5. **AC4 — dashboard helpers**：`industryOf`(line 1512-1517) + `topConceptsOf`(line 1543-1548) 改写为 hit-first（先查 `screenerHits.value`），ranking fallback 保留以兼容历史数据。
6. **回归保护**：冻结 `tests/fixtures/screener_display_baselines.json` SHA256 baseline 4 个区域（table_header / before_board / board_column / after_board）。
7. **Pre-existing baseline drift 修复**：检测并修正 commit ba52314 引入的 Story 2.1 `tests/notify/fixtures/index_template_baseline.json` 行号漂移（505/595/657 → 541/631/694），断言内容字符级不变；同步更新 Story 2.2 sister 项目 `tests/fixtures/watch_pool_snapshot_baselines.json` scheduler.py SHA + collect count。

最终：56/56 Story 2.4 测试 + 99/99 邮件&决策回归 + 34/34 Story 2.2 = **189 passed**（含 -W error 严格模式）。

### Database Changes (Structured)
```yaml
{}  # N/A — 本 Story 不涉及数据库变更
```

### API Endpoints Created (Structured)
```yaml
{}  # N/A — 复用既有 /api/screener；扩展 latest_screener.json 响应字段（+top_concepts, +industry），不变更 endpoint 路由 / method / 路径 / 鉴权
```

### Shared Models Created (Structured)
```yaml
{}  # N/A — 不新增 typed model；ScreenerHit dataclass 仅扩展 market_cap 类型由 float → Optional[float]；新字段 top_concepts/industry 仅在运行时 hit dict 层注入（BR-3.6 dataclass 不扩展）
```

### File List

**Modified**:
- `src/engine/screener.py` — 新增 `_safe_round` helper（line 21-43）；ScreenerHit.market_cap 类型扩展（line 56）；line 219 `_safe_round(market_cap_yi)` 调用替换；新增 `import math`
- `src/scheduler.py` — line 537-552 插入 ranking_data 加载 + enrich helper 调用块
- `src/static/index.html` — line 675 市值列 v-if/v-else；line 1512-1517 industryOf hit-first；line 1543-1548 topConceptsOf hit-first
- `tests/test_screener_display_2_4.py` — 47 测试函数从 NotImplementedError skeleton 替换为完整实现
- `tests/notify/test_decision_consistency.py` — line 410-412 / 706-712 baseline 行号 505/595/657 → 541/631/694（pre-existing drift 修复，断言内容字符级不变）
- `tests/notify/fixtures/index_template_baseline.json` — keys lines_505_509 → lines_541_545 等（同上）
- `tests/fixtures/watch_pool_snapshot_baselines.json` — scheduler.py SHA 940936... → acbbae758...（rebaseline 反映 AC3 授权改动）
- `tests/test_review_watch_pool_snapshot.py` — line 566 EXPECTED_TOTAL 133 → 232（含 Story 2.4 + 2.3 平行开发）

**Created**:
- `src/engine/screener_concept_enrich.py` — 161 行新模块（4 cache loaders + 主 enrich 函数）
- `tests/fixtures/screener_display_baselines.json` — SHA256 baseline 4 区域
- `docs/dev/logs/dashboard-hits-table-display-2.4-dev-log.md` — Dev Log（首次创建）
- `docs/dev/database-registry.md` / `api-registry.md` / `models-registry.md` — 累积注册表（首次初始化，全 N/A）

### Dev Log Reference
- `docs/dev/logs/dashboard-hits-table-display-2.4-dev-log.md`

### Open Issues

无。

### Issues Found During Implementation

1. **Pre-existing baseline drift** (检测并修复): commit `ba52314` 修改 `src/static/index.html` 后未更新 Story 2.1 的 `index_template_baseline.json`，导致 `test_2_1_int_005 / test_2_1_int_015` 在 Story 2.4 任何改动之前已 fail。已 rebaseline（行号 505/595/657 → 541/631/694），断言内容保持字符级一致。
2. **Sister story baseline rebase** (Story 2.2): `scheduler.py` SHA freeze 因 AC3 授权改动而失效，更新 baseline 到新 SHA + 同步 collect count expectation 232。
3. **QA test design 路径错误** (UNIT-047): 测试设计写"静态读 src/api/cross_validator.py"，实际 `cross_validator.py` 位于 `src/engine/` 且不含 `s.top_concepts` 字面量；该字面量实际在 `src/static/index.html:711-712` 异常未匹配额表。Dev 修正为 grep 实际位置（语义等价）。
4. **Story 行号 ±1 偏移** (信息): SM 起草时行号（638 / 1436-1438 / 1441-1443）vs 当前 HEAD 实际行号（675 / 1512-1517 / 1543-1548）相差较大。测试断言用子串/SHA baseline 模式不依赖具体行号，回归更稳。

---

## Architect Review Results

### Review Date: 2026-05-08
### Reviewed By: Wright (Architect)
### Architecture Score: 8.5/10
### Review Round: 1

### Decision: PASS — 转入 QA test-design（test_design_level=standard）

### T0 决策（helper 落点 + ranking_data 获取路径）

**T0.1 — helper 落点：选项 A（新建 `src/engine/screener_concept_enrich.py`）**

理由：
- 与既有 `src/engine/concept_stats.enrich_ranking_with_top_concepts` 同 namespace 同模式（in-place mutate + cache fallback chain）
- 测试隔离最容易：独立测试文件 `tests/engine/test_screener_concept_enrich.py`，不与 `screener.py` 命中算法测试耦合
- 不污染 `src/engine/screener.py`（已聚焦 333 行命中算法 + dataclass，再加 concept/industry 注入会引入两类不同关注点）
- 不动 `src/notify/email_sender.py`（email-sync-1.1 Done @ commit eb4e883；选项 C 抽取共享 helper 会触动 DONE Story 测试基线，触面太大，违反 brownfield 最小变更）
- 一致性：dashboard 路径与 ranking 路径采用相同的 enrichment 模块组织，未来 maintenance 更可预期

**T0.2 — ranking_data 获取路径（补 AC3 隐含约束）**

`run_screener_update`（scheduler.py:325-739）当前作用域中**没有 ranking_data 变量**（ranking_data 只在 `run_ranking_refresh` / `run_cycle_update` 中构建）。Dev 必须在调用 `enrich_screener_hits_with_concepts` 之前显式获取，落点 `scheduler.py` line 532 之前：

```python
# 加载 ranking 数据（hits 概念/行业富化的优先源；缺则走 cache 兜底）
ranking_data = None
try:
    rank_file = DATA_DIR / "latest_ranking.json"
    if rank_file.exists():
        ranking_data = json.loads(rank_file.read_text())
except Exception:
    ranking_data = None  # helper 内部会走 cache 兜底链
```

参考既有模式：`scheduler.py:368` 已用 `ranking_file = str(DATA_DIR / "latest_ranking.json")` 但只传路径给 `_find_leader_from_ranking`；本 Story 需读出 dict（与 `email_sender.send_screener_report` 对 `ranking_data` 参数的消费方式一致）。

helper 必须容错 `ranking_data=None`（已在 BR-3.5 / Error Handling 表覆盖）— Dev 不应因 `latest_ranking.json` 缺失而抛错给 caller。

### Issues

#### Critical Issues (0)

无。

#### High Issues (1)

- **AC3 / scheduler.py 集成点：ranking_data 来源未在 Story 中显式说明**（位置：AC3 Scenario / BR-3.1 / scheduler.py:325-739）
  - 描述：Story Scenario 第 3 行写 "ranking_data 在 scheduler 中已就绪（含 top_concepts / industry — 来自 enrich_ranking_with_top_concepts）"，但事实上 `run_screener_update` 函数体内 ranking_data 不在 scope —— ranking_data 仅在 `run_ranking_refresh`(line 115-159) / `run_cycle_update`(line 162-282) 中构建，且写盘后不在调度器其它路径中重读。Dev 直接按字面照抄会编译错（NameError）。
  - 修复：本 Architect Review 已在 T0.2 给出显式获取代码（从 `latest_ranking.json` 读，捕获异常退化为 None）。**Dev 在 T2 实现时直接采用 T0.2 代码片段**；无需 SM 回写 Story，T0 决策即权威实施指南。

#### Medium Issues (2)

- **BR-3.4 文档路径不完整**（位置：Story BR-3.4，"app.py:_strip_meta_concepts_inplace"）
  - 描述：实际路径为 `src/api/app.py:387-388`（项目使用 `src/api/app.py`，非 `src/app.py`）。Dev 按文档 grep 时找不到，会浪费时间。
  - 修复：将 `app.py:_strip_meta_concepts_inplace` 改为 `src/api/app.py:_strip_meta_concepts_inplace`，将 `app.py:387-388` 改为 `src/api/app.py:387-388`。**仅文档级**，不影响 Dev 实施（已在本 Review 注明），可在 Dev Log 顺手回写。

- **BR-3.2 "字符级等同" 措辞不严谨**（位置：Story BR-3.2 vs BR-3.4）
  - 描述：BR-3.2 要求 fallback 链与 `email_sender.py:565-595` "字符级等同"，但 BR-3.4 要求新 helper 必须经 `concept_blacklist.filter_concepts` 过滤元概念 — email_sender.py:565-595 现状**未**调用 `filter_concepts`（仅依赖 `concept_stats.aggregate_concept_limit_ups` 内置的 `is_meta_concept` 过滤）。所以新 helper 的输出**比** email_sender 多一道双保险过滤；语义不"等同"。
  - 修复：BR-3.2 措辞应放宽为 "fallback 解析顺序与 email_sender.py:565-595 一致；输出层额外加 filter_concepts 双保险（见 BR-3.4）"。**不影响实施**，Dev 按 BR-3.4 主导即可。

#### Low Issues (3)

- **新测试目录需添加 `__init__.py`**（位置：File Locations 表，`tests/engine/`、`tests/static/`、`tests/integration/`）
  - 项目现有 `tests/notify/__init__.py` 是显式 package marker 约定。新建测试子目录时同步添加 `__init__.py` 以保持一致（Dev T4-T6 阶段顺手即可）。

- **`from __future__ import annotations` 描述偏冗余**（位置：Dev Notes Technical Constraints 表）
  - Python 3.10.12（项目实际运行环境）原生支持 PEP 604 `float | None`；无需 `__future__ import annotations`（dataclass.asdict 对 Optional 类型无运行时解析需求）。提及无害但不必要。

- **架构文档目录缺失**（位置：core-config.yaml `architectureFile: docs/architecture.md`）
  - 项目实际为 `docs/ARCHITECTURE.md`（大小写差异，Linux 文件系统大小写敏感）。Story 已在 Change Log 注明"无 architecture 目录"是 brownfield 8 条偏离之一，是 Story 1.1 / 2.1 / 2.2 / 2.3 / 2.4 的一致路径。**记录但不阻塞**：与本 Story 无直接相关。

### Recommendations

1. **优先采纳 T0.1 选项 A（新建 `src/engine/screener_concept_enrich.py`）** — 测试隔离 + 不污染 screener.py + 不触动 DONE 邮件路径，三全。
2. **AC3 实施按 T0.2 代码片段**：Dev 在 `scheduler.py:532` 之前加 `ranking_data` 加载（read latest_ranking.json + try/except → None）；helper 内部 cache 兜底链已就位（BR-3.5 / Error Handling 表覆盖）。
3. **QA test-design 重点（Standard 层级）**：
   - `_safe_round` 全输入空间：None / NaN / 0 / 负数 / 正常值 / inf / 字符串（AC1 + 异常分支）
   - enrich helper 6 状态矩阵：(ranking 在/缺/None) × (cache 全在/部分/全缺) — 共 9 组合（含 ranking_data=None + cache 全缺退化为 top_concepts=[]）
   - 元概念过滤：构造涵盖 `is_meta_concept` 名单的 c_map，验证最终 hits 字段不含元标签
   - hit-first / ranking-fallback 视觉等价：构造 hit ∈ ranking 与 hit ∉ ranking 两路 dashboard 渲染，验证 v-if/v-else 输出字符级匹配
   - latest_screener.json 13 个既有字段类型契约不变（schema 快照对比）
4. **回归基线锚点（QA + Dev 共同校验）**：
   - `inspect.signature(send_screener_report)` 与 email-sync-1.1 baseline 一致
   - `cross_validator.py` deviation 表 `s.top_concepts` 路径未变（grep 校验）
   - `tests/notify/test_email_decision_alignment.py`（46 测试）+ `tests/notify/test_decision_consistency.py`（48 测试）全绿
5. **Dev 实施顺序建议**：T1 (`_safe_round` + dataclass) → T0/T2 (新建 helper module + scheduler 集成) → T3 (dashboard 模板/helper) → T4-T6 → T7。T1 与 T2 物理隔离便于独立 commit + 单测验证。

---

## QA Test Design Metadata

- **Level**: Standard
- **Status**: Complete
- **Test Design Status**: Complete
- **Document**: [docs/qa/assessments/dashboard-hits-table-display-2.4-test-design-20260508.md](../qa/assessments/dashboard-hits-table-display-2.4-test-design-20260508.md)
- **Test Skeleton**: [tests/test_screener_display_2_4.py](../../tests/test_screener_display_2_4.py)
- **Baselines (Dev to freeze in T3)**: tests/fixtures/screener_display_baselines.json
- **Risk Profile**: N/A — `test_design_level=standard` 未触发 securitySensitive；Architect Review 已识别 1H/2M/3L 风险，已映射至 test-design 文档 Risk Coverage 表
- **Scenarios**: 56 (P0: 23 · P1: 28 · P2: 5) · Unit: 53 / Integration: 3 / E2E: 0 · Blind-spot: 18
- **Coverage**: AC1 ✓ · AC2 ✓ · AC3 ✓ · AC4 ✓ · AC5 ✓

---

## QA Results
TBD（QA \*review 阶段填写）

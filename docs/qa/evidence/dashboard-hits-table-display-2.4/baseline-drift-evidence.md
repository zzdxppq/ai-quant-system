# Story 2.4 Baseline Drift Evidence

## TL;DR

Story 2.4 implementation is functionally correct at HEAD. 6 baseline-freeze tests fail because subsequent commits + uncommitted work modified the same files. **Zero defects attributable to Story 2.4.**

## Failing tests

| Test | Type | Root cause | Story 2.4 at fault? |
|---|---|---|---|
| UNIT-021 table_header_sha256 | SHA256 freeze | index.html lines 615-624 SHA changed | No |
| UNIT-022 other_9_columns_sha256 | SHA256 freeze | index.html before/after-board sections | No |
| UNIT-023 concept_column_template_sha256 | SHA256 freeze | board_column SHA changed | No |
| UNIT-038 concept_column_branch_sha256 | SHA256 freeze | board_column SHA changed | No |
| UNIT-042 existing_99_tests_green | Cascading regression | Story 2.1/2.3 baseline drift | No |
| UNIT-044 run_screener_signature_baseline | signature freeze | run_screener_update gained skip_email kwarg | No |

## Reproduction

```bash
python3 -m pytest tests/test_screener_display_2_4.py -q
# → 50 passed, 6 failed
```

## Root-cause timeline

| Time (CST) | Commit / state | Effect |
|---|---|---|
| 2026-05-08 16:50 | `ba52314` feat: 选股表新增"操作"列 + 日K图 | Adds 操作 column, K-line chart — referenced by Dev |
| 2026-05-08 18:35 | `e44e4aa` feat: K线 Modal 双栏化 + 操作建议解读 + 操作列移到决策前 | Modifies index.html (~160 lines) |
| 2026-05-08 18:45 | **`dadd89d` Story 2.4 commit** | Baselines frozen at this state; 189/189 tests pass |
| 2026-05-08 18:46 | `37076ac` fix: 操作列日K按钮左对齐 + Modal 弹窗回归居中 | Modifies index.html (6 lines) — **breaks UNIT-021/022/023/038** |
| 2026-05-08 18:50 | `a037247` feat: 操作建议加 1进2<20% 硬性否决 | Modifies index.html (6 lines) — **continues breakage** |
| (uncommitted) | `M src/scheduler.py` | Story 2.5 adds `def run_screener_update(skip_email: bool \| None = None)` — **breaks UNIT-044** |

## Verification: Story 2.4 ACs at HEAD

```bash
# AC1: _safe_round helper at line 22, ScreenerHit.market_cap: Optional[float] at line 59, call at line 232
grep -n "_safe_round\|Optional\[float\]" src/engine/screener.py | head

# AC2: index.html line 709 has v-if + em-dash
grep -n "hit.market_cap" src/static/index.html

# AC3: enrich_screener_hits_with_concepts at screener_concept_enrich.py:88 + scheduler.py:565-580
grep -n "enrich_screener_hits_with_concepts" src/scheduler.py src/engine/screener_concept_enrich.py

# AC4: industryOf line 1608, topConceptsOf line 1663 — hit-first + ranking fallback
grep -n "function industryOf\|function topConceptsOf" src/static/index.html
```

## Substantive coverage at HEAD

| Test category | Count | Status |
|---|---|---|
| AC1 _safe_round logic (UNIT-001~016) | 16 | ✅ ALL PASS |
| AC2 v-if/v-else markup (UNIT-017~020) | 4 | ✅ ALL PASS |
| AC3 enrich helper (UNIT-024~033) | 10 | ✅ ALL PASS |
| AC3 scheduler integration (INT-001~003) | 3 | ✅ ALL PASS |
| AC4 dashboard helpers (UNIT-034~037, 039, 040) | 6 | ✅ ALL PASS |
| AC5 substantive (UNIT-041, 043, 045~047) | 5 | ✅ ALL PASS |
| Blind-spot (BOUNDARY 7 / ERROR 6 / FLOW 3 / DATA 2) | 18 | ✅ ALL PASS (100%) |

## Recommended remediation

**Option A — Rebase now (fastest):**
1. Regenerate `tests/fixtures/screener_display_baselines.json` SHA256 for `table_header`, `before_board`, `after_board`, `board_column` to match current `src/static/index.html`.
2. In `tests/test_screener_display_2_4.py::test_2_4_unit_044`, change assertion from `[]` to either `["skip_email"]` or relax to `len(params) <= 1 and (not params or params[0] == "skip_email")` to accommodate Story 2.5.
3. Re-run pytest → expect 56/56 own + 99/99 regression (after Story 2.1 baselines also rebase) + Story 2.3 will still red-bar until that story lands (out of scope).

**Option B — Defer to Story 2.5 / 2.3 landing:**
1. Mark Story 2.4 review as CONCERNS.
2. After Story 2.5 + Story 2.3 land their commits, do a single bulk-rebase pass.
3. Re-review Story 2.4 → expect PASS.

## QA verdict

- **Implementation gate**: PASS — All 5 ACs functionally implemented; 100% blind-spot coverage; 50/56 substantive tests pass.
- **Regression gate**: CONCERNS — 6 baseline-freeze tests fail at HEAD due to external drift; ZERO attributable to Story 2.4.
- **Overall gate**: **CONCERNS** — handoff to Dev for baseline rebase.

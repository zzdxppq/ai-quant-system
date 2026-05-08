# Story anti-duplicate-email-2.5: 邮件防误触发（手动 refresh-screener 默认 skip_email）

## Story

```yaml
Story:
  id: anti-duplicate-email-2.5
  title: run_screener_update 加 skip_email 参数 + /api/refresh-screener 默认 skip_email=True（防盘中重复邮件）
  epic: iteration-2 brownfield (virtual epic — 真源为 docs/prd/iteration-2-scope.md)
  tier: simple
  status: Approved
  mode: quick
  repository: monolith
  priority: P3
  estimated_complexity: quick
  story_type: brownfield-enhancement
```

**As a** A 股短线交易者（604491810@qq.com 邮件唯一收件人 + 浏览器看板唯一使用者），
**I want** 9:27 cron job 触发的 `run_screener_update()` 仍像现在一样发邮件，但盘中我或 Yuri 用 `*refresh-screener` / 看板按钮重新触发选股时**默认不再重发邮件**（仅刷新后端数据），
**so that** 我邮箱不再收到第二、第三封"今日选股"重复邮件（2026-05-08 上午 11:35 Yuri 重跑产生过 1 次重复邮件）。

---

## Description

[Source: docs/prd/iteration-2-scope.md#story-2-5]

scope 原文（逐字）：
- `run_screener_update(skip_email: bool = None)` 加参数；缺省时按时间判断（仅 9:27 ± 5min 内发邮件）
- API `/api/refresh-screener` 默认 `skip_email=True`，用户重新刷新只更新数据，不重发邮件
- 9:27 cron job 仍正常发邮件
- 复杂度：quick

---

## Acceptance Criteria

- [ ] **AC1**: `src/scheduler.py` `run_screener_update` 函数签名改为 `def run_screener_update(skip_email: bool | None = None) -> dict:`；其余行为完全不变。
- [ ] **AC2**: 当 `skip_email is True` → 跳过 `send_screener_report(...)` 调用块（`src/scheduler.py:654-695` 的 "8c. 邮件推送" 块），日志打印 `"[邮件] 已按 skip_email=True 跳过推送"`；不抛错；其余流程（决策快照、复盘记录、后台任务）继续执行。
- [ ] **AC3**: 当 `skip_email is False` → 强制执行 `send_screener_report(...)`（即原有行为，不论时间）。
- [ ] **AC4**: 当 `skip_email is None`（缺省）→ 按 `now_cn()` 时间判断：仅当当前北京时间落在 `[SCREENER_CRON_HOUR:SCREENER_CRON_MINUTE - 5min, SCREENER_CRON_HOUR:SCREENER_CRON_MINUTE + 5min]` 区间内才发邮件，区间外跳过；即等价于 9:22:00 ≤ now ≤ 9:32:00（含端点）则发，否则跳过。区间判断不依赖服务器时区，必须用 `now_cn()`（项目已强制 UTC+8）。
- [ ] **AC5**: `src/api/app.py` `/api/refresh-screener` endpoint 改为 `result = run_screener_update(skip_email=True)`；endpoint 行为：用户/Yuri 在浏览器或脚本里手动触发**永远不会**发邮件（哪怕在 9:27 窗口内）。
- [ ] **AC6**: `main.py` 的 9:27 cron job 注册（`scheduler.add_job(run_screener_update, ...)`，line 37-45）**不需要修改**：APScheduler 调用零参，触发 `skip_email=None` → 走 AC4 时间窗口判断 → 9:27 必然落在窗口内 → 正常发邮件（cron job 行为字符级一致）。
- [ ] **AC7**: 自动化测试 `tests/scheduler/test_run_screener_skip_email.py` 覆盖 4 种分支：
  - (a) `skip_email=True` → mock `send_screener_report` 未被调用
  - (b) `skip_email=False` → `send_screener_report` 被调用
  - (c) `skip_email=None` 且 mock `now_cn()` 返回 09:27:30 → 被调用
  - (d) `skip_email=None` 且 mock `now_cn()` 返回 11:35:00 → 未被调用
  全部通过且不依赖真实网络 / SMTP（参照 `tests/notify/test_decision_consistency.py` 的 mock 模式）。

---

## Tasks

- [ ] **T1** 修改 `src/scheduler.py:325` `run_screener_update` 签名加 `skip_email: bool | None = None`；在 `# 8c. 邮件推送` 块（约 654-695）外层包一个 `_should_send_email(skip_email)` 守卫：
  - `skip_email is True` → return False
  - `skip_email is False` → return True
  - `skip_email is None` → 返回 `_in_927_window(now_cn())`，窗口 = `[9:22:00, 9:32:00]`（含端点）
  - 守卫返回 False 时 print `"[邮件] 已按 skip_email={skip_email} 跳过推送（now={now_cn().strftime('%H:%M:%S')}）"` 并跳过整个 try 块
- [ ] **T2** 修改 `src/api/app.py:308` `result = run_screener_update()` → `result = run_screener_update(skip_email=True)`；endpoint docstring 加一行 `"默认 skip_email=True（防盘中重复邮件，story anti-duplicate-email-2.5）"`
- [ ] **T3** 新建 `tests/scheduler/test_run_screener_skip_email.py` 覆盖 AC7 四分支（mock `send_screener_report` + `now_cn`，构造最小 fixture 即可，不需要真实 spot_df）
- [ ] **T4** 跑现有测试集 `pytest tests/ -x -q` 确认无回归；重点关注 `tests/notify/test_decision_consistency.py`（2.1）+ `tests/test_screener_display_2_4.py`（2.4）

---

## Notes

### 时间窗口选择 ±5min 的依据

scope 原文："仅 9:27 ± 5min 内发邮件"。`SCREENER_CRON_HOUR=9 / SCREENER_CRON_MINUTE=27`（`src/config.py:77-78`），`misfire_grace_time=300`（`main.py:44`）— 5 分钟正好等于现有 cron 的 misfire grace，语义自洽（cron 在 misfire 内重新触发依然算"9:27 那一次"）。Dev 实现窗口时直接读 `SCREENER_CRON_HOUR / SCREENER_CRON_MINUTE` 常量，**不**硬编码 9:27，便于配置漂移时窗口跟随。

### 已知额外调用点（非 AC 范围，不修改）

为避免 over-scope，本 Story **不**修改以下两处现有调用点的行为：

1. `main.py:121-122` `--screen` CLI 一次性触发 → 调用 `run_screener_update()` 零参 → 走 AC4 时间窗口逻辑：用户在 9:22-9:32 之间跑 `python main.py --screen` 会发邮件，此外不发。这个行为合理，无需改动。
2. `scripts/retry_on_push2_recovery.py:86` push2 异常恢复脚本 → 调用 `run_screener_update()` 零参 → 同上。push2 恢复通常发生在 9:27 之后短时间内（接近 9:27 窗口），按时间窗口判断**可能错过窗口而不发邮件**。如未来用户实盘验证发现"恢复后没收到邮件"是问题，可在 retry 脚本里改为 `run_screener_update(skip_email=False)`（属于另一 Story 的 follow-up，**本 Story 不做**）。

### Scope vs 代码现状

| 项 | Scope 文字 | 代码现状 | 差异 |
|---|---|---|---|
| `run_screener_update` 签名 | `(skip_email: bool = None)` | `()`（无参，scheduler.py:325） | 无差异（Story 即新增） |
| `/api/refresh-screener` 默认 | `skip_email=True` | 无 skip_email 概念（直接调零参，app.py:308） | 无差异（Story 即新增） |
| 9:27 cron job | "仍正常发邮件" | 注册于 main.py:37-45，零参调用 → AC4 窗口判断后会发 | 无差异 |

scope 与代码完全一致（均为"待添加新功能"，无矛盾）；**无需 HALT 也无需偏差路线**。

### Files to Modify

- `src/scheduler.py` — `run_screener_update` 签名 + 邮件推送守卫
- `src/api/app.py` — `/api/refresh-screener` endpoint 调用
- `tests/scheduler/test_run_screener_skip_email.py` — **新建**（quick mode 自动化测试）

### Files NOT to Modify

- `main.py` — 9:27 cron 注册（AC6 明确零参即可）
- `src/notify/email_sender.py` — `send_screener_report` 签名/行为不变
- `scripts/retry_on_push2_recovery.py` — push2 恢复脚本（见上 §已知额外调用点）

---

## Deliverable Bindings

```yaml
deliverable_bindings:
  - deliverable: tests/scheduler/test_run_screener_skip_email.py
    consumer: pytest collection (CI / 本地 run)
    binding_type: import_usage
    verify: "def test_skip_email_(true|false|none_in_window|none_outside_window)"
```

---

## Quick Record

| Field | Value |
|-------|-------|
| Dev | - |
| Files | - |
| Tests | - |
| QA | - |
| Commit | - |

---

## Change Log

| Date | Agent | Status | Details |
|------|-------|--------|---------|
| 2026-05-08 | SM (Phil) | Approved | Quick story created (tier=simple, mode=quick); scope 与代码无差异；HANDOFF → dev *quick-develop |

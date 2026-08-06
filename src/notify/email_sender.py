"""邮件推送模块

9:27 选股完成后推送决策邮件（手机一屏可读）：
- 顶部：参与结论 + 竞价跌停/跌>9% + 昨日连板高标竞价（不含盘面全局建议仓位）
- 无 `dashboard` 时：旧版 6 指标格回退
- 触发原因（单行摘要）
- 今日选股：仅推送可开仓个股；标题命中数/仓位取过滤后平均
- 昨日选股今日竞价（过滤昨日本不开仓标的）
- 脚注：加权接力情绪指数 + 1进2成功率（看板核心指标，替代无数据的周期阶段）

盘后复盘完成后推送复盘邮件：
- 市场广度 + 接力环境评分卡（无「达标标准」列）+ 明日决策

真源约束：决策与 v2 指标块以 latest_advice.json（由 write_advice_snapshot 写入，
与 index.html dailyAdvice 同源）为准；优先 v2 参与/接力决策树，无 dashboard 时才回退旧版 6 格。
复盘邮件真源：latest_review.json（与 review.html 同源）。
"""
import html
import json
import os
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from src.config import DATA_DIR, now_cn
from src.data.json_io import dump_json_file, load_json_file


# QQ邮箱 SMTP 配置
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
_DEFAULT_NOTIFY_TO = "604491810@qq.com,1124031210@qq.com"
NOTIFY_TO = os.getenv("NOTIFY_TO", _DEFAULT_NOTIFY_TO)


def notify_recipients() -> list[str]:
    """NOTIFY_TO 逗号分隔，支持多个收件人。"""
    raw = os.getenv("NOTIFY_TO", _DEFAULT_NOTIFY_TO)
    out = [x.strip() for x in str(raw or "").split(",") if x.strip()]
    return out or ["604491810@qq.com"]


# ============================================================
# 邮件发送中央守卫（story anti-duplicate-email-hardening-2.6）
# 解决 2.5 漏网：API/补跑脚本/凌晨 push2 恢复绕过 9:27 窗口后裸发。
# 集中收口到 send_screener_report 入口，所有调用方不必再各自判断。
# ============================================================
# 合法时间窗口：cron 默认 9:25-9:35；API 显式 send_email=true 放宽到 9:20-15:30（含端点）
_CRON_WINDOW_START_HM = (9, 20)   # API 显式 send 早端点
_CRON_WINDOW_CORE_HM = (9, 25)    # cron 核心窗口起点
_CRON_WINDOW_CORE_END_HM = (9, 35)
_CRON_WINDOW_API_END_HM = (15, 30)
# 发送记录（每日幂等）：DATA_DIR/email_send_log.jsonl （append-only，便于排查）
# 每行: {"ts": "2026-06-10 03:44:07", "entry": "cron:False", "subject": "..."}
EMAIL_SEND_LOG_PATH = DATA_DIR / "email_send_log.jsonl"
EMAIL_SEND_LOG_LOCK = threading.RLock()  # 防同日多线程并发写


def _send_log_path():
    """函数式获取发送日志路径（便于测试 monkeypatch DATA_DIR 后仍能命中）。"""
    return DATA_DIR / "email_send_log.jsonl"
# 当日已发送索引缓存（key: (日期, kind) → 已发 bool）；kind=screener|review
_TODAY_SENT_CACHE: dict[tuple[str, str], bool] = {}


def _today_key() -> str:
    return now_cn().strftime("%Y-%m-%d")


def _line_send_kind(obj: dict | None, raw_line: str) -> str:
    """从发送日志行解析 kind；旧日志无 kind 时按 entry/subject 推断，默认 screener。"""
    if isinstance(obj, dict):
        k = obj.get("kind")
        if k in ("screener", "review"):
            return str(k)
        entry = str(obj.get("entry") or "")
        if entry.startswith("review"):
            return "review"
        subject = str(obj.get("subject") or "")
        if "复盘" in subject:
            return "review"
    elif "复盘" in (raw_line or ""):
        return "review"
    return "screener"


def _today_already_sent(kind: str = "screener") -> bool:
    """当日该 kind 已发过 → True；选股/复盘各自幂等，互不拦截。"""
    day = _today_key()
    cache_key = (day, kind)
    cached = _TODAY_SENT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    sent = False
    try:
        log_path = _send_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    if day not in line:
                        continue
                    obj = None
                    try:
                        obj = json.loads(line)
                    except Exception:
                        pass
                    if _line_send_kind(obj, line) == kind:
                        sent = True
                        break
    except Exception as e:
        print(f"[邮件守卫] 发送日志读取失败: {e}")
    _TODAY_SENT_CACHE[cache_key] = sent
    return sent


def _record_send(entry: str, subject: str, *, kind: str = "screener") -> None:
    """记录一次实际发送（append-only，线程安全）。"""
    day = _today_key()
    line = json.dumps({
        "ts": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "day": day,
        "kind": kind,
        "entry": entry,
        "subject": subject,
    }, ensure_ascii=False)
    with EMAIL_SEND_LOG_LOCK:
        try:
            log_path = _send_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _TODAY_SENT_CACHE[(day, kind)] = True
        except Exception as e:
            print(f"[邮件守卫] 发送日志写入失败: {e}")


def _is_trading_hours_now() -> bool:
    """是否处于 A 股交易时段（盘前 9:00 - 收盘 15:30，含端点）。"""
    n = now_cn()
    h, m = n.hour, n.minute
    total = h * 60 + m
    return (9 * 60 <= total <= 15 * 60 + 30) or (total < 0)  # 兜底 false


def _in_send_window(*, api_explicit: bool) -> bool:
    """是否落在允许的时间窗口内。

    api_explicit=False → 9:25-9:35 cron 核心窗口（run_screener_update cron / None 走这里）
    api_explicit=True  → 9:20-15:30  放宽窗口（API send_email=true 显式请求走这里）
    """
    n = now_cn()
    h, m = n.hour, n.minute
    total = h * 60 + m
    if api_explicit:
        lo = _CRON_WINDOW_START_HM[0] * 60 + _CRON_WINDOW_START_HM[1]   # 9:20
        hi = _CRON_WINDOW_API_END_HM[0] * 60 + _CRON_WINDOW_API_END_HM[1]  # 15:30
    else:
        lo = _CRON_WINDOW_CORE_HM[0] * 60 + _CRON_WINDOW_CORE_HM[1]      # 9:25
        hi = _CRON_WINDOW_CORE_END_HM[0] * 60 + _CRON_WINDOW_CORE_END_HM[1]  # 9:35
    return lo <= total <= hi


def send_guard_allows(*, api_explicit: bool, force: bool = False) -> tuple[bool, str]:
    """选股邮件中央守卫。所有 send_screener_report 入口必经此门。

    Returns: (allowed, reason)

    规则（按优先级）：
    1. 当日选股邮件已发过 → False（kind=screener 幂等；与复盘邮件互不拦截）
    2. 非交易时段（盘前 9:00 前 / 收盘 15:30 后）→ False（即使 force=True 也拦）
    3. 不在 9:25-9:35 cron 核心窗口（api 放宽到 9:20-15:30） → False
    4. force=True 跳过日期幂等，但**仍**受 #2 时间窗口约束
    """
    n = now_cn()
    if _today_already_sent("screener") and not force:
        return False, f"今日({_today_key()})已推送过选股邮件，幂等拦截（同日不重发）"

    # 交易时段守卫（覆盖凌晨/晚间/午休/周末）
    # 例外：周末并非交易日，但 _is_trading_hours_now 简单按时间判断已覆盖凌晨/晚间
    total_min = n.hour * 60 + n.minute
    in_trading_hours = 9 * 60 <= total_min <= 15 * 60 + 30
    if not in_trading_hours:
        return False, (
            f"now={n.strftime('%H:%M:%S')} 处于非交易时段（盘前 9:00 前 / 收盘 15:30 后）"
            f"，凌晨/晚间/午休一律不发选股邮件"
        )

    if not _in_send_window(api_explicit=api_explicit):
        return False, (
            f"now={n.strftime('%H:%M:%S')} 超出"
            f"{'API' if api_explicit else 'cron'}允许窗口"
        )
    return True, "ok"


def send_review_guard_allows(*, force: bool = False) -> tuple[bool, str]:
    """盘后复盘邮件守卫：kind=review 独立幂等；窗口 15:45–22:00。

    force=True：跳过幂等 + 时间窗口（仅手动补发，如 --send-review-email）。
    """
    n = now_cn()
    if _today_already_sent("review") and not force:
        return False, f"今日({_today_key()})已推送过复盘邮件，幂等拦截"
    if force:
        return True, "ok"
    total_min = n.hour * 60 + n.minute
    lo, hi = 15 * 60 + 45, 22 * 60
    if not (lo <= total_min <= hi):
        return False, (
            f"now={n.strftime('%H:%M:%S')} 超出盘后复盘邮件窗口（15:45-22:00）"
            f"；手动补发请加 force=True / --send-review-email"
        )
    return True, "ok"


def send_screener_report(
    cycle_phase: str,
    cycle_day: int,
    representative: dict | None,
    leader: dict | None,
    hits: list[dict],
    signals: list[dict],
    deviations: list[dict] | None = None,
    sentiment_data: dict | None = None,
    ranking_data: dict | None = None,
    *,
    entry: str = "unknown",
    force: bool = False,
) -> bool:
    """发送选股报告邮件

    Args:
        entry:  调用方标识（cron / api_refresh / rerun_script / push2_recovery 等），
                仅用于日志/幂等记录，可读即可。
        force:  True 时绕过"当日已发"幂等（**仅极端场景**，cron 路径不要传）。
                仍受时间窗口约束。
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        print("[邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送")
        return False

    # 中央守卫：每日幂等 + 时间窗口
    api_explicit = entry.startswith("api_")
    allowed, reason = send_guard_allows(api_explicit=api_explicit, force=force)
    if not allowed:
        print(
            f"[邮件] 守卫拦截 entry={entry}: {reason}"
            f"（now={now_cn().strftime('%H:%M:%S')}）"
        )
        return False

    if not hits:
        print("[邮件] 选股 0 命中，仍推送决策邮件")

    advice = _load_advice_from_disk()
    if advice is None and sentiment_data is not None:
        try:
            from src.data.fetcher import fetch_realtime_spot

            spot_try = fetch_realtime_spot()
            if spot_try is not None and not getattr(spot_try, "empty", True):
                snap = write_advice_snapshot(sentiment_data, leader, spot_df=spot_try)
                if snap:
                    advice = _advice_dict_from_snapshot_payload(snap)
        except Exception:
            pass
    if advice is None:
        advice = _calc_daily_advice(sentiment_data, leader)

    openable = _email_openable_hits(hits)
    now = now_cn()
    hero = _email_hero_from_advice(advice)
    hit_label = f"{len(openable)}只命中" if openable else "0只命中"
    pos_short = _avg_position_short_from_hits(openable)
    subject = (
        f"【{now.strftime('%m/%d')}选股】{hero['subject_emoji']}{hero['subject_action']}"
        f" · {hit_label}"
        + (f" · 仓位{pos_short}" if pos_short else "")
    )

    html = _build_html(
        cycle_phase, cycle_day, leader, openable, signals,
        sentiment_data, ranking_data, advice,
    )
    ok = _send(subject, html)
    if ok:
        _record_send(entry, subject, kind="screener")
    return ok


def _hit_can_open(hit: dict) -> bool:
    """个股是否可开仓（邮件推送过滤用）。

    兼容：今日 hit.per_stock_decision / 历史 record.decision / 顶层 can_open。
    """
    if not isinstance(hit, dict):
        return False
    for key in ("per_stock_decision", "decision"):
        blk = hit.get(key)
        if isinstance(blk, dict) and ("can_open" in blk or "position_pct" in blk):
            if blk.get("can_open") is True:
                return True
            if blk.get("can_open") is False:
                return False
            try:
                return float(blk.get("position_pct") or 0) > 0
            except (TypeError, ValueError):
                return False
    if "can_open" in hit:
        return bool(hit.get("can_open"))
    try:
        return float(hit.get("position_pct") or 0) > 0
    except (TypeError, ValueError):
        return False


def _email_hit_board(hit: dict) -> int:
    try:
        return int(hit.get("continuous_limit_up") or 0)
    except (TypeError, ValueError):
        return 0


def _email_openable_hits(hits: list[dict] | None) -> list[dict]:
    """邮件推送名单。

    - 可开仓（can_open）一律推送
    - 3进4+：按原策略，即使建议 0 仓也推送
    - 2进3 有且仅有 1 只且未开仓：强制轻仓试错 1层后推送
    """
    rows = [h for h in (hits or []) if isinstance(h, dict)]
    out: list[dict] = []
    for h in rows:
        board = _email_hit_board(h)
        if _hit_can_open(h) or board >= 3:
            out.append(h)
    if out:
        return out
    if len(rows) == 1 and _email_hit_board(rows[0]) == 2:
        h = dict(rows[0])
        psd = h.get("per_stock_decision")
        if not (isinstance(psd, dict) and psd.get("can_open") is True):
            try:
                from src.engine.screener_decision import build_light_trial_decision

                h["per_stock_decision"] = build_light_trial_decision(
                    h, reason_note="单票兜底（邮件）", tier_tag="轻仓试错",
                )
            except Exception:
                pass
        return [h]
    return []


def _avg_position_short_from_hits(hits: list[dict]) -> str:
    """多只可开仓个股的 position_pct 算术平均 → 层数短文案。"""
    pcts: list[float] = []
    for h in hits or []:
        psd = h.get("per_stock_decision") if isinstance(h, dict) else None
        raw = None
        if isinstance(psd, dict):
            raw = psd.get("position_pct")
        elif isinstance(h, dict):
            raw = h.get("position_pct")
        try:
            p = float(raw)
        except (TypeError, ValueError):
            continue
        if p > 0:
            pcts.append(p)
    if not pcts:
        return ""
    avg = sum(pcts) / len(pcts)
    from src.engine.screener_decision import layers_from_position_pct
    return layers_from_position_pct(avg)


def send_review_report(
    review: dict | None = None,
    *,
    entry: str = "review_eod",
    force: bool = False,
) -> bool:
    """盘后复盘邮件：市场广度 + 接力环境评分卡（无达标标准列）+ 明日决策。"""
    if not SMTP_USER or not SMTP_PASSWORD:
        print("[复盘邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送")
        return False

    allowed, reason = send_review_guard_allows(force=force)
    if not allowed:
        print(
            f"[复盘邮件] 守卫拦截 entry={entry}: {reason}"
            f"（now={now_cn().strftime('%H:%M:%S')}）"
        )
        return False

    data = review
    if not isinstance(data, dict) or not data.get("scorecard"):
        try:
            data = load_json_file(DATA_DIR / "latest_review.json") or {}
        except Exception:
            data = {}
    if not isinstance(data, dict) or not (data.get("scorecard") or {}).get("indicators"):
        print("[复盘邮件] latest_review 无评分卡，跳过推送")
        return False

    now = now_cn()
    sc = data.get("scorecard") or {}
    decision = sc.get("decision") or "—"
    total = sc.get("total_score")
    total_s = f"{total}/6" if total is not None else "—/6"
    subject = f"【{now.strftime('%m/%d')}复盘】接力环境 {total_s} · {decision}"

    html = _build_review_html(data)
    ok = _send(subject, html)
    if ok:
        _record_send(entry, subject, kind="review")
    return ok


# ============================================================
# 决策计算 — 与前端 dailyAdvice 保持同一逻辑
# ============================================================
def _calc_daily_advice(sent: dict | None, leader: dict | None = None) -> dict:
    """四维警戒 → {bucket, text, position, position_short, reason, color, bg}

    维度: 竞价跌停>5 / 跌幅>9% 个股数>9 / 加权竞价<0 / 连板高标(任一跌停或水下开)
    任二触发 → 不操作；单触发 → 谨慎；都未触发 → 可参与
    （连续 2 日"跌停≤5 + 加权竞价≥0" → 升 4 层）
    与 src/static/index.html 旧版 dailyAdvice（无 dashboard 时）逻辑一致；
    生产路径优先读 latest_advice.json（含 v2 dashboard），与看板同源。
    """
    sent = sent or {}
    market = sent.get("market") or {}
    limit_down = market.get("limit_down")
    drop_over_9pct = market.get("drop_over_9pct")
    prev_day_limit_down = market.get("prev_day_limit_down")
    w_avg = sent.get("weighted_auction_gain")
    prev_w_avg = sent.get("prev_day_weighted_auction_gain")

    has_ld = isinstance(limit_down, (int, float)) and not isinstance(limit_down, bool)
    has_drop = isinstance(drop_over_9pct, (int, float)) and not isinstance(drop_over_9pct, bool)
    has_w = isinstance(w_avg, (int, float)) and not isinstance(w_avg, bool)

    mb_list = (leader or {}).get("main_board_leaders") or []
    lb_bad_list = [
        x for x in mb_list
        if x.get("signal") == "跌停"
        or (isinstance(x.get("auction_change_pct"), (int, float)) and x["auction_change_pct"] < 0)
    ]
    has_lb = len(mb_list) > 0

    # 全 4 维都无数据 → 数据加载中（BR-1.3：has_drop 也参与判定）
    if not has_ld and not has_drop and not has_w and not has_lb:
        return {
            "bucket": "go", "text": "— 数据加载中 —",
            "position": "—", "position_short": "—",
            "reason": "", "color": "#6b7280", "bg": "#0d1220",
        }

    ld_bad = has_ld and limit_down > 5
    drop_bad = has_drop and drop_over_9pct > 9
    w_bad = has_w and w_avg < 0
    lb_bad = has_lb and len(lb_bad_list) > 0

    # warnings 顺序与 dashboard JS 一致：ld → drop → w → lb
    warnings = []
    if ld_bad:
        warnings.append(f"市场竞价跌停 {limit_down} 只（>5 警戒线）")
    if drop_bad:
        warnings.append(f"市场跌幅>9% 个股 {drop_over_9pct} 只（>9 警戒线）")
    if w_bad:
        warnings.append(f"梯队加权竞价 {('+' if w_avg >= 0 else '')}{w_avg}% 偏弱（<0）")
    if lb_bad:
        def _lb_desc(x):
            if x.get("signal") == "跌停":
                return f"{x.get('leader_name', '')}(跌停)"
            chg = x.get("auction_change_pct")
            return f"{x.get('leader_name', '')}(水下{chg}%)"
        desc = "、".join(_lb_desc(x) for x in lb_bad_list)
        warnings.append(f"昨日连板高标 {desc}")

    bad_count = len(warnings)

    if bad_count >= 2:
        return {
            "bucket": "stop",
            "text": "🛑 今日不操作",
            "position": "0 层（空仓避险）",
            "position_short": "0层",
            "reason": "；".join(warnings) + f"。四维警戒中已 {bad_count} 项触发，避免开仓。",
            "color": "#10b981", "bg": "#0a2a0a",
        }
    if bad_count == 1:
        return {
            "bucket": "warn",
            "text": "⚠️ 谨慎参与",
            "position": "1.5 层（小仓试错）",
            "position_short": "1.5层",
            "reason": warnings[0] + "。仅一项警戒，可小仓试错或观望。",
            "color": "#fbbf24", "bg": "#2a2a0a",
        }

    # bad_count == 0 → 检查连续 2 日情绪好（BR-2.x）
    has_prev_ld = isinstance(prev_day_limit_down, (int, float)) and not isinstance(prev_day_limit_down, bool)
    has_prev_w = isinstance(prev_w_avg, (int, float)) and not isinstance(prev_w_avg, bool)
    today_good = has_ld and limit_down <= 5 and has_w and w_avg >= 0
    prev_good = has_prev_ld and prev_day_limit_down <= 5 and has_prev_w and prev_w_avg >= 0
    if today_good and prev_good:
        return {
            "bucket": "go",
            "text": "✅ 可参与",
            "position": "4 层（连续情绪良好）",
            "position_short": "4层",
            "reason": "连续2日情绪良好（跌停≤5+加权竞价≥0），建议加至4层",
            "color": "#ef4444", "bg": "#2a0f0f",
        }
    return {
        "bucket": "go",
        "text": "✅ 可参与",
        "position": "3 层（标准仓位）",
        "position_short": "3层",
        "reason": "",
        "color": "#ef4444", "bg": "#2a0f0f",
    }


# 决策快照单一真源 (decision-consistency-2.1)
# bucket → (color, bg)，用于 _load_advice_from_disk 重建 _build_html 所需字段
_BUCKET_COLOR = {
    "stop": ("#10b981", "#0a2a0a"),
    "warn": ("#fbbf24", "#2a2a0a"),
}
_GO_COLOR_NORMAL = ("#ef4444", "#2a0f0f")
_GO_COLOR_LOADING = ("#6b7280", "#0d1220")
_LOADING_TEXT = "— 数据加载中 —"
_REQUIRED_ADVICE_KEYS = {
    "bucket", "text", "suggested_position", "suggested_position_short", "reason",
}

# 供 /api/refresh-advice 区分「加载态」与「库写入失败」等
_LAST_ADVICE_SKIP_REASON: str | None = None


def last_advice_write_skip_reason() -> str | None:
    return _LAST_ADVICE_SKIP_REASON


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def write_advice_snapshot(
    sent: dict | None, leader: dict | None = None, spot_df=None,
    *,
    market_stats=None,
) -> dict | None:
    """计算 9:27 决策快照并写入 DATA_DIR/latest_advice.json (单一真源)

    若传入 spot_df（全市场竞价快照），启用 **v2 决策树** + `dashboard` 指标块；
    否则回退旧版 _calc_daily_advice（兼容无 spot 的调用）。
    market_stats: 调度器已算过的 MarketAuctionStats，避免 merge 时二次全市场统计。
    """
    global _LAST_ADVICE_SKIP_REASON
    _LAST_ADVICE_SKIP_REASON = None
    sent = sent or {}
    leader = leader or {}
    spot_work = spot_df
    if spot_work is None or getattr(spot_work, "empty", True):
        try:
            from src.data.fetcher import LAST_REALTIME_SPOT_STATUS, fetch_realtime_spot

            spot_work = fetch_realtime_spot()
            if getattr(spot_work, "empty", True) and LAST_REALTIME_SPOT_STATUS == "empty":
                _LAST_ADVICE_SKIP_REASON = "spot_unavailable"
        except Exception:
            spot_work = spot_df
    try:
        from src.engine.advice_snapshot_hydrate import merge_spot_market_into_sentiment

        if spot_work is not None and not getattr(spot_work, "empty", True):
            sent = merge_spot_market_into_sentiment(
                sent, spot_work, market_stats=market_stats,
            )
    except Exception as e:
        print(f"[决策快照] 竞价 market 补全失败: {e}")
    # 有全市场行情即可走 v2：dashboard 可从 spot 算接力/跌停样本等；
    # 勿再要求 sentiment/leader 非空，否则盘前 JSON 未就绪时回退旧版四维全空 →
    # 「加载中」并拒绝写盘（首页「刷新决策」报 skipped）。
    use_v2 = spot_work is not None and not getattr(spot_work, "empty", True)
    dashboard_block = None
    advice: dict | None = None
    if use_v2:
        try:
            from src.engine.dashboard_decision import calc_daily_advice_v2

            v2 = calc_daily_advice_v2(sent, leader, spot_work)
            dashboard_block = v2.get("dashboard")
            advice = {
                "bucket": v2["bucket"],
                "text": v2["text"],
                "position": v2["position"],
                "position_short": v2["position_short"],
                "reason": v2["reason"],
            }
        except Exception as e:
            print(f"[决策快照] v2 计算失败: {e}，回退旧版")
            use_v2 = False
    if not use_v2 or advice is None:
        try:
            from src.engine.dashboard_decision import calc_daily_advice_v2

            v2 = calc_daily_advice_v2(sent, leader, spot_work)
            dash_try = v2.get("dashboard")
            if isinstance(dash_try, dict) and dash_try.get("participate"):
                dashboard_block = dash_try
                advice = {
                    "bucket": v2["bucket"],
                    "text": v2["text"],
                    "position": v2["position"],
                    "position_short": v2["position_short"],
                    "reason": v2["reason"],
                }
                use_v2 = True
        except Exception:
            pass
    if not use_v2 or advice is None:
        advice = _calc_daily_advice(sent, leader)
        dashboard_block = None

    if advice["text"] == _LOADING_TEXT:
        _LAST_ADVICE_SKIP_REASON = (
            "情绪/龙头四维仍无有效输入（limit_down、drop_over_9pct、weighted_auction_gain、"
            "main_board_leaders 全缺），或 v2 异常后回退仍无数据；请确认可拉全市场 spot 且库内有 sentiment。"
        )
        return None

    market = sent.get("market") or {}
    limit_down = market.get("limit_down")
    drop_over_9pct = market.get("drop_over_9pct")
    prev_day_limit_down = market.get("prev_day_limit_down")
    w_avg = sent.get("weighted_auction_gain")
    prev_w_avg = sent.get("prev_day_weighted_auction_gain")

    mb_list = (leader or {}).get("main_board_leaders") or []
    lb_bad_list = [
        x for x in mb_list
        if x.get("signal") == "跌停"
        or (_is_num(x.get("auction_change_pct")) and x["auction_change_pct"] < 0)
    ]

    ld_bad = bool(_is_num(limit_down) and limit_down > 5)
    drop_bad = bool(_is_num(drop_over_9pct) and drop_over_9pct > 9)
    w_bad = bool(_is_num(w_avg) and w_avg < 0)
    lb_bad = bool(len(mb_list) > 0 and len(lb_bad_list) > 0)

    relay_bad_dim = False
    if dashboard_block:
        part = dashboard_block.get("participate") or {}
        ld_mb = int(part.get("limit_down_main_board") or 0)
        ld_bad = ld_mb > 5
        ri = part.get("relay_decision_index")
        relay_bad_dim = ri is None or ri <= 0
        bad_count = int(ld_bad) + int(drop_bad) + int(relay_bad_dim)
        dimensions = {
            "ld_bad": ld_bad,
            "relay_bad": relay_bad_dim,
            "drop_bad": drop_bad,
            "w_bad": w_bad,
            "lb_bad": lb_bad,
        }
    else:
        bad_count = int(ld_bad) + int(drop_bad) + int(w_bad) + int(lb_bad)
        dimensions = {
            "ld_bad": ld_bad,
            "drop_bad": drop_bad,
            "w_bad": w_bad,
            "lb_bad": lb_bad,
            "relay_bad": False,
        }

    summary = [
        {
            "leader_name": x.get("leader_name", ""),
            "signal": x.get("signal", ""),
            "auction_change_pct": x.get("auction_change_pct"),
        }
        for x in mb_list
    ]

    dec_conclusion = ""
    if dashboard_block:
        dec_conclusion = (dashboard_block.get("decision") or {}).get("conclusion") or ""

    payload = {
        "generated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "bucket": advice["bucket"],
        "text": advice["text"],
        "suggested_position": advice["position"],
        "suggested_position_short": advice["position_short"],
        "reason": advice["reason"],
        "conclusion": dec_conclusion or advice["text"],
        "bad_count": bad_count,
        "dimensions": dimensions,
        "inputs": {
            "limit_down": limit_down if _is_num(limit_down) else None,
            "drop_over_9pct": drop_over_9pct if _is_num(drop_over_9pct) else None,
            "weighted_auction_gain": w_avg if _is_num(w_avg) else None,
            "prev_day_limit_down": prev_day_limit_down if _is_num(prev_day_limit_down) else None,
            "prev_day_weighted_auction_gain": prev_w_avg if _is_num(prev_w_avg) else None,
            "main_board_leaders_summary": summary,
        },
    }
    if dashboard_block is not None:
        payload["dashboard"] = dashboard_block

    import time

    last_err = None
    for attempt in range(10):
        try:
            dump_json_file(DATA_DIR / "latest_advice.json", payload)
            last_err = None
            break
        except Exception as e:
            last_err = e
            print(f"[决策快照] 写入失败(重试 {attempt + 1}/10): {e}")
            time.sleep(0.12 * (attempt + 1))
    if last_err is not None:
        _LAST_ADVICE_SKIP_REASON = f"quant 库写入失败（可能被主进程占用）: {last_err}"
        print(f"[决策快照] 放弃写入: {last_err}")
        return None

    if dashboard_block is not None:
        try:
            from src.engine.dashboard_metrics_persist import persist_dashboard_v2_to_detail_tables

            persist_dashboard_v2_to_detail_tables(sent, leader, dashboard_block, spot_work)
        except Exception as e:
            print(f"[决策快照] 看板指标回写明细表失败: {e}")

    return payload


def _load_advice_from_disk() -> dict | None:
    """读 latest_advice.json，反向重命名 snake_case → 内部 dict (BR-3.5)

    保 _build_html 完全无感知（继续使用 position / position_short / color / bg）。
    缺失 / 损坏 / 字段不全 → 返回 None，由 caller fallback 到 _calc_daily_advice。
    """
    p = DATA_DIR / "latest_advice.json"
    # 真源在 DuckDB，落库后可能删除 data/latest_advice.json，不得用 exists() 判定
    data = load_json_file(p)
    if not isinstance(data, dict):
        print("[邮件] 决策快照解析失败，回退实时计算")
        return None
    if not _REQUIRED_ADVICE_KEYS.issubset(data.keys()):
        print("[邮件] 决策快照字段不全，回退实时计算")
        return None

    return _advice_dict_from_snapshot_payload(data)


def _advice_dict_from_snapshot_payload(data: dict) -> dict:
    bucket = data["bucket"]
    text = data["text"]
    if bucket in _BUCKET_COLOR:
        color, bg = _BUCKET_COLOR[bucket]
    elif text == _LOADING_TEXT:
        color, bg = _GO_COLOR_LOADING
    else:
        color, bg = _GO_COLOR_NORMAL
    return {
        "bucket": bucket,
        "text": text,
        "position": data["suggested_position"],
        "position_short": data["suggested_position_short"],
        "reason": data["reason"],
        "color": color,
        "bg": bg,
        "conclusion": data.get("conclusion") or text,
        "dashboard": data.get("dashboard"),
    }


def _email_hero_from_advice(advice: dict) -> dict:
    """邮件标题/大字栏：与看板 v2 同源，避免展示旧版「四维警戒」文案。"""
    bucket = advice.get("bucket") or "warn"
    subject_emoji = {"stop": "🛑", "warn": "⚠️", "go": "✅"}.get(bucket, "⚠️")
    dash = advice.get("dashboard")
    if isinstance(dash, dict) and isinstance(dash.get("decision"), dict):
        d = dash["decision"]
        title = str(d.get("headline") or advice.get("text") or "—")
        position = str(d.get("position") or advice.get("position") or "—")
        reason = str(d.get("conclusion") or d.get("tagline") or advice.get("conclusion") or "")
        tone = str(d.get("tone") or bucket)
        if tone == "go":
            subject_action = "可参与"
        elif tone == "stop":
            subject_action = "空仓"
        else:
            subject_action = "低仓位参与"
        color, bg = _BUCKET_COLOR.get(bucket, _GO_COLOR_NORMAL)
        return {
            "title": title,
            "position": position,
            "reason": reason,
            "color": color,
            "bg": bg,
            "subject_emoji": subject_emoji,
            "subject_action": subject_action,
        }
    reason = str(advice.get("reason") or "")
    if "四维警戒" in reason or "四维警戒" in str(advice.get("text") or ""):
        reason = str(advice.get("conclusion") or "")
    subject_action = {"stop": "今日不操作", "warn": "谨慎参与", "go": "可参与"}.get(
        bucket, "谨慎参与",
    )
    return {
        "title": advice.get("text") or "—",
        "position": advice.get("position") or "—",
        "reason": reason,
        "color": advice.get("color", "#6b7280"),
        "bg": advice.get("bg", "#0d1220"),
        "subject_emoji": subject_emoji,
        "subject_action": subject_action,
    }


def _calc_lianban_state(leader: dict | None) -> dict:
    """昨日连板高标整体定性 强/一般/弱"""
    if not leader:
        return {"label": "—", "color": "#6b7280", "icon": "—", "detail": ""}
    mb_list = leader.get("main_board_leaders") or []
    if not mb_list:
        return {"label": "无", "color": "#6b7280", "icon": "—", "detail": "今日无主板连板高标候选"}

    detail = " · ".join(
        f"{x.get('leader_name','')}({x.get('board_count') or x.get('leader_gain_10d')}板) "
        f"{('+' if (x.get('auction_change_pct') or 0) >= 0 else '')}{x.get('auction_change_pct')}%"
        for x in mb_list
    )

    has_ld = any(x.get("signal") == "跌停" for x in mb_list)
    has_neg = any(x.get("signal") == "负反馈" for x in mb_list)
    all_strong = all(x.get("signal") == "强正反馈" for x in mb_list)

    if has_ld:
        return {"label": "弱(跌停)", "color": "#10b981", "icon": "🛑", "detail": detail}
    if has_neg:
        return {"label": "弱", "color": "#10b981", "icon": "🌧️", "detail": detail}
    if all_strong:
        return {"label": "强", "color": "#ef4444", "icon": "🚀", "detail": detail}
    return {"label": "一般", "color": "#60a5fa", "icon": "☁️", "detail": detail}


# ============================================================
# HTML 构建
# ============================================================
_LEVEL_META = {
    "strong": {"icon": "🟢", "color": "#10b981", "bg": "#0f2a1a"},
    "normal": {"icon": "🔵", "color": "#60a5fa", "bg": "#1a2332"},
    "watch":  {"icon": "🟡", "color": "#fbbf24", "bg": "#2a2a1a"},
    "avoid":  {"icon": "🛑", "color": "#ef4444", "bg": "#2a1a1a"},
}


def _render_concept_industry_cell(top_concepts: list, industry: str) -> str:
    """渲染"概念A/概念B (行业)"单元格 — 概念红字、行业灰字"""
    industry = industry or "-"
    if top_concepts:
        return (
            f'<span style="color:#ef4444;font-weight:600;">{"/".join(top_concepts)}</span> '
            f'<span style="color:#6b7280;font-size:11px;">({industry})</span>'
        )
    return f'<span style="color:#a0aec0;">{industry}</span>'


def _fmt_dash_pct(v) -> str:
    """与看板 fmtDashPct 一致：两位小数 + 符号。"""
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    return ("+" if n >= 0 else "") + f"{n:.2f}%"


def _v2_td(label: str, main_html: str, sub_html: str = "", *, width_pct: str = "50%") -> str:
    sub = (
        f'<div style="font-size:10px;color:#6b7280;margin-top:3px;line-height:1.35;">{sub_html}</div>'
        if sub_html else ""
    )
    return (
        f'<td style="background:#f8f9fa;border:1px solid #e5e7eb;padding:8px 10px;'
        f'border-radius:6px;vertical-align:top;width:{width_pct};">'
        f'<div style="font-size:11px;color:#888;">{label}</div>'
        f'<div style="font-size:15px;font-weight:700;margin-top:4px;line-height:1.2;">{main_html}</div>'
        f"{sub}</td>"
    )


def _dashboard_email_compact_metrics_html(participate: dict) -> str:
    """邮件决策卡：仅保留建议仓位旁的两项核心指标（跌停/跌>9%、连板高标竞价）。"""
    c_up = "#ef4444"
    c_dn = "#10b981"
    p = participate or {}

    def _isn(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    ld_mb = p.get("limit_down_main_board")
    drop9 = p.get("drop_over_9pct")
    ld_s = str(int(ld_mb)) if _isn(ld_mb) else "—"
    d9_s = str(int(drop9)) if _isn(drop9) else "—"
    c_ld = c_dn if (_isn(ld_mb) and ld_mb > 5) else ("#60a5fa" if _isn(ld_mb) else "#6b7280")
    c_d9 = c_dn if (_isn(drop9) and drop9 > 9) else ("#60a5fa" if _isn(drop9) else "#6b7280")
    row_ld = (
        f'<span style="color:{c_ld};">{html.escape(ld_s)}</span>'
        f'<span style="color:#4b5563;font-weight:500;"> / </span>'
        f'<span style="color:{c_d9};">{html.escape(d9_s)}</span>'
    )

    spct = p.get("space_board_auction_pct")
    slab = p.get("space_board_label") or "—"
    if not _isn(spct):
        c_sp = "#6b7280"
    elif spct > 0:
        c_sp = c_up
    elif spct < 0:
        c_sp = c_dn
    else:
        c_sp = "#60a5fa"
    sn = p.get("space_board_name")
    sbc = p.get("space_board_board_count")
    sub_parts = []
    if sn:
        sub_parts.append(html.escape(str(sn)))
    if sbc is not None:
        try:
            sub_parts.append(f"{int(sbc)}板")
        except (TypeError, ValueError):
            pass
    row_sp_sub = " · ".join(sub_parts)

    return (
        '<table style="width:100%;border-collapse:separate;border-spacing:6px 0;margin-top:10px;">'
        "<tr>"
        + _v2_td(
            "竞价跌停（主板）(&gt;5⚠) / 跌&gt;9% (&gt;9⚠)",
            row_ld,
            "左：主板跌停；右：跌&gt;9%家数",
        )
        + _v2_td(
            "昨日连板高标竞价",
            f'<span style="color:{c_sp};">{html.escape(str(slab))}</span>',
            row_sp_sub,
        )
        + "</tr></table>"
    )


def _dashboard_v2_metrics_html(participate: dict, reference: dict) -> str:
    """看板 v2 全量指标（测试/回退用）；生产邮件走 _dashboard_email_compact_metrics_html。"""
    # 与看板 gain-pos / gain-neg 一致：涨红 #ef4444，跌绿 #10b981
    c_up = "#ef4444"
    c_dn = "#10b981"
    p = participate or {}
    r = reference or {}

    def _isn(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    ld_mb = p.get("limit_down_main_board")
    drop9 = p.get("drop_over_9pct")
    ld_s = str(int(ld_mb)) if _isn(ld_mb) else "—"
    d9_s = str(int(drop9)) if _isn(drop9) else "—"
    # 与看板 hm-bad 一致：超阈（跌家数多）用绿 #10b981，否则中性蓝
    c_ld = c_dn if (_isn(ld_mb) and ld_mb > 5) else ("#60a5fa" if _isn(ld_mb) else "#6b7280")
    c_d9 = c_dn if (_isn(drop9) and drop9 > 9) else ("#60a5fa" if _isn(drop9) else "#6b7280")
    row1a_main = (
        f'<span style="color:{c_ld};">{html.escape(ld_s)}</span>'
        f'<span style="color:#4b5563;font-weight:500;"> / </span>'
        f'<span style="color:{c_d9};">{html.escape(d9_s)}</span>'
    )
    row1a_sub = "左：主板跌停样本；右：全市场跌&gt;9%家数"

    ri = p.get("relay_decision_index")
    if not _isn(ri):
        c_ri, ri_txt = "#6b7280", "—"
    elif ri >= 0:
        c_ri, ri_txt = c_up, html.escape(_fmt_dash_pct(ri))
    else:
        c_ri, ri_txt = c_dn, html.escape(_fmt_dash_pct(ri))
    rd = p.get("relay_decision_detail") or {}
    rv, rpd = rd.get("verdict"), rd.get("pool_date")
    row1b_sub = ""
    if rv or rpd:
        row1b_sub = html.escape(str(rv or "")) + (
            f" · 池{html.escape(str(rpd))}" if rpd else ""
        )

    b1 = p.get("b1_rate")
    if _isn(b1):
        c_b1 = c_up if b1 >= 15 else "#60a5fa"
        b1_main = f'<span style="color:{c_b1};">{b1:.1f}%</span>'
    else:
        b1_main = '<span style="color:#6b7280;">—</span>'
    row1c_sub = "≥15% 偏强"

    spct = p.get("space_board_auction_pct")
    slab = p.get("space_board_label") or "—"
    if not _isn(spct):
        c_sp = "#6b7280"
    elif spct > 0:
        c_sp = c_up
    elif spct < 0:
        c_sp = c_dn
    else:
        c_sp = "#60a5fa"
    row1d_main = f'<span style="color:{c_sp};">{html.escape(str(slab))}</span>'
    sn = p.get("space_board_name")
    sbc = p.get("space_board_board_count")
    src = p.get("space_board_source")
    row1d_parts = []
    if sn:
        row1d_parts.append(html.escape(str(sn)))
    if sbc is not None:
        try:
            row1d_parts.append(f"{int(sbc)}板")
        except (TypeError, ValueError):
            pass
    row1d_sub = " · ".join(row1d_parts) if row1d_parts else ""
    if src == "market_leader":
        row1d_sub += (
            '<span style="color:#6b7280;"> · 口径：无≥2板主板高标，暂用10日市场高标</span>'
        )
    elif src == "main_board_lianban_relay":
        row1d_sub += (
            '<span style="color:#6b7280;"> · 口径：与复盘 relay 空间板一致</span>'
        )
    if not row1d_sub:
        row1d_sub = "数据来自 latest_leader 竞价字段"

    yld = r.get("yesterday_limit_down_avg")
    c_yld = c_up if (_isn(yld) and yld >= 0) else (c_dn if _isn(yld) else "#6b7280")
    pool_w = r.get("pool_weighted_auction_top30")
    c_pw = c_up if (_isn(pool_w) and pool_w >= 0) else (c_dn if _isn(pool_w) else "#6b7280")
    yzb = r.get("yesterday_zb_avg")
    c_yzb = c_up if (_isn(yzb) and yzb >= 0) else (c_dn if _isn(yzb) else "#6b7280")

    row1 = (
        "<tr>"
        + _v2_td(
            "竞价跌停（主板）(&gt;5⚠) / 跌&gt;9% (&gt;9⚠)",
            row1a_main,
            row1a_sub,
        )
        + _v2_td("加权接力情绪指数", f'<span style="color:{c_ri};">{ri_txt}</span>', row1b_sub)
        + _v2_td("1进2成功率", b1_main, row1c_sub)
        + _v2_td("昨日连板高标竞价", row1d_main, row1d_sub)
        + "</tr>"
    )
    row2 = (
        "<tr>"
        + _v2_td(
            "昨日跌停平均反馈",
            f'<span style="color:{c_yld};">{html.escape(_fmt_dash_pct(yld))}</span>',
        )
        + _v2_td(
            "梯队加权竞价(10日)",
            f'<span style="color:{c_pw};">{html.escape(_fmt_dash_pct(pool_w))}</span>',
        )
        + _v2_td(
            "昨日炸板平均反馈",
            f'<span style="color:{c_yzb};">{html.escape(_fmt_dash_pct(yzb))}</span>',
        )
        + "<td></td>"
        + "</tr>"
    )
    return (
        '<table style="width:100%;border-collapse:separate;border-spacing:6px 6px;margin-top:10px;">'
        f"{row1}{row2}</table>"
    )


def _render_per_stock_decision_email(hit: dict, advice: dict) -> str:
    """与看板决策列一致：per_stock_decision 优先，否则按全局 bucket 回退。

    空仓时额外展示 reason（看板 decision 列 title / 空仓原因同源）。
    """
    psd = hit.get("per_stock_decision") or {}
    action = psd.get("action")
    if action == "开仓":
        label = "✅ 开仓"
        border, bg = "#10b981", "#0f2a1a"
        is_empty = False
    elif action == "观察":
        label = "👁 观察"
        border, bg = "#fbbf24", "#2a2a1a"
        is_empty = False
    elif action:
        label = "⛔ 空仓"
        border, bg = "#ef4444", "#2a1a1a"
        is_empty = True
    else:
        label = html.escape(str(advice.get("text") or "—"))
        border, bg = "#6b7280", "#1f2937"
        line2 = ""
        b = advice.get("bucket")
        mc = bool(hit.get("matched_cycle"))
        if b == "go":
            line2 = "3层（🎯周期股）" if mc else "2层（非周期降1层）"
        elif b == "warn":
            line2 = "1.5层（谨慎）"
        else:
            line2 = "0层"
        return (
            f'<div style="display:block;padding:6px 10px;border-radius:6px;'
            f'border-left:3px solid {border};background:{bg};">'
            f'<div style="font-size:12px;font-weight:600;color:#e0e6ed;">{label}</div>'
            f'<div style="font-size:11px;margin-top:3px;color:#a0aec0;">{html.escape(line2)}</div>'
            f"</div>"
        )

    pos = html.escape(str(psd.get("position_text") or ""))
    lad = psd.get("ladder_label")
    lad_html = (
        f' <span style="color:#60a5fa;">{html.escape(str(lad))}</span>' if lad else ""
    )
    reason_html = ""
    if is_empty:
        reason = psd.get("reason") or psd.get("veto_reason") or ""
        if reason:
            reason_html = (
                f'<div style="font-size:11px;margin-top:4px;color:#fca5a5;line-height:1.4;">'
                f'空仓原因：{html.escape(str(reason))}</div>'
            )
    return (
        f'<div style="display:block;padding:6px 10px;border-radius:6px;'
        f'border-left:3px solid {border};background:{bg};">'
        f'<div style="font-size:12px;font-weight:600;color:#e0e6ed;">{label}</div>'
        f'<div style="font-size:11px;margin-top:3px;color:#a0aec0;">{pos}{lad_html}</div>'
        f"{reason_html}"
        f"</div>"
    )


_SELL_TONE_COLOR = {"sell": "#10b981", "hold": "#ef4444", "partial": "#fbbf24"}


def _load_yesterday_selections_for_email() -> tuple[str, list[dict]]:
    """与看板 yesterdaySelections 同源，但邮件过滤掉昨日本不开仓（0层）标的。"""
    try:
        from src.engine.screener_history import _load, _record_date_str, yesterday_pick_date
    except Exception as e:
        print(f"[邮件] 加载昨日选股失败: {e}")
        return "", []
    today = now_cn().strftime("%Y-%m-%d")
    ysd = yesterday_pick_date(today)
    if not ysd:
        return "", []
    rows = [r for r in _load() if _record_date_str(r) == ysd]
    rows = [r for r in rows if _hit_can_open(r)]
    return ysd, rows


def _fmt_email_pct(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    return ("+" if n >= 0 else "") + f"{n:.2f}%"


def _render_yesterday_selections_email(date: str, rows: list[dict]) -> str:
    """昨日选股 + 今日决策（仅昨日有命中时调用）。"""
    if not rows:
        return ""
    cards = []
    for r in rows:
        code = html.escape(str(r.get("code") or ""))
        name = html.escape(str(r.get("name") or ""))
        lb = r.get("continuous_limit_up")
        lb_txt = f"{lb}板" if lb not in (None, "") else "—"
        y_auc = _fmt_email_pct(r.get("auction_gain"))
        y_chg = _fmt_email_pct(r.get("day_change"))
        t_auc = r.get("next_day_auction_gain")
        t_auc_txt = _fmt_email_pct(t_auc)
        try:
            t_auc_f = float(t_auc) if t_auc is not None else None
        except (TypeError, ValueError):
            t_auc_f = None
        if t_auc_f is None:
            t_color = "#6b7280"
        elif t_auc_f >= 0:
            t_color = "#ef4444"
        else:
            t_color = "#10b981"

        sell = r.get("next_day_sell_advice") if isinstance(r.get("next_day_sell_advice"), dict) else None
        if sell and sell.get("summary"):
            tone_c = _SELL_TONE_COLOR.get(str(sell.get("tone") or ""), "#a0aec0")
            lad = sell.get("ladder_label")
            lad_html = (
                f' <span style="color:#8892a8;">{html.escape(str(lad))}</span>' if lad else ""
            )
            decision_block = (
                f'<div style="margin-top:6px;padding:6px 10px;border-radius:6px;'
                f'border-left:3px solid {tone_c};background:#0a0e14;">'
                f'<div style="font-size:12px;font-weight:600;color:#60a5fa;">今日决策{lad_html}</div>'
                f'<div style="font-size:11px;margin-top:3px;color:{tone_c};line-height:1.4;">'
                f'{html.escape(str(sell["summary"]))}</div></div>'
            )
        elif t_auc_f is not None:
            decision_block = (
                '<div style="margin-top:6px;font-size:11px;color:#6b7280;">'
                "今日决策生成中</div>"
            )
        else:
            decision_block = (
                '<div style="margin-top:6px;font-size:11px;color:#6b7280;">'
                "等待今日竞价回填</div>"
            )

        cards.append(f"""
            <div style="padding:10px 0;border-bottom:1px solid #1e2a45;">
              <div style="font-size:14px;font-weight:600;color:#e0e6ed;line-height:1.35;">
                <span style="color:#8892a8;font-weight:500;">{code}</span>
                {name}
                <span style="color:#fbbf24;font-size:12px;margin-left:6px;">{lb_txt}</span>
              </div>
              <div style="font-size:12px;color:#9ca3af;margin-top:4px;line-height:1.45;">
                昨竞价 {html.escape(y_auc)} · 昨涨幅 {html.escape(y_chg)}
                · 今竞价 <b style="color:{t_color};">{html.escape(t_auc_txt)}</b>
              </div>
              {decision_block}
            </div>
        """)

    return f"""
      <h3 style="margin:20px 0 8px 0;color:#e0e6ed;font-size:14px;
                 border-bottom:1px solid #1e2a45;padding-bottom:6px;">
        ⏪ 昨日选股今日竞价 · {html.escape(date)} · {len(rows)} 只
      </h3>
      <div style="font-size:12px;background:#111827;border-radius:8px;padding:4px 12px 8px;">
        {''.join(cards)}
      </div>
    """


def _email_footer_metrics_html(advice: dict) -> str:
    """脚注：加权接力 + 1进2（看板参与指标；替代无数据的周期阶段行）。"""
    dash = advice.get("dashboard") if isinstance(advice.get("dashboard"), dict) else {}
    p = dash.get("participate") if isinstance(dash.get("participate"), dict) else {}

    def _isn(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    ri = p.get("relay_decision_index")
    if _isn(ri):
        c_ri = "#ef4444" if ri >= 0 else "#10b981"
        ri_txt = f'<span style="color:{c_ri};">{html.escape(_fmt_dash_pct(ri))}</span>'
    else:
        ri_txt = '<span style="color:#6b7280;">—</span>'

    b1 = p.get("b1_rate")
    if _isn(b1):
        c_b1 = "#ef4444" if b1 >= 15 else "#60a5fa"
        b1_txt = f'<span style="color:{c_b1};">{b1:.1f}%</span>'
    else:
        b1_txt = '<span style="color:#6b7280;">—</span>'

    return (
        f'<p style="color:#6b7280;font-size:11px;margin-top:20px;text-align:center;">'
        f'加权接力情绪 {ri_txt}'
        f'<span style="color:#4b5563;"> · </span>'
        f'1进2成功率 {b1_txt}'
        f"</p>"
    )


def _build_html(
    cycle_phase: str, cycle_day: int,
    leader: dict | None, hits: list[dict], signals: list[dict],
    sentiment_data: dict | None, ranking_data: dict | None,
    advice: dict,
) -> str:
    now = now_cn().strftime("%Y-%m-%d %H:%M:%S")

    dash = advice.get("dashboard")
    use_v2 = (
        isinstance(dash, dict)
        and isinstance(dash.get("participate"), dict)
        and isinstance(dash.get("reference"), dict)
    )

    if use_v2:
        metrics_html = _dashboard_email_compact_metrics_html(dash["participate"])
    else:
        # === 6 指标格数据（看板 v-else hero-metrics 同源） ===
        market = (sentiment_data or {}).get("market") or {}
        limit_down = market.get("limit_down")
        drop_over_9pct = market.get("drop_over_9pct")
        prev_day_limit_down = market.get("prev_day_limit_down")
        w_avg = (sentiment_data or {}).get("weighted_auction_gain")
        y_avg = ((leader or {}).get("yesterday_main_board_avg_auction") or {})
        y_zb = ((leader or {}).get("yesterday_zb_today_auction") or {})
        y_ld = ((leader or {}).get("yesterday_limit_down_today_auction") or {})
        lianban = _calc_lianban_state(leader)

        def _is_num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        def num_color(v, threshold_high_is_bad=False):
            if v is None or v == "—":
                return "#6b7280"
            try:
                v = float(v)
            except (ValueError, TypeError):
                return "#6b7280"
            if threshold_high_is_bad:
                return "#10b981" if v > 5 else "#60a5fa"
            return "#ef4444" if v >= 0 else "#10b981"

        def fmt_pct(v):
            if v is None:
                return "—"
            return f"{'+' if v >= 0 else ''}{v}%"

        def _metric_cell(label, value, color, sub=""):
            sub_html = f'<div style="font-size:10px;color:#999;margin-top:1px;">{sub}</div>' if sub else ''
            return (
                f'<td style="background:#f8f9fa;border:1px solid #e5e7eb;padding:6px 10px;border-radius:6px;width:33%;vertical-align:top;">'
                f'<div style="font-size:11px;color:#888;">{label}</div>'
                f'<div style="font-size:16px;font-weight:700;color:{color};margin-top:2px;">{value}</div>'
                f'{sub_html}</td>'
            )

        ld_disp = limit_down if _is_num(limit_down) else "—"
        drop_disp = drop_over_9pct if _is_num(drop_over_9pct) else "—"
        ld_color = "#10b981" if (_is_num(limit_down) and limit_down > 5) else (
            "#60a5fa" if _is_num(limit_down) else "#6b7280"
        )
        drop_color = "#10b981" if (_is_num(drop_over_9pct) and drop_over_9pct > 9) else (
            "#60a5fa" if _is_num(drop_over_9pct) else "#6b7280"
        )

        arrow_html = ""
        if _is_num(limit_down) and _is_num(prev_day_limit_down):
            if limit_down > prev_day_limit_down:
                arrow, ar_color = "↑", "#10b981"
            elif limit_down < prev_day_limit_down:
                arrow, ar_color = "↓", "#ef4444"
            else:
                arrow, ar_color = "→", "#6b7280"
            arrow_html = (
                f'<span style="font-size:13px;color:{ar_color};margin-left:6px;">{arrow}</span>'
            )

        if _is_num(prev_day_limit_down):
            if _is_num(limit_down):
                diff = limit_down - prev_day_limit_down
                diff_str = ("+" if diff > 0 else "") + str(diff)
                ld_sub_html = (
                    f'<div style="font-size:10px;color:#999;margin-top:1px;">'
                    f'昨日跌停 {prev_day_limit_down}（差值{diff_str}）</div>'
                )
            else:
                ld_sub_html = (
                    f'<div style="font-size:10px;color:#999;margin-top:1px;">'
                    f'昨日跌停 {prev_day_limit_down}</div>'
                )
        else:
            ld_sub_html = (
                '<div style="font-size:10px;color:#999;margin-top:1px;">昨日跌停 —</div>'
            )

        if (_is_num(limit_down) and limit_down > 5) or (_is_num(drop_over_9pct) and drop_over_9pct > 9):
            main1_color = "#10b981"
        elif _is_num(limit_down) or _is_num(drop_over_9pct):
            main1_color = "#60a5fa"
        else:
            main1_color = "#6b7280"

        cell1_html = (
            '<td style="background:#f8f9fa;border:1px solid #e5e7eb;padding:6px 10px;'
            'border-radius:6px;width:33%;vertical-align:top;">'
            '<div style="font-size:11px;color:#888;">竞价跌停 (&gt;5⚠) / 跌&gt;9% (&gt;9⚠)</div>'
            f'<div style="font-size:16px;font-weight:700;color:{main1_color};margin-top:2px;">'
            f'{ld_disp} / {drop_disp}{arrow_html}</div>'
            f'{ld_sub_html}</td>'
        )

        sample = y_avg.get("sample_count")
        avg_chg = y_avg.get("avg_change_pct")
        pos_cnt = y_avg.get("positive_count")
        neg_cnt = y_avg.get("negative_count")
        ld_cnt = y_avg.get("limit_down_count")
        median_chg = y_avg.get("median_change_pct")
        high5 = y_avg.get("high5_count")
        flat2 = y_avg.get("flat2_count")
        low5 = y_avg.get("low5_count")

        render_sub = _is_num(median_chg)
        sub4_str = ""
        if render_sub:
            median_str = f"{'+' if median_chg >= 0 else ''}{median_chg}%"
            sub4_str = (
                f"中位数 {median_str} · "
                f"高开>5%:{high5 if _is_num(high5) else '—'} · "
                f"平开±2%:{flat2 if _is_num(flat2) else '—'} · "
                f"低开<-5%:{low5 if _is_num(low5) else '—'}"
            )

        if _is_num(sample) and sample > 0:
            title4 = (
                f"昨日涨停 {sample} 只 · "
                f"高开 {pos_cnt if _is_num(pos_cnt) else '—'} / "
                f"低开 {neg_cnt if _is_num(neg_cnt) else '—'}"
            )
            if _is_num(ld_cnt) and ld_cnt > 0:
                title4 += f" / 跌停 {ld_cnt}"
            main4 = fmt_pct(avg_chg) if _is_num(avg_chg) else "—"
            main4_color = num_color(avg_chg) if _is_num(avg_chg) else "#6b7280"
        else:
            title4 = "昨日涨停 — 只"
            main4 = "—"
            main4_color = "#6b7280"

        sub_div = (
            f'<div style="font-size:10px;color:#999;margin-top:1px;">{sub4_str}</div>'
            if render_sub else ''
        )
        cell4_html = (
            '<td style="background:#f8f9fa;border:1px solid #e5e7eb;padding:6px 10px;'
            'border-radius:6px;width:33%;vertical-align:top;">'
            '<div style="font-size:11px;color:#888;">接力情绪</div>'
            f'<div style="font-size:10px;color:#6b7280;margin-top:1px;">{title4}</div>'
            f'<div style="font-size:16px;font-weight:700;color:{main4_color};margin-top:2px;">{main4}</div>'
            f'{sub_div}'
            '</td>'
        )

        lb_val = lianban['label']
        lb_sub = lianban['detail'][:30] + ('…' if len(lianban['detail']) > 30 else '')

        metrics_html = f"""
    <table style="width:100%;border-collapse:separate;border-spacing:6px 4px;margin-top:8px;">
      <tr>
        {cell1_html}
        {_metric_cell('梯队加权竞价', fmt_pct(w_avg), num_color(w_avg))}
        {_metric_cell('连板高标竞价', lb_val, lianban['color'], lb_sub)}
      </tr>
      <tr>
        {cell4_html}
        {_metric_cell('昨日炸板今日', fmt_pct(y_zb.get('avg_change_pct')), num_color(y_zb.get('avg_change_pct')),
                       f"{y_zb.get('sample_count','')}只" if y_zb.get('sample_count') else '')}
        {_metric_cell('昨日跌停平均反馈', fmt_pct(y_ld.get('avg_change_pct')), num_color(y_ld.get('avg_change_pct')),
                       f"{y_ld.get('sample_count','')}只" if y_ld.get('sample_count') else '')}
      </tr>
    </table>
    """

    hero = _email_hero_from_advice(advice)

    # === 触发原因（单行，省纵向空间） ===
    reason_html = ""
    if hero.get("reason"):
        reason_html = (
            '<div style="margin-top:8px;font-size:12px;color:#9ca3af;line-height:1.4;">'
            f"💡 {html.escape(str(hero['reason']))}</div>"
        )

    # === 决策大字栏（手机一屏：结论 + 2 项核心指标；不含盘面全局建议仓位） ===
    hero_html = f"""
    <div style="padding:14px 16px;border:2px solid {hero['color']};border-radius:10px;
                background:linear-gradient(135deg, {hero['bg']} 0%, #0d1220 70%);margin-bottom:12px;">
      <div style="font-size:22px;font-weight:800;color:{hero['color']};line-height:1.15;">
        {html.escape(str(hero['title']))}
      </div>
      {metrics_html}
      {reason_html}
    </div>
    """

    # === 选股表（精简列：代码/名称/价格/竞价%/市值/板块/连板/10日%/决策） ===
    if hits:
        # 板块查表（ranking_data + industry_cache 兜底）
        industry_map = {}
        try:
            ic = DATA_DIR / "industry_cache.json"
            raw = load_json_file(ic)
            if isinstance(raw, dict):
                industry_map = raw
        except Exception:
            pass
        if ranking_data:
            for r in (ranking_data.get("ranking") or []):
                code = str(r.get("code", "")).strip().zfill(6)
                if len(code) == 6 and code.isdigit() and r.get("industry"):
                    industry_map[code] = r["industry"]

        # 概念 top_concepts：优先 ranking 已注入字段，缺失则按全市场涨停聚合补齐
        top_concepts_map: dict[str, list[str]] = {}
        if ranking_data:
            for r in (ranking_data.get("ranking") or []):
                code = str(r.get("code", "")).strip().zfill(6)
                if len(code) == 6 and code.isdigit() and r.get("top_concepts"):
                    top_concepts_map[code] = list(r["top_concepts"])
        try:
            from src.data.concept_fetcher import load_stock_to_concepts
            from src.engine.concept_stats import (
                aggregate_concept_limit_ups, top_concepts_for_stock,
            )
            c_map = load_stock_to_concepts() or {}
            heats = []
            try:
                lu_file = DATA_DIR / "limit_up_cache.json"
                lu = load_json_file(lu_file) or {}
                if lu:
                    latest = sorted(lu.keys())[-1]
                    heats = aggregate_concept_limit_ups(lu.get(latest, []) or [], c_map)
            except Exception:
                pass
            for h in hits:
                code = str(h.get("code", "")).strip().zfill(6)
                if len(code) == 6 and code.isdigit() and code not in top_concepts_map and code in c_map:
                    top_concepts_map[code] = top_concepts_for_stock(
                        list(c_map.get(code) or []), heats, top_n=2,
                    )
        except Exception:
            pass

        # 10日涨幅兜底（不在排行中的标的用K线计算）
        gain_10d_map = {}
        if ranking_data:
            for r in (ranking_data.get("ranking") or []):
                code = str(r.get("code", "")).strip().zfill(6)
                if len(code) == 6 and code.isdigit():
                    gain_10d_map[code] = r.get("gain_10d")
        hit_codes_missing_10d = []
        for h in hits:
            c6 = str(h.get("code", "")).strip().zfill(6)
            if len(c6) == 6 and c6.isdigit() and c6 not in gain_10d_map and not h.get("gain_10d"):
                hit_codes_missing_10d.append(c6)
        if hit_codes_missing_10d:
            try:
                from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
                for c in hit_codes_missing_10d:
                    df = fetch_kline(c, SCALE_DAILY, datalen=12)
                    if df is not None and len(df) >= 2:
                        close_now = float(df.iloc[-1]["close"])
                        idx = max(0, len(df) - 11)
                        base = float(df.iloc[idx]["close"])
                        if base > 0:
                            gain_10d_map[c] = round((close_now / base - 1) * 100, 2)
            except Exception:
                pass

        rank_codes: set[str] = set()
        if ranking_data:
            for r in (ranking_data.get("ranking") or []):
                c0 = str(r.get("code", "")).strip().zfill(6)
                if len(c0) == 6 and c0.isdigit():
                    rank_codes.add(c0)

        rows = []
        for h in hits:
            code = str(h.get("code", "")).strip().zfill(6)
            cycle_tag = (
                ' <span style="background:#1e3a5f;color:#60a5fa;padding:1px 5px;border-radius:3px;font-size:11px;">🎯</span>'
                if h.get("matched_cycle") else ""
            )
            at = h.get("auction_turnover")
            try:
                at_f = float(at) if at is not None else None
            except (TypeError, ValueError):
                at_f = None
            at_disp = "—" if at_f is None else f"{at_f:.2f}%"

            decision_html = _render_per_stock_decision_email(h, advice)
            is_cyc = code in rank_codes
            cyc_badge = (
                ' <span style="color:#60a5fa;font-size:11px;">🎯周期</span>'
                if is_cyc else ""
            )
            sanban_badge = (
                '<span style="background:#7f1d1d;color:#fca5a5;padding:1px 4px;border-radius:3px;'
                'font-size:10px;margin-left:4px;">⚠三板组</span>'
                if h.get("sanbanzhu") else ""
            )

            gain_10d = h.get("gain_10d") or gain_10d_map.get(code)
            gain_10d_html = '—' if gain_10d is None else (
                f'<span style="color:{"#ef4444" if gain_10d >= 0 else "#10b981"};">'
                f'{("+" if gain_10d > 0 else "")}{gain_10d}%</span>'
            )
            concept_html = _render_concept_industry_cell(
                top_concepts_map.get(code) or [], industry_map.get(code, "-"),
            )
            lb = h.get("continuous_limit_up")
            lb_txt = f"{lb}板" if lb not in (None, "") else "—"

            rows.append(f"""
            <div style="padding:10px 0;border-bottom:1px solid #1e2a45;">
              <div style="font-size:14px;font-weight:600;color:#e0e6ed;line-height:1.35;">
                <span style="color:#8892a8;font-weight:500;">{code}</span>
                {html.escape(str(h.get('name', '')))}{cyc_badge}{sanban_badge}
              </div>
              <div style="font-size:12px;color:#9ca3af;margin-top:4px;line-height:1.45;">
                竞价 <b style="color:#ef4444;">{h.get('auction_gain', '—')}%</b>
                · 开 {h.get('open_price', '—')}
                · {lb_txt}
                · 10日 {gain_10d_html}
                · {h.get('market_cap', '—')}亿
                · 换手 {at_disp}
              </div>
              <div style="font-size:12px;margin-top:3px;">{concept_html}</div>
              <div style="margin-top:6px;">{decision_html}</div>
            </div>
            """)

        hits_html = f"""
        <div style="font-size:12px;background:#111827;border-radius:8px;padding:4px 12px 8px;">
          {''.join(rows)}
        </div>
        """
    else:
        hits_html = '<p style="color:#9ca3af;text-align:center;padding:40px;">无命中标的</p>'

    # 昨日选股（仅昨日有命中时展示；数据须在发信前完成竞价/卖出建议回填）
    ysd, y_rows = _load_yesterday_selections_for_email()
    yesterday_html = _render_yesterday_selections_email(ysd, y_rows) if y_rows else ""
    footer_html = _email_footer_metrics_html(advice)
    # cycle_phase / cycle_day 保留入参兼容旧调用方，脚注已改用看板核心指标
    _ = (cycle_phase, cycle_day)

    # === 整体页面 ===
    return f"""
    <div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
                max-width:780px;margin:0 auto;padding:24px;
                background:#0a0e1a;color:#e0e6ed;">
      <h2 style="margin:0 0 4px 0;font-size:18px;color:#e0e6ed;">
        AI量化 · 9:27 决策推送
      </h2>
      <p style="color:#6b7280;font-size:12px;margin:0 0 12px 0;">{now}</p>

      {hero_html}

      <h3 style="margin:20px 0 8px 0;color:#e0e6ed;font-size:14px;
                 border-bottom:1px solid #1e2a45;padding-bottom:6px;">
        🔍 今日选股 · {len(hits)} 只
      </h3>
      {hits_html}

      {yesterday_html}

      {footer_html}
    </div>
    """


def _build_review_html(review: dict) -> str:
    """复盘邮件 HTML：市场广度 + 评分卡（无达标标准列）+ 明日决策。

    手机端优先：禁用 flex；广度用 table；评分卡竖排卡片（避免四列挤扁）。
    """
    now = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    date = html.escape(str(review.get("date") or "")[:10])
    sc = review.get("scorecard") or {}
    indicators = sc.get("indicators") or []
    total = sc.get("total_score")
    decision = html.escape(str(sc.get("decision") or "—"))
    dec_color = html.escape(str(sc.get("decision_color") or "#6b7280"))
    total_s = html.escape(f"{total}/6" if total is not None else "—/6")

    def _num(v, default="—"):
        if v is None or v == "":
            return default
        return html.escape(str(v))

    # 市场广度（table 布局，兼容 QQ/手机邮件客户端）
    mb = review.get("market_breadth") or {}
    breadth_html = ""
    if mb and (mb.get("sh_close") is not None or mb.get("advance") is not None):
        sh_close = _num(mb.get("sh_close"))
        sh_pct = mb.get("sh_pct")
        try:
            sh_pct_f = float(sh_pct) if sh_pct is not None else None
        except (TypeError, ValueError):
            sh_pct_f = None
        sh_pct_txt = html.escape(_fmt_email_pct(sh_pct)) if sh_pct_f is not None else ""
        sh_color = (
            "#ef4444" if sh_pct_f is not None and sh_pct_f >= 0
            else ("#10b981" if sh_pct_f is not None else "#6b7280")
        )
        adv_raw = mb.get("advance")
        dec_raw = mb.get("decline")
        adv = _num(adv_raw)
        dec = _num(dec_raw)
        flat = _num(mb.get("flat")) if mb.get("flat") is not None and not mb.get("counts_unavailable") else "—"
        ratio_txt = "—"
        ratio_color = "#6b7280"
        try:
            a = float(adv_raw)
            d = float(dec_raw)
            if d > 0:
                ratio_txt = f"{a / d:.2f}"
                ratio_color = "#ef4444" if a > d else "#10b981"
        except (TypeError, ValueError):
            pass
        breadth_html = f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:#111827;border-radius:8px;margin:0 0 14px 0;border-collapse:separate;">
        <tr>
          <td style="padding:12px 10px;width:50%;vertical-align:top;">
            <div style="color:#6b7280;font-size:11px;line-height:1.2;">上证指数</div>
            <div style="font-weight:700;color:{sh_color};font-size:16px;margin-top:4px;line-height:1.3;">
              {sh_close}
              <span style="font-size:13px;font-weight:600;">{sh_pct_txt}</span>
            </div>
          </td>
          <td style="padding:12px 10px;width:50%;vertical-align:top;text-align:right;">
            <div style="color:#6b7280;font-size:11px;line-height:1.2;">涨跌比</div>
            <div style="font-weight:700;color:{ratio_color};font-size:16px;margin-top:4px;">{ratio_txt}</div>
          </td>
        </tr>
        <tr>
          <td colspan="2" style="padding:0 10px 12px 10px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="width:33%;vertical-align:top;">
                  <div style="color:#6b7280;font-size:11px;">上涨家数</div>
                  <div style="font-weight:700;color:#ef4444;font-size:15px;margin-top:2px;">{adv}</div>
                </td>
                <td style="width:34%;vertical-align:top;text-align:center;">
                  <div style="color:#6b7280;font-size:11px;">下跌家数</div>
                  <div style="font-weight:700;color:#10b981;font-size:15px;margin-top:2px;">{dec}</div>
                </td>
                <td style="width:33%;vertical-align:top;text-align:right;">
                  <div style="color:#6b7280;font-size:11px;">平盘</div>
                  <div style="font-weight:700;color:#6b7280;font-size:15px;margin-top:2px;">{flat}</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
        """

    # 评分卡：竖排行（指标 + 得分徽章 / 今日数据 / 说明），手机一屏可读
    rows_html = []
    for ind in indicators:
        if not isinstance(ind, dict):
            continue
        label = html.escape(str(ind.get("label") or ""))
        today = html.escape(str(ind.get("today") or "—"))
        score = ind.get("score")
        try:
            score_i = int(score) if score is not None else 0
        except (TypeError, ValueError):
            score_i = 0
        detail = html.escape(str(ind.get("detail") or ""))
        val_color = "#ef4444" if score_i == 1 else "#10b981"
        score_bg = "#7f1d1d" if score_i == 1 else "#1f2937"
        score_fg = "#fca5a5" if score_i == 1 else "#9ca3af"
        detail_html = (
            f'<div style="font-size:11px;color:#6b7280;margin-top:4px;line-height:1.4;">{detail}</div>'
            if detail else ""
        )
        rows_html.append(f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #1e2a45;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="vertical-align:middle;">
                  <span style="font-size:13px;font-weight:700;color:#e0e6ed;">{label}</span>
                </td>
                <td style="vertical-align:middle;text-align:right;width:44px;">
                  <span style="display:inline-block;min-width:22px;padding:2px 7px;border-radius:4px;
                               background:{score_bg};color:{score_fg};font-size:12px;font-weight:700;
                               text-align:center;">{score_i}</span>
                </td>
              </tr>
            </table>
            <div style="font-size:15px;font-weight:700;color:{val_color};margin-top:4px;line-height:1.35;">
              {today}
            </div>
            {detail_html}
          </td>
        </tr>
        """)

    ta = sc.get("today_action") if isinstance(sc.get("today_action"), dict) else {}
    action_html = ""
    if ta:
        verdict = html.escape(str(ta.get("verdict") or decision))
        position = html.escape(str(ta.get("position") or "—"))
        ladders = html.escape(str(ta.get("ladders") or "—"))
        note = html.escape(str(ta.get("note") or ""))
        note_html = (
            f'<div style="margin-top:6px;font-size:12px;color:#a0aec0;line-height:1.45;">{note}</div>'
            if note else ""
        )
        action_html = f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="margin-top:14px;border:1px solid #2563eb;border-radius:8px;background:#0d1528;">
        <tr>
          <td style="padding:12px 14px;">
            <div style="margin-bottom:8px;">
              <span style="display:inline-block;padding:5px 10px;border-radius:6px;
                           background:{dec_color};color:#fff;font-weight:700;font-size:13px;">
                🎯 明日决策：{verdict}
              </span>
              <span style="color:#8892a8;font-size:11px;margin-left:6px;">（据今日复盘）</span>
            </div>
            <div style="font-size:13px;color:#e0e6ed;line-height:1.55;">
              总分 <b>{total_s}</b> → 明日仓位
              <b style="color:#fbbf24;">{position}</b>
            </div>
            <div style="font-size:13px;color:#e0e6ed;line-height:1.55;margin-top:2px;">
              推荐梯队 <b style="color:#60a5fa;">{ladders}</b>
            </div>
            {note_html}
          </td>
        </tr>
      </table>
        """

    return f"""
    <div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
                max-width:640px;margin:0 auto;padding:16px 12px;
                background:#0a0e1a;color:#e0e6ed;">
      <h2 style="margin:0 0 4px 0;font-size:18px;color:#e0e6ed;line-height:1.3;">
        AI量化 · 今日复盘
      </h2>
      <p style="color:#6b7280;font-size:12px;margin:0 0 14px 0;">{now} · {date}</p>

      {breadth_html}

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:#111827;border-radius:8px;border-collapse:separate;">
        <tr>
          <td style="padding:14px 12px 6px 12px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="vertical-align:middle;">
                  <span style="font-size:14px;font-weight:700;color:#e0e6ed;">🎯 接力环境评分卡</span>
                </td>
                <td style="vertical-align:middle;text-align:right;">
                  <span style="display:inline-block;background:{dec_color};color:#fff;
                               padding:3px 8px;border-radius:4px;font-size:12px;font-weight:700;">
                    {total_s} · {decision}
                  </span>
                </td>
              </tr>
            </table>
            <div style="font-size:11px;color:#6b7280;margin-top:6px;">{date}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:0 12px 8px 12px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              {''.join(rows_html)}
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:0 12px 14px 12px;">
            {action_html}
          </td>
        </tr>
      </table>
    </div>
    """


def _send(subject: str, html: str) -> bool:
    recipients = notify_recipients()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
        print(f"[邮件] 推送成功 → {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"[邮件] 推送失败: {e}")
        return False

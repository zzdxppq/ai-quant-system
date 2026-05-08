"""邮件推送模块

9:27 选股完成后，按看板新版布局推送决策邮件：
- 顶部决策大字栏（当日操作建议 + 建议仓位）
- 6 指标格（竞价跌停+跌>9% / 梯队加权竞价 / 连板高标竞价 / 接力情绪 / 昨日炸板今日 / 昨日跌停平均反馈）
- 触发原因
- 今日选股精简表（代码/名称/价格/竞价/市值/板块/连板/10日%/决策）
- 当前周期阶段（小字脚注）

不包含（避免阅读量）：
- 周期代表股 / 龙头反馈 / 昨日涨停股 详情
- 交叉验证信号详情
- 四维评分卡 / 回测统计

真源约束：所有决策算法与指标格内容以 src/static/index.html 的 dailyAdvice
（1196-1268 行）+ hero-metrics（505-600 行）为唯一真源；任何分歧由 Python 端
对齐 JS，不得反向。
"""
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from src.config import DATA_DIR, now_cn


# QQ邮箱 SMTP 配置
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_TO = os.getenv("NOTIFY_TO", "604491810@qq.com")


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
) -> bool:
    """发送选股报告邮件"""
    if not SMTP_USER or not SMTP_PASSWORD:
        print("[邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送")
        return False

    advice = _load_advice_from_disk() or _calc_daily_advice(sentiment_data, leader)

    now = now_cn()
    subject_emoji = {"stop": "🛑", "warn": "⚠️", "go": "✅"}[advice["bucket"]]
    subject_action = {"stop": "今日不操作", "warn": "谨慎参与", "go": "可参与"}[advice["bucket"]]
    subject = (
        f"【{now.strftime('%m/%d')}选股】{subject_emoji}{subject_action}"
        f" · {len(hits)}只命中 · 仓位{advice['position_short']}"
    )

    html = _build_html(
        cycle_phase, cycle_day, leader, hits, signals,
        sentiment_data, ranking_data, advice,
    )
    return _send(subject, html)


# ============================================================
# 决策计算 — 与前端 dailyAdvice 保持同一逻辑
# ============================================================
def _calc_daily_advice(sent: dict | None, leader: dict | None = None) -> dict:
    """四维警戒 → {bucket, text, position, position_short, reason, color, bg}

    维度: 竞价跌停>5 / 跌幅>9% 个股数>9 / 加权竞价<0 / 连板高标(任一跌停或水下开)
    任二触发 → 不操作；单触发 → 谨慎；都未触发 → 可参与
    （连续 2 日"跌停≤5 + 加权竞价≥0" → 升 4 层）
    与 src/static/index.html:1196-1268 dailyAdvice 完全等价。
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


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def write_advice_snapshot(sent: dict | None, leader: dict | None = None) -> dict | None:
    """计算 9:27 决策快照并写入 DATA_DIR/latest_advice.json (单一真源)

    与 src/static/index.html dailyAdvice computed 等价；不引入新算法。
    bucket / text / position / position_short / reason 由 _calc_daily_advice 给出，
    本函数另独立计算 dimensions/bad_count/inputs（持久化扩展字段，BR-1.6）。

    Returns: 写入的 payload dict；写盘失败返回 None（不抛错给 caller）。
    """
    advice = _calc_daily_advice(sent, leader)

    sent = sent or {}
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
    bad_count = int(ld_bad) + int(drop_bad) + int(w_bad) + int(lb_bad)

    summary = [
        {
            "leader_name": x.get("leader_name", ""),
            "signal": x.get("signal", ""),
            "auction_change_pct": x.get("auction_change_pct"),
        }
        for x in mb_list
    ]

    payload = {
        "generated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "bucket": advice["bucket"],
        "text": advice["text"],
        "suggested_position": advice["position"],
        "suggested_position_short": advice["position_short"],
        "reason": advice["reason"],
        "bad_count": bad_count,
        "dimensions": {
            "ld_bad": ld_bad,
            "drop_bad": drop_bad,
            "w_bad": w_bad,
            "lb_bad": lb_bad,
        },
        "inputs": {
            "limit_down": limit_down if _is_num(limit_down) else None,
            "drop_over_9pct": drop_over_9pct if _is_num(drop_over_9pct) else None,
            "weighted_auction_gain": w_avg if _is_num(w_avg) else None,
            "prev_day_limit_down": prev_day_limit_down if _is_num(prev_day_limit_down) else None,
            "prev_day_weighted_auction_gain": prev_w_avg if _is_num(prev_w_avg) else None,
            "main_board_leaders_summary": summary,
        },
    }

    try:
        (DATA_DIR / "latest_advice.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[决策快照] 写入失败: {e}")
        return None

    return payload


def _load_advice_from_disk() -> dict | None:
    """读 latest_advice.json，反向重命名 snake_case → 内部 dict (BR-3.5)

    保 _build_html 完全无感知（继续使用 position / position_short / color / bg）。
    缺失 / 损坏 / 字段不全 → 返回 None，由 caller fallback 到 _calc_daily_advice。
    """
    p = DATA_DIR / "latest_advice.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[邮件] 决策快照解析失败: {e}，回退实时计算")
        return None
    if not isinstance(data, dict) or not _REQUIRED_ADVICE_KEYS.issubset(data.keys()):
        print("[邮件] 决策快照字段不全，回退实时计算")
        return None

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


def _build_html(
    cycle_phase: str, cycle_day: int,
    leader: dict | None, hits: list[dict], signals: list[dict],
    sentiment_data: dict | None, ranking_data: dict | None,
    advice: dict,
) -> str:
    now = now_cn().strftime("%Y-%m-%d %H:%M:%S")

    # === 6 指标格数据 ===
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

    # === 第 1 格：竞价跌停 / 跌>9% 双数 + 箭头 + 昨日对比 ===
    # 真源 src/static/index.html:513-530
    ld_disp = limit_down if _is_num(limit_down) else "—"
    drop_disp = drop_over_9pct if _is_num(drop_over_9pct) else "—"
    ld_color = "#ef4444" if (_is_num(limit_down) and limit_down > 5) else "#10b981"
    drop_color = "#ef4444" if (_is_num(drop_over_9pct) and drop_over_9pct > 9) else "#10b981"

    arrow_html = ""
    if _is_num(limit_down) and _is_num(prev_day_limit_down):
        if limit_down > prev_day_limit_down:
            arrow, ar_color = "↑", "#ef4444"
        elif limit_down < prev_day_limit_down:
            arrow, ar_color = "↓", "#10b981"
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

    # 主值整体颜色：任一维触发警戒 → 红，否则绿
    if (_is_num(limit_down) and limit_down > 5) or (_is_num(drop_over_9pct) and drop_over_9pct > 9):
        main1_color = "#ef4444"
    elif _is_num(limit_down) or _is_num(drop_over_9pct):
        main1_color = "#10b981"
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

    # === 第 4 格：接力情绪 + title + 细分子项 ===
    # 真源 src/static/index.html:556-568
    sample = y_avg.get("sample_count")
    avg_chg = y_avg.get("avg_change_pct")
    pos_cnt = y_avg.get("positive_count")
    neg_cnt = y_avg.get("negative_count")
    ld_cnt = y_avg.get("limit_down_count")
    median_chg = y_avg.get("median_change_pct")
    high5 = y_avg.get("high5_count")
    flat2 = y_avg.get("flat2_count")
    low5 = y_avg.get("low5_count")

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
        median_str = (f"{'+' if median_chg >= 0 else ''}{median_chg}%") if _is_num(median_chg) else "—"
        sub4_str = (
            f"中位数 {median_str} · "
            f"高开>5%:{high5 if _is_num(high5) else '—'} · "
            f"平开±2%:{flat2 if _is_num(flat2) else '—'} · "
            f"低开<-5%:{low5 if _is_num(low5) else '—'}"
        )
    else:
        title4 = "昨日涨停 — 只"
        main4 = "—"
        main4_color = "#6b7280"
        sub4_str = "中位数 — · 高开>5%:— · 平开±2%:— · 低开<-5%:—"

    cell4_html = (
        '<td style="background:#f8f9fa;border:1px solid #e5e7eb;padding:6px 10px;'
        'border-radius:6px;width:33%;vertical-align:top;">'
        '<div style="font-size:11px;color:#888;">接力情绪</div>'
        f'<div style="font-size:10px;color:#6b7280;margin-top:1px;">{title4}</div>'
        f'<div style="font-size:16px;font-weight:700;color:{main4_color};margin-top:2px;">{main4}</div>'
        f'<div style="font-size:10px;color:#999;margin-top:1px;">{sub4_str}</div>'
        '</td>'
    )

    # 连板高标：去掉emoji，缩短文字
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

    # === 触发原因 ===
    reason_html = ""
    if advice["reason"]:
        reason_html = f"""
        <div style="margin-top:12px;padding:10px 14px;background:rgba(0,0,0,0.3);
                    border-radius:6px;font-size:13px;color:#d1d5db;line-height:1.5;">
          💡 {advice['reason']}
        </div>
        """

    # === 决策大字栏 ===
    hero_html = f"""
    <div style="padding:18px 24px;border:2px solid {advice['color']};border-radius:12px;
                background:linear-gradient(135deg, {advice['bg']} 0%, #0d1220 70%);margin-bottom:16px;">
      <div style="font-size:30px;font-weight:800;color:{advice['color']};line-height:1.1;">
        {advice['text']}
      </div>
      <div style="font-size:14px;color:#a0aec0;margin-top:6px;">
        建议仓位：<b style="color:#e0e6ed;">{advice['position']}</b>
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
            if ic.exists():
                industry_map = json.loads(ic.read_text())
        except Exception:
            pass
        if ranking_data:
            for r in (ranking_data.get("ranking") or []):
                code = str(r.get("code", ""))
                if code and r.get("industry"):
                    industry_map[code] = r["industry"]

        # 概念 top_concepts：优先 ranking 已注入字段，缺失则按全市场涨停聚合补齐
        top_concepts_map: dict[str, list[str]] = {}
        if ranking_data:
            for r in (ranking_data.get("ranking") or []):
                code = str(r.get("code", ""))
                if code and r.get("top_concepts"):
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
                if lu_file.exists():
                    lu = json.loads(lu_file.read_text()) or {}
                    if lu:
                        latest = sorted(lu.keys())[-1]
                        heats = aggregate_concept_limit_ups(lu.get(latest, []) or [], c_map)
            except Exception:
                pass
            for h in hits:
                code = str(h.get("code", ""))
                if code and code not in top_concepts_map and code in c_map:
                    top_concepts_map[code] = top_concepts_for_stock(
                        list(c_map.get(code) or []), heats, top_n=2,
                    )
        except Exception:
            pass

        # 10日涨幅兜底（不在排行中的标的用K线计算）
        gain_10d_map = {}
        if ranking_data:
            for r in (ranking_data.get("ranking") or []):
                code = str(r.get("code", ""))
                if code:
                    gain_10d_map[code] = r.get("gain_10d")
        hit_codes_missing_10d = [
            str(h.get("code", "")) for h in hits
            if str(h.get("code", "")) not in gain_10d_map and not h.get("gain_10d")
        ]
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

        signal_map = {str(s.get("code", "")): s for s in (signals or [])}

        rows = []
        for h in hits:
            code = str(h.get("code", ""))
            cycle_tag = (
                ' <span style="background:#1e3a5f;color:#60a5fa;padding:1px 5px;border-radius:3px;font-size:11px;">🎯</span>'
                if h.get("matched_cycle") else ""
            )
            sig = signal_map.get(code, {})
            lvl = sig.get("level", "")
            meta = _LEVEL_META.get(lvl, {"icon": "⚪", "color": "#9ca3af", "bg": "#1f2937"})
            decision_html = (
                f'<div style="display:inline-block;padding:4px 8px;border-radius:6px;'
                f'border-left:3px solid {meta["color"]};background:{meta["bg"]};">'
                f'<div style="font-size:13px;font-weight:700;color:{meta["color"]};">'
                f'{meta["icon"]} {sig.get("suggested_position", "—")}</div>'
                f'</div>'
            ) if sig else '<span style="color:#6b7280;">—</span>'

            gain_10d = h.get("gain_10d") or gain_10d_map.get(code)
            gain_10d_html = '—' if gain_10d is None else (
                f'<b style="color:{"#ef4444" if gain_10d >= 0 else "#10b981"};">'
                f'{("+" if gain_10d > 0 else "")}{gain_10d}%</b>'
            )

            rows.append(f"""
            <tr>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;">{code}</td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;color:#e0e6ed;">
                {h.get('name', '')}{cycle_tag}{'<span style="background:#7f1d1d;color:#fca5a5;padding:1px 4px;border-radius:3px;font-size:10px;margin-left:4px;">⚠三板组</span>' if h.get('sanbanzhu') else ''}
              </td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;">{h.get('open_price', '—')}</td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;color:#ef4444;font-weight:700;">
                {h.get('auction_gain', '—')}%
              </td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;">{h.get('market_cap', '—')}亿</td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;font-size:12px;">
                {_render_concept_industry_cell(top_concepts_map.get(code) or [], industry_map.get(code, '-'))}
              </td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;font-weight:700;">
                {h.get('continuous_limit_up', '')}板
              </td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;">{gain_10d_html}</td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;">{decision_html}</td>
            </tr>
            """)

        hits_html = f"""
        <table style="width:100%;border-collapse:collapse;font-size:13px;background:#111827;">
          <thead>
            <tr style="background:#0d1220;color:#8892a8;">
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">代码</th>
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">名称</th>
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">价格</th>
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">竞价涨幅</th>
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">市值</th>
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">板块</th>
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">连板</th>
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">10日%</th>
              <th style="padding:8px;text-align:left;border-bottom:1px solid #1e2a45;">决策</th>
            </tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
        """
    else:
        hits_html = '<p style="color:#9ca3af;text-align:center;padding:40px;">无命中标的</p>'

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

      <p style="color:#4b5563;font-size:11px;margin-top:20px;text-align:center;">
        当前周期：{cycle_phase} · 第 {cycle_day} 天 · 详情看板 dashboard
      </p>
    </div>
    """


def _send(subject: str, html: str) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [NOTIFY_TO], msg.as_string())
        print(f"[邮件] 推送成功 → {NOTIFY_TO}")
        return True
    except Exception as e:
        print(f"[邮件] 推送失败: {e}")
        return False

"""邮件推送模块

9:27 选股完成后，按看板新版布局推送决策邮件：
- 顶部决策大字栏（当日操作建议 + 建议仓位）
- 4 指标格（竞价跌停 / 梯队加权竞价 / 昨日涨停溢价率 / 昨日连板高标竞价）
- 触发原因
- 今日选股精简表（代码/名称/价格/竞价/市值/板块/连板/10日%/决策）
- 当前周期阶段（小字脚注）

不包含（避免阅读量）：
- 周期代表股 / 龙头反馈 / 昨日涨停股 详情
- 交叉验证信号详情
- 四维评分卡 / 回测统计
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from src.config import now_cn


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

    advice = _calc_daily_advice(sentiment_data, leader)

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
    """三维警戒 → {bucket, text, position, position_short, reason, color, bg}

    维度: 竞价跌停>5 / 加权竞价<0 / 连板高标(任一跌停或水下开)
    任二触发 → 不操作；单触发 → 谨慎；都未触发 → 可参与
    """
    sent = sent or {}
    market = sent.get("market") or {}
    limit_down = market.get("limit_down")
    w_avg = sent.get("weighted_auction_gain")

    has_ld = isinstance(limit_down, (int, float))
    has_w = isinstance(w_avg, (int, float))

    mb_list = (leader or {}).get("main_board_leaders") or []
    lb_bad_list = [
        x for x in mb_list
        if x.get("signal") == "跌停"
        or (isinstance(x.get("auction_change_pct"), (int, float)) and x["auction_change_pct"] < 0)
    ]
    has_lb = len(mb_list) > 0

    if not has_ld and not has_w and not has_lb:
        return {
            "bucket": "go", "text": "— 数据加载中 —",
            "position": "—", "position_short": "—",
            "reason": "", "color": "#6b7280", "bg": "#0d1220",
        }

    ld_bad = has_ld and limit_down > 5
    w_bad = has_w and w_avg < 0
    lb_bad = has_lb and len(lb_bad_list) > 0

    warnings = []
    if ld_bad:
        warnings.append(f"市场竞价跌停 {limit_down} 只（>5 警戒线）")
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
            "reason": "；".join(warnings) + f"。三维警戒中已 {bad_count} 项触发，避免开仓。",
            "color": "#10b981", "bg": "#0a2a0a",
        }
    if bad_count == 1:
        return {
            "bucket": "warn",
            "text": "⚠️ 谨慎参与",
            "position": "1-2 层（小仓试错）",
            "position_short": "1-2层",
            "reason": warnings[0] + "。仅一项警戒，可小仓试错或观望。",
            "color": "#fbbf24", "bg": "#2a2a0a",
        }
    return {
        "bucket": "go",
        "text": "✅ 可参与",
        "position": "3-6 层（标准仓位）",
        "position_short": "3-6层",
        "reason": "",
        "color": "#ef4444", "bg": "#2a0f0f",
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
    w_avg = (sentiment_data or {}).get("weighted_auction_gain")
    y_avg = ((leader or {}).get("yesterday_main_board_avg_auction") or {})
    y_zb = ((leader or {}).get("yesterday_zb_today_auction") or {})
    y_ld = ((leader or {}).get("yesterday_limit_down_today_auction") or {})
    lianban = _calc_lianban_state(leader)

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

    _cell = 'style="background:#f8f9fa;border:1px solid #e5e7eb;padding:10px 12px;border-radius:8px;width:33%;vertical-align:top;"'
    _label = 'style="font-size:12px;color:#6b7280;margin-bottom:4px;"'
    _val = 'style="font-size:20px;font-weight:700;margin-top:4px;"'
    _sub = 'style="font-size:11px;color:#9ca3af;margin-top:2px;"'

    metrics_html = f"""
    <table style="width:100%;border-collapse:separate;border-spacing:8px 6px;margin-top:12px;">
      <tr>
        <td {_cell}>
          <div {_label}>竞价跌停 (&gt;5⚠)</div>
          <div {_val} style="color:{num_color(limit_down, True)};">
            {limit_down if limit_down is not None else '—'}
          </div>
        </td>
        <td {_cell}>
          <div {_label}>梯队加权竞价</div>
          <div {_val} style="color:{num_color(w_avg)};">
            {fmt_pct(w_avg)}
          </div>
        </td>
        <td {_cell}>
          <div {_label}>昨日连板高标竞价</div>
          <div {_val} style="color:{lianban['color']};">
            {lianban['icon']} {lianban['label']}
          </div>
          <div {_sub}>{lianban['detail'][:40]}{'…' if len(lianban['detail']) > 40 else ''}</div>
        </td>
      </tr>
      <tr>
        <td {_cell}>
          <div {_label}>昨日涨停溢价率</div>
          <div {_val} style="color:{num_color(y_avg.get('avg_change_pct'))};">
            {fmt_pct(y_avg.get('avg_change_pct'))}
          </div>
          <div {_sub}>{f"样本{y_avg.get('sample_count')}只 高开{y_avg.get('positive_count',0)}/低开{y_avg.get('negative_count',0)}" if y_avg.get('sample_count') else ''}</div>
        </td>
        <td {_cell}>
          <div {_label}>昨日炸板今日均价</div>
          <div {_val} style="color:{num_color(y_zb.get('avg_change_pct'))};">
            {fmt_pct(y_zb.get('avg_change_pct'))}
          </div>
          <div {_sub}>{f"样本{y_zb.get('sample_count')}只" if y_zb.get('sample_count') else '—'}</div>
        </td>
        <td {_cell}>
          <div {_label}>昨日跌停今日均价</div>
          <div {_val} style="color:{num_color(y_ld.get('avg_change_pct'))};">
            {fmt_pct(y_ld.get('avg_change_pct'))}
          </div>
          <div {_sub}>{f"样本{y_ld.get('sample_count')}只" if y_ld.get('sample_count') else '—'}</div>
        </td>
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
        # 板块查表（来自 ranking_data）
        industry_map = {}
        if ranking_data:
            for r in (ranking_data.get("ranking") or []):
                code = str(r.get("code", ""))
                if code and r.get("industry"):
                    industry_map[code] = r["industry"]

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

            gain_10d = h.get("gain_10d")
            gain_10d_html = '—' if gain_10d is None else (
                f'<b style="color:{"#ef4444" if gain_10d >= 0 else "#10b981"};">'
                f'{("+" if gain_10d > 0 else "")}{gain_10d}%</b>'
            )

            rows.append(f"""
            <tr>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;">{code}</td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;color:#e0e6ed;">
                {h.get('name', '')}{cycle_tag}
              </td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;">{h.get('open_price', '—')}</td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;color:#ef4444;font-weight:700;">
                {h.get('auction_gain', '—')}%
              </td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;">{h.get('market_cap', '—')}亿</td>
              <td style="padding:8px;border-bottom:1px solid #1e2a45;color:#a0aec0;font-size:12px;">
                {industry_map.get(code, '-')}
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

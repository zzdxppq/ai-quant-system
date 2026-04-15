"""邮件推送模块

9:27选股完成后，将结果推送到指定邮箱
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from src.config import now_cn


# QQ邮箱 SMTP 配置
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
SMTP_USER = os.getenv("SMTP_USER", "")           # 发件邮箱
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")    # QQ邮箱授权码
NOTIFY_TO = os.getenv("NOTIFY_TO", "604491810@qq.com")


def send_screener_report(
    cycle_phase: str,
    cycle_day: int,
    representative: dict | None,
    leader: dict | None,
    hits: list[dict],
    signals: list[dict],
    deviations: list[dict] | None = None,
) -> bool:
    """发送选股报告邮件

    Returns:
        True if sent successfully
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        print("[邮件] 未配置 SMTP_USER 或 SMTP_PASSWORD，跳过推送")
        return False

    now = now_cn()
    subject = f"【周期选股】{now.strftime('%m/%d')} {cycle_phase} | "

    if signals:
        strong = [s for s in signals if s.get("level") == "strong"]
        if strong:
            subject += f"🟢 强信号 {len(strong)} 只"
        else:
            subject += f"{len(signals)} 只信号"
    else:
        subject += "无命中"

    html = _build_html(cycle_phase, cycle_day, representative, leader, hits, signals, deviations)

    return _send(subject, html)


def _build_html(
    cycle_phase, cycle_day, representative, leader, hits, signals, deviations
) -> str:
    """构建邮件HTML"""
    now = now_cn().strftime("%Y-%m-%d %H:%M:%S")

    # 周期状态颜色
    phase_colors = {
        "孕育期": "#6b7280", "小周期启动": "#fbbf24", "混沌期": "#ef4444",
        "小周期完成": "#34d399", "完整周期": "#10b981", "余温期": "#f59e0b", "退潮期": "#8b5cf6",
    }
    phase_color = phase_colors.get(cycle_phase, "#6b7280")

    # 龙头反馈（市场高标 + 主板最高标）
    leader_html = ""
    leader_icons = {
        "强正反馈": "🚀", "正反馈": "☀️", "中性": "☁️", "负反馈": "🌧️", "跌停": "⛔"
    }

    def _render_leader_block(tag: str, ld: dict) -> str:
        if not ld or not ld.get("signal"):
            return ""
        icon = leader_icons.get(ld["signal"], "❓")
        color = "#10b981" if ld.get("can_trade") else "#ef4444"
        chg = ld.get("auction_change_pct", 0)
        chg_color = "#ef4444" if chg >= 0 else "#10b981"
        chg_sign = "+" if chg > 0 else ""
        trade_text = (
            f"可操作 - {ld.get('aggression', '')}"
            if ld.get("can_trade")
            else "⛔ 当日不操作"
        )
        return f"""
        <div style="background:#f8f9fa;border-left:4px solid {color};padding:12px;margin:8px 0;border-radius:4px;">
            <b>{icon} {tag}：{ld.get('leader_name', '')}</b>
            （10日涨幅 {ld.get('leader_gain_10d', '')}%）
            竞价 <span style="color:{chg_color}">{chg_sign}{chg}%</span>
            → <b style="color:{color}">{trade_text}</b>
            <div style="color:#666;font-size:12px;margin-top:4px;">{ld.get('reason', '')}</div>
        </div>
        """

    if leader:
        # 新格式：market_leader + main_board_leader
        market_ld = leader.get("market_leader")
        mb_ld = leader.get("main_board_leader")
        if market_ld or mb_ld:
            leader_html = _render_leader_block("市场高标", market_ld) + _render_leader_block("主板高标", mb_ld)
        elif leader.get("signal"):
            # 兼容旧格式（只有一个龙头）
            leader_html = _render_leader_block("高标龙头", leader)

    # 代表股
    rep_html = ""
    if representative:
        rep_html = f"""
        <div style="background:#f0f9ff;border-left:4px solid #3b82f6;padding:12px;margin:12px 0;border-radius:4px;">
            <b>周期代表股：</b>{representative.get('name', '')}({representative.get('code', '')})
            <span style="color:#ef4444;font-weight:bold;">10日涨幅 {representative.get('gain_10d', 0)}%</span>
            · 持续{representative.get('sustain_days', 0)}天
            · 榜首{representative.get('top_days', 0)}天
            · {'主板' if representative.get('is_main_board') else '创/科'}
        </div>
        """

    # 选股结果
    hits_html = "<p style='color:#999;'>无命中标的</p>"
    if hits:
        dev_map = {}
        if deviations:
            dev_map = {d["code"]: d for d in deviations}

        rows = ""
        for h in hits:
            cycle_tag = ' <span style="background:#dbeafe;color:#2563eb;padding:1px 6px;border-radius:3px;font-size:11px;">🎯 周期匹配</span>' if h.get("matched_cycle") else ""
            dev = dev_map.get(h.get("code", ""))
            dev_str = ""
            if dev:
                dev_str = f"{dev['deviation_pct']}%"
                if dev.get("is_excessive"):
                    dev_str = f'<span style="color:#ef4444">{dev_str} ⚠️</span>'

            rows += f"""
            <tr>
                <td style="padding:8px;border-bottom:1px solid #eee;">{h.get('code','')}</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{h.get('name','')}{cycle_tag}</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{h.get('continuous_limit_up','')}板</td>
                <td style="padding:8px;border-bottom:1px solid #eee;color:#ef4444;">{h.get('auction_gain','')}%</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{h.get('auction_turnover','')}%</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{h.get('market_cap','')}亿</td>
                <td style="padding:8px;border-bottom:1px solid #eee;">{dev_str}</td>
            </tr>
            """
        hits_html = f"""
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
            <thead>
                <tr style="background:#f8f9fa;">
                    <th style="padding:8px;text-align:left;">代码</th>
                    <th style="padding:8px;text-align:left;">名称</th>
                    <th style="padding:8px;text-align:left;">连板</th>
                    <th style="padding:8px;text-align:left;">竞价涨幅</th>
                    <th style="padding:8px;text-align:left;">换手</th>
                    <th style="padding:8px;text-align:left;">市值</th>
                    <th style="padding:8px;text-align:left;">240周偏离</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        """

    # 交叉验证信号
    signals_html = ""
    if signals:
        level_icons = {"strong": "🟢", "normal": "🔵", "watch": "🟡", "avoid": "🔴"}
        level_colors = {"strong": "#065f46", "normal": "#1e3a5f", "watch": "#78350f", "avoid": "#7f1d1d"}
        for s in signals:
            lv = s.get("level", "watch")
            signals_html += f"""
            <div style="background:#f8f9fa;border-left:4px solid {level_colors.get(lv,'#ccc')};padding:10px;margin:8px 0;border-radius:4px;">
                <b>{level_icons.get(lv,'')} {s.get('name','')}({s.get('code','')})</b>
                → <b>{s.get('suggested_position','')}</b>
                <div style="color:#666;font-size:12px;margin-top:2px;">{s.get('reason','')}</div>
            </div>
            """
    else:
        signals_html = "<p style='color:#999;'>无交叉验证信号</p>"

    return f"""
    <div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:640px;margin:0 auto;padding:20px;">
        <h2 style="margin:0 0 4px 0;">AI量化周期看板 · 选股日报</h2>
        <p style="color:#999;font-size:13px;margin:0 0 16px 0;">{now}</p>

        <div style="background:{phase_color};color:white;display:inline-block;padding:8px 20px;border-radius:8px;font-size:20px;font-weight:bold;margin-bottom:12px;">
            {cycle_phase} · 第{cycle_day}天
        </div>

        {rep_html}
        {leader_html}

        <h3 style="margin:20px 0 8px 0;border-bottom:1px solid #eee;padding-bottom:6px;">📋 选股结果</h3>
        {hits_html}

        <h3 style="margin:20px 0 8px 0;border-bottom:1px solid #eee;padding-bottom:6px;">🎯 交叉验证信号</h3>
        {signals_html}

        <p style="color:#ccc;font-size:11px;margin-top:24px;text-align:center;">
            AI量化周期看板 · 自动推送
        </p>
    </div>
    """


def _send(subject: str, html: str) -> bool:
    """通过QQ邮箱SMTP发送"""
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

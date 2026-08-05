"""月度策略报告

每月1日自动生成上月报告，或手动触发。
内容：胜率/盈亏/连板分布/情绪准确率/偏差分析/优化建议
"""
from datetime import datetime, timedelta
from typing import Optional

from src.config import DATA_DIR, now_cn
from src.data.json_io import dump_json_file
from src.engine.screener_history import _load as load_history, calc_win_stats


def generate_monthly_report(year: int = None, month: int = None) -> dict:
    """生成月度报告

    Args:
        year, month: 指定月份，默认上月
    """
    now = now_cn()
    if year is None or month is None:
        # 默认上月
        first_of_this_month = now.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        year = last_month_end.year
        month = last_month_end.month

    month_start = f"{year}-{month:02d}-01"
    if month == 12:
        month_end = f"{year+1}-01-01"
    else:
        month_end = f"{year}-{month+1:02d}-01"

    records = load_history()
    month_records = [
        r for r in records
        if r.get("date", "") >= month_start and r.get("date", "") < month_end
    ]

    judged = [r for r in month_records if r.get("is_win") is not None]

    report = {
        "period": f"{year}年{month}月",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "total_signals": len(month_records),
        "judged_signals": len(judged),
    }

    # 基础胜率
    if judged:
        wins = sum(1 for r in judged if r["is_win"])
        report["wins"] = wins
        report["losses"] = len(judged) - wins
        report["win_rate"] = round(wins / len(judged) * 100, 1)
    else:
        report["wins"] = 0
        report["losses"] = 0
        report["win_rate"] = 0

    # 按连板分布
    board_dist = {}
    for r in judged:
        label = r.get("board_label", f"{r.get('continuous_limit_up',0)}进{r.get('continuous_limit_up',0)+1}")
        board_dist.setdefault(label, {"total": 0, "wins": 0})
        board_dist[label]["total"] += 1
        if r["is_win"]:
            board_dist[label]["wins"] += 1
    for k, v in board_dist.items():
        v["win_rate"] = round(v["wins"] / v["total"] * 100, 1) if v["total"] > 0 else 0
    report["board_distribution"] = board_dist

    # 按结果分类
    outcome_dist = {"涨停": 0, "涨停炸板": 0, "未涨停": 0}
    for r in judged:
        if r.get("is_limit_up"):
            outcome_dist["涨停"] += 1
        elif r.get("is_zhaban"):
            outcome_dist["涨停炸板"] += 1
        else:
            outcome_dist["未涨停"] += 1
    report["outcome_distribution"] = outcome_dist

    # 市场情绪分析
    strong_market = [r for r in judged if (r.get("market_limit_down") or 0) <= 3]
    weak_market = [r for r in judged if (r.get("market_limit_down") or 0) > 7]
    report["emotion_analysis"] = {
        "strong_market_trades": len(strong_market),
        "strong_market_win_rate": round(sum(1 for r in strong_market if r["is_win"]) / len(strong_market) * 100, 1) if strong_market else 0,
        "weak_market_trades": len(weak_market),
        "weak_market_win_rate": round(sum(1 for r in weak_market if r["is_win"]) / len(weak_market) * 100, 1) if weak_market else 0,
    }

    # 三板组统计
    sbz_records = [r for r in judged if r.get("sanbanzhu")]
    report["sanbanzhu_count"] = len(sbz_records)
    if sbz_records:
        sbz_wins = sum(1 for r in sbz_records if r["is_win"])
        report["sanbanzhu_win_rate"] = round(sbz_wins / len(sbz_records) * 100, 1)
    else:
        report["sanbanzhu_win_rate"] = None

    # 优化建议（基于数据自动生成）
    suggestions = []

    if report["win_rate"] < 50:
        suggestions.append("整体胜率偏低，建议收紧信号筛选条件或降低仓位")

    # 找最佳和最差连板类型
    if board_dist:
        best_board = max(board_dist.items(), key=lambda x: x[1]["win_rate"])
        worst_board = min(board_dist.items(), key=lambda x: x[1]["win_rate"])
        if best_board[1]["total"] >= 3:
            suggestions.append(f"最佳连板类型：{best_board[0]}（胜率{best_board[1]['win_rate']}%），可适当加仓")
        if worst_board[1]["total"] >= 3 and worst_board[1]["win_rate"] < 40:
            suggestions.append(f"最弱连板类型：{worst_board[0]}（胜率{worst_board[1]['win_rate']}%），建议降仓或回避")

    if report["emotion_analysis"]["weak_market_trades"] > 0 and report["emotion_analysis"]["weak_market_win_rate"] < 30:
        suggestions.append("弱势市场操作胜率极低，建议跌停>7时严格空仓")

    if sbz_records and report["sanbanzhu_win_rate"] is not None and report["sanbanzhu_win_rate"] < 40:
        suggestions.append("三板组标记的标的胜率低，建议回避或降仓")

    report["suggestions"] = suggestions

    # 保存
    report_file = DATA_DIR / f"report_{year}_{month:02d}.json"
    dump_json_file(report_file, report)

    return report

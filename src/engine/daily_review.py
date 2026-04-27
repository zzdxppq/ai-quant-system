"""盘后复盘引擎 — 次日鱼塘

15:45 收盘后自动运行：
1. 涨停梯队分析（按板块分组，识别主线）
2. 连板股梯队（按连板数排序）
3. 生成次日观察池（3只核心标的+逻辑备注）
4. 输出到 data/latest_review.json + 邮件推送
"""
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd

from src.config import DATA_DIR, now_cn


@dataclass
class WatchCandidate:
    """次日观察池候选"""
    code: str
    name: str
    board_count: int            # 连板数
    industry: str               # 板块
    close: float                # 收盘价
    market_cap_yi: float        # 流通市值(亿)
    total_gain_pct: float       # 累计涨幅%
    reason: str                 # 看好逻辑
    watch_points: str           # 次日观察点
    auction_range: str          # 理想竞价区间


@dataclass
class DailyReview:
    """每日复盘结果"""
    date: str
    # 涨停梯队
    limit_up_count: int = 0                     # 今日涨停总数
    main_board_limit_up: int = 0                # 主板涨停数
    sector_groups: dict = field(default_factory=dict)  # 板块分组 {板块: [股票列表]}
    main_theme: str = ""                        # 主线板块
    theme_strength: str = ""                    # 主线强度
    # 连板梯队
    lianban_ladder: list = field(default_factory=list)  # [{code,name,board_count,industry}]
    highest_board: int = 0                      # 最高连板数
    # 次日观察池
    watch_pool: list = field(default_factory=list)      # [WatchCandidate]
    # 市场情绪小结
    market_summary: str = ""


def run_daily_review() -> Optional[DailyReview]:
    """执行盘后复盘"""
    today = now_cn().strftime("%Y-%m-%d")
    print(f"[复盘] {today} 开始...")

    # 1. 获取涨停数据
    limit_up_data = _get_today_limit_ups()
    if not limit_up_data:
        print("[复盘] 无涨停数据，跳过")
        return None

    # 2. 获取连板数据
    lianban_data = _get_lianban_ladder()

    # 3. 获取排行数据补充板块信息
    ranking_data = _get_ranking_data()

    # 4. 构建复盘
    review = DailyReview(date=today)

    # 涨停梯队分析
    review.limit_up_count = len(limit_up_data)
    review.main_board_limit_up = sum(
        1 for s in limit_up_data
        if not str(s.get("code", "")).startswith(("300", "301", "688", "8", "4"))
    )

    # 按板块分组
    sector_map = defaultdict(list)
    for s in limit_up_data:
        industry = s.get("industry", "未知")
        if not industry or industry == "":
            industry = _lookup_industry(s.get("code", ""), ranking_data)
        sector_map[industry].append({
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "change_pct": s.get("change_pct", 0),
        })

    review.sector_groups = dict(sector_map)

    # 识别主线（涨停数最多的板块）
    if sector_map:
        sorted_sectors = sorted(sector_map.items(), key=lambda x: len(x[1]), reverse=True)
        review.main_theme = sorted_sectors[0][0]
        top_count = len(sorted_sectors[0][1])
        if top_count >= 5:
            review.theme_strength = "极强（5+涨停）"
        elif top_count >= 3:
            review.theme_strength = "较强（3-4涨停）"
        elif top_count >= 2:
            review.theme_strength = "一般（2涨停）"
        else:
            review.theme_strength = "散乱（无明确主线）"

    # 连板梯队
    review.lianban_ladder = lianban_data
    review.highest_board = max((s.get("board_count", 0) for s in lianban_data), default=0)

    # 5. 生成次日观察池
    review.watch_pool = _generate_watch_pool(lianban_data, ranking_data, review)

    # 6. 市场情绪小结
    review.market_summary = _generate_summary(review)

    # 7. 保存
    _save_review(review)
    print(f"[复盘] 完成: 涨停{review.limit_up_count}只 主线:{review.main_theme} 观察池:{len(review.watch_pool)}只")

    return review


def _get_today_limit_ups() -> list[dict]:
    """获取今日涨停股列表"""
    cache_file = DATA_DIR / "limit_up_cache.json"
    if not cache_file.exists():
        return []

    try:
        cache = json.loads(cache_file.read_text())
        today = now_cn().strftime("%Y%m%d")
        # 取今天或最近一天的数据
        if today in cache:
            return cache[today]
        # 取最近的
        if cache:
            latest = sorted(cache.keys())[-1]
            return cache[latest]
    except Exception:
        pass
    return []


def _get_lianban_ladder() -> list[dict]:
    """获取连板梯队"""
    cache_file = DATA_DIR / "limit_up_cache.json"
    if not cache_file.exists():
        return []

    try:
        cache = json.loads(cache_file.read_text())
        sorted_dates = sorted(cache.keys(), reverse=True)
        if not sorted_dates:
            return []

        # 从最近一天开始往前回溯计算连板
        latest_date = sorted_dates[0]
        latest_stocks = {r.get("code", ""): r for r in cache.get(latest_date, [])}

        result = []
        for code, info in latest_stocks.items():
            # 往前数连续出现的天数
            count = 1
            for d in sorted_dates[1:]:
                codes_in_day = [r.get("code", "") for r in cache.get(d, [])]
                if code in codes_in_day:
                    count += 1
                else:
                    break

            is_main = not code.startswith(("300", "301", "688", "8", "4"))
            result.append({
                "code": code,
                "name": info.get("name", ""),
                "board_count": count,
                "is_main_board": is_main,
                "change_pct": info.get("change_pct", 0),
            })

        # 按连板数降序
        result.sort(key=lambda x: (-x["board_count"], -x.get("change_pct", 0)))
        return result

    except Exception:
        return []


def _get_ranking_data() -> dict:
    """获取排行数据（用于板块信息）"""
    ranking_file = DATA_DIR / "latest_ranking.json"
    if not ranking_file.exists():
        return {}
    try:
        data = json.loads(ranking_file.read_text())
        return {str(r["code"]): r for r in data.get("ranking", [])}
    except Exception:
        return {}


def _lookup_industry(code: str, ranking_data: dict) -> str:
    """从排行数据查板块"""
    r = ranking_data.get(code)
    if r:
        return r.get("industry", "未知")
    return "未知"


def _generate_watch_pool(
    lianban: list[dict],
    ranking: dict,
    review: DailyReview,
) -> list[dict]:
    """生成次日观察池（最多3只主板2连板以上）"""
    candidates = []

    # 筛选：主板、2连板以上
    main_board_lianban = [
        s for s in lianban
        if s.get("is_main_board") and s.get("board_count", 0) >= 2
    ]

    for s in main_board_lianban[:5]:  # 取前5候选
        code = s["code"]
        name = s["name"]
        bc = s["board_count"]

        # 从排行补充信息
        r = ranking.get(code, {})
        industry = r.get("industry", "未知")
        close = r.get("close", 0)
        mc = r.get("market_cap_yi", 0)
        gain_10d = r.get("gain_10d", 0)

        # 生成逻辑备注
        reason = _generate_reason(s, review)
        watch_points = _generate_watch_points(s, review)
        auction_range = _calc_auction_range(close)

        candidates.append(asdict(WatchCandidate(
            code=code,
            name=name,
            board_count=bc,
            industry=industry,
            close=close,
            market_cap_yi=mc,
            total_gain_pct=gain_10d,
            reason=reason,
            watch_points=watch_points,
            auction_range=auction_range,
        )))

    return candidates[:3]


def _generate_reason(stock: dict, review: DailyReview) -> str:
    """生成看好逻辑"""
    bc = stock.get("board_count", 0)
    name = stock.get("name", "")
    parts = []

    if bc >= 5:
        parts.append(f"{bc}连板妖股，市场情绪标杆")
    elif bc >= 3:
        parts.append(f"{bc}连板龙头，板块领涨核心")
    else:
        parts.append(f"{bc}连板，有晋级预期")

    if review.main_theme and review.main_theme != "未知":
        parts.append(f"主线板块：{review.main_theme}")

    return "；".join(parts)


def _generate_watch_points(stock: dict, review: DailyReview) -> str:
    """生成次日观察点"""
    bc = stock.get("board_count", 0)
    points = []

    points.append("竞价涨幅4-7.5%为理想区间")
    if bc >= 3:
        points.append("关注是否缩量加速（一字或T字板更强）")
    else:
        points.append("关注竞价承接力度和封单情况")

    points.append("同板块是否有跟风助攻")

    return "；".join(points)


def _calc_auction_range(close: float) -> str:
    """计算理想竞价区间"""
    if close <= 0:
        return "-"
    low = round(close * 1.04, 2)
    high = round(close * 1.075, 2)
    return f"{low} ~ {high}"


def _generate_summary(review: DailyReview) -> str:
    """生成市场情绪小结"""
    parts = []

    # 涨停数量判断
    lu = review.limit_up_count
    if lu >= 80:
        parts.append(f"今日涨停{lu}只，市场赚钱效应极强")
    elif lu >= 50:
        parts.append(f"今日涨停{lu}只，市场情绪活跃")
    elif lu >= 30:
        parts.append(f"今日涨停{lu}只，市场情绪一般")
    elif lu >= 15:
        parts.append(f"今日涨停{lu}只，市场偏冷")
    else:
        parts.append(f"今日涨停{lu}只，市场冰点")

    # 主线
    if review.theme_strength:
        parts.append(f"主线{review.main_theme}（{review.theme_strength}）")

    # 连板高度
    if review.highest_board >= 5:
        parts.append(f"最高{review.highest_board}连板，高度空间充足")
    elif review.highest_board >= 3:
        parts.append(f"最高{review.highest_board}连板，有晋级空间")
    else:
        parts.append("连板高度低，短线谨慎")

    return "。".join(parts) + "。"


def _save_review(review: DailyReview):
    """保存复盘结果"""
    data = asdict(review)
    (DATA_DIR / "latest_review.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2)
    )

    # 追加历史
    history_file = DATA_DIR / "review_history.json"
    history = []
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
        except Exception:
            pass
    # 去重
    history = [h for h in history if h.get("date") != review.date]
    history.append(data)
    history = history[-30:]  # 保留30天
    history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2))

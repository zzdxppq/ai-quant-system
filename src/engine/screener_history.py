"""选股记录模块

功能：
1. 每日9:27选股后归档当日选股结果
2. 每日15:30收盘后回填当日收盘价+日内表现
3. 次日9:27回填昨日记录的"次日竞价涨幅"
4. 按周/月/年/总统计胜率

数据结构（screener_history.json）：
[
  {
    "date": "2026-04-17",
    "code": "002297",
    "name": "博云新材",
    "continuous_limit_up": 3,
    "open_price": 25.5,         # 竞价开盘价
    "auction_gain": 5.2,        # 竞价涨幅%
    "close_price": null,        # 当日收盘价（15:30回填）
    "close_gain": null,         # 日内涨幅 (close/open - 1)*100（15:30回填）
    "next_day_open": null,      # 次日开盘价（次日9:27回填）
    "next_day_auction_gain": null,  # 次日竞价涨幅 vs 今收（次日9:27回填）
    "is_win": null,             # 胜负（close_gain > 0）
    "status": "pending"         # pending→closed→settled
  }
]
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.config import DATA_DIR, now_cn


HISTORY_FILE = DATA_DIR / "screener_history.json"


def _load() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return []


def _save(records: list[dict]):
    HISTORY_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2))


def _lookup_limit_up_time(code: str) -> str | None:
    """从 latest_review.lianban_ladder 查该 code 当日的封板时间（HH:MM:SS）；
    未涨停或无数据返回 None"""
    try:
        rf = DATA_DIR / "latest_review.json"
        if not rf.exists():
            return None
        rd = json.loads(rf.read_text())
        for s in (rd.get("lianban_ladder") or []):
            if str(s.get("code", "")) == str(code):
                lbt = s.get("lbt") or s.get("last_limit_up_time")
                return str(lbt) if lbt else None
    except Exception:
        pass
    return None


def _load_daily_market_env() -> dict:
    """从 latest_sentiment / latest_leader / latest_review 读取当日市场环境指标

    Returns:
        {
            "market_limit_down": int,            # 全市场竞价跌停数
            "weighted_auction_gain": float,      # 梯队加权竞价(%)
            "yesterday_lianban_today_avg": float,# 昨日(主板)连板股今日平均涨幅(%)
            "b1_rate": float,                    # 1进2成功率(%) — 当日复盘评分卡值
            "concentration": float,              # 板块集中度(%) — 当日复盘评分卡值
        }
    """
    env = {
        "market_limit_down": None,
        "weighted_auction_gain": None,
        "yesterday_lianban_today_avg": None,
        "b1_rate": None,
        "concentration": None,
    }
    try:
        sf = DATA_DIR / "latest_sentiment.json"
        if sf.exists():
            sd = json.loads(sf.read_text())
            env["weighted_auction_gain"] = sd.get("weighted_auction_gain")
            mk = sd.get("market") or {}
            env["market_limit_down"] = mk.get("limit_down")
    except Exception:
        pass
    try:
        lf = DATA_DIR / "latest_leader.json"
        if lf.exists():
            ld = json.loads(lf.read_text())
            ya = ld.get("yesterday_main_board_avg_auction") or {}
            env["yesterday_lianban_today_avg"] = ya.get("avg_change_pct")
    except Exception:
        pass
    # 1进2成功率 + 板块集中度（来自 latest_review.scorecard.indicators）
    try:
        rf = DATA_DIR / "latest_review.json"
        if rf.exists():
            rd = json.loads(rf.read_text())
            sc = rd.get("scorecard") or {}
            for ind in (sc.get("indicators") or []):
                lbl = ind.get("label", "")
                raw = ind.get("raw")
                if raw is None:
                    continue
                if lbl == "1进2成功率":
                    env["b1_rate"] = float(raw)
                elif lbl == "板块集中度":
                    env["concentration"] = float(raw)
    except Exception:
        pass
    return env


def _get_market_highest_board() -> int:
    """获取市场当前最高连板数"""
    try:
        cache_file = DATA_DIR / "limit_up_cache.json"
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
            sorted_dates = sorted(cache.keys(), reverse=True)
            if not sorted_dates:
                return 0
            latest = sorted_dates[0]
            stocks = cache[latest]
            # 计算每只的连板数
            max_board = 0
            for s in stocks:
                code = s.get("code", "")
                count = 1
                for d in sorted_dates[1:]:
                    codes_in_day = [r.get("code", "") for r in cache.get(d, [])]
                    if code in codes_in_day:
                        count += 1
                    else:
                        break
                max_board = max(max_board, count)
            return max_board
    except Exception:
        pass
    return 0


def _load_top30_codes() -> set:
    """读取最新 ranking TOP30 的代码集合（用于判定周期股）"""
    try:
        rf = DATA_DIR / "latest_ranking.json"
        if rf.exists():
            data = json.loads(rf.read_text())
            return {str(r.get("code", "")) for r in data.get("ranking", [])}
    except Exception:
        pass
    return set()


def _load_top30_streak_map() -> dict:
    """读取最新 ranking 中每只 TOP30 个股的连续在榜天数（top30_streak）"""
    try:
        rf = DATA_DIR / "latest_ranking.json"
        if rf.exists():
            data = json.loads(rf.read_text())
            return {
                str(r.get("code", "")): r.get("top30_streak")
                for r in data.get("ranking", [])
                if r.get("top30_streak") is not None
            }
    except Exception:
        pass
    return {}


def _lookup_industry(code: str) -> str:
    """从缓存查板块"""
    try:
        ic = DATA_DIR / "industry_cache.json"
        if ic.exists():
            ind_map = json.loads(ic.read_text())
            if code in ind_map:
                return ind_map[code]
        rf = DATA_DIR / "latest_ranking.json"
        if rf.exists():
            rd = json.loads(rf.read_text())
            for r in rd.get("ranking", []):
                if str(r.get("code", "")) == code:
                    return r.get("industry", "")
    except Exception:
        pass
    return ""


def _calc_gain_10d(code: str) -> float:
    """用K线算10日涨幅（选股标的可能不在排行榜中）"""
    try:
        from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
        df = fetch_kline(code, SCALE_DAILY, datalen=12)
        if df is not None and len(df) >= 2:
            today_str = now_cn().strftime("%Y-%m-%d")
            last_date = str(df.iloc[-1]["date"])[:10]
            idx = max(0, len(df) - 11) if last_date == today_str else max(0, len(df) - 10)
            close_now = float(df.iloc[-1]["close"])
            base = float(df.iloc[idx]["close"])
            if base > 0:
                return round((close_now / base - 1) * 100, 2)
    except Exception:
        pass
    return 0


def archive_today_hits(hits: list[dict], spot_df=None):
    """9:27选股后归档当日结果

    Args:
        hits: ScreenerHit 的 asdict() 列表
        spot_df: 实时行情（用于获取昨收价）
    """
    if not hits:
        return

    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    market_env = _load_daily_market_env()

    # 构建昨收价映射
    pre_close_map = {}
    if spot_df is not None and not spot_df.empty:
        for _, row in spot_df.iterrows():
            code = str(row.get("code", ""))
            pc = float(row.get("pre_close", 0))
            if code and pc > 0:
                pre_close_map[code] = pc

    # 去重：同一天同一只股票不重复写入
    existing = {(r["date"], r["code"]) for r in records}

    top30_codes = _load_top30_codes()
    streak_map = _load_top30_streak_map()
    new_count = 0
    for h in hits:
        code = h.get("code", "")
        key = (today, code)
        if key in existing:
            continue
        board = h.get("continuous_limit_up", 0)
        # 连板标签：1进2 / 2进3 / 3进4 / 4进5 / 5进6+
        board_label = f"{board}进{board+1}" if board >= 1 else "首板"
        # 市场最高板
        highest_board = _get_market_highest_board()
        # 板块
        industry = h.get("industry", "") or _lookup_industry(code)

        # 当日封板时间（若涨停）— 从 latest_review.lianban_ladder 查
        lbt = _lookup_limit_up_time(code)
        records.append({
            "date": today,
            "code": code,
            "name": h.get("name", ""),
            "continuous_limit_up": board,
            "board_label": board_label,
            "open_price": h.get("open_price", 0),
            "pre_close": pre_close_map.get(code, 0),
            "auction_gain": h.get("auction_gain", 0),
            "auction_turnover": h.get("auction_turnover"),       # 竞价换手率(%)
            "auction_volume_ratio": h.get("auction_volume_ratio"),  # 竞价量比
            "market_cap": h.get("market_cap", 0),
            "gain_10d": h.get("gain_10d", 0) or _calc_gain_10d(code),
            "industry": industry,
            "market_highest_board": highest_board,
            "limit_up_time": lbt,                                # 封板时间 HH:MM:SS（涨停时）
            "close_price": None,
            "close_gain": None,
            "day_change": None,
            "next_day_open": None,
            "next_day_auction_gain": None,
            "next_day_close_gain": None,
            "is_win": None,
            "is_limit_up": None,
            "is_zhaban": None,         # 涨停炸板
            "sanbanzhu": False,        # 三板组标记
            "sanbanzhu_detail": "",    # 三板组席位详情
            "is_cycle_stock": str(code) in top30_codes,  # 是否周期股（在TOP30）
            "top30_streak": streak_map.get(str(code)),     # 周期计数：在TOP30连续在榜天数（不在为None）
            "status": "pending",
            "market_limit_down": market_env["market_limit_down"],
            "weighted_auction_gain": market_env["weighted_auction_gain"],
            "yesterday_lianban_today_avg": market_env["yesterday_lianban_today_avg"],
            "b1_rate": market_env.get("b1_rate"),
            "concentration": market_env.get("concentration"),
        })
        existing.add(key)
        new_count += 1

    _save(records)
    print(f"[选股记录] 归档 {new_count} 只 ({today}) · 市场跌停={market_env['market_limit_down']} "
          f"加权竞价={market_env['weighted_auction_gain']} 昨日连板均价={market_env['yesterday_lianban_today_avg']}")


def backfill_close(spot_df):
    """15:30收盘后回填当日收盘价

    Args:
        spot_df: 全市场实时行情 DataFrame（含 code, close 列）
    """
    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    updated = 0

    # 构建 code→close 映射（优先K线收盘价，更准确）
    price_map = {}
    pending_codes = [r["code"] for r in records if r["date"] == today and r["status"] == "pending"]
    if pending_codes:
        try:
            from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
            for code in pending_codes:
                df = fetch_kline(code, SCALE_DAILY, datalen=3)
                if df is not None and not df.empty:
                    last_date = str(df.iloc[-1]["date"])[:10]
                    if last_date == today:
                        price_map[code] = float(df.iloc[-1]["close"])
        except Exception:
            pass

    # K线没拿到的用spot兜底
    if spot_df is not None and not spot_df.empty:
        for _, row in spot_df.iterrows():
            code = str(row.get("code", ""))
            if code not in price_map:
                close_val = float(row.get("close", 0))
                if close_val > 0:
                    price_map[code] = close_val

    for r in records:
        if r["date"] == today and r["status"] == "pending":
            code = r["code"]
            close = price_map.get(code)
            if close and close > 0:
                open_p = r.get("open_price", 0)
                pre_close = r.get("pre_close", 0)
                r["close_price"] = round(close, 2)
                r["close_gain"] = round((close / open_p - 1) * 100, 2) if open_p > 0 else 0
                # 涨停判定
                code = str(r.get("code", ""))
                if code.startswith(("300", "301", "688")):
                    limit_threshold = 19.5
                else:
                    limit_threshold = 9.8
                day_change = (close / pre_close - 1) * 100 if pre_close > 0 else r["close_gain"]
                r["day_change"] = round(day_change, 2)
                r["is_limit_up"] = day_change >= limit_threshold
                # 涨停炸板判定：最高价触及涨停但收盘未涨停
                # 需要K线的high数据，这里先用day_change近似：
                # 如果 day_change >= limit_threshold * 0.9 但 < limit_threshold → 疑似炸板
                if not r["is_limit_up"] and day_change >= limit_threshold * 0.85:
                    r["is_zhaban"] = True
                elif r["is_limit_up"]:
                    r["is_zhaban"] = False
                # 胜负判定规则：当日涨停 + 次日收红 = 盈，否则 = 亏
                if not r["is_limit_up"]:
                    r["is_win"] = False
                else:
                    r["is_win"] = None  # 待次日数据回填后判定
                r["status"] = "closed"
                updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] 回填收盘价 {updated} 只 ({today})")


def backfill_next_day_auction(spot_df):
    """回填 status=closed 记录的次日竞价涨幅

    使用K线数据回填（不依赖实时行情，避免隔天数据错位）。
    实时行情只用于回填"昨天"的记录（确保是紧接的下一个交易日）。

    Args:
        spot_df: 今日实时行情（竞价后）
    """
    from src.data.sina_kline_api import fetch_kline, SCALE_DAILY

    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    updated = 0

    # 用实时行情构建今日开盘价映射
    today_open_map = {}
    if spot_df is not None and not spot_df.empty:
        for _, row in spot_df.iterrows():
            code = str(row.get("code", ""))
            open_p = float(row.get("open", 0))
            if code and open_p > 0:
                today_open_map[code] = open_p

    for r in records:
        # 处理 closed（需要次日竞价+收盘）和 settled 但缺次日收盘的记录
        need_full = r["status"] == "closed"
        need_close_only = r["status"] == "settled" and r.get("next_day_close_gain") is None
        if not need_full and not need_close_only:
            continue

        code = r["code"]
        rec_date = r["date"]
        close_p = r.get("close_price", 0)
        if not close_p or close_p <= 0:
            continue

        # 用K线找记录日期的下一个交易日数据
        next_open = None
        next_close = None
        try:
            df = fetch_kline(code, SCALE_DAILY, datalen=10)
            if not df.empty:
                dates = [str(row["date"])[:10] for _, row in df.iterrows()]
                if rec_date in dates:
                    idx = dates.index(rec_date)
                    if idx + 1 < len(df):
                        next_open = float(df.iloc[idx + 1]["open"])
                        next_close = float(df.iloc[idx + 1]["close"])
        except Exception:
            pass

        # K线没有次日数据时，用今日实时开盘价兜底（限最近5天内）
        if next_open is None and rec_date != today:
            next_open = today_open_map.get(code)
            from datetime import datetime as _dt
            try:
                days_diff = (now_cn().replace(tzinfo=None) - _dt.strptime(rec_date, "%Y-%m-%d")).days
                if days_diff > 5:
                    next_open = None
            except Exception:
                next_open = None

        if need_close_only:
            # settled 但缺次日收盘：只补 next_day_close_gain
            if next_close and next_close > 0:
                r["next_day_close_gain"] = round((next_close / close_p - 1) * 100, 2)
                # 重新判定胜负
                is_limit_up = r.get("is_limit_up", False)
                if not is_limit_up:
                    r["is_win"] = False
                else:
                    r["is_win"] = r["next_day_close_gain"] > 0
                updated += 1
        elif next_open and next_open > 0:
            r["next_day_open"] = round(next_open, 2)
            r["next_day_auction_gain"] = round((next_open / close_p - 1) * 100, 2)
            if next_close and next_close > 0:
                r["next_day_close_gain"] = round((next_close / close_p - 1) * 100, 2)
            # 最终胜负判定：当日涨停 + 次日收红 = 盈，否则 = 亏
            is_limit_up = r.get("is_limit_up", False)
            next_close_gain = r.get("next_day_close_gain")
            if not is_limit_up:
                r["is_win"] = False
            elif next_close_gain is not None:
                r["is_win"] = next_close_gain > 0
            else:
                r["is_win"] = r["next_day_auction_gain"] > 0 if r.get("next_day_auction_gain") is not None else None
            r["status"] = "settled"
            updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] 回填次日竞价 {updated} 只")


def calc_win_stats(filter_year: int = None, filter_month: int = None) -> dict:
    """计算胜率统计

    Args:
        filter_year: 选定的年（用于 yearly + 分维度统计 + monthly的年定位）
        filter_month: 选定的月（用于 monthly + 分维度统计；需配合 filter_year）

    口径：
        - total: 全部历史选股记录
        - weekly: 当前自然周（实时，与年月选择无关）
        - monthly: filter_year+filter_month 指定的月；未指定则当前自然月
        - yearly: filter_year 指定的年；未指定则当前自然年
        - by_*: 按 filter_year+filter_month 过滤的子集（无过滤则全量）
    """
    records = _load()
    now = now_cn()

    # 只统计已有胜负判定的记录
    judged = [r for r in records if r.get("is_win") is not None]

    def _stat(subset):
        total = len(subset)
        wins = sum(1 for r in subset if r["is_win"])
        return {
            "trades": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        }

    # 本周（周一起，永远基于今天）
    weekday = now.weekday()
    monday = (now - timedelta(days=weekday)).strftime("%Y-%m-%d")
    weekly = [r for r in judged if r["date"] >= monday]

    # 本月
    if filter_year and filter_month:
        m_start = f"{int(filter_year)}-{int(filter_month):02d}-01"
        ny, nm = int(filter_year), int(filter_month) + 1
        if nm > 12:
            ny, nm = ny + 1, 1
        m_end = f"{ny}-{nm:02d}-01"
        monthly = [r for r in judged if m_start <= r["date"] < m_end]
    else:
        month_start = now.strftime("%Y-%m-01")
        monthly = [r for r in judged if r["date"] >= month_start]

    # 本年
    if filter_year:
        ystr = str(int(filter_year))
        yearly = [r for r in judged if r["date"][:4] == ystr]
    else:
        year_start = now.strftime("%Y-01-01")
        yearly = [r for r in judged if r["date"] >= year_start]

    # 分维度子集：按 filter_year+filter_month 过滤
    if filter_year and filter_month:
        scoped = monthly
    elif filter_year:
        scoped = yearly
    else:
        scoped = judged

    # === 分维度统计 ===
    # 按连板类型
    by_board = {}
    for r in scoped:
        label = r.get("board_label", f"{r.get('continuous_limit_up',0)}进{r.get('continuous_limit_up',0)+1}")
        by_board.setdefault(label, []).append(r)

    board_stats = {}
    for label, subset in sorted(by_board.items()):
        board_stats[label] = _stat(subset)

    # 按市场情绪（用跌停数分档）
    emotion_stats = {}
    for r in scoped:
        ld = r.get("market_limit_down")
        if ld is not None:
            if ld <= 3:
                emo = "强势(跌停≤3)"
            elif ld <= 7:
                emo = "正常(跌停4-7)"
            else:
                emo = "弱势(跌停>7)"
        else:
            emo = "未知"
        emotion_stats.setdefault(emo, []).append(r)
    emotion_stats = {k: _stat(v) for k, v in emotion_stats.items()}

    # 按结果分类（涨停/炸板/未涨停）
    outcome_stats = {"涨停": [], "涨停炸板": [], "未涨停": []}
    for r in scoped:
        if r.get("is_limit_up"):
            outcome_stats["涨停"].append(r)
        elif r.get("is_zhaban"):
            outcome_stats["涨停炸板"].append(r)
        else:
            outcome_stats["未涨停"].append(r)
    outcome_stats = {k: _stat(v) for k, v in outcome_stats.items() if v}

    # 按板块
    by_industry = {}
    for r in scoped:
        ind = r.get("industry", "未知") or "未知"
        by_industry.setdefault(ind, []).append(r)
    industry_stats = {k: _stat(v) for k, v in sorted(by_industry.items(), key=lambda x: -len(x[1]))}

    # 按是否周期股（周期股=归档时该股在TOP30 ranking中）
    cs_groups = {"周期股": [], "非周期股": []}
    for r in scoped:
        if r.get("is_cycle_stock"):
            cs_groups["周期股"].append(r)
        elif r.get("is_cycle_stock") is False:
            cs_groups["非周期股"].append(r)
        # is_cycle_stock 为 None 的旧数据不计入
    cycle_stock_stats = {k: _stat(v) for k, v in cs_groups.items() if v}

    result = {
        "total": _stat(judged),
        "weekly": _stat(weekly),
        "monthly": _stat(monthly),
        "yearly": _stat(yearly),
        "by_board": board_stats,
        "by_emotion": emotion_stats,
        "by_outcome": outcome_stats,
        "by_industry": industry_stats,
        "by_cycle_stock": cycle_stock_stats,
    }
    result["suggestion"] = gen_open_suggestion(result)
    return result


def refresh_today_records() -> int:
    """对今日已归档记录做一次"事后修正"：

    - 用最新K线刷新 close_price / day_change / close_gain / gain_10d / is_limit_up / is_zhaban
    - 用最新 latest_sentiment / latest_leader 刷新 weighted_auction_gain / market_limit_down / yesterday_lianban_today_avg
      （与首页看板对齐，避免9:27归档时点过早导致数值偏差）
    - 用最新 latest_ranking TOP30 刷新 is_cycle_stock

    幂等可重复调用。返回更新的记录数。
    """
    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    today_recs = [r for r in records if r["date"] == today]
    if not today_recs:
        return 0

    # 1) 市场环境（与首页保持同源）
    env = _load_daily_market_env()

    # 2) TOP30 周期股集合 + 周期计数（streak）映射
    top30_codes = _load_top30_codes()
    streak_map = _load_top30_streak_map()

    # 3) K线刷新每只标的的当日数据
    try:
        from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
    except Exception:
        fetch_kline = None
        SCALE_DAILY = None

    updated = 0
    for r in today_recs:
        code = r["code"]
        changed = False

        # 用最新市场环境覆盖（如果有值）
        # 封板时间（涨停后才有）— 用 latest_review.lianban_ladder 兜底回填
        new_lbt = _lookup_limit_up_time(r.get("code", ""))
        if new_lbt and r.get("limit_up_time") != new_lbt:
            r["limit_up_time"] = new_lbt
            changed = True

        for k in ("market_limit_down", "weighted_auction_gain", "yesterday_lianban_today_avg",
                  "b1_rate", "concentration"):
            v = env.get(k)
            if v is not None and r.get(k) != v:
                r[k] = v
                changed = True

        # is_cycle_stock
        new_cs = str(code) in top30_codes
        if r.get("is_cycle_stock") != new_cs:
            r["is_cycle_stock"] = new_cs
            changed = True

        # top30_streak（周期计数）：仅当今日在 TOP30 且 streak_map 有值时刷新；否则保留原值
        new_streak = streak_map.get(str(code))
        if new_streak is not None and r.get("top30_streak") != new_streak:
            r["top30_streak"] = new_streak
            changed = True

        # K线：当日close + 10日涨幅
        if fetch_kline is not None:
            try:
                df = fetch_kline(code, SCALE_DAILY, datalen=12)
                if df is not None and len(df) >= 1:
                    last_date = str(df.iloc[-1]["date"])[:10]
                    if last_date == today:
                        kline_close = float(df.iloc[-1]["close"])
                        kline_high = float(df.iloc[-1]["high"]) if "high" in df.columns else kline_close
                        if kline_close > 0:
                            open_p = r.get("open_price", 0) or 0
                            pre_close = r.get("pre_close", 0) or 0
                            new_close_price = round(kline_close, 2)
                            new_close_gain = round((kline_close / open_p - 1) * 100, 2) if open_p > 0 else 0
                            new_day_change = round((kline_close / pre_close - 1) * 100, 2) if pre_close > 0 else new_close_gain
                            if r.get("close_price") != new_close_price:
                                r["close_price"] = new_close_price
                                changed = True
                            if r.get("close_gain") != new_close_gain:
                                r["close_gain"] = new_close_gain
                                changed = True
                            if r.get("day_change") != new_day_change:
                                r["day_change"] = new_day_change
                                changed = True
                            # 涨停/炸板判定（基于实际day_change和最高价）
                            limit_thr = 19.5 if str(code).startswith(("300", "301", "688")) else 9.8
                            new_lu = new_day_change >= limit_thr
                            new_zb = (not new_lu) and (kline_high / pre_close - 1) * 100 >= limit_thr if pre_close > 0 else False
                            if r.get("is_limit_up") != new_lu:
                                r["is_limit_up"] = new_lu
                                changed = True
                            if r.get("is_zhaban") != new_zb:
                                r["is_zhaban"] = new_zb
                                changed = True
                            # 重新判胜负（仅状态≤closed时；settled保持）
                            if r.get("status") in ("pending", "closed"):
                                if not new_lu:
                                    r["is_win"] = False
                                else:
                                    # 涨停且未settled → 等待次日数据
                                    pass
                        # 10日涨幅：取倒数第10根（含今日为11根）
                        idx = max(0, len(df) - 11) if last_date == today else max(0, len(df) - 10)
                        base = float(df.iloc[idx]["close"])
                        if base > 0:
                            new_g10 = round((kline_close / base - 1) * 100, 2)
                            if r.get("gain_10d") != new_g10:
                                r["gain_10d"] = new_g10
                                changed = True
            except Exception:
                pass

        if changed:
            updated += 1

    if updated:
        _save(records)
    return updated


def gen_open_suggestion(stats: dict) -> dict:
    """基于多维实战数据，生成可执行的资深龙头选手视角指导。

    返回结构（前端按段渲染；旧字符串接口已废弃）：
        {
            "headline": "一句核心结论",
            "rules":    ["可执行规则", ...],
            "risks":    ["风险/规避项", ...],
            "notes":    ["数据质量/样本提示", ...],
        }

    策略思路（资深短线 / 龙头选手视角）：
        1. 样本量定可信度（<10 噪声大；10-30 参考；30+ 可信）
        2. 总胜率对比 50% 基准 + 与涨停成功率比对（信号→涨停的转化率）
        3. 连板甜点位置（哪一档胜率最高；首板/低位 vs 高位接力差异）
        4. 周期股 vs 非周期股（动能主线；偏离则示警）
        5. 市场情绪适配（弱势/强势市场下的子集胜率）
        6. 行业集中度（是否在抱主线）
        7. 炸板率（情绪反复信号）
    """
    headline = ""
    rules: list[str] = []
    risks: list[str] = []
    notes: list[str] = []

    total = stats.get("total", {}) or {}
    n = int(total.get("trades", 0) or 0)
    wr = float(total.get("win_rate", 0) or 0)
    wins = int(total.get("wins", 0) or 0)

    # ===== 样本量与可信度 =====
    if n == 0:
        return {
            "headline": "暂无样本，先按既定体系跑一段时间再回看",
            "rules": [], "risks": [], "notes": ["当前周期无已结算选股记录"],
        }
    if n < 10:
        notes.append(f"样本仅 {n} 笔，结论参考价值有限——把它当作方向，不要当作铁律")
    elif n < 30:
        notes.append(f"样本 {n} 笔，数据有方向性参考但置信区间宽，建议继续累积")
    else:
        notes.append(f"样本 {n} 笔，统计意义已较稳定")

    # ===== 总胜率定调 =====
    if wr >= 60:
        headline = f"整体胜率 {wr}%（{wins}/{n}）— 系统当前对路，保持节奏"
    elif wr >= 50:
        headline = f"整体胜率 {wr}%（{wins}/{n}）— 略胜基准，重点做高胜率子集放大优势"
    elif wr >= 40:
        headline = f"整体胜率 {wr}%（{wins}/{n}）— 接近基准，需要靠子集筛选拉开差距"
    elif wr >= 30:
        headline = f"整体胜率 {wr}%（{wins}/{n}）— 低于基准，先减仓+收紧筛选，再看修复"
    else:
        headline = f"整体胜率 {wr}%（{wins}/{n}）— 系统当前不顺，建议空仓观察、复盘信号"

    # ===== 涨停转化率（核心质量指标） =====
    by_outcome = stats.get("by_outcome", {}) or {}
    zt_count = (by_outcome.get("涨停") or {}).get("trades", 0)
    zb_count = (by_outcome.get("涨停炸板") or {}).get("trades", 0)
    fail_count = (by_outcome.get("未涨停") or {}).get("trades", 0)
    if n >= 5:
        zt_rate = round(zt_count / n * 100, 1) if n else 0
        zb_rate = round(zb_count / n * 100, 1) if n else 0
        if zt_rate >= 50:
            rules.append(f"信号→涨停转化率 {zt_rate}%（{zt_count}/{n}），信号识别强，按既定标准开仓即可")
        elif zt_rate >= 30:
            rules.append(f"信号→涨停转化率 {zt_rate}%（{zt_count}/{n}），中等水平，尽量在竞价 +3~+6% 区间介入")
        else:
            risks.append(f"信号→涨停转化率仅 {zt_rate}%（{zt_count}/{n}），多数标的没站上涨停——筛选条件偏宽，建议收紧前置（量比、板块强度）")
        if zb_rate >= 20:
            risks.append(f"涨停炸板率 {zb_rate}%（{zb_count}笔），情绪反复严重——开盘买入后设 1.5~2% 止损，破开盘价快出")

    # ===== 连板甜点 =====
    by_board = stats.get("by_board", {}) or {}
    qualified_boards = [(k, v) for k, v in by_board.items() if v.get("trades", 0) >= 3]
    if qualified_boards:
        sorted_boards = sorted(qualified_boards, key=lambda x: x[1]["win_rate"], reverse=True)
        best_b, best_v = sorted_boards[0]
        worst_b, worst_v = sorted_boards[-1]
        if best_v["win_rate"] >= 60:
            rules.append(f"连板甜点：{best_b} 胜率 {best_v['win_rate']}%（{best_v['wins']}/{best_v['trades']}），优先满仓做这档")
        elif best_v["win_rate"] >= 50:
            rules.append(f"连板偏好：{best_b} 胜率 {best_v['win_rate']}%（{best_v['wins']}/{best_v['trades']}）相对最优，仓位向其倾斜")
        if best_b != worst_b and worst_v["win_rate"] < 40:
            risks.append(f"避做 {worst_b}：{worst_v['win_rate']}%（{worst_v['wins']}/{worst_v['trades']}）属于亏损区，没把握就放弃这档")
        # 主战场告警：占比最大的档位胜率太低 — 这是体系性的问题
        main_b, main_v = max(qualified_boards, key=lambda x: x[1]["trades"])
        share = round(main_v["trades"] / n * 100) if n else 0
        if share >= 40 and main_v["win_rate"] < 35:
            risks.append(
                f"主战场失灵：{main_b} 占了 {share}% 仓位（{main_v['trades']}笔）但胜率仅 {main_v['win_rate']}%，"
                f"本月这档明显不顺，反思——是位置太低导致跟风资金不够，还是市场情绪不支持低位接力？"
                f"建议短期把比重切到更高板（3进4 / 4进5）或 TOP30 周期主线"
            )

    # ===== 周期股属性 =====
    by_cs = stats.get("by_cycle_stock", {}) or {}
    cs_y = by_cs.get("周期股") or {}
    cs_n = by_cs.get("非周期股") or {}
    cs_y_n = cs_y.get("trades", 0)
    cs_n_n = cs_n.get("trades", 0)
    if cs_y_n >= 3 and cs_n_n >= 3:
        diff = cs_y["win_rate"] - cs_n["win_rate"]
        if diff >= 15:
            rules.append(f"周期股优势明显：周期股 {cs_y['win_rate']}% vs 非周期 {cs_n['win_rate']}%（差 {diff:+.0f}pt），抱团 TOP30 主线")
        elif diff <= -15:
            risks.append(f"周期股拖后腿：{cs_y['win_rate']}% vs 非周期 {cs_n['win_rate']}%（差 {diff:+.0f}pt），主线哑火，转向超跌弹性股或题材妖股")
        elif abs(diff) < 8:
            notes.append(f"周期 vs 非周期：{cs_y['win_rate']}% vs {cs_n['win_rate']}% 接近，无明显主线偏好")
    elif cs_y_n >= 3 and cs_n_n < 3:
        notes.append(f"主要做周期股（{cs_y_n}笔），胜率 {cs_y['win_rate']}%；非周期样本不足无法对比")
    elif cs_n_n >= 3 and cs_y_n < 3:
        notes.append(f"主要做非周期股（{cs_n_n}笔），胜率 {cs_n['win_rate']}%；周期股样本不足无法对比")

    # ===== 市场情绪适配 =====
    by_emo = stats.get("by_emotion", {}) or {}
    strong = by_emo.get("强势(跌停≤3)") or {}
    weak = by_emo.get("弱势(跌停>7)") or {}
    if weak.get("trades", 0) >= 3:
        if weak["win_rate"] < 30:
            risks.append(f"弱势市场（跌停>7）{weak['trades']}笔胜率仅 {weak['win_rate']}%——这种环境直接空仓，等情绪修复")
        elif weak["win_rate"] < 45:
            risks.append(f"弱势市场（跌停>7）胜率 {weak['win_rate']}%（{weak['wins']}/{weak['trades']}），仓位降到 1/3 以下")
    if strong.get("trades", 0) >= 3 and strong["win_rate"] >= 60:
        rules.append(f"强势市场（跌停≤3）胜率 {strong['win_rate']}%（{strong['wins']}/{strong['trades']}），是主战场，加大仓位")

    # ===== 行业集中度 =====
    by_industry = stats.get("by_industry", {}) or {}
    sig_inds = [(k, v) for k, v in by_industry.items() if v.get("trades", 0) >= 3]
    if sig_inds:
        sig_inds_sorted = sorted(sig_inds, key=lambda x: x[1]["win_rate"], reverse=True)
        top_ind, top_v = sig_inds_sorted[0]
        if top_v["win_rate"] >= 60 and top_v["trades"] >= 3:
            rules.append(f"行业胜率最高：{top_ind} {top_v['win_rate']}%（{top_v['wins']}/{top_v['trades']}），抓主线就盯它")
        if len(sig_inds_sorted) >= 2:
            bot_ind, bot_v = sig_inds_sorted[-1]
            if bot_v["win_rate"] < 30 and bot_ind != top_ind:
                risks.append(f"行业埋点：{bot_ind} 胜率 {bot_v['win_rate']}%（{bot_v['wins']}/{bot_v['trades']}），近期不要碰")

    # ===== 兜底（数据稀疏时给方向性建议） =====
    if not rules and not risks:
        if wr >= 50:
            rules.append("当前数据未识别出显著子集优势——按既定 3 维度信号联动正常开仓，每只 1/4~1/3 仓")
        else:
            risks.append("当前胜率偏低且未识别出明显子集优势——半仓为限，破信号位坚决出，复盘是否情绪误判")

    return {
        "headline": headline,
        "rules": rules,
        "risks": risks,
        "notes": notes,
    }


def get_history(limit: int = 200, year: int = None, month: int = None) -> list[dict]:
    """获取选股记录（按日期降序）

    Args:
        limit: 0/None 表示不限
        year: 仅返回该年记录；None 不过滤
        month: 配合 year 仅返回该月；单独传 month 不生效
    """
    records = _load()
    if year:
        ystr = f"{int(year):04d}"
        if month:
            prefix = f"{ystr}-{int(month):02d}"
            records = [r for r in records if str(r.get("date", ""))[:7] == prefix]
        else:
            records = [r for r in records if str(r.get("date", ""))[:4] == ystr]
    records.sort(key=lambda r: r["date"], reverse=True)
    if limit and limit > 0:
        records = records[:limit]
    return records


def list_available_periods() -> list[dict]:
    """历史选股记录中出现过的 (年, 月) 组合 + 当前自然月，按年月降序

    保证当前月即使无选股命中也可被下拉选中（月初进入新月时本月直接可见）。
    """
    from src.config import now_cn
    records = _load()
    seen: set[tuple[int, int]] = set()
    for r in records:
        d = str(r.get("date", ""))
        if len(d) >= 7:
            try:
                seen.add((int(d[:4]), int(d[5:7])))
            except ValueError:
                continue
    # 当前自然月强制纳入
    n = now_cn()
    seen.add((n.year, n.month))
    return [{"year": y, "month": m} for (y, m) in sorted(seen, reverse=True)]

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
    # 晋级矩阵（按昨日板数分组，每组含晋级 + 失败）
    # [{prev_board, next_board, label, promoted: [...], failed: [...]}]
    prev_board_groups: list = field(default_factory=list)
    # 板块涨停统计 [{industry, count, leader: {code, name, board_count, change_pct}}]
    sector_zt_stats: list = field(default_factory=list)
    # 兼容字段：晋级失败扁平化列表
    failed_promotion_list: list = field(default_factory=list)
    # 次日观察池
    watch_pool: list = field(default_factory=list)      # [WatchCandidate]
    # 市场情绪小结
    market_summary: str = ""


_SPOT_CACHE = {"df": None, "ts": 0}


def _get_spot_cached(max_age_sec: int = 600):
    """daily_review 内部各子函数共享一份 sina spot 快照，避免重复拉取（~15s/次）"""
    import time
    now = time.time()
    if _SPOT_CACHE["df"] is None or (now - _SPOT_CACHE["ts"]) > max_age_sec:
        try:
            from src.data.sina_spot_api import fetch_a_share_list_sina
            df = fetch_a_share_list_sina()
            if df is not None and not df.empty:
                _SPOT_CACHE["df"] = df
                _SPOT_CACHE["ts"] = now
        except Exception as e:
            print(f"[复盘] spot 缓存拉取失败: {e}")
    return _SPOT_CACHE["df"]


def run_daily_review() -> Optional[DailyReview]:
    """执行盘后复盘"""
    today = now_cn().strftime("%Y-%m-%d")
    print(f"[复盘] {today} 开始...")
    # 预热 spot 缓存
    _get_spot_cached()

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

    # 4.5 晋级矩阵：按昨日板数分组，每组含 晋级 + 失败
    review.prev_board_groups = _build_prev_board_groups(limit_up_data, lianban_data)
    # 4.6 板块涨停统计 + 龙头
    review.sector_zt_stats = _build_sector_zt_stats(lianban_data)
    # 兼容字段：扁平化失败列表
    review.failed_promotion_list = _get_failed_promotion_list(limit_up_data)

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


def _build_sector_zt_stats(lianban_data: list[dict]) -> list[dict]:
    """按板块统计今日涨停数 + 龙头（连板数最高，同板数取涨幅）"""
    if not lianban_data:
        return []
    by_industry: dict[str, list] = {}
    for s in lianban_data:
        ind = s.get("industry") or "-"
        by_industry.setdefault(ind, []).append(s)

    result = []
    for ind, stocks in by_industry.items():
        stocks_sorted = sorted(
            stocks,
            key=lambda x: (-int(x.get("board_count", 1) or 1),
                           -float(x.get("change_pct", 0) or 0))
        )
        leader = stocks_sorted[0]
        result.append({
            "industry": ind,
            "count": len(stocks),
            "leader": {
                "code": leader["code"],
                "name": leader["name"],
                "board_count": leader.get("board_count", 1),
                "change_pct": leader.get("change_pct", 0),
                "is_main_board": leader.get("is_main_board", True),
            },
        })
    # 按涨停数降序，相同时按龙头板数
    result.sort(key=lambda r: (-r["count"], -r["leader"]["board_count"]))
    return result


def _build_prev_board_groups(today_limit_up: list[dict], lianban_data: list[dict]) -> list[dict]:
    """构造按昨日板数分组的晋级矩阵

    一个组 = 一个昨日板数梯队。组内包含：
      - promoted: 今日成功晋级（昨日 N 板 → 今日 N+1 板）
      - failed:   今日断板（昨日涨停今日没涨停）

    特殊组 prev_board=0 = 今日新进首板（昨日没涨停今日首次涨停）。

    Returns:
        [
          {prev_board, next_board, label, promoted: [...], failed: [...]},
          ...
          {prev_board: 0, label: "今日新进首板", promoted: [...新进首板], failed: []}
        ]
    """
    cache_file = DATA_DIR / "limit_up_cache.json"
    if not cache_file.exists():
        return []
    try:
        cache = json.loads(cache_file.read_text())
    except Exception:
        return []

    today_str = now_cn().strftime("%Y%m%d")
    sorted_dates = sorted(cache.keys(), reverse=True)
    past_dates = [d for d in sorted_dates if d != today_str]
    if not past_dates:
        return []
    yesterday_str = past_dates[0]

    yesterday_pool = {str(r.get("code", "")): r for r in cache.get(yesterday_str, [])}
    today_codes = {str(r.get("code", "")) for r in (today_limit_up or [])}
    today_lianban_map = {str(s["code"]): s for s in lianban_data}

    if not yesterday_pool and not today_lianban_map:
        return []

    # 行业 + 计算昨日各股板数
    industry_map: dict = {}
    try:
        ic = DATA_DIR / "industry_cache.json"
        if ic.exists():
            industry_map = json.loads(ic.read_text())
    except Exception:
        pass

    def _calc_prev_board(code: str) -> int:
        """计算昨日板数（含昨日往前回溯）"""
        if code not in yesterday_pool:
            return 0
        bc = 1
        for d in past_dates[1:]:
            day_codes = [str(r.get("code", "")) for r in cache.get(d, [])]
            if code in day_codes:
                bc += 1
            else:
                break
        return bc

    # 拉今日全市场 spot 用于 failed 算 today_pct
    spot_map = {}
    try:
        spot = _get_spot_cached()
        if spot is not None and not spot.empty:
            for _, r in spot.iterrows():
                spot_map[str(r["code"])] = r
    except Exception as e:
        print(f"[复盘] 晋级矩阵 spot 取用失败: {e}")

    def _is_main(code: str) -> bool:
        return not str(code).startswith(("300", "301", "688", "8", "4"))

    def _classify(pct: float) -> str:
        if pct <= -9.5:
            return "跌停"
        if pct <= -5:
            return "深水"
        if pct < -1:
            return "绿盘"
        if pct <= 1:
            return "平开"
        return "炸板"

    def _today_pct(code: str) -> tuple[float, float]:
        row = spot_map.get(code)
        if row is None:
            return 0.0, 0.0
        try:
            close = float(row.get("close", 0) or 0)
            pre = float(row.get("pre_close", 0) or 0)
            if close > 0 and pre > 0:
                return round(close, 2), round((close / pre - 1) * 100, 2)
        except (ValueError, TypeError):
            pass
        return 0.0, 0.0

    # ---- 组装：晋级 vs 失败，按昨日板数分组 ----
    groups_by_prev: dict = {}

    # 1) 昨日涨停股遍历：在 today_codes → 晋级；不在 → 失败
    for code, info in yesterday_pool.items():
        prev_b = _calc_prev_board(code)
        grp = groups_by_prev.setdefault(prev_b, {"promoted": [], "failed": []})

        name = info.get("name", "")
        is_main = _is_main(code)
        ind = industry_map.get(code, "-")
        if code in today_codes:
            today_info = today_lianban_map.get(code, {})
            grp["promoted"].append({
                "code": code,
                "name": name,
                "today_board": today_info.get("board_count", prev_b + 1),
                "today_pct": today_info.get("change_pct", 0),
                "industry": ind,
                "is_main_board": is_main,
                "lbt": today_info.get("lbt", ""),
                "is_flat": today_info.get("is_flat", False),
            })
        else:
            close, pct = _today_pct(code)
            grp["failed"].append({
                "code": code,
                "name": name,
                "today_close": close,
                "today_pct": pct,
                "status": _classify(pct),
                "industry": ind,
                "is_main_board": is_main,
            })

    # 2) 今日新进首板：今日涨停 但 不在昨日池
    new_first = []
    for code in today_codes:
        if code in yesterday_pool:
            continue
        info = today_lianban_map.get(code, {})
        new_first.append({
            "code": code,
            "name": info.get("name", ""),
            "today_board": 1,
            "today_pct": info.get("change_pct", 0),
            "industry": industry_map.get(code, "-"),
            "is_main_board": _is_main(code),
            "lbt": info.get("lbt", ""),
            "is_flat": info.get("is_flat", False),
        })
    new_first.sort(key=lambda s: -float(s.get("today_pct", 0) or 0))

    # ---- 输出 ----
    result = []
    # 按昨日板数从高到低
    for prev_b in sorted(groups_by_prev.keys(), reverse=True):
        grp = groups_by_prev[prev_b]
        # promoted 按今日涨幅倒序；failed 按今日跌幅升序
        grp["promoted"].sort(key=lambda s: -float(s.get("today_pct", 0) or 0))
        grp["failed"].sort(key=lambda s: float(s.get("today_pct", 0) or 0))
        label = f"昨日{prev_b}板" if prev_b > 1 else "昨日首板"
        result.append({
            "prev_board": prev_b,
            "next_board": prev_b + 1,
            "label": label,
            "promoted": grp["promoted"],
            "failed": grp["failed"],
        })

    # 新进首板单独成组（prev_board=0）
    if new_first:
        result.append({
            "prev_board": 0,
            "next_board": 1,
            "label": "今日新进首板",
            "promoted": new_first,
            "failed": [],
        })

    return result


def _get_failed_promotion_list(today_limit_up: list[dict]) -> list[dict]:
    """晋级失败列表：昨日涨停但今日没涨停的股票

    Returns:
        [{code, name, prev_board, today_pct, today_close, status, industry, is_main_board}]
        status: '炸板' / '平开' / '绿盘' / '跌停' / '深水'
    """
    cache_file = DATA_DIR / "limit_up_cache.json"
    if not cache_file.exists():
        return []
    try:
        cache = json.loads(cache_file.read_text())
    except Exception:
        return []

    today_str = now_cn().strftime("%Y%m%d")
    all_dates = sorted(cache.keys(), reverse=True)
    past_dates = [d for d in all_dates if d != today_str]
    if not past_dates:
        return []
    yesterday_str = past_dates[0]

    yesterday_pool = {str(r.get("code", "")): r for r in cache.get(yesterday_str, [])}
    if not yesterday_pool:
        return []
    today_codes = {str(r.get("code", "")) for r in (today_limit_up or [])}

    # 昨日涨停 - 今日涨停 = 晋级失败
    failed_codes = [c for c in yesterday_pool.keys() if c not in today_codes]
    if not failed_codes:
        return []

    # 行业 cache
    industry_map: dict = {}
    try:
        ic = DATA_DIR / "industry_cache.json"
        if ic.exists():
            industry_map = json.loads(ic.read_text())
    except Exception:
        pass

    # 计算昨日各股的"昨日板数"（用 lianban_history）
    prev_board_map: dict = {}
    for code in failed_codes:
        bc = 1
        for d in past_dates[1:]:
            day_codes = [str(r.get("code", "")) for r in cache.get(d, [])]
            if code in day_codes:
                bc += 1
            else:
                break
        prev_board_map[code] = bc

    # 拉今日全市场 spot 算 close + change_pct
    spot_map = {}
    try:
        spot = _get_spot_cached()
        if spot is not None and not spot.empty:
            for _, r in spot.iterrows():
                spot_map[str(r["code"])] = r
    except Exception as e:
        print(f"[复盘] 晋级失败列表 spot 取用失败: {e}")

    def _classify(pct: float) -> str:
        if pct <= -9.5:
            return "跌停"
        if pct <= -5:
            return "深水"
        if pct < -1:
            return "绿盘"
        if pct <= 1:
            return "平开"
        return "炸板"  # 涨但未涨停 — 高位没封住

    result = []
    for code in failed_codes:
        info = yesterday_pool.get(code, {})
        name = info.get("name", "")
        is_main = not code.startswith(("300", "301", "688", "8", "4"))
        row = spot_map.get(code)
        if row is None:
            today_close = 0
            today_pct = 0
            status = "无数据"
        else:
            try:
                today_close = float(row.get("close", 0) or 0)
                pre_close = float(row.get("pre_close", 0) or 0)
                today_pct = round((today_close / pre_close - 1) * 100, 2) if pre_close > 0 else 0
            except (ValueError, TypeError):
                today_close, today_pct = 0, 0
            status = _classify(today_pct)
        result.append({
            "code": code,
            "name": name,
            "prev_board": prev_board_map.get(code, 1),
            "today_close": round(today_close, 2),
            "today_pct": today_pct,
            "status": status,
            "industry": industry_map.get(code, "-"),
            "is_main_board": is_main,
        })

    # 按今日跌幅升序（最惨的在前 — 用户想看"翻车"程度）
    result.sort(key=lambda s: s["today_pct"])
    return result


def _get_lianban_ladder() -> list[dict]:
    """获取连板梯队（含 industry 板块 + 封板时间 + 一字标记）"""
    cache_file = DATA_DIR / "limit_up_cache.json"
    if not cache_file.exists():
        return []

    # 行业映射
    industry_map: dict = {}
    try:
        ic = DATA_DIR / "industry_cache.json"
        if ic.exists():
            industry_map = json.loads(ic.read_text())
    except Exception:
        pass

    try:
        cache = json.loads(cache_file.read_text())
        sorted_dates = sorted(cache.keys(), reverse=True)
        if not sorted_dates:
            return []

        latest_date = sorted_dates[0]
        latest_stocks = {r.get("code", ""): r for r in cache.get(latest_date, [])}

        # 拉今日 zt_pool 拿封板时间（lbt）
        zt_pool: dict = {}
        try:
            from src.data.zt_pool_api import fetch_zt_pool
            zt_pool = fetch_zt_pool(latest_date) or {}
        except Exception as e:
            print(f"[复盘] 涨停池(封板时间) 拉取失败: {e}")

        # 拉今日 spot 算开盘价 → 判定一字板
        spot_map: dict = {}
        try:
            spot = _get_spot_cached()
            if spot is not None and not spot.empty:
                for _, r in spot.iterrows():
                    spot_map[str(r["code"])] = r
        except Exception as e:
            print(f"[复盘] 一字板判定 spot 取用失败: {e}")

        def _is_flat(code: str, is_main: bool) -> bool:
            row = spot_map.get(str(code))
            if row is None:
                return False
            try:
                op = float(row.get("open", 0) or 0)
                pc = float(row.get("pre_close", 0) or 0)
                if op <= 0 or pc <= 0:
                    return False
                pct = (op / pc - 1) * 100
                # 一字 = 开盘即触及涨停价
                return pct >= (9.7 if is_main else 19.4)
            except (ValueError, TypeError):
                return False

        result = []
        for code, info in latest_stocks.items():
            count = 1
            for d in sorted_dates[1:]:
                codes_in_day = [r.get("code", "") for r in cache.get(d, [])]
                if code in codes_in_day:
                    count += 1
                else:
                    break

            is_main = not code.startswith(("300", "301", "688", "8", "4"))
            zt_info = zt_pool.get(code, {}) or zt_pool.get(str(code).zfill(6), {})
            result.append({
                "code": code,
                "name": info.get("name", ""),
                "board_count": count,
                "is_main_board": is_main,
                "change_pct": info.get("change_pct", 0),
                "industry": industry_map.get(code, "-"),
                "lbt": zt_info.get("lbt", ""),       # 最后封板时间 HH:MM:SS
                "is_flat": _is_flat(code, is_main),  # 一字板
            })

        result.sort(key=lambda x: (-x["board_count"], -x.get("change_pct", 0)))
        return result

    except Exception as e:
        print(f"[复盘] 连板梯队构造失败: {e}")
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
    """生成次日观察池（最多3只主板2连板以上 · 流通市值<100亿）"""
    # 筛选：主板、2连板以上
    main_board_lianban = [
        s for s in lianban
        if s.get("is_main_board") and s.get("board_count", 0) >= 2
    ]
    if not main_board_lianban:
        return []

    # 给所有候选补齐 market_cap：先用 ranking 的字段，缺失再用腾讯接口批量拉
    pool_by_code = {s["code"]: dict(s) for s in main_board_lianban}
    missing_mc = []
    for code, s in pool_by_code.items():
        r = ranking.get(code, {})
        mc = r.get("market_cap_yi", 0)
        if mc and mc > 0:
            s["_market_cap_yi"] = mc
            s["_close"] = r.get("close", 0)
            s["_industry"] = r.get("industry", "未知")
            s["_gain_10d"] = r.get("gain_10d", 0)
        else:
            missing_mc.append(code)

    if missing_mc:
        try:
            from src.data.tencent_api import fetch_stock_details
            df = fetch_stock_details(missing_mc)
            if df is not None and not df.empty:
                df["code"] = df["code"].astype(str)
                for _, row in df.iterrows():
                    code = str(row["code"])
                    if code in pool_by_code:
                        pool_by_code[code]["_market_cap_yi"] = float(row.get("market_cap_yi", 0) or 0)
                        pool_by_code[code]["_close"] = float(row.get("close", 0) or 0)
                        pool_by_code[code].setdefault("_industry", "未知")
                        pool_by_code[code].setdefault("_gain_10d", 0)
        except Exception as e:
            print(f"[复盘] 鱼塘市值补齐失败（保留有数据的候选）: {e}")

    # 严格过滤：必须有市值 且 < 100亿
    filtered = [
        s for s in pool_by_code.values()
        if s.get("_market_cap_yi", 0) > 0 and s["_market_cap_yi"] < 100
    ]

    # 维持原排序：按连板数 desc + change_pct desc
    filtered.sort(
        key=lambda s: (-s.get("board_count", 0), -s.get("change_pct", 0))
    )

    candidates = []
    for s in filtered[:5]:
        candidates.append(asdict(WatchCandidate(
            code=s["code"],
            name=s["name"],
            board_count=s["board_count"],
            industry=s.get("_industry", "未知"),
            close=s.get("_close", 0),
            market_cap_yi=s.get("_market_cap_yi", 0),
            total_gain_pct=s.get("_gain_10d", 0),
            reason=_generate_reason(s, review),
            watch_points=_generate_watch_points(s, review),
            auction_range=_calc_auction_range(s.get("_close", 0)),
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

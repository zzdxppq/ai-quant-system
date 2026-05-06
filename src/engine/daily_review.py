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
    # 概念涨停统计（一对多聚合，一股可同时支撑多个题材）
    # [{name, limit_up_count, max_board, top_stock_name, ladder:{board:count}, ...}]
    concept_zt_stats: list = field(default_factory=list)
    # 兼容字段：晋级失败扁平化列表
    failed_promotion_list: list = field(default_factory=list)
    # 次日观察池
    watch_pool: list = field(default_factory=list)      # [WatchCandidate]
    # 市场情绪小结
    market_summary: str = ""

    # ========== 接力专项（针对2板以上接力策略）==========
    # 区域 A：接力环境评分卡 + 一句话决策
    # {
    #   "indicators": [{label, today, target, score, raw_today}, ...5 项],
    #   "total_score": 0..5,
    #   "decision": "重仓"/"正常"/"试错"/"空仓",
    #   "decision_color": "...",
    #   "headline": "一句话决策建议 ..."
    # }
    scorecard: dict = field(default_factory=dict)
    # 区域 B：资金接力情绪
    # { space_board: {...}, prev_space_board_today: ±%, ladder_distribution: {2:N,3:N,...},
    #   accelerating_count, decelerating_count, yesterday_zb_today_avg_pct }
    relay_env: dict = field(default_factory=dict)
    # 区域 C：晋级矩阵摘要
    # [{label:"1进2", success, total, success_rate, fail_avg_pct}, ...]
    promotion_summary: list = field(default_factory=list)


_SPOT_CACHE = {"df": None, "ts": 0}


def _get_spot_cached(max_age_sec: int = 600):
    """daily_review 内部各子函数共享一份 spot 快照（走 fetcher 多源兜底）"""
    import time
    now = time.time()
    if _SPOT_CACHE["df"] is not None and (now - _SPOT_CACHE["ts"]) <= max_age_sec:
        return _SPOT_CACHE["df"]
    df = None
    try:
        from src.data.fetcher import fetch_realtime_spot
        df = fetch_realtime_spot()
    except Exception as e:
        print(f"[复盘] fetcher.fetch_realtime_spot 失败: {e}")
    if df is None or df.empty or len(df) < 100:
        # 降级：直接走新浪
        try:
            from src.data.sina_spot_api import fetch_a_share_list_sina
            sina_df = fetch_a_share_list_sina()
            if sina_df is not None and not sina_df.empty and (df is None or df.empty or len(sina_df) > len(df)):
                df = sina_df
        except Exception as e:
            print(f"[复盘] 新浪 spot 兜底失败: {e}")
    if df is not None and not df.empty:
        _SPOT_CACHE["df"] = df
        _SPOT_CACHE["ts"] = now
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
    # 4.6b 概念涨停统计（一对多聚合，最强概念梯队展示）
    review.concept_zt_stats = _build_concept_zt_stats(lianban_data)
    # 兼容字段：扁平化失败列表
    review.failed_promotion_list = _get_failed_promotion_list(limit_up_data)

    # 4.7 接力专项数据（区域 B/C/A 顺序计算：B 依赖梯队、C 依赖矩阵、A 综合）
    review.relay_env = _build_relay_env(review, review.prev_board_groups, lianban_data)
    review.promotion_summary = _build_promotion_summary(review.prev_board_groups)
    review.scorecard = _build_scorecard(
        review.prev_board_groups, review.relay_env,
        review.sector_zt_stats, review.limit_up_count,
        concept_zt_stats=review.concept_zt_stats,
        highest_board=review.highest_board,
    )

    # 5. 生成次日观察池（严格 3 条件）
    review.watch_pool = _generate_watch_pool(lianban_data, ranking_data, review)

    # 5.5 各 stock 节点注入 top_concepts（晋级矩阵 / space_board / watch_pool / 天梯）
    _enrich_review_top_concepts(review, lianban_data)

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


def _enrich_review_top_concepts(review, lianban_data: list[dict]) -> None:
    """为复盘各节点（晋级矩阵 / space_board / watch_pool）注入 top_concepts。

    数据源：lianban_data 已含 concepts；按全市场涨停聚合热度选 top 2。
    """
    try:
        from src.engine.concept_stats import (
            aggregate_concept_limit_ups, top_concepts_for_stock,
        )
        from src.data.concept_fetcher import load_stock_to_concepts
    except Exception:
        return

    c_map = load_stock_to_concepts() or {}
    if not c_map and not lianban_data:
        return

    heats = aggregate_concept_limit_ups(lianban_data, c_map)

    def pick(code: str) -> list[str]:
        cs = list(c_map.get(str(code)) or [])
        return top_concepts_for_stock(cs, heats, top_n=2)

    # 1) 晋级矩阵
    for grp in (review.prev_board_groups or []):
        for s in grp.get("promoted", []) + grp.get("failed", []):
            s["top_concepts"] = pick(s.get("code", ""))

    # 2) relay_env.space_board / prev_space_board
    relay = review.relay_env or {}
    sb = relay.get("space_board")
    if isinstance(sb, dict) and sb.get("code"):
        sb["top_concepts"] = pick(sb["code"])

    # 3) watch_pool（次日观察池每条候选）
    for w in (review.watch_pool or []):
        if isinstance(w, dict) and w.get("code"):
            w["top_concepts"] = pick(w["code"])

    # 4) lianban_ladder 自身（display 用）
    for s in (review.lianban_ladder or []):
        if isinstance(s, dict) and s.get("code"):
            s["top_concepts"] = pick(s["code"])


def _build_concept_zt_stats(lianban_data: list[dict]) -> list[dict]:
    """按概念聚合今日涨停 — 一对多映射，一股贡献给所有所属概念

    Returns: 按 (limit_up_count desc, max_board desc) 排序的概念热度列表
        每项: {name, limit_up_count, max_board, top_stock_code, top_stock_name,
               ladder:{board:count}, limit_up_codes/names, avg_change_pct}
    """
    if not lianban_data:
        return []
    try:
        from src.engine.concept_stats import aggregate_concept_limit_ups, serialize_heat
        heats = aggregate_concept_limit_ups(lianban_data)
        return [serialize_heat(h) for h in heats]
    except Exception as e:
        print(f"[复盘] 概念涨停聚合失败: {e}")
        return []


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

    def _classify(pct: float | None) -> str:
        if pct is None:
            return "数据缺失"
        if pct <= -9.5:
            return "跌停"
        if pct <= -5:
            return "深水"
        if pct < -1:
            return "绿盘"
        if pct <= 1:
            return "平开"
        return "炸板"

    def _today_pct(code: str) -> tuple[float | None, float | None]:
        """缺数据时返回 (None, None)，区分真平开（0%）与缺失"""
        row = spot_map.get(code)
        if row is None:
            return None, None
        try:
            close = float(row.get("close", 0) or 0)
            pre = float(row.get("pre_close", 0) or 0)
            if close > 0 and pre > 0:
                return round(close, 2), round((close / pre - 1) * 100, 2)
        except (ValueError, TypeError):
            pass
        return None, None

    def _today_open_pct(code: str) -> float | None:
        """今日开盘相对昨收的涨跌幅；缺数据返回 None，区分真平开"""
        row = spot_map.get(code)
        if row is None:
            return None
        try:
            op = float(row.get("open", 0) or 0)
            pre = float(row.get("pre_close", 0) or 0)
            if op > 0 and pre > 0:
                return round((op / pre - 1) * 100, 2)
        except (ValueError, TypeError):
            pass
        return None

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
                "today_open_pct": _today_open_pct(code),
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
            "today_open_pct": _today_open_pct(code),
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
    """获取连板梯队（含 industry 板块 + concepts 概念 + 封板时间 + 一字标记）"""
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

    # 概念映射（一对多）
    concept_map: dict[str, list[str]] = {}
    try:
        from src.data.concept_fetcher import load_stock_to_concepts
        concept_map = load_stock_to_concepts() or {}
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
                "concepts": list(concept_map.get(code) or []),
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
    """次日观察池入口（向后兼容包装）— 复用 build_watch_pool_from_ranking"""
    return build_watch_pool_from_ranking(ranking)


def build_watch_pool_from_ranking(ranking: dict | list) -> list[dict]:
    """次日观察池（从10日涨幅榜 top30 中筛主板高位连板）

    条件（必须全部满足）：
    1. 当前在 10日涨幅榜 top30 中
    2. gain_10d >= 45%
    3. continuous_limit_up >= 2（连续两日及以上涨停）
    4. is_main_board（主板）

    Args:
        ranking: {code: ranking_row} 或 [ranking_row, ...]
                 row 含 gain_10d / continuous_limit_up / is_main_board /
                       market_cap_yi / industry / concepts / close
    """
    if not ranking:
        return []

    rows = ranking.values() if isinstance(ranking, dict) else ranking

    qualified: list[dict] = []
    for r in rows:
        gain = float(r.get("gain_10d") or 0)
        clu = int(r.get("continuous_limit_up") or 0)
        is_main = bool(r.get("is_main_board"))
        if gain < 45 or clu < 2 or not is_main:
            continue
        qualified.append(dict(r))

    if not qualified:
        return []

    # 排序：连板数 desc → 10日涨幅 desc
    qualified.sort(
        key=lambda s: (
            -int(s.get("continuous_limit_up") or 0),
            -float(s.get("gain_10d") or 0),
        ),
    )

    out: list[dict] = []
    for s in qualified:
        ind = s.get("industry") or "未知"
        candidate = asdict(WatchCandidate(
            code=str(s.get("code", "")),
            name=s.get("name", ""),
            board_count=int(s.get("continuous_limit_up") or 0),
            industry=ind,
            close=float(s.get("close") or 0),
            market_cap_yi=float(s.get("market_cap_yi") or 0),
            total_gain_pct=float(s.get("gain_10d") or 0),
            reason=(
                f"{s.get('continuous_limit_up')}连板·10日涨幅 "
                f"{s.get('gain_10d')}%·主板"
            ),
            watch_points="竞价 4~7.5% 介入；竞价回落破开盘价不接；同板块跟风缩量则减仓",
            auction_range=_calc_auction_range(float(s.get("close") or 0)),
        ))
        # 透传 top_concepts / is_main_board 给前端展示
        candidate["top_concepts"] = list(s.get("top_concepts") or [])
        candidate["is_main_board"] = bool(s.get("is_main_board", True))
        out.append(candidate)
    return out


def _lbt_to_sec(lbt: str) -> int:
    """HH:MM:SS → 秒；空串/格式不对返回 0"""
    try:
        parts = lbt.strip().split(":")
        if len(parts) >= 2:
            h, m = int(parts[0]), int(parts[1])
            s = int(parts[2]) if len(parts) >= 3 else 0
            return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        pass
    return 0


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


def _build_promotion_summary(prev_board_groups: list[dict]) -> list[dict]:
    """晋级矩阵摘要（区域 C）：每一档 N→N+1 的成功率 + 失败票今日均价。"""
    summary = []
    for g in prev_board_groups:
        pb = g.get("prev_board", 0)
        if pb == 0:
            continue  # 新进首板不算晋级
        promoted = g.get("promoted") or []
        failed = g.get("failed") or []
        total = len(promoted) + len(failed)
        if total == 0:
            continue
        rate = round(len(promoted) / total * 100, 1)
        # fail_avg：仅对有有效今日 close 的失败票求均值
        # 历史快照可能把缺数据存为 today_pct=0.0 / today_close=0，过滤掉
        valid_pcts: list[float] = []
        for s in failed:
            tp = s.get("today_pct")
            tc = s.get("today_close")
            if tp is None:
                continue
            if (tc is None) or (float(tc or 0) <= 0):
                continue
            try:
                valid_pcts.append(float(tp))
            except (TypeError, ValueError):
                continue
        fail_avg = round(sum(valid_pcts) / len(valid_pcts), 2) if valid_pcts else None
        # 失败票均跌幅 < -3% 或 success_rate < 40 → warn（fail_avg 缺失则跳过）
        warn = (pb == 2 and rate < 40) or (fail_avg is not None and fail_avg < -3.0)
        summary.append({
            "label": f"{pb}进{pb+1}",
            "success": len(promoted),
            "total": total,
            "success_rate": rate,
            "fail_avg_pct": fail_avg,
            "warn": warn,
        })
    return summary


def _build_relay_env(
    review: DailyReview,
    prev_board_groups: list[dict],
    lianban_data: list[dict],
) -> dict:
    """资金接力情绪（区域 B）"""
    # 1. 今日空间板（最高连板，多只取涨幅最高的）
    space_board = None
    if lianban_data:
        top_b = max((s.get("board_count", 0) for s in lianban_data), default=0)
        candidates = [s for s in lianban_data if s.get("board_count", 0) == top_b]
        if candidates:
            best = max(candidates, key=lambda s: s.get("change_pct", 0) or 0)
            space_board = {
                "code": best["code"], "name": best["name"],
                "board_count": top_b, "change_pct": best.get("change_pct", 0),
                "industry": best.get("industry", "-"),
            }

    # 2. 昨日空间板今日表现 — 从 review_history 取昨日 highest_board 那只
    prev_space = None
    try:
        hist_file = DATA_DIR / "review_history.json"
        if hist_file.exists():
            history = json.loads(hist_file.read_text())
            today = now_cn().strftime("%Y-%m-%d")
            past = [h for h in history if h.get("date") and h["date"] != today]
            if past:
                yhist = sorted(past, key=lambda h: h["date"])[-1]
                ylb = yhist.get("lianban_ladder") or []
                ymax = yhist.get("highest_board", 0)
                ytop = [s for s in ylb if s.get("board_count", 0) == ymax]
                if ytop:
                    ybest = max(ytop, key=lambda s: s.get("change_pct", 0) or 0)
                    yc = str(ybest.get("code", ""))
                    yn = ybest.get("name", "")
                    # 1) spot 算今日涨跌 + 开盘相对昨收（精确）
                    today_pct = None
                    today_open_pct = None
                    today_zt = False
                    try:
                        spot = _get_spot_cached()
                        if spot is not None and not spot.empty:
                            row = spot[spot["code"].astype(str) == yc]
                            if not row.empty:
                                close = float(row.iloc[0].get("close", 0) or 0)
                                pre = float(row.iloc[0].get("pre_close", 0) or 0)
                                op = float(row.iloc[0].get("open", 0) or 0)
                                if close > 0 and pre > 0:
                                    today_pct = round((close / pre - 1) * 100, 2)
                                    is_main = not yc.startswith(("300", "301", "688", "8", "4"))
                                    thr = 9.7 if is_main else 19.4
                                    today_zt = today_pct >= thr
                                if op > 0 and pre > 0:
                                    today_open_pct = round((op / pre - 1) * 100, 2)
                    except Exception:
                        pass
                    # 2) spot 拿不到 → 回退到 limit_up_cache 判断今日是否仍涨停
                    if today_pct is None:
                        try:
                            cf = DATA_DIR / "limit_up_cache.json"
                            if cf.exists():
                                cache = json.loads(cf.read_text())
                                today_str = now_cn().strftime("%Y%m%d")
                                if today_str in cache:
                                    today_codes_set = {str(r.get("code", "")) for r in cache[today_str]}
                                    if yc in today_codes_set:
                                        today_zt = True
                                        today_pct = 9.99  # 占位（涨停）
                        except Exception:
                            pass
                    prev_space = {
                        "code": yc, "name": yn,
                        "yesterday_board": ymax,
                        "today_pct": today_pct,
                        "today_open_pct": today_open_pct,
                        "today_held": today_zt,
                    }
    except Exception as e:
        print(f"[复盘] 昨日空间板取数失败: {e}")

    # 3. 连板梯队完整性（按板数分布）
    ladder_dist = {}
    for s in lianban_data:
        bc = s.get("board_count", 1)
        if bc >= 2:
            ladder_dist[bc] = ladder_dist.get(bc, 0) + 1

    # 4. 加速/减速 — 来自 latest_insight.json
    acc, dec = 0, 0
    try:
        insight_file = DATA_DIR / "latest_insight.json"
        if insight_file.exists():
            ins = json.loads(insight_file.read_text())
            acc = int(ins.get("accelerating_count", 0) or 0)
            dec = int(ins.get("decelerating_count", 0) or 0)
    except Exception:
        pass

    # 5. 昨日炸板今日均价 — 来自 latest_leader.yesterday_zb_today_auction.avg_change_pct
    zb_avg = None
    try:
        leader_file = DATA_DIR / "latest_leader.json"
        if leader_file.exists():
            ld = json.loads(leader_file.read_text())
            zb = ld.get("yesterday_zb_today_auction") or {}
            v = zb.get("avg_change_pct")
            if v is not None:
                zb_avg = round(float(v), 2)
    except Exception:
        pass

    return {
        "space_board": space_board,
        "prev_space_board_today": prev_space,
        "ladder_distribution": ladder_dist,
        "accelerating_count": acc,
        "decelerating_count": dec,
        "yesterday_zb_today_avg_pct": zb_avg,
    }


def _build_scorecard(
    prev_board_groups: list[dict],
    relay_env: dict,
    sector_zt_stats: list[dict],
    limit_up_count: int,
    concept_zt_stats: list[dict] | None = None,
    highest_board: int = 0,
) -> dict:
    """接力环境评分卡（区域 A）"""
    # === 指标 1: 1进2 成功率 ≥40% ===
    g1 = next((g for g in prev_board_groups if g.get("prev_board") == 1), None)
    if g1:
        b1_promoted = len(g1.get("promoted") or [])
        b1_total = b1_promoted + len(g1.get("failed") or [])
        b1_rate = round(b1_promoted / b1_total * 100, 1) if b1_total else 0.0
    else:
        b1_promoted, b1_total, b1_rate = 0, 0, 0.0
    score_1 = 1 if b1_rate >= 40 else 0

    # === 指标 2: 2进3 成功率 ≥40% ===
    # 阈值与 _build_promotion_summary 中 2进3 warn 阈值一致（< 40% 即视为接力恶化）
    # 改用成功率而非"红盘晋级数"：避免 today_open_pct 数据质量影响判定
    g2 = next((g for g in prev_board_groups if g.get("prev_board") == 2), None)
    if g2:
        b2_promoted = len(g2.get("promoted") or [])
        b2_total = b2_promoted + len(g2.get("failed") or [])
        b2_rate = round(b2_promoted / b2_total * 100, 1) if b2_total else 0.0
    else:
        b2_promoted, b2_total, b2_rate = 0, 0, 0.0
    score_2 = 1 if b2_rate >= 40 else 0

    # === 指标 3: 板块集中度（前3概念覆盖的【独立】涨停股 / 总涨停数）≥50% ===
    # 一股多概念时只计一次，避免累加超 100%
    if concept_zt_stats and limit_up_count > 0:
        top3_codes: set[str] = set()
        for c in concept_zt_stats[:3]:
            for code in (c.get("limit_up_codes") or []):
                top3_codes.add(str(code))
        sec_concentration = round(len(top3_codes) / limit_up_count * 100, 1)
    elif sector_zt_stats and limit_up_count > 0:
        # 无概念数据兜底：旧 industry 路径
        top3_count = sum(s.get("count", 0) for s in sector_zt_stats[:3])
        sec_concentration = round(top3_count / limit_up_count * 100, 1)
    else:
        sec_concentration = 0.0
    score_3 = 1 if sec_concentration >= 50 else 0

    # === 指标 4: 空间板（红盘晋级 1.0 / 低开晋级 0.5 / 断板 0）===
    # 三级判定：今日涨停且开盘 ≥0% 为红盘强势；今日涨停但低开为犹豫；未涨停=断板
    prev_space = relay_env.get("prev_space_board_today") or {}
    space_held = bool(prev_space.get("today_held"))
    space_open_pct = prev_space.get("today_open_pct")
    if not space_held:
        score_4 = 0.0
        space_status_text = "断板" if prev_space else "无昨日数据"
    elif space_open_pct is None:
        # 涨停但开盘数据缺失：保守给 0.5
        score_4 = 0.5
        space_status_text = "晋级（开盘数据缺失）"
    elif space_open_pct >= 0:
        score_4 = 1.0
        space_status_text = f"红盘晋级（开{space_open_pct:+.1f}%）"
    else:
        score_4 = 0.5
        space_status_text = f"低开晋级（开{space_open_pct:+.1f}%）"

    # === 指标 5: 昨日连板指数今日涨跌幅 >+2% ===
    # 阈值改 2%：0~2% 是"赚不到钱"区间，必须正向溢价才算赚钱效应延续
    pcts = []
    for g in prev_board_groups:
        if g.get("prev_board", 0) >= 2:
            for s in (g.get("promoted") or []):
                tp = s.get("today_pct")
                if tp is not None:
                    pcts.append(float(tp))
            for s in (g.get("failed") or []):
                tp = s.get("today_pct")
                if tp is not None and (s.get("today_close") or 0) > 0:
                    pcts.append(float(tp))
    lianban_index_pct = round(sum(pcts) / len(pcts), 2) if pcts else 0.0
    score_5 = 1 if lianban_index_pct > 2 else 0

    # === 指标 6: 高度突破（今日最高板 vs 昨日最高板）===
    # 接力升温信号：高度有没有创新高
    prev_max = int(prev_space.get("yesterday_board") or 0)
    today_max = int(highest_board or 0)
    if today_max > prev_max:
        score_6, height_text = 1.0, f"{today_max}板↑（昨{prev_max}板）"
    elif today_max == prev_max and today_max > 0:
        score_6, height_text = 0.5, f"{today_max}板=（昨{prev_max}板）"
    else:
        score_6, height_text = 0.0, f"{today_max}板↓（昨{prev_max}板）"

    indicators = [
        {"label": "1进2成功率",   "today": f"{b1_rate}%",          "target": "≥40%", "score": score_1, "raw": b1_rate, "detail": f"{b1_promoted}/{b1_total}"},
        {"label": "2进3成功率",   "today": f"{b2_rate}%",          "target": "≥40%", "score": score_2, "raw": b2_rate, "detail": f"{b2_promoted}/{b2_total}"},
        {"label": "板块集中度",   "today": f"{sec_concentration}%","target": "≥50%", "score": score_3, "raw": sec_concentration, "detail": "前3概念涨停占比"},
        {"label": "空间板",       "today": space_status_text,      "target": "红盘晋级", "score": score_4, "raw": (space_open_pct if space_open_pct is not None else 0), "detail": (f"昨{prev_space.get('yesterday_board')}板{prev_space.get('name','')}" if prev_space else "—")},
        {"label": "昨日连板指数", "today": f"{lianban_index_pct:+.2f}%", "target": ">+2%", "score": score_5, "raw": lianban_index_pct, "detail": f"昨≥2板共{len(pcts)}只今日均值"},
        {"label": "高度突破",     "today": height_text,            "target": "高度创新", "score": score_6, "raw": today_max - prev_max, "detail": f"今日最高{today_max}板 / 昨日{prev_max}板"},
    ]
    total_score = round(sum(ind["score"] for ind in indicators), 1)
    # 6 项总分 0~6，含 0.5 fractional：≥5 重仓 / ≥4 正常 / ≥3 试错 / 否则空仓
    if total_score >= 5:
        decision, color = "重仓", "#ef4444"
    elif total_score >= 4:
        decision, color = "正常", "#10b981"
    elif total_score >= 3:
        decision, color = "试错", "#fbbf24"
    else:
        decision, color = "空仓", "#6b7280"

    headline = _build_decision_headline(
        total_score, decision, indicators, prev_board_groups,
        sec_concentration=sec_concentration,
    )

    return {
        "indicators": indicators,
        "total_score": total_score,
        "decision": decision,
        "decision_color": color,
        "headline": headline,
    }


def _build_decision_headline(
    total: int, decision: str, indicators: list[dict],
    prev_board_groups: list[dict], sec_concentration: float,
) -> str:
    """生成顶部一句话决策建议（按用户给的模板）"""
    # 找最差项（score=0 中 raw 最差的）
    zeros = [i for i in indicators if i["score"] == 0]
    worst_label = zeros[0]["label"] if zeros else "无明显短板"
    worst_today = zeros[0]["today"] if zeros else ""

    # 中位股断层：3板及以上 ≤ 1 只
    mid_count = 0
    for g in prev_board_groups:
        if g.get("prev_board", 0) == 0:
            continue
        for s in (g.get("promoted") or []):
            if s.get("today_board", 0) >= 3:
                mid_count += 1
    mid_break = mid_count <= 1

    # 2进3 红盘晋级数 / 总晋级
    g2 = next((g for g in prev_board_groups if g.get("prev_board") == 2), None)
    promoted2 = (g2 or {}).get("promoted") or []
    g2_total = len(promoted2)
    g2_red = sum(1 for s in promoted2 if (s.get("today_open_pct") or 0) >= 0)
    g2_green = g2_total - g2_red

    # 1进2 成功率
    b1_ind = next((i for i in indicators if i["label"] == "1进2成功率"), {})
    b1_rate_str = b1_ind.get("today", "")

    # 按 decision 选 headline（与 _build_scorecard 的阈值一致：≥5/≥4/≥3/<3）
    if decision == "空仓":
        if mid_break:
            return (
                f"空仓。最大风险：中位股断层+无主线。今日 2进3 晋级 {g2_total} 只"
                f"但红盘开仅 {g2_red} 只（绿盘开 {g2_green} 只），"
                f"被动晋级为主，次日大概率补跌。"
                f"除非明日竞价板块集中度>40% 且 1进2 成功率>40%，否则不开仓。"
            )
        return (
            f"空仓。最大风险：{worst_label}（{worst_today}）。"
            f"今日 1进2 仅 {b1_rate_str}、板块集中度 {sec_concentration}%，"
            f"接力生态恶化。除非明日板块集中度>40% 且空间板未断板，否则不开仓。"
        )
    if decision == "试错":
        space_ok = next((i for i in indicators if i["label"] == "空间板"), {}).get("score", 0) >= 1
        if space_ok:
            return (
                f"试错。最大风险：{worst_label}（{worst_today}）。"
                f"空间板未断但其他维度偏弱，仅做最强主线方向、仓位 ≤20%。"
            )
        return (
            f"试错。最大风险：{worst_label}（{worst_today}）。"
            f"接力生态半生不熟，仓位严格控制在 1/4 以下，破信号位坚决出。"
        )
    if decision == "正常":
        return (
            f"正常出击。主线相对清晰（板块集中度 {sec_concentration}%），晋级率健康。"
            f"重点做 2进3 / 3进4 换手板。唯一风险：{worst_label}（{worst_today}），"
            f"一旦恶化立即收手。"
        )
    # decision == "重仓"
    return (
        f"重仓出击。接力生态满血（板块集中度 {sec_concentration}%、1进2 {b1_rate_str}）。"
        f"重点做 2进3 / 3进4 / 4进5 换手板，仓位可上 70%。"
        f"唯一风险：空间板一旦断板立即减仓。"
    )


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

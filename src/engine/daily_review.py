"""盘后复盘引擎 — 次日鱼塘

盘后（调度 18:00 run_eod_bundle → run_post_market_bundle）自动运行：
1. 涨停梯队分析（按板块分组，识别主线）
2. 连板股梯队（按连板数排序）
3. 生成次日观察池（3只核心标的+逻辑备注）
4. 输出到 data/latest_review.json
5. 邮件推送由 scheduler.run_post_market_bundle → send_review_report 完成
"""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd

from src.config import DATA_DIR, now_cn
from src.data.json_io import dump_json_file, load_json_file


def _norm_zt_code(code) -> str:
    """涨停池 / 连板梯队 code 归一化为 6 位数字串，避免 1211 vs 001211 对不上。"""
    if code is None:
        return ""
    s = str(code).strip()
    if not s:
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if s.isdigit():
        return s.zfill(6)[-6:]
    return s


def _limit_up_cache_keys_sorted(cache: dict) -> list[str]:
    return sorted(
        str(k) for k in (cache or {}).keys()
        if str(k).isdigit() and len(str(k)) == 8
    )


def _get_limit_up_session_for_review() -> tuple[list[dict], str]:
    """返回 (当日涨停列表, 对应的 limit_up_cache 日期键 YYYYMMDD)。

    若当前自然日键尚未写入缓存，则用缓存中最新一日（避免复盘/晋级矩阵日期错位）。
    """
    cache_file = DATA_DIR / "limit_up_cache.json"
    try:
        cache = load_json_file(cache_file) or {}
    except Exception:
        return [], now_cn().strftime("%Y%m%d")

    today_key = now_cn().strftime("%Y%m%d")
    keys = _limit_up_cache_keys_sorted(cache)
    if today_key in cache:
        return list(cache.get(today_key) or []), today_key
    if keys:
        k0 = keys[-1]
        return list(cache.get(k0) or []), k0
    return [], today_key


def _session_key_to_iso(session_key: str) -> str:
    s = str(session_key)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return now_cn().strftime("%Y-%m-%d")


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
    # 复盘日全市场涨跌家数 + 上证（与 /api/market-insight.breadth 同源，按日复盘冻结）
    market_breadth: dict = field(default_factory=dict)


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


def _lbc_from_record(rec: dict) -> int:
    """涨停 cache / zt_pool 写入的连板数（优先于历史日递推）。"""
    if not rec:
        return 0
    try:
        v = int(rec.get("continuous_limit_up") or rec.get("board_count") or 0)
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return 0


def _board_count_walk(code: str, as_of_key: str, cache: dict) -> int:
    """截至 as_of_key 当日收盘的连板数（有 lbc 则用，否则按 cache 连续涨停日递推）。"""
    code = _norm_zt_code(code)
    if not code or not as_of_key:
        return 0
    keys = _limit_up_cache_keys_sorted(cache)
    if as_of_key not in keys:
        return 0
    day_pool = cache.get(as_of_key) or []
    day_codes = {_norm_zt_code(r.get("code")) for r in day_pool}
    if code not in day_codes:
        return 0
    for r in day_pool:
        if _norm_zt_code(r.get("code")) == code:
            lbc = _lbc_from_record(r)
            if lbc > 0:
                return lbc
            break
    idx = keys.index(as_of_key)
    count = 1
    for i in range(idx - 1, -1, -1):
        prev_codes = {_norm_zt_code(r.get("code")) for r in (cache.get(keys[i]) or [])}
        if code in prev_codes:
            count += 1
        else:
            break
    return count


def _yesterday_cache_key(session_today_key: str, cache: dict) -> str | None:
    keys = _limit_up_cache_keys_sorted(cache)
    before = [k for k in keys if k < str(session_today_key)]
    return before[-1] if before else None


def _space_leader_on_day(day_key: str, cache: dict) -> dict | None:
    """指定交易日 cache 上的空间板（最高连板，同板取涨幅最高）。"""
    stocks = cache.get(day_key) or []
    if not stocks:
        return None
    best: dict | None = None
    best_bc, best_pct = 0, -999.0
    for r in stocks:
        c = _norm_zt_code(r.get("code"))
        if not c:
            continue
        bc = _board_count_walk(c, day_key, cache)
        pct = float(r.get("change_pct", 0) or 0)
        if bc > best_bc or (bc == best_bc and pct > best_pct):
            best_bc, best_pct = bc, pct
            best = {"code": c, "name": r.get("name", ""), "board_count": bc, "change_pct": pct}
    return best


def _row_as_dict(row) -> dict:
    if row is None:
        return {}
    if hasattr(row, "to_dict"):
        return row.to_dict()
    return dict(row) if isinstance(row, dict) else {}


def _session_ymd_from_review_date(review_date: str) -> str:
    rk = re.sub(r"\D", "", str(review_date or ""))[:8]
    if len(rk) == 8:
        return rk
    return now_cn().strftime("%Y%m%d")


def _pre_close_before_session(code: str, session_ymd: str) -> float | None:
    """复盘日开盘价分母：前一交易日收盘价（日 K 缓存）。"""
    yc = _norm_zt_code(code)
    sk = _session_ymd_from_review_date(session_ymd)
    if len(yc) != 6 or len(sk) != 8:
        return None
    try:
        import pandas as pd
        from src.data.kline_file_cache import try_read_cache

        df = try_read_cache(yc, "240", 40, allow_stale=True)
        if df is None or df.empty or "date" not in df.columns:
            return None
        d = df.copy()
        d["_dt"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
        d = d.dropna(subset=["_dt"]).sort_values("_dt")
        target = pd.Timestamp(f"{sk[:4]}-{sk[4:6]}-{sk[6:8]}")
        before = d[d["_dt"] < target]
        if before.empty:
            return None
        pc = float(before.iloc[-1].get("close") or 0)
        return pc if pc > 0 else None
    except Exception:
        return None


def open_pct_for_session(
    code: str,
    session_ymd: str,
    spot_row: dict | None = None,
    prev_board_groups: list | None = None,
) -> float | None:
    """复盘日开盘涨幅 %：spot → 晋级矩阵 → 日 K → 竞价 tick 缓存。"""
    yc = _norm_zt_code(code)
    sk = _session_ymd_from_review_date(session_ymd)
    if len(yc) != 6:
        return None

    if prev_board_groups:
        for g in prev_board_groups:
            for bucket in ("promoted", "failed"):
                for s in g.get(bucket) or []:
                    if _norm_zt_code(s.get("code")) != yc:
                        continue
                    v = s.get("today_open_pct")
                    if v is not None:
                        try:
                            return round(float(v), 2)
                        except (TypeError, ValueError):
                            pass

    row = spot_row or _build_spot_map_for_codes({yc}).get(yc) or {}
    try:
        op = float(row.get("open", 0) or 0)
        pre = float(row.get("pre_close", 0) or 0)
        if op > 0 and pre > 0:
            return round((op / pre - 1) * 100, 2)
    except (TypeError, ValueError):
        pass

    try:
        import pandas as pd
        from src.data.kline_file_cache import try_read_cache

        df = try_read_cache(yc, "240", 40, allow_stale=True)
        if df is not None and not df.empty and "date" in df.columns:
            d = df.copy()
            d["_k"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y%m%d")
            hit = d[d["_k"] == sk]
            if not hit.empty:
                r = hit.iloc[-1]
                op = float(r.get("open") or 0)
                pre = float(r.get("pre_close") or 0)
                if op <= 0 or pre <= 0:
                    pre = _pre_close_before_session(yc, sk) or pre
                if op > 0 and pre > 0:
                    return round((op / pre - 1) * 100, 2)
    except Exception:
        pass

    try:
        from src.data.structured_store import load_auction_session

        sess = load_auction_session(yc, sk)
        if sess:
            ot = sess.get("open_tick")
            if not isinstance(ot, dict) and sess.get("auction_window"):
                aw = sess.get("auction_window") or []
                ot = aw[0] if aw else None
            price = float((ot or {}).get("price") or 0)
            pre = _pre_close_before_session(yc, sk)
            if price > 0 and pre and pre > 0:
                return round((price / pre - 1) * 100, 2)
    except Exception:
        pass

    try:
        from src.data.json_io import load_json_file

        doc = load_json_file(f"auction_cache/{sk}/{yc}.json")
        if isinstance(doc, dict):
            ot = doc.get("open_tick")
            if not isinstance(ot, dict) and doc.get("auction_window"):
                aw = doc.get("auction_window") or []
                ot = aw[0] if aw else None
            price = float((ot or {}).get("price") or 0)
            pre = _pre_close_before_session(yc, sk)
            if price > 0 and pre and pre > 0:
                return round((price / pre - 1) * 100, 2)
    except Exception:
        pass
    return None


def enrich_prev_space_board_open(
    relay_env: dict | None,
    review_date: str = "",
    prev_board_groups: list | None = None,
) -> dict:
    """补全昨日空间板今日开盘涨幅（盘内只读库，不打全市场）。"""
    out = dict(relay_env or {})
    prev = out.get("prev_space_board_today")
    if not isinstance(prev, dict) or not prev.get("code"):
        return out
    if prev.get("today_open_pct") is not None:
        return out
    yc = _norm_zt_code(prev.get("code"))
    sk = _session_ymd_from_review_date(review_date)
    row = _build_spot_map_for_codes({yc}).get(yc) if yc else None
    op_pct = open_pct_for_session(yc, sk, row, prev_board_groups)
    if op_pct is not None:
        updated = dict(prev)
        updated["today_open_pct"] = op_pct
        out["prev_space_board_today"] = updated
    return out


def _build_spot_map_for_codes(codes: set[str]) -> dict[str, dict]:
    """复盘用 spot：全市场缓存 + 腾讯批量补缺（避免 mock 小样本导致开盘缺失）。"""
    need = {_norm_zt_code(c) for c in codes if _norm_zt_code(c)}
    if not need:
        return {}
    spot_map: dict[str, dict] = {}
    try:
        spot = _get_spot_cached()
        if spot is not None and not spot.empty:
            for _, r in spot.iterrows():
                c = _norm_zt_code(r.get("code"))
                if c in need:
                    spot_map[c] = _row_as_dict(r)
    except Exception as e:
        print(f"[复盘] spot 缓存取用失败: {e}")
    missing = [c for c in need if c not in spot_map]
    if missing:
        try:
            from src.data.tencent_api import fetch_stock_details
            df = fetch_stock_details(missing, batch_size=80)
            if df is not None and not df.empty:
                for _, r in df.iterrows():
                    c = _norm_zt_code(r.get("code"))
                    if c:
                        spot_map[c] = _row_as_dict(r)
        except Exception as e:
            print(f"[复盘] 腾讯 spot 补缺失败: {e}")
    return spot_map


def run_daily_review() -> Optional[DailyReview]:
    """执行盘后复盘"""
    # 预热 spot 缓存
    _get_spot_cached()

    # 1. 获取涨停数据（与 limit_up_cache 日期键对齐）
    limit_up_data, session_zt_key = _get_limit_up_session_for_review()
    if not limit_up_data:
        print("[复盘] 无涨停数据，跳过")
        return None

    print(f"[复盘] {_session_key_to_iso(session_zt_key)} (cache={session_zt_key}) 开始...")
    # 2. 获取连板数据
    lianban_data = _get_lianban_ladder(session_today_key=session_zt_key)

    # 3. 获取排行数据补充板块信息
    ranking_data = _get_ranking_data()

    # 4. 构建复盘（date 与缓存「当日」键一致，避免跨日矩阵/空间板错位）
    review = DailyReview(date=_session_key_to_iso(session_zt_key))

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
    review.prev_board_groups = _build_prev_board_groups(
        limit_up_data, lianban_data, session_today_key=session_zt_key,
    )
    # 4.6 板块涨停统计 + 龙头
    review.sector_zt_stats = _build_sector_zt_stats(lianban_data)
    # 4.6b 概念涨停统计（一对多聚合，最强概念梯队展示）
    review.concept_zt_stats = _build_concept_zt_stats(lianban_data)
    # 兼容字段：扁平化失败列表
    review.failed_promotion_list = _get_failed_promotion_list(
        limit_up_data, session_today_key=session_zt_key,
    )

    # 4.7 接力专项数据（区域 B/C/A 顺序计算：B 依赖梯队、C 依赖矩阵、A 综合）
    review.relay_env = _build_relay_env(
        review, review.prev_board_groups, lianban_data,
        session_today_key=session_zt_key,
    )
    review.promotion_summary = _build_promotion_summary(review.prev_board_groups)
    review.scorecard = _build_scorecard(
        review.prev_board_groups, review.relay_env,
        review.sector_zt_stats, review.limit_up_count,
        concept_zt_stats=review.concept_zt_stats,
        highest_board=review.highest_board,
    )

    try:
        from src.engine.market_insight import _compute_market_breadth

        mb = _compute_market_breadth()
        if isinstance(mb, dict):
            review.market_breadth = mb
    except Exception as e:
        print(f"[复盘] market_breadth 统计失败: {e}")

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
    """获取今日涨停股列表（兼容旧调用；与缓存最新交易日对齐）。"""
    data, _ = _get_limit_up_session_for_review()
    return data


def _zt_pool_lbc(zt_pool: dict, code: str) -> int:
    """东财涨停池 lbc（与同花顺连板晋级同源）。"""
    c6 = _norm_zt_code(code)
    if not c6:
        return 0
    info = zt_pool.get(c6) or zt_pool.get(code) or {}
    try:
        return int(info.get("lbc") or 0)
    except (TypeError, ValueError):
        return 0


def _limit_up_close_thr(code: str) -> float:
    """主板/创业板收盘涨停近似阈值（%）。"""
    c6 = _norm_zt_code(code)
    if c6.startswith(("300", "301", "688")):
        return 19.4
    return 9.7


def _spot_close_pct(row: dict | None) -> float | None:
    if not row:
        return None
    try:
        close = float(row.get("close", 0) or 0)
        pre = float(row.get("pre_close", 0) or 0)
        if close > 0 and pre > 0:
            return round((close / pre - 1) * 100, 2)
    except (TypeError, ValueError):
        pass
    return None


def _merge_today_limit_up_codes(
    today_limit_up: list[dict],
    zt_today: dict,
    spot_map: dict | None = None,
    extra_codes: set[str] | None = None,
) -> set[str]:
    """当日收盘涨停 code 集合：本地 cache + 东财涨停池 + spot 触板（连板晋级真源优先 zt_pool）。"""
    codes = {
        _norm_zt_code(s.get("code"))
        for s in (today_limit_up or [])
        if _norm_zt_code(s.get("code"))
    }
    for raw in (zt_today or {}):
        c6 = _norm_zt_code(raw)
        if c6 and _zt_pool_lbc(zt_today, c6) > 0:
            codes.add(c6)
    if spot_map:
        for c6, row in spot_map.items():
            if not c6:
                continue
            pct = _spot_close_pct(row if isinstance(row, dict) else None)
            if pct is not None and pct >= _limit_up_close_thr(c6):
                codes.add(c6)
    for c6 in extra_codes or set():
        nc = _norm_zt_code(c6)
        if nc:
            codes.add(nc)
    return codes


def _prev_board_for_promotion(
    code: str,
    yesterday_str: str,
    yesterday_pool: dict[str, dict],
    cache: dict,
    zt_yesterday: dict,
) -> int:
    """昨日收盘连板数：优先涨停池 lbc，避免 cache 递推高估为 2 板导致分母偏小。"""
    zt_b = _zt_pool_lbc(zt_yesterday, code)
    if zt_b > 0:
        return zt_b
    if code not in yesterday_pool:
        return 0
    lbc = _lbc_from_record(yesterday_pool.get(code) or {})
    if lbc > 0:
        return lbc
    return _board_count_walk(code, yesterday_str, cache)


def _today_board_for_promotion(
    code: str,
    today_lianban_map: dict,
    zt_today: dict,
) -> int:
    """今日收盘连板数：优先当日涨停池 lbc，其次天梯 board_count。"""
    zt_b = _zt_pool_lbc(zt_today, code)
    if zt_b > 0:
        return zt_b
    info = today_lianban_map.get(code) or {}
    try:
        return int(info.get("board_count") or 0)
    except (TypeError, ValueError):
        return 0


def _is_promoted_board(prev_board: int, today_board: int, *, in_today_limit_up: bool) -> bool:
    """晋级：今日在涨停池且连板数 ≥ 昨日+1（对齐同花顺 N进N+1，非仅「今日曾涨停」）。"""
    if not in_today_limit_up or prev_board <= 0 or today_board <= 0:
        return False
    return today_board >= prev_board + 1


def _promotion_candidates(
    yesterday_pool: dict[str, dict],
    zt_yesterday: dict,
    yesterday_str: str,
    cache: dict,
) -> dict[str, tuple[int, dict]]:
    """晋级矩阵分母：优先昨日东财涨停池 lbc（与同花顺 N进N+1 分母一致），cache 仅补缺。"""
    out: dict[str, tuple[int, dict]] = {}
    for raw_code, zinfo in (zt_yesterday or {}).items():
        c6 = _norm_zt_code(raw_code)
        if not c6:
            continue
        pb = _zt_pool_lbc(zt_yesterday, c6)
        if pb <= 0:
            continue
        info = yesterday_pool.get(c6) or {
            "code": c6,
            "name": zinfo.get("name", ""),
            "continuous_limit_up": pb,
        }
        out[c6] = (pb, info)
    for code, info in yesterday_pool.items():
        if code in out:
            continue
        pb = _prev_board_for_promotion(
            code, yesterday_str, yesterday_pool, cache, zt_yesterday,
        )
        if pb <= 0:
            continue
        out[code] = (pb, info)
    return out


def _build_prev_board_groups(
    today_limit_up: list[dict],
    lianban_data: list[dict],
    *,
    session_today_key: str,
) -> list[dict]:
    """构造按昨日板数分组的晋级矩阵（session_today_key = 复盘对应的 limit_up_cache 当日键）。"""
    cache_file = DATA_DIR / "limit_up_cache.json"
    try:
        cache = load_json_file(cache_file) or {}
    except Exception:
        return {}

    keys_sorted = _limit_up_cache_keys_sorted(cache)
    sk = str(session_today_key)
    before = [k for k in keys_sorted if k < sk]
    if not before:
        return []
    yesterday_str = before[-1]

    yesterday_raw = cache.get(yesterday_str, []) or []
    yesterday_pool: dict[str, dict] = {}
    for r in yesterday_raw:
        kc = _norm_zt_code(r.get("code"))
        if kc:
            yesterday_pool[kc] = r
    today_lianban_map = {
        _norm_zt_code(s.get("code")): s for s in lianban_data if _norm_zt_code(s.get("code"))
    }

    zt_yesterday: dict = {}
    zt_today: dict = {}
    try:
        from src.data.zt_pool_api import fetch_zt_pool_with_retry

        zt_yesterday = fetch_zt_pool_with_retry(yesterday_str) or {}
        zt_today = fetch_zt_pool_with_retry(sk) or {}
    except Exception as e:
        print(f"[复盘] 涨停池(lbc) 拉取失败: {e}")

    candidates = _promotion_candidates(
        yesterday_pool, zt_yesterday, yesterday_str, cache,
    )
    if not candidates and not today_lianban_map:
        return []

    industry_map: dict = {}
    try:
        ic = DATA_DIR / "industry_cache.json"
        industry_map = load_json_file(ic) or {}
    except Exception:
        pass

    need_codes = set(candidates.keys()) | {
        _norm_zt_code(s.get("code")) for s in (today_limit_up or []) if _norm_zt_code(s.get("code"))
    }
    spot_map = _build_spot_map_for_codes(need_codes)
    today_codes = _merge_today_limit_up_codes(
        today_limit_up, zt_today, spot_map, extra_codes=need_codes,
    )

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

    groups_by_prev: dict = {}
    for code, (prev_b, info) in candidates.items():
        grp = groups_by_prev.setdefault(prev_b, {"promoted": [], "failed": []})
        name = info.get("name", "") or (zt_yesterday.get(code) or {}).get("name", "")
        is_main = _is_main(code)
        ind = industry_map.get(code, "-")
        today_bc = _today_board_for_promotion(code, today_lianban_map, zt_today)
        in_today = code in today_codes
        if _is_promoted_board(prev_b, today_bc, in_today_limit_up=in_today):
            today_info = today_lianban_map.get(code, {})
            _close, _pct = _today_pct(code)
            grp["promoted"].append({
                "code": code,
                "name": name,
                "today_board": today_bc,
                "today_pct": _pct if _pct is not None else today_info.get("change_pct", 0),
                "today_open_pct": _today_open_pct(code),
                "industry": ind,
                "is_main_board": is_main,
                "lbt": today_info.get("lbt", "") or (zt_today.get(code) or {}).get("lbt", ""),
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

    result = []
    for prev_b in sorted(groups_by_prev.keys(), reverse=True):
        grp = groups_by_prev[prev_b]
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

    if new_first:
        result.append({
            "prev_board": 0,
            "next_board": 1,
            "label": "今日新进首板",
            "promoted": new_first,
            "failed": [],
        })

    return result


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

    for grp in (review.prev_board_groups or []):
        for s in grp.get("promoted", []) + grp.get("failed", []):
            s["top_concepts"] = pick(s.get("code", ""))

    relay = review.relay_env or {}
    sb = relay.get("space_board")
    if isinstance(sb, dict) and sb.get("code"):
        sb["top_concepts"] = pick(sb["code"])

    for w in (review.watch_pool or []):
        if isinstance(w, dict) and w.get("code"):
            w["top_concepts"] = pick(w["code"])

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


def _get_failed_promotion_list(
    today_limit_up: list[dict],
    *,
    session_today_key: str,
) -> list[dict]:
    """晋级失败列表：昨日涨停但今日没涨停的股票"""
    cache_file = DATA_DIR / "limit_up_cache.json"
    try:
        cache = load_json_file(cache_file) or {}
    except Exception:
        return []

    keys_sorted = _limit_up_cache_keys_sorted(cache)
    sk = str(session_today_key)
    before = [k for k in keys_sorted if k < sk]
    if not before:
        return []
    yesterday_str = before[-1]
    past_desc = [k for k in reversed(keys_sorted) if k <= yesterday_str]

    yesterday_raw = cache.get(yesterday_str, []) or []
    yesterday_pool: dict[str, dict] = {}
    for r in yesterday_raw:
        kc = _norm_zt_code(r.get("code"))
        if kc:
            yesterday_pool[kc] = r
    if not yesterday_pool:
        return []
    zt_today: dict = {}
    try:
        from src.data.zt_pool_api import fetch_zt_pool_with_retry

        zt_today = fetch_zt_pool_with_retry(sk) or {}
    except Exception:
        pass
    spot_map_pre = _build_spot_map_for_codes(set(yesterday_pool.keys()))
    today_codes = _merge_today_limit_up_codes(
        today_limit_up, zt_today, spot_map_pre,
    )

    # 昨日涨停 - 今日涨停 = 晋级失败
    failed_codes = [c for c in yesterday_pool.keys() if c not in today_codes]
    if not failed_codes:
        return []

    # 行业 cache
    industry_map: dict = {}
    try:
        ic = DATA_DIR / "industry_cache.json"
        industry_map = load_json_file(ic) or {}
    except Exception:
        pass

    prev_board_map: dict = {}
    for code in failed_codes:
        lbc = _lbc_from_record(yesterday_pool.get(code) or {})
        prev_board_map[code] = lbc if lbc > 0 else _board_count_walk(code, yesterday_str, cache)

    spot_map = _build_spot_map_for_codes(set(failed_codes))

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


def _get_lianban_ladder(*, session_today_key: str = "") -> list[dict]:
    """获取连板梯队（含 industry 板块 + concepts 概念 + 封板时间 + 一字标记）"""
    cache_file = DATA_DIR / "limit_up_cache.json"

    # 行业映射
    industry_map: dict = {}
    try:
        ic = DATA_DIR / "industry_cache.json"
        industry_map = load_json_file(ic) or {}
    except Exception:
        pass

    # cache 缺失（多见于北交所 920 系列）即时调 emweb 单股补，并写回 cache
    def _fill_missing_industry(codes: list[str]) -> None:
        miss = [c for c in codes if not industry_map.get(c)]
        if not miss:
            return
        try:
            from src.data.ranking_scanner import _fetch_industry_emweb
            from concurrent.futures import ThreadPoolExecutor, as_completed
            updated = 0
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(_fetch_industry_emweb, c): c for c in miss}
                for fut in as_completed(futs):
                    code = futs[fut]
                    try:
                        ind = fut.result()
                    except Exception:
                        ind = ""
                    if ind:
                        industry_map[code] = ind
                        updated += 1
            if updated:
                try:
                    dump_json_file(DATA_DIR / "industry_cache.json", industry_map)
                except Exception:
                    pass
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
        cache = load_json_file(cache_file) or {}
        sorted_dates = sorted(
            [k for k in cache.keys() if str(k).isdigit() and len(str(k)) == 8],
            reverse=True,
        )
        if not sorted_dates:
            return []

        sk = str(session_today_key or "")
        latest_date = sk if sk and sk in cache else sorted_dates[0]
        latest_stocks = {
            _norm_zt_code(r.get("code")): r
            for r in cache.get(latest_date, [])
            if _norm_zt_code(r.get("code"))
        }

        # industry_cache 缺失即时补（多见于北交所 920）
        _fill_missing_industry(list(latest_stocks.keys()))

        # 拉今日 zt_pool 拿封板时间（lbt）
        zt_pool: dict = {}
        try:
            from src.data.zt_pool_api import fetch_zt_pool_with_retry
            zt_pool = fetch_zt_pool_with_retry(latest_date) or {}
        except Exception as e:
            print(f"[复盘] 涨停池(封板时间) 拉取失败: {e}")

        # 合并 zt_pool：东财涨幅榜筛涨停无连板字段且易漏板，高连板仅以涨停池为准
        for raw_code, zt_info in (zt_pool or {}).items():
            code = _norm_zt_code(raw_code)
            if not code:
                continue
            if code in latest_stocks:
                continue
            latest_stocks[code] = {
                "code": code,
                "name": str(zt_info.get("name") or ""),
                "change_pct": 0.0,
            }

        spot_map = _build_spot_map_for_codes(set(latest_stocks.keys()))

        def _is_flat(code: str, is_main: bool) -> bool:
            row = spot_map.get(_norm_zt_code(code))
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
            zt_info = zt_pool.get(code, {}) or zt_pool.get(str(code).zfill(6), {})
            zt_lbc = 0
            try:
                zt_lbc = int(zt_info.get("lbc") or 0)
            except (TypeError, ValueError):
                zt_lbc = 0
            count = zt_lbc if zt_lbc > 0 else _lbc_from_record(info)
            if count <= 0:
                count = 1
                for d in sorted_dates[1:]:
                    codes_in_day = {
                        _norm_zt_code(r.get("code"))
                        for r in cache.get(d, [])
                    }
                    if code in codes_in_day:
                        count += 1
                    else:
                        break

            is_main = not code.startswith(("300", "301", "688", "8", "4"))

            # 流通市值（亿）+ 当日收盘价 — spot 含 market_cap（单位元）+ close
            # 注意：spot_map 存的是 pandas Series，不能 `Series or {}`（Series 真值含混）
            row = spot_map.get(code) or {}
            try:
                mc_raw = float(row.get("market_cap", 0) or 0)
                close_p = float(row.get("close", 0) or 0)
                spot_pct = float(row.get("change_pct", 0) or 0)
            except (TypeError, ValueError):
                mc_raw = 0.0
                close_p = 0.0
                spot_pct = 0.0
            mc_yi = round(mc_raw / 1e8, 2) if mc_raw > 0 else None
            close_p = round(close_p, 2) if close_p > 0 else None
            chg = float(info.get("change_pct", 0) or 0)
            if spot_pct != 0:
                chg = spot_pct

            result.append({
                "code": code,
                "name": info.get("name", "") or zt_info.get("name", ""),
                "board_count": count,
                "is_main_board": is_main,
                "change_pct": chg,
                "industry": industry_map.get(code, "-"),
                "concepts": list(concept_map.get(code) or []),
                "lbt": zt_info.get("lbt", ""),       # 最后封板时间 HH:MM:SS
                "is_flat": _is_flat(code, is_main),  # 一字板
                "market_cap_yi": mc_yi,              # 流通市值（亿）— Rule A 筛选用
                "close_price": close_p,              # 当日收盘价 — watch_pool 冻结用
            })

        result.sort(key=lambda x: (-x["board_count"], -x.get("change_pct", 0)))
        return result

    except Exception as e:
        print(f"[复盘] 连板梯队构造失败: {e}")
        return []


def _get_ranking_data() -> dict:
    """获取排行数据（用于板块信息）"""
    ranking_file = DATA_DIR / "latest_ranking.json"
    try:
        data = load_json_file(ranking_file)
        if data is not None:
            return {str(r["code"]): r for r in data.get("ranking", [])}
    except Exception:
        pass
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
    return build_watch_pool_from_ranking(ranking, lianban_ladder=lianban)


def build_watch_pool_from_ranking(
    ranking: dict | list,
    lianban_ladder: list[dict] | None = None,
) -> list[dict]:
    """次日观察池（用户口径，主板专员）

    入选规则：主板 + 流通市值 ≤ 100亿 + continuous_limit_up ≥ 2
    （从全市场涨停/连板天梯扫描，不限 top30）

    Args:
        ranking: {code: ranking_row} 或 [ranking_row, ...]
    """
    rows = list(ranking.values() if isinstance(ranking, dict) else (ranking or []))
    qualified: list[dict] = []
    seen_codes: set[str] = set()

    qualified.extend(_scan_full_market_rule_a(rows, seen_codes, lianban_ladder))

    if not qualified:
        return []

    # 排序：连板数 desc → 10日涨幅 desc
    qualified.sort(
        key=lambda s: (
            -int(s.get("continuous_limit_up") or 0),
            -float(s.get("gain_10d") or 0),
        ),
    )

    # 加载 concept_zt_stats（用于板块助攻）
    concept_zt_map: dict[str, dict] = {}
    try:
        review_file = DATA_DIR / "latest_review.json"
        review_data = load_json_file(review_file) or {}
        for c in (review_data.get("concept_zt_stats") or []):
            concept_zt_map[c.get("name", "")] = c
    except Exception:
        pass

    out: list[dict] = []
    for s in qualified:
        ind = s.get("industry") or "未知"
        clu = int(s.get("continuous_limit_up") or 0)
        gain = float(s.get("gain_10d") or 0)
        reason = f"{clu}连板·流通市值{s.get('market_cap_yi','-')}亿·主板（小盘接力）"
        candidate = asdict(WatchCandidate(
            code=str(s.get("code", "")),
            name=s.get("name", ""),
            board_count=clu,
            industry=ind,
            close=float(s.get("close") or 0),
            market_cap_yi=float(s.get("market_cap_yi") or 0),
            total_gain_pct=gain,
            reason=reason,
            watch_points="",  # 不再使用，由 observation 字段替代
            auction_range="",  # 去掉竞价区间
        ))
        # 透传 top_concepts / is_main_board / pool_tag 给前端展示
        candidate["top_concepts"] = list(s.get("top_concepts") or [])
        candidate["is_main_board"] = bool(s.get("is_main_board", True))
        candidate["pool_tag"] = s.get("_pool_tag") or "小盘接力"
        # 计算 6 字段观察要点
        candidate["observation"] = _compute_watch_observation(s, concept_zt_map)
        out.append(candidate)
    return out


# ── 观察要点 6 字段（用户口径） ───────────────────────────────────

def _scan_full_market_rule_a(
    ranking_rows: list[dict],
    seen_codes: set[str],
    lianban_ladder: list[dict] | None = None,
) -> list[dict]:
    """规则 A：从全市场今日涨停池筛 主板 + 流通市值≤100亿 + ≥2连板

    数据源优先级（用户偏好：复用连板天梯数据，避免重复抓接口）：
      1. lianban_ladder 已含 code/name/board_count/industry/concepts/lbt/market_cap_yi
         → 一站式 cover，全市场覆盖
      2. zt_pool 兜底 + ranking/tencent 补市值（旧路径，应对 lianban_ladder 缺时）
    """
    out: list[dict] = []

    # === 路径 1: lianban_ladder（如有）一站式 ===
    if lianban_ladder:
        for s in lianban_ladder:
            code = str(s.get("code", ""))
            board = int(s.get("board_count", 0) or 0)
            # is_main_board 字段缺失/为 None 时按代码前缀兜底（与路径 2 保持一致）
            is_main_field = s.get("is_main_board", None)
            if is_main_field is None:
                is_main = not str(code).startswith(("300", "301", "688", "8", "4", "92"))
            else:
                is_main = bool(is_main_field)
            mc = s.get("market_cap_yi")
            if code in seen_codes:
                continue
            if not is_main or board < 2:
                continue
            try:
                mc_f = float(mc) if mc is not None else 0
            except (TypeError, ValueError):
                continue
            if not (0 < mc_f <= 100):
                continue
            cp = s.get("close_price")
            try:
                close_val = float(cp) if cp is not None else 0
            except (TypeError, ValueError):
                close_val = 0
            out.append({
                "code": code,
                "name": s.get("name", ""),
                "continuous_limit_up": board,
                "last_limit_up_time": s.get("lbt", ""),
                "is_main_board": True,
                "market_cap_yi": mc_f,
                "industry": s.get("industry", "未知"),
                "concepts": list(s.get("concepts") or []),
                "top_concepts": [],  # API 层若需可后处理
                "close": close_val,  # 当日收盘价（lianban_ladder.close_price）
                "gain_10d": 0,
                "_pool_tag": "小盘接力",
            })
            seen_codes.add(code)
        if out:
            return out

    # === 路径 2: 兜底 — zt_pool + ranking + tencent ===
    # 1. 拉今日涨停池（含 lbc/lbt/zbc）
    try:
        from src.data.zt_pool_api import fetch_zt_pool_with_retry
        zt_pool = fetch_zt_pool_with_retry() or {}
    except Exception:
        zt_pool = {}
    if not zt_pool:
        return out

    # 2. 候选 = lbc>=2 + 主板（非 300/301/688/8x/4x/92x）
    #    92xxxx = 北交所京A，trend_screener._is_main_board_code 同样口径
    def _is_main(code: str) -> bool:
        return not str(code).startswith(("300", "301", "688", "8", "4", "92"))

    cand_codes = []
    for code, info in zt_pool.items():
        if int(info.get("lbc", 0) or 0) < 2:
            continue
        if not _is_main(code):
            continue
        if str(code) in seen_codes:
            continue
        cand_codes.append((code, info))
    if not cand_codes:
        return out

    # 3. ranking 内的市值优先用 ranking 数据
    ranking_map = {str(r.get("code", "")): r for r in ranking_rows}
    missing_mc: list[str] = []
    cand_with_mc: list[tuple[dict, float]] = []  # (info_dict, market_cap_yi)
    for code, zinfo in cand_codes:
        r = ranking_map.get(code)
        if r and r.get("market_cap_yi"):
            cand_with_mc.append(({
                "code": code,
                "name": zinfo.get("name") or r.get("name", ""),
                "continuous_limit_up": int(zinfo.get("lbc", 0) or 0),
                "last_limit_up_time": zinfo.get("lbt", ""),
                "is_main_board": True,
                "market_cap_yi": float(r.get("market_cap_yi") or 0),
                "industry": r.get("industry", "未知"),
                "concepts": list(r.get("concepts") or []),
                "top_concepts": list(r.get("top_concepts") or []),
                "close": float(r.get("close", 0) or 0),
                "gain_10d": float(r.get("gain_10d", 0) or 0),
            }, float(r.get("market_cap_yi") or 0)))
        else:
            missing_mc.append(code)

    # 4. ranking 没的从腾讯批量拉
    tx_map: dict = {}
    if missing_mc:
        try:
            from src.data.tencent_api import fetch_stock_details
            df = fetch_stock_details(missing_mc)
            if df is not None and not df.empty:
                df["code"] = df["code"].astype(str)
                for _, row in df.iterrows():
                    tx_map[str(row["code"])] = row
        except Exception:
            pass

    # 5. industry/concepts 对全市场涨停的 cache 兜底
    industry_cache: dict = {}
    try:
        ic = DATA_DIR / "industry_cache.json"
        industry_cache = load_json_file(ic) or {}
    except Exception:
        pass
    concept_cache: dict = {}
    try:
        from src.data.concept_fetcher import load_stock_to_concepts
        concept_cache = load_stock_to_concepts() or {}
    except Exception:
        pass

    for code in missing_mc:
        zinfo = next((z for c, z in cand_codes if c == code), {})
        tx = tx_map.get(code)
        if tx is None:
            continue  # 拉不到市值就跳过
        try:
            mc = float(tx.get("market_cap_yi", 0) or 0)
            close = float(tx.get("close", 0) or 0)
        except (TypeError, ValueError):
            continue
        if mc <= 0:
            continue
        cand_with_mc.append(({
            "code": code,
            "name": zinfo.get("name") or tx.get("name", ""),
            "continuous_limit_up": int(zinfo.get("lbc", 0) or 0),
            "last_limit_up_time": zinfo.get("lbt", ""),
            "is_main_board": True,
            "market_cap_yi": mc,
            "industry": industry_cache.get(code, "未知"),
            "concepts": list(concept_cache.get(code) or []),
            "top_concepts": [],
            "close": close,
            "gain_10d": 0,
        }, mc))

    # 6. 过滤 流通市值≤100亿
    for info, mc in cand_with_mc:
        if 0 < mc <= 100:
            info["_pool_tag"] = "小盘接力"
            out.append(info)

    return out


def _qual_lbt(lbt: str) -> str:
    """封板时间定性"""
    if not lbt:
        return "—"
    t = str(lbt)
    if t < "10:00:00":
        return "早封板，强势"
    if t < "13:00:00":
        return "盘中板，正常"
    if t < "14:00:00":
        return "下午板，犹豫"
    return "尾盘板，谨慎"


def _qual_turnover(rate: float | None) -> str:
    if rate is None:
        return "—"
    if rate >= 20:
        return "极充分"
    if rate >= 10:
        return "充分"
    if rate >= 5:
        return "一般"
    return "偏低"


def _qual_concept(lu_count: int, ladder_2plus: int) -> str:
    if lu_count >= 5 and ladder_2plus >= 2:
        return "助攻强"
    if lu_count >= 3:
        return "助攻中"
    if lu_count >= 2:
        return "助攻弱"
    return "无助攻"


def _compute_watch_observation(row: dict, concept_zt_map: dict) -> dict:
    """6 字段观察要点"""
    code = str(row.get("code", ""))
    close = float(row.get("close", 0) or 0)
    lbt = str(row.get("last_limit_up_time") or "")
    turnover = row.get("turnover_rate") or row.get("turnover")
    if turnover is not None:
        try:
            turnover = float(turnover)
        except (TypeError, ValueError):
            turnover = None

    # 板块助攻：top_concepts[0] 在 concept_zt_stats 里查
    top_concepts = row.get("top_concepts") or []
    concept_name = top_concepts[0] if top_concepts else None
    concept_info = concept_zt_map.get(concept_name) if concept_name else None
    if concept_info:
        lu_count = int(concept_info.get("limit_up_count") or 0)
        ladder = concept_info.get("ladder") or {}
        # 同概念 ≥2板 数量
        ladder_2plus = sum(int(v) for k, v in ladder.items() if int(k) >= 2)
        ladder_1 = int(ladder.get("1", 0))
        concept_q = _qual_concept(lu_count, ladder_2plus)
        # 文案：新能源车今日涨停5只（2板2只），助攻强
        ladder_desc = []
        for board in sorted([int(k) for k in ladder.keys()], reverse=True):
            cnt = int(ladder.get(str(board), 0))
            if cnt > 0 and board >= 2:
                ladder_desc.append(f"{board}板{cnt}只")
        ladder_str = "（" + "/".join(ladder_desc) + "）" if ladder_desc else ""
        concept_text = f"{concept_name}今日涨停{lu_count}只{ladder_str}，{concept_q}"
    else:
        lu_count = 0
        ladder_2plus = 0
        concept_q = "—"
        concept_text = f"{concept_name or '—'}（无聚合数据）"

    # 双线突破 + 压力位 — 拉 K线 计算
    qiba: float | None = None
    mixian: float | None = None
    resistance: float | None = None
    above_both = False
    try:
        from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
        from src.engine.kline_chart import compute_indicators
        # 500 根日K（≈2 年）— 秘线/起拔线 需回溯到老历史顶
        df = fetch_kline(code, SCALE_DAILY, datalen=500)
        if df is not None and not df.empty and len(df) >= 35:
            ind = compute_indicators(df)
            qiba_arr = ind.get("qiba")
            if qiba_arr is not None and len(qiba_arr):
                last_q = qiba_arr[-1]
                if last_q is not None and not (isinstance(last_q, float) and (last_q != last_q)):  # NaN check
                    qiba = round(float(last_q), 2)
            mx = ind.get("mixian_y")
            if mx is not None:
                mixian = round(float(mx), 2)
            # 关键压力位：60 日内排除今日的 max(high)
            try:
                import numpy as _np
                highs = df["high"].astype(float).values[-60:-1]
                if len(highs) > 0:
                    resistance = round(float(_np.max(highs)), 2)
            except Exception:
                pass
    except Exception:
        pass

    # 双线突破判定（与 analyze_stock_action 同口径，严格 AND）：
    # 起拔/秘线必须都存在且 close 同时高于，才算双线突破；
    # 只一条达标 → above_both=False；都不存在（历史新高）→ above_both=True
    above_qiba = qiba is not None and close > qiba
    above_mixian = mixian is not None and close > mixian
    if qiba is None and mixian is None:
        above_both = True   # 全无数据 → 视为历史新高
    else:
        above_both = above_qiba and above_mixian

    distance_pct = None
    if resistance is not None and close > 0:
        distance_pct = round((resistance / close - 1) * 100, 1)

    return {
        "close": close,
        "lbt": lbt or None,
        "lbt_qualitative": _qual_lbt(lbt),
        "turnover": turnover,
        "turnover_qualitative": _qual_turnover(turnover),
        "concept": {
            "name": concept_name,
            "lu_count": lu_count,
            "ladder_2plus_count": ladder_2plus,
            "qualitative": concept_q,
            "text": concept_text,
        },
        "double_break": {
            "qiba": qiba,
            "mixian": mixian,
            "above_qiba": above_qiba,
            "above_mixian": above_mixian,
            "above_both": above_both,
        },
        "resistance": {
            "price": resistance,
            "distance_pct": distance_pct,
        },
    }


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
    *,
    session_today_key: str = "",
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

    # 2. 昨日空间板今日表现 — 从 limit_up_cache 昨日键取最高连板（与晋级矩阵同源）
    prev_space = None
    try:
        cache = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
        sk = str(session_today_key or review.date.replace("-", ""))
        ykey = _yesterday_cache_key(sk, cache)
        yleader = _space_leader_on_day(ykey, cache) if ykey else None
        if yleader:
            yc = yleader["code"]
            yn = yleader.get("name", "")
            ymax = int(yleader.get("board_count") or 0)
            today_pct = None
            today_open_pct = None
            today_zt = False
            zt_today_sb: dict = {}
            try:
                from src.data.zt_pool_api import fetch_zt_pool_with_retry

                zt_today_sb = fetch_zt_pool_with_retry(sk) or {}
            except Exception:
                pass
            zt_lbc_today = _zt_pool_lbc(zt_today_sb, yc)
            if zt_lbc_today >= ymax + 1:
                today_zt = True
            row = _build_spot_map_for_codes({yc}).get(yc) or {}
            if row:
                try:
                    close = float(row.get("close", 0) or 0)
                    pre = float(row.get("pre_close", 0) or 0)
                    op = float(row.get("open", 0) or 0)
                    if close > 0 and pre > 0:
                        today_pct = round((close / pre - 1) * 100, 2)
                        if not today_zt:
                            today_zt = today_pct >= _limit_up_close_thr(yc)
                    if op > 0 and pre > 0:
                        today_open_pct = round((op / pre - 1) * 100, 2)
                except (TypeError, ValueError):
                    pass
            if today_open_pct is None:
                today_open_pct = open_pct_for_session(yc, sk, row)
            if today_pct is None:
                tk = sk if sk in cache else (_limit_up_cache_keys_sorted(cache)[-1] if cache else "")
                today_codes_set = _merge_today_limit_up_codes(
                    cache.get(tk, []) or [],
                    zt_today_sb,
                    {yc: row} if row else None,
                )
                if yc in today_codes_set:
                    today_zt = True
                    today_pct = 9.99
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
        ins = load_json_file(insight_file) or {}
        acc = int(ins.get("accelerating_count", 0) or 0)
        dec = int(ins.get("decelerating_count", 0) or 0)
    except Exception:
        pass

    # 5. 昨日炸板今日均价 — 来自 latest_leader.yesterday_zb_today_auction.avg_change_pct
    zb_avg = None
    try:
        leader_file = DATA_DIR / "latest_leader.json"
        ld = load_json_file(leader_file) or {}
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


def hydrate_relay_env_from_stores(relay_env: dict | None, review_date: str = "") -> dict:
    """读库刷新接力情绪可漂移字段（不重跑复盘、默认不拉东财）。

    GET /api/review 在返回前调用：加速/减速 ← latest_insight；炸板反馈 ← latest_leader；
    昨日空间板今日涨跌 ← 已有 spot 缓存（10 分钟内）或涨停池，避免每次打开复盘页打行情。
    """
    out = dict(relay_env or {})

    try:
        ins = load_json_file(DATA_DIR / "latest_insight.json") or {}
        out["accelerating_count"] = int(ins.get("accelerating_count", 0) or 0)
        out["decelerating_count"] = int(ins.get("decelerating_count", 0) or 0)
    except Exception:
        pass

    try:
        ld = load_json_file(DATA_DIR / "latest_leader.json") or {}
        zb = ld.get("yesterday_zb_today_auction") or {}
        v = zb.get("avg_change_pct")
        if v is not None:
            out["yesterday_zb_today_avg_pct"] = round(float(v), 2)
    except Exception:
        pass

    prev = out.get("prev_space_board_today")
    if not isinstance(prev, dict) or not prev.get("code"):
        return out
    yc = _norm_zt_code(prev.get("code"))
    if not yc:
        return out

    today_pct = prev.get("today_pct")
    today_open_pct = prev.get("today_open_pct")
    today_held = prev.get("today_held")
    sk = _session_ymd_from_review_date(review_date)
    ymax = int(prev.get("yesterday_board") or 0)
    row: dict = {}
    zt_today_h: dict = {}
    try:
        from src.data.zt_pool_api import fetch_zt_pool_with_retry

        zt_today_h = fetch_zt_pool_with_retry(sk) or {}
    except Exception:
        pass
    zt_lbc = _zt_pool_lbc(zt_today_h, yc)
    if ymax > 0 and zt_lbc >= ymax + 1:
        today_held = True
    try:
        row = _build_spot_map_for_codes({yc}).get(yc) or {}
        if row:
            close = float(row.get("close", 0) or 0)
            pre = float(row.get("pre_close", 0) or 0)
            op = float(row.get("open", 0) or 0)
            if close > 0 and pre > 0:
                today_pct = round((close / pre - 1) * 100, 2)
                if today_held is not True:
                    today_held = today_pct >= _limit_up_close_thr(yc)
            if op > 0 and pre > 0:
                today_open_pct = round((op / pre - 1) * 100, 2)
    except Exception:
        pass
    if today_open_pct is None:
        today_open_pct = open_pct_for_session(yc, sk, row)

    if today_pct is None:
        try:
            cache = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
            keys_sorted = _limit_up_cache_keys_sorted(cache)
            tk = now_cn().strftime("%Y%m%d")
            if review_date:
                rk = review_date.replace("-", "")
                if len(rk) == 8 and rk in cache:
                    tk = rk
            elif tk not in cache and keys_sorted:
                tk = keys_sorted[-1]
            today_codes_set = _merge_today_limit_up_codes(
                cache.get(tk, []) or [],
                zt_today_h,
                {yc: row} if row else None,
            )
            if yc in today_codes_set:
                today_held = True
                if today_pct is None:
                    today_pct = 9.99
        except Exception:
            pass

    updated = dict(prev)
    if today_pct is not None:
        updated["today_pct"] = today_pct
    if today_open_pct is not None:
        updated["today_open_pct"] = today_open_pct
    if today_held is not None:
        updated["today_held"] = bool(today_held)
    out["prev_space_board_today"] = updated
    return out


def _build_scorecard(
    prev_board_groups: list[dict],
    relay_env: dict,
    sector_zt_stats: list[dict],
    limit_up_count: int,
    concept_zt_stats: list[dict] | None = None,
    highest_board: int = 0,
) -> dict:
    """接力环境评分卡（区域 A）"""
    # === 指标 1: 1进2 成功率 ≥15%（v3.2 校准：常态门槛）===
    g1 = next((g for g in prev_board_groups if g.get("prev_board") == 1), None)
    if g1:
        b1_promoted = len(g1.get("promoted") or [])
        b1_total = b1_promoted + len(g1.get("failed") or [])
        b1_rate = round(b1_promoted / b1_total * 100, 1) if b1_total else 0.0
    else:
        b1_promoted, b1_total, b1_rate = 0, 0, 0.0
    score_1 = 1 if b1_rate >= 15 else 0

    # === 指标 2: 2进3 成功率 ≥25%（与评分卡 target 一致）===
    # 晋级矩阵行警告：2进3 成功率 <40% 标 ⚠（弱于健康区，但不等于未达标）
    g2 = next((g for g in prev_board_groups if g.get("prev_board") == 2), None)
    if g2:
        b2_promoted = len(g2.get("promoted") or [])
        b2_total = b2_promoted + len(g2.get("failed") or [])
        b2_rate = round(b2_promoted / b2_total * 100, 1) if b2_total else 0.0
    else:
        b2_promoted, b2_total, b2_rate = 0, 0, 0.0
    score_2 = 1 if b2_rate >= 25 else 0

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
    score_3 = 1 if sec_concentration >= 35 else 0

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
    score_5 = 1 if lianban_index_pct > 1 else 0

    # === 指标 6: 高度突破（今日最高板 ≥ 昨日最高板 - 1 即达标）===
    # 阈值放宽：连板高度可比昨日小幅回落 1 板，仍视为接力延续
    prev_max = int(prev_space.get("yesterday_board") or 0)
    tier_max = max((int(g.get("prev_board") or 0) for g in prev_board_groups), default=0)
    if tier_max > prev_max:
        prev_max = tier_max
    today_max = int(highest_board or 0)
    if today_max > prev_max:
        score_6, height_text = 1.0, f"{today_max}板↑（昨{prev_max}板）"
    elif today_max == prev_max and today_max > 0:
        score_6, height_text = 1.0, f"{today_max}板=（昨{prev_max}板）"
    elif prev_max > 0 and today_max == prev_max - 1:
        score_6, height_text = 1.0, f"{today_max}板↓1（昨{prev_max}板，可接受）"
    else:
        score_6, height_text = 0.0, f"{today_max}板↓（昨{prev_max}板）"

    indicators = [
        {"label": "1进2成功率",   "today": f"{b1_rate}%",          "target": "≥15%", "score": score_1, "raw": b1_rate, "detail": f"{b1_promoted}/{b1_total}"},
        {"label": "2进3成功率",   "today": f"{b2_rate}%",          "target": "≥25%", "score": score_2, "raw": b2_rate, "detail": f"{b2_promoted}/{b2_total}"},
        {"label": "板块集中度",   "today": f"{sec_concentration}%","target": "≥35%", "score": score_3, "raw": sec_concentration, "detail": "前3概念涨停占比"},
        {"label": "空间板",       "today": space_status_text,      "target": "红盘晋级", "score": score_4, "raw": (space_open_pct if space_open_pct is not None else 0), "detail": (f"昨{prev_space.get('yesterday_board')}板{prev_space.get('name','')}" if prev_space else "—")},
        {"label": "昨日连板指数", "today": f"{lianban_index_pct:+.2f}%", "target": ">+1%", "score": score_5, "raw": lianban_index_pct, "detail": f"昨≥2板共{len(pcts)}只今日均值"},
        {"label": "高度突破",     "today": height_text,            "target": "≥昨高-1", "score": score_6, "raw": today_max - prev_max, "detail": f"今日最高{today_max}板 / 昨日{prev_max}板"},
    ]
    total_score = round(sum(ind["score"] for ind in indicators), 1)
    # 决策映射 → 明日接力参考（依据今日复盘六维）：
    #   ≤2 → 空仓
    #   3-4 → 轻仓 10-20%（仅 3进4 及以上）
    #   5   → 正常 30%（可做 2进3）
    #   6   → 重仓 50%+
    if total_score >= 6:
        decision, color = "重仓", "#ef4444"
        today_action = {
            "verdict": "重仓",
            "position": "≥50%",
            "ladders": "全梯队（2进3 / 3进4 / 4进5+）",
            "note": "六维满分，明日可全梯队接力",
        }
    elif total_score >= 5:
        decision, color = "正常", "#10b981"
        today_action = {
            "verdict": "正常",
            "position": "30%",
            "ladders": "2进3 / 3进4 / 4进5+",
            "note": "5/6 维达标，明日可做 2进3 起",
        }
    elif total_score >= 3:
        decision, color = "试错", "#fbbf24"
        today_action = {
            "verdict": "轻仓",
            "position": "10-20%",
            "ladders": "3进4 及以上",
            "note": "明日 2进3 暂不参与",
        }
    else:  # total_score <= 2
        decision, color = "空仓", "#6b7280"
        today_action = {
            "verdict": "空仓",
            "position": "0%",
            "ladders": "无",
            "note": "≤2 维达标，接力恶化，明日不开仓"
        }

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
        "today_action": today_action,
    }


def _build_decision_headline(
    total: float, decision: str, indicators: list[dict],
    prev_board_groups: list[dict], sec_concentration: float,
) -> str:
    """生成专业版决策建议（针对龙头 / 连板接力策略，融入周期系统理念）

    输出多行结构：
      第 1 行：周期阶段 + 关键指标 + 总评
      第 2 行：龙头组（龙妖股 / 弹性股）建议
      第 3 行：接力组（1进2 / 2进3 / 3进4 ...）建议
      第 4 行：风险点 / 信号位
    """
    # 1. 读周期阶段
    cycle_phase = ""
    cycle_day = 0
    rep_name = ""
    rep_gain = 0.0
    try:
        snap_file = DATA_DIR / "latest_snapshot.json"
        snap = load_json_file(snap_file) or {}
        cycle_phase = snap.get("phase", "") or ""
        cycle_day = int(snap.get("phase_day", 0) or 0)
        rep = snap.get("representative") or {}
        rep_name = rep.get("name", "") or ""
        rep_gain = float(rep.get("gain_10d", 0) or 0)
    except Exception:
        pass
    cyc_tag = f"{cycle_phase}{cycle_day}日" if cycle_phase else "周期未知"
    rep_tag = f"·龙头 {rep_name}({rep_gain:.0f}%)" if rep_name else ""

    # 2. 取关键指标
    ind_map = {i["label"]: i for i in indicators}
    b1 = ind_map.get("1进2成功率", {})
    b2 = ind_map.get("2进3成功率", {})
    space = ind_map.get("空间板", {})
    lianban_idx = ind_map.get("昨日连板指数", {})
    height = ind_map.get("高度突破", {})
    b1_rate = b1.get("today", "—")
    b2_rate = b2.get("today", "—")
    space_text = space.get("today", "—")
    lb_idx_text = lianban_idx.get("today", "—")
    height_text = height.get("today", "—")
    space_ok = space.get("score", 0) >= 1.0

    # 找最差项（score 最低，非 None）
    sortable = [i for i in indicators if i.get("score") is not None]
    sortable.sort(key=lambda i: i["score"])
    worst = sortable[0] if sortable else {}
    worst_label = worst.get("label", "无")
    worst_today = worst.get("today", "")

    # 中位股断层判定
    mid_count = sum(
        1 for g in prev_board_groups if g.get("prev_board", 0) > 0
        for s in (g.get("promoted") or []) if s.get("today_board", 0) >= 3
    )

    # 周期相位下的额外动作语
    is_ebb = cycle_phase in ("退潮期", "余温期", "混沌期")
    is_main_rise = cycle_phase in ("完整周期", "小周期完成")
    is_breeding = cycle_phase in ("孕育期", "小周期启动")

    # === 空仓 ===
    if decision == "空仓":
        line1 = (
            f"⛔ 空仓 | {cyc_tag}{rep_tag} | 1进2 {b1_rate}·2进3 {b2_rate}·空间板{space_text}"
            f"·连板指数{lb_idx_text} → 接力链断裂"
        )
        if is_ebb:
            line2 = "🐯 龙头组：龙妖T仓全清；余温/退潮期不接 7 字头反抽，盯老龙是否完成最后一次诱多。"
        elif is_breeding:
            line2 = "🐯 龙头组：弹性股仓位降至底仓，等下一波 5 日均线启动；不主动追买。"
        else:
            line2 = "🐯 龙头组：仓位 0；只用观察池标记次日可能反弹的一日游标的（昨日跌停/炸板今日反包）。"
        if mid_count <= 1:
            line3 = (
                f"🪜 接力组：中位股断层（3板及以上仅 {mid_count} 只），1进2 {b1_rate} 全是被动接力；"
                "不做 2进3 及以上，只看竞价分歧。"
            )
        else:
            line3 = (
                f"🪜 接力组：清空所有接力仓；1进2 {b1_rate}、空间板{space_text}，"
                "新进首板成功率太低，宁可错过不做错。"
            )
        line4 = (
            "🚦 重启信号：明日竞价 ≥2 项达标——板块集中度>35% / 空间板红盘晋级 / "
            "1进2>15% / 今日最高≥昨日最高-1，达 ≥2 项恢复试错仓位。"
        )
        return "\n".join([line1, line2, line3, line4])

    # === 试错 ===
    if decision == "试错":
        line1 = (
            f"🟡 试错 | {cyc_tag}{rep_tag} | 1进2 {b1_rate}·2进3 {b2_rate}·空间板{space_text}"
            f"·高度{height_text} → 接力生态半生不熟"
        )
        if is_main_rise and space_ok:
            line2 = (
                "🐯 龙头组：留 1 只最强龙头继任者底仓 ≤15%；空间板若再封 +1 板加 5%，"
                "炸板立即清。不接低位补涨股。"
            )
        elif is_breeding:
            line2 = (
                f"🐯 龙头组：弹性股关注度 ≥{rep_gain:.0f}% 的标的；只买红盘开盘+缩量阴线启动型，仓位 ≤15%。"
            )
        else:
            line2 = "🐯 龙头组：龙妖T仓 ≤10%；不持仓过夜，盘中只做最强龙头日内回撤反包。"
        line3 = (
            f"🪜 接力组：仓位严控 ≤20%。只做 3进4+（竞价 4-8% / 换手>0.5%）"
            f"{'，首选红盘高开+空间板未断' if space_ok else '，且昨日空间板今日不能跌停'}；"
            f"2进3 不参与，避免被动接力。"
        )
        line4 = f"🚦 风险位：{worst_label}（{worst_today}）。一旦再恶化立即清仓；破开盘价无条件出。"
        return "\n".join([line1, line2, line3, line4])

    # === 正常 ===
    if decision == "正常":
        line1 = (
            f"🟢 正常出击 | {cyc_tag}{rep_tag} | 板块集中度 {sec_concentration}%·1进2 {b1_rate}"
            f"·2进3 {b2_rate}·空间板{space_text} → 接力健康"
        )
        if is_main_rise:
            line2 = (
                f"🐯 龙头组：满仓持有空间板继任者，仓位 30-40%；"
                f"龙头{rep_name} 若放量再创新高，仓位上 40-50%；"
                "空间板若低开晋级则减 1/3 锁利。"
            )
        elif is_breeding:
            line2 = (
                "🐯 龙头组：弹性股选 1-2 只满仓持有，仓位 25-35%；"
                "重点关注次日是否打开新空间。"
            )
        else:
            line2 = "🐯 龙头组：龙妖股仓位 20-30%；只持有红盘晋级标的，绿盘走势收手。"
        line3 = (
            f"🪜 接力组：重点做 2进3 / 3进4 换手板，仓位 30-50%；"
            f"4 板及以上 {'可参与' if 'creating' in str(height_text) or '↑' in str(height_text) else '回避（除非高度突破）'}；"
            "首选红盘晋级股+主线方向。"
        )
        line4 = (
            f"🚦 最弱环节：{worst_label}（{worst_today}）。一旦该项再恶化先减半，"
            "次日空间板若低开 -2% 以上立即降至试错仓位。"
        )
        return "\n".join([line1, line2, line3, line4])

    # === 重仓 ===
    line1 = (
        f"🔴 重仓出击 | {cyc_tag}{rep_tag} | 板块集中度 {sec_concentration}%·1进2 {b1_rate}"
        f"·2进3 {b2_rate}·空间板{space_text}·高度{height_text} → 接力生态满血"
    )
    if is_main_rise:
        line2 = (
            f"🐯 龙头组：满仓持有龙头+次龙头，仓位 50-60%；"
            f"{rep_name}({rep_gain:.0f}%) 若继续封 +1 板，仓位最高上 65%；"
            "可中线持仓直至涨速放缓信号。"
        )
    else:
        line2 = (
            f"🐯 龙头组：弹性股满仓 40-50%；空间板红盘晋级则跟随加仓 5-10%；"
            "保持快进快出节奏。"
        )
    line3 = (
        f"🪜 接力组：全梯队参与 2进3 / 3进4 / 4进5，仓位 60-70%；"
        f"红盘晋级股优先，{'继续追高' if '↑' in str(height_text) else '主做高度持平梯队'}；"
        "破板后竞价位接低吸。"
    )
    line4 = (
        "🚦 唯一风险：空间板一旦炸板（盘中跌破封板价 ≥3 次），立即减一半仓位；"
        "次日竞价若 ≤+2% 全部清仓避免接力链塌陷连锁。"
    )
    return "\n".join([line1, line2, line3, line4])


def _flatten_failed_from_groups(groups: list[dict]) -> list[dict]:
    """从晋级矩阵各组 failed 扁平化（API 同步写库用）。"""
    out: list[dict] = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        pb = int(g.get("prev_board") or 0)
        for s in g.get("failed") or []:
            if not isinstance(s, dict):
                continue
            row = dict(s)
            row.setdefault("prev_board", pb)
            out.append(row)
    return out


def _hydrate_review_from_limit_up_cache(rev_date_iso: str, out: dict) -> bool:
    """从 limit_up_cache 刷新 limit_up_count / lianban_ladder / 板块概念统计（sync-persist 用）。"""
    sk = re.sub(r"\D", "", str(rev_date_iso or ""))[:8]
    if len(sk) != 8:
        return False
    try:
        cache = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
    except Exception:
        return False
    limit_up_data = list(cache.get(sk) or [])
    if not limit_up_data:
        return False

    lianban_data = _get_lianban_ladder(session_today_key=sk)
    out["limit_up_count"] = len(limit_up_data)
    out["main_board_limit_up"] = sum(
        1 for s in limit_up_data
        if not str(s.get("code", "")).startswith(("300", "301", "688", "8", "4"))
    )
    out["lianban_ladder"] = lianban_data
    out["highest_board"] = max((s.get("board_count", 0) for s in lianban_data), default=0)
    out["sector_zt_stats"] = _build_sector_zt_stats(lianban_data)
    out["concept_zt_stats"] = _build_concept_zt_stats(lianban_data)
    out["_limit_up_cache_hydrated"] = True
    return True


def rebuild_prev_board_groups_for_date(review_date_iso: str) -> list[dict]:
    """按复盘日 limit_up_cache 键重建晋级矩阵（历史快照缺 groups 时 API 兜底）。"""
    sk = re.sub(r"\D", "", str(review_date_iso or ""))[:8]
    if len(sk) != 8:
        return []
    try:
        cache = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
    except Exception:
        return []
    today_limit_up = cache.get(sk) or []
    if not today_limit_up:
        return []
    lianban_data = _get_lianban_ladder(session_today_key=sk)
    return _build_prev_board_groups(
        today_limit_up,
        lianban_data,
        session_today_key=sk,
    )


def apply_prev_space_board_patch(
    review: dict,
    patch: dict | None,
) -> dict:
    """人工更正昨日空间板（如 cache lbc 偏低：昨利仁 8 板写成 7）。"""
    if not isinstance(review, dict) or not isinstance(patch, dict) or not patch:
        return review
    out = dict(review)
    relay = dict(out.get("relay_env") or {})
    prev = dict(relay.get("prev_space_board_today") or {})
    for k, v in patch.items():
        if v is not None:
            prev[k] = v
    if prev.get("code"):
        prev["code"] = _norm_zt_code(str(prev.get("code")))
    relay["prev_space_board_today"] = prev
    out["relay_env"] = relay
    return out


def sync_review_payload_for_api(review: dict, *, persist: bool = False) -> dict:
    """GET /api/review 返回前：重建晋级矩阵/评分卡/空间板开盘，与连板天梯一致。

    评分卡仅由 prev_board_groups 推导。历史落库常带旧口径矩阵（如 16/61），
    若只在 groups 为空时才 rebuild，强刷页面仍会看到 26.2% 等旧数。
    因此只要有复盘日且 limit_up_cache 可重建，一律用新矩阵覆盖后再算 scorecard。
    persist=True 时将同步后的整包写回 DuckDB（latest_review + review_history）。
    """
    out = dict(review) if isinstance(review, dict) else {}
    rev_date = str(out.get("date") or "")[:10]
    groups = list(out.get("prev_board_groups") or [])
    if rev_date:
        rebuilt = rebuild_prev_board_groups_for_date(rev_date)
        if rebuilt:
            groups = rebuilt
            out["prev_board_groups"] = groups
            out["_prev_board_groups_rebuilt"] = True
        _hydrate_review_from_limit_up_cache(rev_date, out)

    if not groups:
        return out

    try:
        out["relay_env"] = enrich_prev_space_board_open(
            out.get("relay_env") or {},
            rev_date,
            groups,
        )
        out["promotion_summary"] = _build_promotion_summary(groups)
        out["failed_promotion_list"] = _flatten_failed_from_groups(groups)
        out["scorecard"] = _build_scorecard(
            groups,
            out.get("relay_env") or {},
            out.get("sector_zt_stats") or [],
            int(out.get("limit_up_count") or 0),
            concept_zt_stats=out.get("concept_zt_stats") or [],
            highest_board=int(out.get("highest_board") or 0),
        )
        out["_scorecard_synced"] = True
        if persist:
            persist_review_payload(out)
            out["_persisted"] = True
    except Exception as e:
        import traceback

        print(f"[复盘API] scorecard 同步失败: {e}")
        traceback.print_exc()
    return out


def persist_review_payload(data: dict) -> None:
    """将复盘 dict 写回 latest_review + review_history（与盘后 _save_review 同源）。"""
    if not isinstance(data, dict) or not data.get("date"):
        return
    rev_date = str(data["date"])[:10]
    dump_json_file(DATA_DIR / "latest_review.json", data)
    history_file = DATA_DIR / "review_history.json"
    history = load_json_file(history_file)
    if not isinstance(history, list):
        history = []
    history = [h for h in history if str(h.get("date") or "")[:10] != rev_date]
    history.append(data)
    history = history[-30:]
    dump_json_file(history_file, history)
    print(f"[复盘] 已写回库: {rev_date} prev_board_groups={len(data.get('prev_board_groups') or [])} 组")


def _save_review(review: DailyReview):
    """保存复盘结果（固定 UTF-8，避免 Windows 默认 GBK 无法写入 emoji/四字节字符）。"""
    data = asdict(review)
    dump_json_file(DATA_DIR / "latest_review.json", data)

    history_file = DATA_DIR / "review_history.json"
    history = load_json_file(history_file)
    if not isinstance(history, list):
        history = []
    history = [h for h in history if h.get("date") != review.date]
    history.append(data)
    history = history[-30:]
    dump_json_file(history_file, history)

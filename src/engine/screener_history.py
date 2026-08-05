"""选股记录模块

功能：
1. 每日9:27选股后归档当日选股结果
2. 每日15:30收盘后回填当日收盘价+日内表现
3. 次日9:27回填昨日记录的"次日竞价涨幅"
4. 按周/月/年/总统计胜率

数据结构（`screener_history_entry` 表，经 analytics_store 读写）：
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.config import DATA_DIR, now_cn
from src.data.json_io import load_json_file


def _norm_hist_code(code: str) -> str:
    """6 位数字代码（与东财/新浪混用时的 2918 vs 002918 对齐）。"""
    d = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not d:
        return ""
    if len(d) < 6:
        return d.zfill(6)
    return d[-6:].zfill(6)


def _load() -> list[dict]:
    from src.data.analytics_store import load_screener_history_entries

    return load_screener_history_entries()


def _save(records: list[dict]) -> None:
    from src.data.analytics_store import (
        backfill_daily_screener_hit_from_history,
        replace_screener_history_entries,
    )

    replace_screener_history_entries(records)
    try:
        backfill_daily_screener_hit_from_history()
    except Exception:
        pass


def _lookup_limit_up_time(code: str) -> str | None:
    """从 latest_review.lianban_ladder 查该 code 当日的封板时间（HH:MM:SS）；
    未涨停或无数据返回 None"""
    try:
        rd = load_json_file(DATA_DIR / "latest_review.json")
        if not rd:
            return None
        for s in (rd.get("lianban_ladder") or []):
            if str(s.get("code", "")) == str(code):
                lbt = s.get("lbt") or s.get("last_limit_up_time")
                return str(lbt) if lbt else None
    except Exception:
        pass
    return None


def _load_daily_market_env() -> dict:
    """9:27 选股市场环境（1进2/集中度 = 上一交易日复盘 scorecard）。"""
    from src.engine.screener_market_env import load_screener_market_env

    return load_screener_market_env()


def _get_market_highest_board() -> int:
    """获取市场当前最高连板数"""
    try:
        cache = load_json_file(DATA_DIR / "limit_up_cache.json")
        if not isinstance(cache, dict):
            return 0
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
        data = load_json_file(DATA_DIR / "latest_ranking.json")
        if data is not None:
            return {str(r.get("code", "")) for r in data.get("ranking", [])}
    except Exception:
        pass
    return set()


def _load_top30_streak_map() -> dict:
    """读取最新 ranking 中每只 TOP30 个股的连续在榜天数（top30_streak）"""
    try:
        data = load_json_file(DATA_DIR / "latest_ranking.json")
        if data is not None:
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
        ind_map = load_json_file(DATA_DIR / "industry_cache.json")
        if isinstance(ind_map, dict) and code in ind_map:
            return ind_map[code]
        rd = load_json_file(DATA_DIR / "latest_ranking.json")
        if rd is not None:
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


def archive_today_hits(hits: list[dict], spot_df=None, *, trade_date: str | None = None):
    """9:27选股后归档当日结果

    Args:
        hits: ScreenerHit 的 asdict() 列表
        spot_df: 实时行情（用于获取昨收价）
        trade_date: 归档交易日 YYYY-MM-DD；默认 now_cn() 当日
    """
    if not hits:
        return

    records = _load()
    today = str(trade_date or now_cn().strftime("%Y-%m-%d"))[:10]
    # 非交易日拒绝归档（避免周末/节假日污染选股历史）
    from src.config import is_trading_day
    if not is_trading_day():
        print(f"[选股记录] 跳过归档：{today} 为非交易日")
        return
    market_env = _load_daily_market_env()

    # 构建昨收价映射
    pre_close_map = {}
    if spot_df is not None and not spot_df.empty:
        for _, row in spot_df.iterrows():
            code = _norm_hist_code(row.get("code", ""))
            pc = float(row.get("pre_close", 0))
            if code and pc > 0:
                pre_close_map[code] = pc

    # 去重：同一天同一只股票不重复写入；重跑选股时更新 v4 决策快照
    existing = {(r["date"], r["code"]) for r in records}
    existing_idx = {(r["date"], r["code"]): i for i, r in enumerate(records)}

    top30_codes = _load_top30_codes()
    streak_map = _load_top30_streak_map()
    new_count = 0
    for h in hits:
        code = _norm_hist_code(h.get("code", ""))
        if not code:
            continue
        board = int(h.get("continuous_limit_up", 0) or 0)
        board_label = f"{board}进{board + 1}" if board >= 1 else "首板"
        key = (today, code)

        psd = h.get("per_stock_decision") or {}
        if not psd and board >= 2:
            try:
                from src.engine.screener_decision import compute_per_stock_decision
                from src.engine.screener_market_env import load_screener_review_context

                ctx = load_screener_review_context()
                psd = compute_per_stock_decision(
                    h,
                    market_env,
                    concept_zt_stats=ctx.get("concept_zt_stats"),
                    space_board_today=ctx.get("space_board_today"),
                    market_highest_board=ctx.get("market_highest_board"),
                    highest_board_tier_today=ctx.get("highest_board_tier_today"),
                )
            except Exception:
                psd = {}
        decision_snapshot = _decision_snapshot_from_psd(psd, market_env, h) if psd else None

        if key in existing:
            idx = existing_idx.get(key)
            if idx is not None and decision_snapshot:
                records[idx]["decision"] = decision_snapshot
                records[idx]["auction_gain"] = h.get("auction_gain", records[idx].get("auction_gain"))
                records[idx]["auction_turnover"] = h.get("auction_turnover")
                records[idx]["continuous_limit_up"] = board
                records[idx]["board_label"] = board_label
                if h.get("top_concepts"):
                    records[idx]["top_concepts"] = h.get("top_concepts")
                records[idx]["b1_rate"] = market_env.get("b1_rate")
                records[idx]["concentration"] = market_env.get("concentration")
            continue

        highest_board = _get_market_highest_board()
        industry = h.get("industry", "") or _lookup_industry(code)
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
            "decision": decision_snapshot,                        # v3.3 决策快照
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
            "top_concepts": h.get("top_concepts") or [],
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
    try:
        recompute_history_decisions_v4(pick_dates=[today])
    except Exception as e:
        print(f"[选股记录] 当日 v4 决策重算跳过: {e}")
    print(f"[选股记录] 归档 {new_count} 只 ({today}) · 市场跌停={market_env['market_limit_down']} "
          f"加权竞价={market_env['weighted_auction_gain']} 昨日连板均价={market_env['yesterday_lianban_today_avg']}")


def ensure_today_archived(*, spot_df=None) -> int:
    """幂等：latest_screener → screener_history_entry；必要时从 daily_screener_hit 补缺。"""
    today = now_cn().strftime("%Y-%m-%d")
    ymd = today.replace("-", "")

    data = load_json_file(DATA_DIR / "latest_screener.json") or {}
    hits = list(data.get("hits") or [])
    if hits:
        archive_today_hits(hits, spot_df=spot_df)

    try:
        from src.data.analytics_store import sync_screener_history_from_daily_hit

        n = sync_screener_history_from_daily_hit(hit_dates_yyyymmdd=[ymd])
        if n:
            print(f"[选股记录] 从 daily_screener_hit 补写 {n} 条 ({today})")
    except Exception as e:
        print(f"[选股记录] daily_screener_hit 补写失败: {e}")

    n_board = reconcile_history_board_counts(trade_dates=[today])
    if n_board:
        print(f"[选股记录] 连板校正 {n_board} 条 ({today})")

    return sum(1 for r in _load() if str(r.get("date") or "")[:10] == today)


def _board_as_of_pick_morning(code: str, pick_date_iso: str) -> int:
    """选股日 9:27 口径：上一交易日涨停池中的连板数（优先 lbc）。"""
    cache = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
    if not isinstance(cache, dict):
        return 0
    from src.engine.daily_review import _board_count_walk, _yesterday_cache_key

    pick_ymd = pick_date_iso.replace("-", "")[:8]
    if len(pick_ymd) != 8:
        return 0
    prev_key = _yesterday_cache_key(pick_ymd, cache)
    if not prev_key:
        return 0
    return _board_count_walk(_norm_hist_code(code), prev_key, cache)


def _tier_ctx_by_pick_date(records: list[dict]) -> dict[str, dict[str, Any]]:
    """同日归档记录 → 最高连板档均竞价（与当日 v4 个股决策一致）。"""
    by_date: dict[str, list[dict]] = {}
    for r in records:
        d = _record_date_str(r)
        if len(d) == 10:
            by_date.setdefault(d, []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for d, group in by_date.items():
        max_bc = max(int(x.get("continuous_limit_up") or 0) for x in group)
        if max_bc < 2:
            continue
        tier = [x for x in group if int(x.get("continuous_limit_up") or 0) == max_bc]
        pcts: list[float] = []
        for x in tier:
            try:
                ag = x.get("auction_gain")
                if ag is not None:
                    pcts.append(float(ag))
            except (TypeError, ValueError):
                continue
        if not pcts:
            continue
        avg = round(sum(pcts) / len(pcts), 2)
        nm = (
            f"{len(tier)}只{max_bc}板"
            if len(tier) > 1
            else str(tier[0].get("name") or "")
        )
        out[d] = {
            "yesterday_board": max_bc,
            "count": len(tier),
            "avg_today_pct": avg,
            "today_pct": avg,
            "name": nm,
        }
    return out


def _decision_snapshot_from_psd(psd: dict, env: dict, hit: dict | None = None) -> dict:
    snap = {
        "action": psd.get("action"),
        "position_pct": psd.get("position_pct"),
        "position_text": psd.get("position_text"),
        "ladder_label": psd.get("ladder_label"),
        "can_open": psd.get("can_open"),
        "reason": psd.get("reason"),
        "b1_rate": env.get("b1_rate"),
        "concentration": env.get("concentration"),
        "rules_version": psd.get("rules_version"),
    }
    # history 表无独立 prev_day 列，写入 decision_json 以便重算/回测复用
    src = hit if isinstance(hit, dict) else {}
    for k in ("prev_day_turnover", "prev_amount_ratio", "prev_day_yizi"):
        v = src.get(k)
        if v is not None:
            snap[k] = v
        elif psd.get(k) is not None:
            snap[k] = psd.get(k)
    return snap


def _decision_needs_v4_recompute(r: dict, *, force: bool = False) -> bool:
    """是否应按选股日 v4 重算 decision（含「已是 v4 但缺复盘环境」的脏快照）。"""
    if force:
        return True
    board = int(r.get("continuous_limit_up") or 0)
    if board < 2:
        return False
    dec = r.get("decision")
    expected_ladder = f"{board}进{board + 1}"
    if not dec or not isinstance(dec, dict):
        return True
    if dec.get("rules_version") != "v4.0":
        return True
    if dec.get("ladder_label") is None:
        return True
    if expected_ladder and dec.get("ladder_label") != expected_ladder:
        return True
    reason = str(dec.get("reason") or "")
    if dec.get("b1_rate") is None and (
        "晋级率缺失" in reason or "板块集中度未知" in reason
    ):
        return True
    return False


def _parse_prev_day_from_reason(reason: str) -> dict[str, float]:
    """从旧 decision.reason 文本回填昨换手/额比（离线重算，避免拉 K 线）。"""
    out: dict[str, float] = {}
    if not reason:
        return out
    import re

    m = re.search(r"昨日换手率\s*([-\d.]+)\s*%", reason)
    if m:
        try:
            out["prev_day_turnover"] = float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"(?:昨日/前日)?成交额比\s*([-\d.]+)", reason)
    if m:
        try:
            out["prev_amount_ratio"] = float(m.group(1))
        except ValueError:
            pass
    return out


def _recompute_record_decision(
    r: dict,
    pick_date: str,
    *,
    review_ctx: dict[str, Any] | None = None,
    tier_ctx: dict[str, Any] | None = None,
    allow_network_enrich: bool = False,
) -> bool:
    """按选股日 v4 规则重算 decision 快照（回测明细决策列）。"""
    board = int(r.get("continuous_limit_up") or 0)
    if board < 2:
        return False
    from src.engine.screener_decision import compute_per_stock_decision
    from src.engine.screener_market_env import (
        review_context_for_pick_date,
        scorecard_b1_and_concentration,
        load_review_document_for_pick_date,
    )

    prev_rev = load_review_document_for_pick_date(pick_date)
    b1, conc = scorecard_b1_and_concentration(prev_rev)
    if b1 is None and r.get("b1_rate") is not None:
        try:
            b1 = float(r.get("b1_rate"))
        except (TypeError, ValueError):
            b1 = None
    if b1 is None:
        return False
    ctx = review_ctx or review_context_for_pick_date(pick_date, tier_ctx=tier_ctx)
    top_concepts = list(r.get("top_concepts") or [])
    if not top_concepts and allow_network_enrich:
        try:
            from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts

            stub = [{"code": r.get("code"), "name": r.get("name")}]
            enrich_screener_hits_with_concepts(stub)
            top_concepts = list(stub[0].get("top_concepts") or [])
        except Exception:
            pass

    dec0 = r.get("decision") if isinstance(r.get("decision"), dict) else {}
    # 优先记录字段 → decision 快照 → 旧 reason 解析（history 表无 prev_day 列）
    for k in ("prev_day_turnover", "prev_amount_ratio", "prev_day_yizi"):
        if r.get(k) is None and dec0.get(k) is not None:
            r[k] = dec0.get(k)
    if r.get("prev_day_turnover") is None or r.get("prev_amount_ratio") is None:
        parsed = _parse_prev_day_from_reason(str(dec0.get("reason") or ""))
        for k, v in parsed.items():
            if r.get(k) is None:
                r[k] = v

    hit = {
        "code": r.get("code"),
        "name": r.get("name"),
        "continuous_limit_up": board,
        "auction_gain": r.get("auction_gain"),
        "auction_turnover": r.get("auction_turnover"),
        "auction_volume_ratio": r.get("auction_volume_ratio"),
        "market_cap": r.get("market_cap"),
        "top_concepts": top_concepts,
        "prev_day_turnover": r.get("prev_day_turnover"),
        "prev_amount_ratio": r.get("prev_amount_ratio"),
        "prev_day_yizi": r.get("prev_day_yizi"),
    }
    # 补齐昨换手/额比：默认仅本地 K 缓存；allow_network_enrich 才打外网
    if hit.get("prev_day_turnover") is None or hit.get("prev_amount_ratio") is None:
        try:
            from src.engine.screener_prev_day_enrich import enrich_hits_with_prev_day_kline

            stub = {"date": pick_date, "hits": [hit]}
            enrich_hits_with_prev_day_kline(
                stub, max_workers=2, cache_only=not allow_network_enrich,
            )
            for k in ("prev_day_turnover", "prev_amount_ratio", "prev_day_yizi"):
                if hit.get(k) is not None:
                    r[k] = hit.get(k)
        except Exception:
            pass

    # 2进3 缺昨换手/额比时，勿用「缺失」误判空仓覆盖历史决策
    if (
        board == 2
        and hit.get("prev_day_yizi") is not True
        and (hit.get("prev_day_turnover") is None or hit.get("prev_amount_ratio") is None)
        and not allow_network_enrich
    ):
        return False

    env = {
        "b1_rate": b1 if b1 is not None else r.get("b1_rate"),
        "concentration": conc if conc is not None else r.get("concentration"),
        "market_limit_down": r.get("market_limit_down"),
    }
    try:
        from src.engine.screener_decision import _resolve_space_red

        sr = _resolve_space_red(
            env, ctx.get("space_board_today"), ctx.get("highest_board_tier_today"),
        )
        if sr is not None:
            env["space_red"] = sr
    except Exception:
        pass
    try:
        psd = compute_per_stock_decision(
            hit,
            env,
            concept_zt_stats=ctx.get("concept_zt_stats") or [],
            space_board_today=ctx.get("space_board_today"),
            market_highest_board=ctx.get("market_highest_board"),
            highest_board_tier_today=ctx.get("highest_board_tier_today"),
        )
        r["decision"] = _decision_snapshot_from_psd(psd, env, hit)
        r["b1_rate"] = round(float(b1), 2)
        if conc is not None:
            r["concentration"] = round(float(conc), 2)
        return True
    except Exception:
        return False


def reconcile_history_board_counts(*, trade_dates: list[str] | None = None) -> int:
    """用 limit_up_cache 校正 screener_history 连板数 / board_label / decision（归档递推错误时）。"""
    records = _load()
    if not records:
        return 0

    date_set: set[str] | None = None
    if trade_dates:
        date_set = set()
        for d in trade_dates:
            s = str(d).strip()
            if len(s) == 8 and s.isdigit():
                date_set.add(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
            elif len(s) >= 10:
                date_set.add(s[:10])

    updated = 0
    for r in records:
        pick_date = _record_date_str(r)
        if date_set is not None and pick_date not in date_set:
            continue
        code = _norm_hist_code(str(r.get("code") or ""))
        if not code or len(pick_date) != 10:
            continue
        board = _board_as_of_pick_morning(code, pick_date)
        if board <= 0:
            continue
        old = int(r.get("continuous_limit_up") or 0)
        if board == old:
            continue
        r["continuous_limit_up"] = board
        r["board_label"] = f"{board}进{board + 1}" if board >= 1 else "首板"
        if _recompute_record_decision(r, pick_date):
            pass
        updated += 1
        print(f"[选股记录] 连板校正 {pick_date} {code} {old}→{board} ({r.get('board_label')})")

    if updated:
        _save(records)
        try:
            from src.data.analytics_store import backfill_daily_screener_hit_from_history

            backfill_daily_screener_hit_from_history(min_iso_date=min(date_set) if date_set else "2026-04-17")
        except Exception as e:
            print(f"[选股记录] daily_screener_hit 回写失败: {e}")
    return updated


def _record_date_str(r: dict) -> str:
    return str(r.get("date") or "")[:10]


def yesterday_pick_date(today: str | None = None) -> str | None:
    """最近一个早于 today 的选股日（与看板 yesterdaySelections 同源）。"""
    today_iso = str(today or now_cn().strftime("%Y-%m-%d"))[:10]
    dates = sorted(
        {
            _record_date_str(r)
            for r in _load()
            if _record_date_str(r) and _record_date_str(r) < today_iso
        }
    )
    return dates[-1] if dates else None


def _pick_date_market_settled(pick_date: str) -> bool:
    """选股日是否已过 A 股收盘（可落盘正式收盘价并将 pending→closed）。"""
    from src.market_schedule import is_post_market_data_window

    pick = str(pick_date or "")[:10]
    today = now_cn().strftime("%Y-%m-%d")
    if len(pick) != 10:
        return False
    if pick < today:
        return True
    if pick > today:
        return False
    return is_post_market_data_window()


def _spot_close_for_code(spot_df, code: str) -> float | None:
    if spot_df is None or getattr(spot_df, "empty", True):
        return None
    code = _norm_hist_code(code)
    if len(code) != 6:
        return None
    try:
        s = spot_df.copy()
        s["_c6"] = s["code"].astype(str).map(_norm_hist_code)
        row = s[s["_c6"] == code]
        if row.empty:
            return None
        c = float(row.iloc[0].get("close", 0) or 0)
        return c if c > 0 else None
    except Exception:
        return None


def _daily_kline_df(code: str, *, datalen: int = 120):
    from src.data.sina_kline_api import fetch_daily_kline_robust

    return fetch_daily_kline_robust(_norm_hist_code(code), min_bars=5, datalen=datalen)


def _kline_next_session(code: str, rec_date: str) -> tuple[float, float, str] | None:
    """选股日之后第一个交易日的 (开盘, 收盘, 日期)。"""
    rec_date = rec_date[:10]
    code = _norm_hist_code(code)
    if len(code) != 6 or len(rec_date) != 10:
        return None
    try:
        df = _daily_kline_df(code, datalen=60)
        if df is None or df.empty:
            return None
        dates = [str(row["date"])[:10] for _, row in df.iterrows()]
        if rec_date not in dates:
            return None
        idx = dates.index(rec_date)
        if idx + 1 >= len(df):
            return None
        row = df.iloc[idx + 1]
        o = float(row.get("open", 0) or 0)
        c = float(row.get("close", 0) or 0)
        d = dates[idx + 1]
        if o <= 0 and c <= 0:
            return None
        return (o, c, d)
    except Exception:
        return None


def _close_on_trade_date(code: str, trade_date: str) -> float | None:
    """日 K 中取 trade_date（YYYY-MM-DD）当日收盘价（新浪不足时东财回退）。"""
    trade_date = trade_date[:10]
    if len(trade_date) != 10:
        return None
    code = _norm_hist_code(code)
    if len(code) != 6:
        return None
    try:
        df = _daily_kline_df(code)
        if df is None or df.empty:
            return None
        for _, row in df.iterrows():
            if str(row.get("date"))[:10] == trade_date:
                v = float(row.get("close", 0))
                return v if v > 0 else None
    except Exception:
        pass
    return None


def _ensure_pre_close_from_auction(r: dict) -> None:
    """归档时 pre_close 常为 0：用 open_price 与 auction_gain 反推昨收。"""
    try:
        pc = float(r.get("pre_close") or 0)
    except (TypeError, ValueError):
        pc = 0.0
    if pc > 0:
        return
    try:
        op = float(r.get("open_price") or 0)
        ag = float(r.get("auction_gain") or 0)
    except (TypeError, ValueError):
        return
    if op > 0:
        r["pre_close"] = round(op / (1 + ag / 100), 2)


def _resolve_pre_close_for_record(r: dict) -> float:
    """选股日「昨收」：优先日 K 上一根收盘价（与 day% 口径一致），其次归档值，最后竞价反推。"""
    code = str(r.get("code") or "")
    trade_date = _record_date_str(r)
    pc = _pre_close_before_trade_date(code, trade_date)
    if pc and pc > 0:
        return round(pc, 2)
    try:
        stored = float(r.get("pre_close") or 0)
    except (TypeError, ValueError):
        stored = 0.0
    if stored > 0:
        return round(stored, 2)
    _ensure_pre_close_from_auction(r)
    try:
        return float(r.get("pre_close") or 0)
    except (TypeError, ValueError):
        return 0.0


def _apply_day_change_from_close(r: dict, close: float, pre_close: float) -> None:
    """按昨收与收盘价写入 day_change / 涨停炸板标记。"""
    open_p = float(r.get("open_price") or 0)
    code = str(r.get("code", ""))
    limit_threshold = 19.5 if code.startswith(("300", "301", "688")) else 9.8
    r["close_price"] = round(close, 2)
    r["close_gain"] = round((close / open_p - 1) * 100, 2) if open_p > 0 else 0
    if pre_close > 0:
        r["pre_close"] = round(pre_close, 2)
        day_change = round((close / pre_close - 1) * 100, 2)
    else:
        day_change = r.get("close_gain")
        if day_change is not None:
            day_change = round(float(day_change), 2)
    if day_change is None:
        return
    r["day_change"] = day_change
    r["is_limit_up"] = day_change >= limit_threshold
    if not r["is_limit_up"] and day_change >= limit_threshold * 0.85:
        r["is_zhaban"] = True
    elif r["is_limit_up"]:
        r["is_zhaban"] = False


def recalc_day_change_for_code(code: str, trade_date: str = "") -> dict:
    """用日 K 昨收重算指定标的 day_change（修复错误 pre_close 导致的 day% 偏差）。"""
    records = _load()
    code = _norm_hist_code(code)
    trade_date = (trade_date or "")[:10]
    updated = 0
    samples: list[dict] = []
    for r in records:
        if _norm_hist_code(str(r.get("code") or "")) != code:
            continue
        d = _record_date_str(r)
        if trade_date and d != trade_date:
            continue
        try:
            close = float(r.get("close_price") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        pre = _resolve_pre_close_for_record(r)
        if pre <= 0:
            continue
        old_dc = r.get("day_change")
        _apply_day_change_from_close(r, close, pre)
        if r.get("day_change") != old_dc or r.get("pre_close") != pre:
            updated += 1
            samples.append(
                {
                    "date": d,
                    "pre_close": r.get("pre_close"),
                    "day_change": r.get("day_change"),
                    "close_price": r.get("close_price"),
                }
            )
    if updated:
        _save(records)
    return {"code": code, "updated": updated, "records": samples}


def _limit_up_close_from_pre_close(code: str, pre_close: float) -> float:
    thresh = 1.20 if str(code).startswith(("300", "301", "688")) else 1.10
    return round(pre_close * thresh, 2)


def _infer_close_when_kline_missing(
    r: dict,
    spot_df=None,
    *,
    allow_today: bool = False,
) -> float | None:
    """K 线拉取失败时：昨收反推 → 涨停价估算 → 次日开盘价/竞价涨幅反推。

    当日盘中禁止用涨停价臆测收盘（会导致收盘涨幅虚高）；仅 spot 现价或留空。
    """
    pick_date = _record_date_str(r)
    today = now_cn().strftime("%Y-%m-%d")
    if pick_date == today and not allow_today:
        spot_c = _spot_close_for_code(spot_df, str(r.get("code") or ""))
        return spot_c
    _ensure_pre_close_from_auction(r)
    try:
        pre = float(r.get("pre_close") or 0)
    except (TypeError, ValueError):
        pre = 0.0

    try:
        ng = r.get("next_day_auction_gain")
        no = r.get("next_day_open")
        if no is not None and ng is not None:
            o = float(no)
            g = float(ng)
            if o > 0:
                return round(o / (1 + g / 100), 2)
    except (TypeError, ValueError):
        pass

    code = _norm_hist_code(str(r.get("code") or ""))
    if spot_df is not None and not getattr(spot_df, "empty", True) and len(code) == 6:
        try:
            s = spot_df.copy()
            s["_c6"] = s["code"].astype(str).map(_norm_hist_code)
            m = s[s["_c6"] == code]
            if not m.empty:
                row = m.iloc[0]
                o = float(row.get("open", 0) or 0)
                if o > 0 and pre > 0:
                    lim = _limit_up_close_from_pre_close(code, pre)
                    if int(r.get("continuous_limit_up") or 0) >= 1:
                        return lim
        except Exception:
            pass

    if pre > 0 and int(r.get("continuous_limit_up") or 0) >= 1:
        return _limit_up_close_from_pre_close(code, pre)
    return None


def _pre_close_before_trade_date(code: str, trade_date: str) -> float | None:
    """trade_date 前一交易日的收盘价（作昨收）。"""
    trade_date = trade_date[:10]
    code = _norm_hist_code(code)
    if len(code) != 6 or len(trade_date) != 10:
        return None
    try:
        df = _daily_kline_df(code)
        if df is None or df.empty:
            return None
        dates = [str(row.get("date"))[:10] for _, row in df.iterrows()]
        if trade_date not in dates:
            return None
        idx = dates.index(trade_date)
        if idx <= 0:
            return None
        v = float(df.iloc[idx - 1].get("close", 0))
        return v if v > 0 else None
    except Exception:
        return None


def _apply_close_to_pending_record(r: dict, close: float, *, finalize: bool | None = None) -> None:
    """写入收盘价；finalize=True 时 pending→closed 并判胜负，盘中仅刷新展示字段。"""
    pick_date = _record_date_str(r)
    settled = finalize if finalize is not None else _pick_date_market_settled(pick_date)
    pre_close = _resolve_pre_close_for_record(r)
    _apply_day_change_from_close(r, close, pre_close)
    if not settled:
        return
    if not r.get("is_limit_up"):
        r["is_win"] = False
    else:
        r["is_win"] = None
    r["status"] = "closed"


def _patch_close_price_keep_status(r: dict, close: float) -> None:
    """closed/settled 等记录若缺当日收盘价，用 K 线补上展示字段（不改 status / is_win）。"""
    pre_close = _resolve_pre_close_for_record(r)
    _apply_day_change_from_close(r, close, pre_close)


def repair_missing_close_prices() -> int:
    """date < 今天 且仍缺 close_price 的记录用日 K 补缺（含非 pending），幂等。"""
    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    n = _repair_missing_close_in_list(records, today)
    if n:
        _save(records)
        print(f"[选股记录] 补缺收盘价 {n} 条（历史漏填）")
    return n


def _repair_missing_close_in_list(records: list[dict], today: str) -> int:
    updated = 0
    for r in records:
        d = _record_date_str(r)
        code = _norm_hist_code(str(r.get("code") or ""))
        if len(d) != 10 or d >= today or len(code) != 6:
            continue
        cp = r.get("close_price")
        if cp is not None:
            try:
                if float(cp) > 0:
                    continue
            except (TypeError, ValueError):
                pass
        close = _close_on_trade_date(code, d)
        if not close or close <= 0:
            close = _infer_close_when_kline_missing(r)
        if not close or close <= 0:
            continue
        st = r.get("status")
        if st == "pending":
            _apply_close_to_pending_record(r, close, finalize=_pick_date_market_settled(d))
            updated += 1
        elif st in ("closed", "settled"):
            _patch_close_price_keep_status(r, close)
            updated += 1
    return updated


def backfill_close(spot_df):
    """收盘后回填收盘价：当日 pending + 历史上漏跑的 pending（避免次日竞价表永远等 9:27）。"""
    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    updated = _repair_missing_close_in_list(records, today)

    pending = [r for r in records if r.get("status") == "pending"]
    if not pending:
        if updated:
            _save(records)
            print(f"[选股记录] 回填收盘价（仅补缺）{updated} 只（基准日 {today}）")
        return

    from src.data.sina_kline_api import fetch_daily_kline_robust

    today_settled = _pick_date_market_settled(today)
    price_map_today: dict[str, float] = {}
    today_codes = list(dict.fromkeys(
        _norm_hist_code(str(r["code"]))
        for r in pending
        if _record_date_str(r) == today and r.get("code") and len(_norm_hist_code(str(r["code"]))) == 6
    ))
    if today_codes:
        if today_settled:
            for code in today_codes:
                try:
                    df = fetch_daily_kline_robust(code, min_bars=1, datalen=5)
                    if df is not None and not df.empty:
                        last_date = str(df.iloc[-1]["date"])[:10]
                        if last_date == today:
                            price_map_today[str(code)] = float(df.iloc[-1]["close"])
                except Exception:
                    pass
        for code in today_codes:
            sc = _spot_close_for_code(spot_df, code)
            if sc:
                price_map_today[str(code)] = sc

    if today_settled and spot_df is not None and not spot_df.empty:
        for _, row in spot_df.iterrows():
            code = _norm_hist_code(str(row.get("code", "")))
            if len(code) != 6 or code in price_map_today:
                continue
            close_val = float(row.get("close", 0))
            if close_val > 0:
                price_map_today[code] = close_val

    for r in pending:
        rec_date = _record_date_str(r)
        code = _norm_hist_code(str(r.get("code") or ""))
        if len(code) != 6 or len(rec_date) != 10 or rec_date > today:
            continue
        close = None
        if rec_date == today:
            close = price_map_today.get(code)
            if not close:
                close = _close_on_trade_date(code, rec_date)
        else:
            close = _close_on_trade_date(code, rec_date)

        if not close or close <= 0:
            close = _infer_close_when_kline_missing(
                r, spot_df, allow_today=_pick_date_market_settled(rec_date),
            )

        if close and close > 0:
            _apply_close_to_pending_record(
                r, close, finalize=_pick_date_market_settled(rec_date),
            )
            updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] 回填收盘价 {updated} 只（含历史 pending/补缺，基准日 {today}）")


def _apply_next_session_to_record(r: dict, close_p: float, next_open: float, next_close: float) -> bool:
    """写入次日开盘/竞价/收盘涨幅；返回是否有字段变更。"""
    changed = False
    if next_open > 0:
        no = round(next_open, 2)
        nag = round((next_open / close_p - 1) * 100, 2)
        if r.get("next_day_open") != no:
            r["next_day_open"] = no
            changed = True
        if r.get("next_day_auction_gain") != nag:
            r["next_day_auction_gain"] = nag
            changed = True
    if next_close > 0:
        ncg = round((next_close / close_p - 1) * 100, 2)
        if r.get("next_day_close_gain") != ncg:
            r["next_day_close_gain"] = ncg
            changed = True
    if not changed:
        return False
    is_limit_up = r.get("is_limit_up", False)
    next_close_gain = r.get("next_day_close_gain")
    if not is_limit_up:
        r["is_win"] = False
    elif next_close_gain is not None:
        r["is_win"] = next_close_gain > 0
    elif r.get("next_day_auction_gain") is not None:
        r["is_win"] = r["next_day_auction_gain"] > 0
    if r.get("status") == "closed":
        r["status"] = "settled"
    return True


def reconcile_next_day_from_kline(*, spot_df=None, lookback_days: int = 90) -> int:
    """按日 K 下一交易日重算 closed/settled 的次日三指标（修正错用竞价%当收盘%等）。"""
    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    cutoff = (now_cn() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    today_close_map: dict[str, float] = {}
    kline_cache: dict[str, Any] = {}
    if spot_df is not None and not getattr(spot_df, "empty", True):
        for _, row in spot_df.iterrows():
            c = _norm_hist_code(str(row.get("code", "")))
            v = float(row.get("close", 0) or 0)
            if len(c) == 6 and v > 0:
                today_close_map[c] = v

    updated = 0
    for r in records:
        if r.get("status") not in ("closed", "settled"):
            continue
        code = _norm_hist_code(str(r.get("code") or ""))
        rec_date = _record_date_str(r)
        if len(code) != 6 or len(rec_date) != 10 or rec_date < cutoff:
            continue

        ag = r.get("next_day_auction_gain")
        cg = r.get("next_day_close_gain")
        try:
            auction_eq_close = (
                ag is not None
                and cg is not None
                and abs(float(ag) - float(cg)) < 0.02
            )
        except (TypeError, ValueError):
            auction_eq_close = False
        try:
            op = float(r.get("open_price") or 0)
            ndo = float(r.get("next_day_open") or 0)
            open_same_as_pick = op > 0 and ndo > 0 and abs(op - ndo) < 0.02
        except (TypeError, ValueError):
            open_same_as_pick = False
        suspicious_flat_close = False
        try:
            if cg is not None and ag is not None and abs(float(cg)) < 0.02 and abs(float(ag)) > 0.5:
                suspicious_flat_close = True
        except (TypeError, ValueError):
            pass
        if (
            cg is not None
            and not auction_eq_close
            and not open_same_as_pick
            and not suspicious_flat_close
        ):
            continue

        try:
            close_p = float(r.get("close_price") or 0)
        except (TypeError, ValueError):
            close_p = 0.0
        kline_pick_close = _close_on_trade_date(code, rec_date)
        if kline_pick_close and kline_pick_close > 0:
            if close_p <= 0 or abs(close_p - kline_pick_close) > 0.05:
                r["close_price"] = round(kline_pick_close, 2)
                _patch_close_price_keep_status(r, kline_pick_close)
                close_p = kline_pick_close
        if close_p <= 0:
            if code not in kline_cache:
                kline_cache[code] = _daily_kline_df(code, datalen=80)
            inferred = _close_on_trade_date(code, rec_date)
            if inferred and inferred > 0:
                r["close_price"] = round(inferred, 2)
                _patch_close_price_keep_status(r, inferred)
                close_p = inferred
                updated += 1
        if close_p <= 0:
            continue

        if code not in kline_cache:
            kline_cache[code] = _daily_kline_df(code, datalen=80)
        sess = _kline_next_session(code, rec_date)
        if not sess:
            continue
        next_open, next_close, sess_date = sess
        if sess_date == today and code in today_close_map:
            next_close = today_close_map[code]
        if _apply_next_session_to_record(r, close_p, next_open, next_close):
            updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] 重算次日行情 {updated} 只（K线下一交易日）")
    return updated


def backfill_next_day_auction(spot_df, *, only_pick_date: str | None = None):
    """回填 status=closed 记录的次日竞价涨幅（K 线下一交易日；今日收盘可用 spot 补全）。

    only_pick_date: 若指定 YYYY-MM-DD，仅处理该选股日记录（9:27 发信前加速用）。
    """
    records = _load()
    today = now_cn().strftime("%Y-%m-%d")
    updated = 0
    pick = str(only_pick_date or "")[:10] if only_pick_date else ""

    for r in records:
        if pick and _record_date_str(r) != pick:
            continue
        need_full = r["status"] in ("closed", "pending")
        need_close_only = r["status"] == "settled" and r.get("next_day_close_gain") is None
        if not need_full and not need_close_only:
            continue

        # 发信路径：已有次日竞价% 则跳过，避免重复打 K 线
        if pick and need_full and r.get("next_day_auction_gain") is not None:
            continue

        code = _norm_hist_code(str(r.get("code") or ""))
        rec_date = _record_date_str(r)
        if len(code) != 6 or len(rec_date) != 10:
            continue

        try:
            close_p = float(r.get("close_price") or 0)
        except (TypeError, ValueError):
            close_p = 0.0
        if close_p <= 0:
            inferred = _close_on_trade_date(code, rec_date)
            if not inferred or inferred <= 0:
                inferred = _infer_close_when_kline_missing(
                    r, spot_df, allow_today=_pick_date_market_settled(rec_date),
                )
            if inferred and inferred > 0:
                _ensure_pre_close_from_auction(r)
                if r.get("status") == "pending":
                    _apply_close_to_pending_record(
                        r, float(inferred), finalize=_pick_date_market_settled(rec_date),
                    )
                else:
                    r["close_price"] = round(float(inferred), 2)
                    _patch_close_price_keep_status(r, float(inferred))
                close_p = float(inferred)
                updated += 1
        if close_p <= 0:
            continue

        # 9:27：优先用今日 spot 开盘算竞价%，跳过 K 线下一会话查询
        if pick and spot_df is not None and not getattr(spot_df, "empty", True):
            try:
                m = spot_df.copy()
                m["_c6"] = m["code"].astype(str).map(_norm_hist_code)
                row = m[m["_c6"] == code]
                if not row.empty:
                    open_p = float(row.iloc[0].get("open", 0) or 0)
                    if open_p <= 0:
                        open_p = float(row.iloc[0].get("close", 0) or 0)
                    if open_p > 0 and close_p > 0:
                        nag = round((open_p / close_p - 1) * 100, 2)
                        r["next_day_open"] = round(open_p, 2)
                        r["next_day_auction_gain"] = nag
                        updated += 1
                        continue
            except Exception:
                pass

        sess = _kline_next_session(code, rec_date)
        if not sess:
            continue
        next_open, next_close, sess_date = sess
        if sess_date == today and spot_df is not None and not spot_df.empty:
            try:
                m = spot_df.copy()
                m["_c6"] = m["code"].astype(str).map(_norm_hist_code)
                row = m[m["_c6"] == code]
                if not row.empty:
                    tc = float(row.iloc[0].get("close", 0) or 0)
                    if tc > 0:
                        next_close = tc
            except Exception:
                pass

        if need_close_only:
            if next_close > 0 and _apply_next_session_to_record(r, close_p, next_open, next_close):
                updated += 1
        elif next_open > 0 and _apply_next_session_to_record(r, close_p, next_open, next_close):
            updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] 回填次日竞价 {updated} 只")


def backfill_missing_b1_and_decision(*, force_v4: bool = False) -> int:
    """补全/重算 b1_rate、concentration、decision（按选股日复盘 + 同日最高连板档均竞价 v4）。"""
    from src.engine.screener_decision import compute_per_stock_decision
    from src.engine.screener_market_env import (
        review_context_for_pick_date,
        scorecard_b1_and_concentration,
        load_review_document_for_pick_date,
    )

    records = _load()
    if not records:
        return 0

    tier_by_date = _tier_ctx_by_pick_date(records)
    ctx_by_date: dict[str, dict[str, Any]] = {}
    updated = 0

    by_date: dict[str, list[dict]] = {}
    for r in records:
        d = _record_date_str(r)
        if len(d) == 10:
            by_date.setdefault(d, []).append(r)

    for pick_date, group in by_date.items():
        tier_ctx = tier_by_date.get(pick_date)
        if pick_date not in ctx_by_date:
            ctx_by_date[pick_date] = review_context_for_pick_date(
                pick_date, tier_ctx=tier_ctx,
            )
        prev_rev = load_review_document_for_pick_date(pick_date)
        b1, conc = scorecard_b1_and_concentration(prev_rev)
        if b1 is None:
            continue
        ctx = ctx_by_date[pick_date]

        for r in group:
            changed = False
            board = int(r.get("continuous_limit_up") or 0)
            if board < 2:
                continue

            if r.get("b1_rate") is None:
                r["b1_rate"] = round(float(b1), 2)
                changed = True
            if r.get("concentration") is None and conc is not None:
                r["concentration"] = round(float(conc), 2)
                changed = True

            need_dec = _decision_needs_v4_recompute(r, force=force_v4)
            if not need_dec:
                continue

            if _recompute_record_decision(
                r, pick_date, review_ctx=ctx, tier_ctx=tier_ctx,
            ):
                changed = True

            if changed:
                updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] 补全/重算 v4 决策 {updated} 条")
    return updated


def recompute_history_decisions_v4(*, pick_dates: list[str] | None = None) -> int:
    """强制按 v4 重算历史 decision（回测明细决策列）。"""
    records = _load()
    if not records:
        return 0
    date_set: set[str] | None = None
    if pick_dates:
        date_set = set()
        for d in pick_dates:
            s = str(d).strip()
            if len(s) == 8 and s.isdigit():
                date_set.add(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
            elif len(s) >= 10:
                date_set.add(s[:10])

    tier_by_date = _tier_ctx_by_pick_date(records)
    from src.engine.screener_market_env import review_context_for_pick_date

    ctx_by_date: dict[str, dict[str, Any]] = {}
    updated = 0
    for r in records:
        pick_date = _record_date_str(r)
        if date_set is not None and pick_date not in date_set:
            continue
        if pick_date not in ctx_by_date:
            ctx_by_date[pick_date] = review_context_for_pick_date(
                pick_date, tier_ctx=tier_by_date.get(pick_date),
            )
        if _recompute_record_decision(
            r,
            pick_date,
            review_ctx=ctx_by_date[pick_date],
            tier_ctx=tier_by_date.get(pick_date),
        ):
            updated += 1

    if updated:
        _save(records)
        print(f"[选股记录] v4 决策重算 {updated} 条")
    return updated


def _limit_up_threshold(code: str) -> float:
    c = str(code or "")
    return 19.5 if c.startswith(("300", "301", "688")) else 9.8


def _is_signal_limit_up(r: dict) -> bool:
    """当日收盘是否涨停（含已回填 is_limit_up 与 day_change 推断）。"""
    if r.get("is_limit_up") is True:
        return True
    if r.get("is_limit_up") is False:
        return False
    dc = r.get("day_change")
    if dc is None:
        return False
    try:
        return float(dc) >= _limit_up_threshold(str(r.get("code", "")))
    except (TypeError, ValueError):
        return False


def _signal_to_limit_pct(signals: list[dict]) -> float:
    """涨停转化率 = 当日收盘涨停数 / 当月（期）总选股信号数。"""
    if not signals:
        return 0.0
    zt = sum(1 for r in signals if _is_signal_limit_up(r))
    return round(zt / len(signals) * 100, 1)


def _board_type_label(r: dict) -> str:
    bl = r.get("board_label")
    if bl is not None and bl != "":
        label = str(bl)
    else:
        n = int(r.get("continuous_limit_up") or 0)
        label = f"{n}进{n + 1}"
    n = int(r.get("continuous_limit_up") or 0)
    if n >= 5 or label.startswith("5进") or label in ("5进6", "6进7", "7进8", "8进9"):
        return "5+"
    return label


def _month_range(year: int, month: int) -> tuple[str, str]:
    m_start = f"{int(year)}-{int(month):02d}-01"
    ny, nm = int(year), int(month) + 1
    if nm > 12:
        ny, nm = ny + 1, 1
    m_end = f"{ny}-{nm:02d}-01"
    return m_start, m_end


def _prev_month_year_month(year: int, month: int) -> tuple[int, int]:
    py, pm = int(year), int(month) - 1
    if pm < 1:
        return py - 1, 12
    return py, pm


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

    from src.engine.screener_backtest_report import _trade_return_pct

    # 只统计已有回测收益（收盘买→次日收盘卖）的记录
    judged = [r for r in records if _trade_return_pct(r) is not None]

    def _stat(subset):
        """Compute stats: 仅 can_open 计交易；样本含空仓；仓位按建议仓位加权复利。"""
        from src.engine.screener_backtest_report import (
            _build_equity_curve,
            _calc_max_drawdown,
            _compound_factor_by_weighted_rows,
            _record_can_open,
            _rows_by_date,
            _trade_return_pct,
        )

        samples: list[dict] = []
        trades: list[dict] = []
        for r in subset:
            row = dict(r)
            ret = _trade_return_pct(row)
            row["_ret"] = ret
            if ret is None:
                continue
            samples.append(row)
            if _record_can_open(row):
                trades.append(row)

        sample_n = len(samples)
        total = len(trades)
        if total == 0:
            return {
                "trades": 0, "wins": 0, "win_rate": 0,
                "avg_return": 0, "win_loss_ratio": 0,
                "cumulative_return": 0, "max_drawdown": 0,
                "zt_rate": 0, "win_amounts": [], "loss_amounts": [],
                "sample_count": sample_n,
                "trade_sample_ratio": 0.0 if sample_n else None,
            }

        rets = [float(r["_ret"]) for r in trades]
        wins = sum(1 for x in rets if x > 0)
        win_amounts = [x for x in rets if x > 0]
        loss_amounts = [x for x in rets if x <= 0]
        avg_return = sum(rets) / len(rets) if rets else 0

        rows_by_date = _rows_by_date(trades)
        factor = _compound_factor_by_weighted_rows(rows_by_date) if rows_by_date else 1.0
        cumulative_return = (factor - 1) * 100

        equity = _build_equity_curve(trades)
        max_drawdown, _ = _calc_max_drawdown(equity)

        avg_win = sum(win_amounts) / len(win_amounts) if win_amounts else 0
        avg_loss = abs(sum(loss_amounts) / len(loss_amounts)) if loss_amounts else 0
        win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        ratio = round(total / sample_n * 100, 1) if sample_n else None

        return {
            "trades": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1),
            "avg_return": round(avg_return, 2),
            "win_loss_ratio": round(win_loss_ratio, 2),
            "cumulative_return": round(cumulative_return, 2),
            "max_drawdown": round(max_drawdown, 2),
            "win_amounts": win_amounts,
            "loss_amounts": loss_amounts,
            "sample_count": sample_n,
            "trade_sample_ratio": ratio,
        }

    def _attach_signal_metrics(stat: dict, signals: list[dict]) -> dict:
        stat = dict(stat)
        # signal_count 保留「当期全部选股信号」；sample_count 为可结算收益样本
        stat["signal_count"] = len(signals)
        if "sample_count" not in stat:
            from src.engine.screener_backtest_report import _trade_return_pct
            stat["sample_count"] = sum(1 for r in signals if _trade_return_pct(r) is not None)
        pct = _signal_to_limit_pct(signals)
        stat["signal_to_limit_pct"] = pct
        stat["zt_rate"] = pct
        return stat

    def _basic_stat(subset):
        """Basic stats without per-record return arrays (for by_* sub-dimensions)."""
        from src.engine.screener_backtest_report import _record_can_open, _trade_return_pct

        samples = [r for r in subset if _trade_return_pct(r) is not None]
        trades = [r for r in samples if _record_can_open(r)]
        rets = [float(_trade_return_pct(r)) for r in trades]
        total = len(rets)
        sample_n = len(samples)
        if total == 0:
            return {
                "trades": 0, "wins": 0, "win_rate": 0, "zt_rate": 0,
                "sample_count": sample_n,
                "trade_sample_ratio": 0.0 if sample_n else None,
            }
        wins = sum(1 for x in rets if x > 0)
        return {
            "trades": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1),
            "zt_rate": _signal_to_limit_pct(subset),
            "signal_to_limit_pct": _signal_to_limit_pct(subset),
            "sample_count": sample_n,
            "trade_sample_ratio": round(total / sample_n * 100, 1) if sample_n else None,
        }

    # 本周（周一起，永远基于今天）
    weekday = now.weekday()
    monday = (now - timedelta(days=weekday)).strftime("%Y-%m-%d")
    weekly = [r for r in judged if r["date"] >= monday]

    # 上月（用于环比对比，始终比 monthly 早一个月）
    if filter_year and filter_month:
        # 选定月的上一个月
        prev_y, prev_m = int(filter_year), int(filter_month) - 1
        if prev_m < 1:
            prev_y, prev_m = prev_y - 1, 12
        pm_start = f"{prev_y}-{prev_m:02d}-01"
        ny, nm = prev_y, prev_m + 1
        if nm > 12:
            ny, nm = ny + 1, 1
        pm_end = f"{ny}-{nm:02d}-01"
    else:
        # 当前月的上一个月
        first_of_this_month = now.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        pm_start = last_month_end.strftime("%Y-%m-01")
        pm_end = now.strftime("%Y-%m-01")
    prev_monthly_signals = [r for r in records if pm_start <= r["date"] < pm_end]
    prev_monthly = [r for r in judged if pm_start <= r["date"] < pm_end]

    # 本月（已结算交易 vs 全部信号分口径）
    if filter_year and filter_month:
        m_start = f"{int(filter_year)}-{int(filter_month):02d}-01"
        ny, nm = int(filter_year), int(filter_month) + 1
        if nm > 12:
            ny, nm = ny + 1, 1
        m_end = f"{ny}-{nm:02d}-01"
        monthly_signals = [r for r in records if m_start <= r["date"] < m_end]
        monthly = [r for r in judged if m_start <= r["date"] < m_end]
    else:
        month_start = now.strftime("%Y-%m-01")
        monthly_signals = [r for r in records if r["date"] >= month_start]
        monthly = [r for r in judged if r["date"] >= month_start]

    # 本年
    if filter_year:
        ystr = str(int(filter_year))
        yearly = [r for r in judged if r["date"][:4] == ystr]
    else:
        year_start = now.strftime("%Y-01-01")
        yearly = [r for r in judged if r["date"] >= year_start]

    # 分维度子集：按 filter_year+filter_month 过滤（全部信号，非仅已结算）
    if filter_year and filter_month:
        scoped = monthly_signals
        scoped_judged = monthly
    elif filter_year:
        scoped = [r for r in records if str(r.get("date", ""))[:4] == str(int(filter_year))]
        scoped_judged = yearly
    else:
        scoped = records
        scoped_judged = judged

    def _board_dimension_stats(signals: list[dict], judged_rows: list[dict]) -> dict[str, dict]:
        from src.engine.screener_backtest_report import _trade_return_pct

        sig_groups: dict[str, list] = {}
        for r in signals:
            sig_groups.setdefault(_board_type_label(r), []).append(r)
        jud_groups: dict[str, list] = {}
        for r in judged_rows:
            jud_groups.setdefault(_board_type_label(r), []).append(r)
        out: dict[str, dict] = {}
        for label in sorted(set(sig_groups) | set(jud_groups)):
            sig = sig_groups.get(label, [])
            jud = jud_groups.get(label, [])
            base = _basic_stat(jud) if jud else {"trades": 0, "wins": 0, "win_rate": 0, "zt_rate": 0}
            rets = [float(x) for x in (_trade_return_pct(r) for r in jud) if x is not None]
            avg_ret = round(sum(rets) / len(rets), 2) if rets else None
            total_ret = round(sum(rets), 2) if rets else 0.0
            base["signal_count"] = len(sig)
            base["signal_to_limit_pct"] = _signal_to_limit_pct(sig)
            base["zt_rate"] = base["signal_to_limit_pct"]
            base["avg_return"] = avg_ret
            base["total_return"] = total_ret
            out[label] = base
        return out

    # === 分维度统计 ===
    board_stats = _board_dimension_stats(scoped, scoped_judged)

    # 上月各连板类型胜率（用于环比）
    prev_monthly_by_board: dict[str, dict] = {}
    if prev_monthly or prev_monthly_signals:
        prev_monthly_by_board = _board_dimension_stats(prev_monthly_signals, prev_monthly)

    # 按市场情绪（用跌停数分档）
    emotion_stats = {}
    for r in scoped_judged:
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
    emotion_stats = {k: _basic_stat(v) for k, v in emotion_stats.items()}

    # 按结果分类（涨停/炸板/未涨停）— 使用 _stat 而非 _basic_stat 以获得 zt_rate
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
    for r in scoped_judged:
        ind = r.get("industry", "未知") or "未知"
        by_industry.setdefault(ind, []).append(r)
    industry_stats = {k: _basic_stat(v) for k, v in sorted(by_industry.items(), key=lambda x: -len(x[1]))}

    cs_groups = {"周期股": [], "非周期股": []}
    for r in scoped_judged:
        if r.get("is_cycle_stock"):
            cs_groups["周期股"].append(r)
        elif r.get("is_cycle_stock") is False:
            cs_groups["非周期股"].append(r)
        # is_cycle_stock 为 None 的旧数据不计入
    cycle_stock_stats = {k: _basic_stat(v) for k, v in cs_groups.items() if v}

    result = {
        "total": _attach_signal_metrics(_stat(judged), records),
        "weekly": _attach_signal_metrics(_stat(weekly), [r for r in records if r["date"] >= monday]),
        "monthly": _attach_signal_metrics(_stat(monthly), monthly_signals),
        "yearly": _attach_signal_metrics(
            _stat(yearly),
            [r for r in records if str(r.get("date", ""))[:4] == (str(int(filter_year)) if filter_year else now.strftime("%Y"))],
        ),
        "prev_monthly": _attach_signal_metrics(_stat(prev_monthly), prev_monthly_signals),
        "by_board": board_stats,
        "prev_monthly_by_board": prev_monthly_by_board,
        "by_emotion": emotion_stats,
        "by_outcome": outcome_stats,
        "by_industry": industry_stats,
        "by_cycle_stock": cycle_stock_stats,
    }
    result["suggestion"] = gen_open_suggestion(result)
    result["backtest_rule"] = "收盘买→次日收盘卖 · 仅策略开仓计交易 · 建议仓位加权按日复利"
    return result


def calc_monthly_trend(months: int = 6) -> list[dict]:
    """近 N 个月的趋势数据（用于折线图），不含当月。

    每个元素：{ year, month, label, trades, win_rate, avg_return, zt_rate }
    """
    from src.config import now_cn
    from src.engine.screener_backtest_report import _trade_return_pct

    records = _load()

    now = now_cn()
    result: list[dict] = []

    for offset in range(1, months + 1):
        # 计算 offset 个月前的年月
        m = now.month - offset
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        m_start = f"{y}-{m:02d}-01"
        ny, nm = y, m + 1
        if nm > 12:
            ny, nm = ny + 1, 1
        m_end = f"{ny}-{nm:02d}-01"

        subset = [r for r in records if m_start <= r["date"] < m_end]
        judged_subset = [r for r in records if m_start <= r["date"] < m_end and _trade_return_pct(r) is not None]
        total = len(judged_subset)
        rets = [float(_trade_return_pct(r)) for r in judged_subset if _trade_return_pct(r) is not None]
        wins = sum(1 for x in rets if x > 0)
        avg_ret = sum(rets) / len(rets) if rets else 0
        zt_count = sum(1 for r in subset if _is_signal_limit_up(r))
        result.append({
            "year": y,
            "month": m,
            "label": f"{m}月",
            "trades": total,
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
            "avg_return": round(avg_ret, 2),
            "zt_rate": round(zt_count / len(subset) * 100, 1) if subset else 0,
            "signal_count": len(subset),
        })
    return list(reversed(result))  # 最早在前


def _hydrate_day_change_from_stored_close(r: dict) -> bool:
    """已有 close_price 但 day_change 空：用昨收反推（无网络）。"""
    if r.get("day_change") is not None:
        return False
    try:
        close = float(r.get("close_price") or 0)
    except (TypeError, ValueError):
        return False
    if close <= 0:
        return False
    pre_close = _resolve_pre_close_for_record(r)
    if pre_close <= 0:
        return False
    old_dc = r.get("day_change")
    _patch_close_price_keep_status(r, close)
    return r.get("day_change") != old_dc


def hydrate_settled_close_on_read(
    records: list[dict],
    *,
    allow_kline_for_today: bool = True,
) -> None:
    """读侧补收盘涨幅：先 close 反推，今日已收盘再拉日 K（不阻塞落库失败时的展示）。"""
    today = now_cn().strftime("%Y-%m-%d")
    for r in records:
        if r.get("day_change") is not None:
            continue
        pick = _record_date_str(r)
        if not pick or pick > today:
            continue
        if pick == today and not _pick_date_market_settled(pick):
            continue
        if _hydrate_day_change_from_stored_close(r):
            continue
        if not allow_kline_for_today or pick != today:
            continue
        code = str(r.get("code") or "")
        close = _close_on_trade_date(code, pick)
        if not close or close <= 0:
            continue
        if r.get("status") == "pending":
            _apply_close_to_pending_record(r, close, finalize=True)
        else:
            _patch_close_price_keep_status(r, close)


def refresh_today_close_light() -> int:
    """盘后轻量：仅用日 K 刷新今日 close/day_change（不拉全市场 spot）。

    供 screener-history?light=1 / 回测明细首屏在 15:00 后补收盘涨幅。
    """
    today = now_cn().strftime("%Y-%m-%d")
    if not _pick_date_market_settled(today):
        return 0
    records = _load()
    today_recs = [r for r in records if _record_date_str(r) == today]
    if not today_recs:
        return 0
    updated = 0
    for r in today_recs:
        code = str(r.get("code") or "")
        if _hydrate_day_change_from_stored_close(r):
            updated += 1
            continue
        if r.get("day_change") is not None:
            try:
                if float(r.get("close_price") or 0) > 0:
                    continue
            except (TypeError, ValueError):
                pass
        close = _close_on_trade_date(code, today)
        if not close or close <= 0:
            continue
        old_dc = r.get("day_change")
        old_cp = r.get("close_price")
        _apply_close_to_pending_record(r, close, finalize=True)
        if r.get("day_change") != old_dc or r.get("close_price") != old_cp:
            updated += 1
    if updated:
        try:
            _save(records)
            print(f"[选股记录] 盘后轻量刷新收盘价 {updated} 只 ({today})")
        except Exception as e:
            print(f"[选股记录] 盘后轻量刷新保存失败（读侧仍会补展示）: {e}")
    return updated


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
    today_recs = [r for r in records if str(r.get("date") or "") == today]
    if not today_recs:
        return 0

    reconcile_history_board_counts(trade_dates=[today])

    # 1) 市场环境（与首页保持同源）
    env = _load_daily_market_env()

    # 2) TOP30 周期股集合 + 周期计数（streak）映射
    top30_codes = _load_top30_codes()
    streak_map = _load_top30_streak_map()

    today_settled = _pick_date_market_settled(today)
    spot_df = None
    if not today_settled:
        try:
            from src.data.fetcher import fetch_realtime_spot

            spot_df = fetch_realtime_spot()
        except Exception:
            pass

    # 3) K线 / 现价刷新每只标的的当日数据
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

        # 当日收盘价：盘后优先日 K，盘中用 spot；避免未收盘时用涨停价臆测
        kline_close = None
        kline_high = None
        df = None
        last_date = ""
        if fetch_kline is not None:
            try:
                df = fetch_kline(code, SCALE_DAILY, datalen=12)
                if df is not None and len(df) >= 1:
                    last_date = str(df.iloc[-1]["date"])[:10]
                    if last_date == today and today_settled:
                        kline_close = float(df.iloc[-1]["close"])
                        kline_high = float(df.iloc[-1]["high"]) if "high" in df.columns else kline_close
            except Exception:
                pass
        if not kline_close:
            sc = _spot_close_for_code(spot_df, code)
            if sc and sc > 0:
                kline_close = sc
                kline_high = sc
        if kline_close and kline_close > 0:
            open_p = r.get("open_price", 0) or 0
            pre_close = _resolve_pre_close_for_record(r)
            new_close_price = round(kline_close, 2)
            new_close_gain = round((kline_close / open_p - 1) * 100, 2) if open_p > 0 else 0
            new_day_change = (
                round((kline_close / pre_close - 1) * 100, 2)
                if pre_close > 0
                else new_close_gain
            )
            if r.get("close_price") != new_close_price:
                r["close_price"] = new_close_price
                changed = True
            if r.get("close_gain") != new_close_gain:
                r["close_gain"] = new_close_gain
                changed = True
            if r.get("day_change") != new_day_change:
                r["day_change"] = new_day_change
                changed = True
            limit_thr = 19.5 if str(code).startswith(("300", "301", "688")) else 9.8
            new_lu = new_day_change >= limit_thr
            kh = kline_high if kline_high is not None else kline_close
            new_zb = (
                (not new_lu) and pre_close > 0 and (kh / pre_close - 1) * 100 >= limit_thr
            )
            if r.get("is_limit_up") != new_lu:
                r["is_limit_up"] = new_lu
                changed = True
            if r.get("is_zhaban") != new_zb:
                r["is_zhaban"] = new_zb
                changed = True
            if today_settled and r.get("status") in ("pending", "closed"):
                if not new_lu:
                    r["is_win"] = False
                if r.get("status") == "pending":
                    r["status"] = "closed"
                    changed = True
            if df is not None and len(df) >= 1:
                idx = max(0, len(df) - 11) if last_date == today else max(0, len(df) - 10)
                base = float(df.iloc[idx]["close"])
                if base > 0:
                    new_g10 = round((kline_close / base - 1) * 100, 2)
                    if r.get("gain_10d") != new_g10:
                        r["gain_10d"] = new_g10
                        changed = True

        if changed:
            updated += 1

    if updated:
        _save(records)
    return updated


def gen_board_tier_guidance(by_board: dict) -> tuple[list[str], list[str]]:
    """按连板梯队生成分档执行规则与风险规避（始终输出，小样本走观察分支）。"""
    rules: list[str] = []
    risks: list[str] = []

    def _bv(key: str) -> tuple[int, float]:
        b = by_board.get(key) or {}
        t = int(b.get("trades", 0) or 0)
        wr = float(b.get("win_rate", 0) or 0) if t > 0 else 0.0
        return t, wr

    t45, wr45 = _bv("4进5")
    if t45 <= 0:
        risks.append("4进5 本月无已结算样本，出现信号时先半仓验证")
    elif wr45 > 60 and t45 >= 3:
        rules.append(f"4进5 本月胜率 {wr45}%，持续有效，可继续重仓（4层）")
    elif wr45 > 60:
        rules.append(f"4进5 本月胜率 {wr45}%（样本较少），可正常参与但需观察")
    else:
        risks.append(f"4进5 本月表现不佳（胜率 {wr45}%），暂停或大幅减仓")

    t5, wr5 = _bv("5+")
    if t5 <= 0:
        risks.append("5+ 本月无已结算样本，高位接力谨慎")
    elif wr5 > 60 and t5 >= 3:
        rules.append(f"5+ 本月胜率 {wr5}%，持续有效，可继续重仓（4层）")
    elif wr5 > 60:
        rules.append(f"5+ 本月胜率 {wr5}%（样本较少），可正常参与但需观察")
    else:
        risks.append(f"5+ 本月表现不佳（胜率 {wr5}%），暂停或大幅减仓")

    t34, wr34 = _bv("3进4")
    if t34 <= 0:
        risks.append("3进4 本月无已结算交易，可能市场高度不足；若下月出现，优先半仓参与")
    elif wr34 >= 50 and t34 >= 3:
        rules.append(f"3进4 本月胜率 {wr34}%（{t34} 笔），可按半仓（2层）参与")
    elif wr34 < 35 and t34 >= 3:
        risks.append(f"3进4 本月胜率 {wr34}%，下月谨慎或只做最强主线")
    elif wr34 > 45:
        rules.append(f"3进4 本月胜率 {wr34}%（{t34} 笔），可半仓参与")
    else:
        risks.append(f"3进4 本月胜率 {wr34}%（{t34} 笔），观望为主")

    t23, wr23 = _bv("2进3")
    if t23 <= 0:
        risks.append("2进3 本月无已结算样本")
    elif wr23 < 30 and t23 >= 3:
        risks.append(f"2进3 本月胜率仅 {wr23}%，下月继续放弃")
    elif wr23 > 45:
        rules.append(f"2进3 本月胜率 {wr23}%，可按小仓（1.5层）参与竞价 5~7.5% 窗口")
    elif t23 < 3:
        risks.append(f"2进3 本月胜率 {wr23}%（{t23} 笔，样本少），小仓验证")
    else:
        risks.append(f"2进3 本月胜率 {wr23}%（{t23} 笔），暂不参与或极小仓")

    return rules, risks


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

    # 优先使用按月/按年过滤后的 scoped 数据（by_* 子集），而非 total（全量历史）
    # 当 filter_year+filter_month 指定时，monthly 才是真正的"整体"；total 是无关的噪声
    scoped_total = stats.get("monthly") or stats.get("yearly") or stats.get("total") or {}
    n = int(scoped_total.get("trades", 0) or 0)
    wr = float(scoped_total.get("win_rate", 0) or 0)
    wins = int(scoped_total.get("wins", 0) or 0)

    by_board = stats.get("by_board", {}) or {}

    # ===== 连板梯队执行规则（优先展示，始终生成） =====
    tier_rules, tier_risks = gen_board_tier_guidance(by_board)
    rules.extend(tier_rules)
    risks.extend(tier_risks)

    # ===== 样本量与可信度 =====
    if n == 0:
        return {
            "headline": "暂无样本，先按既定体系跑一段时间再回看",
            "rules": tier_rules,
            "risks": tier_risks,
            "notes": ["当前周期无已结算选股记录"],
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

    # ===== 涨停转化率（核心质量指标）— 基于当月全部选股信号 =====
    by_outcome = stats.get("by_outcome", {}) or {}
    zt_rate = float(
        scoped_total.get("signal_to_limit_pct")
        or scoped_total.get("zt_rate", 0)
        or 0
    )
    zb_rate = 0.0
    if n >= 5:
        zb_count = (by_outcome.get("涨停炸板") or {}).get("trades", 0)
        zb_rate = round(zb_count / n * 100, 1) if n else 0
        if zt_rate >= 50:
            rules.append(f"信号→涨停转化率 {zt_rate}%，信号识别强，按既定标准开仓即可")
        elif zt_rate >= 30:
            rules.append(f"信号→涨停转化率 {zt_rate}%，中等水平，尽量在竞价 +3~+6% 区间介入")
        else:
            risks.append(f"信号→涨停转化率仅 {zt_rate}%，多数标的没站上涨停——筛选条件偏宽，建议收紧前置（量比、板块强度）")
        if zb_rate >= 20:
            risks.append(f"涨停炸板率 {zb_rate}%，情绪反复严重——开盘买入后设 1.5~2% 止损，破开盘价快出")

    # ===== 连板甜点（样本≥3 的辅助结论） =====
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
    try:
        from src.engine.next_day_sell_advice import hydrate_yesterday_sell_advice_on_store

        hydrate_yesterday_sell_advice_on_store()
    except Exception as e:
        print(f"[选股记录] 次日卖出建议读库补全失败: {e}")

    records = _load()
    if year:
        ystr = f"{int(year):04d}"
        if month:
            prefix = f"{ystr}-{int(month):02d}"
            records = [r for r in records if str(r.get("date", ""))[:7] == prefix]
        else:
            records = [r for r in records if str(r.get("date", ""))[:4] == ystr]
    records.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    if limit and limit > 0:
        records = records[:limit]
    hydrate_settled_close_on_read(records)
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


def _metrics_from_stat(stat: dict) -> dict:
    return {
        "total_trades": int(stat.get("trades", 0) or 0),
        "signal_count": int(stat.get("signal_count", stat.get("trades", 0)) or 0),
        "win_rate": float(stat.get("win_rate", 0) or 0),
        "avg_return_pct": stat.get("avg_return"),
        "profit_loss_ratio": stat.get("win_loss_ratio"),
        "cum_return_pct": stat.get("cumulative_return"),
        "max_drawdown_pct": stat.get("max_drawdown"),
        "signal_to_limit_pct": float(
            stat.get("signal_to_limit_pct") or stat.get("zt_rate", 0) or 0
        ),
    }


def _board_breakdown_from_stats(by_board: dict, *, month_total_return: float) -> dict:
    tiers = ["2进3", "3进4", "4进5", "5+"]
    out: dict[str, dict] = {}
    for tier in tiers:
        s = by_board.get(tier) or {}
        trades = int(s.get("trades", 0) or 0)
        total_ret = float(s.get("total_return", 0) or 0)
        wr = s.get("win_rate")
        contribution = 0.0
        if month_total_return and abs(month_total_return) > 0.01:
            contribution = round(total_ret / month_total_return * 100, 2) / 100
        elif total_ret:
            contribution = round(total_ret, 2)
        tags: list[str] = []
        if trades == 0:
            tags.append("无样本")
        elif trades < 3:
            tags.append("样本少")
        if month_total_return:
            if total_ret > 0 and abs(total_ret) >= abs(month_total_return) * 0.4:
                tags.append("主要盈利源")
            elif total_ret < 0 and abs(total_ret) >= abs(month_total_return) * 0.4:
                tags.append("主要亏损源")
        out[tier] = {
            "trades": trades,
            "win_rate": wr if trades > 0 else None,
            "total_return": round(total_ret, 2),
            "contribution": round(contribution, 2),
            "tags": tags,
        }
    return out


def gen_monthly_improvement_suggestions(
    metrics: dict,
    prev_metrics: dict,
    history_avg: dict,
    board_breakdown: dict,
) -> list[str]:
    """基于本月 vs 历史对比，生成 3 条下月改进建议。"""
    suggestions: list[str] = []

    b23 = board_breakdown.get("2进3") or {}
    wr23 = b23.get("win_rate")
    hist23 = history_avg.get("board_2_3_win_rate")
    if wr23 is not None and hist23 is not None and b23.get("trades", 0) >= 3:
        if wr23 < hist23 - 5:
            suggestions.append(
                f"本月 2进3 胜率 {wr23}%，低于历史均值 {hist23}%，"
                f"建议下月只做竞价 5~7.5% + 晋级率 12~15% 的窗口"
            )
    elif b23.get("trades", 0) == 0:
        suggestions.append("本月无 2进3 开仓样本，下月若出现可小仓验证，勿盲目放大")

    b34 = board_breakdown.get("3进4") or {}
    if b34.get("trades", 0) == 0:
        suggestions.append("本月无 3进4 交易，可能市场高度不足；若下月出现，优先半仓参与")

    wr = metrics.get("win_rate")
    avg_wr = history_avg.get("win_rate")
    trades = metrics.get("total_trades", 0)
    weekly = history_avg.get("weekly_trades")
    if weekly and trades:
        weeks_in_month = 4.0
        curr_weekly = trades / weeks_in_month
        if curr_weekly < weekly * 0.6:
            suggestions.append(
                f"本月交易频率下降（周均 {curr_weekly:.2f} 笔 vs 历史 {weekly:.1f} 笔），"
                f"可接受空仓，但需检查选股条件是否过严导致错失机会"
            )
        elif wr is not None and avg_wr is not None and wr < avg_wr - 8:
            suggestions.append(
                f"本月胜率 {wr}% 低于历史均值 {avg_wr}%，建议收紧弱势市场开仓、优先做高转化连板档"
            )

    sig_zt = metrics.get("signal_to_limit_pct")
    prev_zt = prev_metrics.get("signal_to_limit_pct")
    if sig_zt is not None and prev_zt is not None and sig_zt < prev_zt - 15:
        suggestions.append(
            f"涨停转化率 {sig_zt}% 较上月 {prev_zt}% 明显下滑，下月前置过滤（板块强度/量比）需加强"
        )

    if not suggestions:
        suggestions.append("本月表现与历史接近，维持现有节奏，继续累积样本验证")
    return suggestions[:3]


def build_monthly_review(year: int | None = None, month: int | None = None) -> dict:
    """按月聚合复盘数据（月度复盘页专用 API）。"""
    now = now_cn()
    if year is None or month is None:
        y, m = now.year, now.month
    else:
        y, m = int(year), int(month)

    stats = calc_win_stats(filter_year=y, filter_month=m)
    monthly = stats.get("monthly") or {}
    prev = stats.get("prev_monthly") or {}
    by_board = stats.get("by_board") or {}

    month_total_return = float(monthly.get("cumulative_return") or 0)
    board_breakdown = _board_breakdown_from_stats(by_board, month_total_return=month_total_return)

    trend_raw = calc_monthly_trend(6)
    trend_data = [
        {
            "month": f"{d['year']}-{d['month']:02d}",
            "win_rate": d.get("win_rate", 0),
            "total_trades": d.get("trades", 0),
            "avg_return": d.get("avg_return", 0),
            "signal_to_limit_pct": d.get("zt_rate", 0),
        }
        for d in trend_raw
    ]

    # 近 6 个月历史均值（不含本月）
    if trend_raw:
        hist_wr = [d["win_rate"] for d in trend_raw if d.get("trades", 0) > 0]
        hist_ar = [d["avg_return"] for d in trend_raw if d.get("avg_return") is not None]
        hist_tr = [d["trades"] for d in trend_raw]
        hist_zt = [d.get("zt_rate", 0) for d in trend_raw if d.get("signal_count", d.get("trades", 0)) > 0]
        history_avg = {
            "win_rate": round(sum(hist_wr) / len(hist_wr), 1) if hist_wr else 0,
            "avg_return": round(sum(hist_ar) / len(hist_ar), 2) if hist_ar else 0,
            "weekly_trades": round(sum(hist_tr) / len(hist_tr) / 4, 2) if hist_tr else 0,
            "signal_to_limit_pct": round(sum(hist_zt) / len(hist_zt), 1) if hist_zt else 0,
        }
    else:
        history_avg = {
            "win_rate": 0, "avg_return": 0, "weekly_trades": 0, "signal_to_limit_pct": 0,
        }

    # 近 6 个月各连板档胜率均值（不含本月）
    records = _load()
    board_wr_hist: dict[str, list[float]] = {"2进3": [], "3进4": [], "4进5": []}
    for offset in range(1, 7):
        cm = now.month - offset
        cy = now.year
        while cm <= 0:
            cm += 12
            cy -= 1
        ms, me = _month_range(cy, cm)
        jud = [r for r in records if ms <= r["date"] < me and r.get("is_win") is not None]
        bd = _board_dimension_stats_local(jud, jud)
        for tier in board_wr_hist:
            if tier in bd and bd[tier].get("trades", 0) >= 1:
                board_wr_hist[tier].append(float(bd[tier]["win_rate"]))
    history_avg["board_2_3_win_rate"] = round(sum(board_wr_hist["2进3"]) / len(board_wr_hist["2进3"]), 1) if board_wr_hist["2进3"] else None
    history_avg["board_3_4_win_rate"] = round(sum(board_wr_hist["3进4"]) / len(board_wr_hist["3进4"]), 1) if board_wr_hist["3进4"] else None
    history_avg["board_4_5_win_rate"] = round(sum(board_wr_hist["4进5"]) / len(board_wr_hist["4进5"]), 1) if board_wr_hist["4进5"] else None

    py, pm = _prev_month_year_month(y, m)
    metrics = _metrics_from_stat(monthly)
    prev_metrics = _metrics_from_stat(prev)
    improvements = gen_monthly_improvement_suggestions(
        metrics, prev_metrics, history_avg, board_breakdown,
    )

    return {
        "current_month": f"{y}-{m:02d}",
        "prev_month": f"{py}-{pm:02d}",
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": metrics,
        "prev_metrics": prev_metrics,
        "trend_data": trend_data,
        "board_breakdown": board_breakdown,
        "history_avg": history_avg,
        "stats": stats,
        "suggestion": stats.get("suggestion") or {},
        "backtest_rule": stats.get("backtest_rule"),
        "improvement_suggestions": improvements,
    }


def _board_dimension_stats_local(signals: list[dict], judged_rows: list[dict]) -> dict[str, dict]:
    """build_monthly_review 内部用，避免重复大段逻辑。"""
    sig_groups: dict[str, list] = {}
    for r in signals:
        sig_groups.setdefault(_board_type_label(r), []).append(r)
    jud_groups: dict[str, list] = {}
    for r in judged_rows:
        jud_groups.setdefault(_board_type_label(r), []).append(r)
    out: dict[str, dict] = {}
    for label in sorted(set(sig_groups) | set(jud_groups)):
        jud = jud_groups.get(label, [])
        if not jud:
            continue
        wins = sum(1 for r in jud if r["is_win"])
        out[label] = {
            "trades": len(jud),
            "win_rate": round(wins / len(jud) * 100, 1),
        }
    return out

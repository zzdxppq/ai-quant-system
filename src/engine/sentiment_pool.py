"""梯队情绪池 — top30 10日涨幅龙头的竞价分布判定今日接力意愿

分桶口径（五分桶，互斥）:
    竞价一字  auction_gain ≥ +9.7% (主板) / +19.4% (创科/北交)
    高开      +5% ≤ auction_gain < 一字阈值
    平开      0  ≤ auction_gain < +5%
    低开      -跌停阈值 < auction_gain < 0
    竞价跌停  auction_gain ≤ -9.7% / -19.4%

判定（按排名加权的竞价涨幅）:
    ≥ +2%   → 积极
    0 ~ +2% → 正常
    -2% ~ 0 → 谨慎
    < -2%   → 不操作

加权：rank=1 权重最大，rank=30 权重最小（线性递减）
"""
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import pandas as pd

from src.config import DATA_DIR, now_cn


@dataclass
class MarketAuctionStats:
    """全市场竞价风向标（9:27 竞价结束后统计）"""
    date: str
    total: int                     # 全市场有效样本数
    limit_up_flat: int             # 一字涨停（竞价即封板）
    drop_over_9pct: int            # 跌幅 > 9%（濒临跌停）
    limit_down: int                # 跌停
    verdict: str                   # 强势 / 中性 / 弱势
    limit_up_flat_list: list = None  # 一字涨停个股列表 [{code, name, auction_pct, is_main_board}]
    prev_day_limit_down: int = None  # 昨日竞价跌停数（从 sentiment_history.json 读）
    limit_down_list: list = None     # 竞价跌停个股列表 [{code, name, auction_pct}]


@dataclass
class PoolSentiment:
    date: str
    pool_size: int                 # 实际参与统计的股票数（停牌/无数据的会跳过）
    avg_auction_gain: float        # 算术平均竞价涨幅(%)
    weighted_auction_gain: float   # 按 10 日涨幅排名加权的竞价涨幅(%)
    limit_up_flat: int             # 竞价一字数
    high_open: int                 # 高开数 (≥+5%, <一字)
    flat_open: int                 # 平开数 (0~+5%)
    low_open: int                  # 低开数 (<0, >跌停)
    limit_down: int                # 竞价跌停数
    verdict: str                   # 积极 / 正常 / 谨慎 / 不操作
    reason: str                    # 一句话解释


def _is_gem_or_bse(code: str) -> bool:
    """创业板/科创板/北交所 → 20cm"""
    code = str(code)
    return code.startswith(("300", "301", "688", "8", "4"))


def _classify_auction(code: str, auction_gain: float) -> str:
    limit_thr = 19.4 if _is_gem_or_bse(code) else 9.7
    if auction_gain >= limit_thr:
        return "limit_up_flat"
    if auction_gain >= 5:
        return "high_open"
    if auction_gain >= 0:
        return "flat_open"
    if auction_gain > -limit_thr:
        return "low_open"
    return "limit_down"


def _fetch_pool_spot(codes: list[str], spot_df: pd.DataFrame | None) -> dict[str, dict]:
    """为池内代码获取 open/pre_close，优先腾讯批量接口、spot_df 兜底"""
    result: dict[str, dict] = {}

    try:
        from src.data.tencent_api import fetch_stock_details
        tx = fetch_stock_details(codes)
        if tx is not None and not tx.empty:
            for _, row in tx.iterrows():
                c = str(row["code"])
                o = float(row.get("open", 0))
                pc = float(row.get("pre_close", 0))
                if o > 0 and pc > 0:
                    result[c] = {"open": o, "pre_close": pc}
            print(f"[梯队情绪] 腾讯获取 {len(result)}/{len(codes)} 只竞价数据")
    except Exception as e:
        print(f"[梯队情绪] 腾讯接口失败: {e}，回退 spot_df")

    if spot_df is not None and not spot_df.empty:
        spot = spot_df.copy()
        spot["code"] = spot["code"].astype(str)
        for _, row in spot.iterrows():
            c = str(row["code"])
            if c in result:
                continue
            o = float(row.get("open", 0))
            pc = float(row.get("pre_close", 0))
            if o > 0 and pc > 0:
                result[c] = {"open": o, "pre_close": pc}

    return result


def compute_pool_sentiment(
    pool_codes: list[str],
    spot_df: pd.DataFrame | None = None,
) -> Optional[PoolSentiment]:
    """计算梯队情绪

    Args:
        pool_codes: 按 10 日涨幅排序的池代码列表（通常 top30）
        spot_df: 竞价快照 DataFrame（兜底），优先使用腾讯 API 拉 30 只

    Returns:
        PoolSentiment 或 None（池空/无有效样本）
    """
    if not pool_codes:
        return None

    spot_map = _fetch_pool_spot(pool_codes, spot_df)

    gains: list[float] = []
    weights: list[float] = []
    buckets = {
        "limit_up_flat": 0, "high_open": 0, "flat_open": 0,
        "low_open": 0, "limit_down": 0,
    }
    n = len(pool_codes)

    for idx, code in enumerate(pool_codes):
        row = spot_map.get(str(code))
        if row is None:
            continue
        open_px = row["open"]
        pre_close = row["pre_close"]

        auction_gain = (open_px / pre_close - 1) * 100
        buckets[_classify_auction(str(code), auction_gain)] += 1
        gains.append(auction_gain)
        # 线性降权：rank1=1.0, rank30≈0.033
        weights.append((n - idx) / n)

    if not gains:
        return None

    avg = sum(gains) / len(gains)
    wsum = sum(weights)
    wavg = sum(g * w for g, w in zip(gains, weights)) / wsum if wsum > 0 else avg

    if wavg >= 2:
        verdict = "积极"
    elif wavg >= 0:
        verdict = "正常"
    elif wavg >= -2:
        verdict = "谨慎"
    else:
        verdict = "不操作"

    reason = (
        f"池{len(gains)}只 · 加权竞价{wavg:+.2f}% · "
        f"一字{buckets['limit_up_flat']}/高开{buckets['high_open']}/"
        f"平开{buckets['flat_open']}/低开{buckets['low_open']}/跌停{buckets['limit_down']}"
    )

    return PoolSentiment(
        date=now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        pool_size=len(gains),
        avg_auction_gain=round(avg, 2),
        weighted_auction_gain=round(wavg, 2),
        limit_up_flat=buckets["limit_up_flat"],
        high_open=buckets["high_open"],
        flat_open=buckets["flat_open"],
        low_open=buckets["low_open"],
        limit_down=buckets["limit_down"],
        verdict=verdict,
        reason=reason,
    )


def compute_market_auction_stats(
    spot_df: pd.DataFrame | None = None,
    full_market_threshold: int = 1000,
) -> Optional[MarketAuctionStats]:
    """全市场竞价风向标：一字涨停数 / 跌幅>9% / 跌停数

    要求样本必须接近全市场（A股 5000+ 只）才能准确反映风向。
    若传入 spot_df 样本 < full_market_threshold（说明是东财 top500 等局部样本），
    主动用新浪 stock_zh_a_spot 拉全市场重统计，避免"跌停 0 只"这种假象。
    """
    sample_too_small = (
        spot_df is None
        or spot_df.empty
        or len(spot_df) < full_market_threshold
    )
    if sample_too_small:
        # 走新浪拉全市场（~5200 只）
        try:
            from src.data.sina_spot_api import fetch_a_share_list_sina
            full_df = fetch_a_share_list_sina()
            if full_df is not None and not full_df.empty and len(full_df) >= full_market_threshold:
                if spot_df is not None and not spot_df.empty:
                    print(f"[市场风向] 传入样本仅 {len(spot_df)} 只（不足全市场），改用新浪 {len(full_df)} 只重统计")
                spot_df = full_df
            elif spot_df is None or spot_df.empty:
                # 新浪也没拿到 → 兜底用 fetcher
                from src.data.fetcher import fetch_realtime_spot
                spot_df = fetch_realtime_spot()
        except Exception as e:
            print(f"[市场风向] 全市场拉取失败: {e}")
            if spot_df is None or spot_df.empty:
                return None

    if spot_df is None or spot_df.empty:
        return None

    df = spot_df.copy()
    df["code"] = df["code"].astype(str)

    # 排除 ST/*ST/S*ST/退市整理期（A 股传统不参与跌停统计）
    before_filter = len(df)
    name_u = df["name"].astype(str).str.upper()
    name_raw = df["name"].astype(str)
    df = df[
        ~name_u.str.contains("ST", regex=False)   # 覆盖 ST / *ST / S*ST
        & ~name_raw.str.contains("退", regex=False)  # 退市整理期
    ].copy()
    filtered = before_filter - len(df)
    if filtered > 0:
        print(f"[市场风向] 排除 ST/退市 {filtered} 只 → 有效样本 {len(df)} 只")

    for col in ("open", "pre_close"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df = df[(df["open"] > 0) & (df["pre_close"] > 0)].copy()

    df["auction_pct"] = (df["open"] / df["pre_close"] - 1) * 100
    is_20cm = df["code"].str.startswith(("300", "301", "688", "8", "4"))

    lu_thr = pd.Series(9.7, index=df.index)
    lu_thr[is_20cm] = 19.4
    ld_thr = pd.Series(-9.7, index=df.index)
    ld_thr[is_20cm] = -19.4

    limit_up_flat = int((df["auction_pct"] >= lu_thr).sum())
    limit_down = int((df["auction_pct"] <= ld_thr).sum())
    drop_over_9 = int((df["auction_pct"] <= -9).sum())

    if limit_up_flat >= 10 and limit_down <= 3:
        verdict = "强势"
    elif limit_down >= 10 or drop_over_9 >= 20:
        verdict = "弱势"
    else:
        verdict = "中性"

    # 抽取竞价跌停个股列表（按竞价跌幅升序，含 code+name+pct，用于明日"昨日跌停反馈"统计）
    ld_df = df[df["auction_pct"] <= ld_thr].copy()
    ld_df = ld_df.sort_values("auction_pct")
    ld_list_full = [
        {"code": str(r["code"]), "name": str(r.get("name", "")), "auction_pct": round(float(r["auction_pct"]), 2)}
        for _, r in ld_df.iterrows()
    ]

    # 抽取一字涨停个股列表（按竞价涨幅降序）+ 富化 close/市值/板块/封单
    flat_df = df[df["auction_pct"] >= lu_thr].copy()
    flat_df["is_main_board"] = ~flat_df["code"].str.startswith(("300", "301", "688", "8", "4"))
    flat_df = flat_df.sort_values("auction_pct", ascending=False)
    flat_list = []
    for _, row in flat_df.iterrows():
        flat_list.append({
            "code": str(row["code"]),
            "name": str(row.get("name", "")),
            "auction_pct": round(float(row["auction_pct"]), 2),
            "is_main_board": bool(row["is_main_board"]),
            "close": round(float(row.get("close", 0) or 0), 2),
            # 下面 4 项后续补齐
            "market_cap_yi": None,
            "industry": "-",
            "unmatched_amount_wan": None,
        })

    # 富化：腾讯补市值，新浪 batch 拿 5 档买一封单，ranking 拿板块
    if flat_list:
        codes = [s["code"] for s in flat_list]

        # 1) 腾讯批量拿 market_cap_yi
        try:
            from src.data.tencent_api import fetch_stock_details
            tx = fetch_stock_details(codes)
            if tx is not None and not tx.empty:
                tx_map = {str(r["code"]): r for _, r in tx.iterrows()}
                for s in flat_list:
                    r = tx_map.get(s["code"])
                    if r is not None:
                        s["market_cap_yi"] = round(float(r.get("market_cap_yi", 0) or 0), 2)
        except Exception as e:
            print(f"[一字涨停富化] 腾讯市值失败: {e}")

        # 2) 新浪 batch 拿 bid1_volume，算未匹配额（封单金额，单位：万元）
        try:
            from src.data.sina_api import fetch_realtime_batch as sina_batch
            sina_df = sina_batch(codes)
            if sina_df is not None and not sina_df.empty:
                sina_map = {str(r["code"]): r for _, r in sina_df.iterrows()}
                for s in flat_list:
                    r = sina_map.get(s["code"])
                    if r is not None:
                        bid1_v = float(r.get("bid1_volume", 0) or 0)  # 股
                        bid1_p = float(r.get("bid1_price", 0) or 0)
                        if bid1_v > 0 and bid1_p > 0:
                            # 未匹配额（封单金额）= 价 × 量，单位转万元
                            s["unmatched_amount_wan"] = round(bid1_v * bid1_p / 10000, 1)
        except Exception as e:
            print(f"[一字涨停富化] 新浪封单失败: {e}")

        # 3) 板块查表 — 优先 industry_cache.json（全市场 5000+ 覆盖），fallback latest_ranking
        try:
            ind_map: dict = {}
            ic_file = DATA_DIR / "industry_cache.json"
            if ic_file.exists():
                ind_map = json.loads(ic_file.read_text())
            # ranking 兜底（万一缓存里没命中 — 一般 industry_cache 已覆盖全市场）
            ranking_file = DATA_DIR / "latest_ranking.json"
            if ranking_file.exists():
                rd = json.loads(ranking_file.read_text())
                for r in (rd.get("ranking") or []):
                    code = str(r.get("code", ""))
                    if code and code not in ind_map and r.get("industry"):
                        ind_map[code] = r["industry"]
            for s in flat_list:
                ind = ind_map.get(s["code"])
                if ind:
                    s["industry"] = ind
        except Exception as e:
            print(f"[一字涨停富化] 板块查表失败: {e}")

    stats = MarketAuctionStats(
        date=now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        total=len(df),
        limit_up_flat=limit_up_flat,
        drop_over_9pct=drop_over_9,
        limit_down=limit_down,
        verdict=verdict,
        limit_up_flat_list=flat_list,
        limit_down_list=ld_list_full,
    )
    print(f"[市场风向] {verdict} · 样本{len(df)} · "
          f"一字{limit_up_flat} 跌>9%={drop_over_9} 跌停{limit_down}")
    return stats


def load_pool_from_ranking() -> list[str]:
    """从 latest_ranking.json 读取 top30 池代码（按 10 日涨幅排序）"""
    path = DATA_DIR / "latest_ranking.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return [str(r["code"]) for r in data.get("ranking", [])]
    except Exception:
        return []


def save_sentiment(
    sentiment: PoolSentiment,
    market_stats: MarketAuctionStats | None = None,
) -> None:
    # 写盘前先把昨日竞价跌停数注入 market_stats
    if market_stats is not None:
        market_stats.prev_day_limit_down = _get_prev_day_limit_down(market_stats.date)
    data = asdict(sentiment)
    if market_stats:
        data["market"] = asdict(market_stats)
    path = DATA_DIR / "latest_sentiment.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    # 追加到 sentiment_history.json（用于明日"昨日竞价跌停"展示）
    if market_stats:
        _append_sentiment_history(market_stats)


def _history_file() -> "Path":
    from pathlib import Path
    return DATA_DIR / "sentiment_history.json"


def _load_sentiment_history() -> list:
    p = _history_file()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _append_sentiment_history(stats: MarketAuctionStats) -> None:
    history = _load_sentiment_history()
    today_d = stats.date[:10]
    history = [h for h in history if h.get("date", "")[:10] != today_d]
    history.append({
        "date": stats.date,
        "limit_down": stats.limit_down,
        "drop_over_9pct": stats.drop_over_9pct,
        "limit_up_flat": stats.limit_up_flat,
        "verdict": stats.verdict,
        # 保存代码列表，明日可用于"昨日跌停今日竞价"统计
        "limit_down_codes": [s["code"] for s in (stats.limit_down_list or [])],
    })
    history = history[-60:]
    _history_file().write_text(json.dumps(history, ensure_ascii=False, indent=2))


def _get_prev_day_limit_down(today_date: str) -> Optional[int]:
    """从 sentiment_history.json 读取上一交易日的 limit_down"""
    history = _load_sentiment_history()
    if not history:
        return None
    today_d = today_date[:10]
    past = [h for h in history if h.get("date", "")[:10] != today_d]
    if not past:
        return None
    return past[-1].get("limit_down")


def get_prev_limit_down_codes() -> list:
    """读取上一交易日的竞价跌停股代码列表（用于今日竞价反馈统计）"""
    from src.config import now_cn
    history = _load_sentiment_history()
    if not history:
        return []
    today_d = now_cn().strftime("%Y-%m-%d")
    past = [h for h in history if h.get("date", "")[:10] != today_d]
    if not past:
        return []
    return past[-1].get("limit_down_codes") or []

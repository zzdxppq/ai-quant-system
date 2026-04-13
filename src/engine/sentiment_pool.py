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

from src.config import DATA_DIR


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
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
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


def save_sentiment(sentiment: PoolSentiment) -> None:
    path = DATA_DIR / "latest_sentiment.json"
    path.write_text(json.dumps(asdict(sentiment), ensure_ascii=False, indent=2))

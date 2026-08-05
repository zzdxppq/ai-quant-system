"""动量延续评分系统 — 连板股专用

评估连板股次日能否继续涨停的概率，替代 Kronos 通用时序模型。

评分维度（满分100）：
1. 连板高度 Profile     (25分) — N板历史概率+市值+次新
2. 量能模式 Volume      (25分) — 缩量强/放量弱
3. 封板强度 Seal        (20分) — 一字>秒板>早封>晚封
4. 市场环境 Breadth     (15分) — 涨停数/板块联动/梯队情绪
5. 空间位置 Space       (15分) — 累计涨幅越小空间越大

评分结果：
  80-100: 强延续（大概率继续涨停）
  60-79:  中等延续（高开概率大）
  40-59:  中性（五五开）
  0-39:   衰竭（冲高回落风险大）
"""
from dataclasses import dataclass, field
from typing import Optional

from src.config import DATA_DIR, now_cn


# 连板→下一板的历史基础概率（A股经验统计）
BASE_PROB_TABLE = {
    2: 0.38,
    3: 0.28,
    4: 0.22,
    5: 0.18,
    6: 0.15,
    7: 0.13,
    8: 0.11,
    9: 0.10,
    10: 0.09,
}


@dataclass
class MomentumInput:
    """评分输入"""
    code: str
    name: str
    consecutive_limit_ups: int      # 当前连板数

    # 量能（最近几天涨停日的成交量/换手率）
    volumes: list[float] = field(default_factory=list)
    turnover_rates: list[float] = field(default_factory=list)

    # 封板质量（最近一个涨停日）
    first_seal_minutes: float = 30.0    # 首次封板距开盘分钟数（0=一字）
    seal_breaks: int = 0                # 炸板次数
    seal_order_ratio: float = 1.0       # 封单/成交量比

    # 市场环境
    total_limit_up_count: int = 30      # 全市场涨停数
    sector_limit_up_count: int = 0      # 同板块涨停数
    market_sentiment: float = 0.0       # 梯队情绪加权竞价涨幅

    # 空间
    total_gain_pct: float = 0.0         # 从首板起的累计涨幅%
    market_cap_yi: float = 50.0         # 流通市值(亿)


@dataclass
class MomentumResult:
    """评分结果"""
    code: str
    name: str
    score: float                        # 0-100
    verdict: str                        # strong/moderate/neutral/weak
    verdict_cn: str                     # 强延续/中等/中性/衰竭
    probability: float                  # 调整后延续概率
    components: dict                    # 各维度得分明细
    scored_at: str = ""


def score_momentum(inp: MomentumInput) -> MomentumResult:
    """计算动量延续评分"""
    components = {}

    # ═══ 1. 连板高度 Profile (25分) ═══
    n = min(inp.consecutive_limit_ups, 10)
    base_prob = BASE_PROB_TABLE.get(n, 0.08)

    if n <= 3:
        profile = 20
    elif n <= 5:
        profile = 22
    elif n <= 7:
        profile = 18
    else:
        profile = 14

    # 小盘股加分（跑得更远）
    if inp.market_cap_yi < 30:
        profile = min(25, profile + 3)
    elif inp.market_cap_yi < 50:
        profile = min(25, profile + 1)
    elif inp.market_cap_yi > 100:
        profile = max(0, profile - 3)

    components["连板高度"] = round(profile, 1)

    # ═══ 2. 量能模式 Volume (25分) ═══
    vol_score = 12.5

    if len(inp.volumes) >= 2:
        recent_vol = inp.volumes[-1]
        prior_avg = sum(inp.volumes[:-1]) / len(inp.volumes[:-1])

        if prior_avg > 0:
            vol_ratio = recent_vol / prior_avg
            if vol_ratio < 0.5:
                vol_score = 25      # 极度缩量（一字板）
            elif vol_ratio < 0.8:
                vol_score = 21      # 健康缩量
            elif vol_ratio < 1.0:
                vol_score = 17      # 轻微缩量
            elif vol_ratio < 1.5:
                vol_score = 12      # 轻微放量
            elif vol_ratio < 2.0:
                vol_score = 7       # 明显放量
            else:
                vol_score = 3       # 巨量（分歧大）

    # 换手率修正
    if inp.turnover_rates:
        latest_tr = inp.turnover_rates[-1]
        if latest_tr < 1.0:
            vol_score = min(25, vol_score + 4)  # 一字板
        elif latest_tr > 15.0:
            vol_score = max(0, vol_score - 5)   # 高换手=派发

    components["量能模式"] = round(vol_score, 1)

    # ═══ 3. 封板强度 Seal (20分) ═══
    seal_score = 10

    if inp.first_seal_minutes == 0:
        seal_score = 20     # 一字封板
    elif inp.first_seal_minutes < 5:
        seal_score = 18     # T字板/秒板
    elif inp.first_seal_minutes < 30:
        seal_score = 15     # 早盘封板
    elif inp.first_seal_minutes < 60:
        seal_score = 11     # 上午封板
    elif inp.first_seal_minutes < 120:
        seal_score = 8      # 下午封板
    else:
        seal_score = 5      # 尾盘封板

    # 炸板扣分
    seal_score = max(0, seal_score - inp.seal_breaks * 3)

    # 封单比加分
    if inp.seal_order_ratio > 5.0:
        seal_score = min(20, seal_score + 3)
    elif inp.seal_order_ratio > 2.0:
        seal_score = min(20, seal_score + 1)
    elif inp.seal_order_ratio < 0.5:
        seal_score = max(0, seal_score - 2)

    components["封板强度"] = round(seal_score, 1)

    # ═══ 4. 市场环境 Breadth (15分) ═══
    breadth = 7.5

    if inp.total_limit_up_count > 80:
        breadth = 14
    elif inp.total_limit_up_count > 50:
        breadth = 12
    elif inp.total_limit_up_count > 30:
        breadth = 9
    elif inp.total_limit_up_count > 15:
        breadth = 6
    else:
        breadth = 3     # 冰点市场

    # 板块联动加分
    if inp.sector_limit_up_count >= 3:
        breadth = min(15, breadth + 2)

    # 梯队情绪
    if inp.market_sentiment > 2.0:
        breadth = min(15, breadth + 2)
    elif inp.market_sentiment < -1.0:
        breadth = max(0, breadth - 3)

    components["市场环境"] = round(breadth, 1)

    # ═══ 5. 空间位置 Space (15分) ═══
    gain = inp.total_gain_pct
    if gain < 30:
        space = 14      # 起步阶段
    elif gain < 60:
        space = 12      # 正常
    elif gain < 100:
        space = 9       # 偏高
    elif gain < 150:
        space = 5       # 高位
    else:
        space = 2       # 极高位

    components["空间位置"] = round(space, 1)

    # ═══ 汇总 ═══
    total = sum(components.values())

    if total >= 80:
        verdict, verdict_cn = "strong", "强延续"
    elif total >= 60:
        verdict, verdict_cn = "moderate", "中等"
    elif total >= 40:
        verdict, verdict_cn = "neutral", "中性"
    else:
        verdict, verdict_cn = "weak", "衰竭"

    # 调整概率
    adjusted_prob = base_prob * (1 + (total - 50) * 0.03)
    adjusted_prob = max(0.02, min(0.85, adjusted_prob))

    return MomentumResult(
        code=inp.code,
        name=inp.name,
        score=round(total, 1),
        verdict=verdict,
        verdict_cn=verdict_cn,
        probability=round(adjusted_prob, 3),
        components=components,
        scored_at=now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    )


def score_from_kline(code: str, name: str = "", consecutive: int = 2) -> Optional[MomentumResult]:
    """从K线数据自动构建输入并评分

    简化版：只用可获取的数据（不需要封板时间等盘口数据）
    """
    try:
        from src.data.sina_kline_api import fetch_kline, SCALE_DAILY

        df = fetch_kline(code, SCALE_DAILY, datalen=20)
        if df is None or df.empty or len(df) < 5:
            return None

        # 提取最近N天涨停日的量能
        volumes = df["volume"].astype(float).tolist()[-consecutive:]
        # 换手率用成交量变化估算
        all_volumes = df["volume"].astype(float).tolist()
        turnover_rates = []
        for v in volumes:
            avg_20 = sum(all_volumes[-20:]) / min(20, len(all_volumes))
            tr_est = (v / avg_20 * 5) if avg_20 > 0 else 5.0  # 粗估换手率
            turnover_rates.append(round(tr_est, 2))

        # 累计涨幅
        start_idx = max(0, len(df) - consecutive - 1)
        start_price = float(df.iloc[start_idx]["close"])
        end_price = float(df.iloc[-1]["close"])
        total_gain = (end_price / start_price - 1) * 100 if start_price > 0 else 0

        # 市值（从腾讯接口获取）
        market_cap_yi = 50.0  # 默认值
        try:
            from src.data.tencent_api import fetch_stock_details
            detail_df = fetch_stock_details([code])
            if detail_df is not None and not detail_df.empty:
                market_cap_yi = float(detail_df.iloc[0].get("market_cap_yi", 50))
        except Exception:
            pass

        # 市场环境（从缓存读取）
        total_lu = 30
        sentiment = 0.0
        try:
            from src.data.json_io import load_json_file

            sent_file = DATA_DIR / "latest_sentiment.json"
            sent_data = load_json_file(sent_file)
            if isinstance(sent_data, dict):
                sentiment = float(sent_data.get("weighted_auction_gain", 0))
                total_lu = int(sent_data.get("pool_size", 30))
        except Exception:
            pass

        inp = MomentumInput(
            code=code,
            name=name,
            consecutive_limit_ups=consecutive,
            volumes=volumes,
            turnover_rates=turnover_rates,
            first_seal_minutes=30.0,    # 无盘口数据时用默认值
            seal_breaks=0,
            seal_order_ratio=1.0,
            total_limit_up_count=total_lu,
            sector_limit_up_count=0,
            market_sentiment=sentiment,
            total_gain_pct=total_gain,
            market_cap_yi=market_cap_yi,
        )

        return score_momentum(inp)

    except Exception as e:
        print(f"[动量评分] {code} 失败: {e}")
        return None

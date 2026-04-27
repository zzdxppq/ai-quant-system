"""竞价决策卡 — 9:25~9:30 量化打分

4维打分体系（满分100）：
  D1. 目标股自身竞价 (40分) — 竞价涨幅+量比+换手+未匹配状态
  D2. 高标/情绪锚反馈 (25分) — 最高连板股竞价+昨日涨停股溢价
  D3. 涨幅榜竞价方向 (20分) — 主线延续性+亏钱效应
  D4. 板块效应 (15分) — 同板块助攻+梯队完整性

输出：
  总分 + 仓位建议（满仓/半仓/不开仓） + 止损位 + 否决项
"""
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

from src.config import DATA_DIR, now_cn


@dataclass
class AuctionScore:
    """竞价决策卡结果"""
    code: str
    name: str
    date: str

    # 4维得分
    d1_self: float = 0          # 目标股自身竞价 (40)
    d2_leader: float = 0        # 高标/情绪锚 (25)
    d3_ranking: float = 0       # 涨幅榜方向 (20)
    d4_sector: float = 0        # 板块效应 (15)

    total_score: float = 0
    # 明细
    d1_detail: str = ""
    d2_detail: str = ""
    d3_detail: str = ""
    d4_detail: str = ""

    # 决策
    action: str = ""            # "满仓开仓" / "半仓试错" / "放弃"
    position: str = ""          # 仓位建议
    stop_loss: float = 0        # 止损价
    stop_loss_pct: float = 0    # 止损幅度%
    reason: str = ""            # 决策理由

    # 否决项
    vetoes: list = field(default_factory=list)
    has_veto: bool = False


def score_auction(
    hit: dict,
    leader_data: dict = None,
    sentiment_data: dict = None,
    ranking_data: list = None,
    limit_up_data: list = None,
) -> AuctionScore:
    """对选股命中的标的做竞价决策打分

    Args:
        hit: ScreenerHit 的 asdict()
        leader_data: latest_leader.json 数据
        sentiment_data: latest_sentiment.json 数据
        ranking_data: 排行列表
        limit_up_data: 涨停梯队列表
    """
    code = hit.get("code", "")
    name = hit.get("name", "")
    today = now_cn().strftime("%Y-%m-%d")

    result = AuctionScore(code=code, name=name, date=today)

    # ═══ D1. 目标股自身竞价 (40分) ═══
    d1, d1_detail, d1_vetoes = _score_d1_self(hit)
    result.d1_self = d1
    result.d1_detail = d1_detail
    result.vetoes.extend(d1_vetoes)

    # ═══ D2. 高标/情绪锚反馈 (25分) ═══
    d2, d2_detail, d2_vetoes = _score_d2_leader(leader_data)
    result.d2_leader = d2
    result.d2_detail = d2_detail
    result.vetoes.extend(d2_vetoes)

    # ═══ D3. 涨幅榜竞价方向 (20分) ═══
    d3, d3_detail = _score_d3_ranking(ranking_data, sentiment_data)
    result.d3_ranking = d3
    result.d3_detail = d3_detail

    # ═══ D4. 板块效应 (15分) ═══
    d4, d4_detail = _score_d4_sector(hit, limit_up_data, ranking_data)
    result.d4_sector = d4
    result.d4_detail = d4_detail

    # ═══ 汇总 ═══
    result.total_score = round(d1 + d2 + d3 + d4, 1)
    result.has_veto = len(result.vetoes) > 0

    # ═══ 决策 ═══
    _make_decision(result, hit)

    return result


def _score_d1_self(hit: dict) -> tuple[float, str, list]:
    """D1: 目标股自身竞价 (40分)"""
    score = 20  # 基准（选股器已通过基本条件）
    details = []
    vetoes = []

    auction_gain = hit.get("auction_gain", 0)
    volume_ratio = hit.get("auction_volume_ratio", hit.get("volume_ratio", 0))
    board_count = hit.get("continuous_limit_up", 0)

    # 竞价涨幅分布
    if 5.0 <= auction_gain <= 7.0:
        score += 10
        details.append(f"竞价{auction_gain}%，抢筹型高开，理想区间")
    elif 4.0 <= auction_gain < 5.0:
        score += 6
        details.append(f"竞价{auction_gain}%，温和高开")
    elif auction_gain > 7.0:
        score += 4
        details.append(f"竞价{auction_gain}%，偏高（接近涨停价）")
    else:
        score += 2
        details.append(f"竞价{auction_gain}%，偏低")

    # 量比
    if volume_ratio >= 5:
        score += 6
        details.append(f"量比{volume_ratio}，资金积极抢筹")
    elif volume_ratio >= 2:
        score += 4
        details.append(f"量比{volume_ratio}，有增量资金")
    elif volume_ratio >= 1:
        score += 2
        details.append(f"量比{volume_ratio}，正常")
    else:
        details.append(f"量比{volume_ratio}，偏弱")

    # 连板加分
    if board_count >= 4:
        score += 4
        details.append(f"{board_count}连板，市场辨识度高")
    elif board_count >= 3:
        score += 2
        details.append(f"{board_count}连板")

    score = min(40, max(0, score))
    return score, "；".join(details), vetoes


def _score_d2_leader(leader_data: dict) -> tuple[float, str, list]:
    """D2: 高标/情绪锚反馈 (25分)"""
    score = 12.5  # 中性基准
    details = []
    vetoes = []

    if not leader_data:
        return score, "无高标数据", vetoes

    # 市场高标
    ml = leader_data.get("market_leader", {})
    if ml:
        chg = ml.get("auction_change_pct", 0)
        name = ml.get("leader_name", "")
        if chg >= 3:
            score += 6
            details.append(f"市场高标{name}大幅高开{chg:+.1f}%")
        elif chg >= 0:
            score += 3
            details.append(f"市场高标{name}红开{chg:+.1f}%")
        elif chg > -5:
            score -= 3
            details.append(f"市场高标{name}低开{chg:+.1f}%")
        else:
            score -= 8
            details.append(f"市场高标{name}深水开{chg:+.1f}%")
            if chg <= -9:
                vetoes.append(f"高标{name}跌停/深水，情绪崩溃")

    # 昨日涨停股平均竞价
    y_avg = leader_data.get("yesterday_main_board_avg_auction", {})
    if y_avg and y_avg.get("sample_count", 0) > 0:
        avg_chg = y_avg.get("avg_change_pct", 0)
        n = y_avg.get("sample_count", 0)
        if avg_chg >= 1.5:
            score += 6
            details.append(f"昨日涨停股({n}只)平均竞价{avg_chg:+.1f}%，接力情绪强")
        elif avg_chg >= 0:
            score += 3
            details.append(f"昨日涨停股平均竞价{avg_chg:+.1f}%，情绪偏暖")
        elif avg_chg > -2:
            score -= 2
            details.append(f"昨日涨停股平均竞价{avg_chg:+.1f}%，分歧")
        else:
            score -= 6
            details.append(f"昨日涨停股平均竞价{avg_chg:+.1f}%，大面积无溢价")
            if avg_chg <= -3:
                vetoes.append("昨日涨停股大面积闷杀，接力情绪崩溃")

    score = min(25, max(0, score))
    return score, "；".join(details) if details else "无数据", vetoes


def _score_d3_ranking(ranking_data: list, sentiment_data: dict) -> tuple[float, str]:
    """D3: 涨幅榜竞价方向 (20分)"""
    score = 10  # 中性
    details = []

    if not ranking_data:
        return score, "无排行数据"

    # 分析排行榜竞价方向一致性
    # 检查涨幅榜前10的板块集中度
    top10 = ranking_data[:10]
    industries = [r.get("industry", "") for r in top10 if r.get("industry")]
    if industries:
        counter = Counter(industries)
        top_industry, top_count = counter.most_common(1)[0]
        if top_count >= 4:
            score += 6
            details.append(f"涨幅榜集中在{top_industry}（{top_count}/10），主线明确")
        elif top_count >= 3:
            score += 3
            details.append(f"涨幅榜有{top_industry}方向（{top_count}/10）")
        else:
            score -= 2
            details.append("涨幅榜板块杂乱，无核心方向")

    # 梯队情绪
    if sentiment_data:
        verdict = sentiment_data.get("verdict", "")
        weighted = sentiment_data.get("weighted_auction_gain", 0)
        if verdict in ("活跃", "积极"):
            score += 4
            details.append(f"梯队情绪{verdict}(加权竞价{weighted:+.1f}%)")
        elif verdict == "正常":
            score += 1
            details.append(f"梯队情绪正常")
        else:
            score -= 3
            details.append(f"梯队情绪{verdict}")

    score = min(20, max(0, score))
    return score, "；".join(details) if details else "无数据"


def _score_d4_sector(hit: dict, limit_up_data: list, ranking_data: list) -> tuple[float, str]:
    """D4: 板块效应 (15分)"""
    score = 7.5  # 中性
    details = []

    # 找目标股所在板块
    code = hit.get("code", "")
    target_industry = ""
    if ranking_data:
        for r in ranking_data:
            if str(r.get("code", "")) == code:
                target_industry = r.get("industry", "")
                break

    if not target_industry:
        return score, "板块信息缺失"

    # 同板块涨停数
    sector_lu_count = 0
    if limit_up_data:
        for s in limit_up_data:
            if s.get("industry", "") == target_industry:
                sector_lu_count += 1

    # 同板块在涨幅榜的数量
    sector_in_top = 0
    if ranking_data:
        for r in ranking_data[:20]:
            if r.get("industry", "") == target_industry:
                sector_in_top += 1

    if sector_lu_count >= 3:
        score += 5
        details.append(f"{target_industry}板块{sector_lu_count}只涨停，梯队完整")
    elif sector_lu_count >= 2:
        score += 3
        details.append(f"{target_industry}板块{sector_lu_count}只涨停，有助攻")
    elif sector_lu_count == 1:
        score -= 2
        details.append(f"{target_industry}板块仅1只涨停，孤军奋战")
    else:
        score -= 4
        details.append(f"{target_industry}板块无其他涨停，独木难支")

    if sector_in_top >= 3:
        score += 2
        details.append(f"涨幅榜有{sector_in_top}只同板块")

    score = min(15, max(0, score))
    return score, "；".join(details) if details else "无数据"


def _make_decision(result: AuctionScore, hit: dict):
    """根据总分+否决项做决策"""
    open_price = hit.get("open_price", 0)
    pre_close = hit.get("pre_close", 0) or (open_price / (1 + hit.get("auction_gain", 5) / 100) if open_price > 0 else 0)

    # 否决项 → 直接放弃
    if result.has_veto:
        result.action = "放弃"
        result.position = "不开仓"
        result.reason = f"存在否决项：{'、'.join(result.vetoes)}"
        result.stop_loss = 0
        result.stop_loss_pct = 0
        return

    score = result.total_score

    if score >= 75:
        result.action = "果断开仓"
        result.position = "标准仓位（3成）"
        result.reason = f"总分{score}，自身竞价+情绪+板块三重共振"
        # 止损：开盘价下方5%
        if open_price > 0:
            result.stop_loss = round(open_price * 0.95, 2)
            result.stop_loss_pct = -5.0

    elif score >= 55:
        result.action = "小仓试错"
        result.position = "半仓（1.5成）"
        result.reason = f"总分{score}，有亮点但有瑕疵，控制仓位"
        # 止损：开盘价下方3%（更紧）
        if open_price > 0:
            result.stop_loss = round(open_price * 0.97, 2)
            result.stop_loss_pct = -3.0

    else:
        result.action = "放弃"
        result.position = "不开仓"
        result.reason = f"总分{score}，信号偏弱，空仓观望"
        result.stop_loss = 0
        result.stop_loss_pct = 0


def score_all_hits(hits: list[dict]) -> list[dict]:
    """对所有选股命中的标的做竞价决策打分"""
    # 加载辅助数据
    leader_data = _load_json("latest_leader.json")
    sentiment_data = _load_json("latest_sentiment.json")
    ranking = _load_json("latest_ranking.json")
    ranking_list = ranking.get("ranking", []) if ranking else []
    limit_up_data = _get_limit_up_with_industry(ranking_list)

    results = []
    for hit in hits:
        score = score_auction(hit, leader_data, sentiment_data, ranking_list, limit_up_data)
        results.append(asdict(score))

    # 保存
    (DATA_DIR / "latest_auction_scores.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )

    return results


def _load_json(filename: str) -> Optional[dict]:
    f = DATA_DIR / filename
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return None


def _get_limit_up_with_industry(ranking_list: list) -> list[dict]:
    """从涨停缓存+排行数据构建带板块信息的涨停列表"""
    industry_map = {str(r["code"]): r.get("industry", "") for r in ranking_list}

    cache_file = DATA_DIR / "limit_up_cache.json"
    if not cache_file.exists():
        return []

    try:
        cache = json.loads(cache_file.read_text())
        latest = sorted(cache.keys())[-1] if cache else ""
        if not latest:
            return []
        result = []
        for r in cache[latest]:
            code = r.get("code", "")
            result.append({
                "code": code,
                "name": r.get("name", ""),
                "industry": industry_map.get(code, ""),
            })
        return result
    except Exception:
        return []

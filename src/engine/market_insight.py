"""市场洞察引擎 — 从 Top30 涨幅榜中提取四维短线信号

四维分析：
1. 板块集中度（Sector Concentration）— 哪些板块在主导，集中度多高
2. 资金行为画像（Capital Flow Profile）— 新进/加速/持续/退潮 四象限
3. 情绪领袖识别（Emotion Leaders）— 谁在引领情绪，龙头链路
4. 周期波形信号（Cycle Wave Signal）— 从 Top30 整体涨幅分布读周期位置

输入：当日 ranking (list[dict]) + 前一日 ranking (list[dict], 可选)
输出：MarketInsight 结构化分析
"""
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import Counter, defaultdict

from src.config import DATA_DIR, now_cn


# ── 数据结构 ──────────────────────────────────────────────────────────

@dataclass
class SectorHeat:
    """板块热度"""
    industry: str
    count: int                      # 入榜股数
    codes: list[str]                # 入榜代码
    names: list[str]                # 入榜名称
    avg_gain_10d: float             # 板块平均10日涨幅
    max_gain_10d: float             # 板块最高10日涨幅
    top_stock: str                  # 板块内领涨股名
    weight: float                   # 加权占比(%)


@dataclass
class CapitalItem:
    """单只股票的资金行为标签"""
    code: str
    name: str
    gain_10d: float
    change_pct: float               # 今日涨幅
    tag: str                        # new_entry / accelerating / sustained / decelerating / exhausting
    detail: str                     # 一句话说明


@dataclass
class EmotionLeader:
    """情绪领袖"""
    code: str
    name: str
    gain_10d: float
    continuous_limit_up: int
    role: str                       # chain_starter / space_opener / follower
    reason: str


@dataclass
class CycleWaveSignal:
    """周期波形信号"""
    wave_phase: str                 # 启动浪 / 主升浪 / 加速浪 / 衰减浪 / 潮退
    intensity: float                # 强度 0~100
    breadth: int                    # 参与度（>45%的股数）
    top1_gain: float                # 榜首涨幅
    top5_avg: float                 # 前5平均涨幅
    top30_avg: float                # 全榜平均涨幅
    decay_ratio: float              # 衰减比 = 后15均/前15均，<0.5 说明头部集中
    description: str


@dataclass
class MarketInsight:
    """四维市场洞察"""
    date: str
    # 板块集中度
    sector_heats: list[SectorHeat] = field(default_factory=list)
    sector_concentration: float = 0.0        # 前3板块占比
    sector_verdict: str = ""                 # 一句话判断
    # 资金行为
    capital_profile: list[CapitalItem] = field(default_factory=list)
    capital_summary: str = ""
    new_entry_count: int = 0
    accelerating_count: int = 0
    sustained_count: int = 0
    decelerating_count: int = 0
    exhausting_count: int = 0
    # 情绪领袖
    emotion_leaders: list[EmotionLeader] = field(default_factory=list)
    emotion_chain: str = ""                  # 情绪传导链
    # 周期波形
    wave: Optional[CycleWaveSignal] = None


# ── 板块集中度 ────────────────────────────────────────────────────────

def _analyze_sectors(ranking: list[dict]) -> tuple[list[SectorHeat], float, str]:
    """分析板块集中度"""
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for item in ranking:
        ind = item.get("industry", "").strip()
        if not ind:
            ind = "未分类"
        by_industry[ind].append(item)

    total = len(ranking)
    heats = []
    for ind, stocks in by_industry.items():
        gains = [s.get("gain_10d", 0) for s in stocks]
        top = max(stocks, key=lambda s: s.get("gain_10d", 0))
        heats.append(SectorHeat(
            industry=ind,
            count=len(stocks),
            codes=[s["code"] for s in stocks],
            names=[s["name"] for s in stocks],
            avg_gain_10d=round(sum(gains) / len(gains), 2),
            max_gain_10d=round(max(gains), 2),
            top_stock=top.get("name", ""),
            weight=round(len(stocks) / total * 100, 1) if total > 0 else 0,
        ))

    heats.sort(key=lambda h: h.count, reverse=True)

    # 集中度 = 前3板块占榜比例
    top3_count = sum(h.count for h in heats[:3])
    concentration = round(top3_count / total * 100, 1) if total > 0 else 0

    # 判定
    if concentration >= 50:
        verdict = f"高度集中：前3板块({'/'.join(h.industry for h in heats[:3])})占据{concentration}%，主线清晰"
    elif concentration >= 35:
        top_ind = heats[0].industry if heats else "未知"
        verdict = f"适度集中：{top_ind}领涨({heats[0].count}只)，有辨识度但非一枝独秀"
    else:
        verdict = f"高度分散：前3板块仅{concentration}%，无明确主线，题材轮动快"

    return heats, concentration, verdict


# ── 资金行为画像 ──────────────────────────────────────────────────────

def _analyze_capital(
    ranking: list[dict],
    prev_ranking: list[dict],
) -> tuple[list[CapitalItem], str, int, int, int, int]:
    """分析资金行为

    对比今天 vs 昨天的 Top30，标记每只股票的资金状态：
    - new_entry: 今天新进榜（昨天不在）→ 新增量资金关注
    - accelerating: 昨天在榜 + 今日涨幅>3% → 主力加速拉升
    - sustained: 昨天在榜 + 今日涨幅 -3%~+3% → 资金维持不走
    - decelerating: 昨天在榜 + 今日涨幅 -3%~-7% → 获利盘松动
    - exhausting: 昨天在榜 + 今日涨幅<-7% → 资金大幅撤退
    """
    prev_codes = {str(r["code"]) for r in prev_ranking} if prev_ranking else set()
    prev_gain_map = {str(r["code"]): r.get("gain_10d", 0) for r in prev_ranking} if prev_ranking else {}

    items = []
    counts = {"new_entry": 0, "accelerating": 0, "sustained": 0,
              "decelerating": 0, "exhausting": 0}

    for r in ranking:
        code = str(r["code"])
        name = r.get("name", "")
        gain_10d = r.get("gain_10d", 0)
        change_pct = r.get("change_pct", 0)

        if code not in prev_codes:
            tag = "new_entry"
            detail = f"新进榜，10日涨幅{gain_10d:.1f}%，新增资金关注"
        elif change_pct > 3:
            tag = "accelerating"
            detail = f"今日+{change_pct:.1f}%加速，资金主动推升"
        elif change_pct >= -3:
            tag = "sustained"
            detail = f"今日{change_pct:+.1f}%横盘，资金锁仓不走"
        elif change_pct >= -7:
            tag = "decelerating"
            detail = f"今日{change_pct:+.1f}%回落，获利盘松动"
        else:
            tag = "exhausting"
            detail = f"今日{change_pct:+.1f}%大跌，资金大幅撤退"

        counts[tag] += 1
        items.append(CapitalItem(
            code=code, name=name, gain_10d=gain_10d,
            change_pct=change_pct, tag=tag, detail=detail,
        ))

    # 生成总结
    n = len(ranking)
    new_r = counts["new_entry"] / n * 100 if n > 0 else 0
    acc_r = counts["accelerating"] / n * 100 if n > 0 else 0
    dec_r = (counts["decelerating"] + counts["exhausting"]) / n * 100 if n > 0 else 0

    if new_r >= 40:
        summary = f"换血行情：{counts['new_entry']}只新面孔({new_r:.0f}%)，资金在寻找新方向"
    elif acc_r >= 40:
        summary = f"加速攻击：{counts['accelerating']}只加速({acc_r:.0f}%)，做多情绪高涨"
    elif dec_r >= 50:
        summary = f"退潮信号：{counts['decelerating']+counts['exhausting']}只回落/撤退({dec_r:.0f}%)，资金在兑现利润"
    elif counts["sustained"] >= n * 0.5:
        summary = f"缩量僵持：{counts['sustained']}只横盘，多空分歧不大，等待方向选择"
    else:
        summary = (f"新进{counts['new_entry']} · 加速{counts['accelerating']} · "
                   f"持续{counts['sustained']} · 减速{counts['decelerating']} · 退潮{counts['exhausting']}")

    return (items, summary,
            counts["new_entry"], counts["accelerating"], counts["sustained"],
            counts["decelerating"], counts["exhausting"])


# ── 情绪领袖识别 ──────────────────────────────────────────────────────

def _identify_emotion_leaders(ranking: list[dict]) -> tuple[list[EmotionLeader], str]:
    """识别情绪领袖

    规则：
    1. 空间板（space_opener）：10日涨幅最高者，打开了市场高度空间
    2. 连板龙（chain_starter）：连板数最高者，是短线接力情绪的风向标
    3. 当两者不是同一只时，观察两者的关系可以判断情绪传导链
    """
    if not ranking:
        return [], ""

    leaders = []

    # 空间板：10日涨幅最高
    space_opener = ranking[0]  # 已按gain_10d排序
    leaders.append(EmotionLeader(
        code=space_opener["code"],
        name=space_opener["name"],
        gain_10d=space_opener.get("gain_10d", 0),
        continuous_limit_up=space_opener.get("continuous_limit_up", 0),
        role="space_opener",
        reason=f"10日涨幅{space_opener.get('gain_10d', 0):.1f}%领跑全场，定义市场高度天花板",
    ))

    # 连板龙：连板数最高
    lb_stocks = [r for r in ranking if r.get("continuous_limit_up", 0) >= 2]
    lb_stocks.sort(key=lambda r: (r.get("continuous_limit_up", 0), r.get("gain_10d", 0)), reverse=True)

    if lb_stocks:
        chain_leader = lb_stocks[0]
        if chain_leader["code"] != space_opener["code"]:
            leaders.append(EmotionLeader(
                code=chain_leader["code"],
                name=chain_leader["name"],
                gain_10d=chain_leader.get("gain_10d", 0),
                continuous_limit_up=chain_leader.get("continuous_limit_up", 0),
                role="chain_starter",
                reason=f"{chain_leader.get('continuous_limit_up', 0)}连板，短线接力情绪锚",
            ))

    # 当日涨停且在榜的 = 情绪活跃跟随者
    for r in ranking[1:]:  # skip #1
        if r["code"] in {l.code for l in leaders}:
            continue
        is_gem = not r.get("is_main_board", True)
        thr = 19.5 if is_gem else 9.8
        change = r.get("change_pct", 0)
        lb = r.get("continuous_limit_up", 0)
        if change >= thr or lb >= 2:
            leaders.append(EmotionLeader(
                code=r["code"],
                name=r["name"],
                gain_10d=r.get("gain_10d", 0),
                continuous_limit_up=lb,
                role="follower",
                reason=f"{'今日涨停' if change >= thr else ''}"
                       f"{'，' if change >= thr and lb >= 2 else ''}"
                       f"{str(lb) + '连板' if lb >= 2 else ''}，跟随情绪活跃",
            ))
        if len(leaders) >= 6:
            break

    # 情绪传导链描述
    space = leaders[0] if leaders else None
    chain_starters = [l for l in leaders if l.role == "chain_starter"]
    followers = [l for l in leaders if l.role == "follower"]

    parts = []
    if space:
        parts.append(f"{space.name}(空间{space.gain_10d:.0f}%)")
    if chain_starters:
        parts.append(f"{chain_starters[0].name}({chain_starters[0].continuous_limit_up}板接力)")
    if followers:
        f_names = "/".join(f.name for f in followers[:3])
        parts.append(f"{f_names}(跟随)")

    if len(parts) >= 2:
        chain_desc = " → ".join(parts)
    elif parts:
        chain_desc = parts[0] + " 独领风骚，缺乏接力梯队"
    else:
        chain_desc = "无明确情绪领袖"

    return leaders, chain_desc


# ── 周期波形信号 ──────────────────────────────────────────────────────

def _analyze_cycle_wave(ranking: list[dict]) -> CycleWaveSignal:
    """从 Top30 涨幅分布判断周期波形

    核心指标：
    - breadth: >45% 的个股数 → 参与度
    - top1_gain: 榜首涨幅 → 高度
    - decay_ratio: 后15均/前15均 → 梯队衰减
    - 综合判断当前处于哪个浪段
    """
    if not ranking:
        return CycleWaveSignal(
            wave_phase="潮退", intensity=0, breadth=0,
            top1_gain=0, top5_avg=0, top30_avg=0,
            decay_ratio=0, description="无数据",
        )

    gains = [r.get("gain_10d", 0) for r in ranking]
    n = len(gains)

    top1 = gains[0] if gains else 0
    top5_avg = sum(gains[:5]) / min(5, n) if gains else 0
    top30_avg = sum(gains) / n if gains else 0
    breadth = sum(1 for g in gains if g >= 45)

    half = n // 2
    first_half_avg = sum(gains[:half]) / half if half > 0 else 0
    second_half_avg = sum(gains[half:]) / (n - half) if (n - half) > 0 else 0
    decay_ratio = round(second_half_avg / first_half_avg, 3) if first_half_avg > 0 else 0

    # 判断波形阶段
    if top1 < 30:
        phase = "潮退"
        intensity = max(0, top1 / 30 * 20)
        desc = f"全场最高仅{top1:.0f}%，无明显赚钱效应，观望为主"
    elif breadth == 0 and top1 < 45:
        phase = "潮退"
        intensity = 15
        desc = f"榜首{top1:.0f}%但无人>45%，赚钱效应衰退中"
    elif breadth <= 2 and top1 >= 45:
        phase = "启动浪"
        intensity = 30 + breadth * 10
        desc = f"仅{breadth}只>45%，新周期种子刚萌发，留意是否扩散"
    elif breadth >= 3 and top1 < 100:
        if decay_ratio >= 0.6:
            phase = "主升浪"
            intensity = 50 + min(breadth, 10) * 3
            desc = (f"{breadth}只>45%且梯队衰减比{decay_ratio:.2f}(健康)，"
                    f"多股共振上攻，做多窗口")
        else:
            phase = "主升浪"
            intensity = 45 + min(breadth, 10) * 2
            desc = (f"{breadth}只>45%但梯队分化(衰减比{decay_ratio:.2f})，"
                    f"资金集中头部，注意选股")
    elif top1 >= 100 and breadth >= 3:
        if decay_ratio >= 0.5:
            phase = "加速浪"
            intensity = 80 + min(10, breadth - 3)
            desc = (f"榜首{top1:.0f}%破百+{breadth}只>45%，"
                    f"全面加速行情，但需警惕过热")
        else:
            phase = "加速浪"
            intensity = 70
            desc = (f"榜首{top1:.0f}%独强但梯队跟不上(衰减比{decay_ratio:.2f})，"
                    f"独狼行情，跟随者风险大")
    elif top1 >= 100 and breadth < 3:
        phase = "衰减浪"
        intensity = 40
        desc = (f"榜首{top1:.0f}%仍高但仅{breadth}只>45%，"
                f"龙头独撑场面，新资金不愿进场")
    else:
        # 回落中但仍有参与者
        phase = "衰减浪"
        intensity = 30 + breadth * 5
        desc = f"涨幅梯队收缩中，前期强股降温，等待新方向"

    return CycleWaveSignal(
        wave_phase=phase,
        intensity=round(intensity, 1),
        breadth=breadth,
        top1_gain=round(top1, 2),
        top5_avg=round(top5_avg, 2),
        top30_avg=round(top30_avg, 2),
        decay_ratio=decay_ratio,
        description=desc,
    )


# ── 主入口 ────────────────────────────────────────────────────────────

def analyze_market_insight(
    ranking: list[dict],
    prev_ranking: list[dict] | None = None,
) -> MarketInsight:
    """四维市场洞察主入口

    Args:
        ranking: 当日 Top30 排行 (list of dict)，需按 gain_10d 降序
        prev_ranking: 前一日 Top30 排行 (可选，用于资金行为对比)

    Returns:
        MarketInsight 结构化分析
    """
    today = now_cn().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 板块集中度
    heats, concentration, sector_verdict = _analyze_sectors(ranking)

    # 2. 资金行为
    (cap_items, cap_summary,
     new_c, acc_c, sus_c, dec_c, exh_c) = _analyze_capital(ranking, prev_ranking or [])

    # 3. 情绪领袖
    leaders, chain_desc = _identify_emotion_leaders(ranking)

    # 4. 周期波形
    wave = _analyze_cycle_wave(ranking)

    return MarketInsight(
        date=today,
        sector_heats=heats,
        sector_concentration=concentration,
        sector_verdict=sector_verdict,
        capital_profile=cap_items,
        capital_summary=cap_summary,
        new_entry_count=new_c,
        accelerating_count=acc_c,
        sustained_count=sus_c,
        decelerating_count=dec_c,
        exhausting_count=exh_c,
        emotion_leaders=leaders,
        emotion_chain=chain_desc,
        wave=wave,
    )


# ── 持久化 ────────────────────────────────────────────────────────────

def save_insight(insight: MarketInsight) -> None:
    """保存分析结果到 JSON"""
    data = asdict(insight)
    path = DATA_DIR / "latest_insight.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_prev_ranking() -> list[dict]:
    """从 cycle_history 中取前一日的 ranking（若有），否则返回空"""
    # 先尝试从 latest_ranking.json 读取（作为 prev 的来源在每日更新前保留旧值）
    prev_path = DATA_DIR / "prev_ranking.json"
    if prev_path.exists():
        try:
            data = json.loads(prev_path.read_text())
            return data.get("ranking", [])
        except Exception:
            pass
    return []


def rotate_ranking_for_prev() -> None:
    """在每日更新前，将当前 latest_ranking 存为 prev_ranking"""
    cur = DATA_DIR / "latest_ranking.json"
    prev = DATA_DIR / "prev_ranking.json"
    if cur.exists():
        try:
            prev.write_text(cur.read_text())
        except Exception:
            pass

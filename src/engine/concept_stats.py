"""概念聚合统计 — 一对多映射，按概念分组涨停股 / 排行股

核心指标（涨停聚合，一股多概念全部计入）：
- limit_up_count: 同概念涨停家数（决定板块集中度）
- max_board:     同概念最高连板高度
- ladder:        同概念涨停股梯队结构 {board_count: stock_count}

数据源：
- 涨停股: data/limit_up_cache.json （含 code, name, change_pct, board_count）
- 概念映射: data/concept_cache.json → load_stock_to_concepts()

下游消费：
- daily_review._build_concept_zt_stats → 复盘"最强概念梯队"展示
- market_insight._analyze_concepts    → 板块集中度（替代 industry 聚合）
- auction_scorer._score_d4_sector     → D4 同概念涨停数加分
- ranking 显示                        → top_concepts_for_stock 选 1-2 热概念
"""
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class ConceptHeat:
    """单个概念的涨停聚合状态"""
    name: str
    limit_up_count: int                  # 同概念涨停家数
    limit_up_codes: list[str] = field(default_factory=list)
    limit_up_names: list[str] = field(default_factory=list)
    max_board: int = 1                   # 同概念最高连板高度
    top_stock_code: str = ""             # 最高连板代表股代码
    top_stock_name: str = ""             # 最高连板代表股名称
    ladder: dict[int, int] = field(default_factory=dict)  # {board: count}
    avg_change_pct: float = 0.0          # 同概念平均当日涨幅


def aggregate_concept_limit_ups(
    limit_up_records: list[dict],
    stock_to_concepts: Optional[dict[str, list[str]]] = None,
) -> list[ConceptHeat]:
    """按概念聚合涨停股 — 一股多概念，全部计入

    Args:
        limit_up_records: 涨停股记录，需含 code, name, board_count(or continuous_limit_up),
                          change_pct，可选 concepts
        stock_to_concepts: code→[concept_name]，None 时尝试从 record.concepts 读取

    Returns: 按 (limit_up_count desc, max_board desc) 排序的概念热度列表
    """
    if stock_to_concepts is None:
        stock_to_concepts = {}

    by_concept: dict[str, list[dict]] = defaultdict(list)
    for rec in limit_up_records:
        code = str(rec.get("code") or "")
        if not code:
            continue
        concepts = (
            stock_to_concepts.get(code)
            or rec.get("concepts")
            or []
        )
        for c in concepts:
            if c:
                by_concept[c].append(rec)

    out: list[ConceptHeat] = []
    for name, stocks in by_concept.items():
        ladder: dict[int, int] = defaultdict(int)
        boards: list[int] = []
        for s in stocks:
            b = int(s.get("board_count")
                    or s.get("continuous_limit_up")
                    or 1)
            b = max(1, b)
            ladder[b] += 1
            boards.append(b)

        max_b = max(boards) if boards else 1
        top = next(
            (s for s in stocks
             if int(s.get("board_count") or s.get("continuous_limit_up") or 1) == max_b),
            stocks[0],
        )
        chgs = [float(s.get("change_pct") or 0) for s in stocks]
        out.append(ConceptHeat(
            name=name,
            limit_up_count=len(stocks),
            limit_up_codes=[str(s.get("code") or "") for s in stocks],
            limit_up_names=[s.get("name") or "" for s in stocks],
            max_board=max_b,
            top_stock_code=str(top.get("code") or ""),
            top_stock_name=top.get("name") or "",
            # 按板数从高到低排（梯队展示需要）
            ladder=dict(sorted(ladder.items(), key=lambda x: -x[0])),
            avg_change_pct=round(sum(chgs) / len(chgs), 2) if chgs else 0.0,
        ))

    out.sort(key=lambda h: (h.limit_up_count, h.max_board), reverse=True)
    return out


def aggregate_concept_ranking(
    ranking: list[dict],
    stock_to_concepts: Optional[dict[str, list[str]]] = None,
) -> list[dict]:
    """按概念聚合排行榜入榜股 — 一股多概念，全部计入

    用途：替代 _analyze_sectors 中按 industry 聚合的逻辑，
        生成"前 N 个最热概念占榜比例"（板块集中度）

    Args:
        ranking: top30 ranking 记录，需含 code, name, gain_10d，可选 concepts
        stock_to_concepts: code→[concept_name]，None 时 fallback 到 record.concepts

    Returns: list of dict(name, count, codes, names, avg_gain_10d, max_gain_10d,
                          top_stock, weight)，按 count desc 排序
    """
    if stock_to_concepts is None:
        stock_to_concepts = {}

    by_concept: dict[str, list[dict]] = defaultdict(list)
    for item in ranking:
        code = str(item.get("code") or "")
        if not code:
            continue
        concepts = (
            stock_to_concepts.get(code)
            or item.get("concepts")
            or []
        )
        for c in concepts:
            if c:
                by_concept[c].append(item)

    total = len(ranking)
    out: list[dict] = []
    for name, stocks in by_concept.items():
        gains = [float(s.get("gain_10d") or 0) for s in stocks]
        top = max(stocks, key=lambda s: float(s.get("gain_10d") or 0))
        out.append({
            "name": name,
            "count": len(stocks),
            "codes": [str(s.get("code") or "") for s in stocks],
            "names": [s.get("name") or "" for s in stocks],
            "avg_gain_10d": round(sum(gains) / len(gains), 2) if gains else 0.0,
            "max_gain_10d": round(max(gains), 2) if gains else 0.0,
            "top_stock": top.get("name") or "",
            # 注意：一股多概念时各概念的 weight 之和 > 100%，仅作展示参考
            "weight": round(len(stocks) / total * 100, 1) if total else 0.0,
        })

    out.sort(key=lambda h: -h["count"])
    return out


def top_concepts_for_stock(
    concepts: list[str],
    global_concept_heats: list[ConceptHeat],
    top_n: int = 2,
) -> list[str]:
    """该股的 1~N 个最热概念（按涨停聚合热度排）— 用于 UI 显示

    Args:
        concepts: 该股所属概念列表
        global_concept_heats: 全市场概念热度（已按热度排序）
    Returns: 概念名列表，无任何概念命中热度时返回空
    """
    if not concepts:
        return []
    rank_map = {h.name: i for i, h in enumerate(global_concept_heats)}
    hot = [c for c in concepts if c in rank_map]
    hot.sort(key=lambda c: rank_map[c])
    return hot[:top_n]


def max_same_concept_lu_count(
    concepts: list[str],
    global_concept_heats: list[ConceptHeat],
) -> int:
    """该股所属概念中的最大涨停家数 — 替代 sector_limit_up_count 语义"""
    if not concepts:
        return 0
    heat_map = {h.name: h.limit_up_count for h in global_concept_heats}
    return max((heat_map.get(c, 0) for c in concepts), default=0)


def serialize_heat(h: ConceptHeat) -> dict:
    """ConceptHeat → JSON 可序列化 dict（dataclass.asdict 已够用，此处显式控字段顺序）"""
    return asdict(h)


def enrich_ranking_with_top_concepts(
    ranking_records: list[dict],
    top_n: int = 2,
) -> list[dict]:
    """为 ranking 每一行注入 top_concepts 字段（按全市场涨停热度排）

    数据源：limit_up_cache.json 最新一天 + concept_cache.json
    实现：先按概念聚合涨停股得到全局热度，再为每只股选 1~N 个最热概念

    Args:
        ranking_records: in-place 修改的 ranking 行（需有 code 字段，原 concepts 字段）
        top_n: 每只股展示的概念数（默认 2）

    Returns: 同入参（原地修改后返回）
    """
    if not ranking_records:
        return ranking_records

    # 1. 加载今日涨停 + 概念映射
    try:
        from src.config import DATA_DIR
        import json as _json
        cache_file = DATA_DIR / "limit_up_cache.json"
        if not cache_file.exists():
            return ranking_records
        cache = _json.loads(cache_file.read_text())
        latest = sorted(cache.keys())[-1] if cache else ""
        if not latest:
            return ranking_records
        today_lu = cache.get(latest, []) or []
    except Exception:
        return ranking_records

    try:
        from src.data.concept_fetcher import load_stock_to_concepts
        c_map = load_stock_to_concepts() or {}
    except Exception:
        c_map = {}

    if not c_map:
        return ranking_records

    # 2. 全市场涨停按概念聚合（一股多概念全部计入）
    heats = aggregate_concept_limit_ups(today_lu, c_map)

    # 3. 每只 ranking 行选 top_n 个最热概念
    for row in ranking_records:
        cs = row.get("concepts") or []
        row["top_concepts"] = top_concepts_for_stock(cs, heats, top_n=top_n)

    return ranking_records


def format_ladder_string(ladder: dict[int, int]) -> str:
    """{3:2, 2:4, 1:7} → "3板(2只), 2板(4只), 首板(7只)"

    板数显示：>=2 板用阿拉伯数字 + "板"；=1 板用 "首板"
    """
    parts = []
    for board, cnt in sorted(ladder.items(), key=lambda x: -x[0]):
        label = "首板" if board == 1 else f"{board}板"
        parts.append(f"{label}({cnt}只)")
    return ", ".join(parts)

"""选股结果概念/行业富化 — 服务端为 latest_screener.json 注入 top_concepts + industry

Story dashboard-hits-table-display-2.4：dashboard 选股表板块列原本只查 ranking
TOP30 涨幅榜的 top_concepts，连板命中股大多落在榜外 → 大多数行只显示纯行业。
本模块在 scheduler 写入 latest_screener.json 之前 in-place 注入两个新字段，
使 dashboard 与 ranking / 邮件路径显示一致（"概念A/概念B (行业)"）。

模式参考：`src/engine/concept_stats.enrich_ranking_with_top_concepts`（同
namespace 同 in-place + cache fallback chain）；解析顺序对齐
`src/notify/email_sender.py:565-595` 的 fallback 链。

设计要点：
- 永不抛错给 caller（BR-3.5）：每一处文件 / 字典访问 try/except 静默
- 幂等（BR-5.7）：每次都基于 hit['code'] 重查 + 覆盖；同输入两次调用字符级相等
- 元概念双保险（BR-3.4）：输出层再过一次 filter_concepts；即便 ranking
  / cache 上游已过滤，也保证最终 hits 字段不漏元标签
- 防御性 dict.get（BLIND-FLOW-003）：hits_data 顶层无 'hits' 键时不抛错
- ScreenerHit dataclass 不扩展（BR-3.6）：top_concepts / industry 仅在
  运行时 dict 层注入，与 ranking 路径模式一致
"""
from typing import Optional

from src.config import DATA_DIR
from src.data.json_io import load_json_file


# ============================================================
# Internal cache loaders (module-level so tests can monkeypatch)
# ============================================================

def _load_stock_to_concepts_safe() -> dict[str, list[str]]:
    """Load `concept_cache.json` → {code: [concept_name, ...]}; {} on any error."""
    try:
        from src.data.concept_fetcher import load_stock_to_concepts
        return load_stock_to_concepts() or {}
    except Exception:
        return {}


def _load_concept_heats_safe(c_map: dict[str, list[str]]) -> list:
    """Aggregate latest-day limit-up records by concept; [] on any error.

    Reads `data/limit_up_cache.json`, picks the latest date key, and runs
    `aggregate_concept_limit_ups` so downstream `top_concepts_for_stock`
    can rank a stock's concepts by current heat.
    """
    try:
        from src.engine.concept_stats import aggregate_concept_limit_ups
        lu_file = DATA_DIR / "limit_up_cache.json"
        lu = load_json_file(lu_file)
        if not isinstance(lu, dict) or not lu:
            return []
        latest = sorted(lu.keys())[-1]
        return aggregate_concept_limit_ups(lu.get(latest, []) or [], c_map)
    except Exception:
        return []


def _load_industry_cache_safe() -> dict[str, str]:
    """Load `industry_cache.json` → {code: industry}; {} on any error."""
    try:
        ic_file = DATA_DIR / "industry_cache.json"
        data = load_json_file(ic_file)
        if not isinstance(data, dict):
            return {}
        data = data or {}
        return {str(k): str(v) for k, v in data.items() if v}
    except Exception:
        return {}


def _filter_concepts_safe(concepts) -> list[str]:
    """Apply concept_blacklist.filter_concepts; raise so caller can decide.

    Caller wraps in try/except and falls back to [] on failure (BR-3.5).
    """
    from src.engine.concept_blacklist import filter_concepts
    return filter_concepts(list(concepts or []))


# ============================================================
# Main entry point
# ============================================================

def enrich_screener_hits_with_concepts(
    hits_data: dict,
    ranking_data: Optional[dict],
) -> None:
    """In-place inject `top_concepts` (list[str]) + `industry` (str|None) per hit.

    Resolution priority (top_concepts):
      1. ranking_data row matching hit['code'] (already-injected
         `top_concepts` by enrich_ranking_with_top_concepts)
      2. concept_cache + limit_up_cache aggregation via
         `top_concepts_for_stock(c_map[code], heats, top_n=2)`
      3. []

    Resolution priority (industry):
      1. ranking_data row's `industry` field
      2. industry_cache.json lookup
      3. None

    All cache I/O / lookup failures degrade silently; helper never raises.
    Ranking-priority short-circuits cache load: if every hit is ranking-resolved
    we still load caches (cheap dict ops + warm file cache) for code uniformity,
    matching email_sender.py:565-595 ordering.

    Args:
        hits_data: {"date": str, "hits": list[dict]}; hits is mutated in place.
                    Robust to missing "hits" key (no-op).
        ranking_data: {"ranking": list[dict]} or None. Each ranking row may
                      contain `top_concepts` (list) and `industry` (str).
    """
    hits = hits_data.get("hits") if isinstance(hits_data, dict) else None
    if not hits:
        return None

    # --- Step 1: ranking-derived lookups (priority 1 for both fields) ---
    ranking_top_concepts: dict[str, list[str]] = {}
    ranking_industry: dict[str, str] = {}
    if isinstance(ranking_data, dict):
        try:
            for r in (ranking_data.get("ranking") or []):
                code = str(r.get("code", "")) if isinstance(r, dict) else ""
                if not code:
                    continue
                tc = r.get("top_concepts")
                if isinstance(tc, list) and tc:
                    ranking_top_concepts[code] = list(tc)
                ind = r.get("industry")
                if ind:
                    ranking_industry[code] = str(ind)
        except Exception:
            pass

    # --- Step 2: cache fallback loaders (priority 2) ---
    try:
        c_map = _load_stock_to_concepts_safe() or {}
    except Exception:
        c_map = {}

    try:
        heats = _load_concept_heats_safe(c_map) or []
    except Exception:
        heats = []

    try:
        industry_cache = _load_industry_cache_safe() or {}
    except Exception:
        industry_cache = {}

    # --- Step 3: per-hit mutation ---
    for h in hits:
        try:
            code = str(h.get("code", "")) if isinstance(h, dict) else ""
        except Exception:
            code = ""

        # top_concepts: ranking → c_map+heats → []
        top_concepts: list[str] = []
        if code and code in ranking_top_concepts:
            top_concepts = list(ranking_top_concepts[code])
        elif code and code in c_map:
            try:
                from src.engine.concept_stats import top_concepts_for_stock
                top_concepts = top_concepts_for_stock(
                    list(c_map.get(code) or []), heats, top_n=2,
                ) or []
            except Exception:
                top_concepts = []

        # Double-safeguard meta-concept filter (BR-3.4); failure → []
        try:
            top_concepts = _filter_concepts_safe(top_concepts) or []
        except Exception as e:
            print(f"[选股富化] 概念过滤失败: {e}")
            top_concepts = []

        # Coerce to list[str] (BR Data Validation: type contract)
        h["top_concepts"] = [str(c) for c in top_concepts if c]

        # industry: ranking → industry_cache → None
        if code and code in ranking_industry:
            h["industry"] = ranking_industry[code]
        elif code and code in industry_cache:
            val = industry_cache[code]
            h["industry"] = str(val) if val else None
        else:
            h["industry"] = None

    return None

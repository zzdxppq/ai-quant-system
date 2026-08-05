"""9:27 选股 / 日K 解读共用的市场环境（与看板 1进2 口径一致）。"""
from __future__ import annotations

from typing import Any, Optional

from src.config import now_cn
from src.data.analytics_store import (
    load_latest_leader_document,
    load_latest_review_document,
    load_latest_sentiment_document,
    load_review_history_document,
)
from src.engine.dashboard_decision import _b1_rate_from_review, _prev_trading_day_iso


def _scorecard_raw(doc: dict | None, label: str) -> Optional[float]:
    if not isinstance(doc, dict):
        return None
    for ind in (doc.get("scorecard") or {}).get("indicators") or []:
        if ind.get("label") == label:
            raw = ind.get("raw")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
            today = str(ind.get("today") or "").strip().replace("%", "").replace("—", "")
            if today:
                try:
                    return float(today.split()[0])
                except (TypeError, ValueError):
                    pass
            break
    return None


def load_prev_trading_day_review_document() -> dict[str, Any]:
    """今日 9:27 选股 / 看板用的「上一交易日」复盘（pick=今日 → 取上一交易日 exact + 矩阵回退）。"""
    return load_review_document_for_pick_date(now_cn().strftime("%Y-%m-%d"))


def prev_trading_day_iso_before(cutoff_iso: str) -> str:
    """选股日 pick 的上一交易日（YYYY-MM-DD），用于回测按日取复盘环境。"""
    from datetime import datetime, timedelta

    try:
        d = datetime.strptime(str(cutoff_iso or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        d = now_cn().date()
    for _ in range(12):
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
    return d.strftime("%Y-%m-%d")


def review_has_b1_source(doc: dict | None) -> bool:
    """复盘是否含可用 1进2（晋级矩阵或 scorecard）。"""
    if not isinstance(doc, dict):
        return False
    if doc.get("prev_board_groups"):
        return True
    return b1_rate_from_review_document(doc) is not None


def _review_has_promotion_matrix(doc: dict | None) -> bool:
    return bool(isinstance(doc, dict) and doc.get("prev_board_groups"))


def _pick_review_for_prev_iso(prev_iso: str, hist: list[dict[str, Any]]) -> dict[str, Any]:
    """上一交易日复盘：优先晋级矩阵，其次 scorecard 1进2。"""
    if isinstance(hist, list):
        for entry in reversed(hist):
            if str(entry.get("date") or "")[:10] == prev_iso:
                if _review_has_promotion_matrix(entry):
                    return entry
                if b1_rate_from_review_document(entry) is not None:
                    return entry
                break

    rd = load_latest_review_document()
    if str(rd.get("date") or "")[:10] == prev_iso:
        if _review_has_promotion_matrix(rd):
            return rd
        if b1_rate_from_review_document(rd) is not None:
            return rd

    if isinstance(hist, list):
        for entry in reversed(hist):
            d = str(entry.get("date") or "")[:10]
            if not d or d > prev_iso:
                continue
            if _review_has_promotion_matrix(entry):
                return entry
        for entry in reversed(hist):
            d = str(entry.get("date") or "")[:10]
            if not d or d > prev_iso:
                continue
            if b1_rate_from_review_document(entry) is not None:
                return entry
    return {}


def _find_review_in_history_by_date(target_iso: str) -> dict[str, Any]:
    target = str(target_iso or "")[:10]
    if not target:
        return {}
    hist = load_review_history_document()
    if isinstance(hist, list):
        for entry in reversed(hist):
            if str(entry.get("date") or "")[:10] == target:
                return entry
    rd = load_latest_review_document()
    if str(rd.get("date") or "")[:10] == target:
        return rd
    return {}


def resolve_b1_with_review_date(*, hint_date: str | None = None) -> tuple[Optional[float], str]:
    """看板 1进2：(成功率, 复盘日期)。hint_date 为落盘 b1_review_date 时优先按该日查 history。"""
    today = now_cn().strftime("%Y-%m-%d")
    prev_iso = prev_trading_day_iso_before(today)

    hint = str(hint_date or "")[:10]
    if hint:
        doc = _find_review_in_history_by_date(hint)
        b1 = b1_rate_from_review_document(doc)
        if b1 is not None:
            return b1, hint

    doc = load_prev_trading_day_review_document()
    b1 = b1_rate_from_review_document(doc)
    d = str((doc or {}).get("date") or "")[:10]
    if b1 is not None:
        return b1, d or prev_iso
    return None, prev_iso


def load_review_document_for_pick_date(pick_date: str) -> dict[str, Any]:
    """选股日早盘所用复盘：上一交易日 exact 匹配，缺则取不晚于该日的最近含 1进2 数据记录。"""
    prev_iso = prev_trading_day_iso_before(pick_date)
    hist = load_review_history_document()
    return _pick_review_for_prev_iso(prev_iso, hist if isinstance(hist, list) else [])


def resolve_review_document_for_api(now=None) -> dict[str, Any]:
    """复盘 API / 看板环境：15:00 前只看上一交易日快照；15:00 后且已有当日复盘则用当日。"""
    n = now if now is not None else now_cn()
    today_str = n.strftime("%Y-%m-%d")
    cutoff = n.replace(hour=15, minute=0, second=0, microsecond=0)

    latest = load_latest_review_document()
    latest_date = str((latest or {}).get("date") or "")[:10]

    if n >= cutoff and latest_date == today_str and isinstance(latest, dict):
        return dict(latest)

    prev_doc = load_prev_trading_day_review_document()
    if prev_doc:
        return dict(prev_doc)

    fallback = latest_review_history_before(today_str)
    return dict(fallback) if isinstance(fallback, dict) else {}


def load_screener_market_env() -> dict[str, Any]:
    """选股 per_stock_decision 用的市场环境。"""
    env: dict[str, Any] = {
        "market_limit_down": None,
        "weighted_auction_gain": None,
        "yesterday_lianban_today_avg": None,
        "b1_rate": None,
        "concentration": None,
    }
    try:
        sd = load_latest_sentiment_document()
        if sd:
            env["weighted_auction_gain"] = sd.get("weighted_auction_gain")
            mk = sd.get("market") or {}
            env["market_limit_down"] = mk.get("limit_down")
    except Exception:
        pass
    try:
        ld = load_latest_leader_document()
        if ld:
            ya = ld.get("yesterday_main_board_avg_auction") or {}
            env["yesterday_lianban_today_avg"] = ya.get("avg_change_pct")
    except Exception:
        pass
    env["b1_rate"] = _b1_rate_from_review()
    prev_rev = load_prev_trading_day_review_document()
    conc = _scorecard_raw(prev_rev, "板块集中度")
    if conc is not None:
        env["concentration"] = conc
    return env


def review_context_for_pick_date(pick_date: str, *, tier_ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """回测/历史归档：按选股日取复盘环境 + 可选同日最高连板档均竞价。"""
    rd = load_review_document_for_pick_date(pick_date)
    if not rd:
        return {
            "concept_zt_stats": [],
            "space_board_today": None,
            "highest_board_tier_today": tier_ctx,
            "market_highest_board": None,
        }
    mhb = int(rd.get("highest_board") or 0)
    return {
        "concept_zt_stats": rd.get("concept_zt_stats") or [],
        "space_board_today": (rd.get("relay_env") or {}).get("prev_space_board_today"),
        "highest_board_tier_today": tier_ctx,
        "market_highest_board": mhb if mhb > 0 else None,
    }


def load_screener_review_context() -> dict[str, Any]:
    """选股决策用的复盘上下文（空间板 / 概念热度 / 最高板）。"""
    rd = load_prev_trading_day_review_document()
    tier_ctx: dict[str, Any] | None = None
    try:
        from src.engine.dashboard_decision import highest_board_tier_from_leader_rows

        ld = load_latest_leader_document() or {}
        rows = list(ld.get("main_board_leaders") or [])
        if not rows and ld.get("main_board_leader"):
            rows = [ld["main_board_leader"]]
        tier_ctx = highest_board_tier_from_leader_rows(rows)
    except Exception:
        tier_ctx = None
    if not rd:
        return {
            "concept_zt_stats": [],
            "space_board_today": None,
            "highest_board_tier_today": tier_ctx,
            "market_highest_board": None,
            "lianban_index_pct": None,
        }
    mhb = int(rd.get("highest_board") or 0)
    return {
        "concept_zt_stats": rd.get("concept_zt_stats") or [],
        "space_board_today": (rd.get("relay_env") or {}).get("prev_space_board_today"),
        "highest_board_tier_today": tier_ctx,
        "market_highest_board": mhb if mhb > 0 else None,
        "lianban_index_pct": _scorecard_raw(rd, "昨日连板指数"),
    }


def latest_review_history_before(cutoff_date: str) -> dict | None:
    """严格早于 cutoff_date（YYYY-MM-DD）的复盘历史中最新一条。"""
    hist = load_review_history_document()
    if not isinstance(hist, list) or not cutoff_date:
        return None
    best: dict | None = None
    best_d = ""
    for h in hist:
        if not isinstance(h, dict):
            continue
        d = str(h.get("date") or "")[:10]
        if not d or d >= cutoff_date[:10]:
            continue
        if best is None or d > best_d:
            best = h
            best_d = d
    return best


def b1_rate_from_prev_board_groups(review: dict | None) -> Optional[float]:
    """从晋级矩阵 1进2 组重算成功率（与 _build_scorecard 同源，优先于落盘 scorecard）。"""
    if not isinstance(review, dict):
        return None
    g1 = next(
        (g for g in (review.get("prev_board_groups") or []) if g.get("prev_board") == 1),
        None,
    )
    if not g1:
        return None
    promoted = len(g1.get("promoted") or [])
    total = promoted + len(g1.get("failed") or [])
    if total <= 0:
        return None
    return round(promoted / total * 100, 1)


def b1_rate_from_review_document(review: dict | None) -> Optional[float]:
    """复盘文档的 1进2 成功率：晋级矩阵优先，避免 scorecard 与雁阵图不一致。"""
    from_groups = b1_rate_from_prev_board_groups(review)
    if from_groups is not None:
        return from_groups
    return _scorecard_raw(review, "1进2成功率")


def scorecard_b1_and_concentration(review: dict | None) -> tuple[Optional[float], Optional[float]]:
    if not isinstance(review, dict):
        return None, None
    return b1_rate_from_review_document(review), _scorecard_raw(review, "板块集中度")


def build_kline_analysis_market_ctx(code: str) -> dict[str, Any]:
    """日 K 右侧解读：1进2/集中度与看板一致，统一为上一交易日复盘（不按个股历史选股日覆盖）。"""
    from src.engine.dashboard_decision import _b1_rate_from_review, _b1_review_date_iso

    env = load_screener_market_env()
    ctx = load_screener_review_context()
    prev_rev = load_prev_trading_day_review_document()
    _, conc_rev = scorecard_b1_and_concentration(prev_rev)
    b1 = _b1_rate_from_review()
    market_ctx: dict[str, Any] = {
        "concentration": conc_rev if conc_rev is not None else env.get("concentration"),
        "b1_rate": b1,
        "b1_review_date": _b1_review_date_iso(),
        "lianban_index_pct": ctx.get("lianban_index_pct"),
        "market_limit_down": env.get("market_limit_down"),
        "concept_zt_stats": ctx.get("concept_zt_stats") or [],
        "space_board_today": ctx.get("space_board_today"),
        "market_highest_board": ctx.get("market_highest_board"),
        "highest_board_tier_today": ctx.get("highest_board_tier_today"),
    }
    try:
        from src.data.analytics_store import load_screener_history_entries

        c6 = str(code).replace(".", "").strip()[-6:].zfill(6)
        rows = [
            r
            for r in load_screener_history_entries()
            if str(r.get("code") or "").replace(".", "").strip()[-6:].zfill(6) == c6
        ]
        rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
        if rows:
            r0 = rows[0]
            for k in ("weighted_auction_gain", "yesterday_lianban_today_avg"):
                v = r0.get(k)
                if v is not None and v != "":
                    try:
                        market_ctx[k] = float(v)
                    except (TypeError, ValueError):
                        pass
            ld = r0.get("market_limit_down")
            if ld is not None and ld != "":
                try:
                    market_ctx["market_limit_down"] = int(ld)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    try:
        from src.data.analytics_store import load_migrated_snapshot

        insight = load_migrated_snapshot("latest_insight.json") or {}
        ap = insight.get("attack_phase") or {}
        market_ctx["attack_phase"] = ap.get("phase")
        market_ctx["attack_count"] = ap.get("attack_count")
    except Exception:
        pass
    return market_ctx


def recompute_latest_screener_per_stock_decisions() -> int:
    """按当前环境口径重算 latest_screener 中 per_stock_decision 并写回库。"""
    from src.data.analytics_store import load_migrated_snapshot, save_from_latest_filename
    from src.engine.screener_decision import compute_per_stock_decision

    payload = load_migrated_snapshot("latest_screener.json")
    if not isinstance(payload, dict):
        return 0
    hits = payload.get("hits") or []
    if not hits:
        return 0
    market_env = load_screener_market_env()
    ctx = load_screener_review_context()
    for h in hits:
        if not isinstance(h, dict):
            continue
        h["per_stock_decision"] = compute_per_stock_decision(
            h,
            market_env,
            concept_zt_stats=ctx.get("concept_zt_stats") or [],
            space_board_today=ctx.get("space_board_today"),
            market_highest_board=ctx.get("market_highest_board"),
            highest_board_tier_today=ctx.get("highest_board_tier_today"),
        )
    save_from_latest_filename("latest_screener.json", payload)
    return len(hits)


def find_hit_in_latest_screener(code: str) -> dict[str, Any]:
    """从 DuckDB latest_screener 行中匹配单只 hit（含 extra_json 字段）。"""
    from src.data.analytics_store import load_migrated_snapshot

    c6 = str(code).replace(".", "").strip()[-6:].zfill(6)
    payload = load_migrated_snapshot("latest_screener.json") or {}
    for r in payload.get("hits") or []:
        if str(r.get("code") or "").replace(".", "").strip()[-6:].zfill(6) == c6:
            return dict(r)
    rank = load_migrated_snapshot("latest_ranking.json") or {}
    for r in rank.get("ranking") or []:
        if str(r.get("code") or "").replace(".", "").strip()[-6:].zfill(6) == c6:
            return dict(r)
    return {}

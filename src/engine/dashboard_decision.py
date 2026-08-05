"""看板参与/参考指标与 v2 决策树（与 latest_advice.json 单一真源对齐）

主板竞价跌停(>5⚠)或跌>9%(>9⚠)、加权接力情绪指数(昨日涨停池·主板全样本分档)、1进2、
昨日主板连板高标竞价（无则回退市场高标）→ 决策树；
参考区含 10 日榜 TOP30 全梯队加权竞价（与 sentiment.weighted_auction_gain 同源）。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import pandas as pd

from src.config import now_cn
from src.data.analytics_store import (
    load_latest_ranking_document,
    load_latest_review_document,
    load_latest_sentiment_document,
    load_review_history_document,
)
from src.engine.leader_feedback import (
    compute_yesterday_limit_down_today_auction,
    compute_yesterday_zb_today_auction,
)
from src.engine.sentiment_pool import _relay_zt_is_main_board_nst


def _norm_code6(s: str) -> str:
    d = "".join(c for c in str(s or "") if c.isdigit())
    if not d:
        return ""
    if len(d) < 6:
        return d.zfill(6)
    return d[-6:].zfill(6)


def _spot_auction_pct(code: str, spot_df: pd.DataFrame | None) -> Optional[float]:
    """从全市场 spot 行算 (open/pre_close-1)*100。"""
    if spot_df is None or getattr(spot_df, "empty", True) or not code:
        return None
    c6 = _norm_code6(code)
    if len(c6) != 6:
        return None
    try:
        s = spot_df.copy()
        s["_c6"] = s["code"].astype(str).map(_norm_code6)
        m = s[s["_c6"] == c6]
        if m.empty:
            return None
        row = m.iloc[0]
        op = float(row.get("open", 0) or 0)
        pc = float(row.get("pre_close", 0) or 0)
        if op <= 0 or pc <= 0:
            return None
        return round((op / pc - 1) * 100, 2)
    except Exception:
        return None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _mb_60_00_nonst(code: str, name: str = "") -> bool:
    c = str(code).strip().zfill(6)
    if len(c) != 6 or not c.isdigit():
        return False
    if c.startswith(("300", "301", "688", "8", "4")):
        return False
    if not (c.startswith("60") or c.startswith("00")):
        return False
    u = (name or "").upper()
    if "ST" in u or "*ST" in u:
        return False
    return True


def count_main_board_auction_limit_down(market: dict | None) -> int:
    """主板（60/00 非ST）中竞价跌幅 ≤ -9.9% 且已计入全市场跌停样本的只数。"""
    ld_list = (market or {}).get("limit_down_list") or []
    n = 0
    for item in ld_list:
        code = str(item.get("code", "")).zfill(6)
        name = str(item.get("name", ""))
        if not _mb_60_00_nonst(code, name):
            continue
        try:
            pct = float(item.get("auction_pct", 0))
        except (TypeError, ValueError):
            continue
        if pct <= -9.9:
            n += 1
    return n


def _prev_trading_date_str() -> str:
    """东财接口 YYYYMMDD：向前找非周末的上一自然日（节假日未用 chinese_calendar，与池接口一致）。"""
    d = now_cn().date()
    for _ in range(10):
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y%m%d")
    return d.strftime("%Y%m%d")


def compute_yesterday_main_board_relay_decision_index(
    spot_df: pd.DataFrame | None,
) -> Optional[dict]:
    """昨日东财涨停池中 **全部** 主板非 ST 样本（60/00，剔除 ST 名），按连板数 lbc 分档后 0.2/0.5/0.3 加权。

    lbc 缺失或为 0 时与首板同档（仍计入昨日涨停主板样本，不丢弃）。
    """
    try:
        from src.data.zt_pool_api import fetch_zt_pool_with_retry
    except Exception:
        return None

    ymd = _prev_trading_date_str()
    pool = fetch_zt_pool_with_retry(ymd) or {}
    if not pool or spot_df is None or getattr(spot_df, "empty", True):
        return None

    spot = spot_df.copy()
    spot["code"] = spot["code"].astype(str).map(_norm_code6)
    by_tier: dict[int, list[float]] = {1: [], 2: [], 3: []}
    for raw_code, info in pool.items():
        nm = str(info.get("name") or "")
        code = _norm_code6(str(raw_code))
        if len(code) != 6 or not _relay_zt_is_main_board_nst(code, nm):
            continue
        board = int(info.get("lbc", 0) or 0)
        c6 = code
        m = spot[spot["code"] == c6]
        if m.empty:
            continue
        row = m.iloc[0]
        try:
            op = float(row.get("open", 0) or 0)
            pc = float(row.get("pre_close", 0) or 0)
        except (TypeError, ValueError):
            continue
        if op <= 0 or pc <= 0:
            continue
        ag = (op / pc - 1) * 100
        # 首板档：lbc<=1（含接口缺省 0），与 sentiment_pool 接力口径一致
        bucket = 1 if board <= 1 else (2 if board == 2 else 3)
        by_tier[bucket].append(ag)

    def _avg(lst: list[float]) -> float:
        return round(sum(lst) / len(lst), 2) if lst else 0.0

    if not any(by_tier[i] for i in (1, 2, 3)):
        return None

    a1, a2, a3 = _avg(by_tier[1]), _avg(by_tier[2]), _avg(by_tier[3])
    index_val = round(a1 * 0.2 + a2 * 0.5 + a3 * 0.3, 2)
    if index_val >= 1.5:
        verdict = "良好"
    elif index_val >= 0:
        verdict = "一般"
    else:
        verdict = "差"

    return {
        "index": index_val,
        "verdict": verdict,
        "first_board": {"avg": a1, "count": len(by_tier[1])},
        "two_board": {"avg": a2, "count": len(by_tier[2])},
        "three_plus": {"avg": a3, "count": len(by_tier[3])},
        "pool_date": ymd,
    }


def _prev_trading_day_iso() -> str:
    ymd = _prev_trading_date_str()
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"


def _b1_rate_from_review() -> Optional[float]:
    """看板 1进2：上一交易日复盘晋级矩阵（与复盘页雁阵图一致）。"""
    from src.engine.screener_market_env import resolve_b1_with_review_date

    b1, _ = resolve_b1_with_review_date()
    return b1


def _b1_review_date_iso() -> str:
    """1进2 对应复盘日期（YYYY-MM-DD），用于看板标注。"""
    from src.engine.screener_market_env import resolve_b1_with_review_date

    _, d = resolve_b1_with_review_date()
    return d


def refresh_space_auction_from_spot(
    sp: dict[str, Any],
    spot_df: pd.DataFrame | None,
    *,
    prefer_spot: bool = True,
) -> dict[str, Any]:
    """用全市场 spot 覆盖连板高标竞价涨跌幅（避免 leader 里残留昨收/涨停价 9.9%）。"""
    if not isinstance(sp, dict):
        return sp or {}
    tier_codes = [
        str(c or "").strip().zfill(6)
        for c in (sp.get("tier_codes") or [])
        if str(c or "").strip().isdigit()
    ]
    tier_codes = [c for c in tier_codes if len(c) == 6]
    if len(tier_codes) > 1 and spot_df is not None and not getattr(spot_df, "empty", True):
        pcts: list[float] = []
        for code in tier_codes:
            ap = _spot_auction_pct(code, spot_df)
            if ap is not None:
                pcts.append(ap)
        if pcts:
            max_bc = sp.get("board_count")
            try:
                max_bc_i = int(max_bc) if _is_num(max_bc) else 0
            except (TypeError, ValueError):
                max_bc_i = 0
            name = sp.get("name") or (
                f"{len(tier_codes)}只{max_bc_i}板" if max_bc_i >= 2 else f"{len(tier_codes)}只高标"
            )
            avg = round(sum(pcts) / len(pcts), 2)
            au = _leader_row_auction(
                {
                    "leader_code": "",
                    "leader_name": name,
                    "auction_change_pct": avg,
                    "board_count": max_bc_i or None,
                    "signal": sp.get("signal"),
                }
            )
            return {
                **sp,
                **au,
                "tier_count": len(tier_codes),
                "tier_codes": tier_codes,
            }
    code = str(sp.get("code") or "").strip().zfill(6)
    if len(code) != 6 or spot_df is None or getattr(spot_df, "empty", True):
        return sp
    ap = _spot_auction_pct(code, spot_df)
    if ap is None:
        return sp
    if not prefer_spot:
        try:
            old = float(sp.get("pct"))
            if old is not None:
                return sp
        except (TypeError, ValueError):
            pass
    au = _leader_row_auction(
        {
            "leader_code": code,
            "leader_name": sp.get("name") or "",
            "auction_change_pct": ap,
            "board_count": sp.get("board_count"),
            "signal": sp.get("signal"),
        }
    )
    return {**sp, **au}


def _leader_row_auction(row: dict | None) -> dict[str, Any]:
    """从 latest_leader 的龙头子 dict 读取今日竞价涨跌幅与展示文案。"""
    if not row:
        return {
            "pct": None, "label": "—", "name": "", "code": "",
            "board_count": None, "signal": None,
        }
    code = str(row.get("leader_code", "") or "")
    name = str(row.get("leader_name", "") or "")
    pct = row.get("auction_change_pct")
    try:
        p = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        p = None
    bc = row.get("board_count")
    sig = row.get("signal")
    if p is None:
        return {
            "pct": None, "label": "—", "name": name, "code": code,
            "board_count": int(bc) if _is_num(bc) else None,
            "signal": sig,
        }
    if p > 2:
        label = f"强(+{p:.1f}%)"
    elif p > 0:
        label = f"红盘(+{p:.1f}%)"
    elif p == 0:
        label = "平盘"
    else:
        label = f"弱({p:+.1f}%)"
    return {
        "pct": p,
        "label": label,
        "name": name,
        "code": code,
        "board_count": int(bc) if _is_num(bc) else None,
        "signal": sig,
    }


def _board_count_row(row: dict | None) -> int:
    if not isinstance(row, dict):
        return 0
    bc = row.get("board_count")
    if _is_num(bc):
        return int(bc)
    return 0


def _yesterday_board_from_relay(relay: dict, code6: str) -> int | None:
    """复盘 relay：昨日空间板连板数（供看板「昨日连板高标」展示，非当日收盘板数）。"""
    if not relay or not code6 or len(code6) != 6:
        return None
    prev = relay.get("prev_space_board_today") or {}
    if str(prev.get("code") or "").strip().zfill(6) == code6:
        yb = prev.get("yesterday_board")
        if _is_num(yb):
            return int(yb)
    return None


def _apply_dashboard_yesterday_board(
    result: dict[str, Any], relay: dict, *, for_dashboard: bool
) -> dict[str, Any]:
    if not for_dashboard:
        return result
    code = str(result.get("code") or "").strip().zfill(6)
    yb = _yesterday_board_from_relay(relay, code)
    if yb is not None and yb > 0:
        return {**result, "board_count": yb}
    return result


def _main_board_leader_rows(le: dict | None) -> list[dict]:
    """合并 main_board_leaders + main_board_leader，按 code 去重保留更高 board。"""
    by_code: dict[str, dict] = {}

    def _upsert(row: dict | None) -> None:
        if not isinstance(row, dict):
            return
        raw = str(row.get("leader_code") or "").strip()
        if not raw.isdigit():
            return
        code6 = raw.zfill(6)
        if len(code6) != 6:
            return
        bc = _board_count_row(row)
        prev = by_code.get(code6)
        if prev is None or bc > _board_count_row(prev):
            by_code[code6] = row

    for row in (le or {}).get("main_board_leaders") or []:
        _upsert(row)
    _upsert((le or {}).get("main_board_leader"))
    return list(by_code.values())


def aggregate_top_tier_lianban_auction(
    rows: list[dict],
    *,
    source: str = "main_board_lianban",
) -> dict[str, Any] | None:
    """昨日最高连板档：多只同板取竞价均值；单只仍用该股。"""
    if not rows:
        return None
    max_bc = max(_board_count_row(r) for r in rows)
    if max_bc < 2:
        return None
    tier = [r for r in rows if _board_count_row(r) == max_bc]
    pcts: list[float] = []
    for r in tier:
        try:
            p = r.get("auction_change_pct")
            if p is not None:
                pcts.append(float(p))
        except (TypeError, ValueError):
            continue
    if not pcts:
        return None
    avg = round(sum(pcts) / len(pcts), 2)
    if len(tier) == 1:
        out = _leader_row_auction(tier[0])
        out["source"] = source
        out["tier_count"] = 1
        return out
    synthetic = {
        "leader_code": "",
        "leader_name": f"{len(tier)}只{max_bc}板",
        "board_count": max_bc,
        "auction_change_pct": avg,
        "signal": None,
    }
    out = _leader_row_auction(synthetic)
    out["source"] = f"{source}_avg"
    out["tier_count"] = len(tier)
    out["tier_codes"] = [
        str(r.get("leader_code") or "").strip().zfill(6) for r in tier
    ]
    return out


def highest_board_tier_from_leader_rows(rows: list[dict]) -> dict[str, Any] | None:
    """供 3进4 空间板门槛：最高连板档均竞价（与看板昨日连板高标一致）。"""
    agg = aggregate_top_tier_lianban_auction(rows, source="main_board_lianban")
    if not agg or agg.get("pct") is None:
        return None
    return {
        "yesterday_board": agg.get("board_count"),
        "count": int(agg.get("tier_count") or 1),
        "avg_today_pct": agg.get("pct"),
        "today_pct": agg.get("pct"),
        "name": agg.get("name") or "",
    }


def main_board_lianban_space_auction(
    leader: dict | None,
    *,
    for_dashboard: bool = False,
) -> dict[str, Any]:
    """昨日主板连板高标今日竞价：最高连板档内多只取均值；单只取该股。
    仅当无任何主板高标样本时才回退 market_leader（10 日市场高标）。"""
    le = leader or {}
    if for_dashboard:
        from src.engine.screener_market_env import load_prev_trading_day_review_document

        rd = load_prev_trading_day_review_document()
    else:
        rd = load_latest_review_document()
    relay = (rd.get("relay_env") or {}) if isinstance(rd, dict) else {}

    rows = _main_board_leader_rows(le)
    agg = aggregate_top_tier_lianban_auction(rows, source="main_board_lianban")
    if agg and agg.get("pct") is not None:
        return _apply_dashboard_yesterday_board(agg, relay, for_dashboard=for_dashboard)

    r2 = _leader_row_auction(le.get("market_leader") if isinstance(le.get("market_leader"), dict) else None)
    r2["source"] = "market_leader" if (r2.get("code") or r2.get("name")) else "none"
    return _apply_dashboard_yesterday_board(r2, relay, for_dashboard=for_dashboard)


def _caution_bits_text(ld_mb: int, drop9: Optional[float]) -> str:
    """谨慎触发条件摘要（用于决策 tagline）。"""
    parts: list[str] = []
    if ld_mb > 5:
        parts.append(f"主板竞价跌停{ld_mb}家（>5⚠）")
    if _is_num(drop9) and drop9 > 9:
        parts.append(f"跌幅>9%个股{int(drop9)}只（>9⚠）")
    return "、".join(parts)


def rebuild_dashboard_decision_from_participate(participate: dict[str, Any]) -> dict[str, Any]:
    """读侧/刷新后按 participate 七项重算决策树，使 tagline 与格子 b1/跌停/高标一致。"""
    p = participate or {}
    ld_mb = int(p.get("limit_down_main_board") or 0)
    drop9 = p.get("drop_over_9pct")
    try:
        drop9f = float(drop9) if drop9 is not None else None
    except (TypeError, ValueError):
        drop9f = None
    relay_idx = p.get("relay_decision_index")
    try:
        relay_f = float(relay_idx) if relay_idx is not None else None
    except (TypeError, ValueError):
        relay_f = None
    b1 = p.get("b1_rate")
    try:
        b1_f = float(b1) if b1 is not None else None
    except (TypeError, ValueError):
        b1_f = None
    pct = p.get("space_board_auction_pct")
    try:
        space_red = pct is not None and float(pct) > 0
    except (TypeError, ValueError):
        space_red = False
    return run_decision_tree_v2(ld_mb, relay_f, b1_f, space_red, drop_over_9pct=drop9f)


def _apply_space_board_to_participate(part: dict[str, Any], sp: dict[str, Any]) -> None:
    if not isinstance(part, dict) or not isinstance(sp, dict):
        return
    for k, pk in (
        ("pct", "space_board_auction_pct"),
        ("label", "space_board_label"),
        ("name", "space_board_name"),
        ("code", "space_board_code"),
        ("board_count", "space_board_board_count"),
        ("signal", "space_board_signal"),
        ("source", "space_board_source"),
    ):
        if sp.get(k) is not None or pk not in part:
            part[pk] = sp.get(k)


def run_decision_tree_v2(
    ld_mb: int,
    relay_idx: Optional[float],
    b1: Optional[float],
    space_red: bool,
    drop_over_9pct: Optional[float] = None,
) -> dict[str, Any]:
    """决策树：主板竞价跌停(>5⚠)或跌>9%个股(>9⚠)、加权接力≤0、1进2、连板高标红盘 → 层数与文案。"""
    relay_bad = relay_idx is None or relay_idx <= 0
    b1_ok = b1 is not None and b1 >= 15.0
    caution_market = ld_mb > 5 or (_is_num(drop_over_9pct) and drop_over_9pct > 9)
    cwarn = _caution_bits_text(ld_mb, drop_over_9pct)

    final_layer = 3.0
    headline = "🟢 正常参与"
    conclusion = "🟢 正常仓位（3层）"
    tagline = ""
    mode = "正常"

    if caution_market:
        mode = "谨慎模式"
        if relay_bad:
            final_layer = 0.0
            headline = "⚠️ 谨慎参与 → 空仓"
            conclusion = "⛔ 空仓"
            tagline = (
                f"{cwarn}，且加权接力情绪指数"
                f"{'—' if relay_idx is None else f'{relay_idx:.2f}%'}（≤0%），强制空仓。"
            )
        else:
            final_layer = 1.5
            headline = "⚠️ 谨慎参与"
            conclusion = "⚠️ 谨慎参与（小仓试错）"
            tagline = f"{cwarn}，进入谨慎模式，仓位上限 1.5 层，仅建议做 3 进 4+。"
    elif relay_bad:
        final_layer = 0.0
        mode = "空仓"
        headline = "⛔ 空仓"
        conclusion = "⛔ 空仓"
        tagline = (
            f"加权接力情绪指数"
            f"{'—' if relay_idx is None else f'{relay_idx:.2f}%'}（≤0%），触发空仓条件。等待该指标转正再开仓。"
        )
    else:
        # 接力>0%：1进2晋级率 × 高标竞价 → 8 档仓位表（你改的新规则）
        # 格式：(b1_min, b1_max, space_red) → (final_layer, headline, conclusion, cap_hint)
        b1_val = b1 if b1 is not None else 0.0
        if b1_val >= 15.0:
            if space_red:
                final_layer, headline, conclusion = 1.5, "🟡 轻仓参与", "🟡 轻仓参与（1.5层）"
                cap_hint = "（情绪过热，即使高标红盘也只给轻仓）"
            else:
                final_layer, headline, conclusion = 1.0, "⚠️ 谨慎参与", "⚠️ 谨慎参与（小仓试错）"
                cap_hint = "（情绪过热 + 高标弱 → 进一步降仓）"
        elif b1_val >= 12.0:
            if space_red:
                final_layer, headline, conclusion = 3.0, "🟢 正常参与", "🟢 正常仓位（3层）"
                cap_hint = "（最佳窗口 + 高标红盘 → 正常仓）"
            else:
                final_layer, headline, conclusion = 2.0, "🟡 轻仓参与", "🟡 轻仓参与（2层）"
                cap_hint = "（最佳窗口但高标弱 → 微降）"
        elif b1_val >= 8.0:
            if space_red:
                final_layer, headline, conclusion = 1.5, "🟡 轻仓参与", "🟡 轻仓参与（1.5层）"
                cap_hint = "（弱势但可参与）"
            else:
                final_layer, headline, conclusion = 1.0, "⚠️ 谨慎参与", "⚠️ 谨慎参与（小仓试错）"
                cap_hint = "（弱势 + 高标弱 → 轻仓）"
        else:
            final_layer, headline, conclusion = 0.0, "⛔ 空仓", "⛔ 空仓"
            cap_hint = "（极弱，不开仓）"
        rtxt = f"{relay_idx:.2f}%" if relay_idx is not None else "—"
        btxt = f"{b1:.1f}%" if b1 is not None else "—"
        if b1_val >= 15.0:
            tier_focus = "2进3"
        else:
            tier_focus = "3进4+"
        if final_layer == 0:
            tagline = (
                f"加权接力情绪指数 {rtxt}（>0%），1进2成功率 {btxt}%（<8%→极弱），"
                f"触发空仓条件。{cap_hint}"
            )
        else:
            tagline = (
                f"加权接力情绪指数 {rtxt}（>0%），1进2成功率 {btxt}%，主攻{tier_focus}，"
                f"仓位 {final_layer:g} 层。关注昨日连板高标竞价{'红盘' if space_red else '绿盘'}。"
                f"{cap_hint}"
            )

    bucket = "stop" if final_layer == 0 else ("warn" if final_layer < 3 else "go")
    pos = "0 层" if final_layer == 0 else (f"{final_layer:g} 层")
    if final_layer == 0:
        pos_short = "0层"
    elif final_layer == 1.5:
        pos_short = "1.5层"
    elif final_layer == 1.0:
        pos_short = "1层"
    else:
        pos_short = f"{int(final_layer)}层"

    return {
        "mode": mode,
        "final_cap_layer": final_layer,
        "headline": headline,
        "conclusion": conclusion,
        "tagline": tagline,
        "bucket": bucket,
        "position": pos,
        "position_short": pos_short,
    }


def _maybe_widen_spot_df(spot_df: pd.DataFrame | None) -> pd.DataFrame | None:
    """局部 spot 不足以覆盖 zb/跌停样本时，换新浪全市场。"""
    if spot_df is None or getattr(spot_df, "empty", True):
        return spot_df
    try:
        from src.data.sina_spot_api import fetch_a_share_list_sina

        full = fetch_a_share_list_sina()
        if full is not None and not full.empty and len(full) > len(spot_df):
            return full
    except Exception:
        pass
    return spot_df


def resolve_auction_market_for_dashboard(market: dict | None) -> dict[str, Any]:
    """09:15 前展示上一交易日竞价跌停/跌>9% 落盘；09:15 后用当日 market。"""
    from src.data.analytics_store import load_prev_trading_day_sentiment_market
    from src.market_schedule import is_before_trading_auction_open

    if not is_before_trading_auction_open():
        return dict(market or {})
    pm, as_of = load_prev_trading_day_sentiment_market()
    if not pm:
        return dict(market or {})
    out = dict(pm)
    out["auction_market_as_of"] = as_of
    out["auction_market_source"] = "prev_trading_day"
    return out


def _apply_persisted_sentiment_fallbacks(sent: dict[str, Any]) -> dict[str, Any]:
    """spot 算不出跌停列表/接力时，用库内 latest_sentiment 已落盘字段补缺（避免盘外刷新写空）。"""
    out = dict(sent) if sent else {}
    try:
        persisted = load_latest_sentiment_document() or {}
    except Exception:
        return out
    if not out.get("relay_sentiment_index") and persisted.get("relay_sentiment_index"):
        out["relay_sentiment_index"] = persisted["relay_sentiment_index"]
    pm = persisted.get("market") if isinstance(persisted.get("market"), dict) else {}
    sm = dict(out.get("market") or {})
    if pm:
        if not sm.get("limit_down_list") and pm.get("limit_down_list"):
            sm = {**pm, **sm}
        else:
            for key in ("limit_down", "drop_over_9pct", "limit_down_main_board", "limit_up_flat"):
                if sm.get(key) is None and pm.get(key) is not None:
                    sm[key] = pm[key]
        out["market"] = sm
    return out


def build_dashboard_payload(sent: dict | None, leader: dict | None, spot_df: pd.DataFrame | None) -> dict[str, Any]:
    """聚合参与/参考指标 + 决策树结果（写入 latest_advice['dashboard']）。"""
    sent = _apply_persisted_sentiment_fallbacks(sent or {})
    spot_work = spot_df
    if spot_work is not None and not getattr(spot_work, "empty", True):
        try:
            from src.engine.advice_snapshot_hydrate import merge_spot_market_into_sentiment

            sent = merge_spot_market_into_sentiment(sent, spot_work)
        except Exception:
            pass
    market = resolve_auction_market_for_dashboard(sent.get("market") or {})
    ld_mb = count_main_board_auction_limit_down(market)
    drop9 = market.get("drop_over_9pct")
    try:
        drop9f = float(drop9) if drop9 is not None else None
    except (TypeError, ValueError):
        drop9f = None

    relay = compute_yesterday_main_board_relay_decision_index(spot_work)
    if relay is None and spot_work is not None and not getattr(spot_work, "empty", True):
        spot_work = _maybe_widen_spot_df(spot_work)
        relay = compute_yesterday_main_board_relay_decision_index(spot_work)

    relay_idx = relay.get("index") if relay else None
    if relay_idx is None:
        rsi = sent.get("relay_sentiment_index") or {}
        if isinstance(rsi, dict) and _is_num(rsi.get("index")):
            relay_idx = float(rsi["index"])
            relay = relay or dict(rsi)

    b1 = _b1_rate_from_review()
    sp = main_board_lianban_space_auction(leader, for_dashboard=True)
    if sp.get("pct") is None and spot_work is not None and not getattr(spot_work, "empty", True):
        c_try = str(sp.get("code") or "").strip()
        tier_codes = sp.get("tier_codes") or []
        if not c_try and not tier_codes:
            rd0 = load_latest_review_document()
            relay0 = (rd0.get("relay_env") or {}) if isinstance(rd0, dict) else {}
            prev0 = relay0.get("prev_space_board_today") or relay0.get("space_board") or {}
            c_try = str(prev0.get("code") or "").strip().zfill(6)
            if c_try and len(c_try) == 6:
                sp = {
                    **sp,
                    "code": c_try,
                    "name": sp.get("name") or str(prev0.get("name") or ""),
                    "board_count": sp.get("board_count") or prev0.get("yesterday_board") or prev0.get("board_count"),
                }
    if spot_work is not None and not getattr(spot_work, "empty", True):
        sp = refresh_space_auction_from_spot(sp, spot_work, prefer_spot=True)
        if sp.get("pct") is None:
            c_try = str(sp.get("code") or "").strip().zfill(6)
            if len(c_try) == 6:
                try:
                    from src.data.fetcher import fetch_realtime_batch

                    fb = fetch_realtime_batch([c_try])
                    sp = refresh_space_auction_from_spot(sp, fb, prefer_spot=True)
                except Exception:
                    pass
    space_red = sp.get("pct") is not None and float(sp["pct"]) > 0

    tree = run_decision_tree_v2(ld_mb, relay_idx, b1, space_red, drop_over_9pct=drop9f)

    w10 = sent.get("weighted_auction_gain")
    if w10 is None:
        try:
            sf = load_latest_sentiment_document()
            wg = sf.get("weighted_auction_gain")
            if wg is not None:
                w10 = float(wg)
        except (TypeError, ValueError, Exception):
            pass

    le = leader or {}
    y_ld = (le.get("yesterday_limit_down_today_auction") or {})
    y_zb = (le.get("yesterday_zb_today_auction") or {})

    if spot_work is not None and not getattr(spot_work, "empty", True):
        if y_ld.get("avg_change_pct") is None:
            try:
                y_ld = compute_yesterday_limit_down_today_auction(spot_work) or y_ld
            except Exception:
                pass
        if y_zb.get("avg_change_pct") is None:
            try:
                zb_try = compute_yesterday_zb_today_auction(spot_work)
                if zb_try is None or (zb_try.get("sample_count") or 0) == 0:
                    wide = _maybe_widen_spot_df(spot_work)
                    if wide is not None and wide is not spot_work:
                        zb_try = compute_yesterday_zb_today_auction(wide)
                        spot_work = wide
                if zb_try:
                    y_zb = zb_try
            except Exception:
                pass

    if w10 is None and spot_work is not None and not getattr(spot_work, "empty", True):
        try:
            from src.engine.sentiment_pool import compute_pool_sentiment

            rd0 = load_latest_ranking_document()
            ranks0 = rd0.get("ranking") or []
            pool_codes = [_norm_code6(str(x.get("code") or "")) for x in ranks0[:30]]
            pool_codes = [c for c in pool_codes if len(c) == 6]
            if pool_codes:
                ps = compute_pool_sentiment(pool_codes, spot_work)
                if ps is not None and ps.weighted_auction_gain is not None:
                    w10 = float(ps.weighted_auction_gain)
        except Exception:
            pass

    rsi_live = sent.get("relay_sentiment_index") or {}
    relay_display_idx = None
    relay_display_prev = None
    relay_display_detail = None
    if isinstance(rsi_live, dict) and _is_num(rsi_live.get("index")):
        relay_display_idx = float(rsi_live["index"])
        relay_display_detail = dict(rsi_live)
        if _is_num(rsi_live.get("prev_index")):
            relay_display_prev = float(rsi_live["prev_index"])
    elif spot_work is not None and not getattr(spot_work, "empty", True):
        try:
            from src.engine.sentiment_pool import compute_relay_sentiment_index

            rsi_calc = compute_relay_sentiment_index(spot_work)
            if rsi_calc:
                relay_display_idx = rsi_calc.get("index")
                relay_display_detail = rsi_calc
                from src.engine.sentiment_pool import _get_prev_day_relay_sentiment_index

                prev_r = _get_prev_day_relay_sentiment_index(
                    str(sent.get("date") or now_cn().strftime("%Y-%m-%d %H:%M:%S"))
                )
                if _is_num(prev_r):
                    relay_display_prev = float(prev_r)
        except Exception:
            pass
    if relay_display_idx is None:
        relay_display_idx = relay_idx
        relay_display_detail = relay
    prev_ld_all = market.get("prev_day_limit_down")
    try:
        prev_ld_all_i = int(prev_ld_all) if prev_ld_all is not None else None
    except (TypeError, ValueError):
        prev_ld_all_i = None
    prev_w10 = sent.get("prev_day_weighted_auction_gain")
    try:
        prev_w10_f = float(prev_w10) if prev_w10 is not None else None
    except (TypeError, ValueError):
        prev_w10_f = None

    participate = {
        "limit_down_main_board": ld_mb,
        "limit_down_all": market.get("limit_down"),
        "drop_over_9pct": drop9f,
        "prev_day_limit_down_all": prev_ld_all_i,
        "auction_market_as_of": market.get("auction_market_as_of"),
        "relay_decision_index": relay_display_idx,
        "relay_decision_prev_index": relay_display_prev,
        "relay_decision_detail": relay_display_detail,
        "b1_rate": b1,
        "b1_review_date": _b1_review_date_iso(),
        "space_board_auction_pct": sp.get("pct"),
        "space_board_label": sp.get("label"),
        "space_board_name": sp.get("name"),
        "space_board_code": sp.get("code"),
        "space_board_board_count": sp.get("board_count"),
        "space_board_signal": sp.get("signal"),
        "space_board_source": sp.get("source"),
    }
    reference = {
        "yesterday_limit_down_avg": y_ld.get("avg_change_pct"),
        "pool_weighted_auction_top30": w10,
        "prev_pool_weighted_auction_top30": prev_w10_f,
        "yesterday_zb_avg": y_zb.get("avg_change_pct"),
        "yesterday_zb_sample_count": y_zb.get("sample_count"),
    }
    return {
        "participate": participate,
        "reference": reference,
        "decision": tree,
    }


def calc_daily_advice_v2(sent: dict | None, leader: dict | None, spot_df: pd.DataFrame | None) -> dict[str, Any]:
    """供 write_advice_snapshot 使用：返回 bucket/text/position + dashboard。"""
    dash = build_dashboard_payload(sent, leader, spot_df)
    d = dash["decision"]
    return {
        "bucket": d["bucket"],
        "text": d["headline"],
        "position": d["position"],
        "position_short": d["position_short"],
        "reason": d["tagline"],
        "dashboard": dash,
    }

"""决策快照写入前的数据补全：用当日全市场 spot 补 market，读库替代已删的 latest_*.json。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd


def load_sentiment_for_advice() -> dict[str, Any]:
    from src.data.analytics_store import load_latest_sentiment_document

    return load_latest_sentiment_document()


def load_leader_for_advice() -> dict[str, Any]:
    from src.data.analytics_store import load_latest_leader_document

    return load_latest_leader_document()


def merge_spot_market_into_sentiment(
    sent: dict[str, Any],
    spot_df: pd.DataFrame | None,
    *,
    market_stats=None,
    skip_recompute: bool = False,
) -> dict[str, Any]:
    """用 compute_market_auction_stats 覆盖 sent['market'] 中与竞价相关的字段（9:27 口径）。

    market_stats: 若调度器已算过，直接复用，避免二次全市场统计 + 一字涨停富化。
    skip_recompute: True 且 market 已含 limit_down 时跳过重算。
    """
    if spot_df is None or getattr(spot_df, "empty", True):
        if market_stats is None:
            return sent

    from src.engine.dashboard_decision import count_main_board_auction_limit_down

    out: dict[str, Any] = dict(sent) if sent else {}
    ex_m = dict(out.get("market") or {})

    if skip_recompute and ex_m.get("limit_down") is not None and market_stats is None:
        if "limit_down_main_board" not in ex_m:
            ex_m["limit_down_main_board"] = count_main_board_auction_limit_down(ex_m)
            out["market"] = ex_m
        return out

    stats = market_stats
    if stats is None:
        from src.engine.sentiment_pool import compute_market_auction_stats

        stats = compute_market_auction_stats(spot_df)
    if stats is None:
        return sent

    md = asdict(stats) if not isinstance(stats, dict) else stats
    ex_m.update(
        {
            "limit_down": md.get("limit_down"),
            "drop_over_9pct": md.get("drop_over_9pct"),
            "limit_down_list": md.get("limit_down_list") or [],
            "limit_up_flat": md.get("limit_up_flat"),
            "limit_up_flat_list": md.get("limit_up_flat_list") or [],
        }
    )
    ex_m["limit_down_main_board"] = count_main_board_auction_limit_down(ex_m)
    pdl = md.get("prev_day_limit_down")
    if pdl is not None:
        ex_m["prev_day_limit_down"] = pdl
    out["market"] = ex_m
    return out

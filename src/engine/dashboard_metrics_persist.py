"""刷新决策后，将看板 v2 七项指标写回 daily_sentiment / latest_leader / latest_review 明细表。"""
from __future__ import annotations

import copy
from typing import Any

import pandas as pd

from src.config import now_cn


def _patch_sentiment_from_dashboard(sent: dict[str, Any], dashboard: dict[str, Any]) -> dict[str, Any]:
    part = dashboard.get("participate") or {}
    ref = dashboard.get("reference") or {}
    # 仅保留 save_sentiment 认识的字段，避免 _load 时 update(extra) 污染整型列
    _KEEP = (
        "date", "pool_size", "avg_auction_gain", "weighted_auction_gain",
        "high_open", "flat_open", "low_open", "limit_down", "limit_up_flat",
        "verdict", "reason", "market", "relay_sentiment_index",
        "prev_day_weighted_auction_gain",
    )
    base = sent or {}
    out: dict[str, Any] = {k: base[k] for k in _KEEP if k in base}

    mkt = dict(out.get("market") or {})
    if part.get("limit_down_main_board") is not None:
        mkt["limit_down_main_board"] = part.get("limit_down_main_board")
    if part.get("limit_down_all") is not None:
        mkt["limit_down"] = part.get("limit_down_all")
    if part.get("drop_over_9pct") is not None:
        mkt["drop_over_9pct"] = part.get("drop_over_9pct")
    out["market"] = mkt

    if ref.get("pool_weighted_auction_top30") is not None:
        out["weighted_auction_gain"] = ref.get("pool_weighted_auction_top30")
    if part.get("limit_down_all") is not None:
        out["limit_down"] = part.get("limit_down_all")

    relay = part.get("relay_decision_detail")
    if isinstance(relay, dict) and relay:
        out["relay_sentiment_index"] = relay

    out["dashboard_v2"] = {
        "participate": part,
        "reference": ref,
        "synced_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not out.get("date"):
        out["date"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    return out


def _patch_leader_from_dashboard(
    leader: dict[str, Any],
    dashboard: dict[str, Any],
    spot_df: pd.DataFrame | None,
) -> dict[str, Any]:
    from src.engine.leader_feedback import (
        compute_yesterday_limit_down_today_auction,
        compute_yesterday_zb_today_auction,
    )

    out = copy.deepcopy(leader) if leader else {}
    part = dashboard.get("participate") or {}

    ref = dashboard.get("reference") or {}
    if spot_df is not None and not getattr(spot_df, "empty", True):
        try:
            y_ld = compute_yesterday_limit_down_today_auction(spot_df)
            if y_ld:
                out["yesterday_limit_down_today_auction"] = y_ld
        except Exception as e:
            print(f"[看板回写] leader 昨日跌停反馈失败: {e}")
        try:
            y_zb = compute_yesterday_zb_today_auction(spot_df)
            if y_zb:
                out["yesterday_zb_today_auction"] = y_zb
        except Exception as e:
            print(f"[看板回写] leader 昨日炸板反馈失败: {e}")
    else:
        if ref.get("yesterday_limit_down_avg") is not None:
            out["yesterday_limit_down_today_auction"] = {
                "avg_change_pct": ref.get("yesterday_limit_down_avg"),
                "sample_count": 1,
                "pool_size": 1,
            }
        if ref.get("yesterday_zb_avg") is not None:
            out["yesterday_zb_today_auction"] = {
                "avg_change_pct": ref.get("yesterday_zb_avg"),
                "sample_count": 1,
                "pool_size": 1,
            }

    code = str(part.get("space_board_code") or "").strip().zfill(6)
    if len(code) == 6 and code.isdigit():
        row = {
            "leader_code": code,
            "leader_name": str(part.get("space_board_name") or ""),
            "auction_change_pct": part.get("space_board_auction_pct"),
            "board_count": part.get("space_board_board_count"),
            "signal": part.get("space_board_signal"),
        }
        out["main_board_leader"] = row
        mbs = list(out.get("main_board_leaders") or [])
        replaced = False
        for i, x in enumerate(mbs):
            if str(x.get("leader_code") or "").zfill(6) == code:
                mbs[i] = {**x, **row}
                replaced = True
                break
        if not replaced:
            mbs.append(row)
        out["main_board_leaders"] = mbs

    out["date"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    out["dashboard_v2"] = {
        "participate": part,
        "reference": dashboard.get("reference") or {},
        "synced_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return out


def persist_dashboard_v2_to_detail_tables(
    sent: dict[str, Any] | None,
    leader: dict[str, Any] | None,
    dashboard: dict[str, Any] | None,
    spot_df: pd.DataFrame | None = None,
) -> None:
    """将 dashboard.participate / reference 写回结构化表（与 daily_advice.dashboard_json 同源）。"""
    if not dashboard or not isinstance(dashboard, dict):
        return
    from src.data.analytics_store import (
        save_from_latest_filename,
        upsert_daily_json_snapshot,
    )

    try:
        sent_doc = _patch_sentiment_from_dashboard(sent or {}, dashboard)
        save_from_latest_filename("latest_sentiment.json", sent_doc)
        print("[看板回写] daily_sentiment 已更新（含 market / weighted / relay / dashboard_v2）")
    except Exception as e:
        print(f"[看板回写] daily_sentiment 失败: {e}")

    try:
        leader_doc = _patch_leader_from_dashboard(leader or {}, dashboard, spot_df)
        upsert_daily_json_snapshot("latest_leader.json", leader_doc)
        print("[看板回写] latest_leader 已更新（含昨日跌停/炸板反馈、空间板竞价）")
    except Exception as e:
        print(f"[看板回写] latest_leader 失败: {e}")


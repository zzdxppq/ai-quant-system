"""定时任务调度器

- 收盘后（15:30）：更新10日涨幅排行 + 周期状态
- 早盘（9:27）：执行选股 + 交叉验证 + 邮件推送
- 数据源：新浪财经(实时) + 东方财富(K线/排行) → AKShare(兜底) → Mock(降级)
"""
import json
import os
from dataclasses import asdict
from datetime import datetime

import pandas as pd

from src.config import DATA_DIR
from src.engine.cycle import CycleEngine, calc_gain_10d
from src.engine.screener import run_screener
from src.engine.cross_validator import cross_validate


def _fetch_ranking() -> pd.DataFrame:
    """获取10日涨幅排行（全市场 top30，过滤 ST+新股，含富化字段）"""
    from src.data.fetcher import fetch_gain_10d_ranking
    return fetch_gain_10d_ranking(top_n=30)


def _fetch_screener_data():
    """获取选股所需数据"""
    from src.data.fetcher import fetch_realtime_spot, fetch_limit_up_history
    return fetch_realtime_spot(), fetch_limit_up_history(days=5)


def run_ranking_refresh() -> dict:
    """仅刷新10日涨幅排行（不触发周期状态机更新）

    用于盘中 10:00 拉取最新排行数据，使实时刷新的基准更贴近当天盘面。
    """
    print("=" * 50)
    print(f"[{datetime.now()}] 盘中排行刷新...")

    ranking_df = _fetch_ranking()
    ranking_data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ranking": ranking_df.to_dict("records"),
    }
    (DATA_DIR / "latest_ranking.json").write_text(
        json.dumps(ranking_data, ensure_ascii=False, indent=2)
    )

    print(f"排行刷新完成: {len(ranking_df)} 只")
    print("=" * 50)
    return ranking_data


def run_cycle_update() -> dict:
    """更新周期状态（收盘后调用）

    1. 拉取全市场实时行情
    2. 计算10日涨幅排行
    3. 更新周期状态机
    4. 保存快照
    """
    print("=" * 50)
    print(f"[{datetime.now()}] 开始周期更新...")

    # 1-2. 获取排行数据
    ranking_df = _fetch_ranking()

    # 保存排行（已是 top30，不再截断）
    ranking_data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ranking": ranking_df.to_dict("records"),
    }
    (DATA_DIR / "latest_ranking.json").write_text(
        json.dumps(ranking_data, ensure_ascii=False, indent=2)
    )

    # 3. 更新周期状态
    engine = CycleEngine()
    snapshot = engine.update(ranking_df)

    # 4. 保存快照
    snapshot_dict = {
        "date": snapshot.date,
        "phase": snapshot.phase,
        "phase_day": snapshot.phase_day,
        "representative": snapshot.representative,
        "candidates": snapshot.candidates,
        "prev_cycle": snapshot.prev_cycle,
        "notes": snapshot.notes,
    }
    (DATA_DIR / "latest_snapshot.json").write_text(
        json.dumps(snapshot_dict, ensure_ascii=False, indent=2)
    )

    # 追加到历史时间线
    _append_history(snapshot_dict)

    print(f"周期状态: {snapshot.phase} (第{snapshot.phase_day}天)")
    if snapshot.representative:
        rep = snapshot.representative
        print(f"代表股: {rep['name']}({rep['code']}) 10日涨幅:{rep['gain_10d']}%")
    print("=" * 50)

    return snapshot_dict


def run_screener_update() -> dict:
    """执行选股（9:27调用）

    1. 拉取实时竞价数据
    2. 获取涨停历史（连板检测）
    3. 高标龙头竞价反馈（仅作当日操作建议，不过滤选股结果）
    4. 执行选股筛选
    5. 交叉验证（结合龙头反馈微调仓位建议）
    """
    print("=" * 50)
    print(f"[{datetime.now()}] 开始选股...")

    # 1-2. 获取选股数据
    spot_df, limit_up_hist = _fetch_screener_data()

    # 3. 加载周期状态获取候选股
    snapshot_file = DATA_DIR / "latest_snapshot.json"
    cycle_codes = []
    cycle_snapshot = None
    if snapshot_file.exists():
        snapshot_data = json.loads(snapshot_file.read_text())
        cycle_snapshot = snapshot_data
        if snapshot_data.get("representative"):
            cycle_codes.append(snapshot_data["representative"]["code"])
        for c in snapshot_data.get("candidates", []):
            cycle_codes.append(c["code"])

    # 4. 高标龙头竞价反馈
    from src.engine.leader_feedback import (
        evaluate_leader, find_leader_from_snapshot, LeaderFeedback, LeaderSignal
    )
    leader_fb = None
    if cycle_snapshot:
        leader_info = find_leader_from_snapshot(cycle_snapshot)
        if leader_info and not spot_df.empty:
            code, name, gain_10d = leader_info
            leader_fb = evaluate_leader(code, name, gain_10d, spot_df)
            print(f"高标龙头: {leader_fb.leader_name} 竞价{leader_fb.auction_change_pct:+.1f}% → {leader_fb.signal.value}")
            print(f"  {leader_fb.reason}")

            # 保存龙头反馈
            leader_data = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "leader_code": leader_fb.leader_code,
                "leader_name": leader_fb.leader_name,
                "leader_gain_10d": leader_fb.leader_gain_10d,
                "auction_change_pct": leader_fb.auction_change_pct,
                "signal": leader_fb.signal.value,
                "can_trade": leader_fb.can_trade,
                "aggression": leader_fb.aggression,
                "reason": leader_fb.reason,
            }
            (DATA_DIR / "latest_leader.json").write_text(
                json.dumps(leader_data, ensure_ascii=False, indent=2)
            )

    # 4.5 梯队情绪池：用 top30 龙头池的竞价分布衡量今日接力意愿
    pool_sent = None
    try:
        from src.engine.sentiment_pool import (
            compute_pool_sentiment, load_pool_from_ranking, save_sentiment,
        )
        pool_codes = load_pool_from_ranking()
        if pool_codes:
            pool_sent = compute_pool_sentiment(pool_codes, spot_df)
            if pool_sent:
                save_sentiment(pool_sent)
                print(f"梯队情绪: {pool_sent.verdict} · {pool_sent.reason}")
            else:
                print("梯队情绪: 无有效样本")
        else:
            print("梯队情绪: latest_ranking.json 池为空，跳过")
    except Exception as e:
        print(f"梯队情绪计算失败: {e}")

    # 5. 执行选股
    hits = run_screener(spot_df, limit_up_hist, cycle_codes)

    # 5.1 腾讯接口富化：补全 market_cap / volume_ratio / turnover 并做严格二次过滤
    # 必要性：sina spot 源缺这些字段，screener 用了软过滤放行；此处用腾讯补齐后按通达信公式严格筛
    if hits:
        from src.config import SCREENER_CONFIG
        from src.data.tencent_api import enrich_screener_hits
        hits = enrich_screener_hits(
            hits,
            market_cap_max=SCREENER_CONFIG["market_cap_max"],
            volume_ratio_min=SCREENER_CONFIG["volume_ratio_min"],
        )

    # 5.5 240周线偏离度过滤
    from src.engine.ma_deviation import batch_check_deviation
    if hits:
        deviation_stocks = [
            {"code": h.code, "name": h.name, "current_price": float(
                spot_df[spot_df["code"].astype(str) == str(h.code)]["close"].values[0]
            ) if not spot_df[spot_df["code"].astype(str) == str(h.code)].empty else 0}
            for h in hits
        ]
        deviations = batch_check_deviation(deviation_stocks)
        deviation_map = {d.code: d for d in deviations}

        # 保存偏离度数据
        deviation_data = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "results": [
                {
                    "code": d.code, "name": d.name,
                    "current_price": d.current_price, "ma240w": d.ma240w,
                    "deviation_pct": d.deviation_pct, "is_excessive": d.is_excessive,
                }
                for d in deviations
            ],
        }
        (DATA_DIR / "latest_deviation.json").write_text(
            json.dumps(deviation_data, ensure_ascii=False, indent=2)
        )

        # 标记偏离过大的标的
        for hit in hits:
            dev = deviation_map.get(hit.code)
            if dev and dev.is_excessive:
                print(f"  ⚠️ {hit.name}({hit.code}) 偏离240周线 {dev.deviation_pct:.1f}%，偏离过大")
    else:
        deviation_map = {}

    # 6. 保存选股结果
    hits_data = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "hits": [asdict(h) for h in hits],
    }
    (DATA_DIR / "latest_screener.json").write_text(
        json.dumps(hits_data, ensure_ascii=False, indent=2)
    )

    # 7. 交叉验证（传入龙头反馈）
    if cycle_snapshot and hits:
        from src.engine.cycle import CycleSnapshot as CS
        cs = CS(
            date=cycle_snapshot.get("date", ""),
            phase=cycle_snapshot.get("phase", "孕育期"),
            phase_day=cycle_snapshot.get("phase_day", 0),
            representative=cycle_snapshot.get("representative"),
            candidates=cycle_snapshot.get("candidates", []),
            prev_cycle=cycle_snapshot.get("prev_cycle"),
        )
        signals = cross_validate(cs, hits, leader_fb, pool_sent)
        signals_data = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "cycle_phase": cycle_snapshot.get("phase", "孕育期"),
            "leader_signal": leader_fb.signal.value if leader_fb else None,
            "leader_can_trade": leader_fb.can_trade if leader_fb else None,
            "signals": [asdict(s) for s in signals],
        }
    else:
        signals_data = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "cycle_phase": cycle_snapshot.get("phase", "孕育期") if cycle_snapshot else "孕育期",
            "signals": [],
        }

    (DATA_DIR / "latest_signals.json").write_text(
        json.dumps(signals_data, ensure_ascii=False, indent=2)
    )

    hit_count = len(hits)
    matched = sum(1 for h in hits if h.matched_cycle)
    print(f"选股结果: {hit_count} 只命中, {matched} 只匹配周期")
    for h in hits:
        flag = " 🎯" if h.matched_cycle else ""
        print(f"  {h.code} {h.name} {h.continuous_limit_up}板 竞价{h.auction_gain}%{flag}")

    # 8. 邮件推送
    try:
        from src.notify.email_sender import send_screener_report
        leader_data = None
        leader_file = DATA_DIR / "latest_leader.json"
        if leader_file.exists():
            leader_data = json.loads(leader_file.read_text())

        dev_data = None
        dev_file = DATA_DIR / "latest_deviation.json"
        if dev_file.exists():
            dev_data = json.loads(dev_file.read_text()).get("results")

        send_screener_report(
            cycle_phase=cycle_snapshot.get("phase", "孕育期") if cycle_snapshot else "孕育期",
            cycle_day=cycle_snapshot.get("phase_day", 0) if cycle_snapshot else 0,
            representative=cycle_snapshot.get("representative") if cycle_snapshot else None,
            leader=leader_data,
            hits=[asdict(h) for h in hits],
            signals=signals_data.get("signals", []),
            deviations=dev_data,
        )
    except Exception as e:
        print(f"[邮件] 推送异常: {e}")

    print("=" * 50)

    return hits_data



def _append_history(snapshot: dict):
    """追加快照到历史时间线"""
    history_file = DATA_DIR / "cycle_history.json"
    history = []
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
        except Exception:
            pass

    # 避免同一天重复
    today = snapshot.get("date", "")
    history = [h for h in history if h.get("date") != today]
    history.append(snapshot)

    # 只保留最近60天
    history = history[-60:]

    history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2))

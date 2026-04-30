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

from src.config import DATA_DIR, now_cn
from src.engine.cycle import CycleEngine, calc_gain_10d
from src.engine.screener import run_screener
from src.engine.cross_validator import cross_validate


def _fetch_ranking() -> pd.DataFrame:
    """获取10日涨幅排行（全市场 top30，过滤 ST+新股，含富化字段）"""
    from src.data.fetcher import fetch_gain_10d_ranking
    return fetch_gain_10d_ranking(top_n=30)


def _fetch_screener_data():
    """获取选股所需数据

    注意：先拉 spot 再拉涨停历史，涨停历史内部也需要 spot 时复用缓存，
    避免短时间内多次拉全市场行情触发限流。
    """
    from src.data.fetcher import fetch_realtime_spot, fetch_limit_up_history

    # 1. 先拿全市场实时行情
    spot_df = fetch_realtime_spot()

    # 2. 拉涨停历史（内部会再拉 spot，但有缓存/去重）
    limit_up_hist = fetch_limit_up_history(days=5)

    # 3. 如果 spot 拿到的是 mock（<100只），再试一次新浪
    if len(spot_df) < 100:
        print(f"  spot 只有 {len(spot_df)} 只，疑似 mock，重试新浪...")
        try:
            from src.data.sina_spot_api import fetch_a_share_list_sina
            df = fetch_a_share_list_sina()
            if not df.empty and len(df) > 100:
                spot_df = df
                print(f"  新浪重试成功: {len(spot_df)} 只")
        except Exception as e:
            print(f"  新浪重试失败: {e}")

    return spot_df, limit_up_hist


def _find_leader_from_ranking(ranking_file: str, main_board_only: bool = False):
    """从排行数据中找高标龙头

    Args:
        ranking_file: latest_ranking.json 路径
        main_board_only: True=只找主板
    Returns:
        (code, name, gain_10d) or None
    """
    from pathlib import Path
    path = Path(ranking_file)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    for item in data.get("ranking", []):
        code = str(item.get("code", ""))
        if main_board_only and not item.get("is_main_board", False):
            continue
        return code, item.get("name", ""), item.get("gain_10d", 0)
    return None


def _run_market_insight(ranking_records: list[dict]) -> None:
    """运行四维市场洞察分析"""
    try:
        from src.engine.market_insight import (
            analyze_market_insight, save_insight, load_prev_ranking,
        )
        prev = load_prev_ranking()
        insight = analyze_market_insight(ranking_records, prev)
        save_insight(insight)
        print(f"[市场洞察] 波形={insight.wave.wave_phase}({insight.wave.intensity:.0f}) · "
              f"板块集中度{insight.sector_concentration}% · {insight.capital_summary}")
    except Exception as e:
        print(f"[市场洞察] 分析失败: {e}")


def run_ranking_refresh() -> dict:
    """仅刷新10日涨幅排行（不触发周期状态机更新）

    用于盘中 10:00 拉取最新排行数据，使实时刷新的基准更贴近当天盘面。
    """
    print("=" * 50)
    print(f"[{now_cn()}] 盘中排行刷新...")

    ranking_df = _fetch_ranking()
    if ranking_df is None or ranking_df.empty:
        print("[排行刷新] 拉取失败（空结果），保留上次成功的 latest_ranking.json + latest_insight.json")
        print("=" * 50)
        # 返回当前盘面的快照供调用方查看
        try:
            return json.loads((DATA_DIR / "latest_ranking.json").read_text())
        except Exception:
            return {"ranking": [], "date": now_cn().strftime("%Y-%m-%d %H:%M:%S")}

    ranking_records = ranking_df.to_dict("records")
    try:
        from src.engine.top30_streak import stamp_streak
        stamp_streak(ranking_records)
    except Exception as e:
        print(f"  周期计数失败: {e}")
    ranking_data = {
        "date": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "ranking": ranking_records,
    }
    (DATA_DIR / "latest_ranking.json").write_text(
        json.dumps(ranking_data, ensure_ascii=False, indent=2)
    )

    # 市场洞察
    _run_market_insight(ranking_records)

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
    print(f"[{now_cn()}] 开始周期更新...")

    # 1-2. 获取排行数据
    ranking_df = _fetch_ranking()

    # 数据源全部不可用：保留上次成功的 latest_ranking.json + insight + snapshot
    if ranking_df is None or ranking_df.empty:
        print("[周期更新] 排行数据为空，保留旧快照不覆盖；后续仍执行涨停缓存刷新与选股记录回填")
        # 仍然继续做后续不依赖 ranking 的工作
        try:
            from src.data.fetcher import fetch_limit_up_history
            print("  刷新涨停缓存...")
            fetch_limit_up_history(days=5)
        except Exception as e:
            print(f"  涨停缓存刷新失败: {e}")
        try:
            from src.engine.screener_history import backfill_close, backfill_next_day_auction
            from src.data.fetcher import fetch_realtime_spot
            spot = fetch_realtime_spot()
            backfill_close(spot)
            backfill_next_day_auction(spot)
        except Exception as e:
            print(f"  选股记录回填失败: {e}")
        print("=" * 50)
        try:
            return json.loads((DATA_DIR / "latest_snapshot.json").read_text())
        except Exception:
            return {}

    # 轮转 prev_ranking（仅在拿到新数据时才轮转，避免把旧 prev 弄丢）
    try:
        from src.engine.market_insight import rotate_ranking_for_prev
        rotate_ranking_for_prev()
    except Exception:
        pass

    # 保存排行（已是 top30，不再截断）
    ranking_records = ranking_df.to_dict("records")
    try:
        from src.engine.top30_streak import stamp_streak
        stamp_streak(ranking_records)
    except Exception as e:
        print(f"  周期计数失败: {e}")
    ranking_data = {
        "date": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "ranking": ranking_records,
    }
    (DATA_DIR / "latest_ranking.json").write_text(
        json.dumps(ranking_data, ensure_ascii=False, indent=2)
    )

    # 市场洞察
    _run_market_insight(ranking_records)

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

    # 收盘后同步刷新涨停缓存（数据最完整，为明天9:27选股准备）
    try:
        from src.data.fetcher import fetch_limit_up_history
        print("  刷新涨停缓存...")
        fetch_limit_up_history(days=5)
    except Exception as e:
        print(f"  涨停缓存刷新失败（不影响周期）: {e}")

    # 收盘后回填选股记录的当日收盘价
    try:
        from src.engine.screener_history import backfill_close, backfill_next_day_auction
        from src.data.fetcher import fetch_realtime_spot
        spot = fetch_realtime_spot()
        backfill_close(spot)
        # 同时回填历史记录的次日数据（收盘后K线已更新，比9:27更完整）
        backfill_next_day_auction(spot)
    except Exception as e:
        print(f"  选股记录回填失败: {e}")

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
    print(f"[{now_cn()}] 开始选股...")

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

    # 4. 高标龙头竞价反馈（市场高标 + 主板高标[昨日最高连板] + 昨日主板涨停股平均竞价）
    from src.engine.leader_feedback import (
        evaluate_leader, find_leader_from_snapshot,
        evaluate_lianban_leader, find_main_board_lianban_leaders,
        compute_yesterday_main_board_auction,
        compute_yesterday_zb_today_auction,
        compute_yesterday_limit_down_today_auction,
        LeaderFeedback, LeaderSignal,
    )
    leader_fb = None          # 市场高标（全市场10日涨幅第一）
    main_board_fbs: list[tuple[LeaderFeedback, int]] = []  # 主板高标 = 主板昨日最高连板（平局多只）
    y_main_board_stats = None # 昨日主板涨停股今日竞价平均
    y_zb_stats = None         # 昨日炸板股今日竞价均价
    y_ld_stats = None         # 昨日跌停股今日竞价均价

    # 高标龙头直接从排行数据取（而非周期快照），确保是最新10日涨幅榜第一
    ranking_file = str(DATA_DIR / "latest_ranking.json")

    if not spot_df.empty:
        # — 市场高标龙头（排行榜第一） —
        leader_info = _find_leader_from_ranking(ranking_file, main_board_only=False)
        # 排行数据可能还没生成，fallback 到周期快照
        if leader_info is None and cycle_snapshot:
            leader_info = find_leader_from_snapshot(cycle_snapshot)
        if leader_info:
            code, name, gain_10d = leader_info
            leader_fb = evaluate_leader(code, name, gain_10d, spot_df)
            print(f"市场高标: {leader_fb.leader_name} 竞价{leader_fb.auction_change_pct:+.1f}% → {leader_fb.signal.value}")
            print(f"  {leader_fb.reason}")

        # — 主板高标 = 主板昨日最高连板（>=2连板，平局全部展示）—
        lb_list = find_main_board_lianban_leaders(limit_up_hist, spot_df)
        if lb_list:
            for lb_code, lb_name, lb_count in lb_list:
                fb = evaluate_lianban_leader(lb_code, lb_name, lb_count, spot_df)
                main_board_fbs.append((fb, lb_count))
                print(f"昨日主板连板高标: {fb.leader_name}({lb_count}连板) "
                      f"竞价{fb.auction_change_pct:+.1f}% → {fb.signal.value}")
                print(f"  {fb.reason}")
        else:
            print("昨日主板连板高标: 无 >=2 连板候选")

        # — 昨日主板涨停股 今日竞价平均表现（接力情绪锚定） —
        y_main_board_stats = compute_yesterday_main_board_auction(limit_up_hist, spot_df)
        if y_main_board_stats:
            print(f"昨日主板涨停股({y_main_board_stats['sample_count']}只): "
                  f"平均竞价{y_main_board_stats['avg_change_pct']:+.2f}% "
                  f"(高开{y_main_board_stats['positive_count']}/低开{y_main_board_stats['negative_count']})")
        else:
            print("昨日主板涨停股: 无样本")

        # — 昨日炸板股 今日竞价均价（接力反向锚定） —
        y_zb_stats = compute_yesterday_zb_today_auction(spot_df)
        if y_zb_stats:
            print(f"昨日炸板股({y_zb_stats['sample_count']}/{y_zb_stats['pool_size']}只): "
                  f"今日均价{y_zb_stats['avg_change_pct']:+.2f}% "
                  f"(高开{y_zb_stats['positive_count']}/低开{y_zb_stats['negative_count']})")
        else:
            print("昨日炸板股: 无样本")

        # — 昨日竞价跌停股 今日竞价均价（弱势股反弹/续跌信号） —
        y_ld_stats = compute_yesterday_limit_down_today_auction(spot_df)
        if y_ld_stats:
            print(f"昨日跌停股({y_ld_stats['sample_count']}/{y_ld_stats['pool_size']}只): "
                  f"今日均价{y_ld_stats['avg_change_pct']:+.2f}%")
        else:
            print("昨日跌停股: 无样本（首次跑无历史数据，明日起有）")

        # 保存龙头反馈（市场高标/主板高标[连板]/昨日主板涨停均值）
        def _fb_to_dict(fb: LeaderFeedback) -> dict:
            return {
                "leader_code": fb.leader_code,
                "leader_name": fb.leader_name,
                "leader_gain_10d": fb.leader_gain_10d,
                "auction_change_pct": fb.auction_change_pct,
                "signal": fb.signal.value,
                "can_trade": fb.can_trade,
                "aggression": fb.aggression,
                "reason": fb.reason,
            }

        def _main_board_fb_to_dict(fb: LeaderFeedback, board_count: int) -> dict:
            d = _fb_to_dict(fb)
            d["board_count"] = board_count
            return d

        main_board_dicts = [
            _main_board_fb_to_dict(fb, count) for fb, count in main_board_fbs
        ]
        leader_data = {
            "date": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "market_leader": _fb_to_dict(leader_fb) if leader_fb else None,
            "main_board_leaders": main_board_dicts,
            # 兼容旧 schema：第一只放在 main_board_leader
            "main_board_leader": main_board_dicts[0] if main_board_dicts else None,
            "yesterday_main_board_avg_auction": y_main_board_stats,
            "yesterday_zb_today_auction": y_zb_stats,
            "yesterday_limit_down_today_auction": y_ld_stats,
            # 兼容旧字段（dashboard / cross_validator 可能读取）
            **(  _fb_to_dict(leader_fb) if leader_fb else {}),
        }
        (DATA_DIR / "latest_leader.json").write_text(
            json.dumps(leader_data, ensure_ascii=False, indent=2)
        )

    # 4.5 全市场竞价风向标 + 梯队情绪池
    pool_sent = None
    market_stats = None
    try:
        from src.engine.sentiment_pool import (
            compute_pool_sentiment, compute_market_auction_stats,
            load_pool_from_ranking, save_sentiment,
        )
        market_stats = compute_market_auction_stats(spot_df)

        pool_codes = load_pool_from_ranking()
        if pool_codes:
            pool_sent = compute_pool_sentiment(pool_codes, spot_df)
            if pool_sent:
                save_sentiment(pool_sent, market_stats)
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
            market_cap_min=SCREENER_CONFIG.get("market_cap_min", 20),
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
            "date": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
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
        "date": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
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
        # 主板连板高标只取 LeaderFeedback（list），不带 board_count
        mb_fbs_only = [fb for fb, _ in main_board_fbs] if main_board_fbs else []
        signals = cross_validate(
            cs, hits, leader_fb, pool_sent,
            market_stats=market_stats,
            main_board_leaders=mb_fbs_only,
        )
        signals_data = {
            "date": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "cycle_phase": cycle_snapshot.get("phase", "孕育期"),
            "leader_signal": leader_fb.signal.value if leader_fb else None,
            "leader_can_trade": leader_fb.can_trade if leader_fb else None,
            "signals": [asdict(s) for s in signals],
        }
    else:
        signals_data = {
            "date": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
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

    # 选股记录：归档今日 + 回填昨日次日竞价
    try:
        from src.engine.screener_history import archive_today_hits, backfill_next_day_auction
        archive_today_hits([asdict(h) for h in hits], spot_df)
        backfill_next_day_auction(spot_df)
    except Exception as e:
        print(f"[选股记录] 异常: {e}")

    # 8a. 竞价决策卡打分
    auction_scores = []
    if hits:
        try:
            from src.engine.auction_scorer import score_all_hits
            auction_scores = score_all_hits([asdict(h) for h in hits])
            for sc in auction_scores:
                veto = " ⚠️否决" if sc.get("has_veto") else ""
                print(f"  [决策卡] {sc['code']} {sc['name']}: {sc['total_score']}分 → {sc['action']}{veto}")
        except Exception as e:
            print(f"[决策卡] 异常: {e}")

    # 8b. 盘前决策记录
    try:
        from src.engine.decision_tracker import create_premarket_record
        # 读取昨日复盘的观察池
        review_file = DATA_DIR / "latest_review.json"
        watch_pool = []
        if review_file.exists():
            review = json.loads(review_file.read_text())
            watch_pool = review.get("watch_pool", [])
        create_premarket_record(watch_pool, auction_scores, [asdict(h) for h in hits])
    except Exception as e:
        print(f"[决策追踪] 异常: {e}")

    # 8c. 邮件推送（紧接选股结果，不等看板数据刷新，确保9:30前送达）
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

        # 新版邮件需要的额外数据：sentiment（含 market 风向）、ranking（板块查表）
        sentiment_email = None
        sent_file = DATA_DIR / "latest_sentiment.json"
        if sent_file.exists():
            try:
                sentiment_email = json.loads(sent_file.read_text())
            except Exception:
                pass
        ranking_email = None
        rank_file = DATA_DIR / "latest_ranking.json"
        if rank_file.exists():
            try:
                ranking_email = json.loads(rank_file.read_text())
            except Exception:
                pass

        send_screener_report(
            cycle_phase=cycle_snapshot.get("phase", "孕育期") if cycle_snapshot else "孕育期",
            cycle_day=cycle_snapshot.get("phase_day", 0) if cycle_snapshot else 0,
            representative=cycle_snapshot.get("representative") if cycle_snapshot else None,
            leader=leader_data,
            hits=[asdict(h) for h in hits],
            signals=signals_data.get("signals", []),
            deviations=dev_data,
            sentiment_data=sentiment_email,
            ranking_data=ranking_email,
        )
    except Exception as e:
        print(f"[邮件] 推送异常: {e}")

    # 9. 异步后台任务（不阻塞选股返回）
    import threading

    def _background_tasks():
        # 9a. 三板组检查（查龙��榜匹配席位）
        if hits:
            try:
                from src.engine.sanbanzhu import check_and_annotate
                hits_dicts = [asdict(h) for h in hits]
                check_and_annotate(hits_dicts)
                # 更新选股记录中的三板组标记
                from src.engine.screener_history import _load, _save
                records = _load()
                today = now_cn().strftime("%Y-%m-%d")
                sbz_map = {h["code"]: h for h in hits_dicts}
                for r in records:
                    if r["date"] == today and r["code"] in sbz_map:
                        h = sbz_map[r["code"]]
                        r["sanbanzhu"] = h.get("sanbanzhu", False)
                        r["sanbanzhu_detail"] = h.get("sanbanzhu_detail", "")
                _save(records)
            except Exception as e:
                print(f"  [三板组] 异常: {e}")

        # 9b. 刷新周期+排行+洞察
        try:
            print("  [后台] 刷新周期+排行+洞察...")
            run_cycle_update()
            print("  [后台] 刷新完成")
        except Exception as e:
            print(f"  [后台] 刷新异常: {e}")

    threading.Thread(target=_background_tasks, daemon=True).start()

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

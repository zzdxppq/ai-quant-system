"""定时任务调度器

- 收盘后（15:30）：更新10日涨幅排行 + 周期状态
- 早盘（9:27）：执行选股 + 交叉验证
- 支持 mock 模式（网络不通时使用模拟数据）
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

# 是否使用模拟数据（环境变量 MOCK=1 或网络不通时自动降级）
USE_MOCK = os.getenv("MOCK", "0") == "1"


def _fetch_ranking() -> pd.DataFrame:
    """获取10日涨幅排行，网络失败时降级为mock"""
    if USE_MOCK:
        from src.data.mock_data import generate_mock_ranking
        scenario = os.getenv("MOCK_SCENARIO", "small_cycle_start")
        print(f"[MOCK] 使用模拟数据，场景: {scenario}")
        return generate_mock_ranking(scenario)

    try:
        from src.data.fetcher import fetch_realtime_spot
        spot_df = fetch_realtime_spot()
        return _calc_10d_gain_from_spot(spot_df)
    except Exception as e:
        print(f"实时数据拉取失败: {e}")
        print("降级为模拟数据...")
        from src.data.mock_data import generate_mock_ranking
        return generate_mock_ranking("small_cycle_start")


def _fetch_screener_data():
    """获取选股所需数据"""
    if USE_MOCK:
        from src.data.mock_data import generate_mock_spot, generate_mock_limit_up_history
        return generate_mock_spot(), generate_mock_limit_up_history()

    try:
        from src.data.fetcher import fetch_realtime_spot, fetch_limit_up_history
        return fetch_realtime_spot(), fetch_limit_up_history(days=5)
    except Exception as e:
        print(f"数据拉取失败: {e}，降级为模拟数据")
        from src.data.mock_data import generate_mock_spot, generate_mock_limit_up_history
        return generate_mock_spot(), generate_mock_limit_up_history()


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

    # 保存排行
    ranking_data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ranking": ranking_df.head(50).to_dict("records"),
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
    3. 高标龙头竞价反馈（一票否决层）
    4. 执行选股筛选
    5. 交叉验证（结合龙头反馈调整信号强度）
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

    # 5. 执行选股
    hits = run_screener(spot_df, limit_up_hist, cycle_codes)

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
        signals = cross_validate(cs, hits, leader_fb)
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


def _calc_10d_gain_from_spot(spot_df: pd.DataFrame) -> pd.DataFrame:
    """从实时快照估算10日涨幅

    AKShare stock_zh_a_spot_em 不直接提供10日涨幅，
    需要拉取个股历史或使用其他接口。

    方案：使用 stock_board_concept_hist_em 或逐个拉取
    高效方案：使用 stock_rank_ljqs_ths（同花顺连续上涨排行）
    或者用 stock_zh_a_hist 按个股拉取10天前收盘价
    """
    import akshare as ak

    # 方案A：尝试用同花顺接口获取区间涨幅
    try:
        # 使用东方财富涨幅排行（可指定区间）
        # stock_rank_cxg_ths: 创新高  stock_rank_lxsz_ths: 连续上涨
        # 这些不直接给10日涨幅

        # 最可靠方案：用全市场快照 + 历史收盘价计算
        # 先获取当前价格
        if spot_df.empty:
            return pd.DataFrame()

        # 获取10天前的收盘价：用 stock_zh_a_hist 拉取每只股票太慢
        # 改用：stock_zh_a_spot_em 的60日涨跌幅 或自行计算
        # 检查是否有相关列
        available_cols = spot_df.columns.tolist()

        # 尝试找到可用的涨幅列
        gain_col_map = {
            "60日涨跌幅": 60,
            "年初至今涨跌幅": 0,
        }

        # 如果有"涨跌幅"列，我们需要累计10天的
        # 最实用方案：选取涨幅前100的股票，逐个拉历史算精确10日涨幅
        result = _calc_10d_precise(spot_df)
        return result

    except Exception as e:
        print(f"计算10日涨幅失败: {e}")
        return pd.DataFrame()


def _calc_10d_precise(spot_df: pd.DataFrame) -> pd.DataFrame:
    """精确计算10日涨幅：先粗筛再精算"""
    import akshare as ak
    from src.data.fetcher import fetch_stock_history

    # 粗筛：用当日涨幅排前面的 + 最近活跃的股票
    # 取全市场当日涨幅>0的股票
    if "change_pct" in spot_df.columns:
        active = spot_df[spot_df["change_pct"] > -5].copy()
    else:
        active = spot_df.copy()

    # 对所有股票计算10日涨幅（批量拉取）
    # 使用 stock_zh_a_hist 单只拉取，限制数量
    # 先取最可能上榜的：当日涨停或近涨停的
    results = []
    codes_to_check = active["code"].tolist()

    # 优化：使用 stock_board_industry_hist_em 或 stock_rank_lxsz_ths
    # 尝试用连续上涨排行
    try:
        lx_df = ak.stock_rank_lxsz_ths()
        if not lx_df.empty:
            code_col = None
            for col in ["股票代码", "代码", "code"]:
                if col in lx_df.columns:
                    code_col = col
                    break
            gain_col = None
            for col in ["累计涨幅", "区间涨跌幅", "连续涨跌幅"]:
                if col in lx_df.columns:
                    gain_col = col
                    break
            name_col = None
            for col in ["股票简称", "名称", "name"]:
                if col in lx_df.columns:
                    name_col = col
                    break

            if code_col and gain_col:
                result = pd.DataFrame({
                    "code": lx_df[code_col].astype(str),
                    "name": lx_df[name_col] if name_col else "",
                    "gain_10d": pd.to_numeric(lx_df[gain_col], errors="coerce"),
                })
                result = result.dropna(subset=["gain_10d"])
                result["is_main_board"] = result["code"].apply(
                    lambda c: not str(c).startswith(("300", "301", "688", "8", "4"))
                )
                return result.sort_values("gain_10d", ascending=False).head(100)
    except Exception as e:
        print(f"连续上涨排行获取失败: {e}")

    # 兜底：逐个拉取Top标的的10日历史
    # 限制数量避免太慢
    check_limit = min(200, len(codes_to_check))
    for code in codes_to_check[:check_limit]:
        try:
            hist = fetch_stock_history(str(code), days=15)
            if hist is not None and len(hist) >= 10:
                close_now = hist.iloc[-1]["收盘"]
                close_10d = hist.iloc[-11]["收盘"] if len(hist) >= 11 else hist.iloc[0]["收盘"]
                gain = (close_now / close_10d - 1) * 100
                name_val = spot_df[spot_df["code"] == code]["name"].values
                results.append({
                    "code": str(code),
                    "name": name_val[0] if len(name_val) > 0 else "",
                    "gain_10d": round(gain, 2),
                })
        except Exception:
            continue

    if results:
        result_df = pd.DataFrame(results)
        result_df["is_main_board"] = result_df["code"].apply(
            lambda c: not str(c).startswith(("300", "301", "688", "8", "4"))
        )
        return result_df.sort_values("gain_10d", ascending=False)

    return pd.DataFrame(columns=["code", "name", "gain_10d", "is_main_board"])


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

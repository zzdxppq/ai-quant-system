"""定时任务调度器

盘后（交易日，串行，北京时间）：
- 18:00 `run_eod_bundle`：周期/TOP30/涨停缓存/趋势/洞察 → 复盘/决策追踪/趋势回填
  全市场 10 日涨幅扫描（5000+ 只）只在这里跑
  （推迟到 18:00 避开收盘后东财接口高并发时段，提高 zt_pool 拉取成功率）

盘中/早盘：
- 9:35-15:05 每 10 分钟 top30 轻量刷新（仅更新现价/涨幅，不全市场扫描）
- 9:27 选股 + 邮件（邮件优先，富化后置）；选股完后台仅对已有 TOP30 做字段补全
  + 重算市场洞察（30 只查询，不全市场扫描；不动周期状态机/趋势/收盘回填）
- 20:00 三板组晚间批量

数据源：新浪财经(实时) + 东方财富(K线/排行) → AKShare(兜底) → zt_pool/腾讯兜底
"""
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.config import DATA_DIR, SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE, now_cn

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULER_LOG_PATH = _PROJECT_ROOT / "logs" / "scheduler.log"

# 串行化写库定时任务（latest_* / 历史表 / DuckDB），避免 9:26 后台周期与 12:30 排行刷新等同写并发
_SCHEDULER_WRITE_LOCK = threading.RLock()
_SCHEDULER_LOG_LOCK = threading.Lock()


def _append_scheduler_log(line: str) -> None:
    SCHEDULER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _SCHEDULER_LOG_LOCK:
        with open(SCHEDULER_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def log_scheduler_cron(
    job_id: str,
    phase: str,
    *,
    ok: bool | None = None,
    error: str | None = None,
    elapsed_s: float | None = None,
) -> None:
    """写入 logs/scheduler.log 并同步 print（供 docker/journal 查看）。"""
    ts = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    extra = ""
    if phase == "DONE":
        if ok is True:
            extra = f" ok elapsed_s={elapsed_s:.1f}" if elapsed_s is not None else " ok"
        elif ok is False:
            err = (error or "unknown").replace("\n", " ")[:200]
            extra = f" FAIL error={err}"
            if elapsed_s is not None:
                extra += f" elapsed_s={elapsed_s:.1f}"
    line = f"{ts} [{job_id}] {phase}{extra}"
    _append_scheduler_log(line)
    print(line, flush=True)


def wrap_cron_job(job_id: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """仅用于 APScheduler cron 注册；CLI/API 直接调原函数不打 START/DONE。"""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        log_scheduler_cron(job_id, "START")
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            log_scheduler_cron(
                job_id, "DONE", ok=True, elapsed_s=time.perf_counter() - t0,
            )
            return result
        except Exception as e:
            log_scheduler_cron(
                job_id,
                "DONE",
                ok=False,
                error=str(e),
                elapsed_s=time.perf_counter() - t0,
            )
            raise

    return wrapper


def _scheduler_write_locked(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _SCHEDULER_WRITE_LOCK:
            return fn(*args, **kwargs)

    return wrapper

from src.data.json_io import dump_json_file, load_json_file
from src.engine.cycle import CycleEngine, calc_gain_10d
from src.engine.screener import run_screener
from src.engine.cross_validator import cross_validate


def _fetch_ranking() -> pd.DataFrame:
    """获取10日涨幅排行（全市场 top30，过滤 ST+新股，含富化字段）"""
    from src.data.fetcher import fetch_gain_10d_ranking
    return fetch_gain_10d_ranking(top_n=30)


def _screener_disk_payload(data: dict) -> dict:
    """落库 latest_screener 时去掉 _spot_* 等内部字段。"""
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _fetch_screener_data():
    """获取选股所需数据（9:26）

    · 竞价统计（一字/跌停、昨日主板涨停均价等）：拉一次全市场当日竞价快照（新浪/东财全量）
    · 选股候选：仍由 run_screener 从昨日涨停池连板递推，不用涨幅榜前 100 定 universe
    """
    from src.data.fetcher import fetch_limit_up_history
    from src.engine.screener import fetch_auction_spot_full

    limit_up_hist = fetch_limit_up_history(days=5)
    spot_df = fetch_auction_spot_full(limit_up_history=limit_up_hist)
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
    data = load_json_file(path)
    if data is None:
        return None
    for item in data.get("ranking", []):
        code = str(item.get("code", ""))
        if main_board_only and not item.get("is_main_board", False):
            continue
        return code, item.get("name", ""), item.get("gain_10d", 0)
    return None


def _run_market_insight(ranking_records: list[dict]) -> None:
    """运行四维市场洞察 + 主动攻击热度聚合"""
    try:
        from src.engine.market_insight import (
            analyze_market_insight, save_insight, load_prev_ranking,
        )
        from src.engine.active_attack import aggregate_market_attack_phase
        from dataclasses import asdict as _asdict
        prev = load_prev_ranking()
        insight = analyze_market_insight(ranking_records, prev)

        # 主动攻击热度（个股 active_attack 已由 scanner 注入）
        attack_phase = aggregate_market_attack_phase(
            ranking_records, date_str=now_cn().strftime("%Y-%m-%d"),
        )
        # 保存：先写 dataclass insight，再 patch attack_phase
        save_insight(insight)
        try:
            from src.config import DATA_DIR as _DD
            p = _DD / "latest_insight.json"
            d = load_json_file(p) or {}
            d["attack_phase"] = attack_phase
            dump_json_file(p, d)
        except Exception as _e:
            print(f"[市场洞察] attack_phase 写入失败: {_e}")

        print(f"[市场洞察] 波形={insight.wave.wave_phase}({insight.wave.intensity:.0f}) · "
              f"板块集中度{insight.sector_concentration}% · {insight.capital_summary}")
        print(f"[主动攻击] {attack_phase['phase']} · 攻击 {attack_phase['attack_count']}/30 · "
              f"翻倍 {attack_phase['doubler_count']} · 仓位上限 {attack_phase['position_cap_pct']}%")
    except Exception as e:
        print(f"[市场洞察] 分析失败: {e}")


def _backfill_yesterday_for_morning_email(spot_df, market_stats=None) -> None:
    """发信前：回填昨日选股的次日竞价% + 今日决策（与看板昨日选股表同源）。"""
    if spot_df is None:
        return
    ysd = ""
    try:
        from src.engine.screener_history import backfill_next_day_auction, yesterday_pick_date

        today = now_cn().strftime("%Y-%m-%d")
        ysd = yesterday_pick_date(today) or ""
        backfill_next_day_auction(spot_df, only_pick_date=ysd or None)
    except Exception as e:
        print(f"[选股记录] 发信前竞价回填异常: {e}")
        return
    try:
        from src.engine.next_day_sell_advice import backfill_next_day_sell_advice

        mkt_ld = None
        if market_stats is not None:
            mkt_ld = getattr(market_stats, "limit_down", None)
        if mkt_ld is None:
            try:
                sf = DATA_DIR / "latest_sentiment.json"
                if sf.exists():
                    sd = load_json_file(sf) or {}
                    mkt_ld = (sd.get("market") or {}).get("limit_down")
            except Exception:
                pass
        b1_env = None
        try:
            from src.engine.screener_market_env import load_screener_market_env

            b1_env = load_screener_market_env().get("b1_rate")
        except Exception:
            pass
        backfill_next_day_sell_advice(
            market_limit_down=mkt_ld,
            b1_rate=b1_env,
        )
    except Exception as e:
        print(f"[次日卖出建议] 发信前回填异常: {e}")


@_scheduler_write_locked
def run_post_market_bundle() -> None:
    """盘后串（由 run_eod_bundle 调用）：复盘 → 复盘邮件 → 决策追踪 → 趋势回填。

    也可手动执行：`python main.py --post-market`（需已 init_db / 数据目录可用）。
    """
    # 1. 盘后复盘（次日鱼塘）
    review_obj = None
    try:
        from src.engine.daily_review import run_daily_review

        review_obj = run_daily_review()
    except Exception as e:
        print(f"[复盘] 异常: {e}")

    # 1b. 复盘邮件（接力环境评分卡，与 review.html 区域 A 同源）
    try:
        from src.notify.email_sender import send_review_report

        payload = None
        if review_obj is not None:
            try:
                payload = asdict(review_obj)
            except Exception:
                payload = None
        if not payload:
            try:
                payload = load_json_file(DATA_DIR / "latest_review.json")
            except Exception:
                payload = None
        sent_ok = send_review_report(payload, entry="review_eod")
        if sent_ok:
            print(f"[复盘邮件] 已推送 ({now_cn().strftime('%H:%M:%S')})")
        else:
            print(f"[复盘邮件] 未推送 ({now_cn().strftime('%H:%M:%S')})")
    except Exception as e:
        print(f"[复盘邮件] 推送异常: {e}")

    # 2. 决策追踪回填
    try:
        from src.engine.decision_tracker import backfill_result

        backfill_result(now_cn().strftime("%Y-%m-%d"))
    except Exception as e:
        print(f"[决策追踪] 回填异常: {e}")

    # 3. 趋势池次日表现回填（K 线已含下一交易日）
    try:
        from src.engine.trend_screener import reconcile_trend_history_file, backfill_trend_next_day

        reconcile_trend_history_file()
        backfill_trend_next_day()
    except Exception as e:
        print(f"[趋势选股] 回填异常: {e}")


@_scheduler_write_locked
def run_ranking_top30_refresh() -> dict:
    """仅更新已有 TOP30 的实时现价/涨幅（不动排名，不全市场扫描）。

    用途：交易时段每 10 分钟定时轻量刷新，与 /api/ranking-live 同数据源。
    仅在 9:35-15:05 交易时段执行，非交易时段调用直接返回。
    """
    n = now_cn()
    h, m = n.hour, n.minute
    total_min = h * 60 + m
    if not (9 * 60 + 35 <= total_min <= 15 * 60 + 5):
        return {"ranking": [], "date": "", "live": False, "skipped": "out_of_trading_hours"}
    ranking_file = DATA_DIR / "latest_ranking.json"
    data = load_json_file(ranking_file)
    if data is None or not isinstance(data, dict):
        return {"ranking": [], "date": "", "live": False}

    items = data.get("ranking") or []
    if not items:
        return {**data, "live": False}

    codes = [str(r["code"]) for r in items]
    base_map = {str(r["code"]): dict(r) for r in items}

    live_map: dict[str, dict] = {}
    try:
        from src.data.tencent_api import fetch_stock_details

        live = fetch_stock_details(codes)
        if live is not None and not live.empty:
            live_map = {str(row["code"]): row for _, row in live.iterrows()}
    except Exception as e:
        print(f"[top30刷新] 腾讯行情: {e}")

    if len(live_map) < len(codes) // 2:
        try:
            from src.data.sina_api import fetch_realtime_batch

            sina_df = fetch_realtime_batch(codes)
            if not sina_df.empty:
                for _, row in sina_df.iterrows():
                    code = str(row.get("code", ""))
                    if code not in live_map:
                        pre = float(row.get("pre_close", 0))
                        close = float(row.get("close", 0))
                        live_map[code] = {
                            "code": code,
                            "close": close,
                            "change_pct": round((close / pre - 1) * 100, 2) if pre > 0 else 0,
                            "market_cap_yi": 0,
                        }
        except Exception as e:
            print(f"[top30刷新] 新浪兜底: {e}")

    def _live_gain_10d_from_snapshot_row(row: dict, live_close: float) -> float | None:
        try:
            gain = float(row.get("gain_10d", 0))
            close = float(row.get("close", 0))
            lc = float(live_close)
            if lc <= 0 or close <= 0 or gain == -100:
                return None
            c10 = close / (1 + gain / 100)
            if c10 <= 0:
                return None
            return round((lc / c10 - 1) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    changed = False
    if live_map:
        for code, base in base_map.items():
            lr = live_map.get(code)
            if lr is None:
                continue
            lc = float(lr.get("close", 0))
            if lc <= 0:
                continue
            g10 = _live_gain_10d_from_snapshot_row(base, lc)
            if g10 is not None:
                base["gain_10d"] = g10
            base["close"] = round(lc, 2)
            base["change_pct"] = round(float(lr.get("change_pct", 0)), 2)
            changed = True

    if changed:
        ts = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        data["date"] = ts
        data["updated_at"] = ts
        data["ranking"] = list(base_map.values())
        # 按 gain_10d 降序重排
        data["ranking"] = sorted(data["ranking"], key=lambda x: float(x.get("gain_10d", -999)), reverse=True)
        dump_json_file(ranking_file, data)

    data["live"] = True
    return data


@_scheduler_write_locked
def run_ranking_refresh() -> dict:
    """仅刷新10日涨幅排行（不触发周期状态机更新）

    定时：交易日 12:30；盘中 TOP30 实时展示由 /api/ranking-live 负责。
    """
    print("=" * 50)
    print(f"[{now_cn()}] 盘中排行刷新...")

    ranking_df = _fetch_ranking()
    if ranking_df is None or ranking_df.empty:
        print("[排行刷新] 拉取失败（空结果），保留上次成功的 latest_ranking.json + latest_insight.json")
        print("=" * 50)
        # 返回当前盘面的快照供调用方查看
        data = load_json_file(DATA_DIR / "latest_ranking.json")
        if data is not None:
            return data
        return {"ranking": [], "date": now_cn().strftime("%Y-%m-%d %H:%M:%S")}

    ranking_records = ranking_df.to_dict("records")
    try:
        from src.engine.top30_streak import stamp_streak
        stamp_streak(ranking_records)
    except Exception as e:
        print(f"  周期计数失败: {e}")
    # 注入 top_concepts（按今日涨停聚合热度选 1-2 个，前端显示用）
    try:
        from src.engine.concept_stats import enrich_ranking_with_top_concepts
        enrich_ranking_with_top_concepts(ranking_records, top_n=2)
    except Exception as e:
        print(f"  概念热度注入失败: {e}")
    ts = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    ranking_data = {
        "date": ts,
        "updated_at": ts,
        "trade_date": now_cn().strftime("%Y-%m-%d"),
        "ranking": ranking_records,
    }
    dump_json_file(DATA_DIR / "latest_ranking.json", ranking_data)

    # 市场洞察
    _run_market_insight(ranking_records)

    print(f"排行刷新完成: {len(ranking_df)} 只")
    print("=" * 50)
    return ranking_data


def _is_main_board_code(code6: str) -> bool:
    c = "".join(ch for ch in str(code6) if ch.isdigit())[-6:].zfill(6)
    if len(c) != 6:
        return False
    if c.startswith(("300", "301", "688")):
        return False
    if c.startswith(("8", "4", "92")):
        return False
    return True


def _re_enrich_and_persist_ranking_inplace() -> int:
    """对 latest_ranking 已有 TOP30 补全行情/连板/板块等字段并写回库。"""
    data = load_json_file(DATA_DIR / "latest_ranking.json")
    if not isinstance(data, dict):
        return 0
    recs = data.get("ranking") or []
    if not recs:
        return 0
    from src.data.ranking_scanner import re_enrich_ranking_records

    enriched = re_enrich_ranking_records(recs)
    data["ranking"] = enriched
    data["updated_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    dump_json_file(DATA_DIR / "latest_ranking.json", data)
    print(f"  TOP30 字段补全: {len(enriched)} 只")
    return len(enriched)


def _refresh_limit_up_cache_with_fallback() -> dict[str, pd.DataFrame] | None:
    """刷新涨停缓存；当日键缺失或过少时用东财 zt_pool 兜底。"""
    hist: dict[str, pd.DataFrame] | None = None
    try:
        from src.data.fetcher import fetch_limit_up_history

        print("  刷新涨停缓存...")
        hist = fetch_limit_up_history(days=5)
    except Exception as e:
        print(f"  涨停缓存刷新失败: {e}")

    try:
        from src.data.fetcher import sync_limit_up_cache_from_zt_pool

        from src.config import is_trading_day

        today_key = now_cn().strftime("%Y%m%d")
        cache = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
        day_rows = cache.get(today_key) if isinstance(cache, dict) else None
        n_rows = len(day_rows) if isinstance(day_rows, list) else 0
        # 交易日盘后：zt_pool 为连板数权威来源，须覆盖东财涨幅榜筛涨停（无 lbc、易漏高连板）
        need_zt = is_trading_day() or n_rows < 5
        if need_zt:
            n = sync_limit_up_cache_from_zt_pool(today_key)
            label = "同步" if is_trading_day() else "兜底"
            print(f"  涨停缓存 zt_pool {label}: {n} 只 ({today_key})")
            if hist is None and n > 0:
                from src.data.fetcher import fetch_limit_up_history

                hist = fetch_limit_up_history(days=5)
    except Exception as e:
        print(f"  zt_pool 兜底失败: {e}")
    return hist


def _run_cycle_close_chores(lim_hist=None) -> None:
    """收盘后通用收尾：日 K 预热、选股记录回填、概念缓存调度。"""
    try:
        warm_focus_klines_after_close(lim_hist)
    except Exception as e:
        print(f"  日K预热失败: {e}")
    try:
        from src.engine.screener_history import backfill_close, backfill_next_day_auction
        from src.engine.screener_history import reconcile_next_day_from_kline
        from src.data.fetcher import fetch_realtime_spot
        from src.engine.screener_backtest_report import invalidate_screener_backtest_cache

        spot = fetch_realtime_spot()
        backfill_close(spot)
        backfill_next_day_auction(spot)
        reconcile_next_day_from_kline(spot_df=spot)
        invalidate_screener_backtest_cache()
    except Exception as e:
        print(f"  选股记录回填失败: {e}")
    _maybe_rebuild_concept_cache()


@_scheduler_write_locked
def run_cycle_update() -> dict:
    return _run_cycle_update_impl()


def _norm_stock_code6(code: object) -> str:
    c = "".join(ch for ch in str(code or "") if ch.isdigit())[-6:].zfill(6)
    return c if len(c) == 6 and c.isdigit() else ""


def _main_board_limit_up_codes_from_hist(limit_up_hist: object | None) -> set[str]:
    """涨停缓存内主板涨停股（最近若干交易日并集，供盘后预热筛选用）。"""
    out: set[str] = set()
    hist = limit_up_hist if isinstance(limit_up_hist, dict) else {}
    for df in hist.values():
        if df is None or getattr(df, "empty", True):
            continue
        try:
            for _, row in df.iterrows():
                c = _norm_stock_code6(row.get("code"))
                if c and _is_main_board_code(c):
                    out.add(c)
        except Exception:
            continue
    return out


def warm_focus_klines_after_close(limit_up_hist: object | None) -> None:
    """盘后收盘：TOP30 + 次日选股池（今日选股命中 ∪ 复盘观察池）中当日涨停股，批量预热日 K。

    盘中打开 K 线弹窗仍为 cache-first + refresh=1；此处仅收盘后写库，便于次日早盘秒开。
    """
    from src.data.stock_search import warm_daily_klines

    codes: set[str] = set()
    top30: set[str] = set()
    pool_zt: set[str] = set()

    rd = load_json_file(DATA_DIR / "latest_ranking.json")
    for item in (rd or {}).get("ranking") or []:
        c = _norm_stock_code6(item.get("code"))
        if c:
            top30.add(c)
            codes.add(c)

    limit_up_codes = _main_board_limit_up_codes_from_hist(limit_up_hist)

    scr = load_json_file(DATA_DIR / "latest_screener.json")
    if isinstance(scr, dict):
        for h in scr.get("hits") or []:
            if not isinstance(h, dict):
                continue
            c = _norm_stock_code6(h.get("code"))
            if c and c in limit_up_codes:
                pool_zt.add(c)
                codes.add(c)

    rev = load_json_file(DATA_DIR / "latest_review.json")
    if isinstance(rev, dict):
        for w in rev.get("watch_pool") or []:
            if not isinstance(w, dict):
                continue
            c = _norm_stock_code6(w.get("code"))
            if c and c in limit_up_codes:
                pool_zt.add(c)
                codes.add(c)

    uniq = sorted(codes)
    if not uniq:
        print("  [日K预热] 无代码可拉取")
        return
    print(
        f"  [日K预热] TOP30={len(top30)} 只 + 次日池涨停={len(pool_zt)} 只，"
        f"去重共 {len(uniq)} 只（拉网写库）…"
    )
    warm_daily_klines(uniq, datalen=500, force_network=True)


def _run_cycle_update_impl() -> dict:
    """更新周期状态（收盘后调用）

    1. 拉取全市场实时行情
    2. 计算10日涨幅排行
    3. 更新周期状态机
    4. 保存快照
    """
    from src.config import is_trading_day
    if not is_trading_day():
        print(f"[{now_cn()}] 非交易日，跳过周期更新；保留上一交易日数据。")
        snap_file = DATA_DIR / "latest_snapshot.json"
        if snap_file.exists():
            return load_json_file(snap_file) or {"status": "skipped", "reason": "non-trading day"}
        return {"status": "skipped", "reason": "non-trading day"}
    print("=" * 50)
    print(f"[{now_cn()}] 开始周期更新...")

    # 1-2. 获取排行数据
    ranking_df = _fetch_ranking()

    # 数据源全部不可用：保留上次成功的 latest_ranking.json + insight + snapshot
    if ranking_df is None or ranking_df.empty:
        print("[周期更新] 排行扫描为空，保留旧 TOP30；执行 zt_pool 兜底 + 字段补全")
        try:
            _re_enrich_and_persist_ranking_inplace()
        except Exception as e:
            print(f"  TOP30 字段补全失败: {e}")
        lim_hist = _refresh_limit_up_cache_with_fallback()
        _run_cycle_close_chores(lim_hist)
        print("=" * 50)
        try:
            return load_json_file(DATA_DIR / "latest_snapshot.json") or {}
        except Exception:
            return {"status": "ranking_scan_empty"}

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
    # 注入 top_concepts（按今日涨停聚合热度选 1-2 个，前端显示用）
    try:
        from src.engine.concept_stats import enrich_ranking_with_top_concepts
        enrich_ranking_with_top_concepts(ranking_records, top_n=2)
    except Exception as e:
        print(f"  概念热度注入失败: {e}")
    ts = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    ranking_data = {
        "date": ts,
        "updated_at": ts,
        "trade_date": now_cn().strftime("%Y-%m-%d"),
        "ranking": ranking_records,
    }
    dump_json_file(DATA_DIR / "latest_ranking.json", ranking_data)

    try:
        _re_enrich_and_persist_ranking_inplace()
    except Exception as e:
        print(f"  TOP30 字段补全失败: {e}")

    # 趋势选股：必须在当日收盘 ranking 落库后执行（旧版 15:05 早于本任务，易用午间榜导致未更新或失真）
    try:
        from src.engine.trend_screener import run_trend_screener

        run_trend_screener()
    except Exception as e:
        print(f"  [趋势选股] 异常: {e}")

    # 市场洞察
    _run_market_insight(ranking_records)

    # 3. 更新周期状态
    engine = CycleEngine()
    snapshot = engine.update(ranking_df)

    # 4. 保存快照
    snap_ts = snapshot.date or now_cn().strftime("%Y-%m-%d %H:%M:%S")
    snapshot_dict = {
        "date": snap_ts,
        "updated_at": snap_ts,
        "phase": snapshot.phase,
        "phase_day": snapshot.phase_day,
        "phase_entered_date": getattr(snapshot, "phase_entered_date", "") or "",
        "representative": snapshot.representative,
        "candidates": snapshot.candidates,
        "prev_cycle": snapshot.prev_cycle,
        "notes": snapshot.notes,
    }
    dump_json_file(DATA_DIR / "latest_snapshot.json", snapshot_dict)

    # 追加到历史时间线
    _append_history(snapshot_dict)

    print(f"周期状态: {snapshot.phase} (第{snapshot.phase_day}天)")
    if snapshot.representative:
        rep = snapshot.representative
        print(f"代表股: {rep['name']}({rep['code']}) 10日涨幅:{rep['gain_10d']}%")

    lim_hist = _refresh_limit_up_cache_with_fallback()
    _run_cycle_close_chores(lim_hist)

    print("=" * 50)

    return snapshot_dict


@_scheduler_write_locked
def run_eod_bundle() -> dict:
    """交易日 18:00 盘后串行任务：周期更新 → 复盘/回填。

    cron 起止状态由 wrap_cron_job 写入 logs/scheduler.log；步骤级失败打印到 stdout。
    返回值含 steps/ok，供 --catchup-eod 等手动入口查看。
    """
    from src.config import is_trading_day

    started = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    summary: dict[str, Any] = {
        "trade_date": now_cn().strftime("%Y-%m-%d"),
        "started_at": started,
        "steps": {},
        "ok": True,
    }
    if not is_trading_day():
        summary["ok"] = True
        summary["skipped"] = "non-trading day"
        summary["finished_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        print("[盘后串行] 非交易日，跳过")
        return summary

    print("=" * 50)
    print(f"[{now_cn()}] 盘后串行任务开始...")

    from src.market_schedule import set_eod_bundle_running

    set_eod_bundle_running(True)
    try:
        try:
            snap = run_cycle_update()
            summary["steps"]["cycle_update"] = {"ok": True, "phase": (snap or {}).get("phase")}
        except Exception as e:
            summary["ok"] = False
            summary["steps"]["cycle_update"] = {"ok": False, "error": str(e)[:300]}
            print(f"[盘后串行] cycle_update 失败: {e}")

        try:
            run_post_market_bundle()
            summary["steps"]["post_market"] = {"ok": True}
        except Exception as e:
            summary["ok"] = False
            summary["steps"]["post_market"] = {"ok": False, "error": str(e)[:300]}
            print(f"[盘后串行] post_market 失败: {e}")

        summary["finished_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[盘后串行] 结束 ok={summary['ok']}")
        print("=" * 50)
        return summary
    finally:
        set_eod_bundle_running(False)


def _maybe_rebuild_concept_cache() -> None:
    """如概念缓存超过 18 小时未更新，detached 启动 builder。

    设计原则：复用盘后 cycle_update 收尾，不另起 cron；
    detached subprocess 避免阻塞 cycle_update。
    """
    try:
        from src.data.concept_fetcher import cache_meta, CACHE_PATH
        from datetime import datetime, timedelta
        meta = cache_meta()
        if meta.get("updated_at"):
            try:
                last = datetime.strptime(meta["updated_at"], "%Y-%m-%d %H:%M:%S")
                if now_cn().replace(tzinfo=None) - last < timedelta(hours=18):
                    print(f"  概念缓存仍新鲜 ({meta['updated_at']})，跳过重建")
                    return
            except ValueError:
                pass

        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        script = root / "scripts" / "build_concept_cache.py"
        if not script.exists():
            return
        log_path = root / "logs" / "concept_build.log"
        log_fh = open(log_path, "a")
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(root),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"  概念缓存 builder 已 detached 启动 → {log_path}")
    except Exception as e:
        print(f"  概念缓存重建调度失败（不影响周期）: {e}")


def _in_927_window(now_dt) -> bool:
    """选股 cron ± 5min 邮件窗口（含端点，中心读 SCREENER_CRON_*）。

    窗口中心读 SCREENER_CRON_HOUR/SCREENER_CRON_MINUTE，避免硬编码。
    """
    center = now_dt.replace(
        hour=SCREENER_CRON_HOUR,
        minute=SCREENER_CRON_MINUTE,
        second=0,
        microsecond=0,
    )
    return abs((now_dt - center).total_seconds()) <= 300


def _should_send_email(skip_email: bool | None) -> bool:
    """邮件推送守卫（story anti-duplicate-email-2.5）。

    - skip_email is True  → False（强制跳过，手动 refresh 用）
    - skip_email is False → True （强制发送，push2 恢复等场景预留）
    - skip_email is None  → 按 now_cn() 时间窗口判断（仅 cron±5min 内发邮件）
    """
    if skip_email is True:
        return False
    if skip_email is False:
        return True
    return _in_927_window(now_cn())


@_scheduler_write_locked
def run_screener_update(skip_email: bool | None = None, api_explicit: bool = False) -> dict:
    """执行选股（早盘 cron 调用，默认 9:27）

    api_explicit=True  → 来自 API 手动 refresh + send_email=true，
                         走 send_screener_report 的 entry=api_* 标记，
                         守卫允许窗口放宽到 9:20-15:30。
                         （仍受当日幂等 + 时间窗口硬约束）

    1. 拉取实时竞价数据
    2. 获取涨停历史（连板检测）
    3. 高标龙头竞价反馈（仅作当日操作建议，不过滤选股结果）
    4. 执行选股筛选
    5. 交叉验证（结合龙头反馈微调仓位建议）
    """
    from src.config import is_trading_day
    if not is_trading_day():
        print(f"[{now_cn()}] 非交易日，跳过选股；保留上一交易日选股结果。")
        scr_file = DATA_DIR / "latest_screener.json"
        if scr_file.exists():
            return load_json_file(scr_file) or {"status": "skipped", "reason": "non-trading day"}
        return {"status": "skipped", "reason": "non-trading day"}
    print("=" * 50)
    print(f"[{now_cn()}] 开始选股...")
    import time as _time
    _t0 = _time.perf_counter()

    def _lap(label: str) -> None:
        print(f"[选股耗时] {label}: {_time.perf_counter() - _t0:.1f}s")

    # 1-2. 获取选股数据
    spot_df, limit_up_hist = _fetch_screener_data()
    _lap("拉数(spot+涨停史)")

    # 3. 加载周期状态获取候选股
    snapshot_file = DATA_DIR / "latest_snapshot.json"
    cycle_codes = []
    cycle_snapshot = None
    if snapshot_file.exists():
        snapshot_data = load_json_file(snapshot_file) or {}
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
        dump_json_file(DATA_DIR / "latest_leader.json", leader_data)

    # 4.5 全市场竞价风向标 + 梯队情绪池
    pool_sent = None
    market_stats = None
    try:
        from src.engine.sentiment_pool import (
            compute_pool_sentiment, compute_market_auction_stats,
            load_pool_from_ranking, save_sentiment,
        )
        # spot_df 已是 9:27 全市场竞价快照；样本足够时不再二次拉全量
        market_stats = compute_market_auction_stats(spot_df)

        pool_codes = load_pool_from_ranking()
        if pool_codes:
            pool_sent = compute_pool_sentiment(pool_codes, spot_df)
            if pool_sent:
                save_sentiment(pool_sent, market_stats, spot_df=spot_df)
                print(f"梯队情绪: {pool_sent.verdict} · {pool_sent.reason}")
            else:
                print("梯队情绪: 无有效样本")
        else:
            print("梯队情绪: latest_ranking.json 池为空，跳过")
    except Exception as e:
        print(f"梯队情绪计算失败: {e}")
    _lap("龙头+情绪")

    # 5. 执行选股
    hits = run_screener(spot_df, limit_up_hist, cycle_codes)
    _lap(f"选股规则({len(hits)}只)")
    fallback_1to2 = False

    # 5.0 主选股 0 命中 → 1进2 公式兜底（有且仅有 1 只才采用）
    if not hits:
        try:
            from src.engine.screener import run_screener_1to2_fallback

            fb_hits = run_screener_1to2_fallback(spot_df, limit_up_hist, cycle_codes)
            if len(fb_hits) == 1:
                hits = fb_hits
                fallback_1to2 = True
                print(f"  [screener] 1进2 兜底采用唯一命中: {hits[0].code} {hits[0].name}")
            elif len(fb_hits) > 1:
                print(
                    f"  [screener] 1进2 兜底命中 {len(fb_hits)} 只，规则要求有且仅有 1 只，跳过"
                )
            else:
                print("  [screener] 1进2 兜底亦 0 命中")
        except Exception as e:
            print(f"  [screener] 1进2 兜底失败: {e}")
        _lap(f"1进2兜底({len(hits)}只)")

    # 5.1 腾讯接口富化：补全 market_cap / volume_ratio / turnover 并做严格二次过滤
    # 必要性：sina spot 源缺这些字段，screener 用了软过滤放行；此处用腾讯补齐后按通达信公式严格筛
    # 1进2 兜底不再做腾讯二次滤（避免把唯一票滤掉）
    if hits and not fallback_1to2:
        from src.config import SCREENER_CONFIG
        from src.data.tencent_api import enrich_screener_hits
        hits = enrich_screener_hits(
            hits,
            market_cap_min=SCREENER_CONFIG.get("market_cap_min", 20),
            market_cap_max=SCREENER_CONFIG["market_cap_max"],
            volume_ratio_min=SCREENER_CONFIG["volume_ratio_min"],
        )
    _lap(f"腾讯富化({len(hits)}只)")

    # 6. 保存选股结果（邮件所需字段先写入；昨日量能富化在邮件后）
    from src.engine.screener import LAST_AUCTION_SPOT_STATUS

    _spot_src = ""
    if spot_df is not None and not spot_df.empty:
        _spot_src = str(getattr(spot_df, "attrs", {}).get("source") or LAST_AUCTION_SPOT_STATUS)
    hits_data = {
        "date": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "hits": [asdict(h) for h in hits],
        "_spot_rows": len(spot_df) if spot_df is not None and not spot_df.empty else 0,
        "_spot_source": _spot_src or LAST_AUCTION_SPOT_STATUS,
    }
    if fallback_1to2:
        hits_data["fallback_1to2"] = True
        for h in hits_data["hits"]:
            h["fallback_1to2"] = True
            h["continuous_limit_up"] = 1

    # 6.1 富化 hits：注入 top_concepts + industry（dashboard 选股表显示用，Story 2.4）
    # 优先源 ranking_data；缺/损时 helper 内部走 cache 兜底链；永不抛错
    ranking_data: dict | None = None
    try:
        rank_file = DATA_DIR / "latest_ranking.json"
        ranking_data = load_json_file(rank_file)
    except Exception:
        ranking_data = None
    try:
        from src.engine.screener_concept_enrich import enrich_screener_hits_with_concepts
        enrich_screener_hits_with_concepts(hits_data, ranking_data)
    except Exception as e:
        print(f"[选股富化] helper 调用失败: {e}")

    # 6.1b 富化昨日量能（prev_day_turnover / prev_amount_ratio / prev_day_yizi）
    # 必须在 compute_per_stock_decision 之前执行，2进3规则依赖这些字段
    try:
        from src.engine.screener_prev_day_enrich import enrich_hits_with_prev_day_kline
        enrich_hits_with_prev_day_kline(hits_data)
    except Exception as e:
        print(f"[选股富化] 昨日量能富化失败: {e}")
    _lap("昨日量能富化")

    # 6.2 每只 hit 注入 per_stock_decision（按梯队 + 竞价 + 环境出仓位建议）
    try:
        from src.engine.screener_decision import compute_per_stock_decision
        # 加载市场环境 + 空间板 + 概念热度
        from src.engine.screener_market_env import (
            load_screener_market_env,
            load_screener_review_context,
        )

        market_env = load_screener_market_env()
        rev_ctx = load_screener_review_context()
        space_board_today = rev_ctx.get("space_board_today")
        highest_board_tier_today = rev_ctx.get("highest_board_tier_today")
        if main_board_fbs:
            try:
                from src.engine.dashboard_decision import highest_board_tier_from_leader_rows

                tier_rows = [
                    {
                        "leader_code": fb.leader_code,
                        "leader_name": fb.leader_name,
                        "board_count": lb,
                        "auction_change_pct": fb.auction_change_pct,
                    }
                    for fb, lb in main_board_fbs
                ]
                live_tier = highest_board_tier_from_leader_rows(tier_rows)
                if live_tier:
                    highest_board_tier_today = live_tier
            except Exception:
                pass
        # 高标红/绿盘供 2进3 分档（与 dashboard space_red 同源）
        try:
            from src.engine.screener_decision import _resolve_space_red

            sr = _resolve_space_red(
                market_env, space_board_today, highest_board_tier_today,
            )
            if sr is not None:
                market_env["space_red"] = sr
        except Exception:
            pass
        concept_zt_stats = rev_ctx.get("concept_zt_stats") or []
        market_highest_board = rev_ctx.get("market_highest_board")
        for h in hits_data.get("hits", []):
            h["per_stock_decision"] = compute_per_stock_decision(
                h, market_env,
                concept_zt_stats=concept_zt_stats,
                space_board_today=space_board_today,
                market_highest_board=market_highest_board,
                highest_board_tier_today=highest_board_tier_today,
            )
        # 有且仅有 1 只且策略不开仓 → 强制轻仓试错 2层（看板+邮件）
        from src.engine.screener_decision import apply_single_hit_light_trial

        if apply_single_hit_light_trial(hits_data.get("hits") or [], market_env):
            print("  [选股决策] 单票兜底：强制轻仓试错 2层")
    except Exception as e:
        print(f"[选股决策] per_stock_decision 注入失败: {e}")

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

    dump_json_file(DATA_DIR / "latest_signals.json", signals_data)

    # 7b. 决策快照（看板 + 邮件单一真源, decision-consistency-2.1）
    # 同步写入 latest_advice.json；必须在 send_screener_report 之前完成。
    try:
        from src.engine.advice_snapshot_hydrate import load_leader_for_advice, load_sentiment_for_advice
        from src.notify.email_sender import write_advice_snapshot

        sentiment_advice = load_sentiment_for_advice()
        leader_advice = load_leader_for_advice()
        write_advice_snapshot(
            sentiment_advice, leader_advice, spot_df=spot_df, market_stats=market_stats,
        )
    except Exception as e:
        print(f"[决策快照] 异常: {e}")
    _lap("决策快照")

    dump_json_file(DATA_DIR / "latest_screener.json", _screener_disk_payload(hits_data))
    print(
        f"[选股] latest_screener 已更新 date={hits_data.get('date')} "
        f"hits={len(hits_data.get('hits') or [])}"
    )

    # 8. 邮件推送（紧接决策快照；先回填昨日选股今日决策，再发信）
    # story anti-duplicate-email-2.5: skip_email 守卫（防盘中重复邮件）
    if _should_send_email(skip_email):
        try:
            _backfill_yesterday_for_morning_email(spot_df, market_stats)
            from src.notify.email_sender import send_screener_report
            leader_data = None
            leader_file = DATA_DIR / "latest_leader.json"
            if leader_file.exists():
                leader_data = load_json_file(leader_file)

            # 新版邮件需要的额外数据：sentiment（含 market 风向）、ranking（板块查表）
            sentiment_email = None
            sent_file = DATA_DIR / "latest_sentiment.json"
            if sent_file.exists():
                sentiment_email = load_json_file(sent_file)
            ranking_email = None
            rank_file = DATA_DIR / "latest_ranking.json"
            if rank_file.exists():
                try:
                    ranking_email = load_json_file(rank_file)
                except Exception:
                    pass

            sent_ok = send_screener_report(
                cycle_phase=cycle_snapshot.get("phase", "孕育期") if cycle_snapshot else "孕育期",
                cycle_day=cycle_snapshot.get("phase_day", 0) if cycle_snapshot else 0,
                representative=cycle_snapshot.get("representative") if cycle_snapshot else None,
                leader=leader_data,
                hits=list(hits_data.get("hits", [])),
                signals=signals_data.get("signals", []),
                deviations=None,
                sentiment_data=sentiment_email,
                ranking_data=ranking_email,
                entry=("api_refresh" if api_explicit else f"cron:{skip_email!r}"),
            )
            if sent_ok:
                print(f"[邮件] 已推送 ({now_cn().strftime('%H:%M:%S')})")
            else:
                print(f"[邮件] 未推送 ({now_cn().strftime('%H:%M:%S')})")
            _lap("SMTP发信")
        except Exception as e:
            print(f"[邮件] 推送异常: {e}")
    else:
        print(
            f"[邮件] 已按 skip_email={skip_email} 跳过推送"
            f"（now={now_cn().strftime('%H:%M:%S')}）"
        )
    _lap("发信前链路合计")

    hit_count = len(hits)
    matched = sum(1 for h in hits if h.matched_cycle)
    print(f"选股结果: {hit_count} 只命中, {matched} 只匹配周期")
    for h in hits:
        flag = " 🎯" if h.matched_cycle else ""
        print(f"  {h.code} {h.name} {h.continuous_limit_up}板 竞价{h.auction_gain}%{flag}")

    # 8-post. 邮件后耗时步骤（不阻塞推送）
    # 注：昨日量能富化已在 6.1b 提前执行，此处仅写盘
    try:
        dump_json_file(DATA_DIR / "latest_screener.json", _screener_disk_payload(hits_data))
    except Exception as e:
        print(f"[选股富化] 写盘失败: {e}")

    try:
        from src.engine.screener_history import archive_today_hits, backfill_next_day_auction

        archive_today_hits([asdict(h) for h in hits], spot_df)
        backfill_next_day_auction(spot_df)
        try:
            from src.engine.next_day_sell_advice import backfill_next_day_sell_advice

            mkt_ld = None
            if market_stats is not None:
                mkt_ld = getattr(market_stats, "limit_down", None)
            if mkt_ld is None:
                try:
                    sf = DATA_DIR / "latest_sentiment.json"
                    if sf.exists():
                        sd = load_json_file(sf) or {}
                        mkt_ld = (sd.get("market") or {}).get("limit_down")
                except Exception:
                    pass
            b1_env = None
            try:
                from src.engine.screener_market_env import load_screener_market_env

                b1_env = load_screener_market_env().get("b1_rate")
            except Exception:
                pass
            backfill_next_day_sell_advice(
                market_limit_down=mkt_ld,
                b1_rate=b1_env,
            )
        except Exception as e_sell:
            print(f"[次日卖出建议] 异常: {e_sell}")
        from src.engine.trend_screener import backfill_trend_morning_auction

        backfill_trend_morning_auction(spot_df)
    except Exception as e:
        print(f"[选股记录] 异常: {e}")
        try:
            from src.engine.screener_history import ensure_today_archived

            n = ensure_today_archived(spot_df=spot_df)
            if n:
                print(f"[选股记录] ensure_today_archived 补写 {n} 条")
        except Exception as e2:
            print(f"[选股记录] ensure_today_archived 失败: {e2}")

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

    try:
        from src.engine.decision_tracker import create_premarket_record
        review_file = DATA_DIR / "latest_review.json"
        watch_pool = []
        if review_file.exists():
            review = load_json_file(review_file) or {}
            watch_pool = review.get("watch_pool", [])
        create_premarket_record(watch_pool, auction_scores, [asdict(h) for h in hits])
    except Exception as e:
        print(f"[决策追踪] 异常: {e}")

    # 9. 异步后台任务（不阻塞选股返回）
    import threading

    def _background_tasks():
        # 9a. 三板组检查（查龙虎榜匹配席位）
        from dataclasses import asdict as _asdict_hits

        if hits:
            try:
                from src.engine.sanbanzhu import check_and_annotate
                hits_dicts = [_asdict_hits(h) for h in hits]
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

        # 9b. 轻量刷新 TOP30 字段 + 市场洞察（仅查 30 只代码，不跑全市场扫描）
        #     全市场 10 日涨幅扫描留给 18:00 run_eod_bundle 跑。
        try:
            print("  [后台] 轻量刷新 TOP30 字段 + 洞察...")
            n = _re_enrich_and_persist_ranking_inplace()
            if n > 0:
                rd = load_json_file(DATA_DIR / "latest_ranking.json") or {}
                recs = rd.get("ranking") or []
                if recs:
                    _run_market_insight(recs)
            print("  [后台] 刷新完成")
        except Exception as e:
            print(f"  [后台] 刷新异常: {e}")

    threading.Thread(target=_background_tasks, daemon=True).start()

    print("=" * 50)

    return hits_data


@_scheduler_write_locked
def run_evening_mainboard_sanbanzhu_batch() -> dict:
    """交易日 20:00：先刷新主板涨停池，再批量拉龙虎榜买入席并写入缓存（见 sanbanzhu 模块）。"""
    from src.engine.sanbanzhu import run_evening_mainboard_limitup_sanbanzhu_batch

    return run_evening_mainboard_limitup_sanbanzhu_batch()


def _append_history(snapshot: dict):
    """追加快照到历史时间线"""
    history_file = DATA_DIR / "cycle_history.json"
    history = load_json_file(history_file)
    if not isinstance(history, list):
        history = []

    # 避免同一天重复
    today = snapshot.get("date", "")
    history = [h for h in history if h.get("date") != today]
    history.append(snapshot)

    # 只保留最近60天
    history = history[-60:]

    dump_json_file(history_file, history)

"""FastAPI 后端"""
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

from src.config import BASE_DIR, DATA_DIR, now_cn
from src.engine.cycle import CycleEngine, CycleSnapshot, calc_gain_10d
from src.engine.screener import run_screener, ScreenerHit
from src.engine.cross_validator import cross_validate

app = FastAPI(title="AI量化周期看板", version="1.0.0")

# 静态文件
static_dir = BASE_DIR / "src" / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# 禁缓存响应头：避免浏览器缓存旧版 HTML 导致前端逻辑过期
_NOCACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _serve_html(filename: str, fallback: str) -> HTMLResponse:
    """统一读取静态 HTML 文件并附加禁缓存头"""
    html_path = static_dir / filename
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"), headers=_NOCACHE_HEADERS)
    return HTMLResponse(fallback, headers=_NOCACHE_HEADERS)


@app.get("/", response_class=HTMLResponse)
async def index():
    """看板主页"""
    return _serve_html("index.html", "<h1>AI量化周期看板</h1>")


@app.get("/history", response_class=HTMLResponse)
async def history_page():
    """选股记录页"""
    return _serve_html("history.html", "<h1>AI量化周期看板</h1><p>前端文件未找到</p>")


@app.get("/ranking", response_class=HTMLResponse)
async def ranking_page():
    """10日涨幅榜独立页"""
    return _serve_html("ranking.html", "<h1>AI量化周期看板</h1><p>前端文件未找到</p>")


@app.get("/api/cycle")
async def get_cycle_state():
    """获取当前周期状态"""
    state_file = DATA_DIR / "cycle_state.json"
    if state_file.exists():
        return JSONResponse(json.loads(state_file.read_text()))
    return JSONResponse({"phase": "孕育期", "phase_day": 0, "tracked": {}})


@app.get("/api/snapshot")
async def get_latest_snapshot():
    """获取最新周期快照"""
    snapshot_file = DATA_DIR / "latest_snapshot.json"
    if snapshot_file.exists():
        return JSONResponse(json.loads(snapshot_file.read_text()))
    return JSONResponse({"date": "", "phase": "孕育期", "candidates": []})


@app.get("/api/screener")
async def get_screener_result():
    """获取最新选股结果"""
    result_file = DATA_DIR / "latest_screener.json"
    if result_file.exists():
        return JSONResponse(json.loads(result_file.read_text()))
    return JSONResponse({"date": "", "hits": []})


@app.get("/api/leader")
async def get_leader_feedback():
    """获取高标龙头竞价反馈"""
    leader_file = DATA_DIR / "latest_leader.json"
    if leader_file.exists():
        return JSONResponse(json.loads(leader_file.read_text()))
    return JSONResponse({"signal": "未知", "can_trade": True})


_DAILY_ADVICE_PLACEHOLDER = {
    "generated_at": "",
    "bucket": "go",
    "text": "— 数据加载中 —",
    "suggested_position": "—",
    "suggested_position_short": "—",
    "reason": "",
    "bad_count": 0,
    "dimensions": {"ld_bad": False, "drop_bad": False, "w_bad": False, "lb_bad": False},
    "inputs": {},
}


@app.get("/api/daily-advice")
async def get_daily_advice():
    """9:27 决策快照 (decision-consistency-2.1)

    看板与邮件共同读这一份 latest_advice.json (单一真源)。
    文件不存在 / 损坏 → 返回占位响应（前端"数据加载中"分支视觉等价）。
    """
    advice_file = DATA_DIR / "latest_advice.json"
    if not advice_file.exists():
        return JSONResponse(dict(_DAILY_ADVICE_PLACEHOLDER))
    try:
        return JSONResponse(json.loads(advice_file.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"[决策快照] 读取失败: {e}")
        return JSONResponse(dict(_DAILY_ADVICE_PLACEHOLDER))


@app.get("/api/signals")
async def get_signals():
    """获取交叉验证信号"""
    signal_file = DATA_DIR / "latest_signals.json"
    if signal_file.exists():
        return JSONResponse(json.loads(signal_file.read_text()))
    return JSONResponse({"date": "", "signals": []})


@app.get("/api/gain-ranking")
async def get_gain_ranking():
    """获取10日涨幅排行"""
    ranking_file = DATA_DIR / "latest_ranking.json"
    if ranking_file.exists():
        return JSONResponse(json.loads(ranking_file.read_text()))
    return JSONResponse({"date": "", "ranking": []})


@app.get("/api/ranking-live")
async def get_ranking_live():
    """实时刷新 TOP30 涨幅并重新排序

    从 latest_ranking.json 读取基准数据，调腾讯接口拉实时行情，
    用 live close 重算 gain_10d，按 gain_10d 降序重排。
    """
    ranking_file = DATA_DIR / "latest_ranking.json"
    if not ranking_file.exists():
        return JSONResponse({"date": "", "ranking": [], "live": False})

    data = json.loads(ranking_file.read_text())
    items = data.get("ranking", [])
    if not items:
        return JSONResponse({**data, "live": False})

    codes = [str(r["code"]) for r in items]
    base_map = {}
    for r in items:
        gain = float(r.get("gain_10d", 0))
        close = float(r.get("close", 0))
        close_10d_ago = close / (1 + gain / 100) if gain != -100 and close > 0 else 0
        base_map[str(r["code"])] = {**r, "_close_10d_ago": close_10d_ago}

    # 实时数据：腾讯→新浪兜底
    live_map = {}
    try:
        from src.data.tencent_api import fetch_stock_details
        live = fetch_stock_details(codes)
        if live is not None and not live.empty:
            live_map = {str(row["code"]): row for _, row in live.iterrows()}
    except Exception:
        pass

    # 腾讯拿不到时用新浪兜底
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
        except Exception:
            pass

    if live_map:
        for code, base in base_map.items():
            lr = live_map.get(code)
            if lr is None:
                continue
            lc = float(lr.get("close", 0))
            if lc <= 0:
                continue
            base["close"] = round(lc, 2)
            base["change_pct"] = round(float(lr.get("change_pct", 0)), 2)
            c10 = base["_close_10d_ago"]
            if c10 > 0:
                base["gain_10d"] = round((lc / c10 - 1) * 100, 2)
            mc = float(lr.get("market_cap_yi", 0))
            if mc > 0:
                base["market_cap_yi"] = round(mc, 2)

    result = sorted(base_map.values(), key=lambda x: x.get("gain_10d", 0), reverse=True)
    for i, r in enumerate(result):
        r["rank"] = i + 1
        r.pop("_close_10d_ago", None)

    return JSONResponse({
        "date": data.get("date", ""),
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "ranking": result,
        "live": True,
    })


@app.get("/api/sentiment")
async def get_pool_sentiment():
    """获取 top30 梯队情绪（竞价分布判定）"""
    p = DATA_DIR / "latest_sentiment.json"
    if p.exists():
        return JSONResponse(json.loads(p.read_text()))
    return JSONResponse({})


@app.get("/api/history")
async def get_cycle_history():
    """获取周期历史时间线"""
    history_file = DATA_DIR / "cycle_history.json"
    if history_file.exists():
        data = json.loads(history_file.read_text())
        return JSONResponse(data)
    return JSONResponse([])


@app.post("/api/refresh-cycle")
async def refresh_cycle():
    """手动触发周期引擎更新"""
    try:
        from src.scheduler import run_cycle_update
        result = run_cycle_update()
        return JSONResponse({"status": "ok", "snapshot": result})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/refresh-screener")
async def refresh_screener():
    """手动触发选股引擎"""
    try:
        from src.scheduler import run_screener_update
        result = run_screener_update()
        return JSONResponse({"status": "ok", "result": result})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/deviation")
async def get_deviation():
    """获取240周线偏离度数据"""
    dev_file = DATA_DIR / "latest_deviation.json"
    if dev_file.exists():
        return JSONResponse(json.loads(dev_file.read_text()))
    return JSONResponse({"date": "", "results": []})


# ========== 复盘 + 决策卡 + 决策追踪 ==========

@app.get("/review", response_class=HTMLResponse)
async def review_page():
    """复盘页面"""
    return _serve_html("review.html", "<h1>复盘</h1>")


@app.get("/api/review")
async def get_review():
    """获取复盘数据。

    时间门控（用户要求）：当天 15:00 前显示昨日复盘，15:00 后显示今日。
    实现：
      · now < 15:00：从 review_history.json 取最近一条 date != today 的记录
      · now >= 15:00：返回 latest_review.json（今日 cron 在 15:45 跑完）
    无论哪种情况，watch_pool（次日观察池）始终用 latest_ranking 重算 —
    "次日观察池"是面向 next-day 的语义，应反映最新已知的 ranking 状态。
    """
    from src.config import now_cn
    from src.engine.daily_review import build_watch_pool_from_ranking

    latest_file = DATA_DIR / "latest_review.json"
    history_file = DATA_DIR / "review_history.json"
    ranking_file = DATA_DIR / "latest_ranking.json"

    n = now_cn()
    today_str = n.strftime("%Y-%m-%d")
    cutoff = n.replace(hour=15, minute=0, second=0, microsecond=0)

    review_data: dict | None = None

    # 15:00 前 → 优先返回 history 中最近一条非今日的记录
    if n < cutoff and history_file.exists():
        try:
            hist = json.loads(history_file.read_text())
            if isinstance(hist, list):
                for entry in reversed(hist):
                    d = str(entry.get("date") or "")[:10]
                    if d and d != today_str:
                        review_data = entry
                        break
        except Exception:
            pass

    # 15:00 后 或 history 不可用 → latest_review.json
    if review_data is None and latest_file.exists():
        try:
            review_data = json.loads(latest_file.read_text())
        except Exception:
            review_data = None

    if review_data is None:
        return JSONResponse({})

    review_data = dict(review_data)  # 避免污染历史 entry

    # 整站只取真概念：剔除历史快照中遗留的元标签
    _strip_meta_concepts_inplace(review_data)

    # watch_pool 始终用最新 ranking 重算（按当前规则：top30+45%+≥2连板+主板）
    if ranking_file.exists():
        try:
            ranking_payload = json.loads(ranking_file.read_text())
            ranking_rows = ranking_payload.get("ranking") or []
            review_data["watch_pool"] = build_watch_pool_from_ranking(ranking_rows)
        except Exception:
            pass

    # scorecard / promotion_summary 用当前代码重算（确保历史快照也用新公式 / 标签）
    try:
        from src.engine.daily_review import _build_scorecard, _build_promotion_summary
        prev_groups = review_data.get("prev_board_groups") or []
        review_data["promotion_summary"] = _build_promotion_summary(prev_groups)
        review_data["scorecard"] = _build_scorecard(
            prev_groups,
            review_data.get("relay_env") or {},
            review_data.get("sector_zt_stats") or [],
            int(review_data.get("limit_up_count") or 0),
            concept_zt_stats=review_data.get("concept_zt_stats") or [],
            highest_board=int(review_data.get("highest_board") or 0),
        )
    except Exception:
        pass

    return JSONResponse(review_data)


def _strip_meta_concepts_inplace(review: dict) -> None:
    """对一个 review 字典做就地深度清洗：移除所有元标签。

    范围：
      · concept_zt_stats: 整条 entry 是元标签则丢弃
      · lianban_ladder[i].concepts / .top_concepts
      · prev_board_groups[].promoted/failed[].concepts / .top_concepts
      · watch_pool[].concepts / .top_concepts
      · relay_env.space_board.concepts / .top_concepts
    """
    from src.engine.concept_blacklist import is_meta_concept, filter_concepts

    # concept_zt_stats: 整条丢弃
    czs = review.get("concept_zt_stats")
    if isinstance(czs, list):
        review["concept_zt_stats"] = [c for c in czs if not is_meta_concept(c.get("name", ""))]

    # 通用：清洗一个 stock-like 行
    def clean_row(s):
        if not isinstance(s, dict):
            return
        if isinstance(s.get("concepts"), list):
            s["concepts"] = filter_concepts(s["concepts"])
        if isinstance(s.get("top_concepts"), list):
            s["top_concepts"] = filter_concepts(s["top_concepts"])

    for s in (review.get("lianban_ladder") or []):
        clean_row(s)
    for grp in (review.get("prev_board_groups") or []):
        for s in (grp.get("promoted") or []) + (grp.get("failed") or []):
            clean_row(s)
    for w in (review.get("watch_pool") or []):
        clean_row(w)
    relay = review.get("relay_env") or {}
    for k in ("space_board", "prev_space_board_today"):
        clean_row(relay.get(k) or {})


@app.get("/api/auction-scores")
async def get_auction_scores():
    """获取竞价决策卡"""
    f = DATA_DIR / "latest_auction_scores.json"
    if f.exists():
        return JSONResponse(json.loads(f.read_text()))
    return JSONResponse([])


@app.get("/api/auction-detail/{code}")
async def get_auction_detail(code: str):
    """获取竞价形态详情（买卖5档盘口+竞价分析）"""
    result = {"code": code, "name": "", "bids": [], "asks": [], "summary": ""}
    try:
        # 新浪接口有买卖5档
        from src.data.sina_api import fetch_realtime_batch
        df = fetch_realtime_batch([code])
        if df.empty:
            return JSONResponse(result)

        r = df.iloc[0]
        result["name"] = str(r.get("name", ""))
        result["open"] = float(r.get("open", 0))
        result["close"] = float(r.get("close", 0))
        result["pre_close"] = float(r.get("pre_close", 0))
        result["volume"] = float(r.get("volume", 0))
        result["amount"] = float(r.get("amount", 0))

        # 新浪原始数据有买卖5档，但我们的解析只取了bid1/ask1
        # 用腾讯接口获取完整5档
        from src.data.tencent_api import fetch_stock_details
        tx = fetch_stock_details([code])
        if tx is not None and not tx.empty:
            tr = tx.iloc[0]
            result["close"] = float(tr.get("close", result["close"]))
            result["change_pct"] = float(tr.get("change_pct", 0))
            result["turnover"] = float(tr.get("turnover", 0))
            result["volume_ratio_tx"] = float(tr.get("volume_ratio", 0))
            result["market_cap_yi"] = float(tr.get("market_cap_yi", 0))

        # 竞价分析
        pre = result.get("pre_close", 0)
        opn = result.get("open", 0)
        auction_gain = (opn / pre - 1) * 100 if pre > 0 else 0
        vol = result.get("volume", 0)

        lines = []
        lines.append(f"竞价开盘: {opn} ({auction_gain:+.2f}%)")
        if auction_gain >= 5:
            lines.append("竞价形态: 抢筹型高开，买方积极")
        elif auction_gain >= 3:
            lines.append("竞价形态: 温和高开，有承接")
        elif auction_gain >= 0:
            lines.append("竞价形态: 平开偏弱")
        else:
            lines.append("竞价形态: 低开，卖方压力大")

        vr = result.get("volume_ratio_tx", 0)
        if vr >= 3:
            lines.append(f"量比: {vr:.2f}（放量，资金活跃）")
        elif vr >= 1:
            lines.append(f"量比: {vr:.2f}（正常）")
        else:
            lines.append(f"量比: {vr:.2f}（缩量）")

        result["auction_gain"] = round(auction_gain, 2)
        result["summary"] = "\n".join(lines)

        # 集合竞价明细 (ticks) — 用于绘制竞价形态图
        try:
            from src.data.auction_api import fetch_auction_ticks
            ticks_data = fetch_auction_ticks(code)
            result["ticks_source"] = ticks_data.get("source", "none")
            result["ticks_auction"] = ticks_data.get("auction_window", [])
            result["ticks_intraday"] = ticks_data.get("intraday_window", [])
            result["ticks_open"] = ticks_data.get("open_tick")
        except Exception as e:
            print(f"[auction-detail] ticks 获取失败 {code}: {e}")
            result["ticks_source"] = "none"
            result["ticks_auction"] = []
            result["ticks_intraday"] = []

    except Exception as e:
        result["summary"] = f"获取失败: {e}"

    return JSONResponse(result)


@app.get("/api/decisions")
async def get_decisions():
    """获取决策追踪记录"""
    from src.engine.decision_tracker import get_records, get_stats
    return JSONResponse({"records": get_records(), "stats": get_stats()})


@app.post("/api/decisions/user")
async def update_user_decision(request: dict):
    """用户填写实际操作"""
    from src.engine.decision_tracker import update_user_decision
    result = update_user_decision(
        date=request.get("date", ""),
        action=request.get("action", ""),
        code=request.get("code", ""),
        price=request.get("price", 0),
        position=request.get("position", ""),
        stop_loss=request.get("stop_loss", 0),
        note=request.get("note", ""),
    )
    return JSONResponse(result)


@app.post("/api/review/run")
async def run_review_now():
    """手动触发复盘"""
    try:
        from src.engine.daily_review import run_daily_review
        result = run_daily_review()
        if result:
            from dataclasses import asdict
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "error", "msg": "无涨停数据"})
    except Exception as e:
        return JSONResponse({"status": "error", "msg": str(e)})


@app.get("/api/monthly-report")
async def get_monthly_report(year: int = 0, month: int = 0, force: int = 0):
    """获取月度报告

    - 不传 year/month: 默认返回最新已保存的报告；若无则生成上月
    - 传 year/month: 优先读取本地已保存的 report_YYYY_MM.json，无则生成
    - force=1: 强制重新生成并覆盖本地文件
    """
    from src.engine.monthly_report import generate_monthly_report
    try:
        if force:
            report = generate_monthly_report(year if year > 0 else None, month if month > 0 else None)
            return JSONResponse(report)

        # 未指定年月：取已保存报告中最新的一份
        if year <= 0 or month <= 0:
            files = sorted(DATA_DIR.glob("report_*.json"), reverse=True)
            if files:
                return JSONResponse(json.loads(files[0].read_text(encoding="utf-8")))
            # 无任何已保存报告，按默认逻辑生成上月
            report = generate_monthly_report(None, None)
            return JSONResponse(report)

        # 指定年月：优先读本地
        report_file = DATA_DIR / f"report_{year}_{month:02d}.json"
        if report_file.exists():
            return JSONResponse(json.loads(report_file.read_text(encoding="utf-8")))
        report = generate_monthly_report(year, month)
        return JSONResponse(report)
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/monthly-report/list")
async def list_monthly_reports():
    """已保存的月度报告列表（按年月降序）"""
    items = []
    for f in sorted(DATA_DIR.glob("report_*.json"), reverse=True):
        # 文件名格式：report_YYYY_MM.json
        try:
            parts = f.stem.split("_")
            y, m = int(parts[1]), int(parts[2])
            items.append({"year": y, "month": m, "period": f"{y}年{m}月"})
        except (IndexError, ValueError):
            continue
    return JSONResponse({"items": items})


@app.get("/api/hit-live")
async def get_hit_live(codes: str = ""):
    """选股标的实时行情（价格+涨幅+板块+10日涨幅），3秒轮询"""
    if not codes:
        return JSONResponse({})

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        return JSONResponse({})

    result = {}
    try:
        from src.data.sina_api import fetch_realtime_batch
        df = fetch_realtime_batch(code_list)
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = str(row.get("code", ""))
                pre = float(row.get("pre_close", 0))
                close = float(row.get("close", 0))
                pct = round((close / pre - 1) * 100, 2) if pre > 0 and close > 0 else None
                result[code] = {
                    "price": round(close, 2) if close > 0 else None,
                    "pct": pct,
                }
    except Exception:
        pass

    # 补充板块和10日涨幅（从排行数据）
    try:
        ranking_file = DATA_DIR / "latest_ranking.json"
        if ranking_file.exists():
            rd = json.loads(ranking_file.read_text())
            rank_map = {str(r["code"]): r for r in rd.get("ranking", [])}
            for code in code_list:
                if code in result:
                    r = rank_map.get(code, {})
                    result[code]["industry"] = r.get("industry", "")
                    result[code]["gain_10d"] = r.get("gain_10d")
                elif code in rank_map:
                    r = rank_map[code]
                    result[code] = {
                        "price": r.get("close"),
                        "pct": r.get("change_pct"),
                        "industry": r.get("industry", ""),
                        "gain_10d": r.get("gain_10d"),
                    }
    except Exception:
        pass

    # 板块兜底（industry_cache）
    try:
        ic = DATA_DIR / "industry_cache.json"
        if ic.exists():
            ind_map = json.loads(ic.read_text())
            for code in code_list:
                if code in result and not result[code].get("industry"):
                    result[code]["industry"] = ind_map.get(code, "")
    except Exception:
        pass

    # 不在排行榜的标的：用腾讯补板块市值，用K线算10日涨幅
    missing_10d = [c for c in code_list if c in result and result[c].get("gain_10d") is None]
    if missing_10d:
        # 腾讯补板块
        try:
            from src.data.tencent_api import fetch_stock_details
            tx = fetch_stock_details(missing_10d)
            if tx is not None and not tx.empty:
                for _, row in tx.iterrows():
                    c = str(row["code"])
                    if c in result:
                        if not result[c].get("industry"):
                            # 腾讯没有板块，跳过
                            pass
                        result[c]["market_cap_yi"] = float(row.get("market_cap_yi", 0))
        except Exception:
            pass

        # K线算10日涨幅
        try:
            from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
            from src.config import now_cn as _now
            today_str = _now().strftime("%Y-%m-%d")
            for c in missing_10d:
                if result[c].get("gain_10d") is not None:
                    continue
                df = fetch_kline(c, SCALE_DAILY, datalen=12)
                if df is not None and len(df) >= 2:
                    close_now = float(result[c].get("price", 0)) or float(df.iloc[-1]["close"])
                    last_date = str(df.iloc[-1]["date"])[:10]
                    idx = max(0, len(df) - 11) if last_date == today_str else max(0, len(df) - 10)
                    base = float(df.iloc[idx]["close"])
                    if base > 0:
                        result[c]["gain_10d"] = round((close_now / base - 1) * 100, 2)
        except Exception:
            pass

    return JSONResponse(result)


@app.get("/api/missed-trades")
async def get_missed_trades():
    """获取踏空追踪（系统选出但未参与的标的）"""
    from src.engine.decision_tracker import get_missed_trades
    return JSONResponse({"records": get_missed_trades()})


@app.get("/api/sanbanzhu/{code}")
async def check_sanbanzhu(code: str):
    """检查单只股票是否三板组"""
    from src.engine.sanbanzhu import check_sanbanzhu as _check
    return JSONResponse(_check(code))


@app.get("/api/market-insight")
async def get_market_insight():
    """获取四维市场洞察（板块集中度/资金行为/情绪领袖/周期波形）"""
    insight_file = DATA_DIR / "latest_insight.json"
    if insight_file.exists():
        return JSONResponse(json.loads(insight_file.read_text()))
    return JSONResponse({"date": "", "sector_heats": [], "wave": None})


@app.get("/api/screener-history")
async def get_screener_history(year: int = 0, month: int = 0):
    """获取选股记录+胜率统计

    Query 参数：
        year, month: 联动过滤记录与分维度统计；不传则返回全量
            - total/weekly 始终为全量/实时
            - monthly = 选定 year+month（不传则当前自然月）
            - yearly = 选定 year（不传则当前自然年）
            - by_*（分维度）= 选定 year+month 子集

    每次请求时对今日记录做一次幂等修正（用最新K线和最新 sentiment 校正
    收盘价/10日涨幅/加权竞价等指标，与首页看板对齐）。
    """
    from src.engine.screener_history import (
        get_history, calc_win_stats, refresh_today_records, list_available_periods,
    )
    try:
        refresh_today_records()
    except Exception as e:
        print(f"[选股记录] refresh_today_records 失败: {e}")
    y = year if year > 0 else None
    m = month if month > 0 else None
    return JSONResponse({
        "records": get_history(limit=0, year=y, month=m),
        "stats": calc_win_stats(filter_year=y, filter_month=m),
        "available_periods": list_available_periods(),
    })


# ========== 自选股模块已移除 ==========

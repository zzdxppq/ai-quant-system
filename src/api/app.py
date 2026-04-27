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


@app.get("/", response_class=HTMLResponse)
async def index():
    """看板主页"""
    html_path = static_dir / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>AI量化���期看板</h1>"


@app.get("/history", response_class=HTMLResponse)
async def history_page():
    """选股记录页"""
    html_path = static_dir / "history.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>AI量化周期看板</h1><p>前端文件未找到</p>"


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
    html_path = static_dir / "review.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>复盘</h1>"


@app.get("/api/review")
async def get_review():
    """获取最新复盘数据"""
    f = DATA_DIR / "latest_review.json"
    if f.exists():
        return JSONResponse(json.loads(f.read_text()))
    return JSONResponse({})


@app.get("/api/auction-scores")
async def get_auction_scores():
    """获取竞价决策卡"""
    f = DATA_DIR / "latest_auction_scores.json"
    if f.exists():
        return JSONResponse(json.loads(f.read_text()))
    return JSONResponse([])


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


@app.get("/api/market-insight")
async def get_market_insight():
    """获取四维市场洞察（板块集中度/资金行为/情绪领袖/周期波形）"""
    insight_file = DATA_DIR / "latest_insight.json"
    if insight_file.exists():
        return JSONResponse(json.loads(insight_file.read_text()))
    return JSONResponse({"date": "", "sector_heats": [], "wave": None})


@app.get("/api/screener-history")
async def get_screener_history():
    """获取选股记录+胜率统计"""
    from src.engine.screener_history import get_history, calc_win_stats
    return JSONResponse({
        "records": get_history(limit=500),
        "stats": calc_win_stats(),
    })


# ========== 自选股 API ==========

@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page():
    """自选股页面"""
    html_path = static_dir / "watchlist.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>自选股</h1>"


@app.get("/api/watchlist")
async def api_get_watchlist():
    """获取自选股列表+预测数据+实时行情"""
    from src.engine.watchlist import get_watchlist, get_all_predictions
    items = get_watchlist()
    predictions = get_all_predictions()

    # 用腾讯接口补充实时行情
    if items:
        try:
            from src.data.tencent_api import fetch_stock_details
            codes = [item["code"] for item in items]
            live_df = fetch_stock_details(codes)
            if live_df is not None and not live_df.empty:
                live_map = {str(row["code"]): row for _, row in live_df.iterrows()}
                for item in items:
                    row = live_map.get(item["code"])
                    if row is not None:
                        item["close"] = float(row.get("close", 0))
                        item["change_pct"] = float(row.get("change_pct", 0))
                        item["market_cap_yi"] = float(row.get("market_cap_yi", 0))
        except Exception:
            pass

        # 补充10日涨幅（从排行数据）
        try:
            ranking_file = DATA_DIR / "latest_ranking.json"
            if ranking_file.exists():
                ranking_data = json.loads(ranking_file.read_text())
                gain_map = {str(r["code"]): r.get("gain_10d", 0) for r in ranking_data.get("ranking", [])}
                for item in items:
                    item["gain_10d"] = gain_map.get(item["code"], None)
        except Exception:
            pass

    # 合并预测到列表
    for item in items:
        pred = predictions.get(item["code"])
        if pred:
            item["trend"] = pred.get("trend", "")
            item["pred_gain"] = pred.get("pred_gain", 0)
            item["confidence"] = pred.get("confidence", "")
            item["predicted_at"] = pred.get("predicted_at", "")
    return JSONResponse({"items": items, "predictions": predictions})


@app.post("/api/watchlist/add")
async def api_add_watchlist(request: dict):
    """添加自选股"""
    from src.engine.watchlist import add_to_watchlist
    code = request.get("code", "")
    name = request.get("name", "")
    result = add_to_watchlist(code, name)
    return JSONResponse(result)


@app.post("/api/watchlist/remove")
async def api_remove_watchlist(request: dict):
    """删除自选股"""
    from src.engine.watchlist import remove_from_watchlist
    code = request.get("code", "")
    result = remove_from_watchlist(code)
    return JSONResponse(result)


@app.get("/api/watchlist/search")
async def api_search_stocks(q: str = ""):
    """搜索股票"""
    from src.engine.watchlist import search_stocks
    results = search_stocks(q)
    return JSONResponse({"results": results})


@app.get("/api/watchlist/prediction/{code}")
async def api_get_prediction(code: str):
    """获取单只股票预测详情"""
    from src.engine.watchlist import get_prediction
    pred = get_prediction(code)
    if pred:
        return JSONResponse(pred)
    return JSONResponse({"code": code, "trend": "", "predictions": []})


@app.post("/api/watchlist/predict/{code}")
async def api_trigger_prediction(code: str):
    """手动触发单只股票预测"""
    from src.engine.watchlist import _trigger_prediction_async
    _trigger_prediction_async(code)
    return JSONResponse({"ok": True, "msg": f"{code} 预测已触发"})


@app.post("/api/watchlist/predict-all")
async def api_predict_all():
    """手动触发全部自选股预测"""
    import threading
    from src.engine.watchlist import run_all_predictions
    threading.Thread(target=run_all_predictions, daemon=True).start()
    return JSONResponse({"ok": True, "msg": "全部预测已触发"})

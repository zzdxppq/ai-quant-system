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


@app.get("/ranking", response_class=HTMLResponse)
async def ranking_page():
    """10日涨幅榜独立页"""
    html_path = static_dir / "ranking.html"
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


# ========== 自选股模块已移除 ==========

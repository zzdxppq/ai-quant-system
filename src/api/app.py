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

    try:
        from src.data.tencent_api import fetch_stock_details
        live = fetch_stock_details(codes)
    except Exception as e:
        return JSONResponse({**data, "live": False, "error": str(e)})

    if live is not None and not live.empty:
        live_map = {str(row["code"]): row for _, row in live.iterrows()}
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


@app.get("/api/market-insight")
async def get_market_insight():
    """获取四维市场洞察（板块集中度/资金行为/情绪领袖/周期波形）"""
    insight_file = DATA_DIR / "latest_insight.json"
    if insight_file.exists():
        return JSONResponse(json.loads(insight_file.read_text()))
    return JSONResponse({"date": "", "sector_heats": [], "wave": None})


@app.post("/api/backtest")
async def run_backtest():
    """运行回测（模拟数据）"""
    try:
        from src.engine.backtest import run_backtest_mock
        from dataclasses import asdict
        result = run_backtest_mock()
        return JSONResponse({"status": "ok", "result": asdict(result)})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

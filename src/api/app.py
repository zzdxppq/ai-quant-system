"""FastAPI 后端"""
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

from src.config import BASE_DIR, DATA_DIR
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

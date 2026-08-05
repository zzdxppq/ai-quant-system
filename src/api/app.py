"""FastAPI 后端"""
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json
import threading

from fastapi import BackgroundTasks, Body, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from src.config import APP_ROOT_PATH, BASE_DIR, DATA_DIR, is_trading_day, now_cn


async def run_in_heavy_pool(fn, /, *args, **kwargs):
    """线程池 + 全局重 IO 锁：避免 DuckDB/行情库在多 worker 并发时原生崩溃 (Windows 0xC0000409)。"""
    from src.market_schedule import run_with_heavy_market_lock

    def _job():
        return run_with_heavy_market_lock(lambda: fn(*args, **kwargs))

    return await run_in_threadpool(_job)
from src.data.json_io import data_dir_glob_json, dump_json_file, load_json_file
from src.engine.cycle import CycleEngine, CycleSnapshot, calc_gain_10d
from src.engine.screener import run_screener, ScreenerHit
from src.engine.cross_validator import cross_validate


def _startup_stock_basic_background() -> None:
    """启动后后台线程：必要时拉全市场列表写入 DuckDB `stock_basic`（不阻塞 uvicorn 监听）。"""
    import os
    import time

    flag = os.getenv("STOCK_BASIC_STARTUP_SYNC", "0").strip().lower()
    if flag in ("0", "false", "no", "off", ""):
        print("[stock-basic] 启动全量同步已关闭（STOCK_BASIC_STARTUP_SYNC=0，看板可手动拉取）")
        return

    # 等服务就绪、首屏 API 先响应，再抢网络/库锁
    time.sleep(15)
    try:
        from src.data.structured_store import load_stock_basic_df_if_fresh, stock_basic_stats
        from src.data.stock_search import refresh_universe_table
        from src.market_schedule import HEAVY_MARKET_NETWORK_LOCK

        st = stock_basic_stats()
        if int(st.get("count") or 0) >= 2500:
            df = load_stock_basic_df_if_fresh(min_rows=2500, max_age_sec=6 * 3600)
            if df is not None:
                print(
                    f"[stock-basic] 启动：本地表 {st.get('count')} 条且 6h 内有效，跳过全量拉网"
                )
                return
        with HEAVY_MARKET_NETWORK_LOCK:
            out = refresh_universe_table()
        print(
            f"[stock-basic] 启动后台同步完成 rows={out.get('rows')} inserted={out.get('inserted')}"
        )
    except Exception as e:
        print(f"[stock-basic] 启动后台同步异常: {e}")


def _startup_advice_refresh_background() -> None:
    """启动后可选后台刷新决策快照（默认关闭，见 STARTUP_ADVICE_REFRESH）。"""
    import time

    from src.market_schedule import (
        HEAVY_MARKET_NETWORK_LOCK,
        startup_advice_refresh_enabled,
    )

    if not startup_advice_refresh_enabled():
        print("[决策快照] 启动后台刷新已关闭（STARTUP_ADVICE_REFRESH=0）")
        return

    time.sleep(20)
    try:
        with HEAVY_MARKET_NETWORK_LOCK:
            out = _refresh_advice_sync()
        if out is None:
            from src.notify.email_sender import last_advice_write_skip_reason

            reason = last_advice_write_skip_reason() or "unknown"
            print(f"[决策快照] 启动后台刷新跳过: {reason}")
        else:
            part = (out.get("dashboard") or {}).get("participate") or {}
            print(
                f"[决策快照] 启动后台刷新完成 bucket={out.get('bucket')} "
                f"relay={part.get('relay_decision_index')} b1={part.get('b1_rate')}"
            )
    except Exception as e:
        print(f"[决策快照] 启动后台刷新异常: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 鉴权启动提示
    try:
        from src.api.auth import log_startup_banner, start_purge_daemon

        log_startup_banner()
        start_purge_daemon()
    except Exception as e:
        print(f"[AUTH] 启动钩子异常: {e}", flush=True)
    threading.Thread(
        target=_startup_stock_basic_background,
        name="stock-basic-sync",
        daemon=True,
    ).start()
    threading.Thread(
        target=_startup_advice_refresh_background,
        name="advice-refresh-startup",
        daemon=True,
    ).start()
    yield


app = FastAPI(title="AI量化周期看板", version="1.0.0", lifespan=lifespan)

# 鉴权 router + 中间件（auth-1.0）
from src.api.auth import (
    get_user_for_request as _get_user_for_request,
    is_whitelisted_path as _is_whitelisted_path,
    router as auth_router,
)  # noqa: E402

app.include_router(auth_router)


@app.middleware("http")
async def _auth_middleware(request, call_next):
    """全局鉴权：白名单放行；其余路径必须带有效 Bearer token（401）。"""
    try:
        from src.api.auth import _auth_required

        if not _auth_required():
            return await call_next(request)
    except Exception:
        return await call_next(request)

    path = request.url.path
    if _is_whitelisted_path(path):
        return await call_next(request)

    user = _get_user_for_request(request)
    if user is None:
        from fastapi.responses import JSONResponse as _JR

        return _JR({"error": "unauthorized"}, status_code=401)
    # 把 user 挂到 request.state 供下游 handler 读
    request.state.user = user
    return await call_next(request)


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


_APP_ROOT_BOOTSTRAP = r"""<script id="app-root-bootstrap">
(function () {{
  var r = {env_root};
  if (!r) {{
    var p = location.pathname;
    if (p === "/quant" || p.indexOf("/quant/") === 0) r = "/quant";
  }}
  window.APP_ROOT = r;
  function appPath(path) {{
    if (!path) path = "/";
    if (path.charAt(0) !== "/") path = "/" + path;
    if (!r) return path;
    if (path === "/") return r + "/";
    return r + path;
  }}
  function withRoot(url) {{
    if (typeof url !== "string" || !url || url.charAt(0) !== "/") return url;
    var iQ = url.indexOf("?"), iH = url.indexOf("#"), end = url.length;
    if (iQ >= 0) end = Math.min(end, iQ);
    if (iH >= 0) end = Math.min(end, iH);
    return appPath(url.slice(0, end)) + url.slice(end);
  }}
  window.appPath = appPath;
  var _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {{
    init = init || {{}};
    init.headers = init.headers || {{}};
    try {{
      var t = localStorage.getItem("auth_token");
      if (t) {{
        var u = (typeof input === "string") ? input : (input && input.url) || "";
        if (!/^https?:\/\//i.test(u)) {{
          init.headers["Authorization"] = "Bearer " + t;
        }}
      }}
    }} catch (e) {{ /* localStorage 不可用时静默 */ }}
    var _doFetch = (typeof input === "string") ? function() {{ return _fetch(withRoot(input), init); }} : function() {{ return _fetch(input, init); }};
    return _doFetch().then(function (resp) {{
      try {{
        if (resp.status === 401) {{
          var u2 = (typeof input === "string") ? input : (input && input.url) || "";
          if (u2.indexOf("/api/auth/") < 0) {{
            try {{
              localStorage.removeItem("auth_token");
              localStorage.removeItem("auth_email");
              localStorage.removeItem("auth_expires");
            }} catch (e2) {{}}
            var loginPath = (window.appPath ? window.appPath("/login") : "/login");
            if (location.pathname.replace(/\/+$/, "") !== loginPath.replace(/\/+$/, "")) {{
              location.replace(loginPath);
            }}
          }}
        }}
      }} catch (e3) {{}}
      return resp;
    }});
  }};
  function fixLinks() {{
    document.querySelectorAll('a[href^="/"]').forEach(function (a) {{
      var h = a.getAttribute("href"), nh = withRoot(h);
      if (nh !== h) a.setAttribute("href", nh);
    }});
  }}
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fixLinks);
  else fixLinks();
}})();
</script>"""


def _resolve_app_root(request: Request | None) -> str:
    root = APP_ROOT_PATH
    if request is not None:
        prefix = (request.headers.get("x-forwarded-prefix") or "").strip().rstrip("/")
        if prefix:
            root = prefix
    return root


def _inject_app_root(html: str, request: Request | None = None) -> str:
    snippet = _APP_ROOT_BOOTSTRAP.format(env_root=json.dumps(_resolve_app_root(request)))
    if "<head>" in html:
        return html.replace("<head>", "<head>\n" + snippet, 1)
    return snippet + html


def _serve_html(filename: str, fallback: str, request: Request | None = None) -> HTMLResponse:
    """统一读取静态 HTML 文件并附加禁缓存头"""
    html_path = static_dir / filename
    if html_path.exists():
        body = _inject_app_root(html_path.read_text(encoding="utf-8"), request)
        return HTMLResponse(body, headers=_NOCACHE_HEADERS)
    return HTMLResponse(_inject_app_root(fallback, request), headers=_NOCACHE_HEADERS)


def _scorecard_b1_concentration_from_review(review: dict | None) -> tuple[float | None, float | None]:
    """从复盘 payload 的 scorecard.indicators 解析 1进2成功率、板块集中度。"""
    if not isinstance(review, dict):
        return None, None
    sc = review.get("scorecard") or {}
    b1: float | None = None
    conc: float | None = None
    for ind in sc.get("indicators") or []:
        if not isinstance(ind, dict):
            continue
        lbl = ind.get("label", "")
        raw = ind.get("raw")
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if lbl == "1进2成功率":
            b1 = val
        elif lbl == "板块集中度":
            conc = val
    return b1, conc


def _latest_review_history_before(cutoff_date: str) -> dict | None:
    """取严格早于 cutoff_date（YYYY-MM-DD）的复盘历史中，日期最新的一条。"""
    from src.engine.screener_market_env import latest_review_history_before

    return latest_review_history_before(cutoff_date)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """看板主页"""
    return _serve_html("index.html", "<h1>AI量化周期看板</h1>", request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页（白名单：middleware 放行）"""
    return _serve_html("login.html", "<h1>请通过 /login 入口登录</h1>", request)


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """选股记录页"""
    return _serve_html("history.html", "<h1>AI量化周期看板</h1><p>前端文件未找到</p>", request)


@app.get("/ranking", response_class=HTMLResponse)
async def ranking_page(request: Request):
    """10日涨幅榜独立页"""
    return _serve_html("ranking.html", "<h1>AI量化周期看板</h1><p>前端文件未找到</p>", request)


@app.get("/api/cycle")
async def get_cycle_state():
    """获取当前周期状态"""
    state_file = DATA_DIR / "cycle_state.json"
    data = load_json_file(state_file)
    if data is None:
        return JSONResponse({"phase": "孕育期", "phase_day": 0, "tracked": {}})
    return JSONResponse(data)


@app.get("/api/market-session")
async def get_market_session():
    """看板轮询策略：盘内才拉 hit-live / screener-history?light=1 写库；EOD 期间 light 只读。"""
    from src.config import is_trading_day
    from src.market_schedule import (
        allow_dashboard_live_poll,
        allow_minute_series_refresh,
        allow_screener_history_light_write,
        is_eod_bundle_running,
        is_intraday_trading_session,
        is_post_market_data_window,
    )

    intraday = is_intraday_trading_session()
    eod = is_eod_bundle_running()
    return JSONResponse({
        "trading_day": is_trading_day(),
        "intraday": intraday,
        "post_market": is_post_market_data_window(),
        "eod_running": eod,
        "poll_live": allow_dashboard_live_poll(),
        "poll_minute": allow_minute_series_refresh(),
        "poll_screener_history_light": allow_screener_history_light_write(),
    })


@app.get("/api/snapshot")
async def get_latest_snapshot():
    """获取最新周期快照"""
    snapshot_file = DATA_DIR / "latest_snapshot.json"
    data = load_json_file(snapshot_file)
    if data is None:
        return JSONResponse({"date": "", "phase": "孕育期", "candidates": []})
    if not isinstance(data, dict):
        return JSONResponse({"date": "", "phase": "孕育期", "candidates": []})
    return JSONResponse(_sanitize_json(data))


@app.get("/api/screener")
async def get_screener_result():
    """获取最新选股结果"""
    result_file = DATA_DIR / "latest_screener.json"
    data = load_json_file(result_file)
    if data is None:
        return JSONResponse({
            "date": "", "hits": [], "data_trade_date": "",
            "is_today": False, "is_trading_day": is_trading_day(),
        })
    hits = data.get("hits") or []
    try:
        await run_in_heavy_pool(_enrich_screener_hits_decisions, hits)
    except Exception:
        pass
    _attach_screener_meta(data)
    return JSONResponse(data)


@app.get("/api/leader")
async def get_leader_feedback():
    """获取高标龙头竞价反馈"""
    leader_file = DATA_DIR / "latest_leader.json"
    data = load_json_file(leader_file)
    if data is None:
        return JSONResponse({"signal": "未知", "can_trade": True})
    return JSONResponse(data)


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

    单一真源 doc_key：`data/latest_advice.json`（逻辑路径）；经 load_json_file 读 quant 库
    quant.duckdb，磁盘可无同名 .json。无数据或解析失败 → 占位（前端等价「数据加载中」）。
    """
    advice_file = DATA_DIR / "latest_advice.json"
    # 落库后会删 .json，不能仅用 exists() 判定（否则永远占位）
    data = load_json_file(advice_file)
    if data is None:
        print(f"[决策快照] 无数据或读取失败: {advice_file}")
        return JSONResponse(dict(_DAILY_ADVICE_PLACEHOLDER))
    return JSONResponse(data)


@app.get("/api/signals")
async def get_signals():
    """获取交叉验证信号"""
    signal_file = DATA_DIR / "latest_signals.json"
    data = load_json_file(signal_file)
    if data is None:
        return JSONResponse({"date": "", "signals": []})
    return JSONResponse(data)


def _kline_stock_name(code: str) -> str:
    name = ""
    for src_file in ("latest_ranking.json", "latest_screener.json"):
        try:
            path = DATA_DIR / src_file
            payload = load_json_file(path)
            if not payload:
                continue
            rows = payload.get("ranking") or payload.get("hits") or []
            for r in rows:
                if str(r.get("code")) == code:
                    name = r.get("name") or ""
                    break
            if name:
                break
        except Exception:
            continue
    if not name:
        try:
            from src.data.analytics_store import load_migrated_snapshot

            for kind in ("latest_screener.json", "latest_ranking.json"):
                payload = load_migrated_snapshot(kind) or {}
                rows = payload.get("hits") or payload.get("ranking") or []
                for r in rows:
                    if str(r.get("code")) == code:
                        name = str(r.get("name") or "")
                        break
                if name:
                    break
        except Exception:
            pass
    return name


def _screener_date_ymd(date_raw: str) -> str:
    digits = "".join(c for c in str(date_raw or "") if c.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _attach_screener_meta(data: dict) -> None:
    ymd = _screener_date_ymd(data.get("date"))
    data["data_trade_date"] = (
        f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}" if len(ymd) == 8 else ""
    )
    today_ymd = now_cn().strftime("%Y%m%d")
    data["is_today"] = bool(ymd and ymd == today_ymd)
    data["is_trading_day"] = is_trading_day()


def _enrich_screener_hits_decisions(hits: list) -> None:
    """补全缺失的 per_stock_decision（仅 GET /api/screener 缺字段时）。"""
    if not hits:
        return
    if not any(isinstance(h, dict) and not h.get("per_stock_decision") for h in hits):
        return
    from src.engine.screener_decision import compute_per_stock_decision
    from src.engine.screener_market_env import load_screener_market_env, load_screener_review_context

    me = load_screener_market_env()
    ctx = load_screener_review_context()
    for h in hits:
        if isinstance(h, dict) and not h.get("per_stock_decision"):
            h["per_stock_decision"] = compute_per_stock_decision(
                h,
                me,
                concept_zt_stats=ctx.get("concept_zt_stats") or [],
                space_board_today=ctx.get("space_board_today"),
                market_highest_board=ctx.get("market_highest_board"),
            )


def _kline_daily_png(code: str, name: str, days: int, refresh: int) -> bytes:
    from src.data.sina_kline_api import (
        fetch_daily_kline_from_cache_only,
        fetch_daily_kline_robust,
    )
    from src.engine.kline_chart import _render_empty, render_kline_chart

    force = int(refresh or 0) != 0
    try:
        if force:
            df = fetch_daily_kline_robust(code, min_bars=35, datalen=500, skip_cache_read=True)
            if df is None or df.empty or len(df) < 35:
                df = fetch_daily_kline_robust(code, min_bars=35, datalen=500, skip_cache_read=False)
        else:
            df = fetch_daily_kline_from_cache_only(code, min_bars=35, datalen=500)
    except Exception as e:
        return _render_empty(code, name, f"日K拉取失败: {e!s}"[:200])
    if df is None or df.empty or len(df) < 35:
        hint = "正在后台拉取行情…" if not force else "暂无足够日K数据（行情源未返回满 35 根）"
        return _render_empty(code, name, hint)
    return render_kline_chart(code, name, df, days=max(20, min(180, int(days))))


def _kline_weekly_png(code: str, name: str, weeks: int, refresh: int = 0) -> bytes:
    from src.data.sina_kline_api import (
        fetch_weekly_kline_from_cache_only,
        fetch_weekly_kline_unified,
    )
    from src.engine.kline_chart import _render_empty, render_weekly_kline_chart

    force = int(refresh or 0) != 0
    try:
        if force:
            df = fetch_weekly_kline_unified(code, datalen=500)
        else:
            df = fetch_weekly_kline_from_cache_only(code, datalen=500)
    except Exception as e:
        return _render_empty(code, name, f"周K拉取失败: {e!s}"[:200])
    if df is None or df.empty:
        hint = "正在后台拉取行情…" if not force else "暂无周K数据（新浪/东财均无周K，且日K无法聚合）"
        return _render_empty(code, name, hint)
    return render_weekly_kline_chart(code, name, df, weeks=max(40, min(200, int(weeks))))


_KLINE_PNG_HEADERS = {"Cache-Control": "private, no-cache"}


@app.get("/api/kline/{code}")
async def get_kline_chart(
    code: str,
    days: int = 60,
    period: str = "daily",
    weeks: int = 120,
    refresh: int = 0,
):
    """K 线图 PNG

    - period=daily（默认）：日 K + 通达信防线 + 形态
    - period=weekly：周 K + MA5/10/21/240

    Args:
        code: 6 位股票代码
        days: 日 K 显示最近 N 个交易日（20–180）
        weeks: 周 K 显示最近 N 根周 K（40–200）
    Returns:
        image/png（无数据或拉取失败时仍返回占位 PNG，避免 <img> 收到 JSON 404）
    """
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return JSONResponse({"error": "invalid code"}, status_code=400)

    name = _kline_stock_name(code)
    period_l = (period or "daily").strip().lower()

    if period_l == "weekly":
        png = await run_in_heavy_pool(_kline_weekly_png, code, name, weeks, refresh)
    else:
        png = await run_in_heavy_pool(_kline_daily_png, code, name, days, refresh)

    return Response(content=png, media_type="image/png", headers=_KLINE_PNG_HEADERS)


@app.get("/api/kline-weekly/{code}")
async def get_kline_weekly_chart(code: str, weeks: int = 120, refresh: int = 0):
    """周 K PNG（独立路径，避免部分代理/浏览器忽略 query 导致与日 K 混淆）。"""
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return JSONResponse({"error": "invalid code"}, status_code=400)
    name = _kline_stock_name(code)
    png = await run_in_heavy_pool(_kline_weekly_png, code, name, weeks, refresh)
    return Response(content=png, media_type="image/png", headers=_KLINE_PNG_HEADERS)


@app.get("/api/minute/{code}")
async def get_minute_series(code: str, refresh: int = 0):
    """分时 JSON：默认先返库内缓存（秒开）；refresh=1 时强制拉腾讯更新。

    响应含 served_from_cache（是否仅用库内数据）、refresh_error（回退时记录本次拉网失败原因）。
    网络成功且有条时仍会 replace_minute_series 写入结构化表（minute_kline 等）。
    """
    from src.data.kline_file_cache import normalize_code6

    c6 = normalize_code6((code or "").strip())
    if len(c6) != 6 or not c6.isdigit():
        return JSONResponse({"error": "invalid code", "bars": []}, status_code=400)

    try:
        return await _get_minute_series_impl(c6, int(refresh or 0) != 0)
    except Exception as e:
        print(f"[minute] api error {c6}: {e}")
        try:
            from src.data.quant_db import reset_shared_connection

            reset_shared_connection()
        except Exception:
            pass
        return JSONResponse(
            {"code": c6, "bars": [], "error": f"minute api: {e}"},
            status_code=200,
        )


async def _get_minute_series_impl(c6: str, force: bool) -> JSONResponse:
    from src.data.structured_store import replace_minute_series
    from src.data.tencent_minute_api import (
        _minute_cache_usable,
        load_latest_minute_store,
        resolve_minute_payload_cached_then_network,
    )
    from src.market_schedule import allow_minute_series_refresh, is_eod_bundle_running

    def _patch_pre_close(payload: dict) -> dict:
        """库内分时可能带陈旧昨收，返回前用新浪/多源日 K 校正涨幅基准。"""
        from src.data.sina_kline_api import resolve_prev_close
        from src.engine.minute_signals import (
            SIGNAL_GROUPS,
            SIGNAL_LEGEND,
            compute_minute_signals,
            default_signal_filter,
        )

        out = dict(payload)
        out["signal_legend"] = SIGNAL_LEGEND
        out["signal_groups"] = SIGNAL_GROUPS
        out["signal_filter_default"] = default_signal_filter()
        pc = resolve_prev_close(c6)
        if pc <= 0:
            out.setdefault("signals", [])
            return out
        out["pre_close"] = round(pc, 4)
        bars = out.get("bars")
        if isinstance(bars, list):
            for b in bars:
                if not isinstance(b, dict):
                    continue
                p = float(b.get("p") or 0)
                if p > 0:
                    b["pct"] = round(((p / pc) - 1.0) * 100.0, 3)
                    ap = float(b.get("avg") or p)
                    b["avg_pct"] = round(((ap / pc) - 1.0) * 100.0, 3)
            out["signals"] = compute_minute_signals(bars, pre_close=pc)
        else:
            out["signals"] = []
        return out

    def _cached_response(cached: dict, *, skipped: str | None = None) -> JSONResponse:
        out = _patch_pre_close(dict(cached))
        out["served_from_cache"] = True
        out["refresh_error"] = None
        if skipped:
            out["refresh_skipped"] = skipped
        return JSONResponse(out)

    if not force:
        cached = load_latest_minute_store(c6)
        if _minute_cache_usable(cached):
            return _cached_response(cached)

    if not allow_minute_series_refresh():
        cached = load_latest_minute_store(c6)
        if _minute_cache_usable(cached):
            skip = "eod_running" if is_eod_bundle_running() else "post_market"
            return _cached_response(cached, skipped=skip)
        return JSONResponse({
            "code": c6,
            "bars": [],
            "error": "盘外仅展示库内分时缓存（无缓存时不拉网）",
            "served_from_cache": False,
            "refresh_skipped": "post_market",
        })

    payload = await run_in_heavy_pool(
        resolve_minute_payload_cached_then_network, c6, force_refresh=force
    )

    if payload.get("bars"):
        payload = _patch_pre_close(payload)
        if not payload.get("served_from_cache"):
            try:
                replace_minute_series(payload)
            except Exception as e:
                print(f"[minute] quant save failed {c6}: {e}")
        return JSONResponse(payload)

    out: dict = dict(payload) if isinstance(payload, dict) else {"code": c6}
    out.setdefault("bars", [])
    if not out.get("error"):
        out["error"] = "no minute data"
    return JSONResponse(out, status_code=200)


@app.get("/api/kline-analysis/{code}")
async def get_kline_analysis(code: str, refresh: int = 0):
    """单股操作建议 JSON：双线突破判定 + 仓位 + 买入条件 + 止损 + 一句话

    日 K 不足或拉取失败时仍返回 200 + analyze_stock_action 占位（避免 JSON 404）。
    """
    import pandas as pd

    from src.data.sina_kline_api import (
        fetch_daily_kline_from_cache_only,
        fetch_daily_kline_robust,
    )
    from src.engine.kline_chart import analyze_stock_action
    from src.engine.screener_market_env import (
        build_kline_analysis_market_ctx,
        find_hit_in_latest_screener,
    )

    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return JSONResponse({"error": "invalid code"}, status_code=400)

    def _build_analysis():
        force = int(refresh or 0) != 0
        served_from_cache = False
        try:
            if force:
                df = fetch_daily_kline_robust(code, min_bars=35, datalen=500, skip_cache_read=True)
                if df is None or df.empty or len(df) < 35:
                    df = fetch_daily_kline_robust(code, min_bars=35, datalen=500, skip_cache_read=False)
            else:
                df = fetch_daily_kline_from_cache_only(code, min_bars=35, datalen=500)
                served_from_cache = df is not None and not df.empty and len(df) >= 35
        except Exception as e:
            nm = _kline_stock_name(code) or code
            out = analyze_stock_action(code, nm, pd.DataFrame(), market_ctx={}, auction_ctx={})
            out["fetch_error"] = str(e)
            out["served_from_cache"] = False
            return out

        if df is None or df.empty or len(df) < 35:
            nm = _kline_stock_name(code) or code
            mctx = build_kline_analysis_market_ctx(code)
            out = analyze_stock_action(
                code, nm, df if df is not None else pd.DataFrame(), market_ctx=mctx, auction_ctx={}
            )
            out["served_from_cache"] = False
            out["cache_miss"] = not force
            return out

        hit = find_hit_in_latest_screener(code)
        name = str(hit.get("name") or _kline_stock_name(code) or code)
        auction_ctx: dict = {}
        if hit:
            if hit.get("open_price") is not None:
                auction_ctx["open_price"] = hit["open_price"]
            if hit.get("auction_gain") is not None:
                auction_ctx["auction_gain"] = hit["auction_gain"]
            if hit.get("auction_volume_lots") is not None:
                auction_ctx["auction_volume_lots"] = hit["auction_volume_lots"]
            if hit.get("auction_turnover") is not None:
                auction_ctx["auction_turnover"] = hit["auction_turnover"]
            if hit.get("auction_volume_ratio") is not None:
                auction_ctx["auction_volume_ratio"] = hit["auction_volume_ratio"]
            auction_ctx["hit_dict"] = dict(hit)

        market_ctx = build_kline_analysis_market_ctx(code)
        out = analyze_stock_action(code, name, df, market_ctx, auction_ctx=auction_ctx)
        if out.get("data_source") != "auction":
            try:
                from src.data.sina_api import fetch_realtime_batch

                rt = fetch_realtime_batch([code])
                if rt is not None and not rt.empty:
                    row = rt.iloc[0]
                    pre = float(row.get("pre_close", 0) or 0)
                    px = float(row.get("close", 0) or 0)
                    vol = float(row.get("volume", 0) or 0)
                    if pre > 0 and px > 0:
                        out["price"] = round(px, 2)
                        out["gain_pct"] = round((px / pre - 1) * 100, 2)
                        out["volume_lots"] = round(vol / 100, 0)
                        out["data_source"] = "spot"
            except Exception:
                pass
        out["served_from_cache"] = served_from_cache
        return out

    result = await run_in_heavy_pool(_build_analysis)
    return JSONResponse(result)


@app.get("/api/stock-search")
async def stock_search(q: str = "", limit: int = 30):
    """全市场代码 / 名称关键字搜索（新浪全市场列表 + 内存缓存）"""
    from src.data.stock_search import search_stocks

    items = search_stocks(q, limit=limit)
    return JSONResponse({"items": items})


@app.get("/api/stock-fundamentals")
async def stock_fundamentals(code: str = ""):
    """个股基本面画像：MX 妙想 API 一次拉 PE/PB/市值/行业/主营业务等。

    返回 {available, code, name, source, fields, error}
    - available=false: MX_APIKEY 未配置 / MX 不可用
    - fields: dict 形式的中文 label → 原始值（最新一列）
    """
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return JSONResponse(
            {"available": False, "code": code, "fields": {}, "error": "invalid code"},
            status_code=400,
        )

    def _fetch():
        from src.data.mx_api import available, fetch_snapshot, get_last_status

        if not available():
            return {
                "available": False,
                "code": code,
                "fields": {},
                "source": "mx_api",
                "error": "MX_APIKEY not configured",
            }
        snap = fetch_snapshot(code)
        # 移除内部标签字段
        fields = {k: v for k, v in (snap or {}).items() if not k.startswith("_")}
        entity_name = (snap or {}).get("_mx_entity", "") if isinstance(snap, dict) else ""
        return {
            "available": bool(fields),
            "code": code,
            "name": entity_name,
            "source": "mx_api",
            "status": get_last_status(),
            "fields": fields,
        }

    try:
        result = await run_in_heavy_pool(_fetch)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(
            {"available": False, "code": code, "fields": {}, "error": str(e)},
            status_code=500,
        )


@app.get("/api/stock-deep-analysis/{code}")
async def stock_deep_analysis(code: str):
    """个股独立基本面弹窗数据源：8 维度聚合。

    返回结构化 sections 数组：
      [
        {key, title, icon, status, items: [{label, value, hint?}]},
        ...
      ]
    任意维度失败 → 该 section.status='unavailable'，不影响其它 section 渲染。
    耗时较长（~5-15s），调用方应用 30 分钟客户端缓存。
    """
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return JSONResponse(
            {"code": code, "sections": [], "error": "invalid code"}, status_code=400
        )

    def _build():
        sections: list[dict] = []
        from src.data.stock_search import get_search_universe
        from src.data.fetcher import fetch_realtime_spot
        from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
        import pandas as pd

        # ── 0. 元信息：代码 → 名称（用作卡片标题与个股名片） ──
        name = code
        industry_from_cache = ""
        try:
            universe = get_search_universe()
            if universe is not None and not universe.empty and "name" in universe.columns:
                hit = universe[universe["code"].astype(str).str.zfill(6) == code]
                if not hit.empty:
                    name = str(hit.iloc[0].get("name") or code)
                    industry_from_cache = str(hit.iloc[0].get("industry") or "")
        except Exception:
            pass

        # 实时快照（最新价/换手/量比/流通市值）
        realtime: dict = {}
        try:
            spot = fetch_realtime_spot()
            if spot is not None and not spot.empty and "code" in spot.columns:
                rows = spot[spot["code"].astype(str).str.zfill(6) == code]
                if not rows.empty:
                    row = rows.iloc[0]
                    realtime = {
                        "price": _safe_float(row.get("close")),
                        "change_pct": _safe_float(row.get("change_pct")),
                        "volume": _safe_float(row.get("volume")),
                        "turnover": _safe_float(row.get("turnover")),
                        "volume_ratio": _safe_float(row.get("volume_ratio")),
                        "market_cap": _safe_float(row.get("market_cap")),
                    }
        except Exception:
            pass

        # 历史 K 线（趋势/资金/催化 复用）
        kline_df = pd.DataFrame()
        try:
            kline_df = fetch_kline(code, SCALE_DAILY, datalen=120)
        except Exception:
            pass

        # ── 1. 个股名片（基于实时 + 名缓存） ──
        items = [
            {"label": "代码", "value": code},
            {"label": "名称", "value": name},
        ]
        if realtime.get("price"):
            items.append({"label": "最新价", "value": f"{realtime['price']:.2f}"})
        if realtime.get("change_pct") is not None:
            items.append({
                "label": "今日涨幅",
                "value": f"{realtime['change_pct']:+.2f}%",
                "tone": "pos" if realtime["change_pct"] >= 0 else "neg",
            })
        sections.append({
            "key": "card", "title": "个股名片", "icon": "🏷️",
            "status": "ok", "items": items,
        })

        # ── 2. 估值与市值（MX 妙想） ──
        try:
            from src.data.mx_api import available, fetch_snapshot
            if available():
                snap = fetch_snapshot(code) or {}
                fields = {k: v for k, v in snap.items() if not k.startswith("_")}
                # 优先使用 MX 的总市值（精度高），缺失则用 realtime 流通市值 × 实估
                if not fields.get("总市值") and realtime.get("market_cap"):
                    fields["总市值（流通）"] = f"{realtime['market_cap'] / 1e8:.1f} 亿"
                if fields:
                    val_items = []
                    for k in ("最新价", "总市值", "总市值（流通）", "流通市值", "PE(TTM)", "PE(动)", "PB", "市净率"):
                        if fields.get(k) not in (None, ""):
                            val_items.append({"label": k, "value": str(fields[k])})
                    # 其它（未归类）
                    known = {"最新价", "总市值", "总市值（流通）", "流通市值", "PE(TTM)", "PE(动)", "PB", "市净率",
                             "所属行业", "主营业务", "所属概念", "申万行业"}
                    for k, v in fields.items():
                        if k in known or v in (None, ""):
                            continue
                        val_items.append({"label": k, "value": str(v)})
                    sections.append({
                        "key": "valuation", "title": "估值与市值", "icon": "💰",
                        "status": "ok" if val_items else "empty",
                        "items": val_items,
                        "source": "mx_api",
                    })
                else:
                    sections.append({
                        "key": "valuation", "title": "估值与市值", "icon": "💰",
                        "status": "empty", "items": [],
                    })
            else:
                sections.append({
                    "key": "valuation", "title": "估值与市值", "icon": "💰",
                    "status": "no_key", "items": [],
                    "hint": "配置 MX_APIKEY 后可显示 PE / PB / 总市值等",
                })
        except Exception as e:
            sections.append({
                "key": "valuation", "title": "估值与市值", "icon": "💰",
                "status": "error", "items": [], "error": str(e)[:120],
            })

        # ── 3. 趋势（基于 K 线） ──
        try:
            if kline_df is None or kline_df.empty or len(kline_df) < 20:
                sections.append({
                    "key": "trend", "title": "趋势强度", "icon": "📈",
                    "status": "empty", "items": [],
                    "hint": "本地 K 线不足 20 根",
                })
            else:
                trend_items = _calc_trend_items(kline_df, realtime)
                sections.append({
                    "key": "trend", "title": "趋势强度", "icon": "📈",
                    "status": "ok", "items": trend_items,
                })
        except Exception as e:
            sections.append({
                "key": "trend", "title": "趋势强度", "icon": "📈",
                "status": "error", "items": [], "error": str(e)[:120],
            })

        # ── 4. 资金面（量价 / 换手 / 量比） ──
        try:
            if kline_df is None or kline_df.empty:
                sections.append({
                    "key": "capital", "title": "资金面", "icon": "💵",
                    "status": "empty", "items": [],
                })
            else:
                cap_items = _calc_capital_items(kline_df, realtime)
                sections.append({
                    "key": "capital", "title": "资金面", "icon": "💵",
                    "status": "ok", "items": cap_items,
                })
        except Exception as e:
            sections.append({
                "key": "capital", "title": "资金面", "icon": "💵",
                "status": "error", "items": [], "error": str(e)[:120],
            })

        # ── 5. 催化（涨停连板历史） ──
        try:
            cat_items = _calc_catalyst_items(code, kline_df)
            sections.append({
                "key": "catalyst", "title": "催化剂", "icon": "🚀",
                "status": "ok" if cat_items else "empty", "items": cat_items,
                "hint": None if cat_items else "近期无涨停 / 连板记录",
            })
        except Exception as e:
            sections.append({
                "key": "catalyst", "title": "催化剂", "icon": "🚀",
                "status": "error", "items": [], "error": str(e)[:120],
            })

        # ── 6. 行业面（MX 行业 + 概念归属） ──
        try:
            from src.data.mx_api import available
            industry_name = industry_from_cache
            concepts: list[str] = []
            if available():
                from src.data.mx_api import fetch_snapshot
                snap = fetch_snapshot(code) or {}
                industry_name = snap.get("所属行业") or industry_name
                # 主营业务 / 所属概念
                main_biz = snap.get("主营业务")
                concept_str = snap.get("所属概念")
                if main_biz and isinstance(main_biz, str):
                    concepts.append(f"主营：{main_biz[:60]}")
                if concept_str and isinstance(concept_str, str):
                    for c in [s.strip() for s in concept_str.replace("、", ",").split(",") if s.strip()][:5]:
                        concepts.append(f"概念：{c}")
            # 概念库补全（即便 MX 不可用，本地概念映射也能给）
            try:
                from src.data.concept_fetcher import load_stock_to_concepts
                local_concepts = load_stock_to_concepts()
                lc = local_concepts.get(code, []) or []
                for c in lc[:3]:
                    if not any(c in s for s in concepts):
                        concepts.append(f"概念：{c}")
            except Exception:
                pass
            industry_items = []
            if industry_name:
                industry_items.append({"label": "所属行业", "value": industry_name})
            for c in concepts[:6]:
                # 形如 "概念：xxx" → label=概念, value=xxx
                if c.startswith("概念："):
                    industry_items.append({"label": "概念", "value": c[3:]})
                elif c.startswith("主营："):
                    industry_items.append({"label": "主营业务", "value": c[3:]})
            sections.append({
                "key": "industry", "title": "行业面", "icon": "🏭",
                "status": "ok" if industry_items else "empty",
                "items": industry_items,
            })
        except Exception as e:
            sections.append({
                "key": "industry", "title": "行业面", "icon": "🏭",
                "status": "error", "items": [], "error": str(e)[:120],
            })

        # ── 7. 风险标识 ──
        try:
            risk_items = _calc_risk_items(kline_df, realtime)
            sections.append({
                "key": "risk", "title": "风险标识", "icon": "⚠️",
                "status": "ok" if risk_items else "empty", "items": risk_items,
            })
        except Exception as e:
            sections.append({
                "key": "risk", "title": "风险标识", "icon": "⚠️",
                "status": "error", "items": [], "error": str(e)[:120],
            })

        # ── 8. 近期新闻（MX news-search 兜底） ──
        try:
            from src.data.mx_api import available, news_search
            if not available():
                sections.append({
                    "key": "news", "title": "近期新闻", "icon": "📰",
                    "status": "no_key", "items": [],
                    "hint": "配置 MX_APIKEY 后可看近期新闻",
                })
            else:
                news_items = _calc_news_items(name, code)
                sections.append({
                    "key": "news", "title": "近期新闻", "icon": "📰",
                    "status": "ok" if news_items else "empty", "items": news_items,
                })
        except Exception as e:
            sections.append({
                "key": "news", "title": "近期新闻", "icon": "📰",
                "status": "error", "items": [], "error": str(e)[:120],
            })

        # ── 9. 龙虎榜（akshare 单股维度） ──
        try:
            lhb_items = _calc_lhb_items(code)
            if lhb_items is None:
                sections.append({
                    "key": "lhb", "title": "龙虎榜", "icon": "🐉",
                    "status": "no_data", "items": [],
                    "hint": "近期未上榜（东财龙虎榜仅在异动日记录）",
                })
            else:
                sections.append({
                    "key": "lhb", "title": "龙虎榜", "icon": "🐉",
                    "status": "ok" if lhb_items else "empty", "items": lhb_items,
                })
        except Exception as e:
            sections.append({
                "key": "lhb", "title": "龙虎榜", "icon": "🐉",
                "status": "error", "items": [], "error": str(e)[:120],
            })

        return {"code": code, "name": name, "sections": sections}

    try:
        result = await run_in_heavy_pool(_build)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(
            {"code": code, "sections": [], "error": str(e)}, status_code=500
        )


# === 基本面弹窗 · 各维度计算 helpers ===

def _safe_float(v) -> float:
    try:
        if v is None or v == "" or v == "-":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ema(s, n: int):
    return s.ewm(span=n, adjust=False).mean()


def _calc_trend_items(df, realtime: dict) -> list[dict]:
    """基于 K 线的趋势强度：MA 站上/跌破 + 多周期涨幅 + 量价配合。"""
    items: list[dict] = []
    close = df["close"].astype(float)
    last = float(close.iloc[-1])
    ma5 = float(_ema(close, 5).iloc[-1]) if len(close) >= 5 else 0
    ma10 = float(_ema(close, 10).iloc[-1]) if len(close) >= 10 else 0
    ma20 = float(_ema(close, 20).iloc[-1]) if len(close) >= 20 else 0

    def _above(value: float) -> bool:
        return last > value

    items.append({
        "label": "MA5",
        "value": f"{ma5:.2f}",
        "tone": "pos" if _above(ma5) else "warn",
        "hint": "✓ 站上" if _above(ma5) else "× 跌破",
    })
    items.append({
        "label": "MA10",
        "value": f"{ma10:.2f}",
        "tone": "pos" if _above(ma10) else "warn",
        "hint": "✓ 站上" if _above(ma10) else "× 跌破",
    })
    items.append({
        "label": "MA20",
        "value": f"{ma20:.2f}",
        "tone": "pos" if _above(ma20) else "warn",
        "hint": "✓ 站上" if _above(ma20) else "× 跌破",
    })

    # 多周期涨幅
    for n, label in ((5, "5日"), (10, "10日"), (20, "20日"), (60, "60日")):
        if len(close) > n:
            base = float(close.iloc[-1 - n])
            gain = (last / base - 1) * 100 if base > 0 else 0
            items.append({
                "label": f"{label}涨幅",
                "value": f"{gain:+.2f}%",
                "tone": ("pos" if gain > 0 else "neg") if abs(gain) > 0.5 else None,
            })

    # 量价配合（最近 5 日均量 vs 20 日均量）
    if "volume" in df.columns and len(df) >= 20:
        vol = df["volume"].astype(float)
        v5 = float(vol.tail(5).mean())
        v20 = float(vol.tail(20).mean())
        ratio = (v5 / v20) if v20 > 0 else 0
        items.append({
            "label": "量能（5日/20日）",
            "value": f"{ratio:.2f}×",
            "tone": "pos" if ratio >= 1.5 else ("warn" if ratio <= 0.6 else None),
            "hint": ("放量" if ratio >= 1.5 else ("缩量" if ratio <= 0.6 else "温和")),
        })
    return items


def _calc_capital_items(df, realtime: dict) -> list[dict]:
    """资金面：换手率 / 量比 / 成交额 / 流通市值。"""
    items: list[dict] = []
    turnover = realtime.get("turnover") or 0
    if turnover:
        items.append({"label": "今日换手率", "value": f"{turnover:.2f}%",
                      "tone": "pos" if turnover >= 5 else ("warn" if turnover >= 15 else None),
                      "hint": ("活跃" if turnover >= 5 else ("过热" if turnover >= 15 else "清淡"))})
    vr = realtime.get("volume_ratio") or 0
    if vr:
        items.append({"label": "量比", "value": f"{vr:.2f}",
                      "tone": "pos" if vr >= 1.5 else ("warn" if vr < 0.6 else None),
                      "hint": ("放量" if vr >= 1.5 else ("缩量" if vr < 0.6 else "持平"))})
    amount = realtime.get("volume") or 0  # 注意：realtime.volume 在此项目中是"成交额(元)"
    if amount and amount > 0:
        items.append({"label": "今日成交额", "value": f"{amount / 1e8:.2f} 亿"})
    mcap = realtime.get("market_cap") or 0
    if mcap and mcap > 0:
        items.append({"label": "流通市值", "value": f"{mcap / 1e8:.1f} 亿"})
    # 5 日资金趋势（成交额递增/递减）
    if "amount" in df.columns and len(df) >= 5:
        amt = df["amount"].astype(float).tail(5)
        slope = (float(amt.iloc[-1]) - float(amt.iloc[0])) / max(float(amt.iloc[0]), 1.0)
        items.append({
            "label": "5日成交额趋势",
            "value": f"{slope * 100:+.1f}%",
            "tone": "pos" if slope > 0.2 else ("warn" if slope < -0.2 else None),
        })
    return items


def _calc_catalyst_items(code: str, df) -> list[dict]:
    """催化：近期涨停 / 连板次数（基于 K 线 + 涨停池缓存）。"""
    items: list[dict] = []
    if df is None or df.empty or len(df) < 5:
        return items
    close = df["close"].astype(float)
    pre_close = df["pre_close"].astype(float) if "pre_close" in df.columns else close.shift(1)
    pct = (close / pre_close - 1) * 100
    is_gem = code.startswith(("300", "301", "688"))
    limit = 19.5 if is_gem else 9.8

    # 近 60 日涨停次数
    window = pct.tail(60)
    limit_ups = int((window >= limit).sum())
    items.append({"label": "近60日涨停", "value": f"{limit_ups} 次",
                  "tone": "pos" if limit_ups >= 3 else ("warn" if limit_ups == 0 else None)})

    # 当前连板（向前连续涨停）
    lianban = 0
    for v in pct.iloc[::-1]:
        if v >= limit:
            lianban += 1
        else:
            break
    if lianban > 0:
        items.append({"label": "当前连板", "value": f"{lianban} 板",
                      "tone": "pos" if lianban >= 2 else None})

    # 涨停池缓存中是否有今天/近几天
    try:
        from src.data.fetcher import _load_limit_up_cache
        from src.config import now_cn
        cache = _load_limit_up_cache()
        today_str = now_cn().strftime("%Y%m%d")
        # 近 5 个交易日
        recent_dates = sorted(cache.keys(), reverse=True)[:5]
        hit_dates: list[str] = []
        for d in recent_dates:
            try:
                df_hits = cache[d]
                codes_hit = set(df_hits["code"].astype(str).str.zfill(6)) if df_hits is not None and not df_hits.empty else set()
                if code in codes_hit:
                    hit_dates.append(d)
            except Exception:
                continue
        if hit_dates:
            # 格式化日期为 MM-DD
            pretty = "/".join(d[4:6] + "-" + d[6:] for d in sorted(hit_dates))
            items.append({"label": "近5日上榜", "value": f"{len(hit_dates)} 次（{pretty}）"})
    except Exception:
        pass

    # 5 日累计涨幅
    if len(close) >= 6:
        gain5 = (float(close.iloc[-1]) / float(close.iloc[-6]) - 1) * 100
        items.append({"label": "5日累计", "value": f"{gain5:+.2f}%",
                      "tone": "pos" if gain5 > 0 else "neg"})
    return items


def _calc_risk_items(df, realtime: dict) -> list[dict]:
    """风险：高位放量 / 涨停断板 / PE 亏损 / 量比异常。"""
    items: list[dict] = []
    if df is None or df.empty or len(df) < 20:
        return items

    close = df["close"].astype(float)
    last = float(close.iloc[-1])
    high60 = float(close.tail(60).max()) if len(close) >= 60 else float(close.max())
    if high60 > 0:
        near_pct = (last / high60 - 1) * 100
        if near_pct >= -5:
            items.append({
                "label": "距 60 日高点",
                "value": f"{near_pct:+.1f}%",
                "tone": "warn" if near_pct >= -3 else None,
                "hint": "⚠ 接近前高" if near_pct >= -3 else None,
            })

    # 涨停断板（昨日涨停今日没封）
    pre_close = df["pre_close"].astype(float) if "pre_close" in df.columns else close.shift(1)
    if len(close) >= 2 and len(pre_close) >= 2:
        prev_pct = (float(close.iloc[-2]) / float(pre_close.iloc[-2]) - 1) * 100
        today_pct = (last / float(pre_close.iloc[-1]) - 1) * 100
        is_gem = False  # 简化：限值用主板
        limit = 9.8
        if prev_pct >= limit and today_pct < limit - 1:
            items.append({
                "label": "涨停断板",
                "value": "是",
                "tone": "warn",
                "hint": "昨日封板今日未封",
            })

    # 量比异常
    vr = realtime.get("volume_ratio") or 0
    if vr >= 3:
        items.append({
            "label": "量比异常",
            "value": f"{vr:.2f}",
            "tone": "warn",
            "hint": "⚠ 短时放量",
        })

    # PE 亏损（MX 拉不到 P/E 为负或缺失 → 用 N/A 占位）
    items.append({"label": "数据来源", "value": "本地 K 线 / 实时 / 涨停池"})
    return items


def _calc_news_items(name: str, code: str) -> list[dict]:
    """近期新闻：MX news-search 兜底。无 key 返回空 list（外层会标 no_key）。"""
    items: list[dict] = []
    try:
        from src.data.mx_api import available, news_search
        if not available():
            return items
        # 缓存 30 分钟（同 fetch_snapshot）
        result = news_search(f"{name} {code}")
        # 解析 dataTableDTOList / newsList（兼容两种返回）
        data = (result or {}).get("data") or {}
        inner = (data.get("data") or {})
        sr = inner.get("searchDataResultDTO") or {}
        # 尝试抽 newsList 字段
        news_list = sr.get("newsList") or []
        if not news_list:
            # 退化：dataTableDTOList[0].table 的最后一行
            for dto in sr.get("dataTableDTOList") or []:
                tbl = dto.get("table") or {}
                for k, v in tbl.items():
                    if k == "headName":
                        continue
                    if isinstance(v, list) and v:
                        items.append({"label": "新闻片段", "value": str(v[-1])[:120]})
                        break
                if items:
                    break
        else:
            for n in news_list[:5]:
                title = n.get("title") or n.get("新闻标题") or ""
                date = n.get("date") or n.get("发布时间") or ""
                if title:
                    items.append({"label": date[:10] if date else "新闻", "value": str(title)[:80]})
    except Exception as e:
        # 外层 status 已经是 error
        raise
    return items


def _calc_lhb_items(code: str):
    """龙虎榜：akshare.stock_lhb_stock_detail_em 单股近 30 天。

    返回 None 表示「明确无上榜」；返回 [] 表示「接口失败 / 拉不到」；返回 list 是有数据。
    """
    try:
        import akshare as ak
    except Exception:
        return []  # akshare 不可用
    try:
        from src.config import now_cn
        # 起始日期 = 30 天前
        end = now_cn().strftime("%Y%m%d")
        from datetime import timedelta
        start = (now_cn() - timedelta(days=60)).strftime("%Y%m%d")
        df = ak.stock_lhb_stock_detail_em(symbol=code, start_date=start, end_date=end)
    except Exception:
        return []
    if df is None or df.empty:
        return None  # 明确无上榜
    items: list[dict] = []
    # 累计上榜次数 / 累计净买额
    rows = df.to_dict("records")
    items.append({"label": "近 60 日上榜", "value": f"{len(rows)} 次",
                  "tone": "pos" if len(rows) >= 2 else None})
    net_buy = 0.0
    for r in rows:
        try:
            nb = float(r.get("净额") or r.get("成交净额") or 0)
            net_buy += nb
        except Exception:
            pass
    if net_buy:
        items.append({
            "label": "累计净买",
            "value": f"{net_buy / 1e8:+.2f} 亿",
            "tone": "pos" if net_buy > 0 else "neg",
        })
    # 最近 3 条
    for r in rows[:3]:
        d = r.get("上榜日") or r.get("日期") or r.get("trade_date") or ""
        expl = r.get("解读") or r.get("上榜原因") or r.get("reason") or ""
        items.append({
            "label": str(d)[:10] if d else "上榜日",
            "value": str(expl)[:60] if expl else "上榜",
        })
    return items


@app.get("/api/stock-basic/status")
async def stock_basic_status():
    """本地 DuckDB `stock_basic` 全市场代码表行数与更新时间。"""
    from src.data.structured_store import stock_basic_stats

    return JSONResponse(stock_basic_stats())


@app.post("/api/stock-basic/refresh")
async def stock_basic_refresh():
    """拉取新浪全市场列表并覆盖写入 stock_basic（首次约数秒，之后搜索走本地表）。"""
    from src.data.stock_search import refresh_universe_table

    out = await run_in_threadpool(refresh_universe_table)
    return JSONResponse(out)


@app.post("/api/stock-warm-kline")
async def stock_warm_kline(
    background_tasks: BackgroundTasks,
    payload: dict = Body(default_factory=dict),
):
    """加入持仓后后台拉取日 K（新浪），不阻塞响应。"""
    from src.data.stock_search import warm_daily_klines

    raw = payload.get("codes")
    if isinstance(raw, str):
        codes = [c.strip() for c in raw.split(",") if c.strip()]
    elif isinstance(raw, list):
        codes = [str(c).strip() for c in raw if str(c).strip()]
    else:
        codes = []
    codes = codes[:20]
    if not codes:
        return JSONResponse({"ok": False, "queued": 0})
    background_tasks.add_task(warm_daily_klines, codes)
    return JSONResponse({"ok": True, "queued": len(codes)})


_MY_HOLDINGS_PATH = DATA_DIR / "my_holdings.json"


def _normalize_my_holdings_rows(holdings: list | None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for x in holdings or []:
        if not isinstance(x, dict):
            continue
        d = "".join(c for c in str(x.get("code", "")) if c.isdigit())
        if len(d) < 6:
            continue
        code = d[-6:]
        if len(code) != 6 or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": str(x.get("name", "") or "").strip()[:80]})
    return out


def _live_gain_10d_from_snapshot_row(row: dict, live_close: float) -> float | None:
    """用最新价重算 10 日涨幅，与 get_ranking_live 中公式一致。

    以落盘时的 close、gain_10d 反推 10 日前基准价，再用现价重算，保证
    今日选股 /api/hit-live 与 10 日涨幅榜 /api/ranking-live 同源。
    """
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


@app.get("/api/my-holdings")
async def get_my_holdings():
    """我的持仓列表（落盘 data/my_holdings.json，UTF-8）。"""
    raw = load_json_file(_MY_HOLDINGS_PATH)
    if isinstance(raw, list):
        return JSONResponse({"holdings": _normalize_my_holdings_rows(raw)})
    if isinstance(raw, dict):
        h = raw.get("holdings")
        if isinstance(h, list):
            return JSONResponse({"holdings": _normalize_my_holdings_rows(h)})
    return JSONResponse({"holdings": []})


@app.put("/api/my-holdings")
async def put_my_holdings(payload: dict = Body(default_factory=dict)):
    """全量覆盖写入持仓（真源：ledger_doc `my_holdings.json`；失败时回退写磁盘）。"""
    import json as _json

    from src.data.ledger_doc_store import upsert_json

    h = payload.get("holdings")
    if not isinstance(h, list):
        return JSONResponse({"ok": False, "error": "holdings must be a list"}, status_code=400)
    clean = _normalize_my_holdings_rows(h)
    body = {"holdings": clean}
    try:
        upsert_json("my_holdings.json", body)
    except Exception as e:
        print(f"[my-holdings] ledger upsert failed: {e}")
        try:
            _MY_HOLDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _MY_HOLDINGS_PATH.write_text(
                _json.dumps(body, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e2:
            return JSONResponse(
                {"ok": False, "error": f"ledger:{e}; disk:{e2}"},
                status_code=500,
            )
    return JSONResponse({"ok": True, "holdings": clean})


@app.get("/api/gain-ranking")
async def get_gain_ranking():
    """获取10日涨幅排行"""
    ranking_file = DATA_DIR / "latest_ranking.json"
    data = load_json_file(ranking_file)
    if data is not None and isinstance(data, dict):
        return JSONResponse(_sanitize_json(data))
    return JSONResponse({"date": "", "ranking": []})


def _ranking_live_sync(*, timeout_sec: float = 12.0) -> dict:
    """同步：TOP30 实时涨幅重算（在线程池执行，避免阻塞事件循环）。"""
    import os

    ranking_file = DATA_DIR / "latest_ranking.json"
    data = load_json_file(ranking_file)
    if data is None:
        return {"date": "", "ranking": [], "live": False}
    if not isinstance(data, dict):
        return {"date": "", "ranking": [], "live": False}
    items = data.get("ranking") or []
    if not items:
        return {**data, "live": False}

    if os.getenv("SKIP_LIVE_RANKING_FETCH", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        out = dict(data)
        out["live"] = False
        out["live_skipped"] = True
        return out

    codes = [str(r["code"]) for r in items]
    base_map = {str(r["code"]): dict(r) for r in items}

    live_map: dict[str, dict] = {}
    try:
        from src.data.tencent_api import fetch_stock_details

        live = fetch_stock_details(codes)
        if live is not None and not live.empty:
            live_map = {str(row["code"]): row for _, row in live.iterrows()}
    except Exception as e:
        print(f"[ranking-live] 腾讯行情: {e}")

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
            print(f"[ranking-live] 新浪兜底: {e}")

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
            mc = float(lr.get("market_cap_yi", 0))
            if mc > 0:
                base["market_cap_yi"] = round(mc, 2)

    result = sorted(base_map.values(), key=lambda x: x.get("gain_10d", 0), reverse=True)
    for i, r in enumerate(result):
        r["rank"] = i + 1

    return {
        "date": data.get("date", ""),
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "ranking": result,
        "live": bool(live_map),
    }


@app.post("/api/ranking/re-enrich")
async def ranking_re_enrich():
    """对 latest_ranking 已有 TOP30 补全缺失列并写回库（不跑全市场扫描）。

    用于 15:30 周期任务失败或旧快照缺 板块/连板/市值/涨幅 等字段时的盘后修复。
    """
    from src.data.ranking_scanner import re_enrich_ranking_records

    ranking_file = DATA_DIR / "latest_ranking.json"
    data = load_json_file(ranking_file)
    if not isinstance(data, dict) or not (data.get("ranking") or []):
        return JSONResponse({"status": "error", "msg": "无 TOP30 落盘数据"}, status_code=404)
    try:
        enriched = await run_in_heavy_pool(
            re_enrich_ranking_records,
            list(data.get("ranking") or []),
        )
        payload = {
            **data,
            "ranking": enriched,
            "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        }
        await run_in_heavy_pool(lambda: dump_json_file(ranking_file, payload))
        sample = enriched[0] if enriched else {}
        return JSONResponse({
            "status": "ok",
            "count": len(enriched),
            "updated_at": payload["updated_at"],
            "sample_fields": {
                "industry": bool(sample.get("industry")),
                "market_cap_yi": sample.get("market_cap_yi"),
                "change_pct": sample.get("change_pct"),
                "continuous_limit_up": sample.get("continuous_limit_up"),
            },
        })
    except Exception as e:
        import traceback

        traceback.print_exc()
        return JSONResponse({"status": "error", "msg": str(e)[:300]}, status_code=500)


@app.get("/api/ranking-live")
async def get_ranking_live():
    """实时刷新 TOP30 涨幅并重新排序

    从 latest_ranking.json 读取基准数据，调腾讯接口拉实时行情，
    用 live close 重算 gain_10d，按 gain_10d 降序重排。
    """
    try:
        payload = await run_in_heavy_pool(_ranking_live_sync)
        return JSONResponse(payload)
    except Exception as e:
        print(f"[ranking-live] 失败: {e}")
        ranking_file = DATA_DIR / "latest_ranking.json"
        data = load_json_file(ranking_file)
        if isinstance(data, dict):
            return JSONResponse({**data, "live": False, "error": str(e)[:200]})
        return JSONResponse({"date": "", "ranking": [], "live": False, "error": str(e)[:200]})


@app.get("/api/sentiment")
async def get_pool_sentiment():
    """获取 top30 梯队情绪（竞价分布判定）"""
    from src.data.analytics_store import load_latest_sentiment_document

    data = load_latest_sentiment_document()
    if not data:
        return JSONResponse({})
    return JSONResponse(data)


@app.get("/api/history")
async def get_cycle_history():
    """获取周期历史时间线"""
    history_file = DATA_DIR / "cycle_history.json"
    data = load_json_file(history_file)
    if data is None:
        return JSONResponse([])
    return JSONResponse(data)


@app.get("/api/trend-pool")
async def get_trend_pool():
    """趋势选股最新结果（含明日观察池 + 全部评分 + 板块动量）"""
    from src.engine.trend_screener import merge_trend_pool_with_manual

    f = DATA_DIR / "latest_trend.json"
    data = load_json_file(f)
    if data is None:
        data = {"date": "", "pool": [], "all_scored": [], "rejected": []}
    elif not isinstance(data, dict):
        return JSONResponse({"date": "", "pool": [], "all_scored": [], "rejected": []})
    return JSONResponse(_sanitize_json(merge_trend_pool_with_manual(data)))


@app.get("/api/trend-history")
async def get_trend_history(light: int = 1, full: int = 0):
    """趋势选股历史记录（含次日表现回填，用于胜率统计）

    默认 light=1：仅读库+统计，不拉 K 线（首页/涨幅榜安全）。
    full=1 且盘后：等同 light=0，执行 reconcile + K 线回填（手动或盘后任务用）。
    盘内即使用 full=1 也会被服务端拒绝回填。
    """
    try:
        from src.engine.trend_screener import get_trend_history_payload
        from src.market_schedule import allow_trend_history_kline_backfill

        light_on = bool(int(light if light is not None else 1))
        want_full = bool(int(full or 0)) or not light_on
        do_backfill = allow_trend_history_kline_backfill(want_full)
        if want_full and not do_backfill:
            print("[trend-history] 盘内跳过 K 线回填，仅返回库内记录")

        if not do_backfill:
            out = get_trend_history_payload(light=True)
        else:
            out = await run_in_heavy_pool(
                get_trend_history_payload, light=False
            )
        return JSONResponse(_sanitize_json(out))
    except Exception as e:
        return JSONResponse(
            {"error": str(e), "records": [], "stats": {}},
            status_code=500,
        )


def _trend_run_sync():
    from src.engine.trend_screener import run_trend_screener, backfill_trend_next_day

    res = run_trend_screener()
    backfill_trend_next_day()
    return res


@app.post("/api/trend-run")
async def trend_run():
    """手动触发趋势选股 (盘后/调试用)"""
    try:
        res = await run_in_threadpool(_trend_run_sync)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"status": "error", "msg": str(e)}, status_code=500)


def _repair_screener_pending_sync():
    """补跑 pending 收盘价 + closed 次日竞价（修复漏跑周期导致看板卡住）。"""
    from src.engine.screener_history import (
        backfill_close,
        backfill_next_day_auction,
        reconcile_next_day_from_kline,
        recompute_history_decisions_v4,
        repair_missing_close_prices,
    )
    from src.data.fetcher import fetch_realtime_spot

    spot = fetch_realtime_spot()
    n_repair = 0
    try:
        n_repair = repair_missing_close_prices()
    except Exception as e:
        print(f"[repair-screener] repair_missing_close_prices: {e}")
    backfill_close(spot)
    backfill_next_day_auction(spot)
    n_next = reconcile_next_day_from_kline(spot_df=spot)
    n_env = recompute_history_decisions_v4()
    return {
        "status": "ok",
        "close_repaired": n_repair,
        "next_day_reconciled": n_next,
        "history_decisions_updated": n_env,
    }


@app.post("/api/repair-screener-pending")
async def repair_screener_pending():
    """手动补写选股记录：历史 pending 收盘价 + 次日竞价（无需整轮周期更新）。"""
    try:
        res = await run_in_threadpool(_repair_screener_pending_sync)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/screener-history/recalc-day-change")
async def screener_recalc_day_change(code: str, date: str = ""):
    """按日 K 昨收重算指定标的 day_change（修复错误 pre_close）。"""
    try:
        from src.engine.screener_history import recalc_day_change_for_code

        res = await run_in_threadpool(recalc_day_change_for_code, code, date)
        return JSONResponse({"status": "ok", **res})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/refresh-cycle")
async def refresh_cycle():
    """手动触发周期引擎更新（含 15:30 排行落库 + 趋势选股观察池）"""
    try:
        from src.scheduler import run_cycle_update

        result = await run_in_threadpool(run_cycle_update)
        return JSONResponse({"status": "ok", "snapshot": result})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/post-market-run")
async def post_market_run():
    """手动触发 15:45 盘后串：复盘 → 决策追踪回填 → 趋势历史 D+1/D+2"""
    try:
        from src.scheduler import run_post_market_bundle

        await run_in_threadpool(run_post_market_bundle)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


def _refresh_advice_sync():
    from src.data.fetcher import fetch_realtime_spot
    from src.engine.advice_snapshot_hydrate import load_leader_for_advice, load_sentiment_for_advice
    from src.notify.email_sender import write_advice_snapshot

    spot_df = fetch_realtime_spot()
    sent = load_sentiment_for_advice()
    leader = load_leader_for_advice()
    return write_advice_snapshot(sent, leader, spot_df=spot_df)


@app.post("/api/refresh-advice")
async def refresh_advice():
    """根据库内 sentiment/leader + 实时 spot 重写决策快照（v2 看板）"""
    try:
        from src.notify.email_sender import last_advice_write_skip_reason

        out = await run_in_threadpool(_refresh_advice_sync)
        if out is None:
            detail = last_advice_write_skip_reason() or "输入不足或仍为加载态，未写入库"
            return JSONResponse({"status": "skipped", "message": detail}, status_code=200)
        return JSONResponse({"status": "ok", "payload": out})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/refresh-screener")
async def refresh_screener(send_email: bool = False):
    """手动触发选股引擎

    默认 skip_email=True（防盘中重复邮件，story anti-duplicate-email-2.5）。
    看板「执行选股」传 send_email=true 可在 0 命中等场景补发决策邮件。

    邮件发送资格最终由 email_sender.send_guard_allows 统一判定（2.6 升级）：
    即便 send_email=true 仍受 9:20-15:30 窗口 + 当日幂等约束。
    """
    try:
        from src.scheduler import run_screener_update

        result = await run_in_threadpool(
            run_screener_update,
            skip_email=False if send_email else True,
            api_explicit=bool(send_email),
        )
        return JSONResponse({"status": "ok", "result": result})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/recompute-screener-decisions")
async def recompute_screener_decisions():
    """重算今日选股 per_stock_decision + 历史归档 decision（v4 口径）。"""
    try:
        from src.engine.screener_history import recompute_history_decisions_v4
        from src.engine.screener_market_env import recompute_latest_screener_per_stock_decisions

        n_live = await run_in_heavy_pool(recompute_latest_screener_per_stock_decisions)
        n_hist = await run_in_heavy_pool(recompute_history_decisions_v4)
        advice_refreshed = False
        try:
            await run_in_threadpool(_refresh_advice_sync)
            advice_refreshed = True
        except Exception as e:
            print(f"[recompute-screener] refresh-advice 跳过: {e}")
        try:
            from src.engine.screener_history import refresh_today_records

            await run_in_heavy_pool(refresh_today_records)
        except Exception as e:
            print(f"[recompute-screener] refresh_today_records 跳过: {e}")
        return JSONResponse({
            "status": "ok",
            "hits_updated": n_live,
            "history_decisions_updated": n_hist,
            "advice_refreshed": advice_refreshed,
        })
    except Exception as e:
        from src.data.quant_db import is_duckdb_invalidated, reset_shared_connection

        if is_duckdb_invalidated(e):
            reset_shared_connection()
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/deviation")
async def get_deviation():
    """获取240周线偏离度数据"""
    dev_file = DATA_DIR / "latest_deviation.json"
    # 可能无同名 .json，仍以 doc_key 读库
    data = load_json_file(dev_file)
    if data is not None:
        return JSONResponse(data)
    return JSONResponse({"date": "", "results": []})


# ========== 复盘 + 决策卡 + 决策追踪 ==========

@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    """复盘页面"""
    return _serve_html("review.html", "<h1>复盘</h1>", request)


@app.get("/api/review")
async def get_review():
    """获取复盘数据。

    时间门控（用户要求）：当天 15:00 前显示昨日复盘，15:00 后显示今日。
    实现：
      · now < 15:00：从 review_history.json 取最近一条 date != today 的记录
      · now >= 15:00：返回 latest_review.json（今日 cron 在 15:45 跑完）

    watch_pool（次日观察池）= review_history / latest_review.json 中 15:45
    冻结快照（_save_review 已持久化）；不再实时重算 — 看板需与"昨晚定的
    明日关注股"保持一致（watch-pool-snapshot-2.2）。

    scorecard / promotion_summary 仍用当前公式重算（用户要求"历史快照也
    用新公式"），仅 watch_pool 是冻结快照。
    """
    from src.engine.screener_market_env import resolve_review_document_for_api

    review_data = resolve_review_document_for_api()
    if not review_data:
        return JSONResponse({})

    review_data = dict(review_data)  # 避免污染历史 entry

    # 整站只取真概念：剔除历史快照中遗留的元标签
    _strip_meta_concepts_inplace(review_data)

    from src.engine.daily_review import sync_review_payload_for_api

    review_data = sync_review_payload_for_api(review_data)

    if not review_data.get("market_breadth"):
        rev_date = str(review_data.get("date") or "")[:10]
        try:
            from src.engine.market_insight import _breadth_looks_stale

            ins = load_json_file(DATA_DIR / "latest_insight.json")
            if isinstance(ins, dict) and str(ins.get("date") or "")[:10] == rev_date:
                b = ins.get("breadth")
                if isinstance(b, dict) and not _breadth_looks_stale(b):
                    review_data["market_breadth"] = b
        except Exception:
            pass

    from src.market_schedule import allow_review_live_hydration

    if allow_review_live_hydration():
        rev_date = str(review_data.get("date") or "")[:10]
        try:
            from src.engine.daily_review import hydrate_relay_env_from_stores

            review_data["relay_env"] = hydrate_relay_env_from_stores(
                review_data.get("relay_env") or {},
                rev_date,
            )
        except Exception:
            pass
    else:
        review_data["_read_only_intraday"] = True

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
    data = load_json_file(f)
    if data is not None:
        return JSONResponse(data)
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


@app.post("/api/limit-up-cache/refresh")
async def refresh_limit_up_cache(days: int = 5):
    """拉取当日涨停池并写入 limit_up_cache（东财→新浪；历史不足时 sina K 线回溯）。"""
    try:
        from src.config import now_cn
        from src.data.fetcher import fetch_limit_up_history

        from src.data.fetcher import sync_limit_up_cache_from_zt_pool

        hist = await run_in_threadpool(fetch_limit_up_history, int(days))
        today_key = now_cn().strftime("%Y%m%d")
        date_counts = {str(k): int(len(v)) for k, v in (hist or {}).items()}
        zt_n = 0
        if date_counts.get(today_key, 0) == 0:
            zt_n = await run_in_threadpool(sync_limit_up_cache_from_zt_pool, today_key)
            if zt_n > 0:
                hist2 = await run_in_threadpool(fetch_limit_up_history, int(days))
                date_counts = {str(k): int(len(v)) for k, v in (hist2 or {}).items()}
        return JSONResponse({
            "status": "ok",
            "today_key": today_key,
            "today_count": date_counts.get(today_key, 0),
            "zt_pool_fallback": zt_n,
            "dates": date_counts,
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/review/sync-persist")
async def sync_persist_review(
    body: dict | None = Body(default=None),
):
    """按涨停池 lbc 重建晋级矩阵/评分卡并写回 DuckDB（修正 5/19 等历史快照）。

    可选 JSON body：
      {"prev_space_board_today": {"code":"001259","name":"利仁科技","yesterday_board":8,
        "today_held":false, "today_pct":-3.5, "today_open_pct": 1.2}}
    用于更正昨日空间板连板数（如东财 lbc 写成 7 而实际昨 8 板、今日 8进9 断板）。
    """
    from functools import partial

    from src.engine.screener_market_env import resolve_review_document_for_api
    from src.engine.daily_review import apply_prev_space_board_patch, sync_review_payload_for_api

    try:
        review_data = resolve_review_document_for_api()
        if not review_data:
            return JSONResponse({"status": "error", "msg": "无复盘数据"}, status_code=404)
        review_data = dict(review_data)
        _strip_meta_concepts_inplace(review_data)
        patch = (body or {}).get("prev_space_board_today") if isinstance(body, dict) else None
        if patch:
            review_data = apply_prev_space_board_patch(review_data, patch)
        out = await run_in_threadpool(
            partial(sync_review_payload_for_api, review_data, persist=True),
        )
        g2 = next(
            (g for g in (out.get("prev_board_groups") or []) if g.get("prev_board") == 2),
            None,
        )
        b2_detail = None
        if g2:
            p, f = len(g2.get("promoted") or []), len(g2.get("failed") or [])
            b2_detail = f"{p}/{p + f}"
        return JSONResponse({
            "status": "ok",
            "date": out.get("date"),
            "persisted": bool(out.get("_persisted")),
            "rebuilt": bool(out.get("_prev_board_groups_rebuilt")),
            "b2_promotion": b2_detail,
        })
    except Exception as e:
        import traceback

        traceback.print_exc()
        return JSONResponse({"status": "error", "msg": str(e)}, status_code=500)


@app.post("/api/review/run")
async def run_review_now(force: int = 0):
    """手动触发复盘（盘内拒绝，仅盘后或定时任务更新）。force=1 忽略时间门控。"""
    from src.market_schedule import is_post_market_data_window

    if not force and not is_post_market_data_window():
        return JSONResponse(
            {
                "status": "skipped",
                "msg": "盘内不更新复盘数据，请在交易日 15:00 后点击「刷新复盘」",
            },
            status_code=200,
        )
    try:
        from src.engine.daily_review import run_daily_review

        result = await run_in_threadpool(run_daily_review)
        if result:
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
            files = data_dir_glob_json("report_*.json")
            if files:
                rep = load_json_file(files[0])
                if rep is not None:
                    return JSONResponse(rep)
            # 无任何已保存报告，按默认逻辑生成上月
            report = generate_monthly_report(None, None)
            return JSONResponse(report)

        # 指定年月：优先读本地（文件或库）
        report_file = DATA_DIR / f"report_{year}_{month:02d}.json"
        rep = load_json_file(report_file)
        if rep is not None:
            return JSONResponse(rep)
        report = generate_monthly_report(year, month)
        return JSONResponse(report)
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/monthly-report/list")
async def list_monthly_reports():
    """已保存的月度报告列表（按年月降序）"""
    items = []
    for f in data_dir_glob_json("report_*.json"):
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
    """持仓等指定代码的实时行情（价格/涨幅等）；前端仅对「我的持仓」3s 轮询，今日选股走落盘数据。"""

    def _norm_hit_code(c: str) -> str:
        d = "".join(ch for ch in str(c) if ch.isdigit())
        if not d:
            return ""
        if len(d) < 6:
            return d.zfill(6)
        return d[-6:].zfill(6)

    if not codes:
        return JSONResponse({})

    raw_list = [c.strip() for c in codes.split(",") if c.strip()]
    seen: set[str] = set()
    code_list: list[str] = []
    for c in raw_list:
        nc = _norm_hit_code(c)
        if len(nc) == 6 and nc not in seen:
            seen.add(nc)
            code_list.append(nc)
    if not code_list:
        return JSONResponse({})

    result = {}
    rank_map: dict = {}
    try:
        from src.data.sina_api import fetch_realtime_batch
        df = fetch_realtime_batch(code_list)
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = _norm_hit_code(str(row.get("code", "")))
                if len(code) != 6:
                    continue
                pre = float(row.get("pre_close", 0))
                close = float(row.get("close", 0))
                pct = round((close / pre - 1) * 100, 2) if pre > 0 and close > 0 else None
                o = float(row.get("open", 0))
                result[code] = {
                    "price": round(close, 2) if close > 0 else None,
                    "open": round(o, 2) if o > 0 else None,
                    "pre_close": round(pre, 2) if pre > 0 else None,
                    "pct": pct,
                }
    except Exception:
        pass

    # 补充板块和10日涨幅（从排行数据；10 日初值来自落盘快照）
    try:
        ranking_file = DATA_DIR / "latest_ranking.json"
        rd = load_json_file(ranking_file)
        if isinstance(rd, dict):
            rank_map = {}
            for r in (rd.get("ranking") or []):
                if not isinstance(r, dict) or not r.get("code"):
                    continue
                k = _norm_hit_code(str(r["code"]))
                if len(k) == 6:
                    rank_map[k] = r
            for code in code_list:
                if code in result:
                    r = rank_map.get(code, {})
                    result[code]["industry"] = r.get("industry", "")
                    result[code]["gain_10d"] = r.get("gain_10d")
                    tc = r.get("top_concepts")
                    if isinstance(tc, list) and tc:
                        result[code]["top_concepts"] = [str(x) for x in tc if x]
                    if r.get("continuous_limit_up") is not None:
                        try:
                            result[code]["continuous_limit_up"] = int(r["continuous_limit_up"])
                        except (TypeError, ValueError):
                            pass
                elif code in rank_map:
                    r = rank_map[code]
                    extra_tc = r.get("top_concepts")
                    top_c: list[str] = (
                        [str(x) for x in extra_tc if x] if isinstance(extra_tc, list) else []
                    )
                    clu_rb = None
                    if r.get("continuous_limit_up") is not None:
                        try:
                            clu_rb = int(r["continuous_limit_up"])
                        except (TypeError, ValueError):
                            clu_rb = None
                    result[code] = {
                        "price": r.get("close"),
                        "open": None,
                        "pre_close": None,
                        "pct": r.get("change_pct"),
                        "industry": r.get("industry", ""),
                        "gain_10d": r.get("gain_10d"),
                        "top_concepts": top_c,
                        **({"continuous_limit_up": clu_rb} if clu_rb is not None else {}),
                    }
    except Exception:
        pass

    # 最新选股结果兜底：行业 / 概念 / 连板（持仓股常不在 TOP30）
    try:
        scr = load_json_file(DATA_DIR / "latest_screener.json")
        if isinstance(scr, dict):
            for h in scr.get("hits") or []:
                if not isinstance(h, dict) or not h.get("code"):
                    continue
                hc = _norm_hit_code(str(h["code"]))
                if len(hc) != 6 or hc not in result:
                    continue
                rw = result[hc]
                if not rw.get("industry"):
                    rw["industry"] = str(h.get("industry") or "")
                if not rw.get("top_concepts"):
                    tc = h.get("top_concepts")
                    if isinstance(tc, list) and tc:
                        rw["top_concepts"] = [str(x) for x in tc if x]
                if rw.get("continuous_limit_up") is None and h.get("continuous_limit_up") is not None:
                    try:
                        rw["continuous_limit_up"] = int(h["continuous_limit_up"])
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass

    # 板块兜底（industry_cache）
    try:
        ic = DATA_DIR / "industry_cache.json"
        ind_map = load_json_file(ic)
        if isinstance(ind_map, dict):
            for code in code_list:
                if code in result and not result[code].get("industry"):
                    ik = _norm_hit_code(str(code))
                    result[code]["industry"] = ind_map.get(ik, ind_map.get(code, ""))
    except Exception:
        pass

    # 概念兜底（持仓股常不在 TOP30 / 今日选股）
    need_tc = [c for c in code_list if c in result and not result[c].get("top_concepts")]
    if need_tc:
        try:
            from src.data.concept_fetcher import load_stock_to_concepts
            from src.engine.concept_stats import (
                aggregate_concept_limit_ups,
                top_concepts_for_stock,
            )
            c_map = load_stock_to_concepts() or {}
            heats = []
            try:
                lu = load_json_file(DATA_DIR / "limit_up_cache.json") or {}
                if lu:
                    latest = sorted(lu.keys())[-1]
                    heats = aggregate_concept_limit_ups(lu.get(latest, []) or [], c_map)
            except Exception:
                pass
            for code in need_tc:
                raw = list(c_map.get(code) or [])
                tc = top_concepts_for_stock(raw, heats, top_n=2) if heats else []
                if not tc and raw:
                    tc = raw[:2]
                if tc:
                    result[code]["top_concepts"] = tc
        except Exception:
            pass

    # 竞价涨幅：开盘相对昨收（与 /api/auction-detail 同源）
    for code in code_list:
        if code not in result:
            continue
        v = result[code]
        try:
            pf = float(v.get("pre_close") or 0)
            of = float(v.get("open") or 0)
            if pf > 0 and of > 0:
                v["auction_gain"] = round((of / pf - 1) * 100, 2)
        except (TypeError, ValueError):
            pass

    # 不在排行榜的标的：用腾讯补市值，用K线算10日涨幅
    missing_10d = [c for c in code_list if c in result and result[c].get("gain_10d") is None]
    if missing_10d:
        # 腾讯补市值
        try:
            from src.data.tencent_api import fetch_stock_details
            tx = fetch_stock_details(missing_10d)
            if tx is not None and not tx.empty:
                for _, row in tx.iterrows():
                    c = _norm_hit_code(str(row["code"]))
                    if len(c) == 6 and c in result:
                        mc = float(row.get("market_cap_yi", 0) or 0)
                        if mc > 0:
                            result[c]["market_cap_yi"] = mc
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

    # 其余标的补流通市值（TOP30 等已有 10 日涨幅但未走上一分支的）
    cap_missing = [c for c in code_list if c in result and not result[c].get("market_cap_yi")]
    if cap_missing:
        try:
            from src.data.tencent_api import fetch_stock_details
            tx2 = fetch_stock_details(cap_missing)
            if tx2 is not None and not tx2.empty:
                for _, row in tx2.iterrows():
                    c = _norm_hit_code(str(row["code"]))
                    if len(c) == 6 and c in result:
                        mc = float(row.get("market_cap_yi", 0) or 0)
                        if mc > 0:
                            result[c]["market_cap_yi"] = mc
        except Exception:
            pass

    # TOP30 内：与 /api/ranking-live 一致，用现价重算 10 日涨幅（避免与涨幅榜不一致）
    for code in code_list:
        row = rank_map.get(code)
        if not row or code not in result:
            continue
        lp = result[code].get("price")
        if lp is None:
            continue
        g10 = _live_gain_10d_from_snapshot_row(row, float(lp))
        if g10 is not None:
            result[code]["gain_10d"] = g10

    return JSONResponse(_sanitize_json(result))


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
async def get_market_insight(refresh_breadth: int = 0):
    """获取四维市场洞察（板块集中度/资金行为/情绪领袖/周期波形）

    盘内只返回落盘快照，不自动拉 akshare 全市场。
    refresh_breadth=1 且已过 15:00（或非交易日）时才重算 breadth。
    """
    insight_file = DATA_DIR / "latest_insight.json"
    data = load_json_file(insight_file)
    if not isinstance(data, dict):
        data = {"date": "", "sector_heats": [], "wave": None}

    from src.engine.market_insight import _compute_market_breadth
    from src.market_schedule import (
        HEAVY_MARKET_NETWORK_LOCK,
        allow_market_breadth_network_refresh,
        is_intraday_trading_session,
    )

    if allow_market_breadth_network_refresh(bool(refresh_breadth)):
        try:

            def _breadth_job():
                with HEAVY_MARKET_NETWORK_LOCK:
                    return _compute_market_breadth()

            fresh = await run_in_threadpool(_breadth_job)
            if fresh:
                data = dict(data)
                data["breadth"] = fresh
                try:
                    dump_json_file(insight_file, data)
                except Exception as e:
                    print(f"[市场洞察] breadth 写回失败: {e}")
        except Exception as e:
            print(f"[市场洞察] breadth 刷新失败: {e}")
    elif bool(refresh_breadth) and is_intraday_trading_session():
        data = dict(data)
        data["breadth_refresh_skipped"] = "intraday"

    from src.engine.market_insight import _breadth_looks_stale

    b = data.get("breadth") if isinstance(data, dict) else None
    if _breadth_looks_stale(b):
        data = dict(data)
        stale = dict(b) if isinstance(b, dict) else {}
        stale["counts_unavailable"] = True
        data["breadth"] = stale

    return JSONResponse(data)


def _sanitize_json(obj):
    """递归把 NaN/Inf 收敛为 None；JSONResponse 默认不允许这两类值"""
    import math as _math
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(v) for v in obj]
    if isinstance(obj, float):
        if _math.isnan(obj) or _math.isinf(obj):
            return None
    return obj


def _get_screener_history_payload(year: int, month: int, *, light: bool = False) -> dict:
    """同步构建选股记录 JSON（含可能较慢的全市场 spot 回填）；在线程池执行以免阻塞其它 API。

    light=True：默认仅读库+统计；盘内且非 EOD 时才做连板校正 / 轻量刷新收盘价。
    """
    from src.engine.screener_history import (
        get_history,
        calc_win_stats,
        calc_monthly_trend,
        refresh_today_records,
        list_available_periods,
        reconcile_history_board_counts,
    )
    from src.market_schedule import (
        allow_screener_history_light_write,
        is_eod_bundle_running,
    )

    light_write = bool(light) and allow_screener_history_light_write()
    if light and not light_write and is_eod_bundle_running():
        print("[选股记录] EOD 运行中，light=1 只读跳过写库")

    if not is_eod_bundle_running() and (light_write or not light):
        try:
            reconcile_history_board_counts(trade_dates=[now_cn().strftime("%Y-%m-%d")])
        except Exception as e:
            print(f"[选股记录] 连板校正失败: {e}")

    if light_write:
        try:
            from src.engine.screener_history import refresh_today_close_light

            refresh_today_close_light()
        except Exception as e:
            print(f"[选股记录] 盘后轻量刷新收盘价失败: {e}")
        try:
            n = now_cn()
            if n.hour > 9 or (n.hour == 9 and n.minute >= 25):
                from src.engine.next_day_sell_advice import backfill_next_day_sell_advice
                from src.engine.screener_market_env import load_screener_market_env

                env = load_screener_market_env()
                sf = load_json_file(DATA_DIR / "latest_sentiment.json") or {}
                backfill_next_day_sell_advice(
                    market_limit_down=(sf.get("market") or {}).get("limit_down"),
                    b1_rate=env.get("b1_rate"),
                )
        except Exception as e:
            print(f"[选股记录] light 次日卖出建议失败: {e}")

    if not light and not is_eod_bundle_running():
        try:
            refresh_today_records()
        except Exception as e:
            print(f"[选股记录] refresh_today_records 失败: {e}")

        try:
            from src.engine.screener_history import (
                backfill_missing_b1_and_decision,
                repair_missing_close_prices,
                reconcile_next_day_from_kline,
            )

            repair_missing_close_prices()
            reconcile_next_day_from_kline()
            backfill_missing_b1_and_decision()
        except Exception as e:
            print(f"[选股记录] 历史补缺失败: {e}")

    if not light and not is_eod_bundle_running():
        try:
            from src.engine.screener_history import backfill_next_day_auction, _load

            n = now_cn()
            cutoff = n.replace(hour=9, minute=25, second=0, microsecond=0)
            if n >= cutoff:
                today_str = n.strftime("%Y-%m-%d")
                recs = _load()
                need_backfill = any(
                    r.get("date") and r.get("date") < today_str and r.get("next_day_open") is None for r in recs
                )
                if need_backfill:
                    from src.data.fetcher import fetch_realtime_spot

                    spot = fetch_realtime_spot()
                    if spot is not None and not spot.empty:
                        backfill_next_day_auction(spot)
                        try:
                            from src.engine.next_day_sell_advice import backfill_next_day_sell_advice
                            from src.engine.screener_market_env import load_screener_market_env

                            env = load_screener_market_env()
                            sf = load_json_file(DATA_DIR / "latest_sentiment.json") or {}
                            backfill_next_day_sell_advice(
                                market_limit_down=(sf.get("market") or {}).get("limit_down"),
                                b1_rate=env.get("b1_rate"),
                            )
                        except Exception as e_sell:
                            print(f"[选股记录] 次日卖出建议回填失败: {e_sell}")
                        from src.engine.trend_screener import backfill_trend_morning_auction

                        backfill_trend_morning_auction(spot)
        except Exception as e:
            print(f"[选股记录] 昨日次日竞价回填失败: {e}")
    y = year if year > 0 else None
    m = month if month > 0 else None
    payload = {
        "records": get_history(limit=0, year=y, month=m),
        "stats": calc_win_stats(filter_year=y, filter_month=m),
        "monthly_trend": calc_monthly_trend(6),
        "available_periods": list_available_periods(),
        "read_only_light": bool(light) and not light_write,
    }
    return _sanitize_json(payload)


@app.post("/api/screener-history/sync-today")
async def sync_screener_history_today():
    """将 latest_screener / daily_screener_hit 补写入 screener_history（补跑后选股记录为空时用）。"""
    try:
        from src.engine.screener_history import ensure_today_archived

        n = await run_in_threadpool(ensure_today_archived)
        return JSONResponse({"status": "ok", "today_count": n})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/monthly-review")
async def get_monthly_review(year: int = 0, month: int = 0):
    """月度复盘聚合接口：绩效对比、趋势、连板归因、开仓指导与改进建议。"""
    try:
        from src.engine.screener_history import build_monthly_review

        y = year if year > 0 else None
        m = month if month > 0 else None
        payload = build_monthly_review(y, m)
        return JSONResponse(_sanitize_json(payload))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/screener-history")
async def get_screener_history(year: int = 0, month: int = 0, light: int = 0):
    """获取选股记录+胜率统计

    Query 参数：
        year, month: 联动过滤记录与分维度统计；不传则返回全量
            - total/weekly 始终为全量/实时
            - monthly = 选定 year+month（不传则当前自然月）
            - yearly = 选定 year（不传则当前自然年）
            - by_*（分维度）= 选定 year+month 子集
        light=1: 仅读库+统计，跳过 K 线拉网修正（首页首屏，秒开）

    默认（light=0）每次请求时对今日记录做一次幂等修正（用最新K线和最新 sentiment 校正
    收盘价/10日涨幅/加权竞价等指标，与首页/历史页对齐）。
    """
    try:
        # light=1 仅读 DuckDB，走主线程避免与 threadpool 其它任务叠加触发 Windows 原生崩溃
        if bool(int(light or 0)):
            payload = _get_screener_history_payload(year, month, light=True)
        else:
            payload = await run_in_heavy_pool(
                _get_screener_history_payload, year, month, light=False
            )
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse(
            {"error": str(e), "records": [], "stats": {}, "available_periods": []},
            status_code=500,
        )


@app.get("/api/screener-backtest")
async def get_screener_backtest(from_date: str = "", to_date: str = ""):
    """历史选股收益回测统计（信号日收盘买 → 次日收盘卖）。"""
    try:
        from src.engine.screener_backtest_report import build_screener_backtest_report

        report = await run_in_heavy_pool(
            build_screener_backtest_report,
            date_from=from_date.strip() or None,
            date_to=to_date.strip() or None,
        )
        return JSONResponse(_sanitize_json(report))
    except Exception as e:
        return JSONResponse({"error": str(e), "summary": {}}, status_code=500)


# ========== 自选股模块已移除 ==========

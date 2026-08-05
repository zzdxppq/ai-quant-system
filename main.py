"""AI量化周期看板 — 启动入口"""
import os

# 避免 akshare 全市场拉取时 tqdm 刷屏；降低部分 Windows 环境下 stderr 异常导致控制台退出
os.environ.setdefault("TQDM_DISABLE", "1")

# 国内行情接口（新浪/东财/腾讯）不应走翻墙代理（mihomo/clash 等）。
# 在 import 任何 httpx 模块前清除继承自 shell 的代理变量，避免 httpx.trust_env 读到失效代理。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import sys
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

# src.config 内部会自动加载 .env
from src.config import API_HOST, API_PORT, SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE, DATA_DIR, TZ_CN
from src.data.models import init_db


def setup_scheduler():
    """配置定时任务（所有 cron 均使用 UTC+8 北京时间）"""
    from src.scheduler import (
        run_eod_bundle,
        run_evening_mainboard_sanbanzhu_batch,
        run_ranking_top30_refresh,
        run_screener_update,
        wrap_cron_job,
    )

    scheduler = BackgroundScheduler(timezone=TZ_CN)

    # 盘后串行（交易日 18:00）：周期/TOP30/涨停缓存/趋势 → 复盘/决策追踪/趋势回填
    # 推迟到 18:00 避开收盘后东财接口高并发时段，提高 zt_pool 拉取成功率
    scheduler.add_job(
        wrap_cron_job("eod_bundle", run_eod_bundle),
        "cron",
        day_of_week="mon-fri",
        hour=18,
        minute=0,
        id="eod_bundle",
        misfire_grace_time=3600,
        max_instances=1,
    )

    # 早盘选股（交易日 9:27 北京时间）
    scheduler.add_job(
        wrap_cron_job("screener_update", run_screener_update),
        "cron",
        day_of_week="mon-fri",
        hour=SCREENER_CRON_HOUR,
        minute=SCREENER_CRON_MINUTE,
        id="screener_update",
        misfire_grace_time=300,
        max_instances=1,
    )

    # 盘中 TOP30 每 10 分钟轻量刷新（仅更新现价/涨幅，不全市场扫描）
    # 交易时段每 10 分钟执行一次（函数内部判断时间）
    scheduler.add_job(
        wrap_cron_job("ranking_top30_refresh", run_ranking_top30_refresh),
        "cron",
        day_of_week="mon-fri",
        minute="*/10",
        id="ranking_top30_refresh",
        misfire_grace_time=120,
        max_instances=1,
    )

    # 趋势选股/复盘等已并入 15:50 eod_bundle 串行执行。

    # 三板组：交易日 20:00 先主板涨停池再批量拉龙虎榜（收盘后榜单约齐）
    scheduler.add_job(
        wrap_cron_job("evening_sanbanzhu_batch", run_evening_mainboard_sanbanzhu_batch),
        "cron",
        day_of_week="mon-fri",
        hour=20,
        minute=0,
        id="evening_sanbanzhu_batch",
        misfire_grace_time=3600,
        max_instances=1,
    )

    scheduler.start()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").strip()
    if not smtp_user or not smtp_pass:
        print("[邮件] 未配置 SMTP_USER/SMTP_PASSWORD，9:27 选股后将跳过邮件推送（请在 .env 或容器 --env-file 中配置）")
    print(f"定时任务已启动（UTC+8 北京时间）:")
    print(f"  - 盘后串行(周期/TOP30/涨停/趋势/复盘/回填): 周一至周五 18:00")
    print(f"  - 选股执行: 周一至周五 {SCREENER_CRON_HOUR}:{SCREENER_CRON_MINUTE:02d}")
    print(f"  - TOP30轻量刷新: 周一至周五 9:35-15:05 每10分钟（实时数据见 /api/ranking-live）")
    print(f"  - 三板组晚间批量(涨停池→龙虎榜): 周一至周五 20:00")
    from src.scheduler import SCHEDULER_LOG_PATH

    print(f"  - cron 起止日志: {SCHEDULER_LOG_PATH}")

    return scheduler


def _print_startup_lock_help(exc: BaseException) -> bool:
    """DuckDB 单写：旧进程未退出时给出可操作的提示。"""
    msg = str(exc)
    low = msg.lower()
    if "quant.duckdb" not in low and "already open" not in low:
        return False
    if "already open" not in low and "cannot open file" not in low and "正在使用" not in msg:
        return False
    print("\n[启动失败] data/quant.duckdb 正被其他 Python 进程占用（通常是上次 main.py 未退出）。")
    print("  一键结束旧服务:  powershell -File scripts/kill_quant.ps1")
    print("  一键重启:        powershell -File scripts/restart_server.ps1")
    print("  若 taskkill 无效: wmic process where ProcessId=<pid> delete")
    print("  （旧进程可能不占 8000 端口，仅占用 duckdb 文件）")
    print(f"  详情: {exc}\n")
    return True


def main():
    # 初始化
    DATA_DIR.mkdir(exist_ok=True)
    try:
        init_db()
    except Exception as e:
        if _print_startup_lock_help(e):
            sys.exit(1)
        raise
    print("数据库初始化完成")

    # 启动定时任务
    scheduler = setup_scheduler()

    # 如果传入 --update 参数，立即执行一次更新
    if "--update" in sys.argv:
        from src.scheduler import run_cycle_update
        run_cycle_update()

    if "--post-market" in sys.argv:
        from src.scheduler import run_post_market_bundle

        run_post_market_bundle()

    # 仅补发今日复盘邮件（读 latest_review.json，force 绕过幂等/时间窗）
    if "--send-review-email" in sys.argv:
        from src.notify.email_sender import send_review_report

        ok = send_review_report(entry="manual_send_review", force=True)
        print(f"[复盘邮件] 手动推送结果: {'成功' if ok else '失败/跳过'}")
        sys.exit(0 if ok else 1)

    if "--catchup-eod" in sys.argv:
        from src.scheduler import (
            run_eod_bundle,
            run_evening_mainboard_sanbanzhu_batch,
        )

        print("[catchup-eod] 1/2 run_eod_bundle (≈18:00 盘后串行)...")
        run_eod_bundle()
        print("[catchup-eod] 2/2 run_evening_mainboard_sanbanzhu_batch (≈20:00)...")
        run_evening_mainboard_sanbanzhu_batch()
        print("[catchup-eod] 完成。")

    if "--screen" in sys.argv:
        from src.scheduler import run_screener_update
        run_screener_update()

    if "--evening-sanbanzhu" in sys.argv:
        from src.scheduler import run_evening_mainboard_sanbanzhu_batch

        run_evening_mainboard_sanbanzhu_batch()

    # 启动 Web 服务
    import faulthandler

    faulthandler.enable()
    from src.api.app import app

    print(f"\n看板地址: http://{API_HOST}:{API_PORT}")
    print("服务运行中，请勿关闭本窗口；结束请按 Ctrl+C\n")
    try:
        uvicorn.run(app, host=API_HOST, port=API_PORT)
    except KeyboardInterrupt:
        print("\n[退出] 已停止服务")
    except Exception as e:
        print(f"\n[致命错误] 服务异常退出: {e}")
        raise


if __name__ == "__main__":
    main()

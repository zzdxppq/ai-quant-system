"""AI量化周期看板 — 启动入口"""
import sys
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

# src.config 内部会自动加载 .env
from src.config import API_HOST, API_PORT, SCREENER_CRON_HOUR, SCREENER_CRON_MINUTE, DATA_DIR
from src.data.models import init_db


def setup_scheduler():
    """配置定时任务"""
    from src.scheduler import run_cycle_update, run_screener_update, run_ranking_refresh

    scheduler = BackgroundScheduler()

    # 收盘后更新周期（交易日 15:30）
    scheduler.add_job(
        run_cycle_update,
        "cron",
        day_of_week="mon-fri",
        hour=15,
        minute=30,
        id="cycle_update",
        misfire_grace_time=3600,
    )

    # 早盘选股（交易日 9:27）
    scheduler.add_job(
        run_screener_update,
        "cron",
        day_of_week="mon-fri",
        hour=SCREENER_CRON_HOUR,
        minute=SCREENER_CRON_MINUTE,
        id="screener_update",
        misfire_grace_time=300,
    )

    # 盘中排行刷新（交易日 10:00）
    scheduler.add_job(
        run_ranking_refresh,
        "cron",
        day_of_week="mon-fri",
        hour=10,
        minute=0,
        id="ranking_refresh",
        misfire_grace_time=600,
    )

    scheduler.start()
    print(f"定时任务已启动:")
    print(f"  - 周期更新: 周一至周五 15:30")
    print(f"  - 选股执行: 周一至周五 {SCREENER_CRON_HOUR}:{SCREENER_CRON_MINUTE:02d}")
    print(f"  - 排行刷新: 周一至周五 10:00")

    return scheduler


def main():
    # 初始化
    DATA_DIR.mkdir(exist_ok=True)
    init_db()
    print("数据库初始化完成")

    # 启动定时任务
    scheduler = setup_scheduler()

    # 如果传入 --update 参数，立即执行一次更新
    if "--update" in sys.argv:
        from src.scheduler import run_cycle_update
        run_cycle_update()

    if "--screen" in sys.argv:
        from src.scheduler import run_screener_update
        run_screener_update()

    # 启动 Web 服务
    from src.api.app import app
    print(f"\n看板地址: http://{API_HOST}:{API_PORT}")
    uvicorn.run(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    main()

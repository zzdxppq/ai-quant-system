"""AI量化周期看板 — 启动入口"""
import os
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
    from src.scheduler import run_cycle_update, run_screener_update, run_ranking_refresh

    scheduler = BackgroundScheduler(timezone=TZ_CN)

    # 收盘后更新周期（交易日 15:30 北京时间）
    scheduler.add_job(
        run_cycle_update,
        "cron",
        day_of_week="mon-fri",
        hour=15,
        minute=30,
        id="cycle_update",
        misfire_grace_time=3600,
    )

    # 早盘选股（交易日 9:27 北京时间）
    scheduler.add_job(
        run_screener_update,
        "cron",
        day_of_week="mon-fri",
        hour=SCREENER_CRON_HOUR,
        minute=SCREENER_CRON_MINUTE,
        id="screener_update",
        misfire_grace_time=300,
    )

    # 盘中排行刷新（交易日 10:00 北京时间）
    scheduler.add_job(
        run_ranking_refresh,
        "cron",
        day_of_week="mon-fri",
        hour=10,
        minute=0,
        id="ranking_refresh_am",
        misfire_grace_time=600,
    )

    # 午间排行刷新（交易日 12:30 北京时间，早盘结束后）
    scheduler.add_job(
        run_ranking_refresh,
        "cron",
        day_of_week="mon-fri",
        hour=12,
        minute=30,
        id="ranking_refresh_noon",
        misfire_grace_time=600,
    )

    # 收盘后综合任务（交易日 15:45 北京时间）
    def _run_post_market():
        # 1. 盘后复盘（次日鱼塘）
        try:
            from src.engine.daily_review import run_daily_review
            run_daily_review()
        except Exception as e:
            print(f"[复盘] 异常: {e}")

        # 2. 决策追踪回填
        try:
            from src.engine.decision_tracker import backfill_result
            from src.config import now_cn
            backfill_result(now_cn().strftime("%Y-%m-%d"))
        except Exception as e:
            print(f"[决策追踪] 回填异常: {e}")

        # 3. 趋势选股 + 历史回填
        try:
            from src.engine.trend_screener import run_trend_screener, backfill_trend_next_day
            run_trend_screener()
            backfill_trend_next_day()
        except Exception as e:
            print(f"[趋势选股] 异常: {e}")

    scheduler.add_job(
        _run_post_market,
        "cron",
        day_of_week="mon-fri",
        hour=15,
        minute=45,
        id="post_market",
        misfire_grace_time=3600,
    )

    scheduler.start()
    print(f"定时任务已启动（UTC+8 北京时间）:")
    print(f"  - 周期更新: 周一至周五 15:30")
    print(f"  - 选股执行: 周一至周五 {SCREENER_CRON_HOUR}:{SCREENER_CRON_MINUTE:02d}")
    print(f"  - 排行刷新: 周一至周五 10:00 / 12:30")
    print(f"  - 盘后复盘+决策追踪: 周一至周五 15:45")

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

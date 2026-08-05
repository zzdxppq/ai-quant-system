#!/usr/bin/env python3
"""拉取今日（当前）行情与业务快照：排行 → 选股链 → 周期 → 盘后复盘。

与 main.py 一致：在 import 业务模块前清除代理环境变量，避免 httpx 走失效代理。

用法（项目根）:
  python -u scripts/pull_today_data.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")


def main() -> int:
    from src.data.models import init_db
    from src.scheduler import run_cycle_update, run_ranking_refresh, run_screener_update

    init_db()

    def _p(msg: str) -> None:
        print(msg, flush=True)

    _p("=== 1/4 排行 + 市场洞察 ===")
    run_ranking_refresh()
    _p("=== 2/4 选股（邮件已跳过）===")
    run_screener_update(skip_email=True)
    _p("=== 3/4 周期更新 ===")
    run_cycle_update()
    _p("=== 4/4 盘后复盘 ===")
    try:
        from src.engine.daily_review import run_daily_review

        run_daily_review()
    except Exception as e:
        _p(f"daily_review 失败: {e}")
    _p("=== 完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

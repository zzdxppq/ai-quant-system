#!/usr/bin/env python3
"""删除 data/ 根目录下「未被系统使用」的 .json 文件（仅根目录，不递归子文件夹）。

白名单来自当前代码库会读写的业务文件名；其余根目录 *.json 视为可清理（如临时导出、误拷文件）。
月报 `report_YYYY_MM.json` 保留。

用法：
  python scripts/prune_unused_data_json.py          # dry-run 打印将删文件
  python scripts/prune_unused_data_json.py --apply  # 执行删除
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ALLOWED_ROOT_JSON = frozenset(
    {
        "_stock_list_cache.json",
        "active_attack_history.json",
        "concept_cache.json",
        "concept_cache.json.partial",
        "cycle_history.json",
        "cycle_state.json",
        "decision_records.json",
        "industry_cache.json",
        "latest_advice.json",
        "latest_auction_scores.json",
        "latest_deviation.json",
        "latest_insight.json",
        "latest_leader.json",
        "latest_ranking.json",
        "latest_review.json",
        "latest_screener.json",
        "latest_sentiment.json",
        "latest_signals.json",
        "latest_snapshot.json",
        "latest_trend.json",
        "limit_up_cache.json",
        "my_holdings.json",
        "prev_ranking.json",
        "review_history.json",
        "sanbanzhu_lhb_cache.json",
        "screener_history.json",
        "sentiment_history.json",
        "top30_streak_state.json",
        "trend_history.json",
        "trend_sector_history.json",
    }
)
REPORT_JSON = re.compile(r"^report_\d{4}_\d{2}\.json$")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent / "data"
    if not root.is_dir():
        print("无 data 目录")
        return

    to_delete: list[Path] = []
    for p in sorted(root.glob("*.json")):
        name = p.name
        if name in ALLOWED_ROOT_JSON or REPORT_JSON.match(name):
            continue
        to_delete.append(p)

    for p in to_delete:
        print(f"{'DEL' if args.apply else 'DRY'} {p.name}")
        if args.apply:
            try:
                p.unlink()
            except OSError as e:
                print(f"  失败: {e}")

    print(f"完成: {'已删除' if args.apply else 'dry-run'} {len(to_delete)} 个根目录 json")


if __name__ == "__main__":
    main()

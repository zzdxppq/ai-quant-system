"""一次性全市场 概念→成分 映射缓存构建器

数据源：东财 push2 接口，无 token。首次全量约 60-90s。
之后 scanner 直接查 cache，不在线拉。

用法:
  python3 scripts/build_concept_cache.py              # 全量构建
  python3 scripts/build_concept_cache.py --workers 5  # 调并发（默认3）
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.config  # noqa: F401  触发 .env 加载
from src.data.concept_fetcher import build_concept_cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3,
                    help="并发线程数，默认 3（东财限流敏感）")
    ap.add_argument("--save-every", type=int, default=50,
                    help="每 N 个板块保存一次断点，默认 50")
    args = ap.parse_args()
    build_concept_cache(workers=args.workers, save_every=args.save_every)


if __name__ == "__main__":
    main()

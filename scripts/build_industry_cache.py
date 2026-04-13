"""一次性全市场 code→行业 映射缓存构建器

用途：独立跑一次填满 data/industry_cache.json，之后 scanner 直接查 cache，
不再在扫描阶段调 emweb / push2。行业分类几乎不变，一周刷一次即可。

用法：
  python3 scripts/build_industry_cache.py              # 只补齐缺失/空值
  python3 scripts/build_industry_cache.py --force      # 强制全量重建
  python3 scripts/build_industry_cache.py --workers 12 # 调并发
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.config  # noqa: F401  触发 .env 加载
from src.config import DATA_DIR
from src.data.ranking_scanner import _fetch_industry_emweb, fetch_full_market_spot

CACHE_PATH = DATA_DIR / "industry_cache.json"


def _load_cache() -> dict[str, str]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    tmp.replace(CACHE_PATH)


def build(force: bool, workers: int, save_every: int) -> None:
    print("=" * 60)
    print(f"行业映射构建器 | force={force} workers={workers}")

    spot = fetch_full_market_spot()
    if spot.empty:
        print("[致命] 全市场 spot 为空，放弃")
        sys.exit(1)
    universe = spot["code"].astype(str).tolist()
    print(f"全市场 universe: {len(universe)} 只")

    cache = _load_cache()
    if force:
        targets = universe
    else:
        targets = [c for c in universe if not cache.get(c)]
    print(f"待拉取: {len(targets)} 只 (已命中 {len(universe) - len(targets)})")

    if not targets:
        print("无需更新")
        return

    t0 = time.time()
    done = 0
    hits = 0
    last_save = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_industry_emweb, c): c for c in targets}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                ind = fut.result()
            except Exception:
                ind = ""
            cache[code] = ind
            done += 1
            if ind:
                hits += 1

            if done - last_save >= save_every:
                _save_cache(cache)
                last_save = done
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(targets) - done) / rate if rate > 0 else 0
                print(f"  进度 {done}/{len(targets)} 命中 {hits} "
                      f"rate={rate:.1f}/s ETA={eta:.0f}s")

    _save_cache(cache)
    elapsed = time.time() - t0
    empty_now = sum(1 for c in universe if not cache.get(c))
    print("-" * 60)
    print(f"完成: {done} 拉取, {hits} 命中, 耗时 {elapsed:.1f}s")
    print(f"全市场覆盖率: {len(universe) - empty_now}/{len(universe)} "
          f"({(1 - empty_now / len(universe)) * 100:.1f}%)")
    print(f"写入: {CACHE_PATH}")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="强制全量重建（忽略已有值）")
    ap.add_argument("--workers", type=int, default=8,
                    help="并发线程数，默认 8")
    ap.add_argument("--save-every", type=int, default=200,
                    help="每 N 只保存一次，默认 200")
    args = ap.parse_args()
    build(force=args.force, workers=args.workers, save_every=args.save_every)


if __name__ == "__main__":
    main()

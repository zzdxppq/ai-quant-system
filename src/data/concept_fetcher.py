"""东财概念板块爬虫 — 全市场 code→[concept_names] 映射

数据源（push2.eastmoney.com，无 token）：
- 概念板块列表：fs=m:90+t:3   → ~400 个 BKxxxx
- 板块成分股：  fs=b:BKxxxx   → 该板块所有成分股

存储格式（concept_cache.json）：
{
  "_meta": {updated_at, concept_count, stock_count, elapsed_sec},
  "concepts": {"BK0xxx": {"name": "特种气体", "stocks": ["600156", ...]}},
  "stock_to_concepts": {"600156": ["特种气体", "半导体材料"]}
}

下游消费：
- load_stock_to_concepts() → {code: [concept_name, ...]}
- load_concept_to_stocks() → {concept_name: [code, ...]}
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from src.config import DATA_DIR, now_cn

_PUSH2_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
_MAX_RETRY = 3
CACHE_PATH = DATA_DIR / "concept_cache.json"
PARTIAL_PATH = DATA_DIR / "concept_cache.json.partial"

_PER_THREAD_SLEEP = 0.5  # 每个板块成分请求后 sleep 0.5s 限流


def _normalize_diff(diff) -> list[dict]:
    """东财 push2 偶尔返回 dict（key=序号 str），统一成 list[dict]"""
    if isinstance(diff, list):
        return diff
    if isinstance(diff, dict):
        return list(diff.values())
    return []


def _get_diff_with_retry(params: dict) -> list[dict]:
    """统一 GET + 重试 + 解析 diff（302→push2delay 等问题集中处理）"""
    last_err = None
    for attempt in range(_MAX_RETRY):
        try:
            with httpx.Client(timeout=10, headers=_HEADERS,
                              http2=False, follow_redirects=True) as c:
                r = c.get(_PUSH2_URL, params=params)
            if r.status_code != 200:
                last_err = f"http {r.status_code}"
                time.sleep(0.5 * (attempt + 1))
                continue
            data = (r.json() or {}).get("data") or {}
            return _normalize_diff(data.get("diff"))
        except Exception as e:
            last_err = str(e)
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(last_err or "unknown")


def fetch_concept_list() -> list[dict]:
    """拉所有概念板块列表 → [{bk_code, name}]，分页100条/次"""
    out: list[dict] = []
    pn = 1
    while True:
        params = {
            "fs": "m:90+t:3",
            "fields": "f12,f14",
            "pn": pn,
            "pz": 100,
            "po": 1,
            "fltt": 2,
            "_": int(time.time() * 1000),
        }
        try:
            diff = _get_diff_with_retry(params)
        except Exception as e:
            print(f"[概念列表] page {pn} 失败: {e}")
            break
        if not diff:
            break
        for item in diff:
            bk = str(item.get("f12") or "").strip()
            name = str(item.get("f14") or "").strip()
            if bk and name:
                out.append({"bk_code": bk, "name": name})
        if len(diff) < 100:
            break
        pn += 1
        time.sleep(0.3)
    return out


def fetch_concept_constituents(bk_code: str) -> list[str]:
    """单个板块成分股代码列表，分页100条/次"""
    out: list[str] = []
    pn = 1
    while True:
        params = {
            "fs": f"b:{bk_code}",
            "fields": "f12",
            "pn": pn,
            "pz": 100,
            "po": 1,
            "fltt": 2,
            "_": int(time.time() * 1000),
        }
        try:
            diff = _get_diff_with_retry(params)
        except Exception:
            return out  # 局部失败：返回已抓到的，不阻断整体
        if not diff:
            break
        for item in diff:
            code = str(item.get("f12") or "").strip()
            if code and code.isdigit():
                out.append(code)
        if len(diff) < 100:
            break
        pn += 1
    return out


def _save(data: dict) -> None:
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(CACHE_PATH)


def _save_partial(concepts: dict) -> None:
    PARTIAL_PATH.write_text(json.dumps({"concepts": concepts}, ensure_ascii=False, indent=2))


def build_concept_cache(workers: int = 3, save_every: int = 50) -> dict:
    """全量构建 concept_cache.json

    流程：
    1. 拉概念板块列表（~400 个）
    2. 并发抓每个板块成分股，每线程 sleep 0.5s 限流
    3. 反向生成 stock_to_concepts
    4. 写盘（断点已在 save_every 时 flush 到 .partial）
    """
    print("=" * 60)
    print(f"概念缓存构建器 | workers={workers}")
    t0 = time.time()

    boards = fetch_concept_list()
    if not boards:
        print("[致命] 概念板块列表为空，放弃")
        return {}
    print(f"概念板块: {len(boards)} 个")

    concepts: dict[str, dict] = {}

    def _task(b: dict) -> tuple[str, str, list[str]]:
        stocks = fetch_concept_constituents(b["bk_code"])
        time.sleep(_PER_THREAD_SLEEP)
        return b["bk_code"], b["name"], stocks

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_task, b): b for b in boards}
        for fut in as_completed(futs):
            try:
                bk_code, name, stocks = fut.result()
                if stocks:
                    concepts[bk_code] = {"name": name, "stocks": stocks}
            except Exception as e:
                print(f"  板块抓取异常: {e}")
            done += 1
            if done % save_every == 0:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(boards) - done) / rate if rate > 0 else 0
                print(f"  进度 {done}/{len(boards)} 命中{len(concepts)} "
                      f"rate={rate:.1f}/s ETA={eta:.0f}s")
                _save_partial(concepts)

    # 反向映射
    stock_to_concepts: dict[str, list[str]] = {}
    for info in concepts.values():
        for code in info["stocks"]:
            stock_to_concepts.setdefault(code, []).append(info["name"])
    for code in stock_to_concepts:
        stock_to_concepts[code] = sorted(set(stock_to_concepts[code]))

    elapsed = time.time() - t0
    out = {
        "_meta": {
            "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "concept_count": len(concepts),
            "stock_count": len(stock_to_concepts),
            "elapsed_sec": round(elapsed, 1),
        },
        "concepts": concepts,
        "stock_to_concepts": stock_to_concepts,
    }
    _save(out)
    if PARTIAL_PATH.exists():
        try:
            PARTIAL_PATH.unlink()
        except Exception:
            pass
    print("-" * 60)
    print(f"完成: {len(concepts)} 概念 / {len(stock_to_concepts)} 股, 耗时 {elapsed:.1f}s")
    print(f"写入: {CACHE_PATH}")
    print("=" * 60)
    return out


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def load_stock_to_concepts() -> dict[str, list[str]]:
    """读取 {code: [concept_name, ...]}，用于 scanner enrich"""
    return _load_cache().get("stock_to_concepts") or {}


def load_concept_to_stocks() -> dict[str, list[str]]:
    """读取 {concept_name: [code, ...]}，用于 stats 聚合"""
    cache = _load_cache()
    out: dict[str, list[str]] = {}
    for info in (cache.get("concepts") or {}).values():
        nm = info.get("name") or ""
        if nm:
            out[nm] = list(info.get("stocks") or [])
    return out


def cache_meta() -> dict:
    """读 _meta（用于判定是否需要刷新）"""
    return _load_cache().get("_meta") or {}

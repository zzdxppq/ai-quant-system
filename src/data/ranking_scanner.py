"""全市场10日涨幅扫描器（盘后用）

逻辑：
1. 拉全市场 spot（约5000只，含价格/市值/今开/昨收/涨跌幅）
2. 过滤 ST / *ST / 退 / 停牌
3. 并行拉 60 根日K → 计算精确 10 日涨幅，剔除新股（<60交易日）
4. 按 10 日涨幅排序取 top_n
5. 富化 top_n：东财涨停板池(连板数+最后封板时间) + 240 周线偏离度

暴露给 fetcher 的入口：scan_full_market_10d_ranking(top_n)
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

import pandas as pd

from src.config import DATA_DIR
from src.data.sina_kline_api import NEW_STOCK_MIN_TRADING_DAYS


def fetch_full_market_spot() -> pd.DataFrame:
    """全市场 spot：一次性拿到 ~5000 只

    优先 akshare.stock_zh_a_spot_em（东财一次全量、含流通市值），
    失败则回退 sina（不含 market_cap，填 0）。
    """
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            raise RuntimeError("stock_zh_a_spot_em 返回空")

        col_map = {
            "代码": "code",
            "名称": "name",
            "最新价": "close",
            "涨跌幅": "change_pct",
            "今开": "open",
            "昨收": "pre_close",
            "流通市值": "market_cap",
            "总市值": "total_market_cap",
        }
        df = df.rename(columns=col_map)
        for col in ["close", "change_pct", "open", "pre_close",
                    "market_cap", "total_market_cap"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            else:
                df[col] = 0.0
        df["code"] = df["code"].astype(str)
        print(f"[东财spot] 全市场 {len(df)} 只")
        return df[["code", "name", "close", "change_pct", "open",
                   "pre_close", "market_cap", "total_market_cap"]]
    except Exception as e:
        print(f"[东财spot] 失败: {e}，回退新浪")

    from src.data.sina_spot_api import fetch_a_share_list_sina
    df = fetch_a_share_list_sina()
    if df.empty:
        return df
    # sina 不含 market_cap，补 0
    if "market_cap" not in df.columns:
        df["market_cap"] = 0.0
    if "total_market_cap" not in df.columns:
        df["total_market_cap"] = 0.0
    return df[["code", "name", "close", "change_pct", "open",
               "pre_close", "market_cap", "total_market_cap"]]


def _fetch_one_gain_10d(code: str, realtime_close: float = 0.0) -> dict | None:
    """单只日K → 10日涨幅；返回 None 仅在拉取失败/当日新股(<2根K线)

    保留 ST、次新股、退市预警股；仅排除当天发行的纯新股。
    若历史不足 11 根，则按"自上市以来最早一根"为基准，等价于"自 IPO 涨幅"，
    避免次新被一刀切。

    Args:
        code: 股票代码
        realtime_close: spot实时价，>0 时优先使用（比K线末根更准）
    """
    from src.data.sina_kline_api import fetch_kline, SCALE_DAILY, calc_10d_gain_from_kline
    df = fetch_kline(code, SCALE_DAILY, datalen=NEW_STOCK_MIN_TRADING_DAYS)
    if df is None or df.empty or len(df) < 2:
        # 至少 2 根 K 线（=昨日+今日），少于 2 根视为当天发行的纯新股，剔除
        return None
    gain = calc_10d_gain_from_kline(df, realtime_close=realtime_close)
    if gain is None:
        return None
    return {"code": code, "gain_10d": gain}


def _scan_10d_gain_parallel(
    codes: list[str],
    spot_prices: dict[str, float] | None = None,
    max_workers: int = 12,
) -> pd.DataFrame:
    """并行扫描 10 日涨幅

    Args:
        codes: 待扫描的股票代码列表
        spot_prices: {code: 实时收盘价} 从spot数据传入，确保涨幅用实时价计算
        max_workers: 并行线程数
    """
    if spot_prices is None:
        spot_prices = {}
    results: list[dict] = []
    total = len(codes)
    print(f"并行扫描 10 日涨幅: {total} 只, workers={max_workers}")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_fetch_one_gain_10d, c, spot_prices.get(c, 0.0)): c
            for c in codes
        }
        done = 0
        for fut in as_completed(futures):
            try:
                r = fut.result()
                if r is not None:
                    results.append(r)
            except Exception:
                pass
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  进度 {done}/{total}, 有效 {len(results)}")

    if not results:
        return pd.DataFrame(columns=["code", "gain_10d"])
    return pd.DataFrame(results)


def _enrich_market_cap(df: pd.DataFrame) -> pd.DataFrame:
    """用腾讯接口补齐 top-N 的流通市值（亿元）

    若 spot 源已带市值（东财路径成功）则跳过；否则调 tencent 批量补。
    """
    if df.empty:
        return df
    # 已有非 0 值（来自东财 spot）则无需补
    existing = pd.to_numeric(df.get("market_cap", 0), errors="coerce").fillna(0)
    if (existing > 0).all():
        return df

    try:
        from src.data.tencent_api import fetch_stock_details
        codes = df["code"].astype(str).tolist()
        tx = fetch_stock_details(codes)
        if tx is None or tx.empty:
            return df
        cap_map = dict(zip(tx["code"].astype(str), tx["market_cap_yi"]))
        df = df.copy()
        # 腾讯返回的单位已是「亿元」，直接放回 market_cap 字段（与东财口径对齐到 *1e8 后）
        df["market_cap"] = df["code"].astype(str).map(
            lambda c: float(cap_map.get(c, 0)) * 1e8
        )
        print(f"[腾讯] 补齐 top{len(df)} 市值")
    except Exception as e:
        print(f"[腾讯] 市值补齐失败: {e}")
    return df


def _load_industry_cache() -> dict[str, str]:
    import json
    p = DATA_DIR / "industry_cache.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_industry_cache(cache: dict[str, str]) -> None:
    import json
    p = DATA_DIR / "industry_cache.json"
    p.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


_EMWEB_F10_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
_EMWEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://emweb.securities.eastmoney.com/",
}


def _to_secucode(code: str) -> str:
    """6位代码 → F10 接口所需的 SHxxxxxx / SZxxxxxx / BJxxxxxx"""
    code = str(code).strip()
    if code.startswith(("50", "51", "52", "55", "56", "58",
                        "60", "68", "90", "110", "113", "118", "132", "204")):
        return f"SH{code}"
    if code.startswith(("43", "82", "83", "87", "88", "89", "92")):
        return f"BJ{code}"
    return f"SZ{code}"


def _fetch_industry_emweb(code: str) -> str:
    """通过 emweb F10 CompanySurvey 拉所属行业

    返回 EM2016 字段的最细分段 (e.g., "食品饮料-饮料-白酒" → "白酒")
    emweb.securities.eastmoney.com 不在 push2 封禁范围，可稳定使用
    """
    import httpx
    secucode = _to_secucode(code)
    for attempt in range(3):
        try:
            with httpx.Client(timeout=8, headers=_EMWEB_HEADERS, http2=False) as client:
                resp = client.get(_EMWEB_F10_URL, params={"code": secucode})
                data = resp.json()
            jbzl = (data.get("jbzl") or [{}])[0]
            em2016 = str(jbzl.get("EM2016") or "").strip()
            if not em2016:
                return ""
            return em2016.split("-")[-1]
        except Exception:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            return ""
    return ""


def _enrich_industry(df: pd.DataFrame) -> pd.DataFrame:
    """为 top-N 补齐所属板块/行业（纯 cache 查询）

    数据源：data/industry_cache.json，由 scripts/build_industry_cache.py 周期性构建。
    扫描阶段不再在线拉 emweb / push2，彻底解耦外部服务可用性。
    缓存缺失的 code 会显示空行业，直到下次运行 builder。
    """
    if df.empty:
        return df

    cache = _load_industry_cache()
    codes = df["code"].astype(str).tolist()

    df = df.copy()
    df["industry"] = df["code"].astype(str).map(lambda c: cache.get(c, ""))

    if not cache:
        print("[行业] cache 为空，请先运行 scripts/build_industry_cache.py")
    else:
        miss = sum(1 for c in codes if not cache.get(c))
        if miss:
            print(f"[行业] cache 命中 {len(codes) - miss}/{len(codes)}，"
                  f"{miss} 只缺失（下次 rebuild cache 时补齐）")
    return df


def _enrich_concepts(df: pd.DataFrame) -> pd.DataFrame:
    """为 top-N 补齐所属概念列表（list[str]）

    数据源：data/concept_cache.json，由 scripts/build_concept_cache.py 周期性构建。
    一只股票可挂多个概念，全量记录；缓存缺失则空列表。
    """
    if df.empty:
        return df

    from src.data.concept_fetcher import load_stock_to_concepts
    cache = load_stock_to_concepts()
    df = df.copy()
    df["concepts"] = df["code"].astype(str).map(lambda c: list(cache.get(c, [])))

    if not cache:
        print("[概念] cache 为空，请先运行 scripts/build_concept_cache.py")
    else:
        codes = df["code"].astype(str).tolist()
        miss = sum(1 for c in codes if not cache.get(c))
        if miss:
            print(f"[概念] cache 命中 {len(codes) - miss}/{len(codes)}")
    return df


def _enrich_zt_pool(df: pd.DataFrame) -> pd.DataFrame:
    """为 top-N 补齐 连板数 + 最后封板时间"""
    try:
        from src.data.zt_pool_api import fetch_zt_pool
        pool = fetch_zt_pool()
    except Exception as e:
        print(f"涨停板池富化失败: {e}")
        pool = {}

    df = df.copy()
    df["continuous_limit_up"] = df["code"].map(
        lambda c: pool.get(str(c), {}).get("lbc", 0)
    ).astype(int)
    df["last_limit_up_time"] = df["code"].map(
        lambda c: pool.get(str(c), {}).get("lbt", "")
    )
    return df


def _enrich_deviation(df: pd.DataFrame, max_workers: int = 8) -> pd.DataFrame:
    """为 top-N 补齐 240 周线偏离度"""
    from src.engine.ma_deviation import check_deviation, _get_weekly_closes

    def _task(row):
        try:
            closes = _get_weekly_closes(str(row["code"]))
            result = check_deviation(
                str(row["code"]), str(row.get("name", "")),
                float(row["close"]), closes,
            )
            return str(row["code"]), result.deviation_pct
        except Exception:
            return str(row["code"]), 0.0

    rows = [row for _, row in df.iterrows()]
    dev_map: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_task, r) for r in rows]
        for fut in as_completed(futures):
            code, dev = fut.result()
            dev_map[code] = dev

    df = df.copy()
    df["deviation_pct"] = df["code"].astype(str).map(lambda c: dev_map.get(c, 0.0))
    return df


def scan_full_market_10d_ranking(top_n: int = 30) -> pd.DataFrame:
    """全市场10日涨幅 top_n 扫描入口

    Returns:
        DataFrame columns:
            rank, code, name, close, market_cap_yi, change_pct, auction_gain,
            gain_10d, continuous_limit_up, last_limit_up_time, deviation_pct,
            is_main_board
    """
    print("=" * 50)
    print("开始全市场10日涨幅扫描...")
    t0 = time.time()

    # 1. 全市场 spot
    spot = fetch_full_market_spot()
    if spot.empty:
        print("[扫描] spot 为空，放弃")
        return pd.DataFrame()

    # 2. 过滤 退市/停牌 — 保留 ST 和次新股（仅排除当天 IPO 在 _fetch_one_gain_10d 中处理）
    name_raw = spot["name"].astype(str)
    universe = spot[
        ~name_raw.str.contains("退")
        & (spot["close"] > 0)
    ].copy()
    print(f"退市/停牌过滤后剩 {len(universe)} 只（保留 ST + 次新）")

    # 3. 并行扫 10 日涨幅（同时过滤新股）
    #    传入 spot 实时价，确保涨幅基于最新行情而非可能延迟的 K 线末根
    spot_prices = dict(zip(
        universe["code"].astype(str),
        universe["close"].astype(float),
    ))
    gain_df = _scan_10d_gain_parallel(
        universe["code"].astype(str).tolist(),
        spot_prices=spot_prices,
        max_workers=12,
    )
    if gain_df.empty:
        print("[扫描] 10日涨幅结果为空")
        return pd.DataFrame()

    # 4. 合并 spot 字段 → 排序取 top_n
    merged = gain_df.merge(universe, on="code", how="left")
    merged["auction_gain"] = merged.apply(
        lambda r: round((r["open"] / r["pre_close"] - 1) * 100, 2)
        if r["pre_close"] > 0 else 0.0,
        axis=1,
    )
    merged["is_main_board"] = merged["code"].apply(_is_main_board)
    merged = (
        merged.sort_values("gain_10d", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
    merged["rank"] = merged.index + 1

    # 5. 富化：腾讯补市值 + 行业 + 概念 + 涨停板池 + 240 周线偏离度
    merged = _enrich_market_cap(merged)
    merged = _enrich_industry(merged)
    merged = _enrich_concepts(merged)
    merged = _enrich_zt_pool(merged)
    merged = _enrich_deviation(merged)

    # 6. 市值换算亿元
    merged["market_cap_yi"] = (
        pd.to_numeric(merged["market_cap"], errors="coerce").fillna(0) / 1e8
    ).round(2)

    elapsed = time.time() - t0
    print(f"扫描完成: top{top_n}, 耗时 {elapsed:.1f}s")
    print("=" * 50)

    keep = [
        "rank", "code", "name", "industry", "concepts", "close", "market_cap_yi",
        "change_pct", "auction_gain", "gain_10d", "continuous_limit_up",
        "last_limit_up_time", "deviation_pct", "is_main_board",
    ]
    return merged[[c for c in keep if c in merged.columns]]


def _is_main_board(code: str) -> bool:
    code = str(code)
    if code.startswith(("300", "301")):
        return False
    if code.startswith("688"):
        return False
    if code.startswith(("8", "4")):
        return False
    return True

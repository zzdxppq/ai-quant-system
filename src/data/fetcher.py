"""数据拉取统一入口

数据源优先级（2026-04 调整，east money push2 被 WAF 限流后）：
1. 东方财富 push2（如可用，字段最全）
2. 新浪（via akshare stock_zh_a_spot，全市场 5500+，缺 volume_ratio / market_cap / turnover）
3. Mock 数据（最终降级）

Screener 对 volume_ratio / market_cap 已做 "有则过滤，无则放行" 软检查
"""
import os

import pandas as pd

USE_MOCK = os.getenv("MOCK", "0") == "1"


def fetch_realtime_spot() -> pd.DataFrame:
    """获取全市场实时快照

    用途：选股引擎9:27调用
    """
    if USE_MOCK:
        from src.data.mock_data import generate_mock_spot
        print("[MOCK] 使用模拟实时行情")
        return generate_mock_spot()

    # 优先东方财富（字段最全）
    try:
        from src.data.eastmoney_api import fetch_a_share_list
        df = fetch_a_share_list()
        if not df.empty:
            return df
        print("东方财富返回空，尝试新浪兜底")
    except Exception as e:
        print(f"东方财富实时行情失败: {e}，尝试新浪兜底")

    # 兜底：新浪 via akshare
    try:
        from src.data.sina_spot_api import fetch_a_share_list_sina
        df = fetch_a_share_list_sina()
        if not df.empty:
            return df
    except Exception as e:
        print(f"新浪兜底失败: {e}")

    from src.data.mock_data import generate_mock_spot
    print("[降级] 使用模拟数据")
    return generate_mock_spot()


def fetch_realtime_batch(codes: list[str]) -> pd.DataFrame:
    """批量获取指定股票实时行情（新浪接口，速度快）

    用途：龙头竞价反馈、单股查询
    """
    if USE_MOCK:
        from src.data.mock_data import generate_mock_spot
        return generate_mock_spot()

    try:
        from src.data.sina_api import fetch_realtime_batch as sina_batch
        return sina_batch(codes)
    except Exception as e:
        print(f"新浪实时接口失败: {e}")
        return pd.DataFrame()


def fetch_gain_10d_ranking(top_n: int = 30) -> pd.DataFrame:
    """获取10日涨幅排行 top_n（盘后全市场扫描）

    主路径：ranking_scanner.scan_full_market_10d_ranking
        - 全市场 spot → 过滤ST/停牌 → 并行60根日K → 剔除新股 → 排序取top_n
        - 富化：涨停板池(连板数+最后封板时间) + 240周线偏离度
    兜底：旧的 eastmoney calc_10d_gain_ranking（候选池采样）
    """
    if USE_MOCK:
        from src.data.mock_data import generate_mock_ranking
        scenario = os.getenv("MOCK_SCENARIO", "small_cycle_start")
        print(f"[MOCK] 使用模拟排行数据，场景: {scenario}")
        return generate_mock_ranking(scenario)

    # 主路径：全市场扫描
    try:
        from src.data.ranking_scanner import scan_full_market_10d_ranking
        result = scan_full_market_10d_ranking(top_n)
        if not result.empty:
            return result
        print("全市场扫描返回空，尝试候选池兜底")
    except Exception as e:
        print(f"全市场扫描失败: {e}，尝试候选池兜底")

    # 兜底：候选池采样（东财 clist 前300只今日涨幅）
    try:
        from src.data.eastmoney_api import calc_10d_gain_ranking
        result = calc_10d_gain_ranking(top_n)
        if not result.empty:
            return result
    except Exception as e:
        print(f"东方财富候选池兜底失败: {e}")

    from src.data.mock_data import generate_mock_ranking
    print("[降级] 使用模拟排行数据")
    return generate_mock_ranking("small_cycle_start")


def fetch_limit_up_history(days: int = 5) -> dict[str, pd.DataFrame]:
    """获取最近N天涨停股列表

    用途：选股引擎连板检测
    策略：今日涨停池（东财→新浪兜底） + 历史涨停池（cache → sina K线回溯补足 → 写回cache）
    保证：返回字典至少覆盖今日+days-1个历史交易日（若 cache 不足会主动回溯并持久化）
    """
    if USE_MOCK:
        from src.data.mock_data import generate_mock_limit_up_history
        print("[MOCK] 使用模拟涨停数据")
        return generate_mock_limit_up_history()

    from src.config import now_cn
    today_str = now_cn().strftime("%Y%m%d")

    # Step 1: 今日涨停池（东财主路径，新浪兜底）
    today_df = None
    try:
        from src.data.eastmoney_api import fetch_limit_up_stocks
        today_df = fetch_limit_up_stocks()
    except Exception as e:
        print(f"东方财富涨停池失败: {e}，尝试新浪")

    if today_df is None or today_df.empty:
        try:
            from src.data.sina_spot_api import fetch_limit_up_stocks_sina
            today_df = fetch_limit_up_stocks_sina()
        except Exception as e:
            print(f"新浪涨停池失败: {e}")

    result: dict[str, pd.DataFrame] = {}
    if today_df is not None and not today_df.empty:
        result[today_str] = today_df
        _save_limit_up_cache(today_str, today_df)

    # Step 2: 加载本地缓存（昨日及更早的持久化历史）
    cache = _load_limit_up_cache()
    for d, df in cache.items():
        result.setdefault(d, df)

    # Step 3: 用 sina K线回溯补足历史涨停池
    #   两种触发条件：
    #   a) 历史天数不足（past_count < days-1）
    #   b) 最近交易日的涨停数据量过少（可能采集不完整，如<10只）
    past_count = sum(1 for d in result.keys() if d != today_str)
    # 检查最近2个交易日涨停数量是否可疑（正常A股每天至少有10+只涨停）
    recent_dates = sorted([d for d in result if d != today_str], reverse=True)[:2]
    cache_suspect = any(len(result[d]) < 10 for d in recent_dates) if recent_dates else False

    if past_count < days - 1 or cache_suspect:
        reason = f"历史仅{past_count}天" if past_count < days - 1 else f"近期涨停数据量可疑({', '.join(f'{d}={len(result[d])}只' for d in recent_dates)})"
        print(f"涨停缓存不完整（{reason}），启动 sina K线回溯补足...")
        try:
            from src.data.sina_spot_api import fetch_limit_up_history_sina
            hist = fetch_limit_up_history_sina(days=days)
            for d, df in hist.items():
                # 回溯结果覆盖缓存中数据量更少的版本
                if d not in result or len(df) > len(result[d]):
                    result[d] = df
                    _save_limit_up_cache(d, df)
        except Exception as e:
            print(f"sina K线回溯补足失败: {e}")

    if not result:
        from src.data.mock_data import generate_mock_limit_up_history
        print("[降级] 使用模拟涨停数据")
        return generate_mock_limit_up_history()

    return result


def fetch_stock_kline(
    code: str,
    klt: str = "101",
    limit: int = 15,
) -> pd.DataFrame:
    """获取单只股票K线

    用途：240周线偏离度、历史回测

    Args:
        code: 股票代码
        klt: K线类型（101=日K, 102=周K）
        limit: K线条数
    """
    if USE_MOCK:
        return pd.DataFrame()

    try:
        from src.data.eastmoney_api import fetch_kline
        return fetch_kline(code, klt=klt, limit=limit)
    except Exception as e:
        print(f"K线获取失败({code}): {e}")
        return pd.DataFrame()


def fetch_stock_list() -> pd.DataFrame:
    """获取全市场股票列表（代码+名称）"""
    if USE_MOCK:
        return pd.DataFrame({"code": ["600519"], "name": ["贵州茅台"]})

    try:
        from src.data.eastmoney_api import fetch_a_share_list
        df = fetch_a_share_list()
        if not df.empty:
            return df[["code", "name"]]
    except Exception:
        pass

    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        return df.rename(columns={"code": "code", "name": "name"})
    except Exception:
        return pd.DataFrame(columns=["code", "name"])


# === 涨停缓存 ===

def _load_limit_up_cache() -> dict[str, pd.DataFrame]:
    """从本地文件加载历史涨停缓存"""
    import json
    from src.config import DATA_DIR

    cache_file = DATA_DIR / "limit_up_cache.json"
    if not cache_file.exists():
        return {}

    try:
        data = json.loads(cache_file.read_text())
        result = {}
        for date_str, records in data.items():
            result[date_str] = pd.DataFrame(records)
        return result
    except Exception:
        return {}


def _save_limit_up_cache(date_str: str, df: pd.DataFrame):
    """保存涨停数据到本地缓存"""
    import json
    from src.config import DATA_DIR

    cache_file = DATA_DIR / "limit_up_cache.json"

    # 加载已有缓存
    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except Exception:
            pass

    # 只保留最近10天
    cache[date_str] = df.to_dict("records")
    sorted_dates = sorted(cache.keys(), reverse=True)[:10]
    cache = {d: cache[d] for d in sorted_dates}

    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2))

"""新浪全市场实时 spot 接口

作为东方财富 push2 被 WAF 限流时的兜底数据源。
依赖 akshare `stock_zh_a_spot()`，该函数内部调用新浪财经公共接口。

注意：新浪接口字段精简，**不含** volume_ratio / market_cap / turnover / pe，
screener 在使用时需对缺失字段做 "有则过滤，无则放行" 降级。
"""
import time
import pandas as pd


def fetch_a_share_list_sina() -> pd.DataFrame:
    """从新浪（via akshare）获取全市场A股实时行情

    Returns:
        DataFrame: code, name, close, change_pct, pre_close, open, high, low,
                   volume, amount
        缺失字段（统一填 0）: volume_ratio, market_cap, turnover, pe, industry
    """
    try:
        import akshare as ak
    except ImportError:
        print("akshare 未安装，无法使用新浪兜底")
        return pd.DataFrame()

    try:
        df = ak.stock_zh_a_spot()
    except Exception as e:
        print(f"新浪 stock_zh_a_spot 失败: {e}")
        return pd.DataFrame()

    if df.empty:
        return df

    # 字段映射
    col_map = {
        "代码": "_raw_code",
        "名称": "name",
        "最新价": "close",
        "涨跌幅": "change_pct",
        "涨跌额": "change_amount",
        "昨收": "pre_close",
        "今开": "open",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    df = df.rename(columns=col_map)

    # 代码格式 sh600519/sz000001/bj920000 → 600519
    df["code"] = df["_raw_code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)

    # 排除北交所（bj 开头），我们的策略只做 A 股主板/创/科
    df = df[~df["_raw_code"].astype(str).str.startswith("bj")].copy()

    # 数值列转 float
    num_cols = ["close", "change_pct", "change_amount", "pre_close", "open",
                "high", "low", "volume", "amount"]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # 缺失字段统一补 0（screener 要做 soft-check）
    for col in ["volume_ratio", "market_cap", "turnover", "pe", "amplitude"]:
        df[col] = 0.0
    df["industry"] = ""

    keep = ["code", "name", "close", "change_pct", "change_amount",
            "pre_close", "open", "high", "low", "volume", "amount",
            "volume_ratio", "market_cap", "turnover", "pe", "amplitude", "industry"]
    df = df[keep].reset_index(drop=True)

    print(f"新浪获取到 {len(df)} 只A股行情")
    return df


def fetch_limit_up_stocks_sina(
    df: pd.DataFrame | None = None, threshold: float = 9.8
) -> pd.DataFrame:
    """从新浪 spot 筛选今日涨停股（change_pct >= threshold）

    Args:
        df: 可选，复用已拉取的 spot DataFrame，避免二次请求
    """
    if df is None:
        df = fetch_a_share_list_sina()
    if df.empty:
        return df

    # 主板 >=9.8%，创业板/科创板 >=19.5%
    is_gem = df["code"].astype(str).str.startswith(("300", "301", "688"))
    limit_up = df[
        (is_gem & (df["change_pct"] >= 19.5))
        | (~is_gem & (df["change_pct"] >= threshold))
    ].copy()

    if not limit_up.empty:
        print(f"新浪涨停股: {len(limit_up)} 只")

    return limit_up[["code", "name", "change_pct"]].reset_index(drop=True)


def fetch_limit_up_history_sina(
    days: int = 5, scan_change_pct_min: float = 2.0
) -> dict[str, pd.DataFrame]:
    """通过新浪K线回溯最近 N 天的涨停池

    策略：
    1. 对今日涨幅 >= scan_change_pct_min% 的股票批量拉 K 线
    2. 额外纳入涨停缓存中近期出现过的股票（防止遗漏连板断裂的标的）
    统计每一天 change_pct 达到该股票涨停线的股票，组成历史涨停池。

    返回格式与 fetch_limit_up_stocks 一致，key=YYYYMMDD
    """
    from src.data.sina_kline_api import fetch_kline

    spot = fetch_a_share_list_sina()
    if spot.empty:
        return {}

    # 选候选：今日涨幅 >= 2%（降低门槛，避免遗漏昨日涨停但今天竞价低开的标的）
    strong = spot[spot["change_pct"] >= scan_change_pct_min][["code", "name"]].copy()

    # 额外纳入涨停缓存中近期出现过的所有股票（确保连板不断裂）
    try:
        import json
        from src.config import DATA_DIR
        cache_file = DATA_DIR / "limit_up_cache.json"
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
            cached_codes = set()
            for records in cache.values():
                for r in records:
                    cached_codes.add(str(r.get("code", "")))
            # 从 spot 中找到这些股票，补入候选
            cached_in_spot = spot[spot["code"].astype(str).isin(cached_codes)][["code", "name"]]
            strong = pd.concat([strong, cached_in_spot]).drop_duplicates(subset="code")
    except Exception:
        pass

    if strong.empty:
        return {}

    name_map = dict(zip(strong["code"].astype(str), strong["name"]))
    codes = strong["code"].astype(str).tolist()
    print(f"回溯近{days}日涨停历史，扫描 {len(codes)} 只候选（涨幅>{scan_change_pct_min}% + 缓存历史涨停股）...")

    # 按日期聚合涨停股
    daily_lu: dict[str, list[dict]] = {}

    for i, code in enumerate(codes):
        try:
            kline = fetch_kline(code, datalen=days + 2)
        except Exception:
            continue

        if kline is None or kline.empty or len(kline) < 2:
            continue

        # 精确判定涨停：close >= round(prev * rate, 2) - 0.005
        # 对齐通达信 ZTPRICE(REF(C,1), 0.1) 四舍五入到分的判定
        is_gem = code.startswith(("300", "301", "688"))
        rate = 1.20 if is_gem else 1.10

        closes = kline["close"].astype(float).tolist()
        dates = kline["date"].astype(str).tolist()
        for j in range(1, len(closes)):
            prev = closes[j - 1]
            curr = closes[j]
            if prev <= 0:
                continue
            limit_price = round(prev * rate, 2)
            if curr >= limit_price - 0.005:
                date_str = dates[j].replace("-", "")[:8]
                if len(date_str) == 8:
                    daily_lu.setdefault(date_str, []).append(
                        {"code": code, "name": name_map.get(code, "")}
                    )

        # 节流，避免反爬
        if (i + 1) % 20 == 0:
            time.sleep(0.2)

    # 转成 {date: DataFrame}
    result = {}
    for date_str, records in daily_lu.items():
        if records:
            result[date_str] = pd.DataFrame(records)

    if result:
        print(
            f"回溯完成: 构建了 {len(result)} 天的涨停池, "
            + ", ".join(f"{d}={len(df)}" for d, df in sorted(result.items(), reverse=True))
        )
    return result

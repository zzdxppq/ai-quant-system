"""新浪财经K线接口

数据源：http://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData
参考：go-stock/backend/data/stock_data_api.go

比东方财富K线接口更稳定，不容易被反爬
"""
import time
import json
from typing import Optional

import httpx
import pandas as pd

SINA_KLINE_URL = "http://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

# scale参数：60=日K, 240=周K, 1680=月K
SCALE_DAILY = "240"    # 新浪日K用240
SCALE_WEEKLY = "1200"  # 周K
SCALE_MONTHLY = "7200" # 月K


def fetch_kline(
    code: str,
    scale: str = SCALE_DAILY,
    datalen: int = 15,
) -> pd.DataFrame:
    """获取K线数据

    Args:
        code: 股票代码（纯数字 000001）
        scale: K线级别
        datalen: 数据条数

    Returns:
        DataFrame: day, open, close, high, low, volume
    """
    symbol = _to_sina_symbol(code)

    params = {
        "symbol": symbol,
        "scale": scale,
        "ma": "no",
        "datalen": str(datalen),
    }

    try:
        with httpx.Client(timeout=10, headers=HEADERS) as client:
            resp = client.get(SINA_KLINE_URL, params=params)
            data = resp.json()

        if not data or not isinstance(data, list):
            return pd.DataFrame()

        rows = []
        for item in data:
            rows.append({
                "date": item.get("day", ""),
                "open": float(item.get("open", 0)),
                "close": float(item.get("close", 0)),
                "high": float(item.get("high", 0)),
                "low": float(item.get("low", 0)),
                "volume": float(item.get("volume", 0)),
            })

        return pd.DataFrame(rows)

    except Exception as e:
        return pd.DataFrame()


def fetch_kline_batch(
    codes: list[str],
    scale: str = SCALE_DAILY,
    datalen: int = 15,
    delay: float = 0.05,
) -> dict[str, pd.DataFrame]:
    """批量获取K线（带间隔避免反爬）"""
    result = {}
    for i, code in enumerate(codes):
        df = fetch_kline(code, scale, datalen)
        if not df.empty:
            result[code] = df
        if delay > 0 and i < len(codes) - 1:
            time.sleep(delay)
    return result


NEW_STOCK_MIN_TRADING_DAYS = 60  # 新股过滤阈值：上市不足60个交易日直接剔除


def calc_10d_gain(
    codes: list[str],
    names: dict[str, str] = None,
    realtime_prices: dict[str, float] = None,
) -> pd.DataFrame:
    """用新浪K线计算10日涨幅

    Args:
        codes: 股票代码列表
        names: {code: name} 映射
        realtime_prices: {code: 最新价}，盘中/收盘时用实时价覆盖K线末根，
                         避免K线当日数据延迟导致涨幅不准

    Returns:
        DataFrame: code, name, gain_10d, close, is_main_board
    """
    if names is None:
        names = {}
    if realtime_prices is None:
        realtime_prices = {}

    results = []
    for i, code in enumerate(codes):
        # 请求60根日K（兼容 240 周线偏离度等其他下游），但本函数只要求 ≥2 根
        df = fetch_kline(code, SCALE_DAILY, datalen=NEW_STOCK_MIN_TRADING_DAYS)
        if df.empty or len(df) < 2:
            # 至少 2 根 K 线（=昨日+今日），少于 2 根视为当天发行的纯新股，剔除
            continue

        # 最新价：优先用实时行情（准确），K线末根兜底
        close_now = realtime_prices.get(code, df.iloc[-1]["close"])

        # 10日涨幅基准：对齐通达信 REF(C,10) = 从今天往前第10根K线收盘价
        from src.config import now_cn
        today_str = now_cn().strftime("%Y-%m-%d")
        last_kline_date = str(df.iloc[-1]["date"])[:10]

        if last_kline_date == today_str:
            # K线含今天 → REF(C,10) = iloc[-11]
            idx = max(0, len(df) - 11)
        else:
            # K线不含今天（延迟）→ 今天视为虚拟末根
            # REF(C,10) 从今天数 = K线的 iloc[-10]
            idx = max(0, len(df) - 10)

        close_10d = df.iloc[idx]["close"]

        if close_10d <= 0:
            continue

        gain_10d = (close_now / close_10d - 1) * 100

        results.append({
            "code": code,
            "name": names.get(code, ""),
            "gain_10d": round(gain_10d, 2),
            "close": close_now,
            "is_main_board": _is_main_board(code),
        })

        # 每10个请求暂停一下
        if (i + 1) % 10 == 0:
            time.sleep(0.1)

    if not results:
        return pd.DataFrame()

    return pd.DataFrame(results).sort_values("gain_10d", ascending=False).reset_index(drop=True)


def _to_sina_symbol(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("50", "51", "60", "68", "90", "110", "113", "132", "204")):
        return f"sh{code}"
    return f"sz{code}"


def _is_main_board(code: str) -> bool:
    code = str(code)
    return not code.startswith(("300", "301", "688", "8", "4"))

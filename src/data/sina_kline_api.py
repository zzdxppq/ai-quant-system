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


# 大盘交易日历缓存：用 600519 茅台（永不停牌的高流动性蓝筹）K线推导，每日刷一次
_CALENDAR_CACHE: dict = {"dates": [], "fetched_for": ""}


def _get_trading_calendar() -> list[str]:
    """返回近期交易日列表（升序），日级 cache。

    用于停牌股票的"近10个交易日"基准日定位 — 自身 K线 bar 数会忽略停牌期，
    导致基准日错位（典型表现：同花顺与本系统口径不一致）。
    """
    from src.config import now_cn
    today_str = now_cn().strftime("%Y-%m-%d")
    if _CALENDAR_CACHE["fetched_for"] == today_str and _CALENDAR_CACHE["dates"]:
        return _CALENDAR_CACHE["dates"]

    try:
        df = fetch_kline("600519", SCALE_DAILY, datalen=60)
        if df is None or df.empty:
            return _CALENDAR_CACHE["dates"]  # 拉失败用旧 cache
        dates = sorted({str(d)[:10] for d in df["date"].tolist()})
        if dates:
            _CALENDAR_CACHE["dates"] = dates
            _CALENDAR_CACHE["fetched_for"] = today_str
        return dates
    except Exception:
        return _CALENDAR_CACHE["dates"]


def _find_close_at_or_before(df: pd.DataFrame, target_date: str) -> float | None:
    """在 K线中找 date <= target_date 的最近一根 close（处理停牌期）"""
    for i in range(len(df) - 1, -1, -1):
        try:
            d = str(df.iloc[i]["date"])[:10]
            if d <= target_date:
                v = float(df.iloc[i]["close"])
                return v if v > 0 else None
        except (KeyError, ValueError):
            continue
    return None


def calc_10d_gain_from_kline(
    df: pd.DataFrame,
    realtime_close: float = 0.0,
    today_str: str | None = None,
) -> float | None:
    """从日K + 实时价计算"近10日涨幅"，对齐同花顺。

    核心：用【大盘交易日历】定位 D-10（10个交易日前），而不是该股自身的 K线 bar 数。
    若该股 D-10 当天停牌没 close，往前找最近一根 K线 close 作为基准。

    场景区分:
      1. K线已含今日 (盘后)：close_now=realtime|末根, D0=today
      2. 实时价显著偏离末根 (盘中)：close_now=realtime, D0=today (虚拟末根)
      3. 实时价≈末根 (盘前/非交易日)：close_now=末根, D0=K线末根日

    Args:
        df: 日K DataFrame，最少 2 根
        realtime_close: spot.close
        today_str: "YYYY-MM-DD"

    Returns:
        gain_10d (%)；历史不足或基准无效返回 None
    """
    if df is None or df.empty or len(df) < 2:
        return None
    try:
        last_close = float(df.iloc[-1]["close"])
    except (KeyError, IndexError, ValueError):
        return None
    last_kline_date = str(df.iloc[-1]["date"])[:10]
    if today_str is None:
        from src.config import now_cn
        today_str = now_cn().strftime("%Y-%m-%d")

    # 1. 决定 close_now 与 D0 (today_effective)
    if last_kline_date == today_str:
        close_now = realtime_close if realtime_close > 0 else last_close
        today_effective = today_str
    elif (
        realtime_close > 0 and last_close > 0
        and abs(realtime_close - last_close) / last_close > 0.005
    ):
        close_now = realtime_close
        today_effective = today_str
    else:
        close_now = last_close
        today_effective = last_kline_date

    # 2. 用大盘交易日历定位 D-10
    calendar = _get_trading_calendar()
    target_date: str | None = None
    if calendar:
        # today_effective 在 calendar 中的位置（不在则视为虚拟末根=len）
        if today_effective in calendar:
            cal_idx = calendar.index(today_effective)
        else:
            # 找 calendar 中最后一个 <= today_effective 的位置 +1（虚拟）
            cal_idx = len(calendar)
            for i in range(len(calendar) - 1, -1, -1):
                if calendar[i] <= today_effective:
                    cal_idx = i + 1
                    break
        target_idx = cal_idx - 10
        if target_idx >= 0:
            target_date = calendar[target_idx]

    # 3. 找该股在 target_date 时的 close（停牌期间 fallback 到之前最近 close）
    close_base: float | None = None
    if target_date:
        close_base = _find_close_at_or_before(df, target_date)
    # calendar 不可用时回退到原 K线 bar 数法（idx -11）
    if close_base is None:
        try:
            base_idx = max(0, len(df) - 11)
            v = float(df.iloc[base_idx]["close"])
            close_base = v if v > 0 else None
        except (KeyError, IndexError, ValueError):
            close_base = None

    if close_base is None or close_base <= 0:
        return None
    return round((close_now / close_base - 1) * 100, 2)


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

        rt_close = float(realtime_prices.get(code, 0) or 0)
        gain_10d = calc_10d_gain_from_kline(df, realtime_close=rt_close)
        if gain_10d is None:
            continue

        # close 字段：对齐 calc 内使用的"今日 close"（盘前=K线末根，盘中=实时价）
        last_close = float(df.iloc[-1]["close"])
        last_kline_date = str(df.iloc[-1]["date"])[:10]
        from src.config import now_cn
        today_str = now_cn().strftime("%Y-%m-%d")
        if last_kline_date == today_str and rt_close > 0:
            close_now = rt_close
        elif (
            rt_close > 0 and last_close > 0
            and abs(rt_close - last_close) / last_close > 0.005
        ):
            close_now = rt_close
        else:
            close_now = last_close

        results.append({
            "code": code,
            "name": names.get(code, ""),
            "gain_10d": gain_10d,
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

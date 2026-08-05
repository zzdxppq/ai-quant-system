"""新浪财经K线接口

数据源：http://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData
参考：go-stock/backend/data/stock_data_api.go

比东方财富K线接口更稳定，不容易被反爬
"""
import logging
import time
import json
from typing import Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

TENCENT_FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_KLINE_HEADERS = {
    "Host": "web.ifzq.gtimg.cn",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

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
    *,
    skip_cache_read: bool = False,
) -> pd.DataFrame:
    """获取K线数据

    Args:
        code: 股票代码（纯数字 000001）
        scale: K线级别
        datalen: 数据条数
        skip_cache_read: True 时跳过本地 TTL 缓存，直接拉网（仍写入缓存）

    Returns:
        DataFrame: day, open, close, high, low, volume
    """
    from src.data import kline_file_cache as _kfc

    code6 = _kfc.normalize_code6(code)
    use_cache = len(code6) == 6 and code6.isdigit()

    if use_cache and not skip_cache_read:
        hit = _kfc.try_read_cache(code6, scale, datalen, allow_stale=False)
        if hit is not None and not hit.empty:
            return hit

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
            if use_cache:
                stale = _kfc.try_read_cache(code6, scale, datalen, allow_stale=True)
                if stale is not None and not stale.empty:
                    return stale
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

        df = pd.DataFrame(rows)
        if not df.empty and use_cache:
            _kfc.write_cache(code6, scale, datalen, df)
        return df

    except Exception:
        if use_cache:
            stale = _kfc.try_read_cache(code6, scale, datalen, allow_stale=True)
            if stale is not None and not stale.empty:
                return stale
        return pd.DataFrame()


def _tencent_daily_bars(code: str, limit: int) -> Optional[pd.DataFrame]:
    """腾讯 fqkline 日 K（东财断连时的降级源）。"""
    from src.data.kline_file_cache import normalize_code6
    from src.data.tencent_api import _to_tencent_code

    code6 = normalize_code6(code)
    if len(code6) != 6:
        return None
    symbol = _to_tencent_code(code6)
    n = max(60, min(2000, int(limit)))
    param = f"{symbol},day,,,{n},qfq"
    try:
        with httpx.Client(timeout=18, headers=TENCENT_KLINE_HEADERS) as client:
            resp = client.get(TENCENT_FQKLINE_URL, params={"param": param})
            root = resp.json()
    except Exception as e:
        logger.warning("腾讯日K失败(%s): %s", code6, e)
        return None
    if not isinstance(root, dict) or int(root.get("code", -1)) != 0:
        return None
    node = (root.get("data") or {}).get(symbol) or {}
    if not isinstance(node, dict):
        return None
    raw = node.get("qfqday") or node.get("day") or []
    rows = []
    for line in raw:
        if isinstance(line, (list, tuple)):
            parts = list(line)
        else:
            parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            rows.append({
                "date": str(parts[0]).strip(),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        except (ValueError, TypeError):
            continue
    if not rows:
        return None
    return pd.DataFrame(rows)


def _em_daily_bars(code: str, limit: int, *, attempts: int = 4) -> Optional[pd.DataFrame]:
    """东财日 K（节流+指数退避在 fetch_kline）；仍失败则腾讯 fqkline。"""
    from src.data.eastmoney_api import FQT_QFQ, KLT_DAILY, fetch_kline as em_fetch_kline

    lim = max(120, int(limit))
    _ = attempts  # 保留参数兼容；重试次数由 EM_KLINE_MAX_RETRIES 控制
    try:
        em = em_fetch_kline(code, KLT_DAILY, FQT_QFQ, limit=lim)
        if em is not None and not em.empty:
            return em[["date", "open", "close", "high", "low", "volume"]].copy()
    except Exception:
        pass
    return _tencent_daily_bars(code, lim)


def _normalize_daily_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
    d = d.dropna(subset=["date"])
    for col in ("open", "high", "low", "close", "volume"):
        if col not in d.columns:
            return pd.DataFrame()
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)
    d = d[d["close"] > 0]
    return d


def _merge_daily_kline_candidates(
    candidates: list[pd.DataFrame],
    *,
    priorities: list[int] | None = None,
) -> pd.DataFrame:
    """按交易日合并多源日 K；同日复用优先级更高且成交量更大的行（东财优先于新浪）。"""
    parts: list[pd.DataFrame] = []
    pris: list[int] = []
    for i, raw in enumerate(candidates):
        d = _normalize_daily_ohlcv(raw)
        if d.empty:
            continue
        pri = priorities[i] if priorities is not None and i < len(priorities) else i
        d = d.copy()
        d["_pri"] = int(pri)
        parts.append(d)
    if not parts:
        return pd.DataFrame()
    all_d = pd.concat(parts, ignore_index=True)
    all_d = all_d.sort_values(["date", "_pri", "volume"])
    merged = all_d.drop_duplicates(subset=["date"], keep="last")
    merged = merged.drop(columns=["_pri"], errors="ignore")
    merged["date"] = merged["date"].dt.strftime("%Y-%m-%d")
    return merged.sort_values("date").reset_index(drop=True)[
        ["date", "open", "high", "low", "close", "volume"]
    ]


def _daily_max_date(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    if df is None or df.empty:
        return None
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    return s.max() if len(s) else None


def _expected_last_daily_bar_date() -> pd.Timestamp:
    """最近一根已收盘日 K 的交易日（盘中 15:00 前仍视为上一交易日）。"""
    from datetime import timedelta

    from src.config import is_trading_day, now_cn

    n = now_cn()
    for i in range(12):
        cur = n - timedelta(days=i)
        if not is_trading_day(cur):
            continue
        if i == 0:
            cutoff = cur.replace(hour=15, minute=0, second=0, microsecond=0)
            if n < cutoff:
                continue
        return pd.Timestamp(cur.date())
    return pd.Timestamp((n - timedelta(days=1)).date())


def _is_daily_kline_stale(df: pd.DataFrame) -> bool:
    mx = _daily_max_date(df)
    if mx is None:
        return True
    exp = _expected_last_daily_bar_date()
    return mx.date() < exp.date()


def prev_close_from_daily_df(df: pd.DataFrame | None) -> float:
    """从日 K 推导「昨收」：取最近一根已收盘交易日的收盘价。

    盘中 15:00 前 _expected_last_daily_bar_date 为上一交易日，避免误用 iloc[-2]
    在库内尚无当日 K 时把「大前天收盘」当成昨收。
    """
    if df is None or df.empty:
        return 0.0
    d = _normalize_daily_ohlcv(df)
    if d.empty:
        return 0.0
    d = d.copy()
    d["_dt"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
    d = d.dropna(subset=["_dt"]).sort_values("_dt")
    if d.empty:
        return 0.0
    exp = _expected_last_daily_bar_date().normalize()
    sub = d[d["_dt"] <= exp]
    if sub.empty:
        sub = d
    pc = float(sub.iloc[-1]["close"])
    return pc if pc > 0 else 0.0


def resolve_prev_close(code: str) -> float:
    """昨收：优先新浪实时字段，其次多源日 K（与 fetch_daily_kline_robust 同源）。"""
    from src.data.kline_file_cache import normalize_code6

    c6 = normalize_code6(code)
    if len(c6) != 6 or not c6.isdigit():
        return 0.0

    try:
        from src.data.sina_api import fetch_realtime_batch

        rt = fetch_realtime_batch([c6])
        if rt is not None and not rt.empty:
            pre = float(rt.iloc[0].get("pre_close", 0) or 0)
            if pre > 0:
                return pre
    except Exception:
        pass

    try:
        df = fetch_daily_kline_robust(c6, min_bars=3, datalen=60)
        pc = prev_close_from_daily_df(df)
        if pc > 0:
            return pc
    except Exception:
        pass

    from src.data import kline_file_cache as _kfc

    for datalen in (60, 30):
        try:
            stale = _kfc.try_read_cache(c6, SCALE_DAILY, datalen, allow_stale=True)
            pc = prev_close_from_daily_df(stale)
            if pc > 0:
                return pc
        except Exception:
            pass
    return 0.0


def fetch_daily_kline_from_cache_only(
    code: str,
    *,
    min_bars: int = 35,
    datalen: int = 500,
) -> pd.DataFrame:
    """仅读 DuckDB 日 K 缓存（含过期），不访问东财/新浪。供 K 线弹窗首屏秒开。"""
    from src.data.kline_file_cache import normalize_code6, try_read_cache

    c6 = normalize_code6(code)
    if len(c6) != 6 or not c6.isdigit():
        return pd.DataFrame()

    best: pd.DataFrame | None = None
    for dl in (int(datalen), 500, 800, 120, 60):
        df = try_read_cache(c6, SCALE_DAILY, dl, allow_stale=True)
        if df is None or df.empty:
            continue
        if best is None or len(df) > len(best):
            best = df.copy()
    if best is None or len(best) < min_bars:
        return pd.DataFrame()
    return best.reset_index(drop=True)


def fetch_weekly_kline_from_cache_only(code: str, datalen: int = 500) -> pd.DataFrame:
    """周 K 首屏：库内周 K → 否则仅用库内日 K 聚合，不拉东财。"""
    from src.data.kline_file_cache import normalize_code6, try_read_cache

    code6 = normalize_code6(code)
    if len(code6) != 6 or not code6.isdigit():
        return pd.DataFrame()

    dfw = try_read_cache(code6, SCALE_WEEKLY, int(datalen), allow_stale=True)
    if dfw is not None and not dfw.empty and len(dfw) >= 20:
        return dfw.reset_index(drop=True)

    daily = fetch_daily_kline_from_cache_only(code6, min_bars=35, datalen=max(800, int(datalen) * 8))
    return _aggregate_daily_to_weekly(daily)


def fetch_daily_kline_robust(
    code: str,
    *,
    min_bars: int = 35,
    datalen: int = 500,
    skip_cache_read: bool = False,
) -> pd.DataFrame:
    """日 K：新浪 + 东财按日合并；末根过旧时强制跳过本地缓存并加重试东财。

    用户打开日 K 弹窗后的「后台修正」走本函数（refresh=1）；首屏勿直接调用。
    """
    from src.data.kline_file_cache import normalize_code6, write_cache

    c6 = normalize_code6(code)
    frames: list[pd.DataFrame] = []
    priorities: list[int] = []

    df1 = fetch_kline(code, SCALE_DAILY, datalen=datalen, skip_cache_read=skip_cache_read)
    if df1 is not None and not df1.empty:
        frames.append(df1)
        priorities.append(1)

    em_df = _em_daily_bars(code, max(datalen, min_bars + 20))
    if em_df is not None and not em_df.empty:
        frames.append(em_df)
        priorities.append(3)

    df_big = fetch_kline(code, SCALE_DAILY, datalen=max(800, int(datalen)), skip_cache_read=False)
    if df_big is not None and not df_big.empty:
        frames.append(df_big)
        priorities.append(2)

    merged = _merge_daily_kline_candidates(frames, priorities=priorities)
    if merged.empty:
        return pd.DataFrame()

    if _is_daily_kline_stale(merged) or skip_cache_read:
        refresh_frames = [merged]
        refresh_pri = [0]
        df_fresh = fetch_kline(code, SCALE_DAILY, datalen=datalen, skip_cache_read=True)
        if df_fresh is not None and not df_fresh.empty:
            refresh_frames.append(df_fresh)
            refresh_pri.append(1)
        em2 = _em_daily_bars(code, max(800, int(datalen)), attempts=5)
        if em2 is not None and not em2.empty:
            refresh_frames.append(em2)
            refresh_pri.append(3)
        merged = _merge_daily_kline_candidates(refresh_frames, priorities=refresh_pri)

    if merged is None or merged.empty:
        return pd.DataFrame()

    if len(merged) >= min_bars and len(c6) == 6:
        try:
            write_cache(c6, SCALE_DAILY, int(datalen), merged)
        except Exception:
            pass
    return merged


def _aggregate_daily_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """将日 K OHLCV 按周（周五收盘）聚合成周 K，列与 fetch_kline 一致。"""
    if daily_df is None or daily_df.empty:
        return pd.DataFrame()
    d = daily_df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"])
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values("date").set_index("date")
    for col in ("open", "high", "low", "close", "volume"):
        if col not in d.columns:
            return pd.DataFrame()
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)
    try:
        w = d.resample("W-FRI").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
    except Exception:
        return pd.DataFrame()
    w = w.dropna(subset=["close"], how="any")
    w = w[w["close"] > 0]
    if w.empty:
        return pd.DataFrame()
    out = w.reset_index()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out[["date", "open", "high", "low", "close", "volume"]]


def fetch_weekly_kline_unified(code: str, datalen: int = 500) -> pd.DataFrame:
    """周 K：新浪 scale=1200 → 东方财富周 K → 日 K 聚合成周 K。

    新浪偶发返回空列表（地区/网关/个别代码未挂周 K），故做链式降级；聚合路径不写入周 K 磁盘缓存，
    避免与原生周 K 混用长期缓存。
    """
    from src.data import kline_file_cache as _kfc

    code6 = _kfc.normalize_code6(code)
    if len(code6) != 6 or not code6.isdigit():
        return pd.DataFrame()

    dfw = fetch_kline(code6, SCALE_WEEKLY, datalen)
    if dfw is not None and not dfw.empty:
        return dfw

    try:
        from src.data.eastmoney_api import FQT_QFQ, KLT_WEEKLY
        from src.data.eastmoney_api import fetch_kline as em_fetch_kline

        lim = max(300, min(1200, int(datalen) * 2))
        em = em_fetch_kline(code6, klt=KLT_WEEKLY, fqt=FQT_QFQ, limit=lim)
        if em is not None and not em.empty and "close" in em.columns:
            out = em.copy()
            out["date"] = out["date"].astype(str).str.strip().str[:10]
            if "volume" not in out.columns:
                out["volume"] = 0.0
            cols = ["date", "open", "high", "low", "close", "volume"]
            if all(c in out.columns for c in cols):
                return out[cols].dropna(subset=["close"])
    except Exception:
        pass

    daily = fetch_kline(code6, SCALE_DAILY, datalen=min(3000, max(800, int(datalen) * 8)))
    return _aggregate_daily_to_weekly(daily)


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
    """新浪 K 线 symbol：沪 sh / 深 sz / 北交所 bj（与腾讯 _to_tencent_code 对齐）。"""
    code = str(code).strip()
    d = "".join(ch for ch in code if ch.isdigit())
    if len(d) >= 6:
        d = d[-6:]
    else:
        d = code
    if len(d) != 6 or not d.isdigit():
        return f"sz{code}"
    # 北交所：4xx / 8xx / 92x（原先误走 sz 会导致周 K 拉空 → 前端「接口异常」）
    if d.startswith(("4", "8", "92")):
        return f"bj{d}"
    if d.startswith(("60", "68", "90", "110", "113", "132", "204")) or d.startswith("5"):
        return f"sh{d}"
    return f"sz{d}"


def _is_main_board(code: str) -> bool:
    code = str(code)
    return not code.startswith(("300", "301", "688", "8", "4"))

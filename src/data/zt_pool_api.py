"""东财涨停板池接口

数据源: push2ex.eastmoney.com/getTopicZTPool

返回字段（按东财文档）:
    c   : 代码
    n   : 名称
    fbt : 首次封板时间 (HHMMSS, int)
    lbt : 最后封板时间 (HHMMSS, int)
    lbc : 连板次数（首板多为 1；偶发 0 多为接口缺省，消费侧宜与首板同档处理）
    zbc : 炸板次数
"""
import time
from datetime import timedelta
from typing import Optional

import httpx

from src.config import now_cn

ZT_POOL_URL = "http://push2ex.eastmoney.com/getTopicZTPool"
ZB_POOL_URL = "http://push2ex.eastmoney.com/getTopicZBPool"  # 炸板池

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
}


def fetch_zt_pool(date: Optional[str] = None) -> dict:
    """拉取指定交易日的涨停板池

    Args:
        date: YYYYMMDD，默认今天

    Returns:
        {code: {"name": str, "lbc": int, "lbt": "HH:MM:SS"}}
        失败返回空 dict
    """
    if date is None:
        date = now_cn().strftime("%Y%m%d")

    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "500",
        "sort": "fbt:asc",
        "date": date,
        "_": str(int(time.time() * 1000)),
    }

    try:
        with httpx.Client(timeout=10, headers=HEADERS) as client:
            resp = client.get(ZT_POOL_URL, params=params)
            data = resp.json()
    except Exception as e:
        print(f"涨停板池请求失败: {e}")
        return {}

    if not data or not data.get("data"):
        return {}
    pool = data["data"].get("pool") or []
    if not pool:
        return {}

    result: dict = {}
    for item in pool:
        code = str(item.get("c", "")).strip()
        if not code:
            continue
        result[code] = {
            "name": item.get("n", ""),
            "lbc": int(item.get("lbc", 0) or 0),
            "lbt": _format_hhmmss(item.get("lbt")),
            "zbc": int(item.get("zbc", 0) or 0),  # 炸板次数
        }

    print(f"涨停板池: {len(result)} 只 (date={date})")
    return result


def fetch_zb_pool(date: Optional[str] = None) -> dict:
    """拉取指定交易日的炸板池（盘中曾涨停但收盘未封住）

    Returns:
        {code: {"name": str, "zbc": int, "industry": str}}
    """
    if date is None:
        date = now_cn().strftime("%Y%m%d")

    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "500",
        "sort": "fbt:asc",
        "date": date,
        "_": str(int(time.time() * 1000)),
    }

    try:
        with httpx.Client(timeout=10, headers=HEADERS) as client:
            resp = client.get(ZB_POOL_URL, params=params)
            data = resp.json()
    except Exception as e:
        print(f"炸板池请求失败: {e}")
        return {}

    if not data or not data.get("data"):
        return {}
    pool = data["data"].get("pool") or []
    if not pool:
        return {}

    result: dict = {}
    for item in pool:
        code = str(item.get("c", "")).strip()
        if not code:
            continue
        result[code] = {
            "name": item.get("n", ""),
            "zbc": int(item.get("zbc", 0) or 0),
            "industry": item.get("hybk", ""),
        }
    print(f"炸板池: {len(result)} 只 (date={date})")
    return result


def prev_trading_date_ymd() -> str:
    """上一交易日 YYYYMMDD（仅跳过周末，与 dashboard 接力池口径一致）。"""
    d = now_cn().date()
    for _ in range(10):
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y%m%d")
    return d.strftime("%Y%m%d")


def fetch_dt_pool(date: Optional[str] = None) -> dict:
    """拉取指定交易日收盘跌停池（akshare 东财 dtgc）。

    用于「昨日跌停今日竞价反馈」：当库内无昨日竞价跌停代码列表时兜底。
    Returns:
        {code6: {"name": str, "continuous_limit_down": int}}
    """
    if date is None:
        date = prev_trading_date_ymd()
    date = str(date).replace("-", "")[:8]
    try:
        import akshare as ak

        df = ak.stock_zt_pool_dtgc_em(date=date)
    except Exception as e:
        print(f"跌停池(akshare) 请求失败 date={date}: {e}")
        return {}
    if df is None or getattr(df, "empty", True):
        print(f"跌停池: 0 只 (date={date})")
        return {}
    result: dict = {}
    code_col = "代码" if "代码" in df.columns else "code"
    name_col = "名称" if "名称" in df.columns else "name"
    cont_col = "连续跌停" if "连续跌停" in df.columns else None
    for _, row in df.iterrows():
        raw = str(row.get(code_col, "")).strip()
        digits = "".join(c for c in raw if c.isdigit())
        if len(digits) < 6:
            continue
        code6 = digits[-6:].zfill(6)
        cont = 1
        if cont_col is not None:
            try:
                cont = int(row.get(cont_col) or 1)
            except (TypeError, ValueError):
                cont = 1
        result[code6] = {
            "name": str(row.get(name_col, "") or ""),
            "continuous_limit_down": cont,
        }
    print(f"跌停池(收盘): {len(result)} 只 (date={date})")
    return result


def _format_hhmmss(ts) -> str:
    """142311 → '14:23:11'；无效值返回空串"""
    try:
        n = int(ts)
    except (ValueError, TypeError):
        return ""
    if n <= 0:
        return ""
    s = str(n).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def fetch_zt_pool_with_retry(date: Optional[str] = None, max_retries: int = 3, retry_delay: float = 2.0) -> dict:
    """带重试的涨停板池拉取（网络抖动时自动重试，最长等待约6秒）。

    进程内短缓存（同 date 60s）避免 9:27 链路重复打东财。
    """
    import time

    cache_key = str(date or "") or "_today_"
    now_m = time.monotonic()
    cache = getattr(fetch_zt_pool_with_retry, "_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        fetch_zt_pool_with_retry._cache = cache
    hit = cache.get(cache_key)
    if hit and (now_m - hit[0]) < 60:
        return dict(hit[1])

    last_error = None
    for attempt in range(max_retries):
        try:
            result = fetch_zt_pool(date)
            if result:
                cache[cache_key] = (now_m, dict(result))
                return result
        except Exception as e:
            last_error = e

        if attempt < max_retries - 1:
            time.sleep(retry_delay)

    if last_error is not None:
        print(f"[zt_pool] 重试{max_retries}次均失败（{last_error}），返回空 dict")
    else:
        print(f"[zt_pool] 重试{max_retries}次均失败（响应为空），返回空 dict")
    return {}

"""东财涨停板池接口

数据源: push2ex.eastmoney.com/getTopicZTPool

返回字段（按东财文档）:
    c   : 代码
    n   : 名称
    fbt : 首次封板时间 (HHMMSS, int)
    lbt : 最后封板时间 (HHMMSS, int)
    lbc : 连板次数
    zbc : 炸板次数
"""
import time
from datetime import datetime
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

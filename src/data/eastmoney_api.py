"""东方财富数据接口

数据源：
  - 实时排行：https://push2.eastmoney.com/api/qt/clist/get
  - 历史K线：https://push2his.eastmoney.com/api/qt/stock/kline/get
  - 涨停池：https://push2.eastmoney.com/api/qt/clist/get (涨停板筛选)

参考：go-stock/backend/data/eastmoney_kline_api.go
"""
import re
import time
import json
from typing import Optional

import httpx
import pandas as pd

# === 实时排行接口 ===
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"

# === 历史K线接口 ===
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
}

KLINE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# A股市场过滤条件
FS_A_SHARE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

# K线类型
KLT_DAILY = "101"
KLT_WEEKLY = "102"
KLT_MONTHLY = "103"
KLT_MIN1 = "1"
KLT_MIN5 = "5"
KLT_MIN15 = "15"
KLT_MIN30 = "30"
KLT_MIN60 = "60"

# 复权类型
FQT_NONE = "0"
FQT_QFQ = "1"   # 前复权
FQT_HFQ = "2"   # 后复权


# 东方财富 clist 接口单页硬性封顶 100 条
# 参考 go-stock 实现：单次只拉一页 top 50~100，不做爆发式分页
_CLIST_PAGE_CAP = 100
# 分页兜底上限：即使 caller 要更多，也最多分 _MAX_PAGES 页，杜绝 50 页爆发
_MAX_PAGES = 5
# 页间节流（秒），go-stock 本身不 sleep 但 go-stock 也不分页
_PAGE_SLEEP = 0.5


def fetch_a_share_list(
    page: int = 1,
    page_size: int = 100,
    sort_field: str = "f3",
    sort_order: int = 1,
) -> pd.DataFrame:
    """获取A股市场实时行情列表（按排序字段降序）

    **参考 go-stock `getDCStockInfo` 实现风格**：
    - 默认单页 top100（与 go-stock `pz=50` 思路一致）
    - 若 caller 请求 >100，最多分 _MAX_PAGES(=5) 页，页间 _PAGE_SLEEP 秒节流
    - 严禁 50 页爆发式请求，避免触发 push2.eastmoney.com WAF 限流

    Args:
        page: 起始页码（从1开始）
        page_size: 期望返回的总条数。默认 100，硬性上限 _MAX_PAGES*100 = 500
        sort_field: 排序字段，f3=涨跌幅
        sort_order: 1=降序（默认，涨幅最大在前），0=升序

    Returns:
        DataFrame: code, name, close, open, high, low, pre_close,
                   change_pct, volume, amount, turnover, market_cap, volume_ratio, pe
    """
    # 请求总量硬性上限，防爆发
    total_target = min(page_size, _CLIST_PAGE_CAP * _MAX_PAGES)
    all_rows: list[dict] = []
    current_page = page
    remaining = total_target

    def _fetch_one_page(client: httpx.Client, pn: int, pz: int) -> Optional[list]:
        """拉单页。发生协议层断连时重试 2 次，每次指数退避"""
        params = {
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "cb": "",
            "fs": FS_A_SHARE,
            "fields": "f12,f13,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23,f100",
            "fid": sort_field,
            "pn": str(pn),
            "pz": str(pz),
            "po": str(sort_order),
            "dect": "1",
            # 参考 go-stock：wbp2u 参数 + 时间戳 cache-buster
            "wbp2u": "|0|0|0|web",
            "_": str(int(time.time() * 1000)),
        }
        last_err = None
        for attempt in range(3):
            try:
                resp = client.get(CLIST_URL, params=params)
                data = resp.json()
                if data.get("data") is None:
                    return None
                return data["data"].get("diff", [])
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
                last_err = e
                time.sleep(0.8 * (attempt + 1))
        print(f"  页{pn}连续3次失败: {last_err}")
        return None

    try:
        # 参考 go-stock：显式 Host + Referer，http2=False 用 HTTP/1.1
        req_headers = dict(HEADERS)
        req_headers["Host"] = "push2.eastmoney.com"
        with httpx.Client(timeout=15, headers=req_headers, http2=False) as client:
            while remaining > 0:
                items = _fetch_one_page(
                    client, current_page, min(remaining, _CLIST_PAGE_CAP)
                )
                if items is None:
                    if current_page == page:
                        print("东方财富首页即失败")
                    break
                if not items:
                    break

                for item in items:
                    all_rows.append({
                        "code": str(item.get("f12", "")),
                        "name": item.get("f14", ""),
                        "close": _safe_float(item.get("f2")),
                        "change_pct": _safe_float(item.get("f3")),
                        "change_amount": _safe_float(item.get("f4")),
                        "volume": _safe_float(item.get("f5")),
                        "amount": _safe_float(item.get("f6")),
                        "amplitude": _safe_float(item.get("f7")),
                        "turnover": _safe_float(item.get("f8")),
                        "pe": _safe_float(item.get("f9")),
                        "volume_ratio": _safe_float(item.get("f10")),
                        "high": _safe_float(item.get("f15")),
                        "low": _safe_float(item.get("f16")),
                        "open": _safe_float(item.get("f17")),
                        "pre_close": _safe_float(item.get("f18")),
                        "market_cap": _safe_float(item.get("f21")),  # 流通市值
                        "industry": item.get("f100", ""),
                    })

                # 本页返回不足 _CLIST_PAGE_CAP 表示已到末尾
                if len(items) < _CLIST_PAGE_CAP:
                    break

                remaining -= len(items)
                current_page += 1
                # 页间节流
                if remaining > 0:
                    time.sleep(_PAGE_SLEEP)

    except Exception as e:
        print(f"东方财富全市场接口失败: {e}")
        if not all_rows:
            return pd.DataFrame()

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    print(f"东方财富获取到 {len(df)} 只A股行情")
    return df


def fetch_kline(
    code: str,
    klt: str = KLT_DAILY,
    fqt: str = FQT_QFQ,
    limit: int = 15,
) -> pd.DataFrame:
    """获取历史K线数据

    Args:
        code: 股票代码（纯数字，如 000001）
        klt: K线类型（101=日K, 102=周K, 103=月K）
        fqt: 复权类型（0=不复权, 1=前复权, 2=后复权）
        limit: 获取的K线条数

    Returns:
        DataFrame: date, open, close, high, low, volume, amount,
                   amplitude, change_pct, change_amount, turnover
    """
    secid = _to_secid(code)

    fields2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

    params = {
        "secid": secid,
        "klt": klt,
        "fqt": fqt,
        "end": "20500101",
        "lmt": str(limit),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": fields2,
        "_": str(int(time.time() * 1000)),
    }

    try:
        with httpx.Client(timeout=10, headers=KLINE_HEADERS) as client:
            resp = client.get(KLINE_URL, params=params)
            data = resp.json()

        if data.get("data") is None:
            return pd.DataFrame()

        klines = data["data"].get("klines", [])
        if not klines:
            return pd.DataFrame()

        rows = []
        for kline_str in klines:
            parts = kline_str.split(",")
            if len(parts) < 11:
                continue
            rows.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
                "amplitude": float(parts[7]),
                "change_pct": float(parts[8]),
                "change_amount": float(parts[9]),
                "turnover": float(parts[10]),
            })

        return pd.DataFrame(rows)

    except Exception as e:
        print(f"东方财富K线接口失败({code}): {e}")
        return pd.DataFrame()


def fetch_kline_batch(
    codes: list[str],
    klt: str = KLT_DAILY,
    fqt: str = FQT_QFQ,
    limit: int = 15,
) -> dict[str, pd.DataFrame]:
    """批量获取K线数据

    Returns:
        {code: DataFrame}
    """
    result = {}
    for code in codes:
        df = fetch_kline(code, klt, fqt, limit)
        if not df.empty:
            result[code] = df
    return result


def calc_10d_gain_ranking(top_n: int = 100) -> pd.DataFrame:
    """计算10日涨幅排行

    方案：
    1. 获取全市场实时行情
    2. 取涨幅前 top_n*3 的股票
    3. 逐个拉10日日K线计算精确涨幅
    4. 排序返回

    Returns:
        DataFrame: code, name, gain_10d, close, is_main_board
    """
    print("正在计算10日涨幅排行...")

    # 1. 获取涨幅降序 top300，作为10日涨幅计算候选（后续只取 top_n*3）
    all_df = fetch_a_share_list(page_size=max(top_n * 3, 300), sort_field="f3", sort_order=1)
    if all_df.empty:
        return pd.DataFrame()

    # 2. 取活跃股票（近期涨幅较大的更可能在10日涨幅前列）
    # 过滤掉停牌和异常数据；保留 ST 和次新股；只剔除退市
    active = all_df[all_df["close"] > 0].copy()
    active = active[~active["name"].astype(str).str.contains("退")]
    # 取涨幅前 top_n*3 候选（新股过滤在 calc_10d_gain 内按K线长度判定）
    candidates = active.head(top_n * 3)

    # 3. 用新浪K线批量计算10日涨幅（更稳定，不易被反爬）
    codes = candidates["code"].tolist()
    names = dict(zip(candidates["code"], candidates["name"]))
    # 传入实时价格，避免K线当日数据延迟导致涨幅不准
    realtime_prices = dict(zip(
        candidates["code"].astype(str),
        candidates["close"].astype(float),
    ))

    print(f"  候选 {len(codes)} 只，拉取10日K线...")

    from src.data.sina_kline_api import calc_10d_gain
    result_df = calc_10d_gain(codes, names, realtime_prices=realtime_prices)

    if result_df.empty:
        # 新浪也失败了，尝试东方财富逐个拉（加间隔）
        print("  新浪K线失败，尝试东方财富...")
        import time
        from src.data.sina_kline_api import NEW_STOCK_MIN_TRADING_DAYS
        results = []
        for i, code in enumerate(codes[:top_n]):
            kline_df = fetch_kline(code, KLT_DAILY, FQT_QFQ, limit=NEW_STOCK_MIN_TRADING_DAYS)
            if kline_df.empty or len(kline_df) < 2:
                # 至少 2 根 K 线（昨日+今日），少于 2 根视为当天发行的纯新股
                continue
            close_now = kline_df.iloc[-1]["close"]
            idx = max(0, len(kline_df) - 11)
            close_10d = kline_df.iloc[idx]["close"]
            if close_10d > 0:
                gain = (close_now / close_10d - 1) * 100
                results.append({"code": code, "name": names.get(code, ""),
                                "gain_10d": round(gain, 2), "close": close_now,
                                "is_main_board": _is_main_board(code)})
            time.sleep(0.2)

        if not results:
            return pd.DataFrame()
        result_df = pd.DataFrame(results).sort_values("gain_10d", ascending=False).reset_index(drop=True)

    result_df["rank"] = result_df.index + 1

    print(f"  10日涨幅排行计算完成，TOP5:")
    for _, row in result_df.head(5).iterrows():
        board = "主板" if row["is_main_board"] else "创/科"
        print(f"    {row['code']} {row['name']} {row['gain_10d']}% [{board}]")

    return result_df.head(top_n)


def fetch_limit_up_stocks(date: str = None) -> pd.DataFrame:
    """获取涨停股列表

    通过东方财富的涨停板筛选获取

    Args:
        date: 日期 YYYYMMDD，默认今天

    Returns:
        DataFrame: code, name, change_pct, limit_up_type
    """
    # 使用涨跌幅排行（降序），筛选涨幅>=9.8%的（接近涨停）；A股涨停数远<300
    all_df = fetch_a_share_list(page_size=300, sort_field="f3", sort_order=1)
    if all_df.empty:
        return pd.DataFrame()

    # 筛选涨停（主板涨幅>=9.8%，创业板/科创板>=19.5%）
    limit_up = all_df[
        ((all_df["code"].str.startswith(("300", "301", "688"))) & (all_df["change_pct"] >= 19.5)) |
        (~all_df["code"].str.startswith(("300", "301", "688")) & (all_df["change_pct"] >= 9.8))
    ].copy()

    # 排除北交所
    limit_up = limit_up[~limit_up["code"].str.startswith(("8", "4"))]

    if not limit_up.empty:
        print(f"涨停股: {len(limit_up)} 只")

    return limit_up[["code", "name", "change_pct"]].reset_index(drop=True)


def _to_secid(code: str) -> str:
    """转换为东方财富secid格式

    000001 → 0.000001 (深圳)
    600519 → 1.600519 (上海)
    """
    code = str(code).strip()
    if code.startswith(("50", "51", "60", "68", "90", "110", "113", "132", "204")):
        return f"1.{code}"
    else:
        return f"0.{code}"


def _is_main_board(code: str) -> bool:
    """判断是否主板"""
    code = str(code)
    if code.startswith(("300", "301")):
        return False  # 创业板
    if code.startswith("688"):
        return False  # 科创板
    if code.startswith(("8", "4")):
        return False  # 北交所
    return True


def _safe_float(val) -> float:
    """安全转换浮点数"""
    if val is None or val == "-":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0

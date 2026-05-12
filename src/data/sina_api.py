"""新浪财经实时行情接口

数据源：http://hq.sinajs.cn
参考：go-stock/backend/data/stock_data_api.go

用途：9:25竞价后获取全市场实时快照
"""
import re
import time
from typing import Optional

import httpx
import pandas as pd

SINA_URL = "http://hq.sinajs.cn/rn={ts}&list={codes}"

HEADERS = {
    "Host": "hq.sinajs.cn",
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
}

# 新浪返回的字段顺序（A股）
FIELDS = [
    "name", "open", "pre_close", "close", "high", "low",
    "bid1_price", "ask1_price", "volume", "amount",
    "bid1_vol", "bid1_p", "bid2_vol", "bid2_p", "bid3_vol", "bid3_p",
    "bid4_vol", "bid4_p", "bid5_vol", "bid5_p",
    "ask1_vol", "ask1_p", "ask2_vol", "ask2_p", "ask3_vol", "ask3_p",
    "ask4_vol", "ask4_p", "ask5_vol", "ask5_p",
    "date", "time",
]


def fetch_realtime_batch(codes: list[str], batch_size: int = 50) -> pd.DataFrame:
    """批量获取实时行情

    Args:
        codes: 股票代码列表（纯数字，如 ['000001', '600519']）
        batch_size: 每批请求的股票数量

    Returns:
        DataFrame with columns: code, name, open, pre_close, close, high, low,
                                volume, amount, bid1_price, ask1_price, date, time
    """
    all_rows = []

    # 分批请求
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        sina_codes = [_to_sina_code(c) for c in batch]
        codes_str = ",".join(sina_codes)

        url = SINA_URL.format(ts=int(time.time() * 1000), codes=codes_str)

        try:
            with httpx.Client(timeout=10, headers=HEADERS) as client:
                resp = client.get(url)
                resp.encoding = "gbk"
                rows = _parse_response(resp.text, batch)
                all_rows.extend(rows)
        except Exception as e:
            print(f"  新浪接口请求失败(batch {i}): {e}")

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)

    # 数值转换
    numeric_cols = ["open", "pre_close", "close", "high", "low", "volume", "amount",
                    "bid1_price", "ask1_price", "bid1_volume", "ask1_volume"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def fetch_single(code: str) -> Optional[dict]:
    """获取单只股票实时行情"""
    df = fetch_realtime_batch([code])
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def _to_sina_code(code: str) -> str:
    """转换为新浪代码格式: 000001 -> sz000001, 600519 -> sh600519

    若调用方已传入 sh/sz/bj 前缀（如指数 sh000001），原样返回；
    否则按代码段推导前缀。
    """
    code = str(code).strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("50", "51", "60", "68", "90", "110", "113", "132", "204")):
        return f"sh{code}"
    else:
        return f"sz{code}"


def _parse_response(text: str, original_codes: list[str]) -> list[dict]:
    """解析新浪响应

    响应格式: var hq_str_sz000001="大秦铁路,27.55,27.25,...";
    """
    rows = []
    lines = text.strip().split("\n")

    code_idx = 0
    for line in lines:
        line = line.strip()
        if not line or '="' not in line:
            continue

        # 提取数据部分
        match = re.search(r'="(.+)"', line)
        if not match:
            code_idx += 1
            continue

        data = match.group(1)
        if not data:
            code_idx += 1
            continue

        parts = data.split(",")
        if len(parts) < 32:
            code_idx += 1
            continue

        original_code = original_codes[code_idx] if code_idx < len(original_codes) else ""

        row = {
            "code": original_code,
            "name": parts[0],
            "open": parts[1],
            "pre_close": parts[2],
            "close": parts[3],
            "high": parts[4],
            "low": parts[5],
            "bid1_price": parts[6],
            "ask1_price": parts[7],
            "volume": parts[8],       # 成交量（股）
            "amount": parts[9],       # 成交额（元）
            "bid1_volume": parts[10] if len(parts) > 10 else "0",  # 买一申报量（股）— 一字涨停封单核心字段
            "ask1_volume": parts[20] if len(parts) > 20 else "0",  # 卖一申报量（股）
            "date": parts[30] if len(parts) > 30 else "",
            "time": parts[31] if len(parts) > 31 else "",
        }
        rows.append(row)
        code_idx += 1

    return rows

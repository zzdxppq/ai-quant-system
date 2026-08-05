"""腾讯 fqkline 日 K 解析（数组 / 字符串两种形态）。"""
from unittest.mock import patch

import pandas as pd

from src.data.sina_kline_api import _tencent_daily_bars


def test_tencent_daily_bars_parses_list_rows():
    payload = {
        "code": 0,
        "data": {
            "sh600156": {
                "qfqday": [
                    ["2026-05-15", "14.880", "13.100", "15.610", "12.770", "1252136.000"],
                    ["2026-05-18", "12.600", "13.490", "13.860", "12.350", "924325.000"],
                ],
            },
        },
    }
    with patch("src.data.sina_kline_api.httpx.Client") as mock_client:
        inst = mock_client.return_value.__enter__.return_value
        inst.get.return_value.json.return_value = payload
        df = _tencent_daily_bars("600156", 10)
    assert df is not None
    assert len(df) == 2
    assert float(df.iloc[-1]["close"]) == 13.49

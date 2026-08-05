"""fetch_realtime_spot：MOCK=0 时全源失败不得 silent mock。"""
import pandas as pd

from src.data import fetcher


def test_fetch_realtime_spot_empty_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr(fetcher, "USE_MOCK", False)
    monkeypatch.setattr(
        "src.data.eastmoney_api.fetch_a_share_list",
        lambda: pd.DataFrame(),
    )
    monkeypatch.setattr(
        "src.data.sina_spot_api.fetch_a_share_list_sina",
        lambda: pd.DataFrame(),
    )

    df = fetcher.fetch_realtime_spot()
    assert df.empty
    assert fetcher.LAST_REALTIME_SPOT_STATUS == "empty"

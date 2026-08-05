"""晋级矩阵分母：昨日涨停池 lbc 优先。"""
from src.engine.daily_review import _promotion_candidates


def test_candidates_primary_zt_lbc_not_cache_walk():
    """cache 递推可能把首板算成 2 板；分母应以昨日 zt lbc=2 的 5 只为准。"""
    cache = {
        "20260518": [
            {"code": "603311", "continuous_limit_up": 3},
            {"code": "002421", "continuous_limit_up": 3},
            {"code": "601678", "continuous_limit_up": 3},
            {"code": "002374", "continuous_limit_up": 3},
            {"code": "601133", "continuous_limit_up": 3},
            {"code": "000001", "continuous_limit_up": 2},
        ],
        "20260519": [],
    }
    yesterday_pool = {r["code"]: r for r in cache["20260518"]}
    zt_y = {
        "603311": {"lbc": 2, "name": "金海高科"},
        "002421": {"lbc": 2, "name": "达实智能"},
        "601678": {"lbc": 2, "name": "滨化股份"},
        "002374": {"lbc": 2, "name": "中锐股份"},
        "601133": {"lbc": 2, "name": "柏诚股份"},
        "000001": {"lbc": 1, "name": "平安"},
    }
    cand = _promotion_candidates(yesterday_pool, zt_y, "20260518", cache)
    two_board = [c for c, (pb, _) in cand.items() if pb == 2]
    assert len(two_board) == 5
    assert "000001" not in two_board or cand["000001"][0] == 1

"""分时做 T 买卖点（7 格坐标）"""
from src.engine.minute_signals import (
    GRID_PCT,
    compute_minute_signals,
    default_signal_filter,
    grid_gap_vs_avg,
    price_at_grid,
)


def _bars(seq):
    """seq: [(p, avg, vol), ...]"""
    out = []
    for i, item in enumerate(seq):
        if len(item) == 2:
            p, a = item
            v = 1000 + i * 50
        else:
            p, a, v = item
        out.append({
            "t": f"9:{30 + i:02d}" if i < 30 else f"10:{i - 30:02d}",
            "p": p,
            "avg": a,
            "vol_bar": v,
            "pct": 0.0,
        })
    return out


def test_grid_price_formula():
    pre = 10.0
    assert price_at_grid(pre, 7) == pre * (1 + 7 * GRID_PCT / 100)
    assert price_at_grid(pre, -7) == pre * (1 - 7 * GRID_PCT / 100)


def test_t_buy_below_ma_and_rebound():
    pre = 10.0
    seq = [
        (10.50, 10.55, 800),
        (10.48, 10.54, 800),
        (10.46, 10.53, 800),
        (10.44, 10.52, 800),
        (10.42, 10.51, 800),
        (10.40, 10.50, 800),
        (10.38, 10.48, 800),
        (10.30, 10.45, 800),
        (10.28, 10.45, 800),
        (10.05, 10.42, 3200),
        (10.00, 10.40, 2800),
        (10.05, 10.85, 900),
    ]
    bars = _bars(seq)
    assert grid_gap_vs_avg(10.05, 10.85, pre) <= -4
    sig = compute_minute_signals(bars, pre_close=pre)
    assert any(s["kind"] == "t_buy" for s in sig)


def test_t_sell_above_ma_shrink_vol():
    pre = 10.0
    seq = []
    for i in range(6):
        seq.append((10.0 + i * 0.02, 10.05, 2000 - i * 250))
    seq.append((10.92, 10.12, 550))
    seq.append((10.99, 10.14, 400))
    bars = _bars(seq)
    assert grid_gap_vs_avg(10.99, 10.14, pre) >= 6
    sig = compute_minute_signals(bars, pre_close=pre)
    assert any(s["kind"] == "t_sell" for s in sig)


def test_default_filter_only_t_kinds():
    f = default_signal_filter()
    assert set(f.keys()) == {"t_buy", "t_sell"}
    assert all(f.values())

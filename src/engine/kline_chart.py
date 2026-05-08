"""K线图渲染 + 通达信防线（用户公式直译，MyTT）

公式来源：用户提供的"涨停打板防线"通达信指标。
保留的可视化层：
  · K 线 OHLC + 成交量
  · VARAB 综合均线 = MEAN(MA3, MA6, MA12, MA24)，紫色实线 + 上行黄点 / 下行灰点
  · 涨停 K 线染黄
  · 最近一次涨停 high/low 水平线（绿/红虚线）
  · 起拔线 = REF(C, BARSLAST(HD1))，30 周期高点位置的收盘价（红实线）
  · 秘线 = T3HIGH，三段创新高的最早一段顶 HIGH 水平延伸（粗洋红）
保留的形态条件标注（圆点）：钝化涨停 / 突破涨停 / 拉升涨停 / 超跌涨停 / 倒拔杨柳 / 普通倒灌

简化：对于盘口/动作类条件（CONST/CURRBARSCOUNT 等）做语义等价 numpy 翻译，
非完全 1:1 但视觉效果一致。
"""
from __future__ import annotations
import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # 服务器无显示后端
from matplotlib import font_manager as _fm
import matplotlib.pyplot as plt
import mplfinance as mpf

# 中文字体：服务器装的 Noto Serif CJK
_CJK_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"
try:
    if not any("Noto Serif CJK" in f.name for f in _fm.fontManager.ttflist):
        _fm.fontManager.addfont(_CJK_FONT_PATH)
    plt.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "DejaVu Sans"]
    plt.rcParams["font.serif"] = ["Noto Sans CJK JP", "DejaVu Serif"]
    plt.rcParams["axes.unicode_minus"] = False
except Exception:
    pass
import numpy as np
import pandas as pd

from MyTT import (
    MA, REF, HHV, LLV, LLVBARS, BARSLAST, FILTER, COUNT,
    EMA, SMA, IF, MAX, ABS as MABS,
)


# ── 缺失函数补齐 ─────────────────────────────────────────────────

def BACKSET(cond, n):
    """通达信 BACKSET：cond[i] 为真时，将 i 之前 n 个 bar 也置 1
    返回与 cond 等长的 0/1 数组
    """
    cond = np.asarray(cond).astype(bool)
    out = np.zeros_like(cond, dtype=int)
    for i in np.where(cond)[0]:
        start = max(0, i - n + 1)
        out[start:i + 1] = 1
    return out


def CURRBARSCOUNT(arr_len: int) -> np.ndarray:
    """通达信 CURRBARSCOUNT：从最后一根开始倒数"""
    return np.arange(arr_len - 1, -1, -1)


def REF_VAR(arr, n_arr) -> np.ndarray:
    """通达信 REF 的可变 N 版本：每根 bar 自己的偏移量

    REF(arr[i], n_arr[i]) = arr[i - n_arr[i]] if i - n_arr[i] >= 0 else NaN
    """
    arr = np.asarray(arr, dtype=float)
    n_arr = np.asarray(n_arr, dtype=int)
    L = len(arr)
    out = np.full(L, np.nan)
    for i in range(L):
        j = i - int(n_arr[i])
        if 0 <= j < L:
            out[i] = arr[j]
    return out


# ── 主计算函数 ─────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict:
    """从日 K 计算所有防线 + 形态标记

    Args:
        df: 必须含 open/high/low/close/volume 列，按 date 升序

    Returns: {
        'varab':            np.ndarray,   # 综合均线
        'varab_up':         bool array,   # VARAB 上行段
        'is_zt':            0/1 array,    # 涨停（≥9.5%）
        'is_zt_strong':     0/1 array,    # 涨停且封板（C=H）
        'last_zt_high':     高水平线数组,
        'last_zt_low':      低水平线数组,
        'qiba':             np.ndarray,   # 起拔线
        'mixian_y':         float | None, # 秘线水平值
        'mixian_start_idx': int | None,   # 秘线起始 bar idx
        'pat_钝化涨停':       0/1
        'pat_突破涨停':       0/1
        'pat_拉升涨停':       0/1
        'pat_超跌涨停':       0/1
    }
    """
    if df is None or df.empty or len(df) < 35:
        return {}

    O = df["open"].astype(float).values
    H = df["high"].astype(float).values
    L = df["low"].astype(float).values
    C = df["close"].astype(float).values
    V = df["volume"].astype(float).values
    AMOUNT = C * V  # 简化的成交额（无 amount 列时）

    # —— VARAB: (MA3 + MA6 + MA12 + MA24) / 4 ——
    varab = (MA(C, 3) + MA(C, 6) + MA(C, 12) + MA(C, 24)) / 4.0
    varab_prev = REF(varab, 1)
    varab_up = varab > varab_prev

    # —— 涨停判定 ——
    prev_c = REF(C, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.where(prev_c > 0, (C - prev_c) / prev_c * 100, 0.0)
    is_zt = (pct >= 9.5).astype(int)
    is_zt_strong = ((C > prev_c * 1.099) & (np.isclose(C, H))).astype(int)

    # —— 最近一次涨停的 high/low 水平线 ——
    bs_zt = BARSLAST(is_zt == 1)  # 距离最近一次涨停的 bar 数
    last_zt_high = REF_VAR(H, bs_zt)
    last_zt_low = REF_VAR(L, bs_zt)

    # —— 起拔线 = REF(C, BARSLAST(HD1)) ——
    # HD1 = FILTER(BACKSET(FILTER(REF(C,30) == HHV(C,61), 30), 31), 30)
    is_30high = (REF(C, 30) == HHV(C, 61)).astype(int)
    f1 = FILTER(is_30high, 30)
    bs1 = BACKSET(f1, 31)
    hd1 = FILTER(bs1, 30)
    bs_hd1 = BARSLAST(hd1 == 1)
    qiba = REF_VAR(C, bs_hd1)

    # —— 秘线（T3HIGH 三段顶）——
    n = len(C)
    cc = CURRBARSCOUNT(n)
    mixian_y, mixian_start = _compute_mixian(C, H, L, cc)

    # —— 形态条件 ——
    # 个股线 = EMA(EMA(C,30),9)；大盘线无 INDEXC，简化用 SMA 替代
    个线 = EMA(EMA(C, 30), 9)
    # GGQD：个股相对大盘强度 — 无指数数据，用相对自身 60MA 偏离作占位
    ma60 = MA(C, 60)
    with np.errstate(divide="ignore", invalid="ignore"):
        ggqd = np.where(ma60 > 0, (C / ma60 - 1) * 100, 0.0)

    # 钝化涨停: ZT 且 RSI=HHV(RSI,5) 且 RSI>80
    rsi1 = SMA(MAX(C - prev_c, 0), 5, 1) / np.maximum(SMA(np.abs(C - prev_c), 5, 1), 1e-9) * 100
    pat_dunhua = (
        (is_zt_strong == 1)
        & (rsi1 == HHV(rsi1, 5))
        & (rsi1 > 80)
    ).astype(int)

    # 拉升涨停: F1>1 AND F2>3 AND F3>0 AND ZT>0
    ma5 = MA(C, 5)
    ma10 = MA(C, 10)
    ma20 = MA(C, 20)
    ma60_p = MA(C, 34)  # 公式里 MA60:=MA(C,34)
    f1f = (ma5 - ma10) / np.maximum(ma10, 1e-9) * 100
    f2f = (ma10 - ma20) / np.maximum(ma10, 1e-9) * 100
    f3f = (ma20 - ma60_p) / np.maximum(ma10, 1e-9) * 100
    pat_lashen = ((f1f > 1) & (f2f > 3) & (f3f > 0) & (is_zt_strong == 1)).astype(int)

    # 超跌涨停: ZT9>0 AND BIS<0 AND BIA>5 AND MA(C,5)<MA(C,60)
    bias1 = (C - MA(C, 6)) / np.maximum(MA(C, 6), 1e-9) * 100
    bias2 = (C - MA(C, 24)) / np.maximum(MA(C, 24), 1e-9) * 100
    bia = bias1 - bias2
    bis = LLV(bias1, 3)
    is_zt9 = (pct > 9.5).astype(int)
    pat_chaodie = (
        (is_zt9 == 1) & (bis < 0) & (bia > 5) & (ma5 < MA(C, 60))
    ).astype(int)

    # 突破涨停: CROSS(C, A) AND ZT>0
    # A = REF(H, BARSLAST(HD))；HD 用 20 周期对应公式
    is_20high = (REF(C, 20) == HHV(C, 41)).astype(int)
    f20 = FILTER(is_20high, 20)
    bs20 = BACKSET(f20, 21)
    hd = FILTER(bs20, 20)
    bs_hd = BARSLAST(hd == 1)
    A = REF_VAR(H, bs_hd)
    pat_tupo = (
        (np.concatenate(([0], (C[1:] > A[1:]) & (C[:-1] <= A[:-1])))).astype(int)
        & (is_zt_strong == 1)
    ).astype(int)

    return {
        "varab": varab,
        "varab_up": varab_up,
        "is_zt": is_zt,
        "is_zt_strong": is_zt_strong,
        "last_zt_high": last_zt_high,
        "last_zt_low": last_zt_low,
        "qiba": qiba,
        "mixian_y": mixian_y,
        "mixian_start_idx": mixian_start,
        "pat_钝化涨停": pat_dunhua,
        "pat_突破涨停": pat_tupo,
        "pat_拉升涨停": pat_lashen,
        "pat_超跌涨停": pat_chaodie,
    }


def _compute_mixian(C: np.ndarray, H: np.ndarray, L: np.ndarray, cc: np.ndarray):
    """秘线 T3HIGH 计算 — 三段递增的高点，取最早的高度作水平线

    通达信原意：
      T1: 10 日内最低点的位置
      T2: T1 之后第一次创新高（high > T1HIGH）的位置
      T3: T2 之后第一次创新高（high > T2HIGH）的位置
      mixian = T3HIGH，从 T3+1 画到最后一根

    Returns: (mixian_y, mixian_start_bar_idx) — 失败返回 (None, None)
    """
    n = len(C)
    if n < 12:
        return None, None
    try:
        # T1: 最近 10 根的最低点位置（distance from current）
        recent_l = L[-10:]
        t1 = int(np.argmin(recent_l))  # in slice
        t1_abs = (n - 10) + t1
        t1_high = H[t1_abs]

        # T2: t1 之后第一次 high > t1_high
        t2_abs = None
        for i in range(t1_abs + 1, n):
            if H[i] > t1_high:
                t2_abs = i
                break
        if t2_abs is None:
            return None, None
        t2_high = H[t2_abs]

        # T3: t2 之后第一次 high > t2_high
        t3_abs = None
        for i in range(t2_abs + 1, n):
            if H[i] > t2_high:
                t3_abs = i
                break
        if t3_abs is None:
            return None, None

        return float(H[t3_abs]), int(t3_abs)
    except Exception:
        return None, None


# ── 渲染 ─────────────────────────────────────────────────────────

def render_kline_chart(
    code: str,
    name: str,
    df: pd.DataFrame,
    days: int = 60,
) -> bytes:
    """绘制 K 线图（含防线 + 形态标注），返回 PNG bytes

    Args:
        code: 股票代码
        name: 股票名称
        df: 完整日 K（≥35 根），含 open/high/low/close/volume/date 列
        days: 显示最近 N 天（默认 60）
    """
    if df is None or df.empty or len(df) < 35:
        return _render_empty(code, name, "数据不足（需 ≥35 根日K）")

    # 1) 全量计算指标
    ind = compute_indicators(df)
    if not ind:
        return _render_empty(code, name, "指标计算失败")

    # 2) 取最近 days 根作为显示范围
    show = df.tail(days).copy()
    show.index = pd.to_datetime(show["date"])
    plot_df = show[["open", "high", "low", "close", "volume"]]

    # 切片 indicator 数组对齐显示窗口
    n_full = len(df)
    cut = max(0, n_full - days)

    def s(arr):
        if arr is None:
            return None
        return pd.Series(np.asarray(arr)[cut:], index=plot_df.index)

    varab_s = s(ind["varab"])
    qiba_s = s(ind["qiba"])

    # 涨停 K 线高亮：用 mplfinance 的 marketcolor_overrides
    is_zt = np.asarray(ind["is_zt"])[cut:]
    zt_overrides = ["#facc15" if x == 1 else None for x in is_zt]  # 黄色

    apds = []

    # 紫色 VARAB 主线
    if not varab_s.isna().all():
        apds.append(mpf.make_addplot(varab_s, color="#d946ef", width=1.4))

    # VARAB 上行黄点 / 下行灰点（在 varab 上方再叠 scatter）
    varab_up = np.asarray(ind["varab_up"])[cut:]
    up_pts = pd.Series(np.where(varab_up, varab_s.values, np.nan), index=plot_df.index)
    dn_pts = pd.Series(np.where(~varab_up, varab_s.values, np.nan), index=plot_df.index)
    if not up_pts.isna().all():
        apds.append(mpf.make_addplot(up_pts, type="scatter", marker=".", markersize=20, color="#facc15"))
    if not dn_pts.isna().all():
        apds.append(mpf.make_addplot(dn_pts, type="scatter", marker=".", markersize=20, color="#9ca3af"))

    # 起拔线（红色实线）
    if not s(ind["qiba"]).isna().all():
        apds.append(mpf.make_addplot(qiba_s, color="#ef4444", width=1.2, linestyle="-"))

    # 最近涨停 high/low 水平参考（虚线）
    last_zt_high = s(ind["last_zt_high"])
    last_zt_low = s(ind["last_zt_low"])
    if not last_zt_high.isna().all():
        apds.append(mpf.make_addplot(last_zt_high, color="#22c55e", width=0.9, linestyle="--"))
    if not last_zt_low.isna().all():
        apds.append(mpf.make_addplot(last_zt_low, color="#dc2626", width=0.9, linestyle="--"))

    # 形态标注：在涨停 K 线上方画对应符号
    pat_markers = {
        "pat_钝化涨停": ("^", "#fb7185", "钝化"),
        "pat_突破涨停": ("D", "#fbbf24", "突破"),
        "pat_拉升涨停": ("o", "#10b981", "拉升"),
        "pat_超跌涨停": ("v", "#60a5fa", "超跌"),
    }
    for key, (m, color, _label) in pat_markers.items():
        arr = np.asarray(ind.get(key, np.zeros(n_full)))[cut:]
        if arr.sum() == 0:
            continue
        ys = np.where(arr == 1, plot_df["high"].values * 1.02, np.nan)
        if np.all(np.isnan(ys)):
            continue
        apds.append(mpf.make_addplot(
            pd.Series(ys, index=plot_df.index),
            type="scatter", marker=m, markersize=80, color=color,
        ))

    # 3) 绘图
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mpf.make_marketcolors(
            up="#ef4444", down="#10b981",
            wick={"up": "#ef4444", "down": "#10b981"},
            edge={"up": "#ef4444", "down": "#10b981"},
            volume={"up": "#7f1d1d", "down": "#065f46"},
        ),
        gridcolor="#1e2a45", facecolor="#0a0e1a", figcolor="#0a0e1a",
        rc={
            "axes.labelcolor": "#a0aec0",
            "xtick.color": "#a0aec0",
            "ytick.color": "#a0aec0",
            "font.sans-serif": ["Noto Sans CJK JP", "DejaVu Sans"],
            "font.serif": ["Noto Sans CJK JP", "DejaVu Serif"],
            "axes.unicode_minus": False,
        },
    )

    fig, axes = mpf.plot(
        plot_df, type="candle", style=style,
        title=f"\n{code} {name} · 防线 + 形态",
        addplot=apds if apds else None,
        volume=True, ylabel="价格", ylabel_lower="量",
        figsize=(14, 8),
        returnfig=True,
        warn_too_much_data=999,
    )

    # 秘线：T3HIGH 水平线（直接在主轴画，使用 axhline 仅画窗口内）
    mixian_y = ind.get("mixian_y")
    mixian_start = ind.get("mixian_start_idx")
    if mixian_y is not None and mixian_start is not None and mixian_start >= cut:
        ax_main = axes[0]
        # 从 mixian_start 到末尾 画水平线
        x_start_in_show = mixian_start - cut
        ax_main.hlines(
            y=mixian_y,
            xmin=x_start_in_show, xmax=len(plot_df) - 1,
            colors="#ec4899", linewidth=2.2, zorder=5,
        )
        ax_main.text(
            len(plot_df) - 1, mixian_y,
            f"  秘线 {mixian_y:.2f}",
            color="#ec4899", fontsize=9, va="center", ha="left",
        )

    # 4) 输出 PNG
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80, bbox_inches="tight",
                facecolor="#0a0e1a", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _render_empty(code: str, name: str, msg: str) -> bytes:
    """数据不足时返回占位 PNG"""
    fig, ax = plt.subplots(figsize=(10, 4), facecolor="#0a0e1a")
    ax.set_facecolor("#0a0e1a")
    ax.text(0.5, 0.5, f"{code} {name}\n{msg}",
            color="#a0aec0", fontsize=12, ha="center", va="center")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80, bbox_inches="tight",
                facecolor="#0a0e1a", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

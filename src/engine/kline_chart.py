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

    # —— 倒拔杨柳 ——
    # ABA = REF(涨停,1) AND O>REF(C,1) AND C<O AND O=H AND V=HHV(V,34)
    # 昨涨停 + 今高开收阴 + 开盘即最高 + 量能 34 日新高
    yesterday_zt_strong = REF(is_zt_strong, 1)  # 昨日是否涨停且封板
    aba_arr = (
        (yesterday_zt_strong == 1)
        & (O > REF(C, 1))
        & (C < O)
        & np.isclose(O, H)
        & np.isclose(V, HHV(V, 34))
    ).astype(int)

    # —— 普通倒灌 ——
    # SAT = (AMOUNT/C) / (HHV(AMOUNT,120)/HHV(C,120))；量能饱和=min(SAT,1)*100
    # 倒灌 = REF(涨停_含一字, 1) AND 量能饱和>75 AND O>REF(C,1) AND C<O AND O<H
    with np.errstate(divide="ignore", invalid="ignore"):
        sat_num = np.where(C > 0, AMOUNT / C, 0.0)
        sat_den = np.where(HHV(C, 120) > 0,
                           HHV(AMOUNT, 120) / HHV(C, 120), 1.0)
        sat = np.where(sat_den > 0, sat_num / sat_den, 0.0)
    saturation = np.minimum(sat, 1.0) * 100
    daoguan_arr = (
        (yesterday_zt_strong == 1)
        & (saturation > 75)
        & (O > REF(C, 1))
        & (C < O)
        & (O < H)
    ).astype(int)

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

    # —— 倒拔杨柳 / 普通倒灌 的水平延伸线起点 ——
    # 用最近 60/90 天内最近一次触发那天的 O / H 作 y 值，从触发 bar 起延伸到末根
    daoba_y, daoba_start = _last_trigger_value(aba_arr, O, window=60)
    daoguan_y, daoguan_start = _last_trigger_value(daoguan_arr, H, window=90)

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
        "aba": aba_arr,           # 倒拔杨柳触发数组
        "daoguan": daoguan_arr,   # 普通倒灌触发数组
        "daoba_y": daoba_y,
        "daoba_start_idx": daoba_start,
        "daoguan_y": daoguan_y,
        "daoguan_start_idx": daoguan_start,
        "pat_钝化涨停": pat_dunhua,
        "pat_突破涨停": pat_tupo,
        "pat_拉升涨停": pat_lashen,
        "pat_超跌涨停": pat_chaodie,
    }


def _last_trigger_value(cond_arr: np.ndarray, val_arr: np.ndarray, window: int):
    """找 cond_arr 在末根往前 window 内最近一次为真的 bar idx 与 val_arr 在该 bar 的值"""
    cond_arr = np.asarray(cond_arr).astype(int)
    n = len(cond_arr)
    start = max(0, n - window)
    for i in range(n - 1, start - 1, -1):
        if cond_arr[i] == 1:
            return float(val_arr[i]), int(i)
    return None, None


def _compute_mixian(C: np.ndarray, H: np.ndarray, L: np.ndarray, cc: np.ndarray):
    """秘线 T3HIGH 计算 — 通达信公式 BACKWARD 回溯三段顶

    通达信原意（CCVV=CURRBARSCOUNT 在末根=0）：
      T1: LLVBARS(LOW, 10) — 近 10 根最低点距末根的 bar 数（同低取最近）
      T2: BARSLAST((CURRBARSCOUNT > T1) AND (HIGH > T1HIGH))
          → 在 T1 【之前】（更老的 bars）最近一次 HIGH > T1HIGH 的位置
      T3: 同理，在 T2 之前最近一次 HIGH > T2HIGH 的位置
      秘线 = T3HIGH，从 T3+1 起到末根画粗水平线

    与之前的 forward 扫描相反：方向是从 T1 → 更早的历史去找历史高点。
    """
    n = len(C)
    if n < 12:
        return None, None
    try:
        last = n - 1

        # T1: 近 10 根 LLV 距末根的 bar 数（同低取最近）
        recent_l = L[-10:]
        # rightmost arg-min（最近的最低）
        rightmost_pos = (len(recent_l) - 1) - int(np.argmin(recent_l[::-1]))
        t1_idx = (n - 10) + rightmost_pos
        t1_high = float(H[t1_idx])

        # T2: 在 t1_idx 之前（更老）最近一次 HIGH > T1HIGH
        t2_idx = None
        for i in range(t1_idx - 1, -1, -1):
            if H[i] > t1_high:
                t2_idx = i
                break
        if t2_idx is None:
            return None, None
        t2_high = float(H[t2_idx])

        # T3: 在 t2_idx 之前（更老）最近一次 HIGH > T2HIGH
        t3_idx = None
        for i in range(t2_idx - 1, -1, -1):
            if H[i] > t2_high:
                t3_idx = i
                break
        if t3_idx is None:
            return None, None

        return float(H[t3_idx]), int(t3_idx)
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

    ax_main = axes[0]

    def _draw_horizontal(y, start_idx, color, label, linewidth=1.6, linestyle="-"):
        """从 start_idx 到末尾画水平线 + 末端文字标签（自动裁剪到显示窗口）"""
        if y is None or start_idx is None:
            return
        x_start = max(0, start_idx - cut)
        if x_start >= len(plot_df):
            return
        ax_main.hlines(
            y=y, xmin=x_start, xmax=len(plot_df) - 1,
            colors=color, linewidth=linewidth, linestyle=linestyle, zorder=5,
        )
        ax_main.text(
            len(plot_df) - 1, y, f"  {label} {y:.2f}",
            color=color, fontsize=9, va="center", ha="left",
        )

    # 秘线（T3HIGH，粗洋红）
    _draw_horizontal(
        ind.get("mixian_y"), ind.get("mixian_start_idx"),
        color="#ec4899", label="秘线", linewidth=2.2,
    )

    # 倒拔杨柳（最近 60 天内 ABA 触发那天的 OPEN，浅灰）
    _draw_horizontal(
        ind.get("daoba_y"), ind.get("daoba_start_idx"),
        color="#d1d5db", label="倒拔杨柳", linewidth=1.5, linestyle="--",
    )

    # 普通倒灌（最近 90 天内倒灌触发那天的 HIGH，浅灰）
    _draw_horizontal(
        ind.get("daoguan_y"), ind.get("daoguan_start_idx"),
        color="#a0aec0", label="倒灌", linewidth=1.5, linestyle="--",
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

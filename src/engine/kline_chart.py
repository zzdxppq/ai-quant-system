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

# 中文字体：Linux Noto（若存在）+ Windows 常见黑体 / 回退
_CJK_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"
_CN_SANS = [
    "Microsoft YaHei",
    "SimHei",
    "DengXian",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "DejaVu Sans",
    "sans-serif",
]
try:
    if Path(_CJK_FONT_PATH).is_file() and not any(
        "Noto Serif CJK" in f.name for f in _fm.fontManager.ttflist
    ):
        _fm.fontManager.addfont(_CJK_FONT_PATH)
except Exception:
    pass
plt.rcParams["font.sans-serif"] = _CN_SANS
plt.rcParams["font.serif"] = _CN_SANS
plt.rcParams["axes.unicode_minus"] = False
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


def _chart_title(code: str, name: str, suffix: str) -> str:
    """避免 Linux 无中文字体时标题乱码，仅用 ASCII + 代码/名称。"""
    nm = (str(name or "").strip() or str(code or "").strip())
    return f"{code} {nm} - {suffix}"


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

    # 同日多根时保留最后一条；归一到日历日，避免同日多时刻索引导致 mplfinance 少画一根
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.normalize()
    d = d.dropna(subset=["date"]).sort_values("date").drop_duplicates(subset=["date"], keep="last")
    df = d.reset_index(drop=True)
    if len(df) < 35:
        return _render_empty(code, name, "数据不足（需 ≥35 根日K）")

    # 1) 全量计算指标
    ind = compute_indicators(df)
    if not ind:
        return _render_empty(code, name, "指标计算失败")

    # 2) 取最近 days 根作为显示范围（索引唯一，避免重复日吞 K）
    show = df.tail(days).copy()
    show["date"] = pd.to_datetime(show["date"], errors="coerce").dt.normalize()
    show = show.dropna(subset=["date"]).sort_values("date")
    show = show.drop_duplicates(subset=["date"], keep="last")
    show.index = pd.DatetimeIndex(show["date"], freq=None)
    plot_df = show[["open", "high", "low", "close", "volume"]]
    if plot_df.index.duplicated().any():
        plot_df = plot_df[~plot_df.index.duplicated(keep="last")]

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
            "font.sans-serif": _CN_SANS,
            "font.serif": _CN_SANS,
            "axes.unicode_minus": False,
        },
    )

    try:
        fig, axes = mpf.plot(
            plot_df, type="candle", style=style,
            title=f"\n{_chart_title(code, name, 'daily K defense')}",
            addplot=apds if apds else None,
            volume=True, ylabel="Price", ylabel_lower="Vol",
            figsize=(14, 8),
            returnfig=True,
            warn_too_much_data=999,
        )
    except Exception as e:
        plt.close("all")
        return _render_empty(code, name, f"日K渲染失败: {e!s}"[:200])

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
    fig.savefig(
        buf,
        format="png",
        dpi=80,
        bbox_inches="tight",
        pad_inches=0.18,
        facecolor="#0a0e1a",
        edgecolor="none",
    )
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def render_weekly_kline_chart(
    code: str,
    name: str,
    df: pd.DataFrame,
    weeks: int = 120,
) -> bytes:
    """周 K 蜡烛 + MA5 / MA10 / MA21 / MA240（周级别均线周期）。

    MA240 需约 240 根周 K 才有意义；不足时仍画 K+短周期均线，且仅在有非 NaN 点时叠加 MA240
    （mplfinance 对全 NaN 的 addplot 会抛错）。
    """
    min_bars = 30
    if df is None or df.empty or len(df) < min_bars:
        return _render_empty(code, name, f"周K 数据不足（需 ≥{min_bars} 根）")

    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if len(d) < min_bars:
        return _render_empty(code, name, f"周K 数据不足（需 ≥{min_bars} 根）")

    c = d["close"].astype(float)
    d["ma5"] = c.rolling(5, min_periods=1).mean()
    d["ma10"] = c.rolling(10, min_periods=1).mean()
    d["ma21"] = c.rolling(21, min_periods=1).mean()
    # 标准 240 周均线：不足 240 根时为 NaN，由下游决定是否绘制
    d["ma240"] = c.rolling(240, min_periods=240).mean()

    w = max(40, min(200, int(weeks)))
    show = d.tail(w).copy()
    show.index = show["date"]
    plot_df = show[["open", "high", "low", "close", "volume"]]

    ma5 = show.set_index("date")["ma5"]
    ma10 = show.set_index("date")["ma10"]
    ma21 = show.set_index("date")["ma21"]
    ma240 = show.set_index("date")["ma240"]

    apds = [
        mpf.make_addplot(ma5, color="#fbbf24", width=1.0, label="MA5"),
        mpf.make_addplot(ma10, color="#60a5fa", width=1.0, label="MA10"),
        mpf.make_addplot(ma21, color="#a78bfa", width=1.0, label="MA21"),
    ]
    if ma240.notna().any():
        apds.append(mpf.make_addplot(ma240, color="#f472b6", width=1.1, label="MA240"))

    ma_note = "MA5/10/21/240" if ma240.notna().any() else "MA5/10/21 no MA240"
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
            "font.sans-serif": _CN_SANS,
            "font.serif": _CN_SANS,
            "axes.unicode_minus": False,
        },
    )

    try:
        fig, axes = mpf.plot(
            plot_df,
            type="candle",
            style=style,
            title=f"\n{_chart_title(code, name, f'weekly K {ma_note}')}",
            addplot=apds,
            volume=True,
            ylabel="Price",
            ylabel_lower="Vol",
            figsize=(14, 8),
            returnfig=True,
            warn_too_much_data=999,
        )

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=80, bbox_inches="tight",
                    facecolor="#0a0e1a", edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        plt.close("all")
        return _render_empty(code, name, f"周K 渲染失败：{e!s}"[:200])


def analyze_stock_action(
    code: str,
    name: str,
    df: pd.DataFrame,
    market_ctx: dict | None = None,
    auction_ctx: dict | None = None,
) -> dict:
    """生成单股操作建议（基于防线 + 市场环境）

    Args:
        df: 完整日 K（≥35 根）
        market_ctx: {
            concentration:    float | None,  # 板块集中度 %
            lianban_index_pct: float | None, # 昨日连板指数 %
            b1_rate:          float | None,  # 1进2 成功率 %
            attack_phase:     str | None,    # 主动攻击热度阶段
            attack_count:     int | None,    # 攻击数 / 30
        }
    Returns: {
        code, name, price, gain_pct, volume_lots,
        qiba, mixian,
        above_qiba, above_mixian, above_both,
        operable: bool,
        position: str,   # "空仓" / "轻仓(10%)" / "正常(20-30%)" / "重仓(50%)"
        buy_condition: str,
        stop_loss: str,
        summary: str,
        market_ctx: dict,
    }
    """
    market_ctx = market_ctx or {}
    out = {
        "code": code, "name": name,
        "price": None, "gain_pct": None, "volume_lots": None,
        "qiba": None, "mixian": None,
        "above_qiba": False, "above_mixian": False, "above_both": False,
        "operable": False, "position": "空仓",
        "buy_condition": "—",
        "stop_loss": "—",
        "summary": "数据不足",
        "market_ctx": market_ctx,
    }
    if df is None or df.empty or len(df) < 35:
        return out

    ind = compute_indicators(df)
    if not ind:
        return out

    today = df.iloc[-1]
    close = float(today["close"])
    from src.data.sina_kline_api import resolve_prev_close

    prev_close = resolve_prev_close(code)
    gain = (close / prev_close - 1) * 100 if prev_close > 0 else 0.0
    volume_lots = float(today.get("volume", 0)) / 100  # 股 → 手

    # 竞价时段数据（9:25 开盘价 + 竞价涨幅 + 竞价成交量）— 优先使用
    # 来自 latest_screener.hits[] / latest_ranking.ranking[] 的 auction_* 字段
    auction_ctx = auction_ctx or {}
    auction_open = auction_ctx.get("open_price")
    auction_gain = auction_ctx.get("auction_gain")
    auction_vol_lots = auction_ctx.get("auction_volume_lots")
    use_auction = auction_open is not None or auction_gain is not None

    display_price = float(auction_open) if auction_open is not None else round(close, 2)
    display_gain = float(auction_gain) if auction_gain is not None else round(gain, 2)
    display_volume = (
        float(auction_vol_lots) if auction_vol_lots is not None
        else round(volume_lots, 0)
    )

    qiba_arr = ind.get("qiba")
    qiba = float(qiba_arr[-1]) if qiba_arr is not None and not np.isnan(qiba_arr[-1]) else None
    mixian = ind.get("mixian_y")

    above_qiba = qiba is not None and close > qiba
    above_mixian = mixian is not None and close > mixian
    above_both = above_qiba and above_mixian

    out.update({
        "price": round(display_price, 2),
        "gain_pct": round(display_gain, 2),
        "volume_lots": round(display_volume, 0),
        "data_source": "auction" if use_auction else "kline_close",
        "qiba": round(qiba, 2) if qiba else None,
        "mixian": round(mixian, 2) if mixian else None,
        "above_qiba": above_qiba,
        "above_mixian": above_mixian,
        "above_both": above_both,
    })

    # ── 走 v4.0 决策规则（与今日选股-决策列同源）──
    psd, checks, hit = _evaluate_v33(
        hit_dict=auction_ctx.get("hit_dict"),
        auction_ctx=auction_ctx,
        market_ctx=market_ctx,
        df=df,
    )

    b1_rate = market_ctx.get("b1_rate")
    if b1_rate is not None:
        try:
            from src.engine.screener_decision import _env_level
            market_ctx["b1_env_level"] = _env_level(float(b1_rate))
        except Exception:
            pass

    operable = bool(psd.get("can_open"))
    position = psd.get("position_text") or "0% (空仓)"
    env_veto = psd.get("veto_reason") in ("no_1to2", "high_board_gate")
    board = int(hit.get("continuous_limit_up", 0) or 0)

    # 止损位（环境否决时不展示，避免误导）
    stop_loss = None
    if not env_veto:
        if qiba is not None and mixian is not None:
            lower = min(qiba, mixian)
            stop_loss = f"跌破 {lower:.2f}（起拔/秘线较低者）-3%（约 {lower * 0.97:.2f}）即清仓"
        elif qiba is not None:
            stop_loss = f"跌破起拔线 {qiba:.2f} -3%（约 {qiba * 0.97:.2f}）即清仓"
        elif mixian is not None:
            stop_loss = f"跌破秘线 {mixian:.2f} -3%（约 {mixian * 0.97:.2f}）即清仓"
        else:
            stop_loss = "跌破今日开盘价 -3% 即清仓"

    buy_condition = None
    reversal_condition = None
    recommended_ladder = psd.get("ladder_label") or ""

    if operable:
        ladder = psd.get("ladder_label", "")
        if ladder.startswith("2进3"):
            buy_condition = "v4.0 全部条件已满足；建议按建议仓位入场。盘中破开盘价 -2% 即出。"
        elif ladder.startswith("3进4") or ladder.startswith("4进5") or ladder.startswith("5进6"):
            buy_condition = "v4.0 高位接力条件已满足；建议按建议仓位入场。盘中破开盘价或起拔线即出。"
        else:
            buy_condition = "已满足开仓条件，按建议仓位入场。"
    elif env_veto and board == 2:
        recommended_ladder = "空仓（1进2<8%，仅观察更高梯队）"
        reversal_condition = "1进2成功率回升至≥8%且硬门槛达标，再按质量分档参与2进3。"
    else:
        buy_condition = "暂不参与（详见综合打分中未达条件）"
        unmet = [c for c in checks if c.get("pass") is False]
        if unmet:
            parts = []
            for c in unmet:
                hint = c.get("hint") or c.get("need") or c.get("label", "")
                parts.append(f"{c.get('label', '')}{hint}".strip())
            reversal_condition = "；".join(parts) + "，再评估是否参与。"
        else:
            reversal_condition = psd.get("reason", "等待 v4.0 规则全部达标")

    if env_veto:
        checks_title = "📋 综合打分（环境未达标，以下仅作记录）"
        checks_conclusion = "→ 结论：环境否决，不开仓。"
    elif operable:
        checks_title = "📋 综合打分（v4.0 决策规则逐项）"
        checks_conclusion = "→ 结论：条件满足，可按建议仓位操作。"
    else:
        checks_title = "📋 综合打分（v4.0 决策规则逐项）"
        checks_conclusion = "→ 结论：条件未达，暂不参与。"

    ladder = recommended_ladder or psd.get("ladder_label") or ""
    if operable:
        summary = f"可操作 · {position}" + (f" · {ladder}" if ladder else "") + (" · 双线已突破" if above_both else "")
    else:
        rsn = str(psd.get("reason") or "条件未达").strip()
        summary = f"暂不操作 · {rsn[:160]}" if rsn else "暂不操作 · 条件未达"

    out.update({
        "operable": operable,
        "position": position,
        "ladder_label": psd.get("ladder_label"),
        "recommended_ladder": recommended_ladder,
        "reason": psd.get("reason"),
        "buy_condition": buy_condition,
        "stop_loss": stop_loss,
        "reversal_condition": reversal_condition,
        "checks": checks,
        "checks_title": checks_title,
        "checks_conclusion": checks_conclusion,
        "env_veto": env_veto,
        "veto_b1": env_veto,
        "summary": summary,
    })
    return out


# =====================================================================
# v4.0 决策映射器：用今日选股的 compute_per_stock_decision 出仓位 + 列条件
# =====================================================================
def _evaluate_v33(hit_dict, auction_ctx, market_ctx, df):
    """从可用上下文构建 hit & market_env，调 v4.0 规则；并产出"综合打分"明细。

    Returns: (psd, checks, hit)
      psd: compute_per_stock_decision 原 dict
      checks: list[{label, value, hint, pass, verdict?}]
      hit: 合并后的 ScreenerHit 字段 dict
    """
    from src.engine.screener_decision import compute_per_stock_decision, _env_level

    auction_ctx = auction_ctx or {}
    market_ctx = market_ctx or {}

    # ── 构造 hit ──
    if hit_dict and isinstance(hit_dict, dict):
        hit = dict(hit_dict)  # shallow copy
    else:
        hit = {}
    # 用 auction_ctx 补全
    for k_src, k_dst in [
        ("auction_gain", "auction_gain"),
        ("auction_turnover", "auction_turnover"),
        ("auction_volume_ratio", "auction_volume_ratio"),
        ("open_price", "open_price"),
    ]:
        if hit.get(k_dst) is None and auction_ctx.get(k_src) is not None:
            hit[k_dst] = auction_ctx[k_src]
    # 板数：缺失时从 K 线推（连续涨停计数）
    if hit.get("continuous_limit_up") is None:
        hit["continuous_limit_up"] = _count_consecutive_lu_from_kline(df, str(hit.get("code") or ""))

    market_env = {
        "b1_rate": market_ctx.get("b1_rate"),
        "concentration": market_ctx.get("concentration"),
        "market_limit_down": market_ctx.get("market_limit_down"),
        "space_red": market_ctx.get("space_red"),
    }
    # concept_zt_stats / space_board 不在 K 线 ctx 里，留空（影响仅在 5+板的备选概念条件）
    psd = compute_per_stock_decision(
        hit, market_env,
        concept_zt_stats=market_ctx.get("concept_zt_stats") or [],
        space_board_today=market_ctx.get("space_board_today"),
        market_highest_board=market_ctx.get("market_highest_board"),
        highest_board_tier_today=market_ctx.get("highest_board_tier_today"),
    )

    checks = _v33_checks(hit, market_env, psd)
    return psd, checks, hit


def _count_consecutive_lu_from_kline(df, code: str) -> int:
    """K 线推连续涨停板数（仅用于非选股池股票兜底）"""
    try:
        import pandas as pd
        if df is None or df.empty or len(df) < 2:
            return 0
        threshold = 19.5 if code.startswith(("300", "301", "688", "689")) else 9.5
        count = 0
        for i in range(len(df) - 1, 0, -1):
            prev_c = float(df.iloc[i - 1]["close"])
            cur_c = float(df.iloc[i]["close"])
            if prev_c <= 0:
                break
            chg = (cur_c / prev_c - 1) * 100
            if chg >= threshold:
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def _check_item(label: str, value: str, passed: bool, hint: str, *, env_veto: bool = False) -> dict:
    """单条综合打分：value 仅数值，hint 为 (需≥xx) / (达标)，verdict 为否决说明。"""
    item = {
        "label": label,
        "value": value,
        "hint": hint if not passed else "(达标)",
        "pass": passed,
        "need": hint.strip("()") if not passed else "达标",
    }
    if not passed:
        item["verdict"] = "一票否决" if env_veto else "未达标"
    return item


def _v33_checks(hit: dict, env: dict, psd: dict) -> list[dict]:
    """产出 v4.0 综合打分明细：每条规则的 label/value/hint/pass/verdict"""
    out: list[dict] = []
    board = int(hit.get("continuous_limit_up", 0) or 0)
    b1 = env.get("b1_rate")
    conc = env.get("concentration")
    at = hit.get("auction_turnover")
    prev_to = hit.get("prev_day_turnover")
    prev_ar = hit.get("prev_amount_ratio")
    prev_yizi = hit.get("prev_day_yizi")
    space_red = env.get("space_red")

    # 1) 1进2 环境门槛（2进3 硬门槛 ≥8%；其它梯队仍参考 ≥12%）
    if b1 is not None:
        try:
            b1f = float(b1)
            need = 8 if board == 2 else 12
            out.append(_check_item(
                "1进2 成功率",
                f"{b1f:.1f}%",
                b1f >= need,
                f"(需≥{need}%)",
                env_veto=(b1f < need),
            ))
        except Exception:
            pass

    # 2) 板块集中度（2进3 仅 3 层硬要求；展示仍用 ≥30；3+板 ≥25）
    threshold_conc = 30 if board == 2 else 25 if board >= 3 else None
    if threshold_conc is not None and conc is not None:
        try:
            cf = float(conc)
            out.append(_check_item(
                "板块集中度",
                f"{cf:.1f}%",
                cf >= threshold_conc,
                f"(需≥{threshold_conc}%)",
            ))
        except Exception:
            pass

    # 3) 竞价换手（2进3 硬门槛 ≥0.3；3+板 >0.5）
    if board == 2:
        threshold_at, at_op = 0.3, ">="
    elif board >= 3:
        threshold_at, at_op = 0.5, ">"
    else:
        threshold_at, at_op = None, ""
    if threshold_at is not None:
        try:
            af = float(at) if at is not None else None
            ok = (af is not None and af >= threshold_at) if at_op == ">=" else (
                af is not None and af > threshold_at
            )
            out.append(_check_item(
                "竞价换手",
                f"{af:.2f}%" if af is not None else "—",
                ok,
                f"(需{at_op}{threshold_at}%)",
            ))
        except Exception:
            pass

    # 4-5) 昨换手 / 成交额比（2进3 非一字：≥3% / ≥0.74 边缘、≥0.8 正常）
    if board == 2 and prev_yizi is False:
        try:
            pf = float(prev_to) if prev_to is not None else None
            out.append(_check_item(
                "昨日换手率",
                f"{pf:.2f}%" if pf is not None else "—",
                pf is not None and pf >= 3,
                "(需≥3%)",
            ))
        except Exception:
            pass
        try:
            from src.engine.screener_decision import PAR_2TO3_EDGE, PAR_2TO3_HARD

            arf = float(prev_ar) if prev_ar is not None else None
            ok = arf is not None and arf >= PAR_2TO3_EDGE
            note = f"(边缘≥{PAR_2TO3_EDGE}/正常≥{PAR_2TO3_HARD})"
            if arf is not None and PAR_2TO3_EDGE <= arf < PAR_2TO3_HARD:
                note = f"(边缘票 {arf:.2f})"
            out.append(_check_item(
                "昨日/前日成交额比",
                f"{arf:.2f}" if arf is not None else "—",
                ok,
                note,
            ))
        except Exception:
            pass
    elif board == 2 and prev_yizi:
        out.append(_check_item("二板形态", "一字板", True, "(一字板免缩量过滤)"))

    # 6) 高标红/绿盘
    if board == 2 and space_red is not None:
        out.append(_check_item(
            "高标竞价",
            "红盘" if space_red else "绿盘",
            True if space_red else (b1 is not None and float(b1) >= 12),
            "(红盘更优；绿盘需晋级率≥12%)",
        ))

    return out


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

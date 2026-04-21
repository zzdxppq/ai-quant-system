"""Kronos 预测引擎

封装 Kronos 基础模型，输入股票代码，输出5日走势预测。

模型：NeoQuasar/Kronos-small (24.7M 参数，CPU推理约5-10秒/只)
输入：最近250根日K线 OHLCV
输出：未来5日 OHLCV 预测 + 趋势判定（涨/跌/震荡）
"""
import sys
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from src.config import DATA_DIR, now_cn

# Kronos 模型路径
KRONOS_PATH = Path("/tmp/Kronos")
MODEL_NAME = "NeoQuasar/Kronos-small"
TOKENIZER_NAME = "NeoQuasar/Kronos-Tokenizer-base"

# 预测参数
LOOKBACK = 250       # 历史K线根数
PRED_LEN = 5         # 预测未来天数
SAMPLE_COUNT = 3     # 集成预测次数
TEMPERATURE = 0.8    # 采样温度
TOP_P = 0.9          # 核采样

# 全局模型实例（懒加载）
_predictor = None
_load_lock = None


def _get_predictor():
    """懒加载 Kronos 模型（首次调用时加载，约10秒）"""
    global _predictor, _load_lock
    import threading

    if _load_lock is None:
        _load_lock = threading.Lock()

    if _predictor is not None:
        return _predictor

    with _load_lock:
        if _predictor is not None:
            return _predictor

        print("[Kronos] 加载模型...")

        # 添加 Kronos 到 path
        if str(KRONOS_PATH) not in sys.path:
            sys.path.insert(0, str(KRONOS_PATH))

        try:
            from model import Kronos, KronosTokenizer, KronosPredictor

            tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_NAME)
            model = Kronos.from_pretrained(MODEL_NAME)
            _predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
            print("[Kronos] 模型加载完成 (CPU)")
        except Exception as e:
            print(f"[Kronos] 模型加载失败: {e}")
            raise

    return _predictor


def predict_stock(code: str) -> Optional[dict]:
    """预测单只股票未来5日走势

    Args:
        code: 股票代码

    Returns:
        {
            "code": "002217",
            "name": "合力泰",
            "predicted_at": "2026-04-21 15:35:00",
            "current_close": 2.85,
            "predictions": [
                {"date": "2026-04-22", "open": x, "high": x, "low": x, "close": x},
                ...
            ],
            "pred_gain": 5.2,        # 预测5日涨幅%
            "trend": "涨",            # 涨/跌/震荡
            "confidence": "中",       # 高/中/低
        }
    """
    # 1. 获取历史K线
    kline_df = _get_history_kline(code)
    if kline_df is None or len(kline_df) < 60:
        print(f"[Kronos] {code} K线不足，跳过")
        return None

    # 2. 获取股票名称
    name = _get_stock_name(code)

    # 3. 准备输入数据
    df_input = kline_df[["open", "high", "low", "close", "volume", "amount"]].copy()
    # 截取最近 LOOKBACK 根
    if len(df_input) > LOOKBACK:
        df_input = df_input.iloc[-LOOKBACK:]

    # 构造时间戳
    x_timestamps = pd.to_datetime(kline_df["date"].iloc[-len(df_input):])
    # 未来时间戳（跳过周末）
    last_date = x_timestamps.iloc[-1]
    y_timestamps = pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=PRED_LEN)

    # 4. 运行预测
    try:
        predictor = _get_predictor()
        # Kronos 需要 pd.Series（有 .dt accessor），不能直接用 DatetimeIndex
        x_ts_series = pd.Series(x_timestamps.values).reset_index(drop=True)
        y_ts_series = pd.Series(y_timestamps.values)
        pred_df = predictor.predict(
            df=df_input.reset_index(drop=True),
            x_timestamp=x_ts_series,
            y_timestamp=y_ts_series,
            pred_len=PRED_LEN,
            T=TEMPERATURE,
            top_p=TOP_P,
            sample_count=SAMPLE_COUNT,
            verbose=False,
        )
    except Exception as e:
        print(f"[Kronos] {code} 预测异常: {e}")
        return None

    # 5. 解析结果（加涨跌停约束）
    current_close = float(df_input.iloc[-1]["close"])
    # A股涨跌停限制
    code_str = str(code)
    if code_str.startswith(("300", "301", "688")):
        limit_pct = 0.20  # 创业板/科创板 20%
    else:
        limit_pct = 0.10  # 主板 10%

    predictions = []
    prev_close = current_close  # 用前一天收盘做涨跌停基准
    for i, ts in enumerate(y_timestamps):
        row = pred_df.iloc[i] if i < len(pred_df) else None
        if row is not None:
            # 涨跌停约束：每日变动不超过限制比例
            upper = prev_close * (1 + limit_pct)
            lower = prev_close * (1 - limit_pct)
            pred_open = max(lower, min(upper, float(row.get("open", 0))))
            pred_high = max(lower, min(upper, float(row.get("high", 0))))
            pred_low = max(lower, min(upper, float(row.get("low", 0))))
            pred_close = max(lower, min(upper, float(row.get("close", 0))))

            predictions.append({
                "date": ts.strftime("%Y-%m-%d"),
                "open": round(pred_open, 2),
                "high": round(pred_high, 2),
                "low": round(pred_low, 2),
                "close": round(pred_close, 2),
            })
            prev_close = pred_close  # 下一天基于今天收盘

    if not predictions:
        return None

    # 6. 计算趋势
    pred_close_5d = predictions[-1]["close"]
    pred_gain = (pred_close_5d / current_close - 1) * 100 if current_close > 0 else 0

    # 趋势判定
    if pred_gain >= 5:
        trend = "涨"
    elif pred_gain <= -5:
        trend = "跌"
    elif pred_gain >= 2:
        trend = "偏涨"
    elif pred_gain <= -2:
        trend = "偏跌"
    else:
        trend = "震荡"

    # 置信度（基于预测路径的一致性）
    # 简单用涨幅绝对值判断：大涨/大跌信号更明确
    abs_gain = abs(pred_gain)
    if abs_gain >= 8:
        confidence = "高"
    elif abs_gain >= 3:
        confidence = "中"
    else:
        confidence = "低"

    return {
        "code": code,
        "name": name,
        "predicted_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "current_close": round(current_close, 2),
        "predictions": predictions,
        "pred_gain": round(pred_gain, 2),
        "trend": trend,
        "confidence": confidence,
    }


def _get_history_kline(code: str) -> Optional[pd.DataFrame]:
    """获取历史日K线"""
    try:
        from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
        df = fetch_kline(code, scale=SCALE_DAILY, datalen=LOOKBACK)
        if df is not None and not df.empty:
            # 确保有 amount 列
            if "amount" not in df.columns:
                df["amount"] = df["volume"] * (df["open"] + df["close"]) / 2
            return df
    except Exception as e:
        print(f"[Kronos] {code} K线获取失败: {e}")
    return None


def _get_stock_name(code: str) -> str:
    """获取股票名称"""
    try:
        from src.data.sina_api import fetch_realtime_batch
        df = fetch_realtime_batch([code])
        if not df.empty:
            return str(df.iloc[0].get("name", ""))
    except Exception:
        pass
    return ""

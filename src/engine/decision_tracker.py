"""决策追踪模块 — 盘前记录+盘后复盘+偏差分析

盘前（9:27自动+用户手动确认）：
  记录观察池、竞价打分、开仓决策、计划仓位、止损位

盘后（15:45自动）：
  对照填写实际结果、分析偏差

数据：data/decision_records.json
"""
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

from src.config import DATA_DIR, now_cn


RECORDS_FILE = DATA_DIR / "decision_records.json"


@dataclass
class DecisionRecord:
    """每日决策记录"""
    date: str

    # 盘前部分（9:27自动填写）
    watch_pool: list = field(default_factory=list)      # 观察池
    auction_scores: list = field(default_factory=list)   # 竞价打分结果
    screener_hits: list = field(default_factory=list)    # 选股命中
    system_action: str = ""                              # 系统建议
    system_position: str = ""                            # 系统建议仓位
    system_stop_loss: float = 0                          # 系统建议止损

    # 用户部分（手动填写）
    user_action: str = ""           # 实际操作（买入/放弃/观望）
    user_code: str = ""             # 实际买入标的
    user_price: float = 0           # 买入价格
    user_position: str = ""         # 实际仓位
    user_stop_loss: float = 0       # 设定止损
    user_note: str = ""             # 操作备注

    # 盘后部分（自动填写）
    result_close: float = 0         # 买入标的收盘价
    result_gain_pct: float = 0      # 日内盈亏%
    result_is_limit_up: bool = False  # 是否涨停
    result_hit_stop: bool = False   # 是否触发止损
    next_day_open: float = 0        # 次日开盘价
    next_day_gain_pct: float = 0    # 次日涨幅%

    # 偏差分析（自动生成）
    deviation: str = ""             # 偏差分析文字
    lesson: str = ""                # 经验教训


def _load_records() -> list[dict]:
    if RECORDS_FILE.exists():
        try:
            return json.loads(RECORDS_FILE.read_text())
        except Exception:
            pass
    return []


def _save_records(records: list[dict]):
    RECORDS_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2))


def create_premarket_record(
    watch_pool: list,
    auction_scores: list,
    screener_hits: list,
) -> dict:
    """盘前自动创建当日决策记录"""
    today = now_cn().strftime("%Y-%m-%d")
    records = _load_records()

    # 去重
    if any(r["date"] == today for r in records):
        # 更新已有记录
        for r in records:
            if r["date"] == today:
                r["watch_pool"] = watch_pool
                r["auction_scores"] = auction_scores
                r["screener_hits"] = screener_hits
                # 取最佳打分的决策
                if auction_scores:
                    best = max(auction_scores, key=lambda x: x.get("total_score", 0))
                    r["system_action"] = best.get("action", "")
                    r["system_position"] = best.get("position", "")
                    r["system_stop_loss"] = best.get("stop_loss", 0)
                _save_records(records)
                return r
    else:
        rec = asdict(DecisionRecord(date=today))
        rec["watch_pool"] = watch_pool
        rec["auction_scores"] = auction_scores
        rec["screener_hits"] = screener_hits
        if auction_scores:
            best = max(auction_scores, key=lambda x: x.get("total_score", 0))
            rec["system_action"] = best.get("action", "")
            rec["system_position"] = best.get("position", "")
            rec["system_stop_loss"] = best.get("stop_loss", 0)
        records.append(rec)
        _save_records(records)
        return rec


def update_user_decision(
    date: str,
    action: str,
    code: str = "",
    price: float = 0,
    position: str = "",
    stop_loss: float = 0,
    note: str = "",
) -> dict:
    """用户手动记录实际操作"""
    records = _load_records()
    for r in records:
        if r["date"] == date:
            r["user_action"] = action
            r["user_code"] = code
            r["user_price"] = price
            r["user_position"] = position
            r["user_stop_loss"] = stop_loss
            r["user_note"] = note
            _save_records(records)
            return r
    return {"error": "未找到该日记录"}


def backfill_result(date: str):
    """盘后自动回填结果"""
    records = _load_records()
    for r in records:
        if r["date"] != date:
            continue
        code = r.get("user_code", "")
        if not code:
            # 没有实际操作，跳过
            continue

        try:
            from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
            df = fetch_kline(code, SCALE_DAILY, datalen=5)
            if df is None or df.empty:
                continue

            dates = [str(row["date"])[:10] for _, row in df.iterrows()]
            if date not in dates:
                continue

            idx = dates.index(date)
            close = float(df.iloc[idx]["close"])
            buy_price = r.get("user_price", 0)

            r["result_close"] = round(close, 2)
            if buy_price > 0:
                r["result_gain_pct"] = round((close / buy_price - 1) * 100, 2)

            # 涨停判定
            if idx > 0:
                pre = float(df.iloc[idx - 1]["close"])
                day_pct = (close / pre - 1) * 100 if pre > 0 else 0
                limit = 19.5 if code.startswith(("300", "301", "688")) else 9.8
                r["result_is_limit_up"] = day_pct >= limit

            # 是否触发止损
            low = float(df.iloc[idx]["low"])
            user_stop = r.get("user_stop_loss", 0)
            if user_stop > 0 and low <= user_stop:
                r["result_hit_stop"] = True

            # 次日数据
            if idx + 1 < len(df):
                next_open = float(df.iloc[idx + 1]["open"])
                r["next_day_open"] = round(next_open, 2)
                if close > 0:
                    r["next_day_gain_pct"] = round((next_open / close - 1) * 100, 2)

            # 偏差分析
            r["deviation"] = _analyze_deviation(r)

        except Exception as e:
            print(f"[决策追踪] {code} 回填失败: {e}")

    _save_records(records)


def _analyze_deviation(r: dict) -> str:
    """自动偏差分析"""
    parts = []
    sys_action = r.get("system_action", "")
    user_action = r.get("user_action", "")
    gain = r.get("result_gain_pct", 0)

    if sys_action and user_action:
        if sys_action == "果断开仓" and user_action == "放弃":
            if gain > 5:
                parts.append(f"系统建议开仓但你放弃了，结果涨{gain}%——可能过于保守")
            else:
                parts.append("系统建议开仓但你放弃了——回避了风险")
        elif sys_action == "放弃" and user_action == "买入":
            if gain < 0:
                parts.append(f"系统建议放弃但你买了，亏{abs(gain)}%——应信任系统否决")
            else:
                parts.append("系统建议放弃但你买了，盈利——你的判断超越了系统")

    if r.get("result_hit_stop"):
        parts.append("触发了止损位——执行纪律良好" if user_action == "买入" else "")

    return "。".join(p for p in parts if p)


def get_records(limit: int = 30) -> list[dict]:
    """获取决策记录列表"""
    records = _load_records()
    records.sort(key=lambda r: r["date"], reverse=True)
    return records[:limit]


def get_stats() -> dict:
    """统计决策表现"""
    records = _load_records()
    traded = [r for r in records if r.get("user_action") == "买入" and r.get("result_gain_pct") is not None]

    total = len(traded)
    if total == 0:
        return {"total": 0, "wins": 0, "win_rate": 0, "avg_gain": 0}

    wins = sum(1 for r in traded if r.get("result_gain_pct", 0) > 0)
    avg_gain = sum(r.get("result_gain_pct", 0) for r in traded) / total

    # 系统准确率
    sys_traded = [r for r in records if r.get("system_action") in ("果断开仓", "小仓试错")]
    sys_correct = sum(
        1 for r in sys_traded
        if (r.get("system_action") == "果断开仓" and r.get("result_gain_pct", 0) > 0)
        or (r.get("system_action") == "放弃" and r.get("result_gain_pct", 0) <= 0)
    )

    return {
        "total": total,
        "wins": wins,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        "avg_gain": round(avg_gain, 2),
        "system_signals": len(sys_traded),
        "system_accuracy": round(sys_correct / len(sys_traded) * 100, 1) if sys_traded else 0,
    }

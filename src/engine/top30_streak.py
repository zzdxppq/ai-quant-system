"""TOP30 周期计数

为 10 日涨幅榜 TOP30 中【且 gain_10d ≥ 45%】的每只个股记录"连续在榜天数"。
- 首次进入并 ≥45%：1
- 连续满足条件：每个新交易日 +1
- 离开 top30 或 gain_10d < 45%：streak=0（重置）
- 重新进入并 ≥45%：从 1 重新计数

状态文件 data/top30_streak_state.json：
{
  "today_date": "YYYY-MM-DD",   # 当前正在累计的交易日
  "today_by_code": {code: count},  # 今日已计数的个股
  "prev_by_code": {code: count}    # 上一交易日的最终计数（用于增量）
}
"""
from typing import Iterable

from src.config import DATA_DIR, now_cn
from src.data.json_io import dump_json_file, load_json_file


_STATE_FILE = DATA_DIR / "top30_streak_state.json"


def _load_state() -> dict:
    data = load_json_file(_STATE_FILE)
    if isinstance(data, dict):
        return data
    return {"today_date": "", "today_by_code": {}, "prev_by_code": {}}


def _save_state(state: dict) -> None:
    try:
        dump_json_file(_STATE_FILE, state)
    except Exception:
        pass


_GAIN_THRESHOLD = 45.0  # gain_10d 阈值：低于此值视为不满足条件，streak 重置


def stamp_streak(records: Iterable[dict]) -> None:
    """给每条 ranking 记录写入 top30_streak 字段（原地修改）。

    入榜条件：在 top30 + gain_10d ≥ 45%。
    - 满足条件：在前一日基础上 +1
    - 不满足条件（gain_10d < 45%）：streak=0
    同一交易日内多次调用幂等：已计数的个股直接读取缓存值。
    跨交易日时，自动将昨日的 today_by_code 轮转为 prev_by_code。
    """
    state = _load_state()
    today = now_cn().strftime("%Y-%m-%d")

    if state.get("today_date") != today:
        # 新交易日：将昨日 today 轮转为 prev，重置今日
        state["prev_by_code"] = state.get("today_by_code", {}) or {}
        state["today_by_code"] = {}
        state["today_date"] = today

    today_map: dict = state["today_by_code"]
    prev_map: dict = state["prev_by_code"]

    for r in records:
        code = str(r.get("code", "") or "")
        if not code:
            continue
        try:
            gain = float(r.get("gain_10d") or 0)
        except (TypeError, ValueError):
            gain = 0.0

        # gain 检查优先于 cache —— 即便今日已计数过，若当前 gain 跌破阈值也要归零
        if gain < _GAIN_THRESHOLD:
            streak = 0
            today_map[code] = streak
        elif code in today_map:
            streak = today_map[code]
        else:
            streak = int(prev_map.get(code, 0)) + 1
            today_map[code] = streak
        r["top30_streak"] = streak

    _save_state(state)

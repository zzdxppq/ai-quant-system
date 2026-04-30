"""TOP30 周期计数

为 10 日涨幅榜 TOP30 中的每只个股记录"连续在榜天数"。
- 首次进入：1
- 连续在榜：每个新交易日 +1
- 离开后再次进入：从 1 重新计数

状态文件 data/top30_streak_state.json：
{
  "today_date": "YYYY-MM-DD",   # 当前正在累计的交易日
  "today_by_code": {code: count},  # 今日已计数的个股
  "prev_by_code": {code: count}    # 上一交易日的最终计数（用于增量）
}
"""
import json
from typing import Iterable

from src.config import DATA_DIR, now_cn


_STATE_FILE = DATA_DIR / "top30_streak_state.json"


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"today_date": "", "today_by_code": {}, "prev_by_code": {}}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception:
        pass


def stamp_streak(records: Iterable[dict]) -> None:
    """给每条 ranking 记录写入 top30_streak 字段（原地修改）。

    同一交易日内多次调用幂等：已计数的个股直接读取缓存值；新进入的个股
    基于上一交易日 prev_by_code 加 1 计数（不在则为 1）。
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
        if code in today_map:
            streak = today_map[code]
        else:
            streak = int(prev_map.get(code, 0)) + 1
            today_map[code] = streak
        r["top30_streak"] = streak

    _save_state(state)

"""周期状态机引擎

核心逻辑：
1. 计算全市场10日涨幅排行
2. 跟踪所有 >40% 标的的持续天数
3. 自动判定周期状态（孕育→小周期启动→小周期完成→完整周期→余温→退潮）
4. 识别混沌期（前任没到100%就衰退，新标的接榜但也未达100%）
"""
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import pandas as pd

from src.config import CYCLE_CONFIG, DATA_DIR, now_cn
from src.data.json_io import dump_json_file, load_json_file


class CyclePhase(str, Enum):
    """周期阶段"""
    BREEDING = "孕育期"           # 无标的>45%
    SMALL_CYCLE_START = "小周期启动"  # 有标的>45%且≥3天
    CHAOS = "混沌期"              # 前任没到100%就衰退，新标的接榜但也未达100%
    SMALL_CYCLE_DONE = "小周期完成"   # 标的>100%且首次站榜首
    FULL_CYCLE = "完整周期"        # 榜首≥3天，涨幅>120%
    AFTERGLOW = "余温期"          # 代表股涨速放缓
    EBB = "退潮期"                # 代表股跌出榜首


@dataclass
class TrackedStock:
    """被跟踪的标的"""
    code: str
    name: str
    gain_10d: float            # 当前10日涨幅
    sustain_days: int = 0      # 持续>阈值的天数
    peak_gain: float = 0.0     # 历史峰值涨幅
    top_days: int = 0          # 站榜首天数
    first_top_date: str = ""   # 首次站榜首日期
    is_main_board: bool = True # 是否主板


@dataclass
class CycleSnapshot:
    """周期快照（某一天的完整状态）"""
    date: str
    phase: str                           # 当前阶段
    phase_day: int = 0                   # 阶段持续天数（每交易日 run_cycle_update +1）
    phase_entered_date: str = ""         # 进入当前阶段的日期 YYYY-MM-DD
    representative: Optional[dict] = None  # 代表股信息
    candidates: list = field(default_factory=list)  # 候选池（所有被跟踪标的）
    prev_cycle: Optional[dict] = None    # 上轮周期信息
    notes: str = ""


class CycleEngine:
    """周期状态机"""

    def __init__(self):
        self.cfg = CYCLE_CONFIG
        self.state_file = DATA_DIR / "cycle_state.json"

        # 当前状态
        self.phase = CyclePhase.BREEDING
        self.phase_day = 0
        self.phase_entered_date: str = ""
        self.tracked: dict[str, TrackedStock] = {}  # code -> TrackedStock
        self.representative: Optional[TrackedStock] = None
        self.prev_cycle: Optional[dict] = None  # 上轮周期信息

        # 加载持久化状态
        self._load_state()

    def update(self, gain_ranking: pd.DataFrame) -> CycleSnapshot:
        """每日更新周期状态

        Args:
            gain_ranking: 10日涨幅排行 DataFrame，需要列：
                - code: 股票代码
                - name: 名称
                - gain_10d: 10日涨幅(%)
                - is_main_board: 是否主板（可选）

        Returns:
            CycleSnapshot 当日周期快照
        """
        today = now_cn().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 更新跟踪池
        self._update_tracked(gain_ranking)

        # 2. 状态转换
        self._transition()

        # 2b. 转换后清理：代表股已被剔除但仍残留在 self.representative 的情况
        #     （主要发生在 EBB 阶段 rep 掉出跟踪池、但状态机未走 _enter_ebb 的残留路径）
        if self.representative and self.representative.code not in self.tracked:
            if not self.prev_cycle:
                self.prev_cycle = {
                    "code": self.representative.code,
                    "name": self.representative.name,
                    "peak_gain": self.representative.peak_gain,
                    "phase": self.phase.value,
                }
            self.representative = None

        # 3. 生成快照
        snapshot = self._build_snapshot(today)

        # 4. 持久化
        self._save_state()

        return snapshot

    def _update_tracked(self, df: pd.DataFrame):
        """更新跟踪池中所有标的的状态"""
        if df.empty:
            return

        # 按10日涨幅降序排列
        df = df.sort_values("gain_10d", ascending=False).reset_index(drop=True)

        # 当前榜首
        top_code = df.iloc[0]["code"] if len(df) > 0 else None
        top_gain = df.iloc[0]["gain_10d"] if len(df) > 0 else 0

        # 标记已有跟踪股是否还在榜上
        current_codes = set(df["code"].tolist())
        for code in list(self.tracked.keys()):
            if code not in current_codes:
                # 跌出跟踪范围，检查是否还>阈值
                stock = self.tracked[code]
                stock.gain_10d = 0  # 假设跌出了
                stock.sustain_days = 0

        # 更新所有 >= tracking_threshold 的标的
        threshold = self.cfg["tracking_threshold"]
        for _, row in df.iterrows():
            code = str(row["code"])
            gain = float(row["gain_10d"])
            name = str(row.get("name", ""))
            is_main = bool(row.get("is_main_board", True))

            if gain < threshold:
                # 低于跟踪阈值，移除
                self.tracked.pop(code, None)
                continue

            if code in self.tracked:
                stock = self.tracked[code]
                stock.gain_10d = gain
                stock.name = name
                stock.peak_gain = max(stock.peak_gain, gain)

                # 持续天数：只要 > 启动阈值就累加
                if gain >= self.cfg["gain_threshold_start"]:
                    stock.sustain_days += 1
                else:
                    stock.sustain_days = 0

                # 榜首天数
                if code == top_code:
                    stock.top_days += 1
                    if not stock.first_top_date:
                        stock.first_top_date = now_cn().strftime("%Y-%m-%d")
                # 非榜首时不重置top_days（可能暂时被超过后又回来）

            else:
                # 新进入跟踪池
                stock = TrackedStock(
                    code=code,
                    name=name,
                    gain_10d=gain,
                    sustain_days=1 if gain >= self.cfg["gain_threshold_start"] else 0,
                    peak_gain=gain,
                    top_days=1 if code == top_code else 0,
                    first_top_date=now_cn().strftime("%Y-%m-%d") if code == top_code else "",
                    is_main_board=is_main,
                )
                self.tracked[code] = stock

        # 清理低于跟踪阈值的
        self.tracked = {
            k: v for k, v in self.tracked.items()
            if v.gain_10d >= threshold
        }

    def _transition(self):
        """状态机转换逻辑"""
        cfg = self.cfg

        # 获取当前关键指标
        candidates_above_start = [
            s for s in self.tracked.values()
            if s.gain_10d >= cfg["gain_threshold_start"]
            and s.sustain_days >= cfg["sustain_days_start"]
        ]

        top_stock = self._get_top_stock()

        # === 状态转换规则 ===

        # 规则1：无标的 > 45% → 孕育期
        any_above_start = any(
            s.gain_10d >= cfg["gain_threshold_start"]
            for s in self.tracked.values()
        )
        if not any_above_start:
            if self.phase not in (CyclePhase.BREEDING, CyclePhase.EBB):
                self._enter_ebb()
            elif self.phase == CyclePhase.EBB:
                self.phase_day += 1
                # 退潮期持续一段时间后进入孕育期
                if self.phase_day >= 3:
                    self._enter_breeding()
            else:
                self.phase_day += 1
            return

        # 规则2：有标的>45%但持续<3天 → 仍在孕育
        if not candidates_above_start and self.phase == CyclePhase.BREEDING:
            self.phase_day += 1
            return

        # 规则3：完整周期 → 检查是否进入余温/退潮
        if self.phase == CyclePhase.FULL_CYCLE:
            if self.representative:
                rep = self.tracked.get(self.representative.code)
                if rep is None or rep.gain_10d < rep.peak_gain * 0.9:
                    # 涨速放缓或跌出，进入余温
                    self._enter_afterglow()
                else:
                    self.phase_day += 1
            return

        # 规则4：余温期 → 检查是否退潮
        if self.phase == CyclePhase.AFTERGLOW:
            if not self.representative:
                # 代表股已谢幕（跌出跟踪池）但阶段仍为余温：仍按交易日累加天数
                self.phase_day += 1
                return
            rep = self.tracked.get(self.representative.code)
            if rep is None:
                # 仍在 self.representative 但已跌出跟踪池：继续余温计时，不立刻转退潮
                self.phase_day += 1
                return
            if top_stock and top_stock.code != self.representative.code:
                # 新龙接棒 → 退潮
                self._enter_ebb()
            else:
                self.phase_day += 1
            return

        # 规则5：退潮期 → 检查新周期种子
        if self.phase == CyclePhase.EBB:
            if candidates_above_start:
                self._enter_small_cycle_start(candidates_above_start)
            else:
                self.phase_day += 1
            return

        # 规则6：小周期完成 → 检查是否达到完整周期
        if self.phase == CyclePhase.SMALL_CYCLE_DONE:
            if self.representative:
                rep = self.tracked.get(self.representative.code)
                if rep and rep.gain_10d >= cfg["gain_threshold_full"] and rep.top_days >= cfg["sustain_days_full"]:
                    self._enter_full_cycle()
                elif rep and rep.gain_10d >= cfg["gain_threshold_complete"]:
                    self.phase_day += 1
                else:
                    # 从小周期完成回落 → 混沌
                    self._enter_chaos()
            return

        # 规则7：小周期启动 → 检查是否有标的达到100%站榜首
        if self.phase == CyclePhase.SMALL_CYCLE_START:
            if top_stock and top_stock.gain_10d >= cfg["gain_threshold_complete"]:
                self._enter_small_cycle_done(top_stock)
            elif candidates_above_start:
                self.phase_day += 1
            else:
                # 候选全部回落 → 混沌或孕育
                self._enter_chaos()
            return

        # 规则8：混沌期 → 等待新种子
        if self.phase == CyclePhase.CHAOS:
            if top_stock and top_stock.gain_10d >= cfg["gain_threshold_complete"]:
                self._enter_small_cycle_done(top_stock)
            elif candidates_above_start:
                self._enter_small_cycle_start(candidates_above_start)
            else:
                self.phase_day += 1
            return

        # 规则9：孕育期 → 检查是否有标的持续>45%达3天
        if self.phase == CyclePhase.BREEDING:
            if candidates_above_start:
                self._enter_small_cycle_start(candidates_above_start)
            else:
                self.phase_day += 1
            return

    def _get_top_stock(self) -> Optional[TrackedStock]:
        """获取当前涨幅最高的被跟踪标的"""
        if not self.tracked:
            return None
        return max(self.tracked.values(), key=lambda s: s.gain_10d)

    # === 状态进入方法 ===

    def _mark_phase_entered(self):
        self.phase_entered_date = now_cn().strftime("%Y-%m-%d")

    def _enter_breeding(self):
        self.phase = CyclePhase.BREEDING
        self.phase_day = 1
        self._mark_phase_entered()
        self.representative = None

    def _enter_small_cycle_start(self, candidates: list[TrackedStock]):
        # 记录上轮周期（如果有代表股）
        if self.representative and self.phase in (CyclePhase.EBB, CyclePhase.AFTERGLOW):
            self.prev_cycle = {
                "code": self.representative.code,
                "name": self.representative.name,
                "peak_gain": self.representative.peak_gain,
                "phase": self.phase.value,
            }

        self.phase = CyclePhase.SMALL_CYCLE_START
        self.phase_day = 1
        self._mark_phase_entered()
        # 代表股 = 候选中涨幅最高的
        self.representative = max(candidates, key=lambda s: s.gain_10d)

    def _enter_small_cycle_done(self, top: TrackedStock):
        self.phase = CyclePhase.SMALL_CYCLE_DONE
        self.phase_day = 1
        self._mark_phase_entered()
        self.representative = top

    def _enter_full_cycle(self):
        self.phase = CyclePhase.FULL_CYCLE
        self.phase_day = 1
        self._mark_phase_entered()

    def _enter_afterglow(self):
        self.phase = CyclePhase.AFTERGLOW
        self.phase_day = 1
        self._mark_phase_entered()

    def _enter_ebb(self):
        # 代表股谢幕 → 归档到 prev_cycle 并清空，避免快照继续显示陈旧代表股
        if self.representative:
            self.prev_cycle = {
                "code": self.representative.code,
                "name": self.representative.name,
                "peak_gain": self.representative.peak_gain,
                "phase": CyclePhase.EBB.value,
            }
        self.phase = CyclePhase.EBB
        self.phase_day = 1
        self._mark_phase_entered()
        self.representative = None

    def _enter_chaos(self):
        self.phase = CyclePhase.CHAOS
        self.phase_day = 1
        self._mark_phase_entered()

    # === 快照与持久化 ===

    def _build_snapshot(self, today: str) -> CycleSnapshot:
        """构建当日快照"""
        rep_dict = None
        # 只在代表股仍处于跟踪池内时渲染；已被剔除的直接不显示
        if self.representative and self.representative.code in self.tracked:
            rep = self.tracked[self.representative.code]
            rep_dict = {
                "code": rep.code,
                "name": rep.name,
                "gain_10d": round(rep.gain_10d, 2),
                "sustain_days": rep.sustain_days,
                "top_days": rep.top_days,
                "peak_gain": round(rep.peak_gain, 2),
                "is_main_board": rep.is_main_board,
            }

        candidates = []
        for s in sorted(self.tracked.values(), key=lambda x: x.gain_10d, reverse=True):
            candidates.append({
                "code": s.code,
                "name": s.name,
                "gain_10d": round(s.gain_10d, 2),
                "sustain_days": s.sustain_days,
                "top_days": s.top_days,
                "is_main_board": s.is_main_board,
            })

        return CycleSnapshot(
            date=today,
            phase=self.phase.value,
            phase_day=self.phase_day,
            phase_entered_date=self.phase_entered_date or "",
            representative=rep_dict,
            candidates=candidates[:20],  # 只保留top20
            prev_cycle=self.prev_cycle,
        )

    def _save_state(self):
        """持久化状态到 JSON"""
        state = {
            "phase": self.phase.value,
            "phase_day": self.phase_day,
            "phase_entered_date": self.phase_entered_date or "",
            "representative": asdict(self.representative) if self.representative else None,
            "tracked": {k: asdict(v) for k, v in self.tracked.items()},
            "prev_cycle": self.prev_cycle,
            "updated_at": now_cn().isoformat(),
        }
        dump_json_file(self.state_file, state)

    def _load_state(self):
        """从 JSON 加载状态"""
        try:
            state = load_json_file(self.state_file)
            if state is None:
                return

            # 恢复 phase
            phase_str = state.get("phase", "孕育期")
            for p in CyclePhase:
                if p.value == phase_str:
                    self.phase = p
                    break

            self.phase_day = state.get("phase_day", 0)
            self.phase_entered_date = str(state.get("phase_entered_date") or "")
            if not self.phase_entered_date:
                raw = str(state.get("updated_at") or "")[:10]
                if len(raw) == 10:
                    self.phase_entered_date = raw

            # 恢复 tracked
            for code, data in state.get("tracked", {}).items():
                self.tracked[code] = TrackedStock(**data)

            # 恢复 representative
            rep_data = state.get("representative")
            if rep_data:
                self.representative = TrackedStock(**rep_data)

            self.prev_cycle = state.get("prev_cycle")

        except Exception as e:
            print(f"加载周期状态失败: {e}，使用默认状态")


def calc_gain_10d(df: pd.DataFrame) -> pd.DataFrame:
    """计算全市场10日涨幅排行

    Args:
        df: 需要包含列 code, name, close, pre_close 以及至少10天的历史数据
            或者直接包含最新收盘价和10天前收盘价

    Returns:
        DataFrame with columns: code, name, gain_10d, is_main_board
    """
    # 如果输入已经是聚合好的数据（有 close_now 和 close_10d_ago）
    if "close_now" in df.columns and "close_10d_ago" in df.columns:
        result = df.copy()
        result["gain_10d"] = (result["close_now"] / result["close_10d_ago"] - 1) * 100
    elif "gain_10d" in df.columns:
        result = df.copy()
    else:
        # 需要从日线数据计算
        result = _calc_from_daily(df)

    # 标记主板
    if "is_main_board" not in result.columns:
        result["is_main_board"] = result["code"].apply(_is_main_board)

    # 排序
    result = result.sort_values("gain_10d", ascending=False).reset_index(drop=True)
    result["rank"] = result.index + 1

    return result


def _calc_from_daily(df: pd.DataFrame) -> pd.DataFrame:
    """从日线数据计算10日涨幅"""
    df = df.sort_values(["code", "date"])

    # 获取每只股票最新和10天前的收盘价
    latest = df.groupby("code").tail(1).set_index("code")
    tenth = df.groupby("code").apply(
        lambda x: x.iloc[-11] if len(x) >= 11 else x.iloc[0]
    )
    if hasattr(tenth, "set_index"):
        tenth = tenth.set_index("code")

    result = pd.DataFrame({
        "code": latest.index,
        "name": latest["name"].values,
        "close_now": latest["close"].values,
        "close_10d_ago": tenth["close"].values,
    })
    result["gain_10d"] = (result["close_now"] / result["close_10d_ago"] - 1) * 100

    return result


def _is_main_board(code: str) -> bool:
    """判断是否主板（排除创业板300/301、科创板688、北交所8/4开头）"""
    code = str(code)
    if code.startswith(("300", "301")):
        return False  # 创业板
    if code.startswith("688"):
        return False  # 科创板
    if code.startswith(("8", "4")):
        return False  # 北交所
    return True

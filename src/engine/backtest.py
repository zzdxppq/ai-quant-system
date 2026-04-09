"""历史回测引擎

回测"周期识别 + 选股公式交叉"的胜率和盈亏比。

回测逻辑：
1. 逐日回放历史数据
2. 每天运行周期状态机 → 确定当前周期阶段
3. 每天运行选股引擎 → 筛选标的
4. 交叉验证 → 生成信号
5. 模拟交易 → 统计胜率/盈亏比

交易规则（简化版）：
- strong信号 → 6层仓位买入，持有至分歧位或止损
- normal信号 → 3层仓位买入
- watch → 不交易
- avoid → 不交易
- 持有期：默认3天（短线），或到达止盈/止损
- 止盈：+10%（主板一个涨停）
- 止损：-5%
"""
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import pandas as pd

from src.config import DATA_DIR
from src.engine.cycle import CycleEngine, CyclePhase


@dataclass
class Trade:
    """单笔交易记录"""
    date: str             # 买入日期
    code: str
    name: str
    signal_level: str     # strong/normal
    cycle_phase: str
    buy_price: float
    sell_price: float = 0.0
    sell_date: str = ""
    pnl_pct: float = 0.0   # 盈亏比例(%)
    hold_days: int = 0
    is_win: bool = False
    position_ratio: float = 0.0  # 仓位比例


@dataclass
class BacktestResult:
    """回测统计"""
    period: str                # 回测区间
    total_trades: int = 0
    win_trades: int = 0
    lose_trades: int = 0
    win_rate: float = 0.0     # 胜率(%)
    avg_win_pct: float = 0.0  # 平均盈利(%)
    avg_lose_pct: float = 0.0 # 平均亏损(%)
    profit_loss_ratio: float = 0.0  # 盈亏比
    max_consecutive_win: int = 0
    max_consecutive_lose: int = 0
    total_pnl_pct: float = 0.0     # 总收益率(%)
    max_drawdown_pct: float = 0.0  # 最大回撤(%)
    trades: list = field(default_factory=list)
    cycle_stats: dict = field(default_factory=dict)  # 按周期阶段统计
    daily_equity: list = field(default_factory=list)  # 每日权益曲线


class Backtester:
    """回测器"""

    def __init__(
        self,
        take_profit: float = 10.0,
        stop_loss: float = -5.0,
        max_hold_days: int = 3,
    ):
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.max_hold_days = max_hold_days

        self.engine = CycleEngine()
        self.trades: list[Trade] = []
        self.open_positions: list[dict] = []  # 未平仓持仓
        self.equity = 1000000.0  # 初始资金100万
        self.peak_equity = self.equity
        self.max_drawdown = 0.0

    def run(self, daily_data: pd.DataFrame) -> BacktestResult:
        """运行回测

        Args:
            daily_data: 全市场日线数据，需要列:
                date, code, name, open, high, low, close, pre_close,
                volume, amount, turnover, market_cap, is_limit_up

        Returns:
            BacktestResult
        """
        if daily_data.empty:
            return BacktestResult(period="无数据")

        # 按日期排序
        dates = sorted(daily_data["date"].unique())
        start_date = str(dates[0])
        end_date = str(dates[-1])

        daily_equity = []

        for i, current_date in enumerate(dates):
            date_str = str(current_date)

            # 当天全市场数据
            day_df = daily_data[daily_data["date"] == current_date]

            # 1. 更新持仓（检查止盈止损）
            self._update_positions(day_df, date_str)

            # 2. 计算10日涨幅排行
            if i >= 10:
                past_10_dates = dates[max(0, i - 10):i + 1]
                ranking = self._calc_ranking(daily_data, past_10_dates)
            else:
                ranking = pd.DataFrame()

            # 3. 更新周期状态
            if not ranking.empty:
                snapshot = self.engine.update(ranking)

                # 4. 模拟选股 + 交叉验证
                if snapshot.phase in (
                    CyclePhase.FULL_CYCLE.value,
                    CyclePhase.SMALL_CYCLE_DONE.value,
                    CyclePhase.SMALL_CYCLE_START.value,
                ):
                    self._try_open(day_df, snapshot, date_str)

            # 记录每日权益
            daily_equity.append({
                "date": date_str,
                "equity": round(self.equity, 2),
            })

            # 更新最大回撤
            if self.equity > self.peak_equity:
                self.peak_equity = self.equity
            dd = (self.peak_equity - self.equity) / self.peak_equity * 100
            if dd > self.max_drawdown:
                self.max_drawdown = dd

        # 平仓所有未结持仓
        self._close_all(daily_data, str(dates[-1]))

        # 统计结果
        return self._calc_stats(f"{start_date} ~ {end_date}", daily_equity)

    def _calc_ranking(self, all_data: pd.DataFrame, dates: list) -> pd.DataFrame:
        """计算10日涨幅排行"""
        if len(dates) < 2:
            return pd.DataFrame()

        latest_date = dates[-1]
        earliest_date = dates[0]

        latest = all_data[all_data["date"] == latest_date][["code", "name", "close"]].copy()
        earliest = all_data[all_data["date"] == earliest_date][["code", "close"]].copy()

        if latest.empty or earliest.empty:
            return pd.DataFrame()

        latest = latest.rename(columns={"close": "close_now"})
        earliest = earliest.rename(columns={"close": "close_10d_ago"})

        merged = latest.merge(earliest, on="code", how="inner")
        merged["gain_10d"] = (merged["close_now"] / merged["close_10d_ago"] - 1) * 100
        merged["is_main_board"] = merged["code"].apply(
            lambda c: not str(c).startswith(("300", "301", "688", "8", "4"))
        )

        return merged.sort_values("gain_10d", ascending=False).head(100)

    def _try_open(self, day_df: pd.DataFrame, snapshot, date_str: str):
        """尝试开仓"""
        rep = snapshot.representative
        if not rep:
            return

        # 只交易主板标的
        if not rep.get("is_main_board", True):
            return

        code = rep["code"]

        # 检查是否已有持仓
        if any(p["code"] == code for p in self.open_positions):
            return

        # 获取该股当天数据
        stock_row = day_df[day_df["code"].astype(str) == str(code)]
        if stock_row.empty:
            return

        row = stock_row.iloc[0]
        buy_price = float(row.get("open", row.get("close", 0)))
        if buy_price <= 0:
            return

        # 确定仓位
        phase = snapshot.phase
        if phase in (CyclePhase.FULL_CYCLE.value, CyclePhase.SMALL_CYCLE_DONE.value):
            position_ratio = 0.6  # 6层
            signal_level = "strong"
        else:
            position_ratio = 0.3  # 3层
            signal_level = "normal"

        position_value = self.equity * position_ratio

        self.open_positions.append({
            "code": code,
            "name": rep.get("name", ""),
            "buy_price": buy_price,
            "buy_date": date_str,
            "position_value": position_value,
            "position_ratio": position_ratio,
            "signal_level": signal_level,
            "cycle_phase": phase,
            "hold_days": 0,
        })

    def _update_positions(self, day_df: pd.DataFrame, date_str: str):
        """更新持仓，检查止盈止损"""
        closed = []

        for pos in self.open_positions:
            pos["hold_days"] += 1
            code = pos["code"]

            stock_row = day_df[day_df["code"].astype(str) == str(code)]
            if stock_row.empty:
                continue

            row = stock_row.iloc[0]
            high = float(row.get("high", 0))
            low = float(row.get("low", 0))
            close = float(row.get("close", 0))
            buy_price = pos["buy_price"]

            if buy_price <= 0:
                continue

            # 盘中最高/最低计算
            high_pct = (high / buy_price - 1) * 100
            low_pct = (low / buy_price - 1) * 100
            close_pct = (close / buy_price - 1) * 100

            sell = False
            sell_price = close
            pnl_pct = close_pct

            # 止盈
            if high_pct >= self.take_profit:
                sell = True
                sell_price = buy_price * (1 + self.take_profit / 100)
                pnl_pct = self.take_profit

            # 止损
            elif low_pct <= self.stop_loss:
                sell = True
                sell_price = buy_price * (1 + self.stop_loss / 100)
                pnl_pct = self.stop_loss

            # 超过最大持有天数
            elif pos["hold_days"] >= self.max_hold_days:
                sell = True
                sell_price = close
                pnl_pct = close_pct

            if sell:
                trade = Trade(
                    date=pos["buy_date"],
                    code=code,
                    name=pos["name"],
                    signal_level=pos["signal_level"],
                    cycle_phase=pos["cycle_phase"],
                    buy_price=round(buy_price, 2),
                    sell_price=round(sell_price, 2),
                    sell_date=date_str,
                    pnl_pct=round(pnl_pct, 2),
                    hold_days=pos["hold_days"],
                    is_win=pnl_pct > 0,
                    position_ratio=pos["position_ratio"],
                )
                self.trades.append(trade)

                # 更新权益
                self.equity += pos["position_value"] * (pnl_pct / 100)

                closed.append(pos)

        for pos in closed:
            self.open_positions.remove(pos)

    def _close_all(self, all_data: pd.DataFrame, last_date: str):
        """平仓所有未结持仓"""
        day_df = all_data[all_data["date"].astype(str) == last_date]
        self._update_positions(day_df, last_date)

        # 强制清理剩余持仓
        for pos in self.open_positions:
            stock_row = day_df[day_df["code"].astype(str) == str(pos["code"])]
            close = float(stock_row.iloc[0]["close"]) if not stock_row.empty else pos["buy_price"]
            pnl_pct = (close / pos["buy_price"] - 1) * 100 if pos["buy_price"] > 0 else 0

            self.trades.append(Trade(
                date=pos["buy_date"], code=pos["code"], name=pos["name"],
                signal_level=pos["signal_level"], cycle_phase=pos["cycle_phase"],
                buy_price=round(pos["buy_price"], 2), sell_price=round(close, 2),
                sell_date=last_date, pnl_pct=round(pnl_pct, 2),
                hold_days=pos["hold_days"], is_win=pnl_pct > 0,
                position_ratio=pos["position_ratio"],
            ))
            self.equity += pos["position_value"] * (pnl_pct / 100)

        self.open_positions.clear()

    def _calc_stats(self, period: str, daily_equity: list) -> BacktestResult:
        """计算统计指标"""
        total = len(self.trades)
        if total == 0:
            return BacktestResult(period=period, daily_equity=daily_equity)

        wins = [t for t in self.trades if t.is_win]
        losses = [t for t in self.trades if not t.is_win]

        win_rate = len(wins) / total * 100 if total > 0 else 0
        avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
        avg_lose = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
        plr = abs(avg_win / avg_lose) if avg_lose != 0 else float("inf")

        # 连胜连亏
        max_cw, max_cl, cw, cl = 0, 0, 0, 0
        for t in self.trades:
            if t.is_win:
                cw += 1; cl = 0
                max_cw = max(max_cw, cw)
            else:
                cl += 1; cw = 0
                max_cl = max(max_cl, cl)

        total_pnl = (self.equity / 1000000 - 1) * 100

        # 按周期阶段统计
        cycle_stats = {}
        for t in self.trades:
            phase = t.cycle_phase
            if phase not in cycle_stats:
                cycle_stats[phase] = {"total": 0, "wins": 0, "pnl_sum": 0}
            cycle_stats[phase]["total"] += 1
            if t.is_win:
                cycle_stats[phase]["wins"] += 1
            cycle_stats[phase]["pnl_sum"] += t.pnl_pct

        for phase, stats in cycle_stats.items():
            stats["win_rate"] = round(stats["wins"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
            stats["avg_pnl"] = round(stats["pnl_sum"] / stats["total"], 2) if stats["total"] > 0 else 0

        return BacktestResult(
            period=period,
            total_trades=total,
            win_trades=len(wins),
            lose_trades=len(losses),
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_lose_pct=round(avg_lose, 2),
            profit_loss_ratio=round(plr, 2),
            max_consecutive_win=max_cw,
            max_consecutive_lose=max_cl,
            total_pnl_pct=round(total_pnl, 2),
            max_drawdown_pct=round(self.max_drawdown, 2),
            trades=[asdict(t) for t in self.trades],
            cycle_stats=cycle_stats,
            daily_equity=daily_equity,
        )


def run_backtest_mock() -> BacktestResult:
    """使用模拟数据运行回测演示"""
    import random
    random.seed(42)

    # 生成60天模拟全市场数据
    dates = pd.date_range("2025-10-01", periods=60, freq="B")
    stocks = [
        ("600519", "贵州茅台"), ("600036", "招商银行"),
        ("601318", "中国平安"), ("000858", "五粮液"),
        ("600276", "恒瑞医药"), ("601012", "隆基绿能"),
        ("600900", "长江电力"), ("000001", "平安银行"),
        ("600000", "浦发银行"), ("601166", "兴业银行"),
    ]

    rows = []
    prices = {code: random.uniform(20, 200) for code, _ in stocks}

    for date in dates:
        for code, name in stocks:
            p = prices[code]
            change = random.uniform(-0.08, 0.10)
            close = round(p * (1 + change), 2)
            high = round(max(close, p) * (1 + random.uniform(0, 0.03)), 2)
            low = round(min(close, p) * (1 - random.uniform(0, 0.03)), 2)
            open_price = round(p * (1 + random.uniform(-0.03, 0.05)), 2)

            rows.append({
                "date": date, "code": code, "name": name,
                "open": open_price, "high": high, "low": low,
                "close": close, "pre_close": round(p, 2),
                "volume": random.randint(10000, 500000),
                "amount": random.randint(1000000, 50000000),
                "turnover": round(random.uniform(0.5, 5), 2),
                "market_cap": random.randint(500, 5000) * 1e8,
                "is_limit_up": change >= 0.095,
            })
            prices[code] = close

    df = pd.DataFrame(rows)

    bt = Backtester(take_profit=10.0, stop_loss=-5.0, max_hold_days=3)
    return bt.run(df)

"""炸板应对规则引擎

当持仓标的涨停炸板时，根据收盘跌幅给出操作建议。
次日竞价时再给出二次建议。

规则：
  当日收盘跌幅 < 3% → 持股不动，次日看竞价
  当日收盘跌幅 3-5% → 持一半，次日竞价低于-3%则出
  当日收盘跌幅 > 5% → 次日集合竞价出

次日竞价：
  高开 > 2% → 持股等涨停（反包概率高）
  平开 → 冲高到+5%出一半
  低开 < -3% → 全出
"""
from dataclasses import dataclass
from typing import Optional

from src.config import now_cn


@dataclass
class ZhabanAdvice:
    """炸板操作建议"""
    code: str
    name: str
    close_drop_pct: float       # 收盘跌幅(相对开盘)
    action: str                 # "持股" / "减半" / "次日出"
    detail: str                 # 详细建议
    next_day_rules: str         # 次日竞价规则


def evaluate_zhaban(code: str, name: str, open_price: float, close_price: float, pre_close: float) -> ZhabanAdvice:
    """评估炸板后的操作建议

    Args:
        open_price: 今日开盘价
        close_price: 今日收盘价
        pre_close: 昨收价
    """
    if open_price <= 0:
        return ZhabanAdvice(code=code, name=name, close_drop_pct=0,
                           action="数据异常", detail="无法计算", next_day_rules="")

    drop_pct = (close_price / open_price - 1) * 100
    day_change = (close_price / pre_close - 1) * 100 if pre_close > 0 else drop_pct

    if drop_pct >= -3:
        action = "持股不动"
        detail = f"收盘距开盘{drop_pct:+.1f}%，跌幅较小，持股等次日修复"
        next_day = "次日竞价>+2%→持股等反包；平开→冲高+5%出一半；低开<-3%→全出"
    elif drop_pct >= -5:
        action = "减持一半"
        detail = f"收盘距开盘{drop_pct:+.1f}%，分歧较大，减持一半控制风险"
        next_day = "次日竞价>0%→持剩余等修复；低开<-3%→全出"
    else:
        action = "次日竞价出"
        detail = f"收盘距开盘{drop_pct:+.1f}%，跌幅较大，次日集合竞价挂单出"
        next_day = "次日不论高低开，集合竞价出清"

    return ZhabanAdvice(
        code=code,
        name=name,
        close_drop_pct=round(drop_pct, 2),
        action=action,
        detail=detail,
        next_day_rules=next_day,
    )


def evaluate_next_day_auction(code: str, name: str, yesterday_close: float, today_open: float) -> dict:
    """次日竞价时给出操作建议"""
    if yesterday_close <= 0 or today_open <= 0:
        return {"action": "数据异常", "detail": ""}

    auction_pct = (today_open / yesterday_close - 1) * 100

    if auction_pct >= 2:
        return {
            "action": "持股等反包",
            "detail": f"竞价高开{auction_pct:+.1f}%，反包概率大，持股"
        }
    elif auction_pct >= 0:
        return {
            "action": "冲高出一半",
            "detail": f"竞价平开{auction_pct:+.1f}%，盘中冲高+5%出一半"
        }
    elif auction_pct >= -3:
        return {
            "action": "观察盘中",
            "detail": f"竞价小幅低开{auction_pct:+.1f}%，看盘中能否翻红，不翻红则出"
        }
    else:
        return {
            "action": "全部出清",
            "detail": f"竞价低开{auction_pct:+.1f}%，确认走弱，全出"
        }

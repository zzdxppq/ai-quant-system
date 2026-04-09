"""数据库模型"""
from sqlalchemy import Column, String, Float, Integer, Date, DateTime, Boolean, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

from src.config import DB_PATH

Base = declarative_base()


class DailyQuote(Base):
    """日线行情"""
    __tablename__ = "daily_quote"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)  # 股票代码
    name = Column(String(20))
    date = Column(Date, nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    pre_close = Column(Float)     # 昨收
    volume = Column(Float)         # 成交量(手)
    amount = Column(Float)         # 成交额(元)
    turnover = Column(Float)       # 换手率(%)
    market_cap = Column(Float)     # 流通市值(元)
    is_limit_up = Column(Boolean, default=False)  # 是否涨停

    class Config:
        unique_together = ("code", "date")


class GainRanking(Base):
    """10日涨幅排行"""
    __tablename__ = "gain_ranking"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    code = Column(String(10), nullable=False)
    name = Column(String(20))
    gain_10d = Column(Float)       # 10日涨幅(%)
    rank = Column(Integer)          # 当日排名
    sustain_days = Column(Integer, default=0)  # 持续天数(>阈值)
    is_top = Column(Boolean, default=False)    # 是否榜首


class CycleState(Base):
    """周期状态记录"""
    __tablename__ = "cycle_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    state = Column(String(20), nullable=False)        # 周期状态
    representative_code = Column(String(10))           # 代表股代码
    representative_name = Column(String(20))           # 代表股名称
    representative_gain = Column(Float)                # 代表股10日涨幅
    representative_top_days = Column(Integer, default=0)  # 代表股榜首天数
    cycle_day = Column(Integer, default=0)             # 周期第N天
    prev_cycle_code = Column(String(10))               # 上轮周期代表股
    prev_cycle_peak = Column(Float)                    # 上轮周期峰值涨幅
    notes = Column(String(200))
    updated_at = Column(DateTime, default=datetime.now)


class ScreenerResult(Base):
    """选股结果"""
    __tablename__ = "screener_result"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    code = Column(String(10), nullable=False)
    name = Column(String(20))
    continuous_limit_up = Column(Integer)    # 连板数
    auction_gain = Column(Float)             # 竞价涨幅(%)
    auction_turnover = Column(Float)         # 竞价换手率(%)
    auction_amount = Column(Float)           # 竞价金额(万元)
    market_cap = Column(Float)               # 流通市值(亿)
    volume_ratio = Column(Float)             # 量比
    matched_cycle = Column(Boolean, default=False)  # 是否匹配周期代表股
    created_at = Column(DateTime, default=datetime.now)


def get_engine():
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session():
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()

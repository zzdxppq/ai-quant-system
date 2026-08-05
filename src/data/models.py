"""数据库模型（与 quant.duckdb 同文件；DuckDB + SQLAlchemy）。"""
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    PrimaryKeyConstraint,
    String,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

Base = declarative_base()


class DailyQuote(Base):
    """日线行情"""

    __tablename__ = "daily_quote"
    __table_args__ = (PrimaryKeyConstraint("stock_code", "trade_date"),)

    stock_code = Column(String(10), nullable=False)
    trade_date = Column(Date, nullable=False)
    stock_name = Column(String(40))
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    pre_close = Column(Float)
    volume = Column(Float)
    amount = Column(Float)
    turnover = Column(Float)
    market_cap = Column(Float)
    is_limit_up = Column(Boolean, default=False)


class GainRanking(Base):
    """10日涨幅排行"""

    __tablename__ = "gain_ranking"
    __table_args__ = (PrimaryKeyConstraint("trade_date", "stock_code"),)

    trade_date = Column(Date, nullable=False)
    rank_pos = Column(Integer, nullable=False)
    stock_code = Column(String(10), nullable=False)
    stock_name = Column(String(40))
    gain_10d = Column(Float)
    sustain_days = Column(Integer, default=0)
    is_top = Column(Boolean, default=False)


class CycleState(Base):
    """周期状态记录（按交易日一行）"""

    __tablename__ = "cycle_state"

    trade_date = Column(Date, primary_key=True)
    state = Column(String(20), nullable=False)
    representative_code = Column(String(10))
    representative_name = Column(String(40))
    representative_gain = Column(Float)
    representative_top_days = Column(Integer, default=0)
    cycle_day = Column(Integer, default=0)
    prev_cycle_code = Column(String(10))
    prev_cycle_peak = Column(Float)
    notes = Column(String(200))
    updated_at = Column(DateTime, default=datetime.now)


def get_engine():
    from src.data.quant_db import get_sqlalchemy_engine

    return get_sqlalchemy_engine()


def init_db():
    """先建 relational / structured / analytics / ledger 表，再建 ORM 表（共用单 DuckDB 连接）。"""
    from src.data.relational_sqlite import init_schema as _init_rel_schema

    _init_rel_schema()
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


_SESSION_FACTORY = None


def get_session():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(bind=get_engine())
    return _SESSION_FACTORY()

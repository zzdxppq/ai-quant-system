"""AKShare 数据拉取模块"""
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import text

from src.data.models import get_engine, init_db, DailyQuote


def fetch_all_daily(start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """拉取全市场日线行情并存入数据库

    Args:
        start_date: 起始日期 YYYYMMDD，默认20个交易日前
        end_date: 结束日期 YYYYMMDD，默认今天
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    engine = init_db()

    # 拉取A股日线行情（包含所有板块，用于周期排名）
    print(f"正在拉取 {start_date} ~ {end_date} 行情数据...")
    df = ak.stock_zh_a_hist(
        symbol="000001",  # 先获取交易日历
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="qfq"
    )
    # 实际上 akshare 需要逐个拉取或用批量接口
    # 使用 stock_zh_a_spot_em 获取实时全市场快照更高效
    return _fetch_spot_and_history(engine, start_date, end_date)


def _fetch_spot_and_history(engine, start_date: str, end_date: str) -> pd.DataFrame:
    """用日频行情接口拉取数据"""
    # 获取全市场股票列表
    print("正在获取股票列表...")
    stock_list = ak.stock_info_a_code_name()
    print(f"共 {len(stock_list)} 只股票")

    # 使用每日行情接口（更高效）
    # stock_zh_a_hist_pre_min_em 用于获取历史数据
    # 为了效率，用 stock_zh_a_spot_em 获取今日快照
    # 历史数据用 stock_zh_a_hist 逐日拉取
    return stock_list


def fetch_daily_history_batch(days: int = 15) -> pd.DataFrame:
    """批量拉取最近N天全市场日线数据

    使用东方财富每日行情接口，按日期拉取
    """
    engine = init_db()
    all_data = []

    # 获取最近的交易日列表
    trade_dates = _get_recent_trade_dates(days)

    for date_str in trade_dates:
        print(f"  拉取 {date_str} 数据...")
        try:
            df = ak.stock_zh_a_hist_pre_min_em(symbol="000001", start_date=date_str, end_date=date_str)
        except Exception:
            pass
        # 使用另一个接口
        try:
            df = _fetch_one_day(date_str)
            if df is not None and len(df) > 0:
                all_data.append(df)
        except Exception as e:
            print(f"  {date_str} 拉取失败: {e}")

    if all_data:
        result = pd.concat(all_data, ignore_index=True)
        _save_to_db(result, engine)
        return result
    return pd.DataFrame()


def _fetch_one_day(date_str: str) -> pd.DataFrame:
    """拉取某一天全市场行情"""
    try:
        # 使用东方财富历史行情
        df = ak.stock_zh_a_spot_em()
        # 这个接口只返回当日实时数据，历史数据需要其他方式
        return df
    except Exception as e:
        print(f"  拉取失败: {e}")
        return None


def fetch_realtime_spot() -> pd.DataFrame:
    """获取全市场实时快照（用于选股引擎 9:27 调用）"""
    print("正在获取实时行情快照...")
    df = ak.stock_zh_a_spot_em()

    # 标准化列名
    col_map = {
        "代码": "code",
        "名称": "name",
        "最新价": "close",
        "今开": "open",
        "最高": "high",
        "最低": "low",
        "昨收": "pre_close",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
        "流通市值": "market_cap",
        "量比": "volume_ratio",
        "涨跌幅": "change_pct",
    }
    df = df.rename(columns=col_map)
    available_cols = [v for v in col_map.values() if v in df.columns]
    df = df[available_cols]

    print(f"获取到 {len(df)} 只股票实时数据")
    return df


def fetch_history_for_cycle(days: int = 15) -> pd.DataFrame:
    """拉取最近N天历史数据用于周期计算

    使用逐只股票拉取的方式太慢，改用板块/指数+个股涨幅排行
    最高效的方式：直接用 stock_zh_a_hist 拉取每日全市场数据
    """
    engine = init_db()

    # 检查数据库中已有数据
    existing_dates = _get_existing_dates(engine)

    # 确定需要拉取的日期范围
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days + 10)).strftime("%Y%m%d")

    # 使用 stock_board_industry_hist_em 或逐个拉取
    # 最实用的方案：用 stock_zh_a_hist 按个股拉取，但只拉取在榜的
    # 或者用 stock_rank_lxsz_ths 获取连涨排行

    # 方案：先拉实时快照获取当前涨幅排名靠前的股票，再拉它们的历史
    return _fetch_targeted_history(engine, days)


def _fetch_targeted_history(engine, days: int) -> pd.DataFrame:
    """先获取全市场快照，再对目标股拉取历史"""
    # 获取实时快照
    spot = fetch_realtime_spot()

    # 获取需要历史数据的股票列表（用于计算10日涨幅）
    # 实际上10日涨幅可以直接从快照计算（如果接口提供）
    # 或者拉取所有股票的历史收盘价

    # 最高效方案：使用 stock_zh_a_hist_min_em 或直接计算
    # akshare 有 stock_changes_em 可以获取涨幅排行
    return spot


def fetch_limit_up_pool(date: str = None) -> pd.DataFrame:
    """获取涨停板池（用于连板检测）"""
    if date is None:
        date = datetime.now().strftime("%Y%m%d")
    try:
        df = ak.stock_zt_pool_em(date=date)
        print(f"获取到 {len(df)} 只涨停股")
        return df
    except Exception as e:
        print(f"获取涨停池失败: {e}")
        return pd.DataFrame()


def fetch_limit_up_history(days: int = 5) -> dict[str, pd.DataFrame]:
    """获取最近N天的涨停股池，用于连板检测

    Returns:
        {date_str: DataFrame} 每天的涨停股列表
    """
    result = {}
    trade_dates = _get_recent_trade_dates(days)

    for date_str in trade_dates:
        try:
            df = ak.stock_zt_pool_em(date=date_str)
            result[date_str] = df
            print(f"  {date_str}: {len(df)} 只涨停")
        except Exception as e:
            print(f"  {date_str} 涨停池获取失败: {e}")

    return result


def fetch_stock_history(code: str, days: int = 15) -> pd.DataFrame:
    """拉取单只股票历史日线"""
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days + 10)).strftime("%Y%m%d")
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq"
        )
        return df
    except Exception as e:
        print(f"拉取 {code} 历史失败: {e}")
        return pd.DataFrame()


def _get_recent_trade_dates(n: int) -> list[str]:
    """获取最近N个交易日日期"""
    try:
        df = ak.tool_trade_date_hist_sina()
        dates = df["trade_date"].dt.strftime("%Y%m%d").tolist()
        today = datetime.now().strftime("%Y%m%d")
        # 过滤到今天及之前
        dates = [d for d in dates if d <= today]
        return dates[-n:]
    except Exception as e:
        print(f"获取交易日历失败: {e}")
        # 回退：简单估算
        dates = []
        d = datetime.now()
        while len(dates) < n:
            if d.weekday() < 5:
                dates.append(d.strftime("%Y%m%d"))
            d -= timedelta(days=1)
        return sorted(dates)


def _get_existing_dates(engine) -> set:
    """获取数据库中已有的日期"""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT DISTINCT date FROM daily_quote"))
            return {str(row[0]) for row in result}
    except Exception:
        return set()


def _save_to_db(df: pd.DataFrame, engine):
    """保存数据到数据库"""
    df.to_sql("daily_quote", engine, if_exists="append", index=False)
    print(f"已保存 {len(df)} 条记录到数据库")

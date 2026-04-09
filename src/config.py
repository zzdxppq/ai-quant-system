"""全局配置"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# 数据库
DB_PATH = BASE_DIR / "data" / "quant.db"

# 数据目录
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 周期引擎参数
CYCLE_CONFIG = {
    "gain_threshold_start": 45,     # 小周期启动阈值（10日涨幅%）
    "gain_threshold_complete": 100, # 小周期完成阈值
    "gain_threshold_full": 120,     # 完整周期阈值（天年）
    "sustain_days_start": 3,        # 小周期启动持续天数
    "sustain_days_full": 3,         # 完整周期榜首持续天数
    "tracking_threshold": 40,       # 跟踪起始阈值
}

# 选股引擎参数
SCREENER_CONFIG = {
    "min_continuous_limit_up": 2,   # 最小连板数
    "auction_gain_min": 4.0,        # 竞价涨幅下限(%)
    "auction_gain_max": 7.5,        # 竞价涨幅上限(%)
    "auction_turnover_min": 0.5,    # 竞价换手率下限(%)
    "auction_amount_min": 1000,     # 竞价金额下限(万元)
    "market_cap_max": 100,          # 流通市值上限(亿)
    "volume_ratio_min": 1.0,        # 量比下限
    "exclude_st": True,
    "exclude_kcb": True,            # 排除科创板(688)
    "exclude_cyb": True,            # 排除创业板(300/301)
    "exclude_bse": True,            # 排除北交所(8/4开头)
}

# 选股执行时间
SCREENER_CRON_HOUR = 9
SCREENER_CRON_MINUTE = 27

# API
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

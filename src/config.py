"""全局配置"""
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ_CN = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    return datetime.now(TZ_CN)

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv():
    """轻量 .env 加载器。只在 key 未显式 export 时注入，避免 shell 环境被覆盖"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv()

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
# 对齐通达信公式 docs/选股公式.md
#   XG := LIANBAN AND PD1 AND PD2 AND PD3 AND PD4 AND PD5 AND VAR5
#         AND DYNAINFO(17)>1 AND 竞换手>0.5 AND 竞价涨幅<7.5
# 其中：
#   JJL := DYNAINFO(15)/DYNAINFO(4)/100   → 竞价成交量(手)
#   PD1 := JJL > 1000                      → 成交量 > 1000 手 = 10 万股
#   PD2 := JJL/CAPITAL*100 > 0.5           → 竞价换手率 > 0.5%
#   PD3 := ZFF > 4 AND ZFF < 7.5           → 竞价涨幅 4%~7.5%
#   PD4 := CAPITAL*C*100/1e8 < 100         → 流通市值 < 100 亿
SCREENER_CONFIG = {
    "min_continuous_limit_up": 2,       # 最小连板数 (LIANBAN)
    "auction_gain_min": 4.0,            # 竞价涨幅下限% (PD3)
    "auction_gain_max": 7.5,            # 竞价涨幅上限% (PD3)
    "auction_turnover_min": 0.5,        # 竞价换手率下限% (PD2)
    "auction_volume_lots_min": 1000,    # 竞价成交量下限(手), PD1; 1 手=100 股
    "market_cap_min": 20,               # 流通市值下限(亿) (PD4: 流通市值>20)
    "market_cap_max": 100,              # 流通市值上限(亿) (PD4: 流通市值<100)
    "volume_ratio_min": 0.9,            # 量比下限 (DYNAINFO(17)>1, 留0.1容差补偿数据源差异)
    "exclude_st": True,
    "exclude_kcb": True,                # 排除科创板(688)
    "exclude_cyb": True,                # 排除创业板(300/301)
    "exclude_bse": True,                # 排除北交所(8/4开头)
}

# 选股执行时间
SCREENER_CRON_HOUR = 9
SCREENER_CRON_MINUTE = 27

# API
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

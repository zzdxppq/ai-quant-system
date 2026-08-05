"""全局配置"""
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ_CN = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    return datetime.now(TZ_CN)


def is_trading_day(d: datetime | None = None) -> bool:
    """A 股交易日判定

    A 股市场：
      · 周六、周日恒定休市（即使是调休补班日也不开盘）
      · 法定节假日休市（含正常工作日落在节假日时）

    判定逻辑：
      1. 周末 → 非交易日
      2. 工作日且 chinese_calendar 标记为节假日（如周三元旦）→ 非交易日
      3. 其他工作日 → 交易日
    """
    d = d or now_cn()
    target = d.date() if hasattr(d, "date") else d
    if target.weekday() >= 5:  # Sat / Sun 一律休市
        return False
    try:
        import chinese_calendar as cc
        return not cc.is_holiday(target)
    except (ImportError, NotImplementedError):
        return True  # 工作日 + 库不可用 → 默认开市

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv():
    """轻量 .env 加载器。只在 key 未显式 export 时注入，避免 shell 环境被覆盖"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key == "DATA_STORAGE_BACKEND":
                # 由 _apply_data_storage_backend_from_env_file 统一写入 os.environ（可覆盖宿主机残留）
                continue
            if key == "DATABASE_ENGINE":
                continue
            if key == "SKIP_JSON_DOC_REGISTRY":
                continue
            os.environ.setdefault(key, val.strip().strip('"').strip("'"))
    except Exception:
        pass


def _apply_data_storage_backend_from_env_file() -> None:
    """业务 JSON 仅走 quant 库；固定为 quant，覆盖宿主机残留的旧值（json/sqlite/dual）。"""
    os.environ["DATA_STORAGE_BACKEND"] = "quant"


def _skip_json_doc_registry_from_env_file() -> bool | None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return None
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() != "SKIP_JSON_DOC_REGISTRY":
                continue
            v = val.strip().strip('"').strip("'").lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
    except Exception:
        pass
    return None


def _apply_skip_json_doc_registry_from_env_file() -> None:
    v = _skip_json_doc_registry_from_env_file()
    if v is not None:
        os.environ["SKIP_JSON_DOC_REGISTRY"] = "1" if v else "0"


_load_dotenv()
_apply_data_storage_backend_from_env_file()
_apply_skip_json_doc_registry_from_env_file()

_sjr = os.getenv("SKIP_JSON_DOC_REGISTRY", "0").strip().lower()
SKIP_JSON_DOC_REGISTRY = _sjr in ("1", "true", "yes", "on")
# SQLAlchemy ORM、structured/analytics/ledger_doc、relational 共用 DuckDB 文件
DB_PATH = BASE_DIR / "data" / "quant.duckdb"

# 数据目录
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 应用 JSON 业务数据：仅 quant 库（DuckDB）；DATA_STORAGE_BACKEND 恒为 quant
DATA_STORAGE_BACKEND = "quant"

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
    "volume_ratio_min": 1.0,            # 量比下限 (DYNAINFO(17)>1, 自算量比不依赖第三方)
    "exclude_st": True,
    "exclude_kcb": True,                # 排除科创板(688)
    "exclude_cyb": True,                # 排除创业板(300/301)
    "exclude_bse": True,                # 排除北交所(8/4开头)
}

# 主选股 0 命中时的 1进2 兜底公式（通达信口径；有且仅有 1 只才采用）
# PD1 量>1000手；PD2 换手>0.5%；PD3 涨幅4~8%；PD4 市值<100亿；PD5 昨日涨停；
# 竞换手>0.3%；DYNAINFO(10)>1；排除 ST/688/30/北交
SCREENER_1TO2_FALLBACK_CONFIG = {
    "min_continuous_limit_up": 1,
    "max_continuous_limit_up": 1,
    "auction_gain_min": 4.0,
    "auction_gain_max": 8.0,
    "auction_turnover_min": 0.5,        # PD2；同时满足竞换手>0.3
    "auction_turnover_soft_min": 0.3,   # 竞换手下限
    "auction_volume_lots_min": 1000,
    "auction_volume_lots_soft_min": 1,  # DYNAINFO(10)>1
    "market_cap_min": 0,
    "market_cap_max": 100,
    "volume_ratio_min": 0,              # 公式无量比硬门槛
    "exclude_st": True,
    "exclude_kcb": True,
    "exclude_cyb": True,
    "exclude_bse": True,
}

# 选股执行时间
SCREENER_CRON_HOUR = 9
SCREENER_CRON_MINUTE = 27

# API
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
# 反向代理子路径（如 /quant）；留空则本地直连根路径
APP_ROOT_PATH = os.getenv("APP_ROOT_PATH", "").strip().rstrip("/")

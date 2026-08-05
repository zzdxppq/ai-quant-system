"""选股引擎 — Python版通达信公式

执行时间：每日 9:26（竞价结束后），见 config.SCREENER_CRON_*。

筛选顺序（与 run_screener 一致；默认阈值见 SCREENER_CONFIG）：
0. 连板：昨日起向前连续涨停，continuous_limit_up >= min_continuous_limit_up（默认 2）
0.5. 在全市场竞价快照上筛候选；缺行情的 2板+ 标的再定向 batch 补缺
1. 排除 ST / 688 / 300|301 / 8|4 北交所（可配）
2. pre_close、open 有效
3. 竞价涨幅 [auction_gain_min, auction_gain_max]（默认 4%~7.5%）
4. 竞价换手：可算且 >0 时 >= auction_turnover_min（默认 0.5%），否则放行
5. 竞价成交量（手）amount/pre_close/100 >= auction_volume_lots_min（默认 1000 手）
6. 流通市值（亿）：可算且 >0 时 (market_cap_min, market_cap_max)（默认 20~100），否则放行
7. 量比：可算且 >0 时 >= volume_ratio_min（默认 1.0）；竞价时段自算，否则第三方或估算，缺失放行
"""
import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.config import SCREENER_CONFIG, now_cn


def _safe_round(v, ndigits: int = 2) -> Optional[float]:
    """Round a numeric value safely; collapse invalid/missing inputs to None.

    Returns None for: None / NaN / ±inf / non-numeric / v <= 0.
    Rationale (Story dashboard-hits-table-display-2.4 BR-1.1): upstream batch
    quote APIs occasionally return missing market_cap as NaN; round(NaN, 2) is
    NaN, which JSON-serializes to literal `NaN` (non-standard) and surfaces in
    the dashboard as a stray '亿' unit. Collapsing to None makes downstream
    rendering decisions explicit (`hit.market_cap != null` v-if branch).
    """
    if v is None:
        return None
    # Reject str / dict / list / bool (bool is int subclass, but semantically not a quantity)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        if not math.isfinite(v):  # covers NaN, +inf, -inf
            return None
        if v <= 0:
            return None
        return round(v, ndigits)
    except (TypeError, ValueError):
        return None


@dataclass
class ScreenerHit:
    """选股命中结果"""
    code: str
    name: str
    continuous_limit_up: int    # 连板数
    open_price: float           # 开盘价（竞价价格）
    auction_gain: float         # 竞价涨幅(%)
    auction_turnover: float     # 竞价换手率(%)
    auction_amount: float       # 竞价金额(万元)
    auction_volume_lots: float  # 竞价成交量(手)
    auction_volume_ratio: float # 竞价量比（自算）
    market_cap: Optional[float] # 流通市值(亿) — None when upstream quote missing/NaN (Story 2.4)
    volume_ratio: float         # 量比
    gain_10d: float = 0         # 10日涨幅(%)
    matched_cycle: bool = False # 是否匹配周期代表股


def run_screener(
    realtime_df: pd.DataFrame,
    limit_up_history: dict[str, pd.DataFrame],
    cycle_codes: Optional[list[str]] = None,
    *,
    config: Optional[dict] = None,
) -> list[ScreenerHit]:
    """执行选股筛选

    Args:
        realtime_df: 实时行情快照（9:27时刻），需要列：
            code, name, open, pre_close, close, volume, amount,
            turnover, market_cap, volume_ratio
        limit_up_history: 最近N天涨停股池 {date_str: DataFrame}
        cycle_codes: 周期代表股/候选股代码列表，用于交叉验证
        config: 覆盖 SCREENER_CONFIG（1进2 兜底等）

    Returns:
        命中结果列表
    """
    cfg = {**SCREENER_CONFIG, **(config or {})}
    if cycle_codes is None:
        cycle_codes = []

    # Step 1: 检测连板（前2日连续涨停）
    continuous_map = _detect_continuous_limit_up(limit_up_history)

    min_boards = int(cfg["min_continuous_limit_up"])
    max_boards = cfg.get("max_continuous_limit_up")
    try:
        max_boards_i = int(max_boards) if max_boards is not None else None
    except (TypeError, ValueError):
        max_boards_i = None

    # 只保留 >= min_continuous_limit_up 的（可选上限，用于 1进2 仅首板）
    qualified_codes = {}
    for code, days in continuous_map.items():
        if days < min_boards:
            continue
        if max_boards_i is not None and days > max_boards_i:
            continue
        qualified_codes[code] = days

    if not qualified_codes:
        print(f"  [screener] 无 >= {min_boards} 连板候选"
              + (f"（且 ≤{max_boards_i}）" if max_boards_i is not None else ""))
        return []

    print(f"  [screener] 连板候选 {len(qualified_codes)} 只（昨日起向前连板）")

    # Step 1.5: 候选来自昨日涨停池；行情用外层传入的全市场竞价快照，仅补缺漏
    if realtime_df is None:
        realtime_df = pd.DataFrame()
    existing_codes: set[str] = set()
    if not realtime_df.empty and "code" in realtime_df.columns:
        existing_codes = set(realtime_df["code"].astype(str))
    missing_codes = [_norm_code6(c) for c in qualified_codes if _norm_code6(c) not in existing_codes]
    if missing_codes:
        try:
            from src.data.fetcher import fetch_realtime_batch

            patch_df = fetch_realtime_batch(missing_codes)
            if patch_df is not None and not patch_df.empty:
                realtime_df = pd.concat(
                    [realtime_df, patch_df], ignore_index=True, sort=False,
                )
                print(f"  [screener] 全量快照未命中，定向补齐 {len(patch_df)}/{len(missing_codes)} 只")
        except Exception as e:
            print(f"  [screener] 定向补齐失败: {e}")

    # Step 2: 从实时行情中筛选
    results = []
    soft_turnover = float(cfg.get("auction_turnover_soft_min") or 0)
    soft_lots = float(cfg.get("auction_volume_lots_soft_min") or 0)
    for _, row in realtime_df.iterrows():
        code = _norm_code6(row.get("code", ""))
        if not code:
            continue

        # 必须在连板名单中
        if code not in qualified_codes:
            continue

        # 排除规则
        name = str(row.get("name", ""))
        if not _pass_exclusion(code, name, cfg):
            continue

        # 计算竞价指标
        pre_close = float(row.get("pre_close", 0))
        open_price = float(row.get("open", 0))
        if pre_close <= 0 or open_price <= 0:
            continue

        auction_gain = (open_price / pre_close - 1) * 100

        # 竞价涨幅过滤
        if not (cfg["auction_gain_min"] <= auction_gain <= cfg["auction_gain_max"]):
            continue

        # 竞价换手率 — 软过滤：缺 market_cap 和 turnover 时放行
        auction_turnover = float(row.get("turnover", 0))
        market_cap_yuan = float(row.get("market_cap", 0))
        volume = float(row.get("volume", 0))

        if market_cap_yuan > 0:
            # 竞价换手率 ≈ 竞价成交额 / 流通市值
            auction_amount_yuan = float(row.get("amount", 0))
            auction_turnover_calc = auction_amount_yuan / market_cap_yuan * 100
        else:
            auction_turnover_calc = auction_turnover

        # 只在有效值下做下限检查，避免数据缺失误杀
        if auction_turnover_calc > 0 and auction_turnover_calc < cfg["auction_turnover_min"]:
            continue
        if soft_turnover > 0 and auction_turnover_calc > 0 and auction_turnover_calc < soft_turnover:
            continue

        # PD1: 竞价成交量(手) > 阈值
        # 通达信 JJL := DYNAINFO(15)/DYNAINFO(4)/100 = 成交额/昨收/100 ≈ 成交量(手)
        # 1 手 = 100 股
        if pre_close > 0:
            auction_amount_yuan = float(row.get("amount", 0))
            auction_lots = auction_amount_yuan / pre_close / 100
        else:
            auction_lots = float(row.get("volume", 0)) / 100
        if auction_lots < cfg["auction_volume_lots_min"]:
            continue
        if soft_lots > 0 and auction_lots < soft_lots:
            continue
        # 折算为万元，仅用于报告展示
        auction_amount = float(row.get("amount", 0)) / 10000

        # 流通市值（亿）— 软过滤：缺字段（market_cap<=0）则放行
        # PD4: 流通市值 > 20亿 AND 流通市值 < 100亿
        market_cap_yi = market_cap_yuan / 1e8 if market_cap_yuan > 1e6 else float(row.get("market_cap", 0))
        if market_cap_yi > 0:
            if market_cap_yi > cfg["market_cap_max"] or market_cap_yi < cfg.get("market_cap_min", 0):
                continue

        # 量比 — 自算竞价量比（对齐通达信 DYNAINFO(17)）
        # 判断是否在竞价时段（9:25~9:30），只有此时段volume是纯竞价成交量
        cur_time = now_cn()
        is_auction = cur_time.hour == 9 and cur_time.minute < 30
        volume_ratio = float(row.get("volume_ratio", 0))  # 第三方值兜底
        vr_self_calc = False
        vr_min = float(cfg.get("volume_ratio_min") or 0)

        if is_auction:
            # 竞价时段：volume就是竞价成交量，直接自算
            # 量比 = 竞价成交量(股) / (5日日均量/240)
            avg_vol_5d = _get_avg_volume_5d(code)
            if avg_vol_5d and avg_vol_5d > 0:
                auction_vol = float(row.get("volume", 0))
                if auction_vol <= 0 and pre_close > 0:
                    auction_vol = float(row.get("amount", 0)) / pre_close
                per_minute_avg = avg_vol_5d / 240
                if per_minute_avg > 0 and auction_vol > 0:
                    volume_ratio = auction_vol / per_minute_avg
                    vr_self_calc = True
        # 非竞价时段：第三方量比是累计口径，不做 PD 量比硬过滤

        if (
            vr_min > 0
            and is_auction
            and vr_self_calc
            and volume_ratio > 0
            and volume_ratio < vr_min
        ):
            continue

        # 计算竞价成交量(手)和竞价量比
        auction_vol_shares = float(row.get("volume", 0))
        if auction_vol_shares <= 0 and pre_close > 0:
            auction_vol_shares = float(row.get("amount", 0)) / pre_close
        auction_vol_lots = round(auction_vol_shares / 100, 2)

        auction_vr = volume_ratio  # 已在上面计算过
        if not is_auction:
            # 非竞价时段，用通达信公式估算：JJL/5日均量(手)/240
            avg_vol_5d_for_vr = _get_avg_volume_5d(code)
            if avg_vol_5d_for_vr and avg_vol_5d_for_vr > 0:
                auction_vr = auction_lots / (avg_vol_5d_for_vr / 100 / 240) if (avg_vol_5d_for_vr / 100 / 240) > 0 else volume_ratio

        # 通过所有筛选
        hit = ScreenerHit(
            code=code,
            name=name,
            continuous_limit_up=qualified_codes[code],
            open_price=round(open_price, 2),
            auction_gain=round(auction_gain, 2),
            auction_turnover=round(auction_turnover_calc, 2),
            auction_amount=round(auction_amount, 2),
            auction_volume_lots=auction_vol_lots,
            auction_volume_ratio=round(auction_vr, 2),
            market_cap=_safe_round(market_cap_yi),
            volume_ratio=round(volume_ratio, 2),
            gain_10d=round(float(row.get("gain_10d", 0)), 2) if row.get("gain_10d") else 0,
            matched_cycle=code in cycle_codes,
        )
        results.append(hit)

    # 按连板数降序排列
    results.sort(key=lambda x: (-x.continuous_limit_up, -x.auction_gain))
    if not results and qualified_codes:
        print(
            f"  [screener] {len(qualified_codes)} 只候选均未过竞价公式"
            f"（涨幅 {cfg['auction_gain_min']}~{cfg['auction_gain_max']}% 等）"
        )
    return results


def run_screener_1to2_fallback(
    realtime_df: pd.DataFrame,
    limit_up_history: dict[str, pd.DataFrame],
    cycle_codes: Optional[list[str]] = None,
) -> list[ScreenerHit]:
    """主选股 0 命中时的 1进2 通达信公式兜底（仅返回候选，调用方要求恰好 1 只才采用）。"""
    from src.config import SCREENER_1TO2_FALLBACK_CONFIG

    print("  [screener] 主选股 0 命中 → 执行 1进2 兜底公式")
    return run_screener(
        realtime_df,
        limit_up_history,
        cycle_codes,
        config=SCREENER_1TO2_FALLBACK_CONFIG,
    )


def _trading_limit_up_dates(limit_up_history: dict[str, pd.DataFrame]) -> list[str]:
    """降序 A 股交易日键：排除今日、周末/节假日、空池（避免缓存污染导致 0 命中）。"""
    from datetime import datetime

    from src.config import TZ_CN, is_trading_day

    today_str = now_cn().strftime("%Y%m%d")
    out: list[str] = []
    for d in sorted(limit_up_history.keys(), reverse=True):
        if d == today_str or len(d) != 8 or not d.isdigit():
            continue
        try:
            dt = datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), tzinfo=TZ_CN)
        except ValueError:
            continue
        if not is_trading_day(dt):
            continue
        df = limit_up_history.get(d)
        if df is None or df.empty:
            continue
        out.append(d)
    return out


def _detect_continuous_limit_up(limit_up_history: dict[str, pd.DataFrame]) -> dict[str, int]:
    """检测「昨日起向前」的连续涨停天数（严格对齐通达信 LIANBAN 语义）

    通达信公式: LIANBAN := REF(ZT0,1) AND REF(ZT0,2)
    要求：**昨日涨停 AND 前日涨停**（今日涨停状态不参与判定）

    算法：
    1. 排除今日（最大日期如果等于当日）
    2. 从上一交易日涨停股开始，继续向前回溯（跳过周末/节假日空键）
    3. 每只股票遇到非涨停日时「封档」——streak 终结，不再递增，但保留已有计数

    Returns:
        {code: consecutive_days_prior_to_today}
        只包含昨日在涨停池的股票；值为 1 代表只昨日涨停，2 代表昨+前两日连板，依此类推
    """
    if not limit_up_history:
        return {}

    past_dates = _trading_limit_up_dates(limit_up_history)
    if not past_dates:
        return {}

    # 上一交易日涨停池
    yesterday_df = limit_up_history[past_dates[0]]
    if yesterday_df.empty:
        return {}
    col = _find_code_column(yesterday_df)
    if not col:
        return {}

    continuous: dict[str, int] = {}
    active: set[str] = set()  # 仍在连续中的 code
    for raw in yesterday_df[col].astype(str).tolist():
        code = _norm_code6(raw)
        if not code:
            continue
        continuous[code] = 1
        active.add(code)

    # 向前回溯
    for date_str in past_dates[1:]:
        if not active:
            break
        df = limit_up_history[date_str]
        if df.empty:
            break
        col = _find_code_column(df)
        if not col:
            break
        day_codes = {_norm_code6(c) for c in df[col].astype(str).tolist()}
        day_codes.discard("")

        still_active: set[str] = set()
        for code in active:
            if code in day_codes:
                continuous[code] += 1
                still_active.add(code)
            # 非涨停 → streak 终结；continuous[code] 保留已有值，不再处理
        active = still_active

    # 上一交易日池内 lbc/continuous_limit_up 优先（与复盘 _board_count_walk 一致）
    for _, row in yesterday_df.iterrows():
        code = _norm_code6(str(row[col]))
        if not code:
            continue
        lbc = _lbc_from_limit_up_row(row)
        if lbc > 0:
            continuous[code] = max(continuous.get(code, 0), lbc)

    return continuous


def _lbc_from_limit_up_row(row) -> int:
    """涨停池行上的连板数（东财/zt_pool 写入的 lbc）。"""
    for key in ("continuous_limit_up", "board_count", "lbc"):
        try:
            if hasattr(row, "index") and key not in row.index:
                continue
            v = row[key] if hasattr(row, "index") else row.get(key)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            n = int(v)
            if n > 0:
                return n
        except (TypeError, ValueError, KeyError):
            continue
    return 0


def _norm_code6(code: str) -> str:
    d = "".join(c for c in str(code) if c.isdigit())
    return d[-6:].zfill(6) if d else ""


def qualified_codes_from_history(
    limit_up_history: dict[str, pd.DataFrame],
    min_continuous: int | None = None,
) -> dict[str, int]:
    """昨日涨停池内、满足最低连板数的候选 {code: 连板数}。"""
    cfg = SCREENER_CONFIG
    need = cfg["min_continuous_limit_up"] if min_continuous is None else min_continuous
    continuous_map = _detect_continuous_limit_up(limit_up_history)
    return {
        code: days
        for code, days in continuous_map.items()
        if days >= need
    }


def collect_auction_quote_codes(
    limit_up_history: dict[str, pd.DataFrame],
    *,
    ranking_file: str | None = None,
    include_anchor_pools: bool = True,
) -> list[str]:
    """9:26 竞价行情 universe：昨日涨停池 + 高标/炸板/跌停锚定，不含全市场 spot。"""
    codes: set[str] = set()

    for code in qualified_codes_from_history(limit_up_history):
        c = _norm_code6(code)
        if c:
            codes.add(c)

    past_dates = _trading_limit_up_dates(limit_up_history)
    if past_dates:
        ydf = limit_up_history.get(past_dates[0])
        if ydf is not None and not ydf.empty:
            col = _find_code_column(ydf)
            if col:
                for raw in ydf[col].astype(str).tolist():
                    c = _norm_code6(raw)
                    if c:
                        codes.add(c)

    if ranking_file:
        try:
            from pathlib import Path

            from src.data.json_io import load_json_file

            data = load_json_file(Path(ranking_file))
            if isinstance(data, dict):
                for item in data.get("ranking") or []:
                    c = _norm_code6(item.get("code", ""))
                    if c:
                        codes.add(c)
        except Exception:
            pass

    if include_anchor_pools:
        try:
            from src.engine.sentiment_pool import get_prev_limit_down_codes

            for c in get_prev_limit_down_codes() or []:
                nc = _norm_code6(c)
                if nc:
                    codes.add(nc)
        except Exception:
            pass
        try:
            from datetime import timedelta

            from src.config import now_cn
            from src.data.zt_pool_api import fetch_zb_pool

            base = now_cn().date()
            for back in range(1, 8):
                d = (base - timedelta(days=back)).strftime("%Y%m%d")
                pool = fetch_zb_pool(d)
                if pool:
                    for code in pool.keys():
                        nc = _norm_code6(code)
                        if nc:
                            codes.add(nc)
                    break
        except Exception:
            pass
        try:
            from src.data.zt_pool_api import fetch_zt_pool_with_retry

            pool = fetch_zt_pool_with_retry() or {}
            for code in pool.keys():
                nc = _norm_code6(code)
                if nc:
                    codes.add(nc)
        except Exception:
            pass

    return sorted(codes)


# ok_sina | ok_eastmoney | ok_tencent_universe | empty
LAST_AUCTION_SPOT_STATUS: str = "unset"


def _spot_frame_from_tencent(df: pd.DataFrame) -> pd.DataFrame:
    """腾讯 qt.gtimg 字段 → 与新浪 spot 对齐（market_cap 为元）。"""
    if df is None or df.empty:
        return pd.DataFrame()
    mc_yi = pd.to_numeric(df.get("market_cap_yi", 0), errors="coerce").fillna(0)
    out = pd.DataFrame({
        "code": df["code"].astype(str).str.zfill(6).str[-6:],
        "name": df["name"],
        "close": pd.to_numeric(df["close"], errors="coerce").fillna(0),
        "change_pct": pd.to_numeric(df["change_pct"], errors="coerce").fillna(0),
        "pre_close": pd.to_numeric(df["pre_close"], errors="coerce").fillna(0),
        "open": pd.to_numeric(df["open"], errors="coerce").fillna(0),
        "high": pd.to_numeric(df["high"], errors="coerce").fillna(0),
        "low": pd.to_numeric(df["low"], errors="coerce").fillna(0),
        "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0),
        "amount": pd.to_numeric(df["amount"], errors="coerce").fillna(0),
        "volume_ratio": pd.to_numeric(df.get("volume_ratio", 0), errors="coerce").fillna(0),
        "market_cap": mc_yi * 1e8,
        "turnover": pd.to_numeric(df.get("turnover", 0), errors="coerce").fillna(0),
        "pe": pd.to_numeric(df.get("pe", 0), errors="coerce").fillna(0),
        "amplitude": 0.0,
        "industry": "",
    })
    out.attrs["source"] = "tencent_universe"
    return out


def _fetch_auction_spot_tencent_universe(
    limit_up_history: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """新浪/东财全量失败时：对选股 universe 批量拉腾讯行情。"""
    from src.config import DATA_DIR
    from src.data.tencent_api import fetch_stock_details

    codes = collect_auction_quote_codes(
        limit_up_history,
        ranking_file=str(DATA_DIR / "latest_ranking.json"),
        include_anchor_pools=True,
    )
    if not codes:
        print("[竞价全量] 腾讯 universe：无候选代码")
        return pd.DataFrame()
    raw = fetch_stock_details(codes)
    out = _spot_frame_from_tencent(raw)
    if out.empty:
        print(f"[竞价全量] 腾讯 universe 失败 universe={len(codes)}")
        return out
    print(f"[竞价全量] 腾讯 universe {len(out)}/{len(codes)} 只（定向兜底，非全市场）")
    return out


def fetch_auction_spot_full(
    *,
    min_samples: int = 1000,
    limit_up_history: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """9:26 拉一次全市场当日竞价快照（新鲜数据，供一字/跌停/昨日涨停均价等共用）。

    不用 fetch_realtime_spot（东财默认涨幅前 100）；优先新浪 ~5200，失败再东财 spot_em，
    再失败则对 limit_up_history universe 走腾讯 batch。
    """
    global LAST_AUCTION_SPOT_STATUS

    try:
        from src.data.sina_spot_api import fetch_a_share_list_sina

        df = fetch_a_share_list_sina()
        if df is not None and not df.empty and len(df) >= min_samples:
            print(f"[竞价全量] 新浪 {len(df)} 只（9:26 共用快照）")
            df = df.copy()
            df.attrs["source"] = "sina_full"
            LAST_AUCTION_SPOT_STATUS = "ok_sina"
            return df
        if df is not None and not df.empty:
            print(f"[竞价全量] 新浪仅 {len(df)} 只，尝试东财全量")
    except Exception as e:
        print(f"[竞价全量] 新浪失败: {e}")

    try:
        from src.data.ranking_scanner import fetch_full_market_spot

        df = fetch_full_market_spot()
        if df is not None and not df.empty:
            print(f"[竞价全量] 东财 spot_em {len(df)} 只（9:26 共用快照）")
            df = df.copy()
            df.attrs["source"] = "eastmoney_full"
            LAST_AUCTION_SPOT_STATUS = "ok_eastmoney"
            return df
    except Exception as e:
        print(f"[竞价全量] 东财 spot_em 失败: {e}")

    if limit_up_history:
        df = _fetch_auction_spot_tencent_universe(limit_up_history)
        if df is not None and not df.empty:
            LAST_AUCTION_SPOT_STATUS = "ok_tencent_universe"
            return df

    LAST_AUCTION_SPOT_STATUS = "empty"
    print("[竞价全量] 全市场竞价快照获取失败（含腾讯 universe 兜底）")
    return pd.DataFrame()


def fetch_auction_spot_batch(
    limit_up_history: dict[str, pd.DataFrame],
    *,
    ranking_file: str | None = None,
) -> pd.DataFrame:
    """仅按代码列表批量拉竞价（测试/兜底）；9:26 主路径请用 fetch_auction_spot_full。"""
    codes = collect_auction_quote_codes(
        limit_up_history,
        ranking_file=ranking_file,
        include_anchor_pools=True,
    )
    if not codes:
        return pd.DataFrame()
    from src.data.fetcher import fetch_realtime_batch

    df = fetch_realtime_batch(codes)
    n = len(df) if df is not None and not df.empty else 0
    print(f"[选股行情] 定向 batch universe={len(codes)} 命中={n}")
    return df if df is not None else pd.DataFrame()


def _find_code_column(df: pd.DataFrame) -> Optional[str]:
    """找到代码列"""
    for col in ["code", "代码", "股票代码"]:
        if col in df.columns:
            return col
    return df.columns[0] if len(df.columns) > 0 else None


def _get_avg_volume_5d(code: str) -> Optional[float]:
    """获取5日平均成交量（股），用于自算量比

    缓存到内存避免重复拉K线
    """
    if not hasattr(_get_avg_volume_5d, "_cache"):
        _get_avg_volume_5d._cache = {}

    if code in _get_avg_volume_5d._cache:
        return _get_avg_volume_5d._cache[code]

    try:
        from src.data.sina_kline_api import fetch_kline, SCALE_DAILY
        df = fetch_kline(code, SCALE_DAILY, datalen=5)
        if df is not None and not df.empty and len(df) >= 3:
            avg_vol = df["volume"].astype(float).mean()
            _get_avg_volume_5d._cache[code] = avg_vol
            return avg_vol
    except Exception:
        pass

    _get_avg_volume_5d._cache[code] = None
    return None


def _pass_exclusion(code: str, name: str, cfg: dict) -> bool:
    """排除规则"""
    # ST
    if cfg["exclude_st"] and ("ST" in name.upper() or "*ST" in name.upper()):
        return False

    # 科创板 688
    if cfg["exclude_kcb"] and code.startswith("688"):
        return False

    # 创业板 300/301
    if cfg["exclude_cyb"] and code.startswith(("300", "301")):
        return False

    # 北交所 8/4 开头
    if cfg["exclude_bse"] and code.startswith(("8", "4")):
        return False

    return True

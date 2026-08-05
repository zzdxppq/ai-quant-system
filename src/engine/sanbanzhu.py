"""三板组识别模块

选股推送后，查近几日龙虎榜数据，匹配已知三板组席位。
如果买入席位中出现2个及以上三板组席位 → 标记为三板组。

数据源：东方财富龙虎榜接口 / 新浪龙虎榜

「三板组」定义：相关游资席位。市场常见口径为下列营业部（东方财富席位名称
可能带「股份有限公司」「有限责任公司」等，故用子串匹配，见 SANBANZHU_SEATS）：

- 招商证券股份有限公司深圳建安路证券营业部
- 信达证券股份有限公司温州瓯江路证券营业部
- 光大证券股份有限公司武汉中北路证券营业部
- 华林证券股份有限公司深圳民田路证券营业部
- 华林证券股份有限公司深圳福华三路证券营业部
- 广发证券股份有限公司茂名双山七路福地大厦证券营业部
- 中信建投证券股份有限公司深圳深南中路中核大厦证券营业部
- 万联证券股份有限公司云南分公司
- 万联证券股份有限公司深圳分公司
- 中国银河证券股份有限公司深圳水贝证券营业部
- 中邮证券有限责任公司温州证券营业部
- 粤开证券股份有限公司广州分公司
- 天风证券股份有限公司江阴人民东路证券营业部
- 联储证券宁波分公司
- 华源证券深圳分公司
- 华鑫证券上海分公司
- 华鑫证券上海陆家嘴证券营业部（及东方财富常见等价表述）
- 华鑫证券上海源深路证券营业部
- 华鑫证券上海宛平南路证券营业部
- 国金证券上海奉贤区金碧路证券营业部
- 东北证券嘉兴新气象路证券营业部
- 长江证券武汉徐东大街证券营业部
"""
import time
from typing import Any

import httpx

from src.config import DATA_DIR, now_cn

SANBANZHU_LHB_CACHE_PATH = DATA_DIR / "sanbanzhu_lhb_cache.json"
# 晚间批量请求间隔（秒），降低东财限频风险
SANBANZHU_BATCH_SLEEP_SEC = 0.15


# 三板组营业部：龙虎榜「买入席位」名称去空格后，包含以下任一子串即视为命中
# （子串为东方财富常见展示中的稳定片段，略去「股份/有限责任」等前缀差异）
SANBANZHU_SEATS = [
    "深圳建安路证券营业",  # 招商证券…深圳建安路…
    "温州瓯江路证券营业",  # 信达证券…温州瓯江路…
    "武汉中北路证券营业",  # 光大证券…武汉中北路…
    "深圳民田路证券营业",  # 华林证券…深圳民田路…
    "深圳福华三路证券营业",  # 华林证券…深圳福华三路…
    "茂名双山七路",  # 广发证券…茂名双山七路福地大厦…
    "深南中路中核",  # 中信建投…深圳深南中路中核大厦…
    "万联证券云南分公司",
    "万联证券深圳分公司",
    "深圳水贝证券营业",  # 中国银河证券…深圳水贝…
    "中邮证券温州",  # 中邮证券有限责任公司温州证券营业部
    "粤开证券广州分公司",
    "江阴人民东路证券营业",  # 天风证券…江阴人民东路…
    "联储证券宁波分公司",
    "华源证券深圳分公司",
    "华鑫证券上海分公司",
    "上海陆家嘴证券营业",  # 华鑫证券…上海陆家嘴…
    "上海源深路证券营业",
    "上海宛平南路证券营业",
    "奉贤区金碧路证券营业",  # 国金证券上海奉贤区金碧路…
    "嘉兴新气象路证券营业",  # 东北证券嘉兴新气象路…
    "武汉徐东大街证券营业",  # 长江证券武汉徐东大街…
]


def _norm_code(code: object) -> str:
    from src.engine.daily_review import _norm_zt_code

    return _norm_zt_code(code)


def is_main_board_limit_up_code(code: object) -> bool:
    """主板涨停池口径：排除创业板/科创板/北交所（与 eastmoney 涨停筛选一致）"""
    c = _norm_code(code)
    if not c:
        return False
    if c.startswith(("300", "301", "688")):
        return False
    if c.startswith(("8", "4")):
        return False
    return True


def _classify_sanbanzhu_from_buy_seats(buy_seats: list[str]) -> dict:
    """由买入席位列表得到三板组判定（与 check_sanbanzhu 输出结构一致）"""
    result: dict[str, Any] = {
        "is_sanbanzhu": False,
        "matched_seats": [],
        "match_count": 0,
        "buy_seats": list(buy_seats),
        "detail": "",
    }
    if not buy_seats:
        result["detail"] = "未上榜，未检测（无龙虎榜数据）"
        return result

    matched: list[str] = []
    for seat in buy_seats:
        seat_cmp = seat.replace(" ", "")
        for keyword in SANBANZHU_SEATS:
            if keyword in seat_cmp:
                matched.append(seat)
                break

    result["matched_seats"] = matched
    result["match_count"] = len(matched)

    if len(matched) >= 2:
        result["is_sanbanzhu"] = True
        short_names = [s[:20] for s in matched]
        result["detail"] = f"⚠️三板组：{'、'.join(short_names)} 同时出现在买入席位"
    elif len(matched) == 1:
        result["detail"] = f"发现1个三板组席位：{matched[0][:20]}（单独出现不确认）"
    else:
        result["detail"] = "未发现三板组席位"

    return result


def _lookup_sanbanzhu_cache(code_norm: str) -> dict | None:
    """取最近一次晚间批量写入的该股结果（按 session_key 新→旧）"""
    from src.data.json_io import load_json_file

    raw = load_json_file(SANBANZHU_LHB_CACHE_PATH)
    if not isinstance(raw, dict):
        return None
    sessions = raw.get("sessions") or {}
    keys = sorted(
        (str(k) for k in sessions.keys() if str(k).isdigit() and len(str(k)) == 8),
        reverse=True,
    )
    for sk in keys:
        block = sessions.get(sk) or {}
        codes = block.get("codes") or {}
        entry = codes.get(code_norm)
        if isinstance(entry, dict) and "is_sanbanzhu" in entry:
            return {
                "is_sanbanzhu": bool(entry.get("is_sanbanzhu")),
                "matched_seats": list(entry.get("matched_seats") or []),
                "match_count": int(entry.get("match_count") or 0),
                "buy_seats": list(entry.get("buy_seats") or []),
                "detail": str(entry.get("detail") or ""),
            }
    return None


def check_sanbanzhu(code: str) -> dict:
    """查询龙虎榜判断是否三板组

    优先使用「上一交易日收盘后晚间批量」写入的缓存，未命中再请求东财。

    Returns:
        {
            "is_sanbanzhu": bool,
            "matched_seats": ["席位A", "席位B"],
            "match_count": 2,
            "buy_seats": [所有买入席位],
            "detail": "招商深圳建安路 + 华鑫上海分公司 同时出现在买入席位"
        }
    """
    c = _norm_code(code)
    if not c:
        return {
            "is_sanbanzhu": False,
            "matched_seats": [],
            "match_count": 0,
            "buy_seats": [],
            "detail": "代码无效",
        }

    cached = _lookup_sanbanzhu_cache(c)
    if cached is not None:
        return cached

    buy_seats = _fetch_lhb_buy_seats(c)
    return _classify_sanbanzhu_from_buy_seats(buy_seats)


def _fetch_lhb_buy_seats(code: str, trade_date_iso: str | None = None) -> list[str]:
    """从东方财富获取近日龙虎榜买入席位

    API: https://datacenter-web.eastmoney.com/api/data/v1/get

    Args:
        trade_date_iso: 若指定 YYYY-MM-DD，只保留该交易日的买入席（晚间批量用）
    """
    seats = _fetch_lhb_eastmoney(code, trade_date_iso=trade_date_iso)
    if seats:
        return seats

    seats = _fetch_lhb_sina(code)
    return seats


def _fetch_lhb_eastmoney(code: str, trade_date_iso: str | None = None) -> list[str]:
    """东方财富龙虎榜买入明细 RPT_BILLBOARD_DAILYDETAILSBUY"""
    try:
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        page_size = "200" if trade_date_iso else "10"
        params = {
            "sortColumns": "NET_BUY_AMT",
            "sortTypes": "-1",
            "pageSize": page_size,
            "pageNumber": "1",
            "reportName": "RPT_BILLBOARD_DAILYDETAILSBUY",
            "columns": "TRADE_DATE,SECURITY_CODE,BUYER_NAME,BUY_AMT,NET_BUY_AMT",
            "filter": f'(SECURITY_CODE="{code}")',
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
        }

        with httpx.Client(timeout=12, headers=headers) as client:
            resp = client.get(url, params=params)
            data = resp.json()

        if data.get("result") and data["result"].get("data"):
            seats: list[str] = []
            for item in data["result"]["data"]:
                if trade_date_iso:
                    td = item.get("TRADE_DATE")
                    if td is None:
                        continue
                    if str(td)[:10] != trade_date_iso:
                        continue
                name = item.get("BUYER_NAME", "")
                if name:
                    seats.append(name)
            return seats

    except Exception as e:
        print(f"[三板组] 东方财富龙虎榜失败({code}): {e}")

    return []


def _fetch_lhb_sina(code: str) -> list[str]:
    """新浪龙虎榜兜底"""
    # 新浪龙虎榜接口较难获取，暂返回空
    return []


def run_evening_mainboard_limitup_sanbanzhu_batch() -> dict:
    """交易日 20:00：先刷新涨停缓存，再对「当日主板涨停池」批量拉龙虎榜买入席并落盘。

    写入 ``data/sanbanzhu_lhb_cache.json``；次日 ``check_sanbanzhu`` 优先读缓存，减少早盘请求。
    """
    from src.config import is_trading_day
    from src.data.fetcher import fetch_limit_up_history
    from src.data.json_io import dump_json_file, load_json_file
    from src.engine.daily_review import (
        _get_limit_up_session_for_review,
        _session_key_to_iso,
    )

    if not is_trading_day():
        print(f"[{now_cn()}] 三板组晚间批量：非交易日，跳过")
        return {"skipped": True, "reason": "non-trading day"}

    print("=" * 50)
    print(f"[{now_cn()}] 三板组晚间批量：刷新涨停池并拉龙虎榜…")

    try:
        fetch_limit_up_history(days=5)
    except Exception as e:
        print(f"  涨停缓存刷新失败（仍尝试用本地池）: {e}")

    rows, session_key = _get_limit_up_session_for_review()
    trade_iso = _session_key_to_iso(session_key)

    pool: list[tuple[str, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        raw = r.get("code", "")
        c = _norm_code(raw)
        if not c or not is_main_board_limit_up_code(c):
            continue
        name = str(r.get("name", ""))
        pool.append((c, name))

    # 去重保序
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for c, name in pool:
        if c in seen:
            continue
        seen.add(c)
        uniq.append((c, name))

    print(f"  会话日={session_key} ({trade_iso})，主板涨停 {len(uniq)} 只，开始拉买入席…")

    codes_out: dict[str, dict] = {}
    for i, (c, name) in enumerate(uniq):
        try:
            buy_seats = _fetch_lhb_buy_seats(c, trade_date_iso=trade_iso)
            one = _classify_sanbanzhu_from_buy_seats(buy_seats)
            one["name"] = name
            codes_out[c] = {
                "name": name,
                "buy_seats": one["buy_seats"],
                "is_sanbanzhu": one["is_sanbanzhu"],
                "matched_seats": one["matched_seats"],
                "match_count": one["match_count"],
                "detail": one["detail"],
            }
        except Exception as e:
            print(f"  [三板组晚间] {c} 失败: {e}")
            codes_out[c] = {
                "name": name,
                "buy_seats": [],
                "is_sanbanzhu": False,
                "matched_seats": [],
                "match_count": 0,
                "detail": f"晚间批量拉取失败: {e}",
            }
        if i and i % 20 == 0:
            print(f"  …已处理 {i}/{len(uniq)}")
        time.sleep(SANBANZHU_BATCH_SLEEP_SEC)

    raw = load_json_file(SANBANZHU_LHB_CACHE_PATH)
    store: dict = raw if isinstance(raw, dict) else {}
    sessions = store.get("sessions") if isinstance(store.get("sessions"), dict) else {}
    if not isinstance(sessions, dict):
        sessions = {}

    sessions[str(session_key)] = {
        "session_key": str(session_key),
        "trade_date_iso": trade_iso,
        "built_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "pool_size": len(uniq),
        "codes": codes_out,
    }

    keys = sorted(
        (str(k) for k in sessions.keys() if str(k).isdigit() and len(str(k)) == 8),
        reverse=True,
    )[:30]
    store["sessions"] = {k: sessions[k] for k in keys if k in sessions}

    dump_json_file(SANBANZHU_LHB_CACHE_PATH, store)
    hit_sbz = sum(1 for v in codes_out.values() if v.get("is_sanbanzhu"))
    print(f"  完成：写入 {SANBANZHU_LHB_CACHE_PATH.name}，命中三板组确认 {hit_sbz} 只")
    print("=" * 50)
    return {
        "session_key": str(session_key),
        "trade_date_iso": trade_iso,
        "pool_size": len(uniq),
        "sanbanzhu_confirmed": hit_sbz,
    }


def check_and_annotate(hits_data: list[dict]) -> list[dict]:
    """批量检查选股结果中的三板组标记

    在选股推送后调用，为每个hit增加 sanbanzhu/sanbanzhu_detail 字段
    """
    for h in hits_data:
        code = h.get("code", "")
        if not code:
            continue
        try:
            result = check_sanbanzhu(code)
            h["sanbanzhu"] = result["is_sanbanzhu"]
            h["sanbanzhu_detail"] = result["detail"]
            if result["is_sanbanzhu"]:
                print(f"  [三板组] {code} {h.get('name','')} ⚠️确认三板组：{len(result['matched_seats'])}个席位")
        except Exception as e:
            print(f"  [三板组] {code} 检查失败: {e}")
            h["sanbanzhu"] = False
            h["sanbanzhu_detail"] = "检查失败"

    return hits_data


def update_history_records():
    """回填选股记录中的三板组标记"""
    from src.engine.screener_history import _load, _save

    records = _load()
    updated = 0
    for r in records:
        if r.get("sanbanzhu") is not None and r.get("sanbanzhu") is not False:
            continue  # 已有标记
        code = r.get("code", "")
        if not code:
            continue
        try:
            result = check_sanbanzhu(code)
            r["sanbanzhu"] = result["is_sanbanzhu"]
            r["sanbanzhu_detail"] = result["detail"]
            updated += 1
            time.sleep(0.3)  # 避免请求过快
        except Exception:
            pass

    if updated:
        _save(records)
        print(f"[三板组] 回填 {updated} 条记录")

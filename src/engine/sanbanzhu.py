"""三板组识别模块

选股推送后，查近几日龙虎榜数据，匹配已知三板组席位。
如果买入席位中出现2个及以上三板组席位 → 标记为三板组。

数据源：东方财富龙虎榜接口 / 新浪龙虎榜
"""
import json
import time
from typing import Optional

import httpx

from src.config import DATA_DIR, now_cn


# 市场公认的20家"三板组"营业部席位关键词
# 匹配规则：龙虎榜买入席位名称中包含以下任一关键词即视为命中
SANBANZHU_SEATS = [
    "招商证券深圳建安路",
    "信达证券温州瓯江路",
    "光大证券武汉中北路",
    "华林证券深圳民田路",
    "华林证券深圳福华三路",
    "广发证券茂名双山七路",
    "中信建投深圳深南中路中核",
    "万联证券云南分公司",
    "万联证券深圳分公司",
    "银河证券深圳水贝",
    "中邮证券温州",
    "粤开证券广州分公司",
    "天风证券江阴人民东路",
    "联储证券宁波分公司",
    "华源证券深圳分公司",
    "华鑫证券上海分公司",
    "华鑫证券上海陆家嘴",
    "国金证券上海奉贤",
    "东北证券嘉兴新气象路",
    "长江证券武汉徐东大街",
]


def check_sanbanzhu(code: str) -> dict:
    """查询龙虎榜判断是否三板组

    Returns:
        {
            "is_sanbanzhu": bool,
            "matched_seats": ["席位A", "席位B"],
            "match_count": 2,
            "buy_seats": [所有买入席位],
            "detail": "招商深圳建安路 + 华鑫上海分公司 同时出现在买入席位"
        }
    """
    result = {
        "is_sanbanzhu": False,
        "matched_seats": [],
        "match_count": 0,
        "buy_seats": [],
        "detail": "",
    }

    # 获取龙虎榜数据
    buy_seats = _fetch_lhb_buy_seats(code)
    if not buy_seats:
        result["detail"] = "未获取到龙虎榜数据"
        return result

    result["buy_seats"] = buy_seats

    # 匹配三板组席位
    matched = []
    for seat in buy_seats:
        for keyword in SANBANZHU_SEATS:
            if keyword in seat.replace(" ", ""):
                matched.append(seat)
                break

    result["matched_seats"] = matched
    result["match_count"] = len(matched)

    # 2个及以上三板组席位 → 确认
    if len(matched) >= 2:
        result["is_sanbanzhu"] = True
        short_names = [s[:20] for s in matched]
        result["detail"] = f"⚠️三板组：{'、'.join(short_names)} 同时出现在买入席位"
    elif len(matched) == 1:
        result["detail"] = f"发现1个三板组席位：{matched[0][:20]}（单独出现不确认）"
    else:
        result["detail"] = "未发现三板组席位"

    return result


def _fetch_lhb_buy_seats(code: str) -> list[str]:
    """从东方财富获取近日龙虎榜买入席位

    API: https://datacenter-web.eastmoney.com/api/data/v1/get
    """
    # 尝试东方财富龙虎榜
    seats = _fetch_lhb_eastmoney(code)
    if seats:
        return seats

    # 兜底：新浪龙虎榜
    seats = _fetch_lhb_sina(code)
    return seats


def _fetch_lhb_eastmoney(code: str) -> list[str]:
    """东方财富龙虎榜API"""
    try:
        # 东方财富龙虎榜明细
        market = "0" if not code.startswith(("6", "9")) else "1"
        secucode = f"{code}.{'SZ' if market == '0' else 'SH'}"

        url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        params = {
            "sortColumns": "NET_BUY_AMT",
            "sortTypes": "-1",
            "pageSize": "10",
            "pageNumber": "1",
            "reportName": "RPT_BILLBOARD_DAILYDETAILSBUY",
            "columns": "TRADE_DATE,SECURITY_CODE,BUYER_NAME,BUY_AMT,NET_BUY_AMT",
            "filter": f'(SECURITY_CODE="{code}")',
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
        }

        with httpx.Client(timeout=10, headers=headers) as client:
            resp = client.get(url, params=params)
            data = resp.json()

        if data.get("result") and data["result"].get("data"):
            seats = []
            for item in data["result"]["data"]:
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

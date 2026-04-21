"""自选股模块

功能：
1. 自选股增删查（持久化到 watchlist.json）
2. 搜索股票（从全市场行情中模糊匹配）
3. Kronos 预测管理（触发/缓存/读取）
"""
import json
import threading
from pathlib import Path
from typing import Optional

from src.config import DATA_DIR, now_cn


WATCHLIST_FILE = DATA_DIR / "watchlist.json"
PREDICTIONS_FILE = DATA_DIR / "watchlist_predictions.json"


# ========== 自选股 CRUD ==========

def _load_watchlist() -> list[dict]:
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text())
        except Exception:
            pass
    return []


def _save_watchlist(items: list[dict]):
    WATCHLIST_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2))


def get_watchlist() -> list[dict]:
    """获取自选股列表"""
    return _load_watchlist()


def add_to_watchlist(code: str, name: str = "") -> dict:
    """添加自选股

    Returns:
        {"ok": True} or {"ok": False, "msg": "..."}
    """
    items = _load_watchlist()

    # 去重
    if any(item["code"] == code for item in items):
        return {"ok": False, "msg": f"{code} 已在自选中"}

    items.append({
        "code": code,
        "name": name,
        "added_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_watchlist(items)

    # 异步触发预测
    _trigger_prediction_async(code)

    return {"ok": True}


def remove_from_watchlist(code: str) -> dict:
    """删除自选股"""
    items = _load_watchlist()
    before = len(items)
    items = [item for item in items if item["code"] != code]
    if len(items) == before:
        return {"ok": False, "msg": f"{code} 不在自选中"}
    _save_watchlist(items)

    # 清除预测缓存
    predictions = _load_predictions()
    predictions.pop(code, None)
    _save_predictions(predictions)

    return {"ok": True}


def search_stocks(keyword: str) -> list[dict]:
    """搜索股票（代码或名称模糊匹配）

    搜索顺序：排行数据 → 腾讯/新浪全市场 → 涨停缓存
    """
    results = []
    seen_codes = set()
    keyword_upper = keyword.strip().upper()
    keyword_raw = keyword.strip()
    if not keyword_raw:
        return results

    # 1. 从排行数据搜
    ranking_file = DATA_DIR / "latest_ranking.json"
    if ranking_file.exists():
        try:
            data = json.loads(ranking_file.read_text())
            for r in data.get("ranking", []):
                code = str(r.get("code", ""))
                name = str(r.get("name", ""))
                if keyword_upper in code or keyword_raw in name:
                    if code not in seen_codes:
                        results.append({"code": code, "name": name})
                        seen_codes.add(code)
        except Exception:
            pass

    if len(results) >= 10:
        return results[:10]

    # 2. 纯数字6位代码 → 新浪直接查
    if keyword_raw.isdigit() and len(keyword_raw) == 6:
        try:
            from src.data.sina_api import fetch_realtime_batch
            df = fetch_realtime_batch([keyword_raw])
            if not df.empty:
                row = df.iloc[0]
                code = str(row["code"])
                if code not in seen_codes:
                    results.append({"code": code, "name": str(row["name"])})
                    seen_codes.add(code)
        except Exception:
            pass
    else:
        # 3. 中文名称 → 腾讯/新浪全市场搜索
        try:
            from src.data.sina_spot_api import fetch_a_share_list_sina
            # 用缓存的全市场数据搜索（避免每次搜索都拉全市场）
            import os
            cache_file = DATA_DIR / "_stock_list_cache.json"
            stock_list = None

            # 缓存有效期1天
            if cache_file.exists():
                import time
                age = time.time() - cache_file.stat().st_mtime
                if age < 86400:
                    stock_list = json.loads(cache_file.read_text())

            if stock_list is None:
                df = fetch_a_share_list_sina()
                if not df.empty:
                    stock_list = [
                        {"code": str(row["code"]), "name": str(row["name"])}
                        for _, row in df[["code", "name"]].iterrows()
                    ]
                    cache_file.write_text(json.dumps(stock_list, ensure_ascii=False))

            if stock_list:
                for s in stock_list:
                    if keyword_raw in s["name"] or keyword_upper in s["code"]:
                        if s["code"] not in seen_codes:
                            results.append(s)
                            seen_codes.add(s["code"])
                            if len(results) >= 10:
                                break
        except Exception:
            pass

    return results[:10]


# ========== 预测管理 ==========

def _load_predictions() -> dict:
    if PREDICTIONS_FILE.exists():
        try:
            return json.loads(PREDICTIONS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_predictions(predictions: dict):
    PREDICTIONS_FILE.write_text(json.dumps(predictions, ensure_ascii=False, indent=2))


def get_prediction(code: str) -> Optional[dict]:
    """获取单只股票的预测结果"""
    predictions = _load_predictions()
    return predictions.get(code)


def get_all_predictions() -> dict:
    """获取所有自选股预测"""
    return _load_predictions()


def _trigger_prediction_async(code: str):
    """异步触发单只股票的预测"""
    def _run():
        try:
            from src.engine.kronos_predictor import predict_stock
            result = predict_stock(code)
            if result:
                predictions = _load_predictions()
                predictions[code] = result
                _save_predictions(predictions)
                print(f"[预测] {code} 完成: {result.get('trend', '?')}")
        except Exception as e:
            print(f"[预测] {code} 失败: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def run_all_predictions():
    """跑全部自选股预测（收盘后调用）"""
    items = _load_watchlist()
    if not items:
        print("[预测] 自选股为空，跳过")
        return

    print(f"[预测] 开始跑 {len(items)} 只自选股预测...")
    predictions = _load_predictions()

    from src.engine.kronos_predictor import predict_stock

    for item in items:
        code = item["code"]
        try:
            result = predict_stock(code)
            if result:
                predictions[code] = result
                print(f"  {code} {item.get('name','')}: {result.get('trend', '?')} ({result.get('pred_gain', 0):+.1f}%)")
        except Exception as e:
            print(f"  {code} 预测失败: {e}")

    _save_predictions(predictions)
    print(f"[预测] 全部完成，共 {len(predictions)} 只")

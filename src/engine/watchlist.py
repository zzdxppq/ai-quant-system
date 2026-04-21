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
    """异步触发单只股票的动量评分"""
    def _run():
        try:
            result = _score_one(code)
            if result:
                predictions = _load_predictions()
                predictions[code] = result
                _save_predictions(predictions)
                print(f"[动量评分] {code} 完成: {result.get('verdict_cn', '?')} ({result.get('score', 0)}分)")
        except Exception as e:
            print(f"[动量评分] {code} 失败: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _score_one(code: str) -> Optional[dict]:
    """对单只股票做动量评分"""
    from src.engine.momentum_scorer import score_from_kline
    import json

    # 获取连板数
    consecutive = 2  # 默认
    try:
        cache_file = DATA_DIR / "limit_up_cache.json"
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
            # 从最近日期往前数连续出现的天数
            sorted_dates = sorted(cache.keys(), reverse=True)
            count = 0
            for d in sorted_dates:
                codes_in_day = [r.get("code", "") for r in cache[d]]
                if code in codes_in_day:
                    count += 1
                else:
                    break
            if count > 0:
                consecutive = count
    except Exception:
        pass

    # 获取名称
    name = ""
    items = _load_watchlist()
    for item in items:
        if item["code"] == code:
            name = item.get("name", "")
            break

    result = score_from_kline(code, name=name, consecutive=max(2, consecutive))
    if result is None:
        return None

    return {
        "code": result.code,
        "name": result.name,
        "score": result.score,
        "verdict": result.verdict,
        "verdict_cn": result.verdict_cn,
        "probability": result.probability,
        "components": result.components,
        "predicted_at": result.scored_at,
        # 兼容前端字段
        "trend": result.verdict_cn,
        "pred_gain": result.score,  # 用分数代替涨幅
        "confidence": f"{result.probability*100:.0f}%",
    }


def run_all_predictions():
    """跑全部自选股动量评分（收盘后调用）"""
    items = _load_watchlist()
    if not items:
        print("[动量评分] 自选股为空，跳过")
        return

    print(f"[动量评分] 开始跑 {len(items)} 只自选股...")
    predictions = _load_predictions()

    for item in items:
        code = item["code"]
        try:
            result = _score_one(code)
            if result:
                predictions[code] = result
                print(f"  {code} {item.get('name','')}: {result['verdict_cn']} ({result['score']}分)")
        except Exception as e:
            print(f"  {code} 评分失败: {e}")

    _save_predictions(predictions)
    print(f"[动量评分] 全部完成，共 {len(predictions)} 只")

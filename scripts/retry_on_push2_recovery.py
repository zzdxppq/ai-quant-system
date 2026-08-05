"""监测 push2.eastmoney.com 恢复并触发一次完整更新

- 每 60s 探测一次 push2 API
- 恢复后立即执行 run_cycle_update + run_screener_update
- 结果写入 logs/retry_result.log
- 最多重试 60 次（约 1 小时），超时退出
"""
import sys
import time
from datetime import datetime
from pathlib import Path

# 确保能 import 项目模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.config  # noqa: F401 触发 .env 加载

import httpx

LOG_FILE = ROOT / "logs" / "retry_result.log"
LOG_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "Host": "push2.eastmoney.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
}
PROBE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
PROBE_PARAMS = {
    "np": "1", "fltt": "2", "invt": "2", "cb": "",
    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "fields": "f12,f14,f2,f3", "fid": "f3",
    "pn": "1", "pz": "5", "po": "1", "dect": "1",
    "wbp2u": "|0|0|0|web",
}


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _probe() -> bool:
    params = dict(PROBE_PARAMS)
    params["_"] = str(int(time.time() * 1000))
    try:
        r = httpx.get(PROBE_URL, params=params, headers=HEADERS, timeout=8)
        if r.status_code == 200 and r.json().get("data") is not None:
            return True
    except Exception:
        pass
    return False


def main():
    _log("=" * 60)
    _log("push2 恢复监测启动")
    max_attempts = 60
    for i in range(1, max_attempts + 1):
        if _probe():
            _log(f"第 {i} 次探测：push2 恢复！")
            break
        if i % 5 == 0:
            _log(f"第 {i}/{max_attempts} 次探测仍失败")
        time.sleep(60)
    else:
        _log("超过 60 次探测仍未恢复，退出")
        return

    _log("触发 run_cycle_update ...")
    from src.scheduler import run_cycle_update, run_screener_update
    try:
        snap = run_cycle_update()
        _log(f"周期更新完成: {snap.get('phase')} (第{snap.get('phase_day')}天) "
             f"代表股={snap.get('representative')}")
    except Exception as e:
        _log(f"周期更新失败: {type(e).__name__}: {e}")
        return

    _log("触发 run_screener_update ...")
    try:
        # push2 恢复时调用；邮件由 send_guard 中央守卫判定（2.6 升级）：
        # 非交易时段（凌晨/晚间）一律不发，仅补跑选股 + 看板数据。
        # skip_email=True → run_screener_update 不走邮件分支；窗口外的邮件发送意义不大
        # （数据基于残缺快照），由下一个 9:27 cron 重新推送。
        hits_data = run_screener_update(skip_email=True)
        hits = hits_data.get("hits", [])
        _log(f"选股完成: {len(hits)} 只命中")
        for h in hits:
            _log(f"  {h.get('code')} {h.get('name')} 连板{h.get('continuous_limit_up')} "
                 f"竞价{h.get('auction_gain')}% 市值{h.get('market_cap')}亿")
    except Exception as e:
        _log(f"选股失败: {type(e).__name__}: {e}")

    _log("完成")


if __name__ == "__main__":
    main()

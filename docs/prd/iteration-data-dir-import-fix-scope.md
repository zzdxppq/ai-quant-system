# Iteration Scope: email_sender.py 死代码 fallback 修复

## 背景

`src/notify/email_sender.py` 的 `_build_html` 函数在渲染选股 hits 表时，
当 `ranking_data` 缺失 `industry` / `top_concepts` 字段，
会进入 fallback 路径加载 `industry_cache.json` 和 `limit_up_cache.json`
做兜底渲染（概念红字 + 行业灰字）。

但**该 fallback 路径自 commit `bbe8c16` 引入起即为死代码**：

- 第 419-451 行使用 `DATA_DIR / "..."` 与 `json.loads(...)`
- 但文件第 19-24 行的 import 列表**未导入 `DATA_DIR` 和 `json`**：
  ```python
  import os
  import smtplib
  from email.mime.text import MIMEText
  from email.mime.multipart import MIMEMultipart
  from src.config import now_cn   # ← DATA_DIR 缺失
  # json 也缺失
  ```
- 外层 `try/except Exception: pass` 静默吞掉 `NameError`，路径**永远走不到**

## 影响

- 当 `ranking_data` 中的 ranking 列表覆盖不到某些命中股时（盘前缓存未刷新 / API 返回不全），
  这些股的板块列与概念列在邮件里显示 "-"，实际上磁盘上 industry/concept 缓存就有数据
- 选股记录的可读性下降，但**不影响决策结果**（决策只看 dailyAdvice 算法 + 6 指标格）

## 范围（共 1 项）

### 🔴 Class A — Import 修复

| # | 项 | 当前 | 期望 |
|---|---|---|---|
| A1 | `email_sender.py` 第 19-24 行 import 列表 | 缺 `DATA_DIR` / `json` | 加 `import json` 和 `from src.config import DATA_DIR` |

## 关键参考

| 用途 | 路径 | 行号 |
|---|---|---|
| 修复目标 | `src/notify/email_sender.py` | 19-24 (import 部分) |
| Bug 触发处 | `src/notify/email_sender.py` | 419-451 (fallback path) |
| `DATA_DIR` 定义 | `src/config.py` | 已 export |
| 引入此 bug 的 commit | `bbe8c16` (概念展示层 Phase 3/3) | 历史 |

## 验收标准（DoD）

1. **Import 完整性**：`python3 -c "from src.notify.email_sender import _build_html"` 不抛 ImportError
2. **静态扫描**：`grep -nE "DATA_DIR|json\." src/notify/email_sender.py` 中所有引用都对应 import 语句
3. **Fallback 路径走通**：构造一个 `ranking_data` 缺 industry/top_concepts 字段、但 hits 中包含已缓存股票的输入，渲染出的 HTML 应包含从 `industry_cache.json` 读到的行业名（不是 "-"）
4. **不引入回归**：现有 `tests/notify/test_email_decision_alignment.py` 46 个测试全绿
5. **测试**：补 `tests/notify/test_email_fallback_industry_concept.py`（或在现有文件加用例）覆盖 fallback 路径

## 范围外（明确不做）

- 不重构 `_build_html` 函数结构
- 不改 `industry_cache.json` / `limit_up_cache.json` 文件格式
- 不改 ranking_data 来源（这是另一个问题）
- 不优化外层 `try/except Exception: pass`（除非 SM/Architect 评估认为有必要）

## Story 类型

**Brownfield 单 Story · Quick 模式**（trivial 1 行 import 修复 + 测试用例）
建议 SM 设置 `mode: quick`，跳过 Architect *review，直接 Dev *quick-develop → QA *quick-verify。

或保持 standard 与 email-sync-1.1 一致，跑完整链路（视 SM 判断）。

"""auth-1.0 鉴权集成测试（12 条 TestClient 用例）。

依赖：
  - conftest 的 _isolate_quant_db_path 把 cfg.DB_PATH 重定向到 tmp_path
  - 本文件 _setup_env 在每个 case 前注入：
      ALLOWED_LOGIN_EMAILS=foo@x.com,bar@y.com
      AUTH_REQUIRED=1
      SMTP_USER/PASSWORD 空 → MOCK 模式
"""
from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# 允许白名单 + MOCK 模式的 fixture（autouse 让所有 case 默认开启）
@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_LOGIN_EMAILS", "foo@x.com,bar@y.com")
    monkeypatch.setenv("AUTH_REQUIRED", "1")
    monkeypatch.setenv("AUTH_CODE_RESEND_COOLDOWN", "0")  # 测试期间不限频
    monkeypatch.setenv("AUTH_TOKEN_TTL_HOURS", "12")
    monkeypatch.setenv("SMTP_USER", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    # 清空进程内 _RUNTIME_SECRET 让每个 test 用 .env 真实值（实际 .env 已设）
    import src.api.auth as _auth
    _auth._RUNTIME_SECRET = ""
    # 初始化 ledger_doc schema 到 conftest 隔离的 tmp_path 库
    from src.data.ledger_doc_store import init_ledger_doc_schema
    from src.data.quant_db import reset_shared_connection
    import src.data.ledger_doc_store as _lds
    _lds._SCHEMA_READY = False  # 重置 schema 缓存让新 tmp_path 库能建表
    reset_shared_connection()
    init_ledger_doc_schema()


def _client() -> TestClient:
    from src.api.app import app
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return _client()


def _request_code(client: TestClient, email: str):
    return client.post("/api/auth/request-code", json={"email": email})


def _read_mock_code_from_stdout() -> str | None:
    """从 _send_verification_code_email 的 stdout 抓 [AUTH-MOCK] code for ... 行。

    简单做法：直接调用内部函数，绕过 SMTP 路径。
    """
    import re
    import sys
    from io import StringIO

    import src.api.auth as _auth

    buf = StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        _auth._send_verification_code_email("foo@x.com", "654321")
    finally:
        sys.stdout = real_stdout
    text = buf.getvalue()
    m = re.search(r"\[AUTH-MOCK\] verification code for \S+: (\d{6})", text)
    return m.group(1) if m else None


# ============================================================
# 1. 白名单外邮箱
# ============================================================
def test_request_code_email_not_allowed(client):
    r = _request_code(_client(), "evil@x.com")
    assert r.status_code == 403
    assert r.json()["error"] == "email_not_allowed"


# ============================================================
# 2. 错验证码
# ============================================================
def test_verify_code_wrong(client):
    c = _client()
    # 直接 mock 写一个 code 记录（绕开发送）
    from src.api.auth import _code_ttl, _hash_code, _save_code_record
    from datetime import datetime
    from src.config import now_cn

    from datetime import timedelta
    now = now_cn()
    future = now + timedelta(minutes=5)
    _save_code_record(
        "foo@x.com",
        {
            "code_hash": _hash_code("123456"),
            "expires_at": future.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "attempts": 0,
            "last_sent_at": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        },
    )
    r = c.post("/api/auth/verify-code", json={"email": "foo@x.com", "code": "999999"})
    assert r.status_code == 401
    assert r.json()["error"] == "code_invalid"

    # attempts 已 +1
    from src.api.auth import _load_code_record
    rec = _load_code_record("foo@x.com")
    assert rec is not None and rec["attempts"] == 1


# ============================================================
# 3. 过期验证码
# ============================================================
def test_verify_code_expired(client):
    c = _client()
    from datetime import timedelta
    from src.api.auth import _hash_code, _save_code_record
    from src.config import now_cn

    past = now_cn() - timedelta(minutes=10)
    _save_code_record(
        "foo@x.com",
        {
            "code_hash": _hash_code("123456"),
            "expires_at": past.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "attempts": 0,
            "last_sent_at": past.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        },
    )
    r = c.post("/api/auth/verify-code", json={"email": "foo@x.com", "code": "123456"})
    assert r.status_code == 401
    assert r.json()["error"] == "code_invalid"


# ============================================================
# 4. 5 次试错后第 6 次
# ============================================================
def test_verify_code_too_many_attempts(client):
    c = _client()
    from src.api.auth import _hash_code, _load_code_record, _save_code_record, _code_max_attempts
    from src.config import now_cn

    from datetime import timedelta
    future = now_cn() + timedelta(minutes=5)
    _save_code_record(
        "foo@x.com",
        {
            "code_hash": _hash_code("123456"),
            "expires_at": future.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "attempts": _code_max_attempts(),  # 已达上限
            "last_sent_at": now_cn().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        },
    )
    r = c.post("/api/auth/verify-code", json={"email": "foo@x.com", "code": "123456"})
    assert r.status_code == 429
    assert r.json()["error"] == "too_many_attempts"
    # 记录已被删
    assert _load_code_record("foo@x.com") is None


# ============================================================
# 5. 无 token 访问 /api/cycle
# ============================================================
def test_no_token_blocks_api(client):
    c = _client()
    r = c.get("/api/cycle")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


# ============================================================
# 6. 错误 token
# ============================================================
def test_wrong_token_blocks_api(client):
    c = _client()
    r = c.get("/api/cycle", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert r.status_code == 401


# ============================================================
# 7. 有效 token 访问 /api/cycle
# ============================================================
def test_valid_token_allows_api(client):
    c = _client()
    # 直接造一个 token 写 ledger（绕开 verify-code，避免依赖 MOCK 流程）
    from src.api.auth import _issue_token
    user = _issue_token("foo@x.com")
    r = c.get("/api/cycle", headers={"Authorization": f"Bearer {user.token}"})
    assert r.status_code == 200


# ============================================================
# 8. 白名单路径豁免
# ============================================================
def test_whitelist_paths_exempt(client):
    c = _client()
    # /, /login, /static/* 都不要求 token
    for path in ["/", "/login", "/static/index.html"]:
        r = c.get(path)
        assert r.status_code in (200, 304), f"{path} 期望豁免，实际 {r.status_code}"


# ============================================================
# 9. 新登录顶号
# ============================================================
def test_new_login_revokes_old_token(client):
    c = _client()
    from src.api.auth import _issue_token, _verify_token

    u1 = _issue_token("foo@x.com")
    # 模拟"另一设备登录"
    u2 = _issue_token("foo@x.com")
    # 旧 token 验签仍能 decode 但 active_token_hash 已被新 token 顶替
    assert _verify_token(u1.token) is None
    assert _verify_token(u2.token) is not None


# ============================================================
# 10. 滑动续期：剩余 < 6h 调 me → 续到 12h
# ============================================================
def test_sliding_refresh_on_me(client, monkeypatch):
    c = _client()
    from src.api.auth import _issue_token, _hash_token
    from src.data.ledger_doc_store import load_json, upsert_json
    from datetime import timedelta
    from src.config import now_cn

    user = _issue_token("foo@x.com")
    # 把 token 记录改成"剩余 1h"
    th = _hash_token(user.token)
    near_expiry = now_cn() + timedelta(hours=1)
    rec = load_json(f"auth:token:{th}")
    rec["expires_at"] = near_expiry.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    upsert_json(f"auth:token:{th}", rec)

    r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {user.token}"})
    assert r.status_code == 200
    body = r.json()
    # 续期后应比 1h + threshold 长
    from datetime import datetime
    exp_str = body["expires_at"]
    if exp_str.endswith("+0800"):
        exp_str = exp_str[:-5] + "+08:00"
    new_exp = datetime.fromisoformat(exp_str)
    delta = (new_exp - now_cn()).total_seconds()
    # 应至少续到 6h（允许一些时钟误差；TTL=12h 续期会回到 ~12h）
    assert delta > 3600 * 6, f"期望至少续到 6h+，实际 {delta/3600:.1f}h"


# ============================================================
# 11. logout 后再用同 token
# ============================================================
def test_logout_invalidates_token(client):
    c = _client()
    from src.api.auth import _issue_token, _verify_token

    user = _issue_token("foo@x.com")
    r = c.post("/api/auth/logout", headers={"Authorization": f"Bearer {user.token}"})
    assert r.status_code == 200
    assert _verify_token(user.token) is None


# ============================================================
# 12. MOCK 模式：env 无 SMTP，request-code 返 mock:true 且 verify 流程通
# ============================================================
def test_mock_mode_full_flow(client):
    c = _client()
    r = _request_code(c, "foo@x.com")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    assert body["mock"] is True

    # 从 stdout 抓出 code（已通过 monkeypatch 的 SMTP 配置触发 print）
    # 用一个独立的 send 调用（直接走内部 helper 重发同一邮箱不重置 attempts）
    import src.api.auth as _auth
    code = "111222"  # 已知我们刚发的是 6 位随机；从记录的 hash 不可逆，直接重发一个固定码
    # 重新请求以让 cooldown 跳过（AUTH_CODE_RESEND_COOLDOWN=0 已在 fixture 设）
    r2 = _request_code(c, "foo@x.com")
    assert r2.status_code == 200

    # 实际场景下：MOCK 模式下 code 通过 stdout 给出；测试通过覆盖 code 记录的 hash 来模拟"拿到了 code"
    from src.api.auth import _hash_code, _save_code_record, _load_code_record
    from src.config import now_cn
    target_code = "654321"
    rec = _load_code_record("foo@x.com")
    assert rec is not None
    rec["code_hash"] = _hash_code(target_code)
    rec["attempts"] = 0
    _save_code_record("foo@x.com", rec)

    r3 = c.post("/api/auth/verify-code", json={"email": "foo@x.com", "code": target_code})
    assert r3.status_code == 200
    j = r3.json()
    assert "token" in j and j["email"] == "foo@x.com"
    # token 立即可用于 /api/cycle
    r4 = c.get("/api/cycle", headers={"Authorization": f"Bearer {j['token']}"})
    assert r4.status_code == 200

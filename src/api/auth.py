"""登录 + 邮箱验证码 + Bearer Token 鉴权（auth-1.0）

路由（全部白名单，无需 token）：
  POST /api/auth/request-code  → 发送 6 位验证码到白名单邮箱
  POST /api/auth/verify-code   → 输码换 JWT token
  POST /api/auth/logout        → 撤销当前 token（幂等，无 token 也 200）
  GET  /api/auth/me            → 探活 / 返回当前用户 + 滑动续期

存储：复用 ledger_doc K-V（src/data/ledger_doc_store.py）
  auth:code:<email_lowercase>      → {code_hash, expires_at, attempts, last_sent_at}
  auth:token:<sha256(token)>       → {email, issued_at, expires_at}
  auth:user:<email_lowercase>      → {active_token_hash}

Token：pyjwt HS256，默认 12h，剩余 < 6h 滑动续期
同账号新登录自动顶号（旧 token 通过 active_token_hash 比对失配 → 401）

MOCK 模式：SMTP_USER/SMTP_PASSWORD 任一为空 → 验证码 print 到 stdout
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Optional

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.config import now_cn
from src.data.ledger_doc_store import (
    delete_ledger_doc_key,
    list_ledger_doc_keys_glob,
    load_json,
    upsert_json,
)


# ============================================================
# 配置（环境变量）
# ============================================================
CN_TZ = timezone(timedelta(hours=8))

# 白名单路径：middleware 直接放行，不查 token
WHITELIST_PATH_EXACT = frozenset({
    "/",
    "/login",
    "/history",
    "/ranking",
    "/review",
    "/favicon.ico",
    "/api/auth/request-code",
    "/api/auth/verify-code",
    "/api/auth/logout",
})
WHITELIST_PATH_PREFIX = (
    "/static/",  # 静态资源（app.mount 先于 middleware 但兜底）
)


def _auth_required() -> bool:
    """AUTH_REQUIRED=0 时中间件旁路（仅本地开发用）。"""
    return os.getenv("AUTH_REQUIRED", "1").strip() != "0"


def _allowed_emails() -> set[str]:
    """读 ALLOWED_LOGIN_EMAILS 逗号分隔；空集合 = fail-closed 拒绝所有。"""
    raw = os.getenv("ALLOWED_LOGIN_EMAILS", "").strip()
    if not raw:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _auth_secret() -> str:
    """JWT HS256 签名密钥；未设时启动时随机生成（重启后所有 token 失效）。"""
    s = os.getenv("AUTH_SECRET", "").strip()
    if s:
        return s
    # 兜底：进程级一次性生成
    global _RUNTIME_SECRET
    if not _RUNTIME_SECRET:
        _RUNTIME_SECRET = secrets.token_hex(32)
    return _RUNTIME_SECRET


_RUNTIME_SECRET: str = ""  # AUTH_SECRET 未设时的进程级兜底


def _code_salt() -> str:
    return os.getenv("AUTH_CODE_SALT", "ai-quant-static-salt").strip()


def _token_ttl() -> timedelta:
    try:
        h = float(os.getenv("AUTH_TOKEN_TTL_HOURS", "12"))
    except ValueError:
        h = 12
    return timedelta(hours=max(1, h))


def _code_ttl() -> timedelta:
    try:
        m = float(os.getenv("AUTH_CODE_TTL_MINUTES", "5"))
    except ValueError:
        m = 5
    return timedelta(minutes=max(1, m))


def _code_max_attempts() -> int:
    try:
        n = int(os.getenv("AUTH_CODE_MAX_ATTEMPTS", "5"))
    except ValueError:
        n = 5
    return max(1, n)


def _resend_cooldown() -> int:
    try:
        s = int(os.getenv("AUTH_CODE_RESEND_COOLDOWN", "60"))
    except ValueError:
        s = 60
    return max(0, s)


def _sliding_threshold() -> timedelta:
    try:
        h = float(os.getenv("AUTH_SLIDING_REFRESH_THRESHOLD_HOURS", "6"))
    except ValueError:
        h = 6
    return timedelta(hours=max(1, h))


# ============================================================
# 校验工具
# ============================================================
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_valid_email(s: str) -> bool:
    if not s or len(s) > 254:
        return False
    return bool(_EMAIL_RE.match(s))


def _hash_code(code: str) -> str:
    return hashlib.sha256((_code_salt() + ":" + code).encode("utf-8")).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _now_utc() -> datetime:
    return datetime.now(CN_TZ)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        # 支持 "...+08:00" 与 "...+0800" 两种
        if s and len(s) >= 5 and s[-5] in ("+", "-") and s[-3] != ":":
            s = s[:-2] + ":" + s[-2:]
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ============================================================
# 邮件 helper（直接 smtplib，不走 send_screener_report）
# ============================================================
def _is_mock_mode() -> bool:
    return not (os.getenv("SMTP_USER", "").strip() and os.getenv("SMTP_PASSWORD", "").strip())


def _send_verification_code_email(to_addr: str, code: str) -> bool:
    """发送 6 位验证码邮件。失败抛异常；MOCK 模式 print 到 stdout 返回 True。"""
    if _is_mock_mode():
        print(f"[AUTH-MOCK] verification code for {to_addr}: {code}", flush=True)
        return True

    smtp_host = os.getenv("SMTP_HOST", "smtp.qq.com").strip() or "smtp.qq.com"
    smtp_port = int(os.getenv("SMTP_PORT", "465").strip() or "465")
    user = os.getenv("SMTP_USER", "").strip()
    pwd = os.getenv("SMTP_PASSWORD", "").strip()

    subject = f"【AI 量化看板】验证码 {code}"
    body = (
        f"您的登录验证码为：{code}\n"
        f"5 分钟内有效，5 次输错失效。\n"
        f"如非本人操作，请忽略。"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
            server.login(user, pwd)
            server.sendmail(user, [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"[AUTH] 验证码邮件发送失败: {e}", flush=True)
        return False


# ============================================================
# 验证码存储：auth:code:<email>
# ============================================================
def _code_key(email: str) -> str:
    return f"auth:code:{email.strip().lower()}"


def _load_code_record(email: str) -> Optional[dict]:
    return load_json(_code_key(email))


def _save_code_record(email: str, rec: dict) -> None:
    upsert_json(_code_key(email), rec)


def _delete_code_record(email: str) -> None:
    delete_ledger_doc_key(_code_key(email))


def _generate_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


# ============================================================
# Token 存储：auth:token:<hash> + auth:user:<email>
# ============================================================
def _token_key(token_hash: str) -> str:
    return f"auth:token:{token_hash}"


def _user_key(email: str) -> str:
    return f"auth:user:{email.strip().lower()}"


@dataclass
class User:
    email: str
    issued_at: datetime
    expires_at: datetime
    token: str  # 原始 JWT（中间件透传到 handler 用）

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "issued_at": _to_iso(self.issued_at),
            "expires_at": _to_iso(self.expires_at),
        }


def _issue_token(email: str) -> User:
    """签 JWT + 写 ledger；同邮箱的旧 active_token 自动顶号。"""
    now = _now_utc()
    exp = now + _token_ttl()
    payload = {
        "sub": email.strip().lower(),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(payload, _auth_secret(), algorithm="HS256")

    th = _hash_token(token)
    upsert_json(
        _token_key(th),
        {
            "email": payload["sub"],
            "issued_at": _to_iso(now),
            "expires_at": _to_iso(exp),
        },
    )
    # 顶号：把当前邮箱的 active_token_hash 指针更新
    upsert_json(_user_key(email), {"active_token_hash": th})
    return User(email=payload["sub"], issued_at=now, expires_at=exp, token=token)


def _verify_token(token: str) -> Optional[User]:
    """验签 + 查 active_token_hash 指针；任一失败返 None。"""
    if not token:
        return None
    try:
        payload = jwt.decode(token, _auth_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None

    email = str(payload.get("sub") or "").strip().lower()
    if not email:
        return None

    th = _hash_token(token)
    rec = load_json(_token_key(th))
    if not rec:
        return None
    if rec.get("email") != email:
        return None

    # 顶号检查：当前 token 必须是该邮箱的最新 active_token
    user_rec = load_json(_user_key(email))
    if not user_rec or user_rec.get("active_token_hash") != th:
        return None

    issued = _parse_iso(rec["issued_at"]) if isinstance(rec.get("issued_at"), str) else None
    expires = _parse_iso(rec["expires_at"]) if isinstance(rec.get("expires_at"), str) else None
    if issued is None or expires is None:
        return None
    if _now_utc() >= expires:
        return None
    return User(email=email, issued_at=issued, expires_at=expires, token=token)


def _maybe_slide(user: User) -> User:
    """滑动续期：剩余 < 阈值时续到 now+TTL。"""
    now = _now_utc()
    if user.expires_at - now > _sliding_threshold():
        return user
    new_exp = now + _token_ttl()
    th = _hash_token(user.token)
    rec = load_json(_token_key(th))
    if rec:
        rec["expires_at"] = _to_iso(new_exp)
        upsert_json(_token_key(th), rec)
    return User(email=user.email, issued_at=user.issued_at, expires_at=new_exp, token=user.token)


def _revoke_token(token: str) -> None:
    """从 ledger 删除 token 记录 + 清掉 user 指针。"""
    if not token:
        return
    try:
        payload = jwt.decode(token, _auth_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return
    email = str(payload.get("sub") or "").strip().lower()
    th = _hash_token(token)
    delete_ledger_doc_key(_token_key(th))
    if email:
        user_rec = load_json(_user_key(email))
        if user_rec and user_rec.get("active_token_hash") == th:
            delete_ledger_doc_key(_user_key(email))


# ============================================================
# 路由
# ============================================================
router = APIRouter(prefix="/api/auth", tags=["auth"])


class RequestCodeBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)


class VerifyCodeBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    code: str = Field(..., min_length=4, max_length=10)


@router.post("/request-code")
async def request_code(payload: RequestCodeBody) -> JSONResponse:
    email = payload.email.strip().lower()
    if not _is_valid_email(email):
        return JSONResponse({"error": "invalid_email"}, status_code=400)

    allowed = _allowed_emails()
    if not allowed or email not in allowed:
        # fail-closed：白名单为空或邮箱不在白名单都拒绝
        return JSONResponse(
            {"error": "email_not_allowed", "allowed_count": len(allowed)},
            status_code=403,
        )

    # 限频：同邮箱 cooldown 秒内不重发
    cooldown = _resend_cooldown()
    if cooldown > 0:
        rec = _load_code_record(email)
        if rec and isinstance(rec.get("last_sent_at"), str):
            last = _parse_iso(rec["last_sent_at"])
            if last and (_now_utc() - last).total_seconds() < cooldown:
                retry = cooldown - int((_now_utc() - last).total_seconds())
                return JSONResponse(
                    {"error": "too_frequent", "retry_after": max(1, retry)},
                    status_code=429,
                )

    code = _generate_code()
    now = _now_utc()
    expires = now + _code_ttl()
    _save_code_record(
        email,
        {
            "code_hash": _hash_code(code),
            "expires_at": _to_iso(expires),
            "attempts": 0,
            "last_sent_at": _to_iso(now),
        },
    )

    sent = _send_verification_code_email(email, code)
    if not sent:
        # 发送失败：清掉记录（让用户重试），但仍然返 500 区分
        _delete_code_record(email)
        return JSONResponse(
            {"error": "send_failed", "email": email},
            status_code=500,
        )
    return JSONResponse(
        {"sent": True, "mock": _is_mock_mode(), "email": email},
        status_code=200,
    )


@router.post("/verify-code")
async def verify_code(payload: VerifyCodeBody) -> JSONResponse:
    email = payload.email.strip().lower()
    code = payload.code.strip()
    if not _is_valid_email(email):
        return JSONResponse({"error": "invalid_email"}, status_code=400)
    if not (code.isdigit() and len(code) == 6):
        return JSONResponse({"error": "invalid_code_format"}, status_code=400)

    # 即使邮箱已被白名单移除也拒绝
    allowed = _allowed_emails()
    if not allowed or email not in allowed:
        return JSONResponse({"error": "email_not_allowed"}, status_code=403)

    rec = _load_code_record(email)
    if not rec:
        return JSONResponse({"error": "code_invalid"}, status_code=401)

    # 过期
    expires_at = _parse_iso(rec.get("expires_at", ""))
    if not expires_at or _now_utc() >= expires_at:
        _delete_code_record(email)
        return JSONResponse({"error": "code_invalid"}, status_code=401)

    # 试错计数
    attempts = int(rec.get("attempts") or 0)
    if attempts >= _code_max_attempts():
        _delete_code_record(email)
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)

    # 比对
    if not _constant_time_eq(_hash_code(code), rec.get("code_hash", "")):
        rec["attempts"] = attempts + 1
        _save_code_record(email, rec)
        return JSONResponse({"error": "code_invalid"}, status_code=401)

    # 校验通过：发 token + 清 code
    _delete_code_record(email)
    user = _issue_token(email)
    return JSONResponse(
        {
            "token": user.token,
            "email": user.email,
            "issued_at": _to_iso(user.issued_at),
            "expires_at": _to_iso(user.expires_at),
        },
        status_code=200,
    )


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """幂等：未带 token 也 200；带 token 则撤销。"""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if token:
        _revoke_token(token)
    return JSONResponse({"ok": True}, status_code=200)


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    """探活 + 滑动续期。401 表示未登录或 token 失效。"""
    user = _get_user_from_request(request)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    user = _maybe_slide(user)
    return JSONResponse(user.to_dict(), status_code=200)


# ============================================================
# 内部 helper：给 app.py 的中间件用
# ============================================================
def _get_user_from_request(request: Request) -> Optional[User]:
    """从 Authorization header 解 Bearer token；不查 ledger 由 _verify_token 内部完成。"""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    return _verify_token(token)


def is_whitelisted_path(path: str) -> bool:
    if path in WHITELIST_PATH_EXACT:
        return True
    for prefix in WHITELIST_PATH_PREFIX:
        if path.startswith(prefix):
            return True
    return False


def get_user_for_request(request: Request) -> Optional[User]:
    """中间件 / 路由调用：返回 User 或 None；调用方决定 401 还是放行。"""
    return _get_user_from_request(request)


# ============================================================
# 周期清理：删除 auth:token:* 过期 > 7 天的记录（供 app.py lifespan 启 daemon）
# ============================================================
def purge_expired_tokens(retention_days: int = 7) -> int:
    """清理过期超过 retention_days 天的 token 记录。返回删除条数。"""
    now = _now_utc()
    cutoff = now - timedelta(days=retention_days)
    keys = list_ledger_doc_keys_glob("auth:token:*")
    n = 0
    for k in keys:
        rec = load_json(k)
        if not isinstance(rec, dict):
            continue
        exp = _parse_iso(rec.get("expires_at", ""))
        if exp and exp < cutoff:
            delete_ledger_doc_key(k)
            n += 1
    return n


def start_purge_daemon() -> threading.Thread:
    """后台线程：每 24h 调用一次 purge_expired_tokens。"""
    stop = threading.Event()

    def _loop():
        while not stop.wait(timeout=24 * 3600):
            try:
                n = purge_expired_tokens()
                if n:
                    print(f"[AUTH] 清理过期 token 记录 {n} 条", flush=True)
            except Exception as e:
                print(f"[AUTH] 清理 daemon 异常: {e}", flush=True)

    t = threading.Thread(target=_loop, daemon=True, name="auth-purge")
    t.start()
    return t


# ============================================================
# 启动提示（供 app.py lifespan 调用）
# ============================================================
def log_startup_banner() -> None:
    allowed = _allowed_emails()
    secret = os.getenv("AUTH_SECRET", "").strip()
    print(f"[AUTH] 鉴权{'启用' if _auth_required() else '已禁用 (AUTH_REQUIRED=0)'}")
    if not secret:
        print(
            "[AUTH] 警告: AUTH_SECRET 未设置，启动时随机生成；"
            "重启后所有历史 token 失效。生产请在 .env 显式设。",
            flush=True,
        )
    if not allowed:
        print(
            "[AUTH] 警告: ALLOWED_LOGIN_EMAILS 为空，鉴权 fail-closed 拒绝所有登录请求",
            flush=True,
        )
    else:
        masked = ", ".join(
            e if len(e) <= 4 else (e[:2] + "***" + e[-2:]) for e in sorted(allowed)
        )
        print(f"[AUTH] 白名单 {len(allowed)} 个邮箱: {masked}", flush=True)
    if _is_mock_mode():
        print("[AUTH] 警告: SMTP 配置缺失，验证码将 print 到 stdout（MOCK 模式）", flush=True)

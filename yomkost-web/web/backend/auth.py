"""Web account authentication for Yomkost Store.

Uses scrypt password hashes and signed, expiring HttpOnly session cookies.
No third-party auth provider is required.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request, Response

COOKIE_NAME = "yomkost_session"
SESSION_SECONDS = 60 * 60 * 24 * 7
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_attempts: dict[str, deque[float]] = defaultdict(deque)
_attempts_lock = Lock()
_SECRET = os.getenv("WEB_SECRET_KEY", "")
if not _SECRET:
    if os.getenv("ENVIRONMENT", "development").lower() == "production":
        raise RuntimeError("WEB_SECRET_KEY must be set in production")
    _SECRET = secrets.token_urlsafe(48)
_SECRET_BYTES = _SECRET.encode("utf-8")


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise HTTPException(status_code=422, detail="Вкажи коректну електронну пошту")
    return email


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise HTTPException(status_code=422, detail="Пароль має містити щонайменше 10 символів")
    if len(password) > 128:
        raise HTTPException(status_code=422, detail="Пароль задовгий")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return "scrypt$16384$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, cost, salt_b64, digest_b64 = stored.split("$", 3)
        if scheme != "scrypt" or cost != "16384":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def create_session_token(account_id: int) -> str:
    payload = _b64(json.dumps({"sub": account_id, "exp": int(time.time()) + SESSION_SECONDS}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(_SECRET_BYTES, payload.encode(), hashlib.sha256).digest())
    return payload + "." + signature


def read_session_token(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(_SECRET_BYTES, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        account_id = int(data.get("sub", 0))
        return account_id if account_id > 0 else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def set_session_cookie(response: Response, account_id: int, request: Request) -> None:
    same_site = os.getenv("COOKIE_SAMESITE", "lax").lower()
    if same_site not in {"lax", "strict", "none"}:
        same_site = "lax"
    is_secure = request.url.scheme == "https" or same_site == "none" or os.getenv("COOKIE_SECURE", "").lower() == "true"
    response.set_cookie(
        COOKIE_NAME, create_session_token(account_id), max_age=SESSION_SECONDS,
        httponly=True, secure=is_secure, samesite=same_site, path="/",
    )


def token_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and len(header) > 10:
        return header[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def clear_session_cookie(response: Response) -> None:
    same_site = os.getenv("COOKIE_SAMESITE", "lax").lower()
    if same_site not in {"lax", "strict", "none"}:
        same_site = "lax"
    response.set_cookie(COOKIE_NAME, "", max_age=0, httponly=True, secure=same_site == "none", samesite=same_site, path="/")


def check_login_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _attempts_lock:
        bucket = _attempts[ip]
        while bucket and now - bucket[0] > 900:
            bucket.popleft()
        if len(bucket) >= 8:
            raise HTTPException(status_code=429, detail="Забагато спроб, спробуй через 15 хвилин")
        bucket.append(now)


def reset_login_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    with _attempts_lock:
        _attempts.pop(ip, None)

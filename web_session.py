"""ChatGPT 会话续期：Access Token 过期时用 session cookie 自动换取新 Token。

Access Token 约 10 天过期，而 chatgpt.com 的 session cookie（__Secure-next-auth.session-token，
即 /api/auth/session 返回的 sessionToken）有效期长得多。配置 CHATGPT_SESSION_TOKEN 后：
chatgpt.com 返回 401 / token_expired 时抛出 TokenExpired，call_with_renew 用 cookie 调用
/api/auth/session 换取新 Token 并重试一次。旧 Token 过期前该接口返回的仍是同一个 Token，所以只在过期后续期。
cookie 每次调用都会轮换，新的 Token 和 cookie 保存到 SESSION_FILE，启动时优先于 .env 读取。
cookie 失效时抛出 SessionExpired（HTTP 401 session_expired），需通过一次性刷新页面（routes_session.py）重新提交会话。
"""

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, TypeVar

from fastapi.responses import JSONResponse

from config import settings
from http_client import build_session

COOKIE = "__Secure-next-auth.session-token"
SESSION_EXPIRED = ("The ChatGPT session expired or was rejected. Create a refresh link with POST /v1/session/link "
                   "on the server and paste https://chatgpt.com/api/auth/session there, "
                   "or update CHATGPT_SESSION_TOKEN / CHATGPT_ACCESS_TOKEN.")

lock = asyncio.Lock()
T = TypeVar("T")


class AuthError(Exception):
    code = "token_expired"

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": {"code": self.code, "message": str(self)}})


class TokenExpired(AuthError):
    pass


class SessionExpired(AuthError):
    code = "session_expired"


def _load() -> dict[str, Any]:
    try:
        with open(settings.session_file, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_state = _load()
if _state.get("accessToken"):
    settings.chatgpt_access_token = _state["accessToken"]


def has_session() -> bool:
    return bool(_state.get("sessionToken") or settings.chatgpt_session_token.strip())


def check_auth(resp) -> None:
    """chatgpt.com 拒绝 Access Token 时抛出 TokenExpired。"""
    if resp.status_code == 401 or (resp.status_code >= 400 and "token_expired" in resp.text):
        raise TokenExpired(f"upstream {resp.status_code}: {resp.text[:200]}")


async def fetch_session(session_token: str) -> dict[str, Any]:
    """用 cookie 调用 /api/auth/session，返回当前 Access Token 和轮换后的 cookie。"""
    url = f"{settings.chatgpt_base_url.rstrip('/')}/api/auth/session"
    async with build_session() as session:
        resp = await session.get(url, cookies={COOKIE: session_token}, timeout=30)
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("application/json"):
        raise Exception(f"{url}: HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("accessToken"):
        raise SessionExpired(SESSION_EXPIRED)  # cookie 失效时返回 {}
    return {"accessToken": data["accessToken"],
            "sessionToken": data.get("sessionToken") or resp.cookies.get(COOKIE) or session_token,
            "sessionExpires": data.get("expires")}


def save(state: dict[str, Any]) -> None:
    """保存会话到 SESSION_FILE（权限 600）并切换到新 Token。"""
    tmp = f"{settings.session_file}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, settings.session_file)
    _state.clear()
    _state.update(state)
    settings.chatgpt_access_token = state["accessToken"]


async def renew(stale_token: str) -> str:
    """用 session cookie 换取新 Access Token；并发请求只续期一次。"""
    async with lock:
        current = settings.chatgpt_access_token.strip()
        if current != stale_token:
            return current  # 其他请求已续期
        session_token = _state.get("sessionToken") or settings.chatgpt_session_token.strip()
        if not session_token:
            raise SessionExpired(SESSION_EXPIRED)
        state = await fetch_session(session_token)
        save(state)
        print(f"access token {'renewed' if state['accessToken'] != stale_token else 'unchanged'} "
              f"from the session cookie (session until {state['sessionExpires']})")
        return state["accessToken"]


async def call_with_renew(call: Callable[[str], Awaitable[T]]) -> T:
    """执行 call(token)；Token 被拒绝时续期并重试一次。"""
    token = settings.chatgpt_access_token.strip() or await renew("")
    try:
        return await call(token)
    except TokenExpired:
        return await call(await renew(token))

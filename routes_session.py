"""一次性会话刷新页面：session cookie 失效后，从任意已登录 ChatGPT 的浏览器（电脑或手机）提交新会话，无需重启。

POST /v1/session/link 只允许本机调用，返回一个 30 分钟内有效、只能成功使用一次的链接。
用户在 /chatgpt-session/<token> 页面粘贴 https://chatgpt.com/api/auth/session 的 JSON，
网关用其中的 sessionToken 向 chatgpt.com 验证后保存（web_session.save）。
链接只保存在内存中，重启后失效；提交失败时链接仍可继续使用。
"""

import hmac
import html
import json
import secrets
import time
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import settings
from web_session import SessionExpired, fetch_session, lock, save

router = APIRouter()

LINK_TTL = 1800
MAX_BODY = 64 * 1024
LOCAL_HOSTS = ("127.0.0.1", "::1")
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")
_links: dict[str, float] = {}  # token -> 过期时间

FORM = """<ol>
<li>在已登录 ChatGPT 的浏览器中打开 / In a browser where you are logged in to ChatGPT, open
<a href="https://chatgpt.com/api/auth/session" target="_blank" rel="noreferrer">https://chatgpt.com/api/auth/session</a></li>
<li>全选并复制页面上的全部文字 / Select all of the text and copy it<br>
<span class="note">手机：长按文字 → 全选 → 复制 / On a phone: long-press the text, Select all, Copy</span></li>
<li>粘贴到下面并保存 / Paste it below and press Save</li>
</ol>
<form method="post">
<textarea name="session" rows="8" required autocomplete="off" autocapitalize="off" spellcheck="false"
 placeholder='{"user": ..., "accessToken": "...", "sessionToken": "..."}'></textarea>
<button type="submit">保存 / Save</button>
</form>
<p class="note">内容只发送给本网关。链接只能成功使用一次，生成 30 分钟后失效。<br>
The text is sent only to this gateway. The link works once and expires 30 minutes after it was created.</p>"""


def _page(status: int, title: str, body: str) -> HTMLResponse:
    return HTMLResponse(status_code=status, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    }, content=f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title>
<style>
body {{ font: 16px/1.5 system-ui, -apple-system, sans-serif; margin: 0 auto; max-width: 40rem; padding: 1rem; color: #222; }}
h1 {{ font-size: 1.3rem; }} a {{ word-break: break-all; }} li {{ margin-bottom: .5rem; }}
textarea {{ box-sizing: border-box; width: 100%; font: 13px monospace; padding: .5rem; }}
button {{ margin-top: .75rem; width: 100%; padding: .8rem; font-size: 1rem; border: 0; border-radius: .4rem; background: #10a37f; color: #fff; }}
.error {{ background: #fdecea; color: #a1281d; padding: .6rem .8rem; border-radius: .4rem; }}
.note {{ color: #666; font-size: .9rem; }}
</style></head>
<body><h1>{title}</h1>
{body}
</body></html>""")


def _form(status: int = 200, error: str = "") -> HTMLResponse:
    return _page(status, "刷新 ChatGPT 会话 / Refresh the ChatGPT session",
                 (f'<p class="error">{html.escape(error)}</p>\n' if error else "") + FORM)


def _not_found() -> HTMLResponse:
    return _page(404, "链接无效 / Link not found",
                 "<p>链接错误、已过期或已使用，请重新生成。<br>This link is wrong, expired or already used. Create a new one.</p>")


def _valid(token: str) -> bool:
    now = time.time()
    for t, expires in list(_links.items()):
        if expires < now:
            del _links[t]
    return any(hmac.compare_digest(t.encode(), token.encode()) for t in _links)


@router.post("/v1/session/link")
async def create_link(request: Request):
    """生成一次性会话刷新链接。只允许本机直接调用，经反向代理转发的请求一律拒绝。"""
    if request.client is None or request.client.host not in LOCAL_HOSTS or any(h in request.headers for h in PROXY_HEADERS):
        return JSONResponse(status_code=403, content={"error": {"message": "localhost only"}})
    token = secrets.token_urlsafe(32)
    _links[token] = time.time() + LINK_TTL
    base = (settings.session_refresh_base_url or str(request.base_url)).rstrip("/")
    return {"url": f"{base}/chatgpt-session/{token}",
            "expires_at": datetime.fromtimestamp(_links[token]).astimezone().isoformat(timespec="minutes")}


@router.get("/chatgpt-session/{token}")
async def refresh_page(token: str):
    return _form() if _valid(token) else _not_found()


@router.post("/chatgpt-session/{token}")
async def refresh_submit(token: str, request: Request):
    if not _valid(token):
        return _not_found()
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return _form(413, "内容过长，请只复制 chatgpt.com/api/auth/session 页面的内容。"
                              " / Too long: copy only the page at chatgpt.com/api/auth/session.")
    try:
        pasted = json.loads(parse_qs(body.decode("utf-8", "replace")).get("session", [""])[0])
        if not (isinstance(pasted.get("sessionToken"), str) and pasted["sessionToken"]):
            raise ValueError
    except (ValueError, AttributeError):
        return _form(400, "这不是会话 JSON（需要包含 sessionToken）。如果页面显示 {}，说明该浏览器未登录，请先登录 ChatGPT 再打开。"
                          " / This is not the session JSON (it needs sessionToken). If the page showed {}, "
                          "you are logged out there: log in to ChatGPT first.")
    async with lock:
        if not _valid(token):
            return _not_found()
        try:
            state = await fetch_session(pasted["sessionToken"])
        except SessionExpired:
            return _form(400, "chatgpt.com 拒绝了该会话，请重新登录 ChatGPT 后再复制。"
                              " / chatgpt.com rejected this session. Log in to ChatGPT again and copy the page again.")
        except Exception as e:
            return _form(502, f"无法连接 chatgpt.com 验证会话（{type(e).__name__}），请稍后重试。"
                              f" / Could not reach chatgpt.com to check the session ({type(e).__name__}). Try again later.")
        save(state)
        del _links[token]
    print(f"session replaced through the refresh page (session until {state['sessionExpires']})")
    until = f"（{state['sessionExpires'][:10]}）" if state.get("sessionExpires") else ""
    return _page(200, "会话已保存 / Session saved",
                 f"<p>ChatGPT 会话已更新{html.escape(until)}，可以关闭此页面。<br>"
                 "The ChatGPT session is updated. You can close this page.</p>")

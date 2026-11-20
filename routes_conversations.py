"""/v1/conversations：列出、搜索、读取、归档和删除账号中的 ChatGPT 网页端对话。"""

from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Path, Query
from fastapi.responses import JSONResponse

from config import settings
from http_client import build_session
from routes_chat import CONVERSATION_ID_PATTERN, WORK_ORIGIN
from web_fingerprint import WebFingerprint
from web_session import call_with_renew, check_auth

router = APIRouter()

ConversationId = Annotated[str, Path(pattern=CONVERSATION_ID_PATTERN)]


async def _backend(method: str, path: str, query: str = "", body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """调用 chatgpt.com/backend-api，返回 (状态码, JSON)。"""
    return await call_with_renew(lambda token: _request(token, method, path, query, body))


async def _request(token: str, method: str, path: str, query: str, body: dict[str, Any] | None) -> tuple[int, Any]:
    fp = WebFingerprint()
    base = settings.chatgpt_base_url.rstrip("/")
    headers = fp.base_headers(token, path)
    if body is not None:
        headers["Content-Type"] = "application/json"
    async with build_session() as session:
        resp = await session.request(method, f"{base}{path}{query}", headers=headers, json=body)
    check_auth(resp)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"detail": resp.text[:300]}


def _error(status: int, data: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": f"upstream {status}: {str(data)[:300]}", "type": "upstream_error"}})


def _iso(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return value


def _url(conversation_id: str) -> str:
    return f"{settings.chatgpt_base_url.rstrip('/')}/c/{conversation_id}"


def _text(message: dict[str, Any]) -> str:
    parts = message.get("content", {}).get("parts") or []
    return "".join(p for p in parts if isinstance(p, str)).strip()


@router.get("/v1/conversations")
async def list_conversations(q: str = "", limit: Annotated[int, Query(ge=1, le=100)] = 20,
                             offset: Annotated[int, Query(ge=0)] = 0, cursor: str = "", archived: bool = False):
    """按更新时间倒序列出对话。带 q 时使用 ChatGPT 自带搜索（标题和消息内容），用 cursor 翻页。"""
    if q:
        status, data = await _backend("GET", "/backend-api/conversations/search", f"?query={quote(q)}&cursor={quote(cursor)}")
        if status >= 400:
            return _error(status, data)
        items = [{"id": i["conversation_id"], "title": i.get("title"), "update_time": _iso(i.get("update_time")),
                  "is_archived": i.get("is_archived"), "url": _url(i["conversation_id"]),
                  "snippet": (i.get("payload") or {}).get("snippet")} for i in data.get("items", [])[:limit]]
        return {"items": items, "cursor": data.get("cursor")}

    query = f"?offset={offset}&limit={limit}&order=updated" + ("&is_archived=true" if archived else "")
    status, data = await _backend("GET", "/backend-api/conversations", query)
    if status >= 400:
        return _error(status, data)
    items = [{"id": i["id"], "title": i.get("title"), "create_time": _iso(i.get("create_time")),
              "update_time": _iso(i.get("update_time")), "is_archived": i.get("is_archived"), "url": _url(i["id"])}
             for i in data.get("items", [])]
    return {"items": items, "has_more": (data.get("total") or 0) > offset + len(items)}


@router.get("/v1/conversations/{conversation_id}")
async def read_conversation(conversation_id: ConversationId):
    """当前分支中可见的用户和助手消息（按时间正序），以及 ChatGPT 在其中发布的站点（可分享的 artifact）链接。"""
    status, data = await _backend("GET", f"/backend-api/conversation/{conversation_id}")
    if status >= 400:
        return _error(status, data)
    chain, node = [], data.get("current_node")
    while node:
        entry = data["mapping"][node]
        chain.append(entry.get("message"))
        node = entry.get("parent")
    messages, sites = [], {}
    for m in reversed(chain):
        sites.update(((m or {}).get("metadata") or {}).get("site_url_to_site_id") or {})
        if (not m or m["author"]["role"] not in ("user", "assistant") or m.get("recipient") != "all"
                or m.get("content", {}).get("content_type") != "text"
                or (m.get("metadata") or {}).get("is_visually_hidden_from_conversation")):
            continue
        text = _text(m)
        if text:
            messages.append({"role": m["author"]["role"], "time": _iso(m.get("create_time")),
                             "model": (m.get("metadata") or {}).get("model_slug"), "text": text})
    return {"id": conversation_id, "title": data.get("title"), "create_time": _iso(data.get("create_time")),
            "update_time": _iso(data.get("update_time")), "is_archived": data.get("is_archived"),
            "url": _url(conversation_id), "mode": "work" if data.get("conversation_origin") == WORK_ORIGIN else "chat",
            "messages": messages, "artifacts": [{"url": url, "id": site_id} for url, site_id in sites.items()]}


@router.patch("/v1/conversations/{conversation_id}")
async def update_conversation(conversation_id: ConversationId, body: dict[str, Any] = Body(...)):
    """归档或取消归档：{"is_archived": true|false}。"""
    if set(body) != {"is_archived"} or not isinstance(body["is_archived"], bool):
        return JSONResponse(status_code=400, content={"error": {"message": 'body must be {"is_archived": true|false}'}})
    status, data = await _backend("PATCH", f"/backend-api/conversation/{conversation_id}", body=body)
    return _error(status, data) if status >= 400 else {"id": conversation_id, **body}


@router.delete("/v1/conversations/{conversation_id}")
async def delete_conversation(conversation_id: ConversationId):
    """删除对话（从历史记录中消失，ChatGPT 不提供恢复）。"""
    status, data = await _backend("PATCH", f"/backend-api/conversation/{conversation_id}", body={"is_visible": False})
    return _error(status, data) if status >= 400 else {"id": conversation_id, "deleted": True}

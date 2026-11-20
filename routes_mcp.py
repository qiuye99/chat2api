"""POST /mcp：极简 MCP 服务，供 AI agent 直接调用 ChatGPT。

Streamable HTTP 传输的无状态子集：每个 JSON-RPC 请求直接返回 JSON（不使用 SSE、不分配会话），
支持 initialize、ping、tools/list、tools/call，无需额外依赖。
"""

import json
import re
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from routes_chat import CONVERSATION_ID_PATTERN, MODES, _complete_chat
from routes_conversations import list_conversations, read_conversation
from routes_session import LOCAL_HOSTS, create_link
from web_session import AuthError, call_with_renew

router = APIRouter()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Ask ChatGPT through the account's web subscription and manage its chats. "
                "Pass conversation_id from a previous answer to continue that chat. "
                "If a tool fails with session_expired, call chatgpt_session_link and give the URL to the user.")
CONVERSATION_ID = {"type": "string", "pattern": CONVERSATION_ID_PATTERN, "description": "ChatGPT conversation id"}
TOOLS = [
    {"name": "chatgpt_ask", "description": "Send a prompt to ChatGPT and return the answer with its conversation_id "
                                           "and conversation_url. Thinking models can take several minutes.",
     "inputSchema": {"type": "object", "required": ["prompt"], "properties": {
         "prompt": {"type": "string"},
         "conversation_id": {**CONVERSATION_ID, "description": "Continue this chat instead of starting a new one"},
         "model": {"type": "string", "description": "ChatGPT model slug, e.g. gpt-4o (default: WEB_CHAT_MODEL, "
                                                    "in Work mode WEB_WORK_MODEL or the account's default Work model)"},
         "mode": {"type": "string", "enum": list(MODES), "description": "chat or work (ChatGPT's Work mode, which can use "
                                                                        "tools and publish shareable artifacts) for a new "
                                                                        "chat (default: WEB_DEFAULT_MODE); a continued chat "
                                                                        "keeps its own mode"},
     }}},
    {"name": "chatgpt_list_conversations", "description": "List the account's chats, newest first, or search them with q.",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string", "description": "Search titles and message text"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
     }}, "annotations": {"readOnlyHint": True}},
    {"name": "chatgpt_read_conversation", "description": "Read the visible user and assistant messages of a chat, its mode "
                                                         "and the links of the artifacts ChatGPT published in it.",
     "inputSchema": {"type": "object", "required": ["conversation_id"], "properties": {"conversation_id": CONVERSATION_ID}},
     "annotations": {"readOnlyHint": True}},
    {"name": "chatgpt_session_link", "description": "Create a one-time URL (valid 30 minutes) where the user pastes a new "
                                                    "ChatGPT session after session_expired. Works only when called from localhost.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _rpc(msg_id: Any, result: Any = None, error: tuple[int, str] | None = None, status: int = 200) -> JSONResponse:
    content = {"jsonrpc": "2.0", "id": msg_id}
    content.update({"error": {"code": error[0], "message": error[1]}} if error else {"result": result})
    return JSONResponse(status_code=status, content=content)


def _conversation_id(value: Any) -> str:
    if not isinstance(value, str) or not re.match(CONVERSATION_ID_PATTERN, value):
        raise ValueError("invalid conversation_id")
    return value


async def _ask(args: dict[str, Any]) -> str | Response:
    if not isinstance(args.get("prompt"), str) or not args["prompt"].strip():
        raise ValueError("prompt is required")
    body: dict[str, Any] = {"messages": [{"role": "user", "content": args["prompt"]}]}
    if args.get("conversation_id"):
        body["conversation_id"] = _conversation_id(args["conversation_id"])
    if args.get("mode"):
        if args["mode"] not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        body["mode"] = args["mode"]
    if args.get("model"):
        body["model"] = args["model"]
    resp = await call_with_renew(lambda token: _complete_chat(token, body))
    if resp.status_code >= 400:
        return resp
    data = json.loads(resp.body)
    return (f"{data['choices'][0]['message']['content']}\n\n---\n"
            f"conversation_id: {data['conversation_id']}\nconversation_url: {data['conversation_url']}\nmode: {data['mode']}")


async def _call_tool(name: str, args: dict[str, Any], request: Request) -> dict[str, Any]:
    try:
        if name == "chatgpt_ask":
            data = await _ask(args)
        elif name == "chatgpt_list_conversations":
            data = await list_conversations(q=args.get("q") or "", limit=min(max(int(args.get("limit") or 20), 1), 100))
        elif name == "chatgpt_read_conversation":
            data = await read_conversation(_conversation_id(args.get("conversation_id")))
        else:
            data = await create_link(request)
    except AuthError as e:
        data = e.response()
    except Exception as e:
        data = JSONResponse(status_code=500, content={"error": {"message": f"{type(e).__name__}: {e}"}})
    is_error = isinstance(data, Response) and data.status_code >= 400
    if isinstance(data, Response):
        data = json.loads(data.body)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


@router.post("/mcp")
async def mcp(request: Request):
    # MCP 规范要求校验 Origin，防止 DNS rebinding：只接受无 Origin（非浏览器客户端）或本机来源
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in ("localhost", *LOCAL_HOSTS):
        return JSONResponse(status_code=403, content={"error": {"message": "origin not allowed"}})
    try:
        msg = await request.json()
    except ValueError:
        return _rpc(None, error=(-32700, "Parse error"), status=400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc(None, error=(-32600, "Invalid Request (batches are not supported)"), status=400)
    if "method" not in msg or "id" not in msg:
        return Response(status_code=202)  # 通知和响应无需回复

    msg_id, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if not isinstance(params, dict) or not isinstance(params.get("arguments") or {}, dict):
        return _rpc(msg_id, error=(-32602, "Invalid params"))
    if method == "initialize":
        version = params.get("protocolVersion")
        return _rpc(msg_id, {
            "protocolVersion": version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "chat2api", "version": request.app.version},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _rpc(msg_id, {})
    if method == "tools/list":
        return _rpc(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in TOOLS}:
            return _rpc(msg_id, error=(-32602, f"Unknown tool: {name}"))
        return _rpc(msg_id, await _call_tool(name, params.get("arguments") or {}, request))
    return _rpc(msg_id, error=(-32601, f"Method not found: {method}"))


@router.api_route("/mcp", methods=["GET", "DELETE"])
async def mcp_not_allowed():
    """无状态服务：不提供 SSE 流，也没有可删除的会话。"""
    return Response(status_code=405, headers={"Allow": "POST"})

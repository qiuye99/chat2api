"""POST /v1/chat/completions — 通过 ChatGPT Web 端实现 Chat Completions。"""

import json
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import settings
from http_client import build_session
from web_fingerprint import WebFingerprint, timezone_fields
from web_proof import build_legacy_requirements_token, build_proof_token
from web_session import AuthError, call_with_renew, check_auth, has_session
from web_sse_parser import _CONVERSATION_ID_RE

router = APIRouter()

CONVERSATION_ID_PATTERN = r"^[0-9A-Za-z-]{1,64}$"
MODES = ("chat", "work")
WORK_ORIGIN = "tpp"  # ChatGPT Work 模式对话的 conversation_origin


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """通过 ChatGPT Web 端实现 OpenAI 兼容的 chat completions。"""
    if not settings.chatgpt_access_token.strip() and not has_session():
        return JSONResponse(status_code=500, content={"error": {"message": "CHATGPT_ACCESS_TOKEN not configured"}})

    body: dict[str, Any] = await request.json()
    if "messages" not in body:
        return JSONResponse(status_code=400, content={"error": {"message": "messages is required"}})
    if body.get("conversation_id") and not re.match(CONVERSATION_ID_PATTERN, str(body["conversation_id"])):
        return JSONResponse(status_code=400, content={"error": {"message": "invalid conversation_id"}})

    if body.get("mode") not in (None, *MODES):
        return JSONResponse(status_code=400, content={"error": {"message": f"mode must be one of {', '.join(MODES)}"}})
    stream = body.get("stream", False)

    try:
        if stream:
            first, chunks = await call_with_renew(lambda token: _start_stream(token, body))
            return StreamingResponse(
                _resume_stream(first, chunks),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        return await call_with_renew(lambda token: _complete_chat(token, body))
    except AuthError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": {"message": str(e)}})


async def _complete_chat(token: str, body: dict[str, Any]) -> JSONResponse:
    """非流式 chat completion。"""
    fp = WebFingerprint()
    base = settings.chatgpt_base_url.rstrip("/")

    async with build_session() as session:
        # 获取 requirements
        reqs = await _get_requirements(session, fp, base, token)

        # 准备对话（带 conversation_id 时续写已有对话，并沿用该对话的模式）
        conversation_id, parent_id, chat_mode = await _continuation(session, fp, base, token, body.get("conversation_id"))
        mode = chat_mode or body.get("mode") or settings.web_default_mode
        model = body.get("model") or await _default_model(session, fp, base, token, mode)
        conduit = await _prepare_conversation(session, fp, base, token, model, reqs, conversation_id, parent_id, mode)

        # 发起对话
        messages = body.get("messages", [])
        prompt = _messages_to_prompt(messages)

        path = "/backend-api/f/conversation"
        conv_body = _build_conversation_body(model, prompt, conversation_id, parent_id, mode)
        headers = fp.image_headers(token, path, reqs["token"], reqs["proof_token"], reqs["so_token"], conduit=conduit, accept="text/event-stream")

        resp = await session.post(f"{base}{path}", json=conv_body, headers=headers)
        check_auth(resp)
        if resp.status_code >= 400:
            return JSONResponse(status_code=resp.status_code, content={"error": {"message": f"upstream {resp.status_code}: {resp.text[:300]}"}})

        # 解析完整响应
        content = _parse_chat_response(resp.text)
        conversation_id = _conversation_id(resp.text)

        # 粗略估算 token
        prompt_tokens = len(prompt) // 4 + 1
        completion_tokens = len(content) // 4 + 1

        return JSONResponse(status_code=200, content={
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "mode": mode,
            **_conversation_fields(base, conversation_id),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
        })


async def _stream_chat(token: str, body: dict[str, Any]):
    """流式 chat completion — 先获取完整响应，再逐步输出给客户端。"""
    fp = WebFingerprint()
    base = settings.chatgpt_base_url.rstrip("/")

    async with build_session() as session:
        # 获取 requirements
        reqs = await _get_requirements(session, fp, base, token)

        # 准备对话（带 conversation_id 时续写已有对话，并沿用该对话的模式）
        conversation_id, parent_id, chat_mode = await _continuation(session, fp, base, token, body.get("conversation_id"))
        mode = chat_mode or body.get("mode") or settings.web_default_mode
        model = body.get("model") or await _default_model(session, fp, base, token, mode)
        conduit = await _prepare_conversation(session, fp, base, token, model, reqs, conversation_id, parent_id, mode)

        # 发起对话
        messages = body.get("messages", [])
        prompt = _messages_to_prompt(messages)

        path = "/backend-api/f/conversation"
        conv_body = _build_conversation_body(model, prompt, conversation_id, parent_id, mode)
        headers = fp.image_headers(token, path, reqs["token"], reqs["proof_token"], reqs["so_token"], conduit=conduit, accept="text/event-stream")

        resp = await session.post(f"{base}{path}", json=conv_body, headers=headers)
        check_auth(resp)
        if resp.status_code >= 400:
            error_chunk = json.dumps({"error": {"message": f"upstream {resp.status_code}: {resp.text[:300]}"}})
            yield f"data: {error_chunk}\n\n"
            return

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        # 解析完整 SSE，收集所有增量内容
        last_content = ""
        chunks: list[str] = []

        for line in resp.text.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            new_content = _extract_content_from_web_chunk(payload)
            if new_content and len(new_content) > len(last_content):
                delta = new_content[len(last_content):]
                last_content = new_content
                chunks.append(delta)

        # 逐 chunk 输出给客户端
        for delta in chunks:
            chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        # finish
        finish_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            **_conversation_fields(base, _conversation_id(resp.text)),
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(finish_chunk)}\n\n"
        yield "data: [DONE]\n\n"


async def _start_stream(token: str, body: dict[str, Any]):
    """先取第一个 chunk：上游请求都在这之前完成，Token 过期时还能续期重试。"""
    chunks = _stream_chat(token, body)
    return await anext(chunks), chunks


async def _resume_stream(first: str, chunks):
    yield first
    async for chunk in chunks:
        yield chunk


async def _get_requirements(session, fp: WebFingerprint, base: str, token: str) -> dict[str, str]:
    """获取 chat requirements。"""
    path = "/backend-api/sentinel/chat-requirements"
    body = {"p": build_legacy_requirements_token(fp.user_agent)}
    headers = fp.base_headers(token, path)
    headers["Content-Type"] = "application/json"

    resp = await session.post(f"{base}{path}", json=body, headers=headers)
    check_auth(resp)
    if resp.status_code >= 400:
        raise Exception(f"Requirements failed: {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    req_token = data.get("token", "")
    if not req_token:
        raise Exception("Requirements missing token")

    proof = ""
    pow_data = data.get("proofofwork", {})
    if pow_data.get("required") and pow_data.get("seed") and pow_data.get("difficulty"):
        proof = build_proof_token(pow_data["seed"], pow_data["difficulty"], fp.user_agent)

    return {"token": req_token, "proof_token": proof, "so_token": data.get("so_token", "")}


async def _continuation(session, fp: WebFingerprint, base: str, token: str,
                        conversation_id: str | None) -> tuple[str | None, str, str | None]:
    """续写已有对话时，返回对话 id、其最新消息 id（新一轮的 parent）和对话的模式。"""
    if not conversation_id:
        return None, "client-created-root", None
    path = f"/backend-api/conversation/{conversation_id}"
    resp = await session.get(f"{base}{path}", headers=fp.base_headers(token, path))
    check_auth(resp)
    if resp.status_code >= 400:
        raise Exception(f"Conversation lookup failed: {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return conversation_id, data["current_node"], "work" if data.get("conversation_origin") == WORK_ORIGIN else "chat"


async def _default_model(session, fp: WebFingerprint, base: str, token: str, mode: str) -> str:
    """chat 模式用 WEB_CHAT_MODEL；work 模式用 WEB_WORK_MODEL，未配置时用账号的默认 Work 模型。"""
    if mode == "chat":
        return settings.web_chat_model
    if settings.web_work_model:
        return settings.web_work_model
    path = "/backend-api/tpp/models/"
    resp = await session.get(f"{base}{path}", headers=fp.base_headers(token, path))
    check_auth(resp)
    if resp.status_code >= 400:
        raise Exception(f"Work models lookup failed: {resp.status_code}: {resp.text[:200]}")
    return resp.json()["default_model_slug"]


def _mode_fields(mode: str) -> dict[str, Any]:
    return {"conversation_origin": WORK_ORIGIN} if mode == "work" else {}


def _conversation_ref(conversation_id: str | None, parent_id: str) -> dict[str, Any]:
    ref: dict[str, Any] = {"parent_message_id": parent_id}
    if conversation_id:
        ref["conversation_id"] = conversation_id
    return ref


def _conversation_id(sse_text: str) -> str:
    match = _CONVERSATION_ID_RE.search(sse_text)
    return match.group(1) if match else ""


def _conversation_fields(base: str, conversation_id: str) -> dict[str, str]:
    """响应中附带的对话 id 和网页地址，可用于续写或在浏览器中打开。"""
    return {"conversation_id": conversation_id, "conversation_url": f"{base}/c/{conversation_id}" if conversation_id else ""}


async def _prepare_conversation(session, fp: WebFingerprint, base: str, token: str, model: str, reqs: dict[str, str],
                                conversation_id: str | None = None, parent_id: str = "client-created-root",
                                mode: str = "chat") -> str:
    """准备对话获取 conduit token。"""
    path = "/backend-api/f/conversation/prepare"
    body = {
        "action": "next",
        "fork_from_shared_post": False,
        **_conversation_ref(conversation_id, parent_id),
        "model": model,
        "client_prepare_state": "none",
        **timezone_fields(),
        **_mode_fields(mode),
        "conversation_mode": {"kind": "primary_assistant"},
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {"app_name": "chatgpt.com"},
        "thinking_effort": "standard",
    }
    headers = fp.image_headers(token, path, reqs["token"], reqs["proof_token"], reqs["so_token"], accept="*/*")
    resp = await session.post(f"{base}{path}", json=body, headers=headers)
    check_auth(resp)
    if resp.status_code >= 400:
        raise Exception(f"Prepare failed: {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    conduit = data.get("conduit_token", "")
    if not conduit:
        raise Exception("Prepare missing conduit token")
    return conduit


def _build_conversation_body(model: str, prompt: str, conversation_id: str | None = None,
                             parent_id: str = "client-created-root", mode: str = "chat") -> dict[str, Any]:
    """构造对话请求体。"""
    return {
        "action": "next",
        "fork_from_shared_post": False,
        **_conversation_ref(conversation_id, parent_id),
        "model": model,
        "client_prepare_state": "success",
        **timezone_fields(),
        **_mode_fields(mode),
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True,
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": [],
        "client_contextual_info": {
            "is_dark_mode": False,
            "time_since_loaded": 30,
            "page_height": 1111,
            "page_width": 1731,
            "pixel_ratio": 1.5,
            "screen_height": 1440,
            "screen_width": 2560,
            "app_name": "chatgpt.com",
        },
        "paragen_cot_summary_display_override": "allow",
        "force_parallel_switch": "auto",
        "thinking_effort": "standard",
        "messages": [{
            "id": str(uuid.uuid4()),
            "author": {"role": "user"},
            "create_time": int(time.time()),
            "content": {"content_type": "text", "parts": [prompt]},
            "metadata": {
                "developer_mode_connector_ids": [],
                "selected_github_repos": [],
                "selected_all_github_repos": False,
                "serialization_metadata": {"custom_symbol_offsets": []},
            },
        }],
    }


def _messages_to_prompt(messages: list[dict]) -> str:
    """将 OpenAI messages 格式转为单个 prompt 文本。"""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = "\n".join(text_parts)
        if content:
            if role == "system":
                parts.append(f"[System]: {content}")
            elif role == "assistant":
                parts.append(f"[Assistant]: {content}")
            else:
                parts.append(content)
    return "\n\n".join(parts)


def _parse_chat_response(sse_text: str) -> str:
    """解析 ChatGPT Web SSE 响应，提取最终文本内容。"""
    last_content = ""
    for line in sse_text.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        content = _extract_content_from_web_chunk(payload)
        # 内容是累积的，取最长的那个
        if content and len(content) >= len(last_content):
            last_content = content
    return last_content


def _extract_content_from_web_chunk(payload: str) -> str:
    """从 ChatGPT Web SSE chunk 中提取文本内容。"""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return ""

    if not isinstance(data, dict):
        return ""

    # 新格式: {"v": {"message": {...}}} 或 {"p": "", "o": "add", "v": {"message": {...}}}
    msg = data.get("message")
    if not isinstance(msg, dict):
        v = data.get("v")
        if isinstance(v, dict):
            msg = v.get("message")
    if not isinstance(msg, dict):
        return ""

    author = msg.get("author")
    if not isinstance(author, dict) or author.get("role") != "assistant":
        return ""
    # Work 模式会先在 commentary 频道输出进度说明，最终回答在其后
    if msg.get("channel") == "commentary":
        return ""

    content = msg.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts", [])
    # 返回最后一个字符串 part（即使为空也返回，因为内容是累积的）
    for part in reversed(parts):
        if isinstance(part, str):
            return part
    return ""

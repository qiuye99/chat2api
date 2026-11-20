---
name: chat2api
description: Ask ChatGPT (the web app, on the account's Plus/Pro subscription) through a local chat2api gateway, continue earlier chats, list, search and read the account's chat history, and recover when the ChatGPT session expires. Use when the user wants a second opinion from ChatGPT, wants to continue or look up a ChatGPT chat, or a chat2api call fails with session_expired.
---

# chat2api

chat2api runs on the user's machine (default `http://127.0.0.1:8700`) and talks to chatgpt.com with the user's own session. Every prompt creates or extends a real chat in their ChatGPT history, so do not send secrets the user would not paste into ChatGPT themselves, and do not delete chats unless asked.

## MCP (preferred)

If the gateway is registered as an MCP server (streamable HTTP, URL `http://127.0.0.1:8700/mcp`), use its tools:

- `chatgpt_ask(prompt, conversation_id?, model?, mode?)`: returns the answer followed by `conversation_id`, `conversation_url` and `mode`. Pass `conversation_id` to continue the same chat; ChatGPT keeps the context, so send only the new question, and the chat keeps its mode. `mode` is `chat` or `work` (ChatGPT's Work mode: it can use tools and publish shareable artifacts such as small web pages) and only matters for a new chat. Thinking models and Work tasks can take several minutes, so allow a long tool timeout.
- `chatgpt_list_conversations(q?, limit?)`: newest chats first; `q` uses ChatGPT's own search over titles and message text.
- `chatgpt_read_conversation(conversation_id)`: the visible user and assistant messages, the chat's `mode` and `artifacts` (links of what ChatGPT published in it). Give the user an artifact link from there if the answer did not include it.
- `chatgpt_session_link()`: a one-time URL for replacing a dead session (see below).

Example client configuration:

```json
{"mcpServers": {"chatgpt": {"type": "http", "url": "http://127.0.0.1:8700/mcp"}}}
```

## HTTP API

```bash
# Ask; the response carries conversation_id and conversation_url
curl -s http://127.0.0.1:8700/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Question"}]}'

# Work mode for a new chat (the model defaults to the account's default Work model)
curl -s http://127.0.0.1:8700/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"mode":"work","messages":[{"role":"user","content":"Task"}]}'

# Continue that chat: send only the new message
curl -s http://127.0.0.1:8700/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","conversation_id":"<id>","messages":[{"role":"user","content":"Follow-up"}]}'

curl -s "http://127.0.0.1:8700/v1/conversations?limit=10"
curl -s "http://127.0.0.1:8700/v1/conversations?q=keyword"
curl -s http://127.0.0.1:8700/v1/conversations/<id>
```

Show the user `conversation_url` when they may want to open the chat in their browser.

## When the session expires

The gateway renews the short-lived access token by itself. Only when the long-lived session cookie is dead too does a call fail with HTTP 401 and `"code": "session_expired"` (in MCP: a tool result with `isError: true` and the same code). Then:

1. Call `chatgpt_session_link` (or `curl -s -X POST http://127.0.0.1:8700/v1/session/link` on the gateway host). This works only from the gateway host itself.
2. Give the returned `url` to the user and ask them to open it in a browser where they are logged in to ChatGPT, then follow the page: open `https://chatgpt.com/api/auth/session`, copy all of it and paste it into the page. The link is valid for 30 minutes and works once.
3. When the user confirms the page said the session was saved, retry the failed call. No restart is needed.

Never ask the user to paste the session JSON, the access token or the cookie into the chat: they are credentials and belong only in the refresh page.

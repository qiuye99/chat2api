<div align="center">

# ChatGPT2API

**将 ChatGPT 网页端能力转为 OpenAI 兼容 API**

通过 Access Token 调用 ChatGPT，走订阅额度，无需 API Key 付费。
任何 OpenAI SDK 可直接对接。

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

</div>

---

## ✨ 特性

- 🔄 **OpenAI 兼容** — 标准 `/v1/chat/completions` 和 `/v1/images/generations` 接口
- 🖼️ **图片生成** — 支持 gpt-image-2，图片保存本地并返回可访问 URL
- 🌊 **流式输出** — 支持 SSE 流式对话
- 🛡️ **绕过 Cloudflare** — 使用 curl_cffi 模拟 Chrome TLS 指纹
- 🧮 **自动 PoW** — 自动计算 Proof-of-Work token 通过反滥用验证
- 🌐 **代理支持** — HTTP / HTTPS / SOCKS5
- 🚀 **零成本** — 走 ChatGPT Plus/Pro 订阅额度，无额外 API 费用

## 📦 快速开始

### 1. 安装

```bash
git clone https://github.com/yourname/chatgpt2api.git
cd chatgpt2api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`：

```env
# 必填：ChatGPT Access Token
CHATGPT_ACCESS_TOKEN=eyJhbGciOi...

# 必填：代理地址（需能访问 chatgpt.com）
PROXY_URL=http://127.0.0.1:7890
```

### 3. 获取 Access Token

1. 浏览器登录 [chatgpt.com](https://chatgpt.com)
2. 访问 [chatgpt.com/api/auth/session](https://chatgpt.com/api/auth/session)
3. 复制 JSON 中的 `accessToken` 字段

> Token 有效期约 10 天，过期后重新获取替换即可。也可以同时填写 JSON 中的 `sessionToken`，见下方[自动续期](#-自动续期--automatic-token-renewal)。

### 4. 启动

```bash
python main.py
```

服务监听 `http://0.0.0.0:8700`，API 文档：`http://localhost:8700/docs`

## 🔌 使用

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    api_key="anything",  # 随便填
    base_url="http://localhost:8700/v1",
)

# 对话
resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)

# 流式对话
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "写一首诗"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")

# 生成图片
resp = client.images.generate(
    model="gpt-image-2",
    prompt="一只戴着礼帽的橘猫，水彩画风格",
    size="1024x1024",
)
print(resp.data[0].url)
# → http://localhost:8700/images/xxxx.png
```

### cURL

```bash
# 对话
curl http://localhost:8700/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'

# 流式
curl http://localhost:8700/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}],"stream":true}'

# 生成图片
curl http://localhost:8700/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-image-2","prompt":"a cute cat","size":"1024x1024"}'
```

### Node.js

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "anything",
  baseURL: "http://localhost:8700/v1",
});

const resp = await client.chat.completions.create({
  model: "gpt-4o",
  messages: [{ role: "user", content: "hello" }],
});
console.log(resp.choices[0].message.content);
```

## 🔑 自动续期 / Automatic token renewal

在 `.env` 中填写 `CHATGPT_SESSION_TOKEN`（[chatgpt.com/api/auth/session](https://chatgpt.com/api/auth/session) 返回 JSON 中的 `sessionToken`，即 `__Secure-next-auth.session-token` cookie）后：

- chatgpt.com 返回 401 / `token_expired` 时，网关用 cookie 换取新的 Access Token 并自动重试一次，调用方无感知；
- cookie 每次续期都会轮换，新值连同 Token 保存到 `SESSION_FILE`（默认 `session.json`，权限 600），启动时优先于 `.env`。手动更换 `.env` 中的凭据时请删除该文件；
- cookie 也失效时（例如退出登录或很久未使用），接口返回 HTTP 401：`{"error": {"code": "session_expired", ...}}`。

cookie 失效后无需登录服务器修改 `.env`：在服务器本机生成一次性刷新链接，在任意已登录 ChatGPT 的浏览器（包括手机）中打开，按页面提示粘贴 `chatgpt.com/api/auth/session` 的内容即可，无需重启：

```bash
curl -X POST http://127.0.0.1:8700/v1/session/link
# {"url": "http://127.0.0.1:8700/chatgpt-session/<token>", "expires_at": "..."}
```

链接 30 分钟内有效，成功提交一次后失效；生成链接的接口只接受本机直连请求（带 `X-Forwarded-For` 等代理头的请求会被拒绝）。若通过反向代理对外提供刷新页面，请设置 `SESSION_REFRESH_BASE_URL`，并只转发 `/chatgpt-session/` 路径。

Set `CHATGPT_SESSION_TOKEN` in `.env` (the `sessionToken` field of [chatgpt.com/api/auth/session](https://chatgpt.com/api/auth/session), which is the `__Secure-next-auth.session-token` cookie) and the gateway renews the access token by itself:

- When chatgpt.com answers 401 / `token_expired`, the gateway mints a new access token from the cookie and retries the request once.
- The cookie rotates on every renewal. The new cookie and token are saved to `SESSION_FILE` (default `session.json`, mode 600), which takes precedence over `.env` at startup. Delete it when you replace the credentials in `.env` by hand.
- When the cookie is dead too (logged out, or unused for a long time), requests fail with HTTP 401 `{"error": {"code": "session_expired", ...}}`.

To replace a dead session without editing `.env` or restarting, create a one-time link on the server (the `curl` above) and open it in any browser that is logged in to ChatGPT, including a phone. The page asks you to paste the content of `chatgpt.com/api/auth/session`, verifies it with chatgpt.com and switches to the new session. The link expires after 30 minutes and works once. Link creation only accepts direct requests from localhost and refuses requests carrying proxy headers such as `X-Forwarded-For`. If you publish the page through a reverse proxy, set `SESSION_REFRESH_BASE_URL` and forward only the `/chatgpt-session/` path.

## 🧪 测试

```bash
python -B test_client.py              # 全部测试
python -B test_client.py health       # 健康检查
python -B test_client.py chat         # 非流式对话
python -B test_client.py chat_stream  # 流式对话
python -B test_client.py image        # 图片生成
```

## ⚙️ 配置项

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CHATGPT_ACCESS_TOKEN` | Access Token（必填） | — |
| `PROXY_URL` | 出站代理（必填） | — |
| `CHATGPT_BASE_URL` | ChatGPT 地址 | `https://chatgpt.com` |
| `PORT` | 监听端口 | `8700` |
| `TIMEOUT` | 请求超时（秒） | `600` |
| `WEB_IMAGE_MODEL` | 图片生成模型 | `gpt-5-5-thinking` |
| `WEB_CHAT_MODEL` | 对话默认模型 | `gpt-4o` |
| `CHATGPT_SESSION_TOKEN` | Session Token，用于自动续期（可选） | — |
| `SESSION_FILE` | 续期后凭据的保存位置 | `session.json` |
| `SESSION_REFRESH_BASE_URL` | 会话刷新页面的对外地址（可选） | 请求地址 |

## 📁 项目结构

```
├── main.py              # 入口
├── config.py            # 配置加载
├── http_client.py       # curl_cffi 客户端（Chrome TLS 指纹）
├── routes_chat.py       # /v1/chat/completions（流式 + 非流式）
├── routes_images.py     # /v1/images/generations（图片保存本地）
├── routes_session.py    # 一次性会话刷新页面
├── web_session.py       # Access Token 自动续期
├── web_client.py        # Web 图片生成完整流程
├── web_fingerprint.py   # 浏览器指纹构造
├── web_proof.py         # Proof-of-Work 计算
├── web_sse_parser.py    # SSE 流解析
├── image_utils.py       # 尺寸/比例映射
├── test_client.py       # 测试脚本
└── images/              # 生成的图片
```

## 🔧 工作原理

```
调用方 (OpenAI SDK)
    │
    ▼
┌──────────────────────────────────┐
│  FastAPI (:8700)                 │
│  curl_cffi (Chrome TLS 指纹)    │
│  PoW Token 自动计算              │
└──────────────┬───────────────────┘
               │ via Proxy
               ▼
┌──────────────────────────────────┐
│  chatgpt.com 内部 API            │
│  /backend-api/f/conversation     │
└──────────────────────────────────┘
```

1. 接收 OpenAI 格式请求
2. 用 curl_cffi 模拟 Chrome 浏览器 TLS 指纹
3. 构造完整的浏览器 Headers（User-Agent、Sec-CH-UA、OAI-Device-Id 等）
4. 计算 Proof-of-Work token 通过反滥用验证
5. 调用 ChatGPT 内部 conversation API
6. 解析 SSE 响应，转换为 OpenAI 兼容格式返回

## ⚠️ 注意事项

- 需要代理才能访问 chatgpt.com（国内直连被 Cloudflare 拦截）
- Access Token 约 10 天过期，需定期更新
- 图片生成耗时 30-60 秒，这是 ChatGPT 本身的速度
- Free 账号有使用限制，Plus/Pro 额度更充裕
- 本项目仅供学习研究，请遵守 OpenAI 使用条款

## 📄 License

MIT

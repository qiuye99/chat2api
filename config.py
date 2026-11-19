"""应用配置，从环境变量 / .env 文件加载。"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ChatGPT Access Token（从 https://chatgpt.com/api/auth/session 获取）
    chatgpt_access_token: str = ""
    chatgpt_base_url: str = "https://chatgpt.com"
    # 可选：ChatGPT session cookie（/api/auth/session 中的 sessionToken），用于 Access Token 过期后自动续期
    chatgpt_session_token: str = ""
    # 续期后的 Token 和轮换后的 cookie 保存在这里，启动时优先于 .env
    session_file: str = "session.json"
    # 会话刷新页面的对外地址（如 https://gpt.example.com），留空则使用请求地址
    session_refresh_base_url: str = ""

    # 出站代理（可选，支持 http/https/socks5）
    proxy_url: str = ""

    # 服务
    port: int = 8700
    timeout: int = 600  # 秒

    # Web 模式图片生成默认模型
    web_image_model: str = "gpt-5-5-thinking"
    # Web 模式 chat 默认模型
    web_chat_model: str = "gpt-4o"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

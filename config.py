"""应用配置，从环境变量 / .env 文件加载。"""

from zoneinfo import ZoneInfo

from pydantic import field_validator
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
    # 新对话的模式：chat（普通对话）或 work（ChatGPT 的 Work 模式），请求中可用 "mode" 单独指定
    web_default_mode: str = "chat"
    # Work 模式默认模型，留空则使用账号的默认 Work 模型
    web_work_model: str = ""

    # 发给 ChatGPT 的时区（IANA 名称，影响回答中的"今天"）和语言（影响回复及思考过程的语言）
    chatgpt_timezone: str = "Asia/Shanghai"
    chatgpt_language: str = "zh-CN"

    @field_validator("chatgpt_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # 无效时区启动时即报错
        return value

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

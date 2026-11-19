"""应用配置，从环境变量 / .env 文件加载。"""

from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ChatGPT Access Token（从 https://chatgpt.com/api/auth/session 获取）
    chatgpt_access_token: str = ""
    chatgpt_base_url: str = "https://chatgpt.com"

    # 出站代理（可选，支持 http/https/socks5）
    proxy_url: str = ""

    # 服务
    port: int = 8700
    timeout: int = 600  # 秒

    # Web 模式图片生成默认模型
    web_image_model: str = "gpt-5-5-thinking"
    # Web 模式 chat 默认模型
    web_chat_model: str = "gpt-4o"

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

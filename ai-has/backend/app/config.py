from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: str = ""
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    chat_model: str = "gemini-3.8-flash"
    embed_model: str = "gemini-embedding-001"
    embed_dimensions: int = 768
    database_url: str = ""
    n8n_webhook_url: str = "http://localhost:5678/webhook"
    n8n_webhook_secret: str = ""
    n8n_timeout_seconds: float = 5.0
    rate_limit_enabled: bool = True
    rate_limit_chat_per_min: int = 20
    rate_limit_tickets_per_min: int = 10
    rate_limit_actions_per_min: int = 20
    session_secret: str = ""
    session_hours: float = 4.0
    cookie_secure: bool = False
    demo_password: str = ""
    persist_chats: bool = True
    login_max_failures_per_email: int = 5
    login_max_failures_per_ip: int = 20
    login_window_minutes: int = 10


settings = Settings()
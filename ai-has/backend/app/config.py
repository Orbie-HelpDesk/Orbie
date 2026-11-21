from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: str = ""
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    chat_model: str = "gemini-3.8-flash"
    chat_model_fallback: str = ""  # optional second model, tried once if the main one fails
    embed_model: str = "gemini-embedding-001"
    embed_dimensions: int = 768
    database_url: str = ""
    chat_timeout_seconds: float = 30.0  # one chat attempt. A slow model may need more
    embed_timeout_seconds: float = 20.0  # one embedding attempt
    llm_call_budget_seconds: float = 45.0  # all attempts of one logical call, retries included
    intent_confidence_threshold: float = 0.75  # below this the rules are not sure enough
    history_turns: int = 4  # earlier exchanges (a question and its answer) sent with a question. 0 turns memory off
    intent_use_model: bool = True  # one small model call when the rules are not sure
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
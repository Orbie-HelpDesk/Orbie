from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: str = ""
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    chat_model: str = "gemini-3.8-flash"
    embed_model: str = "gemini-embedding-001"
    embed_dimensions: int = 768
    database_url: str = ""
    n8n_webhook_url: str = "http://localhost:5678/webhook"
    rate_limit_enabled: bool = True
    rate_limit_chat_per_min: int = 20
    rate_limit_tickets_per_min: int = 10


settings = Settings()
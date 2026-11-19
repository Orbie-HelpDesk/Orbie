from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_chat_model: str = "gpt-4o-mini"
    openai_embed_model: str = "text-embedding-3-small"
    database_url: str = "postgresql+psycopg2://aihas:aihas@localhost:5432/aihas"
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    n8n_webhook_url: str = "http://localhost:5678/webhook"

    class Config:
        env_file = ".env"


settings = Settings()
